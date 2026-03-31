"""
미국 증시 블로그 자동화 메인 스크립트 (이메일 발송 버전)
실행 흐름: 뉴스 수집 → 글 생성(Gemini) → 이미지 생성(SD/Unsplash) → 이메일 발송(Gmail)

포스팅 모드:
  morning (오전 9시 KST) : 미국 전일 증시 마감 리뷰
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


def main():
    mode = detect_mode()
    now_kst = datetime.now(KST)

    if mode == "morning":
        # 오전 9시 포스팅 → 미국 전일(한국 기준 어제) 날짜 표기
        us_date = (now_kst - timedelta(days=1)).strftime("%Y년 %m월 %d일")
        post_label = "전일 마감 리뷰"
    else:
        # 저녁 9시 포스팅 → 미국 당일(오늘) 날짜 표기
        us_date = now_kst.strftime("%Y년 %m월 %d일")
        post_label = "당일 프리마켓 & 이슈"

    logger.info(f"===== 미국 증시 블로그 자동화 시작 [{mode.upper()} / {post_label}] =====")
    logger.info(f"기준 날짜: {us_date}")

    # 1. 뉴스 및 시장 데이터 수집
    logger.info("[1/4] 뉴스 및 시장 데이터 수집 중...")
    fetcher = NewsFetcher(alpha_vantage_key=os.environ["ALPHA_VANTAGE_API_KEY"])
    market_data = fetcher.get_market_summary()
    news_list = fetcher.get_top_news(limit=8)
    if not news_list:
        logger.error("뉴스 수집 실패. 종료합니다.")
        sys.exit(1)
    logger.info(f"  → 뉴스 {len(news_list)}건 수집 완료")

    # 2. 블로그 글 생성 (모드 전달)
    logger.info(f"[2/4] 블로그 글 생성 중 (Gemini / {mode})...")
    generator = ContentGenerator(api_key=os.environ["GEMINI_API_KEY"])
    post = generator.generate_post(
        date=us_date,
        market_data=market_data,
        news_list=news_list,
        mode=mode,
    )
    logger.info(f"  → 제목: {post['title']}")
    logger.info(f"  → 글자 수: {len(post['content'])}자")

    # 3. 이미지 생성 (모드 전달 → 썸네일 분위기 다르게)
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
