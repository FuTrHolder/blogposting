"""
dashboard_client.py
Cloudflare Pages 대시보드(Workers Functions) + GitHub Releases 연동 모듈.

저장 방식 (R2 미사용):
- 이미지/영상은 이 저장소의 GitHub Release(태그: dashboard-assets)에 자산으로
  업로드되고, 거기서 발급되는 공개 다운로드 URL만 Cloudflare D1에 저장합니다.
- Cloudflare Worker는 바이너리를 다루지 않고 메타데이터(JSON)만 받습니다.
- Cloudflare 결제 카드 등록 없이 완전 무료로 운영 가능합니다 (R2 불필요).
- GitHub 쪽 인증은 별도 PAT 발급 없이, GitHub Actions가 매 실행마다 자동으로
  제공하는 GITHUB_TOKEN을 사용합니다 (워크플로우에 permissions: contents: write
  만 선언하면 됩니다).

- main.py                      → push_content()          (본문 + 썸네일)
- marketing/main_marketing.py  → push_marketing_result()  (채널별 발행 결과)

이 모듈의 실패는 절대 전체 파이프라인을 중단시키지 않습니다 (예외를 삼키고 경고 로그만 남김).
DASHBOARD_API_URL이 설정되지 않은 경우 조용히 스킵합니다 (대시보드 미도입 환경 호환).
"""

import logging
import mimetypes
import os

import requests

logger = logging.getLogger(__name__)

RELEASE_TAG = "dashboard-assets"
_GITHUB_API = "https://api.github.com"


# ── Cloudflare 대시보드 인증/주소 ────────────────────────────────────────────

def _dashboard_headers() -> dict:
    headers = {}
    client_id = os.environ.get("CF_ACCESS_CLIENT_ID", "")
    client_secret = os.environ.get("CF_ACCESS_CLIENT_SECRET", "")
    if client_id and client_secret:
        headers["CF-Access-Client-Id"] = client_id
        headers["CF-Access-Client-Secret"] = client_secret
    return headers


def _dashboard_base_url() -> str:
    return os.environ.get("DASHBOARD_API_URL", "").rstrip("/")


# ── GitHub 인증/저장소 정보 ──────────────────────────────────────────────────

def _gh_token() -> str:
    # GH_RELEASE_TOKEN: 워크플로우에서 GitHub Actions 자동 제공 토큰을 이 이름으로 매핑해
    # 전달합니다 (marketing_automation.yml에서는 GITHUB_TOKEN이 이미 Gist용 GH_PAT로
    # 쓰이고 있어 이름 충돌을 피하기 위함). 없으면 GITHUB_TOKEN → GH_PAT 순으로 대체 시도.
    return (
        os.environ.get("GH_RELEASE_TOKEN", "")
        or os.environ.get("GITHUB_TOKEN", "")
        or os.environ.get("GH_PAT", "")
    )


def _gh_owner_repo() -> tuple[str, str]:
    owner = os.environ.get("GITHUB_OWNER", "")
    repo = os.environ.get("GITHUB_REPO", "")
    if owner and repo:
        return owner, repo
    # GITHUB_REPOSITORY는 Actions가 자동 주입하는 "owner/repo" 형식 값
    full = os.environ.get("GITHUB_REPOSITORY", "")
    if "/" in full:
        o, r = full.split("/", 1)
        return o, r
    return "", ""


def _gh_headers() -> dict:
    return {
        "Authorization": f"Bearer {_gh_token()}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "blogposting-dashboard-client",
    }


def _get_or_create_release() -> dict | None:
    owner, repo = _gh_owner_repo()
    if not (owner and repo and _gh_token()):
        logger.info("GitHub 저장소 정보/토큰 확인 불가 — Release 업로드 건너뜀")
        return None

    url = f"{_GITHUB_API}/repos/{owner}/{repo}/releases/tags/{RELEASE_TAG}"
    try:
        resp = requests.get(url, headers=_gh_headers(), timeout=15)
        if resp.status_code == 200:
            return resp.json()

        if resp.status_code == 404:
            create_resp = requests.post(
                f"{_GITHUB_API}/repos/{owner}/{repo}/releases",
                headers=_gh_headers(),
                json={
                    "tag_name": RELEASE_TAG,
                    "name": "대시보드 자산 저장소 (자동 생성 — 삭제하지 마세요)",
                    "body": "대시보드가 사용하는 썸네일/영상 파일이 이 릴리스에 자동 업로드됩니다.",
                    "draft": False,
                    "prerelease": False,
                },
                timeout=20,
            )
            create_resp.raise_for_status()
            return create_resp.json()

        logger.warning(f"GitHub Release 조회 실패 ({resp.status_code}): {resp.text[:200]}")
        return None
    except Exception as e:
        logger.warning(f"GitHub Release 조회/생성 실패: {e}")
        return None


def _upload_asset(file_path: str | None, asset_name: str) -> str:
    """파일을 GitHub Release 자산으로 업로드하고 공개 다운로드 URL을 반환합니다.
    실패 시 빈 문자열을 반환합니다 (파이프라인은 계속 진행)."""
    if not file_path or not os.path.exists(file_path):
        return ""

    release = _get_or_create_release()
    if not release:
        return ""

    owner, repo = _gh_owner_repo()

    # 동일 이름의 기존 자산이 있으면 먼저 삭제 (재실행 시 덮어쓰기 위함)
    for asset in release.get("assets", []):
        if asset.get("name") == asset_name:
            try:
                requests.delete(
                    f"{_GITHUB_API}/repos/{owner}/{repo}/releases/assets/{asset['id']}",
                    headers=_gh_headers(),
                    timeout=15,
                )
            except Exception as e:
                logger.warning(f"기존 자산 삭제 실패 (무시하고 진행, {asset_name}): {e}")
            break

    upload_url = release["upload_url"].split("{")[0]  # "{?name,label}" 템플릿 제거
    content_type = mimetypes.guess_type(asset_name)[0] or "application/octet-stream"

    try:
        with open(file_path, "rb") as f:
            data = f.read()

        resp = requests.post(
            upload_url,
            params={"name": asset_name},
            headers={**_gh_headers(), "Content-Type": content_type},
            data=data,
            timeout=120,
        )
        resp.raise_for_status()
        download_url = resp.json().get("browser_download_url", "")
        logger.info(f"GitHub Release 업로드 완료: {asset_name}")
        return download_url
    except Exception as e:
        logger.warning(f"GitHub Release 업로드 실패 ({asset_name}): {e}")
        return ""


# ── 대시보드로 메타데이터 전송 ────────────────────────────────────────────────

def push_content(
    post_date: str,
    mode: str,
    title: str,
    content: str,
    tags: list[str],
    image_path: str | None = None,
) -> bool:
    """블로그 본문 + 썸네일을 대시보드(D1)에 업로드합니다. 썸네일은 GitHub Release로."""
    base = _dashboard_base_url()
    if not base:
        logger.info("DASHBOARD_API_URL 미설정 — 대시보드 업로드 건너뜀")
        return False

    thumbnail_url = ""
    if image_path:
        ext = os.path.splitext(image_path)[1] or ".jpg"
        thumbnail_url = _upload_asset(image_path, f"thumb_{post_date}_{mode}{ext}")

    payload = {
        "post_date": post_date,
        "mode": mode,
        "title": title,
        "content": content,
        "tags": tags,
        "thumbnail_url": thumbnail_url,
    }

    try:
        resp = requests.post(
            f"{base}/api/ingest/content",
            json=payload,
            headers=_dashboard_headers(),
            timeout=30,
        )
        resp.raise_for_status()
        logger.info(f"대시보드 콘텐츠 업로드 완료: {post_date} {mode}")
        return True
    except Exception as e:
        logger.warning(f"대시보드 콘텐츠 업로드 실패 (파이프라인은 계속 진행): {e}")
        return False


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
    """플랫폼별 마케팅 발행 결과를 대시보드(D1)에 업로드합니다. 미디어는 GitHub Release로."""
    base = _dashboard_base_url()
    if not base:
        logger.info("DASHBOARD_API_URL 미설정 — 대시보드 업로드 건너뜀")
        return False

    thumbnail_url = ""
    if thumbnail_path:
        ext = os.path.splitext(thumbnail_path)[1] or ".jpg"
        thumbnail_url = _upload_asset(thumbnail_path, f"thumb_{post_date}_{mode}_{platform}{ext}")

    video_url = ""
    if video_path:
        video_url = _upload_asset(video_path, f"video_{post_date}_{mode}_{platform}.mp4")

    payload = {
        "post_date": post_date,
        "mode": mode,
        "platform": platform,
        "status": status,
        "message": message,
        "url": url,
        "content_text": content_text,
        "thumbnail_url": thumbnail_url,
        "video_url": video_url,
    }

    try:
        resp = requests.post(
            f"{base}/api/ingest/marketing-result",
            json=payload,
            headers=_dashboard_headers(),
            timeout=30,
        )
        resp.raise_for_status()
        logger.info(f"대시보드 마케팅 결과 업로드 완료: {platform}")
        return True
    except Exception as e:
        logger.warning(f"대시보드 마케팅 결과 업로드 실패 (계속 진행, {platform}): {e}")
        return False
