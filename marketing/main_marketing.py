"""
마케팅 자동화 메인 스크립트 v6
변경사항 v6:
  - 각 플랫폼 발행 결과(링크·썸네일·영상·캡션)를 Cloudflare 대시보드(D1 + R2)에 업로드하는
    [6/6] 단계 추가. DASHBOARD_API_URL 미설정 시 자동으로 건너뜀.
  - 기존 로직(v5) 동일 유지: VideoGenerator에 blog_content, blog_title 전달

실행 흐름:
  1. Gist에서 처리 완료 내역 로드 → 중복 방지
  2. 티스토리 RSS 폴링 → 새 글 감지
  3. 이미 처리된 글이면 즉시 종료
  4. Gemini로 플랫폼별 콘텐츠 생성
  5. 영상 제작 (블로그 본문 기반 나래이션 숏폼)
  6. SNS 썸네일 제작
  7. 각 플랫폼 자동 발행
  8. 처리 완료 내역을 Gist에 저장
  9. 채널별 발행 결과를 Cloudflare 대시보드에 업로드
"""

import os
import re
import sys
import logging
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from tistory_crawler.crawler import TistoryCrawler
from content_adapter.adapter import ContentAdapter
from video_generator.generator import VideoGenerator
from video_generator.thumbnail import SNSThumbnailGenerator
from platform_publishers.publishers import PublisherDispatcher
from state_manager import GistStateManager
import dashboard_client

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)

KST = timezone(timedelta(hours=9))

# 플랫폼별 캡션/썸네일 매핑 (대시보드 업로드용)
_PLATFORM_TEXT_KEY = {
    "youtube": "blog_title",
    "facebook": "facebook_post",
    "instagram": "instagram_post",
    "threads": "threads_post",
    "kakao": "kakao_post",
}
_PLATFORM_THUMB_KEY = {
    "youtube": "facebook",         # 별도 유튜브 전용 썸네일이 없으므로 대표 썸네일 재사용
    "facebook": "facebook",
    "instagram": "instagram",
    "threads": "threads",
    "kakao": "kakao",
}


# ── 키워드 추출 헬퍼 ───────────────────────────────────────────────────────

def _extract_bg_keywords(post: dict, content: dict) -> list[str]:
    thumb_prompt = content.get("thumbnail_prompt", "")
    if thumb_prompt:
        words         = re.findall(r"[a-zA-Z]+", thumb_prompt)
        english_words = [w for w in words if len(w) > 3][:5]
        if english_words:
            return english_words

    tags  = post.get("tags", [])
    title = post.get("title", "")
    text  = " ".join(tags) + " " + title

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
        "유가": "oil energy market",
    }
    for ko, en in keyword_map.items():
        if ko in text:
            return en.split()

    mode = post.get("mode", "morning")
    return (["stock market", "nasdaq", "wall street"]
            if mode == "morning"
            else ["city night", "finance", "market"])


def _push_results_to_dashboard(
    post_date: str,
    mode: str,
    content: dict,
    media_paths: dict,
    results: dict,
):
    """채널별 발행 결과를 Cloudflare 대시보드에 업로드합니다. 실패해도 무시하고 진행."""
    for platform, result in results.items():
        text_key = _PLATFORM_TEXT_KEY.get(platform)
        thumb_key = _PLATFORM_THUMB_KEY.get(platform)

        content_text = content.get(text_key, "") if text_key else ""
        thumbnail_path = media_paths.get(thumb_key) if thumb_key else None
        video_path = media_paths.get("video") if platform == "youtube" else None

        dashboard_client.push_marketing_result(
            post_date=post_date,
            mode=mode,
            platform=platform,
            status=result.get("status", ""),
            message=result.get("message", ""),
            url=result.get("url", ""),
            content_text=content_text,
            thumbnail_path=thumbnail_path,
            video_path=video_path,
        )


# ── 메인 ──────────────────────────────────────────────────────────────────

def main():
    force     = os.environ.get("FORCE_CRAWL", "false").lower() == "true"
    now_kst   = datetime.now(KST)
    timestamp = now_kst.strftime("%Y%m%d_%H%M")
    post_date_str = now_kst.strftime("%Y-%m-%d")

    logger.info("=" * 60)
    logger.info("마케팅 자동화 시작")
    logger.info(f"실행 시각: {now_kst.strftime('%Y-%m-%d %H:%M:%S KST')}")
    logger.info("=" * 60)

    # ── 상태 관리자 초기화 ────────────────────────────────────────────────
    state = GistStateManager()
    state.load()
    state.add_log("RUN_START", f"마케팅 자동화 시작 (force={force})")

    # ── 1. 티스토리 새 글 감지 ────────────────────────────────────────────
    logger.info("[1/6] 티스토리 RSS 크롤링 중...")
    crawler = TistoryCrawler()
    post    = crawler.get_post_as_dict(force=True)

    if not post:
        msg = "RSS에서 글을 가져올 수 없음. 종료."
        logger.info(msg)
        state.add_log("NO_POST", msg, level="WARNING")
        state.save()
        sys.exit(0)

    post_id    = post.get("post_id", "")
    post_title = post.get("title", "")
    post_url   = post.get("url", "")
    # 블로그 본문 (나래이션 생성에 사용)
    blog_content = post.get("full_text", "") or post.get("summary", "")

    logger.info(f"  → 최신 글: {post_title}")
    logger.info(f"  → post_id: {post_id}")
    logger.info(f"  → URL: {post_url}")
    logger.info(f"  → 본문 길이: {len(blog_content)}자")

    # ── Gist 중복 체크 ────────────────────────────────────────────────────
    if not force and state.is_already_processed(post_id):
        msg = f"이미 처리된 포스트. 종료. (post_id: {post_id})"
        logger.info(f"⏭️  {msg}")
        state.add_log("SKIP_DUPLICATE", msg, post_id=post_id, post_title=post_title)
        state.save()
        sys.exit(0)

    if force and state.is_already_processed(post_id):
        logger.warning(f"⚠️  force=true 이므로 재실행: {post_title}")
        state.add_log("FORCE_RERUN", f"force=true로 재실행: {post_title}",
                      post_id=post_id, post_title=post_title, level="WARNING")

    state.add_log("PROCESS_START", f"포스트 처리 시작: {post_title}",
                  post_id=post_id, post_title=post_title)
    logger.info(f"  → 모드: {post['mode']} | 새 글 처리 시작")

    # ── 2. 멀티플랫폼 콘텐츠 생성 ────────────────────────────────────────
    logger.info("[2/6] Gemini 콘텐츠 어댑터 실행 중...")
    try:
        adapter = ContentAdapter(api_key=os.environ["GEMINI_API_KEY"])
        content = adapter.generate_all(post)
        content["blog_thumbnail_url"] = post.get("thumbnail_url", "")
        logger.info("  → 플랫폼별 텍스트 생성 완료")
        state.add_log("CONTENT_GENERATED", "Gemini 콘텐츠 생성 완료", post_id=post_id)
    except Exception as e:
        msg = f"Gemini 콘텐츠 생성 실패: {e}"
        logger.error(f"  → {msg}")
        state.add_log("CONTENT_FAILED", msg, post_id=post_id,
                      post_title=post_title, level="ERROR")
        state.mark_as_processed(post_id, post_title, post_url,
                                 {"error": {"status": "error", "message": msg}})
        state.save()
        sys.exit(1)

    bg_keywords = _extract_bg_keywords(post, content)
    logger.info(f"  → 배경 이미지 키워드: {bg_keywords}")

    # ── 3. 영상 생성 (블로그 본문 기반 나래이션) ─────────────────────────
    logger.info("[3/6] 영상 생성 중... (본문 기반 나래이션 + TTS, BGM 없음)")
    video_path = None
    try:
        video_gen      = VideoGenerator(output_dir="videos")
        video_filename = f"shorts_{post['mode']}_{timestamp}.mp4"
        video_path     = video_gen.generate_with_text_only_fallback(
            script=content.get("youtube_script", []),
            mode=post["mode"],
            filename=video_filename,
            thumbnail_url=post.get("thumbnail_url", ""),
            blog_url=post.get("url", ""),
            bg_keywords=bg_keywords,
            blog_content=blog_content,      # ← 블로그 본문 전달 (나래이션용)
            blog_title=post_title,           # ← 블로그 제목 전달
        )
        logger.info(f"  → 영상 저장: {video_path}")
        state.add_log("VIDEO_GENERATED", f"영상 생성 완료: {video_path}", post_id=post_id)
    except Exception as e:
        logger.warning(f"  → 영상 생성 실패 (계속): {e}")
        state.add_log("VIDEO_FAILED", str(e), post_id=post_id, level="WARNING")

    # ── 4. SNS 썸네일 생성 ───────────────────────────────────────────────
    logger.info("[4/6] SNS 썸네일 생성 중...")
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
            content=content,
        )
        logger.info(f"  → 썸네일 {len(thumb_paths)}개 생성 완료")
        state.add_log("THUMBNAILS_GENERATED", f"썸네일 {len(thumb_paths)}개", post_id=post_id)
    except Exception as e:
        logger.warning(f"  → 썸네일 생성 실패: {e}")
        state.add_log("THUMBNAILS_FAILED", str(e), post_id=post_id, level="WARNING")

    media_paths = {**thumb_paths}
    if video_path:
        media_paths["video"] = video_path

    # ── 5. 플랫폼 발행 ───────────────────────────────────────────────────
    logger.info("[5/6] 플랫폼 발행 중...")
    dispatcher = PublisherDispatcher()
    results    = dispatcher.publish_all(content=content, media_paths=media_paths)

    # ── 결과 요약 ─────────────────────────────────────────────────────────
    logger.info("\n" + "=" * 60)
    logger.info("발행 결과 요약")
    logger.info("=" * 60)
    ok_count    = sum(1 for r in results.values() if r["status"] == "ok")
    skip_count  = sum(1 for r in results.values() if r["status"] == "skip")
    error_count = sum(1 for r in results.values() if r["status"] == "error")

    for platform, result in results.items():
        icon = {"ok": "✅", "skip": "⏭️", "error": "❌"}.get(result["status"], "?")
        msg  = result.get("message", "")
        url  = result.get("url", "")
        logger.info(f"  {icon} {platform.upper()}: {msg} {url}")

    logger.info(f"\n성공: {ok_count} | 건너뜀: {skip_count} | 실패: {error_count}")

    # ── 처리 완료 기록 ────────────────────────────────────────────────────
    state.mark_as_processed(post_id, post_title, post_url, results)
    state.add_log(
        "PROCESS_DONE",
        f"처리 완료 (성공: {ok_count}, 건너뜀: {skip_count}, 실패: {error_count})",
        post_id=post_id, post_title=post_title,
    )
    state.save()

    # ── 6. Cloudflare 대시보드에 채널별 결과 업로드 ───────────────────────
    logger.info("[6/6] Cloudflare 대시보드 업로드 중...")
    try:
        _push_results_to_dashboard(
            post_date=post_date_str,
            mode=post["mode"],
            content=content,
            media_paths=media_paths,
            results=results,
        )
        logger.info("  → 대시보드 업로드 완료 (DASHBOARD_API_URL 미설정 시 건너뜀)")
    except Exception as e:
        logger.warning(f"  → 대시보드 업로드 중 예외 발생 (무시하고 종료): {e}")

    logger.info("=" * 60)
    logger.info("마케팅 자동화 완료")
    logger.info("=" * 60)

    if error_count > 0 and ok_count == 0 and skip_count == 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
