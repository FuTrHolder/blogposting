"""
미국 증시 블로그 자동화 메인 스크립트 (이메일 발송 버전)
실행 흐름: 뉴스 수집 → 글 생성(Gemini) → 이미지 생성(SD/Unsplash) → 이메일 발송(Gmail)

포스팅 모드:
  morning (오전 9시 KST) : 미국 전일 증시 마감 리뷰
    - 제목 날짜 : 포스팅 작성 날짜 (한국 시간 기준)
    - 리뷰 대상 : 종료된 미국 정규장
      예) 한국 4월 4일(토) 오전 포스팅 → 미국 4월 2일(목) 정규장 리뷰
          (미국 4월 3일 굿 프라이데이 휴장이므로 그 전 거래일)

  evening (오후 9시 KST) : 당일 증시 오픈 전 이슈 + 당일 국내외 이슈 정리
"""

import os
import sys
import logging
from datetime import datetime, timezone, timedelta

from news_fetcher import NewsFetcher
from content_generator import ContentGenerator
from image_generator import ImageGenerator
from email_sender import EmailSender

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)

KST = timezone(timedelta(hours=9))

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


def calculate_morning_dates(now_kst: datetime) -> tuple[str, str]:
    """
    오전 포스팅에 필요한 두 가지 날짜를 계산합니다.

    한국 시간 오전 9시 = 미국 동부 시간(EDT) 전날 저녁 8시
    → 미국 정규장(오전 9:30~오후 4:00 ET)은 이미 전날 종료
    → 리뷰 대상: 미국 기준 전날 or 더 이전의 마지막 거래일

    Returns:
        korean_date   : 포스팅 작성 날짜 한국어 문자열 (제목용)
        us_market_date: 리뷰 대상 미국 정규장 날짜 한국어 문자열 (본문용)
    """
    # 포스팅 작성 날짜 (한국 시간)
    korean_date = now_kst.strftime("%Y년 %m월 %d일")

    # 미국 동부 시간 기준: 한국 시간 - 13시간 (EDT 기준)
    # 오전 9시 KST = 전날 저녁 8시 EDT → 미국 정규장은 이미 마감
    us_now = now_kst - timedelta(hours=13)

    # 미국 전날(정규장이 마감된 날)
    us_previous_day = us_now - timedelta(days=1)

    # 마지막 미국 거래일 탐색 (휴장일·주말 건너뜀)
    last_trading_day = get_last_us_trading_day(us_previous_day)

    us_market_date = last_trading_day.strftime("%Y년 %m월 %d일")

    logger.info(f"한국 시간 포스팅 날짜: {korean_date}")
    logger.info(f"미국 정규장 리뷰 대상일: {us_market_date}")

    return korean_date, us_market_date


def main():
    mode = detect_mode()
    now_kst = datetime.now(KST)

    if mode == "morning":
        korean_date, us_market_date = calculate_morning_dates(now_kst)
        post_label = "전일 마감 리뷰"
        logger.info(
            f"[오전 포스팅] 작성일: {korean_date} | 리뷰 대상: 미국 {us_market_date} 정규장"
        )
    else:
        # 저녁 포스팅: 한국 날짜를 date로 사용 (기존 로직 유지)
        korean_date = now_kst.strftime("%Y년 %m월 %d일")
        us_market_date = korean_date  # evening은 사용 안 함
        post_label = "당일 프리마켓 & 이슈"
        logger.info(f"[저녁 포스팅] 작성일: {korean_date}")

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

    # 2. 블로그 글 생성
    logger.info(f"[2/4] 블로그 글 생성 중 (Gemini / {mode})...")
    generator = ContentGenerator(api_key=os.environ["GEMINI_API_KEY"])

    if mode == "morning":
        post = generator.generate_post(
            date=korean_date,           # 하위 호환용 (evening fallback)
            market_data=market_data,
            news_list=news_list,
            mode=mode,
            korean_date=korean_date,    # 제목용 한국 날짜
            us_market_date=us_market_date,  # 본문용 미국 정규장 날짜
        )
    else:
        post = generator.generate_post(
            date=korean_date,
            market_data=market_data,
            news_list=news_list,
            mode=mode,
        )

    logger.info(f"  → 제목: {post['title']}")
    logger.info(f"  → 글자 수: {len(post['content'])}자")

    # 3. 이미지 생성
    logger.info("[3/4] 썸네일 이미지 생성 중...")
    img_gen = ImageGenerator(hf_token=os.environ.get("HF_API_TOKEN", ""))
    timestamp = now_kst.strftime("%Y%m%d_%H%M")
    image_path = img_gen.generate(
        prompt=post["image_prompt"],
        filename=f"thumbnail_{mode}_{timestamp}.png",
        content=post["content"],
        mode=mode,
    )
    logger.info(f"  → 이미지: {image_path}")

    # 4. 이메일 발송
    logger.info("[4/4] 이메일 발송 중...")
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
