"""
마케팅 자동화 메인 스크립트 v8
변경사항 v8:
  - 카카오스토리채널: 공식 발행 API가 없어 자동 발행 대상에서는 제외되지만,
    kakao_post 캡션 + 블로그 URL을 대시보드(D1)에 status="skip"으로 저장해
    대시보드 상단 전용 박스에서 수동 복사할 수 있게 함
  - blog_url을 posts 테이블에도 반영 (main.py는 실제 발행 URL을 모르므로,
    이 파이프라인이 tistory_crawler로 확인한 실제 URL을 채워 넣음)

v7 유지:
  - 틱톡 전용 영상(1분+)을 대시보드(Cloudflare D1)에 업로드
      · dashboard_client.upload_media_get_public_url()로 GitHub Release에 업로드
      · dashboard_client.push_marketing_result()로 대시보드에 tiktok 결과 저장
      · 대시보드에서 영상 재생 및 다운로드 가능
  - YouTube/Facebook/Instagram/Threads 자동 발행

실행 흐름:
  1. Gist에서 처리 완료 내역 로드 → 중복 방지
  2. 티스토리 RSS 폴링 → 새 글 감지
  3. 이미 처리된 글이면 즉시 종료
  4. Gemini로 플랫폼별 콘텐츠 생성
  5a. 쇼츠/릴스용 영상 제작 (58초 이하, 속도 자동 조정)
  5b. 틱톡용 영상 제작 (1분+, 이탈률 개선판)
  6. SNS 썸네일 제작
  7. 발행용 공개 URL 확보 (GitHub Release 업로드)
  8. 기존 플랫폼 자동 발행 (YouTube/Facebook/Instagram/Threads)
  9. 틱톡 영상 → 대시보드 전용 업로드 (GitHub Release → D1)
  10. 카카오스토리채널 캡션 → 대시보드 전용 저장 (수동 게시용)
  11. 처리 완료 내역을 Gist에 저장
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

# 플랫폼별 캡션 키 매핑 (대시보드 업로드용)
_PLATFORM_TEXT_KEY = {
    "youtube":          "blog_title",
    "facebook":         "facebook_post",
    "facebook_reels":   "facebook_post",
    "instagram":        "instagram_post",
    "instagram_reels":  "instagram_post",
    "threads":          "threads_post",
    "threads_reels":    "threads_post",
    "tiktok":           "tiktok_post",   # tiktok_post 없으면 x_post로 fallback
}
_PLATFORM_THUMB_KEY = {
    "youtube":          "facebook",
    "facebook":         "facebook",
    "facebook_reels":   "facebook",
    "instagram":        "instagram",
    "instagram_reels":  "instagram",
    "threads":          "threads",
    "threads_reels":    "threads",
    "tiktok":           "facebook",  # 틱톡은 썸네일 대신 tiktok_video가 메인
}
# 대시보드에 영상을 보여줘야 하는 플랫폼
_VIDEO_RESULT_PLATFORMS = {"youtube", "facebook_reels", "instagram_reels", "threads_reels", "tiktok"}


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
        "S&P":   "S&P 500 trading",
        "반도체": "semiconductor technology",
        "빅테크": "big tech silicon valley",
        "연준":   "federal reserve wall street",
        "금리":   "interest rate finance",
        "AI":    "artificial intelligence technology",
        "엔비디아": "nvidia gpu technology",
        "애플":   "apple technology",
        "테슬라":  "tesla electric vehicle",
        "주식":   "stock market trading floor",
        "마감":   "wall street closing bell",
        "유가":   "oil energy market",
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
        text_key  = _PLATFORM_TEXT_KEY.get(platform)
        thumb_key = _PLATFORM_THUMB_KEY.get(platform)

        # 캡션 텍스트
        content_text = ""
        if text_key:
            content_text = content.get(text_key, "")
            # tiktok_post 없으면 x_post → instagram_post 순으로 fallback
            if platform == "tiktok" and not content_text:
                content_text = content.get("x_post", "") or content.get("instagram_post", "")[:280]

        # 썸네일 경로
        thumbnail_path = media_paths.get(thumb_key) if thumb_key else None

        # 영상 경로 (플랫폼에 따라 쇼츠 vs 틱톡 구분)
        video_path = None
        if platform in _VIDEO_RESULT_PLATFORMS:
            if platform == "tiktok":
                video_path = media_paths.get("tiktok_video")
            else:
                video_path = media_paths.get("video")

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
            blog_url=content.get("blog_url", ""),
        )

    # ── 카카오스토리채널: 자동 발행 대상이 아니므로 results에는 없지만,
    # 대시보드 상단 전용 박스에 본문+블로그링크를 수동 복사용으로 표시하기
    # 위해 kakao_post 캡션만 별도로 대시보드에 저장합니다. status는 항상
    # "skip"으로 기록해(발행을 시도한 적이 없으므로 ok/error가 아님) 마케팅
    # 카드 그리드에는 노출되지 않고(app.js가 platform === "kakao"는
    # 필터링), 상단 카카오 박스가 이 content_text를 읽어와 표시합니다.
    kakao_text = content.get("kakao_post", "")
    if kakao_text:
        blog_url = content.get("blog_url", "")
        if blog_url:
            kakao_text = kakao_text.replace("[블로그 URL]", blog_url)
            kakao_text = kakao_text.replace("[Blog URL]", blog_url)
        dashboard_client.push_marketing_result(
            post_date=post_date,
            mode=mode,
            platform="kakao",
            status="skip",
            message="수동 게시 대기 (카카오스토리채널 공식 API 미지원)",
            url="",
            content_text=kakao_text,
            thumbnail_path=media_paths.get("facebook"),
            video_path=None,
            blog_url=content.get("blog_url", ""),
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

    post_id      = post.get("post_id", "")
    post_title   = post.get("title", "")
    post_url     = post.get("url", "")
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

    # ── 3. 영상 생성 ─────────────────────────────────────────────────────
    video_gen = VideoGenerator(output_dir="videos")

    # 3a. 쇼츠/릴스용 영상 (58초 이하, 속도 자동 조정)
    logger.info("[3a/7] 쇼츠/릴스용 영상 생성 중... (58초 이하)")
    video_path = None
    try:
        shorts_filename = f"shorts_{post['mode']}_{timestamp}.mp4"
        video_path      = video_gen.generate_with_text_only_fallback(
            script=content.get("youtube_script", []),
            mode=post["mode"],
            filename=shorts_filename,
            thumbnail_url=post.get("thumbnail_url", ""),
            blog_url=post.get("url", ""),
            bg_keywords=bg_keywords,
            blog_content=blog_content,
            blog_title=post_title,
        )
        logger.info(f"  → 쇼츠 영상 저장: {video_path}")
        state.add_log("SHORTS_VIDEO_GENERATED", f"쇼츠 생성: {video_path}", post_id=post_id)
    except Exception as e:
        logger.warning(f"  → 쇼츠 영상 생성 실패 (계속): {e}")
        state.add_log("SHORTS_VIDEO_FAILED", str(e), post_id=post_id, level="WARNING")

    # 3b. 틱톡용 영상 (1분+, 이탈률 개선판: 짧은 세그먼트 + 여성 목소리 + 해시태그)
    logger.info("[3b/7] 틱톡용 영상 생성 중... (1분+, 이탈률 개선 버전)")
    tiktok_video_path = None
    try:
        tiktok_filename   = f"tiktok_{post['mode']}_{timestamp}.mp4"
        tiktok_video_path = video_gen.generate_tiktok_with_fallback(
            script=content.get("tiktok_script", content.get("youtube_script", [])),
            mode=post["mode"],
            filename=tiktok_filename,
            thumbnail_url=post.get("thumbnail_url", ""),
            blog_url=post.get("url", ""),
            # bg_keywords를 쇼츠와 공유하지 않고 비워서, generate_tiktok() 내부의
            # PEXELS_KEYWORDS_TIKTOK(더 역동적인 톤)이 적용되도록 함 — 채널별
            # 배경 차별화(쇼츠는 차분한 정보 전달 톤, 틱톡은 화제성 있는 톤)
            bg_keywords=None,
            blog_content=blog_content,
            blog_title=post_title,
        )
        logger.info(f"  → 틱톡 영상 저장: {tiktok_video_path}")
        state.add_log("TIKTOK_VIDEO_GENERATED", f"틱톡 생성: {tiktok_video_path}",
                      post_id=post_id)

        # 영상 생성 과정에서 함께 만들어진 해시태그를 캡션에 자동 반영
        # (검색 유입 개선 — content_adapter가 만든 tiktok_post/x_post 캡션
        #  끝에 아직 해시태그가 없으면 추가)
        generated_tags = getattr(video_gen, "last_tiktok_hashtags", [])
        if generated_tags:
            tag_line = " ".join(f"#{t}" for t in generated_tags)
            for key in ("tiktok_post", "x_post"):
                existing = content.get(key, "")
                if existing and "#" not in existing:
                    content[key] = f"{existing}\n\n{tag_line}"
            content["tiktok_hashtags"] = generated_tags
            logger.info(f"  → 틱톡 해시태그 반영: {generated_tags}")
    except Exception as e:
        logger.warning(f"  → 틱톡 영상 생성 실패 (계속): {e}")
        state.add_log("TIKTOK_VIDEO_FAILED", str(e), post_id=post_id, level="WARNING")

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
            image_prompt=content.get("thumbnail_prompt", ""),
        )
        logger.info(f"  → 썸네일 {len(thumb_paths)}개 생성 완료")
        state.add_log("THUMBNAILS_GENERATED", f"썸네일 {len(thumb_paths)}개", post_id=post_id)
    except Exception as e:
        logger.warning(f"  → 썸네일 생성 실패: {e}")
        state.add_log("THUMBNAILS_FAILED", str(e), post_id=post_id, level="WARNING")

    media_paths = {**thumb_paths}
    if video_path:
        media_paths["video"]        = video_path
    if tiktok_video_path:
        media_paths["tiktok_video"] = tiktok_video_path

    # ── 5. 발행용 공개 URL 확보 (GitHub Release 업로드) ──────────────────
    # Threads/Instagram Reels/Facebook Reels가 사용할 공개 URL
    logger.info("[5/7] 발행용 공개 URL 확보 중 (GitHub Release 업로드)...")
    try:
        if thumb_paths.get("threads"):
            url = dashboard_client.upload_media_get_public_url(
                thumb_paths["threads"],
                f"sns_threads_{post['mode']}_{timestamp}.jpg"
            )
            if url:
                content["threads_thumbnail_url"] = url

        if thumb_paths.get("instagram"):
            url = dashboard_client.upload_media_get_public_url(
                thumb_paths["instagram"],
                f"sns_instagram_{post['mode']}_{timestamp}.jpg"
            )
            if url:
                content["instagram_thumbnail_url"] = url

        if video_path:
            url = dashboard_client.upload_media_get_public_url(
                video_path,
                f"reels_{post['mode']}_{timestamp}.mp4"
            )
            if url:
                content["video_public_url"] = url

        acquired = [k for k in (
            "threads_thumbnail_url", "instagram_thumbnail_url", "video_public_url"
        ) if content.get(k)]
        logger.info(f"  → 확보된 공개 URL: {acquired or '없음'}")
        state.add_log("PUBLIC_URLS_READY", f"공개 URL: {acquired or '없음'}", post_id=post_id)
    except Exception as e:
        logger.warning(f"  → 공개 URL 확보 중 예외 (계속): {e}")
        state.add_log("PUBLIC_URLS_FAILED", str(e), post_id=post_id, level="WARNING")

    # ── 6. 기존 플랫폼 발행 (YouTube/Facebook/Instagram/Threads/Kakao) ───
    logger.info("[6/7] 플랫폼 발행 중 (YouTube/Facebook/Instagram/Threads/Kakao)...")
    dispatcher = PublisherDispatcher()
    results    = dispatcher.publish_all(content=content, media_paths=media_paths)

    # ── 틱톡 영상 대시보드 업로드 (발행 결과로 기록) ──────────────────────
    # TikTok은 공식 API 미지원으로 직접 발행 불가.
    # 대신 틱톡 영상을 GitHub Release에 올리고 대시보드 D1에 기록해두면
    # 대시보드에서 영상 재생 및 다운로드 버튼으로 수동 업로드 가능.
    if tiktok_video_path:
        logger.info("  → 틱톡 영상 GitHub Release 업로드 중...")
        try:
            tiktok_public_url = dashboard_client.upload_media_get_public_url(
                tiktok_video_path,
                f"tiktok_{post['mode']}_{timestamp}.mp4"
            )
            if tiktok_public_url:
                results["tiktok"] = {
                    "status":  "ok",
                    "url":     tiktok_public_url,
                    "message": f"대시보드 업로드 완료 (수동 TikTok 업로드 필요) → {tiktok_public_url[:60]}...",
                }
                logger.info(f"  → 틱톡 영상 업로드 완료: {tiktok_public_url[:60]}...")
            else:
                results["tiktok"] = {
                    "status":  "error",
                    "url":     "",
                    "message": "GitHub Release 업로드 실패 (DASHBOARD_API_URL 또는 GH_RELEASE_TOKEN 확인)",
                }
                logger.warning("  → 틱톡 영상 업로드 실패")
        except Exception as e:
            results["tiktok"] = {"status": "error", "url": "", "message": str(e)}
            logger.warning(f"  → 틱톡 영상 업로드 예외: {e}")
    else:
        results["tiktok"] = {
            "status":  "skip",
            "url":     "",
            "message": "틱톡 영상 생성 실패 또는 미생성",
        }

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
        logger.warning(f"  → 대시보드 업로드 예외 (무시하고 종료): {e}")

    logger.info("=" * 60)
    logger.info("마케팅 자동화 완료")
    logger.info("=" * 60)

    if error_count > 0 and ok_count == 0 and skip_count == 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
