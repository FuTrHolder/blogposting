"""
마케팅 자동화 메인 스크립트 v2
실행 흐름:
  1. 티스토리 RSS 폴링 → 새 글 감지
  2. Gemini로 플랫폼별 콘텐츠 생성
  3. 영상 제작 (YouTube Shorts / TikTok)
     - 배경 이미지: 본문 키워드 → Unsplash 자동 다운로드
     - TTS 내레이션: edge-tts ko-KR-InJoonNeural (젊은 남성)
     - 배경음악: Pixabay CC0 BGM 자동 믹싱
     - 텍스트 오버레이: 제목 + 본문 요약
  4. SNS 썸네일 제작 (Facebook / Threads / Instagram)
  5. 각 플랫폼 자동 발행

환경변수 (GitHub Secrets):
  # 필수
  GEMINI_API_KEY

  # YouTube
  YOUTUBE_CLIENT_ID, YOUTUBE_CLIENT_SECRET, YOUTUBE_REFRESH_TOKEN

  # 선택 (비즈니스 계정 필요)
  TIKTOK_ACCESS_TOKEN, TIKTOK_OPEN_ID

  # SNS (무료)
  FACEBOOK_PAGE_ID, FACEBOOK_PAGE_ACCESS_TOKEN
  THREADS_USER_ID, THREADS_ACCESS_TOKEN

  # X (유료, X_ENABLED=true 설정 시 활성화)
  X_ENABLED, X_API_KEY, X_API_SECRET, X_ACCESS_TOKEN, X_ACCESS_TOKEN_SECRET

  # 카카오 (이메일 대체)
  GMAIL_ADDRESS, GMAIL_APP_PASSWORD, RECIPIENT_EMAIL

  # 이미지 생성 (선택)
  HF_API_TOKEN
"""

import os
import re
import sys
import logging
from datetime import datetime, timezone, timedelta

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


# ── 키워드 추출 헬퍼 ───────────────────────────────────────────────────────

def _extract_bg_keywords(post: dict, content: dict) -> list[str]:
    """
    Unsplash 배경 이미지 검색에 쓸 영문 키워드를 추출합니다.
    1순위: Gemini가 생성한 thumbnail_prompt (영문 프롬프트에서 앞 3단어)
    2순위: 제목/태그에서 금융 관련 영문 키워드 매핑
    """
    # 1순위: thumbnail_prompt
    thumb_prompt = content.get("thumbnail_prompt", "")
    if thumb_prompt:
        # 영문 단어만 추출 (한글/특수문자 제거)
        words = re.findall(r"[a-zA-Z]+", thumb_prompt)
        english_words = [w for w in words if len(w) > 3][:5]
        if english_words:
            return english_words

    # 2순위: 태그/제목 기반 키워드 매핑
    tags = post.get("tags", [])
    title = post.get("title", "")
    text = " ".join(tags) + " " + title

    keyword_map = {
        "나스닥": "nasdaq stock market",
        "S&P": "S&P 500 trading",
        "반도체": "semiconductor technology",
        "빅테크": "big tech silicon valley",
        "연준": "federal reserve wall street",
        "금리": "interest rate finance",
        "AI": "artificial intelligence technology",
        "엔비디아": "nvidia gpu technology",
        "애플": "apple technology",
        "테슬라": "tesla electric vehicle",
        "주식": "stock market trading floor",
        "마감": "wall street closing bell",
    }
    for ko, en in keyword_map.items():
        if ko in text:
            return en.split()

    mode = post.get("mode", "morning")
    return ["stock market", "nasdaq", "wall street"] if mode == "morning" else ["city night", "finance", "market"]


# ── 메인 ──────────────────────────────────────────────────────────────────

def main():
    force = os.environ.get("FORCE_CRAWL", "false").lower() == "true"
    now_kst = datetime.now(KST)
    timestamp = now_kst.strftime("%Y%m%d_%H%M")

    logger.info("===== 마케팅 자동화 시작 =====")

    # ── 1. 티스토리 새 글 감지 ────────────────────────────────────────────
    logger.info("[1/5] 티스토리 크롤링 중...")
    crawler = TistoryCrawler()
    post = crawler.get_post_as_dict(force=force)

    if not post:
        logger.info("새 글 없음. 종료.")
        sys.exit(0)

    logger.info(f"  → 새 글 감지: {post['title']}")
    logger.info(f"  → 모드: {post['mode']} | URL: {post['url']}")

    # ── 2. 멀티플랫폼 콘텐츠 생성 ────────────────────────────────────────
    logger.info("[2/5] Gemini 콘텐츠 어댑터 실행 중...")
    adapter = ContentAdapter(api_key=os.environ["GEMINI_API_KEY"])
    content = adapter.generate_all(post)
    content["blog_thumbnail_url"] = post.get("thumbnail_url", "")
    logger.info("  → 플랫폼별 텍스트 생성 완료")
    logger.info(f"  → YouTube 슬라이드: {len(content.get('youtube_script', []))}장")

    # 배경 이미지 키워드 추출
    bg_keywords = _extract_bg_keywords(post, content)
    logger.info(f"  → 배경 이미지 키워드: {bg_keywords}")

    # ── 3. 영상 생성 ──────────────────────────────────────────────────────
    logger.info("[3/5] 영상 생성 중... (배경 이미지 + TTS + BGM)")
    video_path = None
    try:
        video_gen = VideoGenerator(output_dir="videos")
        video_filename = f"shorts_{post['mode']}_{timestamp}.mp4"
        # generate_with_text_only_fallback: 실패 시 기존 방식으로 자동 폴백
        video_path = video_gen.generate_with_text_only_fallback(
            script=content.get("youtube_script", []),
            mode=post["mode"],
            filename=video_filename,
            thumbnail_url=post.get("thumbnail_url", ""),
            blog_url=post.get("url", ""),
            bg_keywords=bg_keywords,
        )
        logger.info(f"  → 영상 저장: {video_path}")
    except Exception as e:
        logger.warning(f"  → 영상 생성 실패 (텍스트 게시는 계속): {e}")

    # ── 4. SNS 썸네일 생성 ───────────────────────────────────────────────
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

    media_paths = {**thumb_paths}
    if video_path:
        media_paths["video"] = video_path

    # ── 5. 플랫폼 발행 ───────────────────────────────────────────────────
    logger.info("[5/5] 플랫폼 발행 중...")
    dispatcher = PublisherDispatcher()
    results = dispatcher.publish_all(content=content, media_paths=media_paths)

    # ── 결과 요약 ─────────────────────────────────────────────────────────
    logger.info("\n===== 발행 결과 요약 =====")
    ok_count = sum(1 for r in results.values() if r["status"] == "ok")
    skip_count = sum(1 for r in results.values() if r["status"] == "skip")
    error_count = sum(1 for r in results.values() if r["status"] == "error")

    for platform, result in results.items():
        icon = {"ok": "✅", "skip": "⏭️", "error": "❌"}.get(result["status"], "?")
        msg = result.get("message", "")
        url = result.get("url", "")
        logger.info(f"  {icon} {platform.upper()}: {msg} {url}")

    logger.info(f"\n성공: {ok_count} | 건너뜀: {skip_count} | 실패: {error_count}")
    logger.info("===== 마케팅 자동화 완료 =====")

    if error_count > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
