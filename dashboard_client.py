"""
dashboard_client.py
Cloudflare Pages 대시보드(Workers Functions) API 연동 모듈.

- main.py            → push_content()          (본문 + 썸네일 업로드)
- marketing/main_marketing.py → push_marketing_result()  (채널별 발행 결과 업로드)

설계 원칙:
- 대시보드가 아직 안정화되기 전이므로, 이 모듈의 실패는 절대 전체 파이프라인을
  중단시키지 않습니다 (예외를 삼키고 경고 로그만 남김).
- DASHBOARD_API_URL이 설정되지 않은 경우 조용히 스킵합니다 (대시보드 미도입 환경 호환).
- 인증은 Cloudflare Access Service Token 헤더(CF-Access-Client-Id/Secret)를 사용합니다.
"""

import json
import logging
import os

import requests

logger = logging.getLogger(__name__)


def _auth_headers() -> dict:
    headers = {}
    client_id = os.environ.get("CF_ACCESS_CLIENT_ID", "")
    client_secret = os.environ.get("CF_ACCESS_CLIENT_SECRET", "")
    if client_id and client_secret:
        headers["CF-Access-Client-Id"] = client_id
        headers["CF-Access-Client-Secret"] = client_secret
    return headers


def _base_url() -> str:
    return os.environ.get("DASHBOARD_API_URL", "").rstrip("/")


def push_content(
    post_date: str,
    mode: str,
    title: str,
    content: str,
    tags: list[str],
    image_path: str | None = None,
) -> bool:
    """블로그 본문 + 썸네일을 대시보드(D1 + R2)에 업로드합니다."""
    base = _base_url()
    if not base:
        logger.info("DASHBOARD_API_URL 미설정 — 대시보드 업로드 건너뜀")
        return False

    data = {
        "post_date": post_date,
        "mode": mode,
        "title": title,
        "content": content,
        "tags": json.dumps(tags, ensure_ascii=False),
    }

    opened = None
    try:
        files = None
        if image_path and os.path.exists(image_path):
            opened = open(image_path, "rb")
            ext = "png" if image_path.lower().endswith("png") else "jpeg"
            files = {"image": (os.path.basename(image_path), opened, f"image/{ext}")}

        resp = requests.post(
            f"{base}/api/ingest/content",
            data=data,
            files=files,
            headers=_auth_headers(),
            timeout=30,
        )
        resp.raise_for_status()
        logger.info(f"대시보드 콘텐츠 업로드 완료: {post_date} {mode}")
        return True
    except Exception as e:
        logger.warning(f"대시보드 콘텐츠 업로드 실패 (파이프라인은 계속 진행): {e}")
        return False
    finally:
        if opened:
            opened.close()


def push_marketing_result(
    post_date: str,
    mode: str,
    platform: str,
    status: str,
    message: str,
    url: str = "",
    content_text: str = "",
    thumbnail_path: str | None = None,
    video_path: str | None = None,
) -> bool:
    """플랫폼별 마케팅 발행 결과를 대시보드(D1 + R2)에 업로드합니다."""
    base = _base_url()
    if not base:
        logger.info("DASHBOARD_API_URL 미설정 — 대시보드 업로드 건너뜀")
        return False

    data = {
        "post_date": post_date,
        "mode": mode,
        "platform": platform,
        "status": status,
        "message": message,
        "url": url,
        "content_text": content_text,
    }

    opened_files = []
    try:
        files = {}
        if thumbnail_path and os.path.exists(thumbnail_path):
            f = open(thumbnail_path, "rb")
            opened_files.append(f)
            ext = "png" if thumbnail_path.lower().endswith("png") else "jpeg"
            files["thumbnail"] = (os.path.basename(thumbnail_path), f, f"image/{ext}")

        if video_path and os.path.exists(video_path):
            f = open(video_path, "rb")
            opened_files.append(f)
            files["video"] = (os.path.basename(video_path), f, "video/mp4")

        resp = requests.post(
            f"{base}/api/ingest/marketing-result",
            data=data,
            files=files or None,
            headers=_auth_headers(),
            timeout=60,
        )
        resp.raise_for_status()
        logger.info(f"대시보드 마케팅 결과 업로드 완료: {platform}")
        return True
    except Exception as e:
        logger.warning(f"대시보드 마케팅 결과 업로드 실패 (계속 진행, {platform}): {e}")
        return False
    finally:
        for f in opened_files:
            f.close()
