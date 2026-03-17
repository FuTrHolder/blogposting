"""
미국 증시 블로그 자동화 메인 스크립트 (완전 무료 버전)
실행 흐름: 뉴스 수집 → 글 생성(Gemini) → 이미지 생성(SD) → 티스토리 업로드
"""

import os
import sys
import logging
from datetime import datetime

from news_fetcher import NewsFetcher
from content_generator import ContentGenerator
from image_generator import ImageGenerator
from tistory_uploader import TistoryUploader

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)


def main():
    logger.info("===== 미국 증시 블로그 자동화 시작 (완전 무료) =====")
    today = datetime.now().strftime("%Y년 %m월 %d일")

    # 1. 뉴스 및 시장 데이터 수집
    logger.info("[1/4] 뉴스 및 시장 데이터 수집 중...")
    fetcher = NewsFetcher(
        alpha_vantage_key=os.environ["ALPHA_VANTAGE_API_KEY"]
    )
    market_data = fetcher.get_market_summary()
    news_list = fetcher.get_top_news(limit=8)

    if not news_list:
        logger.error("뉴스 수집 실패. 종료합니다.")
        sys.exit(1)
    logger.info(f"  → 뉴스 {len(news_list)}건 수집 완료")

    # 2. 블로그 글 생성 (Google Gemini - 무료)
    logger.info("[2/4] 블로그 글 생성 중 (Gemini 2.5 Flash - 무료)...")
    generator = ContentGenerator(
        api_key=os.environ["GEMINI_API_KEY"]
    )
    post = generator.generate_post(
        date=today,
        market_data=market_data,
        news_list=news_list,
    )
    logger.info(f"  → 글 생성 완료 (제목: {post['title']})")
    logger.info(f"  → 글자 수: {len(post['content'])}자")

    # 3. 이미지 생성 (Stable Diffusion - 무료)
    logger.info("[3/4] 썸네일 이미지 생성 중 (Stable Diffusion - 무료)...")
    img_gen = ImageGenerator(
        hf_token=os.environ.get("HF_API_TOKEN", "")
    )
    image_path = img_gen.generate(
        prompt=post["image_prompt"],
        filename=f"thumbnail_{datetime.now().strftime('%Y%m%d')}.png",
    )
    logger.info(f"  → 이미지 생성 완료: {image_path}")

    # 4. 티스토리 업로드
    logger.info("[4/4] 티스토리 업로드 중...")
    uploader = TistoryUploader(
        kakao_email=os.environ["KAKAO_EMAIL"],
        kakao_password=os.environ["KAKAO_PASSWORD"],
        blog_name=os.environ["TISTORY_BLOG_NAME"],
        category="미국",   # ← 카테고리 이름 (블로그에 맞게 변경)
    )
    result = uploader.upload(
        title=post["title"],
        content=post["content"],
        tags=post["tags"],
        image_path=image_path,
    )
    logger.info(f"  → 업로드 완료! URL: {result['url']}")
    logger.info("===== 자동화 완료 (비용: $0.00) =====")


if __name__ == "__main__":
    main()
