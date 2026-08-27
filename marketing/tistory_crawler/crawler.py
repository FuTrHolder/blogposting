"""
티스토리 블로그 크롤러

기능:
- RSS 피드로 새 글 감지
- BeautifulSoup으로 실제 본문 파싱
- 상태 파일로 마케팅 중복 발행 방지
- 최근 포스트 목록 조회
- 연관 포스트 후보 수집
- TOC 및 연관 포스트 영역을 본문 텍스트에서 제거
- 오전/저녁 모드 판별

중요:
- get_latest_post()는 기존 마케팅 중복 방지용으로 상태 파일을 변경합니다.
- get_recent_posts()는 연관 포스트 검색용 읽기 전용 메서드입니다.
  상태 파일을 절대 변경하지 않습니다.
"""

import feedparser
import requests
import json
import logging
import hashlib
import os
import html as _html_module
from datetime import datetime, timezone, timedelta
from bs4 import BeautifulSoup
from dataclasses import dataclass, asdict

logger = logging.getLogger(__name__)

BLOG_URL = "https://seedsup.tistory.com"
RSS_URL = f"{BLOG_URL}/rss"
STATE_FILE = "last_post_state.json"

KST = timezone(timedelta(hours=9))

# 오전/저녁 포스팅 판별 기준
_MODE_SPLIT_HOUR_KST = 15

# 연관 포스트 후보 기본 수
DEFAULT_RECENT_POST_LIMIT = 20


def _decode_html_entities(text: str) -> str:
    """
    RSS 제목/태그 등에 남아있는 HTML 엔티티를 반복해서 디코딩합니다.
    """
    if not text:
        return text

    prev = None

    while prev != text:
        prev = text
        text = _html_module.unescape(text)

    return text


def _strip_toc_markup(content_area: BeautifulSoup) -> None:
    """
    TOC UI 영역을 제거합니다.
    """
    for el in content_area.select(".index_toc"):
        el.decompose()

    for el in content_area.select("#toc"):
        el.decompose()


def _strip_related_posts_markup(content_area: BeautifulSoup) -> None:
    """
    본문 하단에 코드로 삽입한 '관련 포스트' 영역을 제거합니다.

    연관 포스트 자체는 실제 블로그 HTML에는 필요하지만,
    main_marketing.py에서 SNS/영상용 본문을 만들 때까지 가져가면
    불필요한 링크와 제목이 영상/캡션 생성에 섞일 수 있습니다.

    HTML 구조:
        <h2>관련 포스트</h2>
        <ul>
            <li><a href="...">...</a></li>
            ...
        </ul>

    또는:
        <h2>함께 보면 좋은 글</h2>
        <ul>...</ul>
    """

    target_headings = {
        "관련 포스트",
        "관련포스트",
        "함께 보면 좋은 글",
        "함께보면 좋은 글",
        "추천 포스트",
        "추천포스트",
    }

    headings = content_area.find_all(["h2", "h3"])

    for heading in headings:
        heading_text = heading.get_text(" ", strip=True)
        normalized = " ".join(heading_text.split())

        if normalized not in target_headings:
            continue

        # 제목 제거
        current = heading

        # 제목 다음에 이어지는 요소 중
        # 관련 포스트 목록을 찾아 제거합니다.
        next_element = current.find_next_sibling()

        heading.decompose()

        if next_element:
            tag_name = getattr(next_element, "name", None)

            if tag_name == "ul":
                next_element.decompose()

            elif tag_name == "ol":
                next_element.decompose()

            else:
                # 혹시 p 뒤에 ul이 오는 변형 구조라면
                # 다음 몇 개 sibling까지 검사합니다.
                for sibling in list(content_area.find_all(["ul", "ol"])):
                    if sibling.find_previous(["h2", "h3"]) is None:
                        continue

                    previous_heading = sibling.find_previous(["h2", "h3"])

                    if previous_heading is not None:
                        previous_text = previous_heading.get_text(
                            " ", strip=True
                        )
                        previous_text = " ".join(previous_text.split())

                        if previous_text in target_headings:
                            sibling.decompose()


@dataclass
class BlogPost:
    title: str
    url: str
    summary: str
    full_text: str
    thumbnail_url: str
    tags: list[str]
    published: str
    post_id: str
    mode: str


def _detect_mode(title: str, published_parsed=None) -> str:
    """
    RSS 발행 시각을 기준으로 morning/evening을 판별합니다.

    1순위:
        published_parsed → UTC → KST

    2순위:
        제목 키워드
    """

    if published_parsed:
        try:
            published_utc = datetime(
                *published_parsed[:6],
                tzinfo=timezone.utc,
            )

            published_kst = published_utc.astimezone(KST)

            mode = (
                "morning"
                if published_kst.hour < _MODE_SPLIT_HOUR_KST
                else "evening"
            )

            logger.info(
                "발행 시각 기준 모드 판별: "
                f"{published_kst.strftime('%Y-%m-%d %H:%M KST')} → {mode}"
            )

            return mode

        except Exception as e:
            logger.warning(
                f"발행 시각 파싱 실패, 제목 키워드로 대체 판별: {e}"
            )

    evening_keywords = [
        "프리마켓",
        "이슈",
        "저녁",
        "오늘 밤",
        "개장 전",
    ]

    for kw in evening_keywords:
        if kw in title:
            return "evening"

    return "morning"


def _extract_thumbnail(
    soup: BeautifulSoup,
    entry_summary: str,
) -> str:
    """
    본문에서 대표 이미지 URL을 추출합니다.
    """

    og = soup.find("meta", property="og:image")

    if og and og.get("content"):
        return og["content"]

    img = soup.find("img")

    if img and img.get("src"):
        return img["src"]

    summary_soup = BeautifulSoup(
        entry_summary,
        "html.parser",
    )

    img = summary_soup.find("img")

    if img and img.get("src"):
        return img["src"]

    return ""


def _load_state() -> dict:
    """
    기존 마케팅 중복 방지 상태를 읽습니다.
    """

    if os.path.exists(STATE_FILE):
        try:
            with open(
                STATE_FILE,
                "r",
                encoding="utf-8",
            ) as f:
                state = json.load(f)

            if not isinstance(state, dict):
                return {"last_post_ids": []}

            if not isinstance(
                state.get("last_post_ids"),
                list,
            ):
                state["last_post_ids"] = []

            return state

        except Exception as e:
            logger.warning(
                f"상태 파일 읽기 실패 — 초기 상태 사용: {e}"
            )

    return {"last_post_ids": []}


def _save_state(state: dict):
    """
    기존 마케팅 중복 방지 상태를 저장합니다.
    """

    with open(
        STATE_FILE,
        "w",
        encoding="utf-8",
    ) as f:
        json.dump(
            state,
            f,
            ensure_ascii=False,
            indent=2,
        )


class TistoryCrawler:

    def __init__(
        self,
        blog_url: str = BLOG_URL,
    ):
        self.blog_url = blog_url
        self.rss_url = f"{blog_url}/rss"

        self.session = requests.Session()

        self.session.headers.update(
            {
                "User-Agent": (
                    "Mozilla/5.0 "
                    "(compatible; BlogBot/1.0)"
                )
            }
        )

    def _load_feed(self):
        """
        RSS를 읽습니다.
        """

        try:
            feed = feedparser.parse(self.rss_url)

            if getattr(feed, "bozo", False):
                logger.warning(
                    f"RSS 파싱 경고: {getattr(feed, 'bozo_exception', '')}"
                )

            if not feed.entries:
                logger.warning(
                    "RSS 피드에 항목이 없습니다."
                )
                return None

            return feed

        except Exception as e:
            logger.error(
                f"RSS 조회 실패: {e}"
            )
            return None

    @staticmethod
    def _make_post_id(url: str) -> str:
        """
        URL 기반 안정적인 post_id를 생성합니다.
        """

        return hashlib.md5(
            url.encode("utf-8")
        ).hexdigest()[:12]

    def get_latest_post(
        self,
        force: bool = False,
    ) -> BlogPost | None:
        """
        RSS에서 최신 글 하나를 가져옵니다.

        force=True:
            상태 파일을 무시합니다.

        force=False:
            이미 처리된 글이면 None을 반환합니다.

        주의:
            기존 마케팅 중복 방지 기능 때문에
            성공적으로 파싱한 글은 상태 파일에 기록합니다.
        """

        feed = self._load_feed()

        if not feed:
            return None

        entry = feed.entries[0]

        url = entry.get("link", "").strip()

        if not url:
            logger.warning(
                "RSS 최신 항목에 URL이 없습니다."
            )
            return None

        post_id = self._make_post_id(url)

        if not force:
            state = _load_state()

            if post_id in state["last_post_ids"]:
                logger.info(
                    f"이미 처리된 글: "
                    f"{entry.get('title', '')}"
                )
                return None

        post = self._parse_post(
            entry,
            post_id,
        )

        if post:
            state = _load_state()

            previous_ids = state.get(
                "last_post_ids",
                [],
            )

            state["last_post_ids"] = (
                [post_id] + previous_ids
            )[:20]

            _save_state(state)

        return post

    def get_recent_posts(
        self,
        limit: int = DEFAULT_RECENT_POST_LIMIT,
        exclude_post_id: str | None = None,
        exclude_url: str | None = None,
    ) -> list[BlogPost]:
        """
        최근 포스트를 읽기 전용으로 가져옵니다.

        중요:
        - 상태 파일을 읽거나 변경하지 않습니다.
        - 마케팅 중복 실행 방지와 완전히 분리됩니다.
        - 연관 포스트 후보 수집용입니다.

        Args:
            limit:
                최대 후보 수.

            exclude_post_id:
                현재 포스트 ID.

            exclude_url:
                현재 포스트 URL.

        Returns:
            최신순 BlogPost 리스트
        """

        if limit <= 0:
            return []

        feed = self._load_feed()

        if not feed:
            return []

        posts: list[BlogPost] = []

        normalized_exclude_url = (
            (exclude_url or "").rstrip("/")
        )

        for entry in feed.entries[:limit + 10]:

            url = entry.get(
                "link",
                "",
            ).strip()

            if not url:
                continue

            normalized_url = url.rstrip("/")

            post_id = self._make_post_id(url)

            if exclude_post_id and post_id == exclude_post_id:
                continue

            if (
                normalized_exclude_url
                and normalized_url == normalized_exclude_url
            ):
                continue

            try:
                post = self._parse_post(
                    entry,
                    post_id,
                )

                if post:
                    posts.append(post)

            except Exception as e:
                logger.warning(
                    f"최근 포스트 파싱 실패 "
                    f"(url={url}): {e}"
                )

            if len(posts) >= limit:
                break

        logger.info(
            f"연관 포스트 후보 {len(posts)}개 수집 완료"
        )

        return posts

    def _parse_post(
        self,
        entry,
        post_id: str,
    ) -> BlogPost | None:
        """
        RSS 항목 + 실제 페이지 크롤링으로 BlogPost를 생성합니다.
        """

        title = _decode_html_entities(
            entry.get(
                "title",
                "",
            ).strip()
        )

        url = entry.get(
            "link",
            "",
        ).strip()

        published = entry.get(
            "published",
            "",
        )

        raw_tags = entry.get(
            "tags",
            [],
        )

        tags = []

        for tag in raw_tags:
            term = getattr(
                tag,
                "term",
                "",
            )

            if term:
                tags.append(
                    _decode_html_entities(
                        str(term)
                    )
                )

        if not url:
            return None

        logger.info(
            f"글 크롤링 중: {url}"
        )

        soup = None

        try:
            resp = self.session.get(
                url,
                timeout=15,
            )

            resp.raise_for_status()

            soup = BeautifulSoup(
                resp.text,
                "html.parser",
            )

        except Exception as e:
            logger.warning(
                f"페이지 크롤링 실패: {e}"
            )

            soup = BeautifulSoup(
                entry.get(
                    "summary",
                    "",
                ),
                "html.parser",
            )

        content_area = (
            soup.find(
                "div",
                class_="tt_article_useless_p_margin",
            )
            or soup.find(
                "div",
                {"id": "content"},
            )
            or soup.find("article")
            or soup.find(
                "div",
                class_="entry-content",
            )
        )

        if content_area:

            _strip_toc_markup(
                content_area
            )

            _strip_related_posts_markup(
                content_area
            )

            full_text = content_area.get_text(
                separator="\n",
                strip=True,
            )

        else:

            summary_soup = BeautifulSoup(
                entry.get(
                    "summary",
                    "",
                ),
                "html.parser",
            )

            _strip_toc_markup(
                summary_soup
            )

            _strip_related_posts_markup(
                summary_soup
            )

            full_text = summary_soup.get_text(
                separator="\n",
                strip=True,
            )

        lines = [
            line.strip()
            for line in full_text.split("\n")
            if line.strip()
        ]

        summary = " ".join(
            lines[:5]
        )[:300]

        thumbnail_url = _extract_thumbnail(
            soup,
            entry.get(
                "summary",
                "",
            ),
        )

        mode = _detect_mode(
            title,
            entry.get(
                "published_parsed"
            ),
        )

        return BlogPost(
            title=title,
            url=url,
            summary=summary,
            full_text=full_text[:4000],
            thumbnail_url=thumbnail_url,
            tags=tags,
            published=published,
            post_id=post_id,
            mode=mode,
        )

    def get_post_as_dict(
        self,
        force: bool = False,
    ) -> dict | None:

        post = self.get_latest_post(
            force=force
        )

        return asdict(post) if post else None

    def get_recent_posts_as_dict(
        self,
        limit: int = DEFAULT_RECENT_POST_LIMIT,
        exclude_post_id: str | None = None,
        exclude_url: str | None = None,
    ) -> list[dict]:
        """
        연관 포스트 검색용 최근 글 목록을 dict로 반환합니다.
        """

        posts = self.get_recent_posts(
            limit=limit,
            exclude_post_id=exclude_post_id,
            exclude_url=exclude_url,
        )

        return [
            asdict(post)
            for post in posts
        ]