"""
마케팅 자동화 메인 스크립트
실행 흐름:
  1. 티스토리 RSS 폴링 → 새 글 감지
  2. Gemini로 플랫폼별 콘텐츠 생성
  3. 영상 제작 (YouTube Shorts / TikTok)
  4. SNS 썸네일 제작 (Facebook / Threads)
  5. 각 플랫폼 자동 발행

환경변수 (GitHub Secrets):
  # 필수 (콘텐츠 생성)
  GEMINI_API_KEY

  # 영상 플랫폼
  YOUTUBE_CLIENT_ID, YOUTUBE_CLIENT_SECRET, YOUTUBE_REFRESH_TOKEN

  # 선택 (비즈니스 계정 필요)
  TIKTOK_ACCESS_TOKEN, TIKTOK_OPEN_ID

  # SNS (무료)
  FACEBOOK_PAGE_ID, FACEBOOK_PAGE_ACCESS_TOKEN
  THREADS_USER_ID, THREADS_ACCESS_TOKEN

  # X (유료, X_ENABLED=true 설정 시 활성화)
  X_ENABLED, X_API_KEY, X_API_SECRET, X_ACCESS_TOKEN, X_ACCESS_TOKEN_SECRET

  # 카카오 (이메일 대체, 기존 변수 재활용)
  GMAIL_ADDRESS, GMAIL_APP_PASSWORD, RECIPIENT_EMAIL

  # 이미지 생성 (선택)
  HF_API_TOKEN
"""

import os
import sys
import logging
from datetime import datetime, timezone, timedelta

# 모듈 경로 추가
sys.path.insert(0, os.path.dirname(__file__))
from tistory_crawler.crawler import TistoryCrawler
from content_adapter.adapter import ContentAdapter
from video_generator.generator import VideoGenerator
from video_generator.thumbnail import SNSThumbnailGenerator
from platform_publishers.publishers import PublisherDispatcher

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)

KST = timezone(timedelta(hours=9))


def main():
    force = os.environ.get("FORCE_CRAWL", "false").lower() == "true"
    now_kst = datetime.now(KST)
    timestamp = now_kst.strftime("%Y%m%d_%H%M")

    logger.info("===== 마케팅 자동화 시작 =====")

    # ── 1. 티스토리 새 글 감지 ─────────────────────────────────────────────
    logger.info("[1/5] 티스토리 크롤링 중...")
    crawler = TistoryCrawler()
    post = crawler.get_post_as_dict(force=force)

    if not post:
        logger.info("새 글 없음. 종료.")
        sys.exit(0)

    logger.info(f"  → 새 글 감지: {post['title']}")
    logger.info(f"  → 모드: {post['mode']} | URL: {post['url']}")

    # ── 2. 멀티플랫폼 콘텐츠 생성 ─────────────────────────────────────────
    logger.info("[2/5] Gemini 콘텐츠 어댑터 실행 중...")
    adapter = ContentAdapter(api_key=os.environ["GEMINI_API_KEY"])
    content = adapter.generate_all(post)
    # Instagram 이미지 게시에 사용할 썸네일 URL을 content에 주입
    content["blog_thumbnail_url"] = post.get("thumbnail_url", "")
    logger.info("  → 플랫폼별 텍스트 생성 완료")
    logger.info(f"  → YouTube 슬라이드: {len(content.get('youtube_script', []))}장")

    # ── 3. 영상 생성 ───────────────────────────────────────────────────────
    logger.info("[3/5] 영상 생성 중...")
    video_path = None
    try:
        video_gen = VideoGenerator(output_dir="videos")
        video_filename = f"shorts_{post['mode']}_{timestamp}.mp4"
        video_path = video_gen.generate(
            script=content.get("youtube_script", []),
            mode=post["mode"],
            filename=video_filename,
            thumbnail_url=post.get("thumbnail_url", ""),
            blog_url=post.get("url", ""),
        )
        logger.info(f"  → 영상 저장: {video_path}")
    except Exception as e:
        logger.warning(f"  → 영상 생성 실패 (텍스트 게시는 계속): {e}")

    # ── 4. SNS 썸네일 생성 ────────────────────────────────────────────────
    logger.info("[4/5] SNS 썸네일 생성 중...")
    thumb_paths = {}
    try:
        thumb_gen = SNSThumbnailGenerator(
            hf_token=os.environ.get("HF_API_TOKEN", ""),
            output_dir="images",
        )
        thumb_paths = thumb_gen.generate_all(
            title=post["title"],
            mode=post["mode"],
            thumbnail_url=post.get("thumbnail_url", ""),
            blog_url="seedsup.tistory.com",
            timestamp=timestamp,
        )
        logger.info(f"  → 썸네일 {len(thumb_paths)}개 생성 완료")
    except Exception as e:
        logger.warning(f"  → 썸네일 생성 실패: {e}")

    # 미디어 경로 합치기
    media_paths = {**thumb_paths}
    if video_path:
        media_paths["video"] = video_path

    # ── 5. 플랫폼 발행 ────────────────────────────────────────────────────
    logger.info("[5/5] 플랫폼 발행 중...")
    dispatcher = PublisherDispatcher()
    results = dispatcher.publish_all(content=content, media_paths=media_paths)

    # ── 결과 요약 ─────────────────────────────────────────────────────────
    logger.info("\n===== 발행 결과 요약 =====")
    ok_count = sum(1 for r in results.values() if r["status"] == "ok")
    skip_count = sum(1 for r in results.values() if r["status"] == "skip")
    error_count = sum(1 for r in results.values() if r["status"] == "error")

    for platform, result in results.items():
        status = result["status"]
        icon = {"ok": "✅", "skip": "⏭️", "error": "❌"}.get(status, "?")
        msg = result.get("message", "")
        url = result.get("url", "")
        logger.info(f"  {icon} {platform.upper()}: {msg} {url}")

    logger.info(f"\n성공: {ok_count} | 건너뜀: {skip_count} | 실패: {error_count}")
    logger.info("===== 마케팅 자동화 완료 =====")

    if error_count > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
