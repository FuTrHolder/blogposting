"""
티스토리 블로그 크롤러
- RSS 피드로 새 글 감지 (최근 업로드된 글)
- BeautifulSoup으로 본문 파싱
- 상태 파일로 중복 발행 방지

모드 판별 방식 변경 (중요):
  기존에는 제목에 "프리마켓", "오늘 밤" 같은 키워드가 있는지로 morning/evening을
  추측했으나, 실제 생성된 제목이 그 키워드를 포함하지 않는 경우(예: "혼조세 마감,
  S&P500 0.5% 하락 속 불안한 투자 심리")에 저녁 포스팅이 "morning"으로 잘못
  판별되어, 같은 날짜의 마케팅 결과가 같은 ID(`${post_date}_morning_${platform}`)로
  겹쳐 써지며 아침 결과가 사라지는 문제가 있었습니다.
  → RSS의 실제 발행 시각(published_parsed, UTC)을 한국 시간(KST)으로 변환해
    오전/저녁 여부를 판별하는 방식으로 교체했습니다 (오전 9시 vs 저녁 9시 발행이라
    시각 기준이 제목 키워드보다 훨씬 신뢰도가 높습니다). 시각 파싱이 실패할 때만
    기존 제목 키워드 방식을 폴백으로 사용합니다.
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

# 오전/저녁 포스팅을 가르는 기준 시각 (KST). 실제 발행은 9시/21시 근처이므로
# 정오~오후 중 아무 지점이나 기준으로 잡아도 안전하게 갈립니다.
_MODE_SPLIT_HOUR_KST = 15


def _decode_html_entities(text: str) -> str:
    """
    RSS 제목/태그 등에 남아있는 HTML 엔티티(&amp; &lt; &#39; 등)를 실제 문자로
    변환합니다. feedparser가 일반적으로 엔티티를 디코딩하지만, 티스토리 RSS가
    이중 이스케이프(&amp;amp;)로 내보내는 경우가 있어 완전히 풀릴 때까지
    반복 적용합니다 (예: "S&amp;P 500" → "S&P 500").
    이 값이 title/tags에 남아있으면 YouTube 업로드 제목 등 하위 소비처
    전체에 "&amp;" 같은 깨진 텍스트가 그대로 노출됩니다.
    """
    if not text:
        return text
    prev = None
    while prev != text:
        prev = text
        text = _html_module.unescape(text)
    return text


@dataclass
class BlogPost:
    title: str
    url: str
    summary: str          # 첫 2~3문단 요약
    full_text: str        # 전체 본문 (마크다운 제거된 텍스트)
    thumbnail_url: str    # 대표 이미지 URL
    tags: list[str]
    published: str
    post_id: str          # URL 해시 (중복 방지용)
    mode: str             # morning / evening (발행 시각 기준 판별)


def _detect_mode(title: str, published_parsed=None) -> str:
    """
    포스팅 모드를 판별합니다.
    1순위: RSS의 published_parsed(UTC struct_time)를 KST로 변환해 시각으로 판별
           (09:00 KST 발행 → morning, 21:00 KST 발행 → evening)
    2순위(폴백): 시각 정보가 없거나 파싱 실패 시에만 제목 키워드로 판별
    """
    if published_parsed:
        try:
            published_utc = datetime(*published_parsed[:6], tzinfo=timezone.utc)
            published_kst = published_utc.astimezone(KST)
            mode = "morning" if published_kst.hour < _MODE_SPLIT_HOUR_KST else "evening"
            logger.info(
                f"발행 시각 기준 모드 판별: {published_kst.strftime('%Y-%m-%d %H:%M KST')} → {mode}"
            )
            return mode
        except Exception as e:
            logger.warning(f"발행 시각 파싱 실패, 제목 키워드로 대체 판별: {e}")

    evening_keywords = ["프리마켓", "이슈", "저녁", "오늘 밤", "개장 전"]
    for kw in evening_keywords:
        if kw in title:
            return "evening"
    return "morning"


def _extract_thumbnail(soup: BeautifulSoup, entry_summary: str) -> str:
    """본문에서 첫 번째 이미지 URL을 추출합니다."""
    # og:image 태그 우선
    og = soup.find("meta", property="og:image")
    if og and og.get("content"):
        return og["content"]

    # 본문 첫 번째 img 태그
    img = soup.find("img")
    if img and img.get("src"):
        return img["src"]

    # RSS 요약의 img 태그
    summary_soup = BeautifulSoup(entry_summary, "html.parser")
    img = summary_soup.find("img")
    if img and img.get("src"):
        return img["src"]

    return ""


def _load_state() -> dict:
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"last_post_ids": []}


def _save_state(state: dict):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


class TistoryCrawler:
    def __init__(self, blog_url: str = BLOG_URL):
        self.blog_url = blog_url
        self.rss_url = f"{blog_url}/rss"
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (compatible; BlogBot/1.0)"
        })

    def get_latest_post(self, force: bool = False) -> BlogPost | None:
        """
        RSS에서 최신 글을 가져옵니다.
        force=True: 상태 파일 무시하고 무조건 최신 글 반환
        force=False: 이미 처리된 글이면 None 반환
        """
        feed = feedparser.parse(self.rss_url)
        if not feed.entries:
            logger.warning("RSS 피드에 항목이 없습니다.")
            return None

        entry = feed.entries[0]
        post_id = hashlib.md5(entry.get("link", "").encode()).hexdigest()[:12]

        if not force:
            state = _load_state()
            if post_id in state["last_post_ids"]:
                logger.info(f"이미 처리된 글: {entry.get('title', '')}")
                return None

        post = self._parse_post(entry, post_id)
        if post:
            state = _load_state()
            state["last_post_ids"] = ([post_id] + state["last_post_ids"])[:20]
            _save_state(state)

        return post

    def _parse_post(self, entry, post_id: str) -> BlogPost | None:
        """RSS 항목 + 실제 페이지 크롤링으로 BlogPost 생성."""
        title = _decode_html_entities(entry.get("title", "").strip())
        url = entry.get("link", "")
        published = entry.get("published", "")
        tags = [_decode_html_entities(t.term) for t in entry.get("tags", [])]

        if not url:
            return None

        logger.info(f"글 크롤링 중: {url}")
        try:
            resp = self.session.get(url, timeout=15)
            resp.raise_for_status()
            soup = BeautifulSoup(resp.text, "html.parser")
        except Exception as e:
            logger.warning(f"페이지 크롤링 실패: {e}")
            soup = BeautifulSoup(entry.get("summary", ""), "html.parser")

        # 본문 텍스트 추출 (티스토리 본문 선택자)
        content_area = (
            soup.find("div", class_="tt_article_useless_p_margin")
            or soup.find("div", {"id": "content"})
            or soup.find("article")
            or soup.find("div", class_="entry-content")
        )
        if content_area:
            full_text = content_area.get_text(separator="\n", strip=True)
        else:
            full_text = BeautifulSoup(
                entry.get("summary", ""), "html.parser"
            ).get_text(separator="\n", strip=True)

        # 요약: 첫 300자
        lines = [l.strip() for l in full_text.split("\n") if l.strip()]
        summary = " ".join(lines[:5])[:300]

        thumbnail_url = _extract_thumbnail(soup, entry.get("summary", ""))
        mode = _detect_mode(title, entry.get("published_parsed"))

        return BlogPost(
            title=title,
            url=url,
            summary=summary,
            full_text=full_text[:4000],  # 토큰 절약
            thumbnail_url=thumbnail_url,
            tags=tags,
            published=published,
            post_id=post_id,
            mode=mode,
        )

    def get_post_as_dict(self, force: bool = False) -> dict | None:
        post = self.get_latest_post(force=force)
        return asdict(post) if post else None
