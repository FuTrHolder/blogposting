"""
미국 증시 블로그 자동화 메인 스크립트 (이메일 발송 + Cloudflare 대시보드 업로드 버전)
실행 흐름: 뉴스 수집 → 글 생성(Gemini) → 이미지 생성(SD/Unsplash)
          → Cloudflare 대시보드 업로드 → 이메일 발송(Gmail)

포스팅 모드:
  morning (오전 9시 KST) : 미국 전일 증시 마감 리뷰
    - 제목 날짜 : 포스팅 작성 날짜 (한국 시간 기준)
    - 리뷰 대상 : 종료된 미국 정규장
      예) 한국 7월 9일 오전 9시 포스팅 → 뉴욕 현지 시각은 7월 8일 오후 8시(EDT)
          → 이미 마감된 정규장은 7월 8일 세션 → 리뷰 대상은 7월 8일

  evening (오후 9시 KST) : 당일 증시 오픈 전 이슈 + 당일 국내외 이슈 정리

날짜/시각 계산은 한국 시간(KST)을 실제 미국 뉴욕 현지 시각(zoneinfo, EDT/EST 자동 반영)으로
정확히 변환한 뒤, 그 값을 그대로 Gemini 프롬프트에 명시적으로 전달합니다.
(AI가 매번 시간대를 스스로 계산하게 하면 오류가 잦으므로, 계산은 코드에서 하고
 결과값만 "사실"로 프롬프트에 제공하는 방식)

Cloudflare 대시보드 업로드:
  - DASHBOARD_API_URL이 설정된 경우에만 동작 (미설정 시 조용히 건너뜀)
  - 업로드 실패해도 이메일 발송 등 나머지 흐름은 그대로 진행됨
  - 대시보드가 안정화되기 전까지는 이메일 발송과 함께 병행 운영
"""

import os
import sys
import logging
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

from news_fetcher import NewsFetcher
from content_generator import ContentGenerator
from image_generator import ImageGenerator
from email_sender import EmailSender
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
# 굿 프라이데이, 독립기념일, 추수감사절 다음날, 크리스마스 등
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
    """해당 날짜가 미국 증시 휴장일인지 확인합니다."""
    # 주말 체크
    if date.weekday() >= 5:  # 5=토, 6=일
        return True
    # 공휴일 체크
    if (date.month, date.day) in US_MARKET_HOLIDAYS_2026:
        return True
    return False


def get_last_us_trading_day(from_date: datetime) -> datetime:
    """
    주어진 날짜(미국 기준)에서 가장 최근 미국 정규 거래일을 반환합니다.
    from_date 자체가 거래일이면 그대로 반환, 휴장일이면 직전 거래일 탐색.
    """
    candidate = from_date
    for _ in range(10):  # 최대 10일 전까지 탐색
        if not is_us_market_holiday(candidate):
            return candidate
        candidate -= timedelta(days=1)
    # 탐색 실패 시 그대로 반환 (안전 fallback)
    return from_date


def compute_reference_times(now_kst: datetime) -> dict:
    """
    한국 시간(KST)을 기준으로, 실제 미국 뉴욕 현지 시각(서머타임 자동 반영)과
    가장 최근에 마감된 미국 정규장 거래일을 계산합니다. morning/evening 공통 사용.

    핵심 로직: 뉴욕 현지 시각이 그날 정규장 마감 시각(16:00 ET) 이후이면
    "그 날짜"의 세션이 이미 마감된 것이고, 그 이전(마감 전)이면
    "하루 전" 세션이 가장 최근에 마감된 세션입니다.

    Returns:
        korean_date         : 포스팅 작성 날짜 (한국어, 제목용)
        korean_datetime_str : 포스팅 작성 시각 전체 문자열 (예: '2026-07-09 09:00 KST')
        ny_reference_str    : 뉴욕 기준 현재 시각 전체 문자열 (예: '2026-07-08 20:00 EDT')
        us_market_date       : 가장 최근에 마감된 미국 정규장 날짜 (한국어, 본문/리뷰 대상)
    """
    korean_date = now_kst.strftime("%Y년 %m월 %d일")
    korean_datetime_str = now_kst.strftime("%Y-%m-%d %H:%M KST")

    # 뉴욕 실제 현지 시각 (zoneinfo가 EDT/EST를 자동으로 정확히 반영)
    us_now = now_kst.astimezone(NY_TZ)
    ny_reference_str = us_now.strftime("%Y-%m-%d %H:%M %Z")

    # 뉴욕 현지 시각이 그 날짜의 정규장 마감(16:00 ET) 이후인지 확인
    market_close = us_now.replace(hour=16, minute=0, second=0, microsecond=0)
    candidate = us_now if us_now >= market_close else us_now - timedelta(days=1)

    # 마지막 미국 거래일 탐색 (휴장일·주말 건너뜀)
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
    logger.info("[1/5] 뉴스 및 시장 데이터 수집 중...")
    fetcher = NewsFetcher(alpha_vantage_key=os.environ["ALPHA_VANTAGE_API_KEY"])
    market_data = fetcher.get_market_summary()
    news_list = fetcher.get_top_news(limit=8)
    if not news_list:
        logger.error("뉴스 수집 실패. 종료합니다.")
        sys.exit(1)
    logger.info(f"  → 뉴스 {len(news_list)}건 수집 완료")

    # 1-1. 사실 기준표(Fact Reference) 구성 — 실적/경제지표 발표일 팩트체크용
    #      기존 ALPHA_VANTAGE_API_KEY를 재사용하므로 별도 비용/키 발급이 없습니다.
    #      실패하더라도(네트워크 오류 등) 빈 값으로 안전하게 진행됩니다.
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
    logger.info(f"[2/5] 블로그 글 생성 중 (Gemini / {mode})...")
    generator = ContentGenerator(api_key=os.environ["GEMINI_API_KEY"])

    post = generator.generate_post(
        date=korean_date,           # 하위 호환용
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
    logger.info("[3/5] 썸네일 이미지 생성 중...")
    img_gen = ImageGenerator(hf_token=os.environ.get("HF_API_TOKEN", ""))
    timestamp = now_kst.strftime("%Y%m%d_%H%M")
    image_path = img_gen.generate(
        prompt=post["image_prompt"],
        filename=f"thumbnail_{mode}_{timestamp}.png",
        content=post["content"],
        mode=mode,
    )
    logger.info(f"  → 이미지: {image_path}")

    # 3-1. 썸네일 출처에 맞는 저작자 표시를 본문 끝에 자동 추가
    #      (이메일/대시보드 모두 post["content"]를 그대로 사용하므로 여기 한 곳에서만
    #      처리하면 양쪽에 자동 반영됩니다)
    attribution = img_gen.get_attribution_text()
    if attribution:
        post["content"] = post["content"].rstrip() + f"\n\n---\n*{attribution}*"
        logger.info(f"  → 저작자 표시 추가: {attribution}")

    # 4. Cloudflare 대시보드 업로드 (DASHBOARD_API_URL 미설정 시 자동 스킵)
    logger.info("[4/5] Cloudflare 대시보드 업로드 중...")
    post_date_str = now_kst.strftime("%Y-%m-%d")
    dashboard_ok = dashboard_client.push_content(
        post_date=post_date_str,
        mode=mode,
        title=post["title"],
        content=post["content"],
        tags=post["tags"],
        image_path=image_path,
    )
    logger.info(f"  → 대시보드 업로드: {'완료' if dashboard_ok else '건너뜀/실패 (계속 진행)'}")

    # 5. 이메일 발송 (대시보드 안정화 전까지 계속 병행)
    logger.info("[5/5] 이메일 발송 중...")
    sender = EmailSender(
        gmail_address=os.environ["GMAIL_ADDRESS"],
        gmail_app_password=os.environ["GMAIL_APP_PASSWORD"],
        recipient_email=os.environ["RECIPIENT_EMAIL"],
    )
    result = sender.send(
        title=post["title"],
        content=post["content"],
        tags=post["tags"],
        image_path=image_path,
        mode=mode,
    )
    logger.info(f"  → 발송 완료: {result['to']}")
    logger.info(f"===== 자동화 완료 [{mode.upper()}] =====")


if __name__ == "__main__":
    main()
