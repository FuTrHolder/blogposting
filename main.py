"""
미국 증시 블로그 자동화 메인 스크립트

실행 흐름:
뉴스 수집
→ 글 생성(Gemini)
→ 연관 포스트 3개 선정
→ 원고 하단에 연관 포스트 HTML 삽입
→ 이미지 생성
→ Cloudflare 대시보드 업로드

포스팅 모드:
  morning:
    미국 전일 증시 마감 리뷰

  evening:
    당일 증시 오픈 전 이슈 + 당일 국내외 이슈 정리

연관 포스트:
  - 티스토리 RSS 최근 글을 읽기 전용으로 조회
  - 현재 원고와 관련성이 높은 기존 글 3개 선정
  - 실제 티스토리 URL을 사용
  - Gemini에게 URL 생성을 맡기지 않음
  - 원고 가장 하단에 HTML로 삽입
"""

import os
import sys
import logging
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

from news_fetcher import NewsFetcher
from content_generator import ContentGenerator
from image_generator import ImageGenerator
from fact_reference import build_fact_reference
from marketing.tistory_crawler.crawler import TistoryCrawler
from marketing.related_posts import (
    select_related_posts,
    append_related_posts_html,
)
import dashboard_client


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout)
    ],
)

logger = logging.getLogger(__name__)

KST = timezone(timedelta(hours=9))
NY_TZ = ZoneInfo("America/New_York")


# 미국 증시 정규 휴장일
US_MARKET_HOLIDAYS_2026 = {
    (1, 1),
    (1, 19),
    (2, 16),
    (4, 3),
    (5, 25),
    (6, 19),
    (7, 3),
    (9, 7),
    (11, 26),
    (12, 25),
}


def detect_mode() -> str:
    """
    환경변수 POST_MODE 우선 사용.

    없으면 현재 KST 시각으로 자동 판별:
      00:00 ~ 14:59 → morning
      15:00 ~ 23:59 → evening
    """

    mode = os.environ.get(
        "POST_MODE",
        "",
    ).strip().lower()

    if mode in (
        "morning",
        "evening",
    ):
        logger.info(
            f"환경변수로 모드 지정: {mode}"
        )
        return mode

    hour = datetime.now(KST).hour

    mode = (
        "morning"
        if hour < 15
        else "evening"
    )

    logger.info(
        f"현재 KST {hour}시 "
        f"→ 자동 감지 모드: {mode}"
    )

    return mode


def is_us_market_holiday(
    date: datetime,
) -> bool:
    if date.weekday() >= 5:
        return True

    if (
        date.month,
        date.day,
    ) in US_MARKET_HOLIDAYS_2026:
        return True

    return False


def get_last_us_trading_day(
    from_date: datetime,
) -> datetime:

    candidate = from_date

    for _ in range(10):

        if not is_us_market_holiday(
            candidate
        ):
            return candidate

        candidate -= timedelta(days=1)

    return from_date


def compute_reference_times(
    now_kst: datetime,
) -> dict:
    """
    한국 시간 기준으로 뉴욕 현지 시각과
    가장 최근 미국 정규장 거래일을 계산합니다.
    """

    korean_date = now_kst.strftime(
        "%Y년 %m월 %d일"
    )

    korean_datetime_str = now_kst.strftime(
        "%Y-%m-%d %H:%M KST"
    )

    us_now = now_kst.astimezone(
        NY_TZ
    )

    ny_reference_str = us_now.strftime(
        "%Y-%m-%d %H:%M %Z"
    )

    market_close = us_now.replace(
        hour=16,
        minute=0,
        second=0,
        microsecond=0,
    )

    if us_now >= market_close:
        candidate = us_now
    else:
        candidate = (
            us_now
            - timedelta(days=1)
        )

    last_trading_day = (
        get_last_us_trading_day(
            candidate
        )
    )

    us_market_date = (
        last_trading_day.strftime(
            "%Y년 %m월 %d일"
        )
    )

    logger.info(
        f"한국 시간 포스팅 시각: "
        f"{korean_datetime_str}"
    )

    logger.info(
        f"뉴욕 기준 시각: "
        f"{ny_reference_str}"
    )

    logger.info(
        f"가장 최근 마감된 미국 정규장 날짜: "
        f"{us_market_date}"
    )

    return {
        "korean_date": korean_date,
        "korean_datetime_str": korean_datetime_str,
        "ny_reference_str": ny_reference_str,
        "us_market_date": us_market_date,
    }


def add_related_posts(
    post: dict,
) -> dict:
    """
    생성된 원고에 관련 포스트 3개를 붙입니다.

    중요:
    - RSS에서 최근 글을 읽기만 합니다.
    - 중복 실행 상태 파일을 변경하지 않습니다.
    - 연관 포스트 선정 실패가 블로그 원고 생성 전체를 실패시키지 않습니다.
    """

    content = post.get(
        "content",
        "",
    ) or ""

    if not content:
        logger.warning(
            "본문이 비어 있어 연관 포스트를 삽입하지 않습니다."
        )
        return post

    try:
        crawler = TistoryCrawler()

        current_url = (
            post.get(
                "blog_url",
                "",
            )
            or ""
        )

        # 생성 직후 post에는 실제 블로그 URL이 없을 수 있으므로
        # 현재 생성 원고의 URL을 제외 대상으로 사용할 필요는 없습니다.
        # 이 단계에서는 아직 발행되지 않았기 때문에
        # 최근 기존 글 전체가 후보가 됩니다.
        candidates = crawler.get_recent_posts(
            limit=20,
            exclude_url=current_url,
        )

        if not candidates:
            logger.warning(
                "연관 포스트 후보가 없어 원고를 그대로 사용합니다."
            )
            return post

        current_post = {
            "title": post.get(
                "title",
                "",
            ),
            "url": current_url,
            "summary": content[:1000],
            "full_text": content,
            "tags": post.get(
                "tags",
                [],
            ),
            "published": "",
            "post_id": "",
        }

        related_posts = select_related_posts(
            current_post=current_post,
            candidates=[
                {
                    "title": candidate.title,
                    "url": candidate.url,
                    "summary": candidate.summary,
                    "full_text": candidate.full_text,
                    "thumbnail_url": candidate.thumbnail_url,
                    "tags": candidate.tags,
                    "published": candidate.published,
                    "post_id": candidate.post_id,
                    "mode": candidate.mode,
                }
                for candidate in candidates
            ],
            limit=3,
        )

        if not related_posts:
            logger.warning(
                "관련성이 있는 연관 포스트를 찾지 못했습니다."
            )
            return post

        post = dict(post)

        post["content"] = append_related_posts_html(
            content,
            related_posts,
        )

        # 대시보드나 향후 다른 소비처에서 사용할 수 있도록
        # 실제 선정 결과도 별도 필드로 보관합니다.
        post["related_posts"] = [
            {
                "title": item.get(
                    "title",
                    "",
                ),
                "url": item.get(
                    "url",
                    "",
                ),
                "score": item.get(
                    "_related_score",
                    0,
                ),
            }
            for item in related_posts
        ]

        logger.info(
            f"연관 포스트 {len(related_posts)}개를 "
            "원고 하단에 삽입했습니다."
        )

        for index, item in enumerate(
            related_posts,
            1,
        ):
            logger.info(
                f"  → 관련 {index}: "
                f"{item.get('title', '')} "
                f"({item.get('_related_score', 0)}점)"
            )

        return post

    except Exception as e:
        logger.warning(
            f"연관 포스트 처리 실패 — "
            f"원고는 그대로 계속 진행합니다: {e}"
        )

        return post


def main():

    mode = detect_mode()

    now_kst = datetime.now(KST)

    ref = compute_reference_times(
        now_kst
    )

    korean_date = ref[
        "korean_date"
    ]

    us_market_date = ref[
        "us_market_date"
    ]

    if mode == "morning":

        post_label = "전일 마감 리뷰"

        logger.info(
            f"[오전 포스팅] 작성일: "
            f"{korean_date} | "
            f"리뷰 대상: 미국 "
            f"{us_market_date} 정규장"
        )

    else:

        post_label = "당일 프리마켓 & 이슈"

        logger.info(
            f"[저녁 포스팅] 작성일: "
            f"{korean_date} | "
            f"직전 마감 정규장: 미국 "
            f"{us_market_date}"
        )

    logger.info(
        f"===== 미국 증시 블로그 자동화 시작 "
        f"[{mode.upper()} / {post_label}] ====="
    )

    # ─────────────────────────────────────────
    # 1. 뉴스 및 시장 데이터 수집
    # ─────────────────────────────────────────

    logger.info(
        "[1/5] 뉴스 및 시장 데이터 수집 중..."
    )

    fetcher = NewsFetcher(
        alpha_vantage_key=os.environ[
            "ALPHA_VANTAGE_API_KEY"
        ]
    )

    market_data = (
        fetcher.get_market_summary()
    )

    news_list = (
        fetcher.get_top_news(
            limit=8
        )
    )

    if not news_list:
        logger.error(
            "뉴스 수집 실패. 종료합니다."
        )
        sys.exit(1)

    logger.info(
        f"  → 뉴스 {len(news_list)}건 수집 완료"
    )

    # ─────────────────────────────────────────
    # 1-1. 사실 기준표
    # ─────────────────────────────────────────

    logger.info(
        "[1-1] 실적/경제지표 사실 기준표 구성 중..."
    )

    try:

        (
            fact_reference_block,
            fact_lookup,
        ) = build_fact_reference(
            alpha_vantage_key=os.environ.get(
                "ALPHA_VANTAGE_API_KEY",
                "",
            ),
            now_kst=now_kst,
        )

        n_earnings = len(
            fact_lookup.get(
                "earnings",
                {},
            )
        )

        n_macro = len(
            fact_lookup.get(
                "macro",
                [],
            )
        )

        logger.info(
            f"  → 확인된 실적 발표일 "
            f"{n_earnings}건, "
            f"매크로 지표 {n_macro}건"
        )

    except Exception as e:

        logger.warning(
            f"  → 사실 기준표 구성 실패 "
            f"(팩트체크 없이 계속 진행): {e}"
        )

        fact_reference_block = ""
        fact_lookup = {}

    # ─────────────────────────────────────────
    # 2. 블로그 글 생성
    # ─────────────────────────────────────────

    logger.info(
        f"[2/5] 블로그 글 생성 중 "
        f"(Gemini / {mode})..."
    )

    generator = ContentGenerator(
        api_key=os.environ[
            "GEMINI_API_KEY"
        ]
    )

    post = generator.generate_post(
        date=korean_date,
        market_data=market_data,
        news_list=news_list,
        mode=mode,
        korean_date=korean_date,
        us_market_date=us_market_date,
        korean_datetime_str=ref[
            "korean_datetime_str"
        ],
        ny_reference_str=ref[
            "ny_reference_str"
        ],
        fact_reference_block=fact_reference_block,
        fact_lookup=fact_lookup,
    )

    logger.info(
        f"  → 제목: {post['title']}"
    )

    logger.info(
        f"  → Gemini 원고 글자 수: "
        f"{len(post['content'])}자"
    )

    # ─────────────────────────────────────────
    # 2-1. 연관 포스트 3개 삽입
    # ─────────────────────────────────────────

    logger.info(
        "[2-1/5] 연관 포스트 3개 선정 및 "
        "원고 하단 삽입 중..."
    )

    post = add_related_posts(
        post
    )

    logger.info(
        f"  → 최종 원고 글자 수: "
        f"{len(post.get('content', ''))}자"
    )

    related_posts = post.get(
        "related_posts",
        [],
    )

    if related_posts:

        logger.info(
            f"  → 연관 포스트 "
            f"{len(related_posts)}개 반영 완료"
        )

    else:

        logger.info(
            "  → 연관 포스트 미반영"
        )

    # ─────────────────────────────────────────
    # 3. 이미지 생성
    # ─────────────────────────────────────────

    logger.info(
        "[3/5] 썸네일 이미지 생성 중..."
    )

    img_gen = ImageGenerator(
        hf_token=os.environ.get(
            "HF_API_TOKEN",
            "",
        )
    )

    timestamp = now_kst.strftime(
        "%Y%m%d_%H%M"
    )

    image_path = img_gen.generate(
        prompt=post[
            "image_prompt"
        ],
        filename=(
            f"thumbnail_"
            f"{mode}_"
            f"{timestamp}.png"
        ),
        content=post[
            "content"
        ],
        mode=mode,
        title=post[
            "title"
        ],
    )

    logger.info(
        f"  → 이미지: {image_path} "
        f"(소스: "
        f"{img_gen.last_image_source or 'gradient fallback'})"
    )

    # ─────────────────────────────────────────
    # 3-1. 이미지 출처
    # ─────────────────────────────────────────

    attribution = (
        img_gen.get_attribution_text()
    )

    if attribution:

        logger.info(
            f"  → 썸네일 출처: "
            f"{attribution} "
            f"(본문에는 추가하지 않음)"
        )

    # ─────────────────────────────────────────
    # 4. Cloudflare 대시보드 업로드
    # ─────────────────────────────────────────

    logger.info(
        "[4/5] Cloudflare 대시보드 업로드 중..."
    )

    post_date_str = now_kst.strftime(
        "%Y-%m-%d"
    )

    dashboard_ok = (
        dashboard_client.push_content(
            post_date=post_date_str,
            mode=mode,
            title=post[
                "title"
            ],
            content=post[
                "content"
            ],
            tags=post[
                "tags"
            ],
            image_path=image_path,
        )
    )

    if dashboard_ok:

        logger.info(
            "  → 대시보드 업로드 완료"
        )

    else:

        logger.error(
            "  → 대시보드 업로드 실패. "
            "DASHBOARD_API_URL / "
            "INGEST_SECRET 설정을 확인하세요."
        )

        sys.exit(1)

    # ─────────────────────────────────────────
    # 5. 완료
    # ─────────────────────────────────────────

    logger.info(
        f"===== 자동화 완료 "
        f"[{mode.upper()}] ====="
    )


if __name__ == "__main__":
    main()