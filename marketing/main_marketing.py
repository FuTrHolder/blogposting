"""
마케팅 자동화 메인 스크립트 v3
실행 흐름:
  1. Gist에서 처리 완료 내역 로드 → 중복 방지
  2. 티스토리 RSS 폴링 → 새 글 감지
  3. 이미 처리된 글이면 즉시 종료 (중복 업로드 방지)
  4. Gemini로 플랫폼별 콘텐츠 생성
  5. 영상 제작 (YouTube Shorts / TikTok)
  6. SNS 썸네일 제작
  7. 각 플랫폼 자동 발행
  8. 처리 완료 내역을 Gist에 저장 (이후 중복 방지용)

중복 방지 전략:
  - GitHub Gist를 영구 스토리지로 사용 (무료, GITHUB_TOKEN 자동 제공)
  - 포스트 ID(URL MD5 해시)로 처리 여부 확인
  - 처리 성공/실패 무관하게 시도 즉시 기록 → 재시도 방지
  - GIST_ID를 GitHub Secrets에 저장하면 영속성 보장

환경변수 (GitHub Secrets):
  # 자동 제공 (별도 설정 불필요)
  GITHUB_TOKEN      : GitHub Actions에서 자동 주입

  # 최초 실행 후 Gist ID 저장 (선택, 없으면 매번 새 Gist 생성)
  GIST_ID           : GitHub Secrets에 저장 권장

  # 필수
  GEMINI_API_KEY

  # YouTube
  YOUTUBE_CLIENT_ID, YOUTUBE_CLIENT_SECRET, YOUTUBE_REFRESH_TOKEN

  # SNS (무료)
  FACEBOOK_PAGE_ID, FACEBOOK_PAGE_ACCESS_TOKEN
  THREADS_USER_ID, THREADS_ACCESS_TOKEN
  INSTAGRAM_ACCOUNT_ID, INSTAGRAM_ACCESS_TOKEN

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
from state_manager import GistStateManager

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
    1순위: Gemini가 생성한 thumbnail_prompt (영문 프롬프트에서 앞 5단어)
    2순위: 제목/태그에서 금융 관련 영문 키워드 매핑
    """
    thumb_prompt = content.get("thumbnail_prompt", "")
    if thumb_prompt:
        words = re.findall(r"[a-zA-Z]+", thumb_prompt)
        english_words = [w for w in words if len(w) > 3][:5]
        if english_words:
            return english_words

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

    logger.info("=" * 60)
    logger.info("마케팅 자동화 시작")
    logger.info(f"실행 시각: {now_kst.strftime('%Y-%m-%d %H:%M:%S KST')}")
    logger.info("=" * 60)

    # ── 상태 관리자 초기화 ────────────────────────────────────────────────
    state = GistStateManager()
    state.load()
    state.add_log("RUN_START", f"마케팅 자동화 시작 (force={force})")

    # ── 1. 티스토리 새 글 감지 ────────────────────────────────────────────
    logger.info("[1/5] 티스토리 RSS 크롤링 중...")
    crawler = TistoryCrawler()

    # force 모드일 때는 상태 파일 무시하고 최신 글 가져옴
    # 단, Gist 중복 체크는 항상 수행 (force여도 Gist 기록에 있으면 중단)
    post = crawler.get_post_as_dict(force=True)  # 항상 최신 글 가져옴 (Gist로 중복 관리)

    if not post:
        msg = "RSS에서 글을 가져올 수 없음. 종료."
        logger.info(msg)
        state.add_log("NO_POST", msg, level="WARNING")
        state.save()
        sys.exit(0)

    post_id = post.get("post_id", "")
    post_title = post.get("title", "")
    post_url = post.get("url", "")

    logger.info(f"  → 최신 글: {post_title}")
    logger.info(f"  → post_id: {post_id}")
    logger.info(f"  → URL: {post_url}")

    # ── 핵심: Gist 중복 체크 ─────────────────────────────────────────────
    if not force and state.is_already_processed(post_id):
        msg = f"이미 처리된 포스트. 종료. (post_id: {post_id}, title: {post_title})"
        logger.info(f"⏭️  {msg}")
        state.add_log("SKIP_DUPLICATE", msg, post_id=post_id, post_title=post_title)
        state.save()
        sys.exit(0)

    if force and state.is_already_processed(post_id):
        logger.warning(f"⚠️  force=true 이므로 이미 처리된 포스트도 재실행합니다: {post_title}")
        state.add_log(
            "FORCE_RERUN",
            f"force=true로 재실행: {post_title}",
            post_id=post_id,
            post_title=post_title,
            level="WARNING",
        )

    # 처리 시작 기록 (오류 발생해도 재시도 방지를 위해 즉시 기록)
    # 실제 결과는 이후에 업데이트됨
    state.add_log(
        "PROCESS_START",
        f"포스트 처리 시작: {post_title}",
        post_id=post_id,
        post_title=post_title,
    )

    logger.info(f"  → 모드: {post['mode']} | 새 글 처리 시작")

    # ── 2. 멀티플랫폼 콘텐츠 생성 ────────────────────────────────────────
    logger.info("[2/5] Gemini 콘텐츠 어댑터 실행 중...")
    try:
        adapter = ContentAdapter(api_key=os.environ["GEMINI_API_KEY"])
        content = adapter.generate_all(post)
        content["blog_thumbnail_url"] = post.get("thumbnail_url", "")
        logger.info(f"  → 플랫폼별 텍스트 생성 완료")
        logger.info(f"  → YouTube 슬라이드: {len(content.get('youtube_script', []))}장")
        state.add_log("CONTENT_GENERATED", "Gemini 콘텐츠 생성 완료", post_id=post_id)
    except Exception as e:
        msg = f"Gemini 콘텐츠 생성 실패: {e}"
        logger.error(f"  → {msg}")
        state.add_log("CONTENT_FAILED", msg, post_id=post_id, post_title=post_title, level="ERROR")
        # 콘텐츠 생성 실패 시 처리 완료 기록 (재시도 방지)
        state.mark_as_processed(post_id, post_title, post_url, {"error": {"status": "error", "message": msg}})
        state.save()
        sys.exit(1)

    bg_keywords = _extract_bg_keywords(post, content)
    logger.info(f"  → 배경 이미지 키워드: {bg_keywords}")

    # ── 3. 영상 생성 ──────────────────────────────────────────────────────
    logger.info("[3/5] 영상 생성 중... (배경 이미지 + TTS + BGM)")
    video_path = None
    try:
        video_gen = VideoGenerator(output_dir="videos")
        video_filename = f"shorts_{post['mode']}_{timestamp}.mp4"
        video_path = video_gen.generate_with_text_only_fallback(
            script=content.get("youtube_script", []),
            mode=post["mode"],
            filename=video_filename,
            thumbnail_url=post.get("thumbnail_url", ""),
            blog_url=post.get("url", ""),
            bg_keywords=bg_keywords,
        )
        logger.info(f"  → 영상 저장: {video_path}")
        state.add_log("VIDEO_GENERATED", f"영상 생성 완료: {video_path}", post_id=post_id)
    except Exception as e:
        logger.warning(f"  → 영상 생성 실패 (텍스트 게시는 계속): {e}")
        state.add_log("VIDEO_FAILED", str(e), post_id=post_id, level="WARNING")

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
            content=content,
        )
        logger.info(f"  → 썸네일 {len(thumb_paths)}개 생성 완료")
        state.add_log("THUMBNAILS_GENERATED", f"썸네일 {len(thumb_paths)}개 생성", post_id=post_id)
    except Exception as e:
        logger.warning(f"  → 썸네일 생성 실패: {e}")
        state.add_log("THUMBNAILS_FAILED", str(e), post_id=post_id, level="WARNING")

    media_paths = {**thumb_paths}
    if video_path:
        media_paths["video"] = video_path

    # ── 5. 플랫폼 발행 ───────────────────────────────────────────────────
    logger.info("[5/5] 플랫폼 발행 중...")
    dispatcher = PublisherDispatcher()
    results = dispatcher.publish_all(content=content, media_paths=media_paths)

    # ── 결과 요약 ─────────────────────────────────────────────────────────
    logger.info("\n" + "=" * 60)
    logger.info("발행 결과 요약")
    logger.info("=" * 60)
    ok_count = sum(1 for r in results.values() if r["status"] == "ok")
    skip_count = sum(1 for r in results.values() if r["status"] == "skip")
    error_count = sum(1 for r in results.values() if r["status"] == "error")

    for platform, result in results.items():
        icon = {"ok": "✅", "skip": "⏭️", "error": "❌"}.get(result["status"], "?")
        msg = result.get("message", "")
        url = result.get("url", "")
        logger.info(f"  {icon} {platform.upper()}: {msg} {url}")

    logger.info(f"\n성공: {ok_count} | 건너뜀: {skip_count} | 실패: {error_count}")

    # ── 처리 완료 기록 (Gist 저장) ────────────────────────────────────────
    # 성공/실패 무관하게 처리 완료로 기록 → 재실행 시 중복 방지
    state.mark_as_processed(post_id, post_title, post_url, results)
    state.add_log(
        "PROCESS_DONE",
        f"처리 완료 (성공: {ok_count}, 건너뜀: {skip_count}, 실패: {error_count})",
        post_id=post_id,
        post_title=post_title,
    )
    state.save()

    logger.info("=" * 60)
    logger.info("마케팅 자동화 완료")
    logger.info("=" * 60)

    if error_count > 0 and ok_count == 0 and skip_count == 0:
        # 모든 플랫폼 실패 시에만 exit(1)
        sys.exit(1)


if __name__ == "__main__":
    main()
