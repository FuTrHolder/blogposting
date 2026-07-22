"""
마케팅 자동화 메인 스크립트 v7
변경사항 v7:
  - [신규] 발행 직전 "로컬 미디어 → 공개 URL" 확보 단계 추가 ([5/7]).
      Threads(이미지/영상), Instagram Reels, Facebook Reels API는 전부
      로컬 파일이 아닌 "공개적으로 접근 가능한 URL"만 받습니다. 지금까지는
      썸네일/영상이 로컬 파일로만 존재해 Threads가 이미지를 전혀 못 쓰고
      텍스트 전용으로 계속 전환되는 문제가 있었습니다(Daum CDN 차단 + 로컬
      파일은애초에 후보조차 아니었음).
      이제 SNS 썸네일과 숏폼 영상을 dashboard_client.upload_media_get_public_url()
      로 GitHub Release에 미리 업로드해 공개 URL을 확보하고, content 딕셔너리에
      threads_thumbnail_url / instagram_thumbnail_url / video_public_url로
      담아 각 Publisher가 발행 시점에 바로 쓸 수 있게 합니다.
  - [기능 추가] 숏폼 영상을 Facebook·Instagram·Threads에 '릴스'로 추가 발행.
      기존 썸네일+캡션 발행은 그대로 유지되고, 영상 공개 URL이 확보된 경우
      PublisherDispatcher가 각 플랫폼에 publish_reels()를 추가로 호출합니다
      (결과는 facebook_reels/instagram_reels/threads_reels 키로 별도 기록).
  - 기존 로직(v6) 동일 유지: 각 플랫폼 발행 결과의 Cloudflare 대시보드 업로드

실행 흐름:
  1. Gist에서 처리 완료 내역 로드 → 중복 방지
  2. 티스토리 RSS 폴링 → 새 글 감지
  3. 이미 처리된 글이면 즉시 종료
  4. Gemini로 플랫폼별 콘텐츠 생성
  5. 영상 제작 (블로그 본문 기반 나래이션 숏폼)
  6. SNS 썸네일 제작
  7. 발행용 공개 URL 확보 (GitHub Release 업로드)
  8. 각 플랫폼 자동 발행 (썸네일+캡션, 그리고 가능하면 릴스+캡션도 추가)
  9. 처리 완료 내역을 Gist에 저장
  10. 채널별 발행 결과를 Cloudflare 대시보드에 업로드
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
# *_reels 항목은 동일 플랫폼의 캡션 텍스트를 그대로 재사용합니다
# (릴스도 같은 플랫폼의 같은 콘텐츠를 영상 형태로 추가 발행하는 것이므로).
_PLATFORM_TEXT_KEY = {
    "youtube": "blog_title",
    "facebook": "facebook_post",
    "facebook_reels": "facebook_post",
    "instagram": "instagram_post",
    "instagram_reels": "instagram_post",
    "threads": "threads_post",
    "threads_reels": "threads_post",
    "kakao": "kakao_post",
}
_PLATFORM_THUMB_KEY = {
    "youtube": "facebook",         # 별도 유튜브 전용 썸네일이 없으므로 대표 썸네일 재사용
    "facebook": "facebook",
    "facebook_reels": "facebook",
    "instagram": "instagram",
    "instagram_reels": "instagram",
    "threads": "threads",
    "threads_reels": "threads",
    "kakao": "kakao",
}
# 대시보드에 썸네일 대신 영상을 보여줘야 하는 플랫폼 (릴스 발행 결과들 + 유튜브)
_VIDEO_RESULT_PLATFORMS = {"youtube", "facebook_reels", "instagram_reels", "threads_reels"}


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
        video_path = media_paths.get("video") if platform in _VIDEO_RESULT_PLATFORMS else None

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
    logger.info("[1/7] 티스토리 RSS 크롤링 중...")
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
    logger.info("[2/7] Gemini 콘텐츠 어댑터 실행 중...")
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
    logger.info("[3/7] 영상 생성 중... (본문 기반 나래이션 + TTS, BGM 없음)")
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
    logger.info("[4/7] SNS 썸네일 생성 중...")
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

    # ── 5. 발행용 공개 URL 확보 (GitHub Release 업로드) ──────────────────
    # Threads(이미지/영상), Instagram Reels, Facebook Reels API는 전부
    # "공개적으로 접근 가능한 URL"만 받고 로컬 파일은 받지 않습니다.
    # 여기서 미리 업로드해 content 딕셔너리에 채워 넣어야 각 Publisher가
    # 발행 시점에 바로 사용할 수 있습니다. 실패해도 파이프라인은 계속
    # 진행되며(해당 항목만 없는 채로 폴백), 전체를 중단시키지 않습니다.
    logger.info("[5/7] 발행용 공개 URL 확보 중 (GitHub Release 업로드)...")
    try:
        if thumb_paths.get("threads"):
            url = dashboard_client.upload_media_get_public_url(
                thumb_paths["threads"], f"sns_threads_{post['mode']}_{timestamp}.jpg"
            )
            if url:
                content["threads_thumbnail_url"] = url

        if thumb_paths.get("instagram"):
            url = dashboard_client.upload_media_get_public_url(
                thumb_paths["instagram"], f"sns_instagram_{post['mode']}_{timestamp}.jpg"
            )
            if url:
                content["instagram_thumbnail_url"] = url

        if video_path:
            url = dashboard_client.upload_media_get_public_url(
                video_path, f"reels_{post['mode']}_{timestamp}.mp4"
            )
            if url:
                content["video_public_url"] = url

        acquired = [k for k in (
            "threads_thumbnail_url", "instagram_thumbnail_url", "video_public_url"
        ) if content.get(k)]
        logger.info(f"  → 확보된 공개 URL: {acquired or '없음'}")
        state.add_log("PUBLIC_URLS_READY", f"공개 URL 확보: {acquired or '없음'}", post_id=post_id)
    except Exception as e:
        logger.warning(f"  → 공개 URL 확보 중 예외 발생 (계속 진행, 일부 플랫폼은 텍스트 전용/발행 생략될 수 있음): {e}")
        state.add_log("PUBLIC_URLS_FAILED", str(e), post_id=post_id, level="WARNING")

    # ── 6. 플랫폼 발행 (썸네일+캡션, 그리고 가능하면 릴스+캡션도 추가) ────
    logger.info("[6/7] 플랫폼 발행 중...")
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

    # ── 7. Cloudflare 대시보드에 채널별 결과 업로드 ───────────────────────
    logger.info("[7/7] Cloudflare 대시보드 업로드 중...")
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
