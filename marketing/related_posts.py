"""
연관 포스트 선정 및 HTML 생성 모듈.

역할:
1. 현재 포스트와 기존 포스트의 관련성 점수 계산
2. 상위 N개 포스트 선정
3. 실제 크롤링된 URL을 이용해 HTML 생성

Gemini 호출 없음.
외부 API 호출 없음.
"""

import html
import logging
import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

logger = logging.getLogger(__name__)


# 너무 일반적인 단어는 관련성 점수에서 제외합니다.
_STOPWORDS = {
    "미국",
    "증시",
    "미국증시",
    "주식",
    "시장",
    "오늘",
    "전일",
    "이번",
    "정리",
    "분석",
    "전망",
    "관련",
    "주요",
    "마감",
    "종목",
    "투자",
    "투자자",
    "시장동향",
    "경제",
    "뉴스",
    "이슈",
    "증권",
    "주가",
    "나스닥",
    "s&p",
    "sp500",
    "다우",
}


def _normalize_text(value: str) -> str:
    if not value:
        return ""

    value = html.unescape(str(value))
    value = value.lower()

    value = re.sub(
        r"[^0-9a-zA-Z가-힣+#&.\- ]+",
        " ",
        value,
    )

    value = re.sub(
        r"\s+",
        " ",
        value,
    )

    return value.strip()


def _tokenize(value: str) -> set[str]:
    """
    제목/태그/본문에서 비교용 토큰을 추출합니다.

    한국어는 형태소 분석기를 추가하지 않고,
    현재 프로젝트 의존성을 늘리지 않는 방식으로 처리합니다.
    """

    normalized = _normalize_text(value)

    if not normalized:
        return set()

    raw_tokens = normalized.split()

    result = set()

    for token in raw_tokens:

        token = token.strip(
            ".,:;!?()[]{}\"'“”‘’"
        )

        if not token:
            continue

        if token in _STOPWORDS:
            continue

        # 너무 짧은 영어 토큰 제거
        if token.isascii() and len(token) < 3:
            continue

        # 너무 짧은 숫자 제거
        if token.isdigit() and len(token) < 2:
            continue

        result.add(token)

    return result


def _tags(post: dict) -> set[str]:
    result = set()

    for tag in post.get("tags", []) or []:

        if isinstance(tag, str):
            value = tag

        elif isinstance(tag, dict):
            value = tag.get(
                "name",
                tag.get("term", ""),
            )

        else:
            value = str(tag)

        token_set = _tokenize(value)
        result.update(token_set)

    return result


def _title_tokens(post: dict) -> set[str]:
    return _tokenize(
        post.get("title", "")
    )


def _content_tokens(post: dict) -> set[str]:
    text = " ".join(
        [
            post.get("summary", "") or "",
            post.get("full_text", "") or "",
        ]
    )

    return _tokenize(text)


def _published_datetime(value: str):
    """
    RSS published 문자열을 datetime으로 변환합니다.
    """

    if not value:
        return None

    try:
        dt = parsedate_to_datetime(value)

        if dt.tzinfo is None:
            dt = dt.replace(
                tzinfo=timezone.utc
            )

        return dt.astimezone(timezone.utc)

    except Exception:
        pass

    # 일부 ISO 형태 대응
    try:
        normalized = value.replace(
            "Z",
            "+00:00",
        )

        dt = datetime.fromisoformat(
            normalized
        )

        if dt.tzinfo is None:
            dt = dt.replace(
                tzinfo=timezone.utc
            )

        return dt.astimezone(timezone.utc)

    except Exception:
        return None


def _recency_score(post: dict) -> float:
    """
    최근 글일수록 약간의 가산점을 줍니다.

    너무 오래된 글을 무조건 배제하지 않고,
    같은 관련성이라면 최근 글이 우선되도록 합니다.
    """

    dt = _published_datetime(
        post.get("published", "")
    )

    if dt is None:
        return 0.0

    now = datetime.now(timezone.utc)

    age_days = max(
        0.0,
        (now - dt).total_seconds()
        / 86400.0,
    )

    # 30일 이내에서 최대 10점
    return max(
        0.0,
        10.0 - min(age_days, 30.0) / 3.0,
    )


def calculate_related_score(
    current_post: dict,
    candidate: dict,
) -> float:
    """
    현재 포스트와 후보 포스트의 관련성 점수를 계산합니다.

    총점:
      태그 교집합        최대 40점
      제목 핵심어        최대 30점
      본문 핵심어        최대 20점
      최근성             최대 10점

    URL은 실제 크롤링 결과만 사용합니다.
    """

    current_tags = _tags(
        current_post
    )

    candidate_tags = _tags(
        candidate
    )

    current_title = _title_tokens(
        current_post
    )

    candidate_title = _title_tokens(
        candidate
    )

    current_content = _content_tokens(
        current_post
    )

    candidate_content = _content_tokens(
        candidate
    )

    # ── 태그 점수 ─────────────────────────────

    tag_overlap = (
        current_tags
        & candidate_tags
    )

    if current_tags:
        tag_ratio = len(tag_overlap) / max(
            1,
            min(
                len(current_tags),
                6,
            ),
        )
    else:
        tag_ratio = 0.0

    tag_score = min(
        40.0,
        tag_ratio * 40.0,
    )

    # ── 제목 점수 ─────────────────────────────

    title_overlap = (
        current_title
        & candidate_title
    )

    if current_title:
        title_ratio = len(title_overlap) / max(
            1,
            min(
                len(current_title),
                6,
            ),
        )
    else:
        title_ratio = 0.0

    title_score = min(
        30.0,
        title_ratio * 30.0,
    )

    # 동일한 중요한 제목 키워드가 있으면 추가 보정
    important_overlap = {
        token
        for token in title_overlap
        if len(token) >= 2
    }

    if important_overlap:
        title_score += min(
            5.0,
            len(important_overlap) * 2.5,
        )

    title_score = min(
        30.0,
        title_score,
    )

    # ── 본문 점수 ─────────────────────────────

    content_overlap = (
        current_content
        & candidate_content
    )

    if current_content:
        # 본문 전체 토큰은 많기 때문에
        # 상위 100개 정도까지만 영향력을 제한합니다.
        denominator = max(
            1,
            min(
                len(current_content),
                100,
            ),
        )

        content_ratio = min(
            1.0,
            len(content_overlap)
            / denominator,
        )

    else:
        content_ratio = 0.0

    content_score = min(
        20.0,
        content_ratio * 80.0,
    )

    # ── 최근성 점수 ───────────────────────────

    recency_score = _recency_score(
        candidate
    )

    total = (
        tag_score
        + title_score
        + content_score
        + recency_score
    )

    return round(
        min(100.0, total),
        2,
    )


def select_related_posts(
    current_post: dict,
    candidates: list[dict],
    limit: int = 3,
) -> list[dict]:
    """
    현재 포스트와 관련성이 높은 포스트를 선정합니다.

    안전장치:
    - 현재 URL 제외
    - 현재 post_id 제외
    - URL 없는 후보 제외
    - 중복 URL 제거
    - 같은 글 중복 제거
    """

    if not current_post:
        return []

    if limit <= 0:
        return []

    current_url = (
        current_post.get(
            "url",
            "",
        )
        or ""
    ).rstrip("/")

    current_id = current_post.get(
        "post_id",
        "",
    )

    scored = []

    seen_urls = set()

    for candidate in candidates or []:

        url = (
            candidate.get(
                "url",
                "",
            )
            or ""
        ).strip()

        normalized_url = url.rstrip("/")

        if not normalized_url:
            continue

        if normalized_url == current_url:
            continue

        if (
            current_id
            and candidate.get("post_id") == current_id
        ):
            continue

        if normalized_url in seen_urls:
            continue

        seen_urls.add(normalized_url)

        score = calculate_related_score(
            current_post,
            candidate,
        )

        candidate_copy = dict(candidate)

        candidate_copy["_related_score"] = score

        scored.append(
            candidate_copy
        )

    scored.sort(
        key=lambda item: (
            item.get(
                "_related_score",
                0.0,
            ),
            _published_datetime(
                item.get(
                    "published",
                    "",
                )
            )
            or datetime.min.replace(
                tzinfo=timezone.utc
            ),
        ),
        reverse=True,
    )

    selected = scored[:limit]

    logger.info(
        "연관 포스트 선정 결과:"
    )

    for index, post in enumerate(
        selected,
        1,
    ):
        logger.info(
            f"  {index}. "
            f"[{post.get('_related_score', 0)}점] "
            f"{post.get('title', '')}"
        )

    return selected


def build_related_posts_html(
    related_posts: list[dict],
    heading: str = "관련 포스트",
) -> str:
    """
    실제 URL을 이용해 티스토리 본문 하단에 넣을 HTML을 생성합니다.

    출력 예:

    <h2>관련 포스트</h2>
    <ul>
    <li><a href="...">...</a></li>
    ...
    </ul>

    URL과 제목은 HTML escape 처리합니다.
    """

    if not related_posts:
        return ""

    items = []

    for post in related_posts:

        url = (
            post.get(
                "url",
                "",
            )
            or ""
        ).strip()

        title = (
            post.get(
                "title",
                "",
            )
            or ""
        ).strip()

        if not url or not title:
            continue

        safe_url = html.escape(
            url,
            quote=True,
        )

        safe_title = html.escape(
            title,
            quote=False,
        )

        items.append(
            f'<li><a href="{safe_url}">{safe_title}</a></li>'
        )

    if not items:
        return ""

    safe_heading = html.escape(
        heading,
        quote=False,
    )

    return (
        f"<h2>{safe_heading}</h2>\n"
        "<ul>\n"
        + "\n".join(items)
        + "\n</ul>"
    )


def append_related_posts_html(
    content: str,
    related_posts: list[dict],
) -> str:
    """
    본문 하단에 관련 포스트 3개를 삽입합니다.

    최종 순서:

        본문
        ↓
        관련 포스트
        ↓
        면책조항

    이미 관련 포스트 영역이 있으면 중복 삽입하지 않습니다.
    """

    if not content:
        return content

    if not related_posts:
        return content

    existing_markers = (
        "<h2>관련 포스트</h2>",
        "<h2>관련포스트</h2>",
        "<h2>함께 보면 좋은 글</h2>",
        "<h2>추천 포스트</h2>",
    )

    for marker in existing_markers:
        if marker in content:
            logger.info(
                "본문에 이미 연관 포스트 영역이 있어 "
                "추가 삽입하지 않습니다."
            )
            return content

    related_html = build_related_posts_html(
        related_posts
    )

    if not related_html:
        return content

    content = content.rstrip()

    # 현재 프로젝트의 면책조항은
    # <blockquote data-ke-style="style3">...</blockquote>
    # 형태를 사용합니다.
    #
    # 마지막 blockquote를 면책조항으로 간주합니다.
    disclaimer_pattern = re.compile(
        r'(<blockquote\b[^>]*data-ke-style\s*=\s*["\']style3["\'][^>]*>.*?</blockquote>)\s*$',
        re.IGNORECASE | re.DOTALL,
    )

    match = disclaimer_pattern.search(
        content
    )

    if match:

        body_before_disclaimer = (
            content[:match.start()].rstrip()
        )

        disclaimer = match.group(1).strip()

        return (
            body_before_disclaimer
            + "\n\n"
            + related_html
            + "\n\n"
            + disclaimer
        )

    # 면책조항을 찾지 못한 경우에는
    # 안전하게 본문 끝에 붙입니다.
    logger.warning(
        "style3 면책조항을 찾지 못했습니다. "
        "연관 포스트를 본문 끝에 삽입합니다."
    )

    return (
        content
        + "\n\n"
        + related_html
    )