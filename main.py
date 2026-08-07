"""
미국 증시 블로그 자동화 메인 스크립트 (Cloudflare 대시보드 업로드 전용)
실행 흐름: 뉴스 수집 → 글 생성(Gemini) → 이미지 생성(Pexels/Pixabay)
          → Cloudflare 대시보드 업로드 (GitHub Releases 썸네일 포함)

포스팅 모드:
  morning (오전 9시 KST) : 미국 전일 증시 마감 리뷰
    - 제목 날짜 : 포스팅 작성 날짜 (한국 시간 기준)
    - 리뷰 대상 : 종료된 미국 정규장
      예) 한국 7월 9일 오전 9시 포스팅 → 뉴욕 현지 시각은 7월 8일 오후 8시(EDT)
          → 이미 마감된 정규장은 7월 8일 세션 → 리뷰 대상은 7월 8일

  evening (오후 9시 KST) : 당일 증시 오픈 전 이슈 + 당일 국내외 이슈 정리

날짜/시각 계산은 한국 시간(KST)을 실제 미국 뉴욕 현지 시각(zoneinfo, EDT/EST 자동 반영)으로
정확히 변환한 뒤, 그 값을 그대로 Gemini 프롬프트에 명시적으로 전달합니다.
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
import dashboard_client

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)

KST = timezone(timedelta(hours=9))
NY_TZ = ZoneInfo("America/New_York")  # 서머타임(EDT/EST)을 자동으로 정확히 반영

# 미국 증시 정규 휴장일 (월/일 형식, 매년 업데이트 필요)
US_MARKET_HOLIDAYS_2026 = {
    (1, 1),   # 신정
    (1, 19),  # MLK Day
    (2, 16),  # Presidents' Day
    (4, 3),   # Good Friday
    (5, 25),  # Memorial Day
    (6, 19),  # Juneteenth
    (7, 3),   # Independence Day (observed)
    (9, 7),   # Labor Day
    (11, 26), # Thanksgiving
    (12, 25), # Christmas
}


def detect_mode() -> str:
    """
    환경변수 POST_MODE 우선 사용.
    없으면 현재 KST 시각으로 자동 판별:
      00:00 ~ 14:59 → morning
      15:00 ~ 23:59 → evening
    """
    mode = os.environ.get("POST_MODE", "").strip().lower()
    if mode in ("morning", "evening"):
        logger.info(f"환경변수로 모드 지정: {mode}")
        return mode

    hour = datetime.now(KST).hour
    mode = "morning" if hour < 15 else "evening"
    logger.info(f"현재 KST {hour}시 → 자동 감지 모드: {mode}")
    return mode


def is_us_market_holiday(date: datetime) -> bool:
    if date.weekday() >= 5:
        return True
    if (date.month, date.day) in US_MARKET_HOLIDAYS_2026:
        return True
    return False


def get_last_us_trading_day(from_date: datetime) -> datetime:
    candidate = from_date
    for _ in range(10):
        if not is_us_market_holiday(candidate):
            return candidate
        candidate -= timedelta(days=1)
    return from_date


def compute_reference_times(now_kst: datetime) -> dict:
    """
    한국 시간(KST)을 기준으로, 실제 미국 뉴욕 현지 시각(서머타임 자동 반영)과
    가장 최근에 마감된 미국 정규장 거래일을 계산합니다.
    """
    korean_date = now_kst.strftime("%Y년 %m월 %d일")
    korean_datetime_str = now_kst.strftime("%Y-%m-%d %H:%M KST")

    us_now = now_kst.astimezone(NY_TZ)
    ny_reference_str = us_now.strftime("%Y-%m-%d %H:%M %Z")

    market_close = us_now.replace(hour=16, minute=0, second=0, microsecond=0)
    candidate = us_now if us_now >= market_close else us_now - timedelta(days=1)

    last_trading_day = get_last_us_trading_day(candidate)
    us_market_date = last_trading_day.strftime("%Y년 %m월 %d일")

    logger.info(f"한국 시간 포스팅 시각: {korean_datetime_str}")
    logger.info(f"뉴욕 기준 시각: {ny_reference_str}")
    logger.info(f"가장 최근 마감된 미국 정규장 날짜: {us_market_date}")

    return {
        "korean_date": korean_date,
        "korean_datetime_str": korean_datetime_str,
        "ny_reference_str": ny_reference_str,
        "us_market_date": us_market_date,
    }


def main():
    mode = detect_mode()
    now_kst = datetime.now(KST)
    ref = compute_reference_times(now_kst)

    korean_date = ref["korean_date"]
    us_market_date = ref["us_market_date"]

    if mode == "morning":
        post_label = "전일 마감 리뷰"
        logger.info(
            f"[오전 포스팅] 작성일: {korean_date} | 리뷰 대상: 미국 {us_market_date} 정규장"
        )
    else:
        post_label = "당일 프리마켓 & 이슈"
        logger.info(
            f"[저녁 포스팅] 작성일: {korean_date} | 직전 마감 정규장: 미국 {us_market_date}"
        )

    logger.info(f"===== 미국 증시 블로그 자동화 시작 [{mode.upper()} / {post_label}] =====")

    # 1. 뉴스 및 시장 데이터 수집
    logger.info("[1/4] 뉴스 및 시장 데이터 수집 중...")
    fetcher = NewsFetcher(alpha_vantage_key=os.environ["ALPHA_VANTAGE_API_KEY"])
    market_data = fetcher.get_market_summary()
    news_list = fetcher.get_top_news(limit=8)
    if not news_list:
        logger.error("뉴스 수집 실패. 종료합니다.")
        sys.exit(1)
    logger.info(f"  → 뉴스 {len(news_list)}건 수집 완료")

    # 1-1. 사실 기준표 구성 (실적/경제지표 발표일 팩트체크용)
    logger.info("[1-1] 실적/경제지표 사실 기준표 구성 중...")
    try:
        fact_reference_block, fact_lookup = build_fact_reference(
            alpha_vantage_key=os.environ.get("ALPHA_VANTAGE_API_KEY", ""),
            now_kst=now_kst,
        )
        n_earnings = len(fact_lookup.get("earnings", {}))
        n_macro = len(fact_lookup.get("macro", []))
        logger.info(f"  → 확인된 실적 발표일 {n_earnings}건, 매크로 지표 {n_macro}건")
    except Exception as e:
        logger.warning(f"  → 사실 기준표 구성 실패(팩트체크 없이 계속 진행): {e}")
        fact_reference_block, fact_lookup = "", {}

    # 2. 블로그 글 생성
    logger.info(f"[2/4] 블로그 글 생성 중 (Gemini / {mode})...")
    generator = ContentGenerator(api_key=os.environ["GEMINI_API_KEY"])
    post = generator.generate_post(
        date=korean_date,
        market_data=market_data,
        news_list=news_list,
        mode=mode,
        korean_date=korean_date,
        us_market_date=us_market_date,
        korean_datetime_str=ref["korean_datetime_str"],
        ny_reference_str=ref["ny_reference_str"],
        fact_reference_block=fact_reference_block,
        fact_lookup=fact_lookup,
    )
    logger.info(f"  → 제목: {post['title']}")
    logger.info(f"  → 글자 수: {len(post['content'])}자")

    # 3. 이미지 생성
    logger.info("[3/4] 썸네일 이미지 생성 중...")
    img_gen = ImageGenerator(hf_token=os.environ.get("HF_API_TOKEN", ""))
    timestamp = now_kst.strftime("%Y%m%d_%H%M")
    image_path = img_gen.generate(
        prompt = post.get("image_prompt") or (
             "Wall Street skyline, stock market chart, "
             "modern finance illustration"
        ),
        filename=f"thumbnail_{mode}_{timestamp}.png",
        content=post["content"],
        mode=mode,
    )
    logger.info(f"  → 이미지: {image_path}")

    # 3-1. 썸네일 저작자 표시는 로그로만 남기고 본문에는 추가하지 않음
    #      (블로그 포스팅 본문은 순수 콘텐츠에만 집중 — 사진 출처 표기가
    #      필요하면 대시보드나 발행 후 별도로 관리)
    attribution = img_gen.get_attribution_text()
    if attribution:
        logger.info(f"  → 썸네일 출처: {attribution} (본문에는 추가하지 않음)")

    # 4. Cloudflare 대시보드 업로드
    logger.info("[4/4] Cloudflare 대시보드 업로드 중...")
    post_date_str = now_kst.strftime("%Y-%m-%d")
    dashboard_ok = dashboard_client.push_content(
        post_date=post_date_str,
        mode=mode,
        title=post["title"],
        content=post["content"],
        tags=post["tags"],
        image_path=image_path,
    )
    if dashboard_ok:
        logger.info("  → 대시보드 업로드 완료")
    else:
        logger.error("  → 대시보드 업로드 실패. DASHBOARD_API_URL / INGEST_SECRET 설정을 확인하세요.")
        sys.exit(1)

    logger.info(f"===== 자동화 완료 [{mode.upper()}] =====")


if __name__ == "__main__":
    main()
