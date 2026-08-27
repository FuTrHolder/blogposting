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
import re

import requests

logger = logging.getLogger(__name__)

RELEASE_TAG = "dashboard-assets"
_GITHUB_API = "https://api.github.com"


# ── Cloudflare 대시보드 인증/주소 ────────────────────────────────────────────

def _dashboard_headers() -> dict:
    secret = os.environ.get("INGEST_SECRET", "")
    if secret:
        return {"X-Ingest-Secret": secret}
    return {}


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
        resp = requests.get(
            url,
            headers=_gh_headers(),
            timeout=15,
        )

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

        logger.warning(
            f"GitHub Release 조회 실패 ({resp.status_code}): "
            f"{resp.text[:200]}"
        )
        return None

    except Exception as e:
        logger.warning(f"GitHub Release 조회/생성 실패: {e}")
        return None


def _upload_asset(file_path: str | None, asset_name: str) -> str:
    """
    파일을 GitHub Release 자산으로 업로드하고 공개 다운로드 URL을 반환합니다.

    실패 시 빈 문자열을 반환합니다.
    파이프라인은 계속 진행됩니다.
    """
    if not file_path or not os.path.exists(file_path):
        return ""

    release = _get_or_create_release()

    if not release:
        return ""

    owner, repo = _gh_owner_repo()

    # 동일 이름의 기존 자산이 있으면 먼저 삭제
    # 재실행 시 덮어쓰기를 위해 사용
    for asset in release.get("assets", []):
        if asset.get("name") == asset_name:
            try:
                requests.delete(
                    f"{_GITHUB_API}/repos/{owner}/{repo}/releases/assets/{asset['id']}",
                    headers=_gh_headers(),
                    timeout=15,
                )
            except Exception as e:
                logger.warning(
                    f"기존 자산 삭제 실패 "
                    f"(무시하고 진행, {asset_name}): {e}"
                )
            break

    upload_url = release["upload_url"].split("{")[0]
    content_type = (
        mimetypes.guess_type(asset_name)[0]
        or "application/octet-stream"
    )

    try:
        with open(file_path, "rb") as f:
            data = f.read()

        resp = requests.post(
            upload_url,
            params={"name": asset_name},
            headers={
                **_gh_headers(),
                "Content-Type": content_type,
            },
            data=data,
            timeout=120,
        )

        resp.raise_for_status()

        download_url = resp.json().get(
            "browser_download_url",
            "",
        )

        logger.info(
            f"GitHub Release 업로드 완료: {asset_name}"
        )

        return download_url

    except Exception as e:
        logger.warning(
            f"GitHub Release 업로드 실패 ({asset_name}): {e}"
        )
        return ""


# ── 로컬 파일 → 공개 URL 변환 ────────────────────────────────────────────────

def upload_media_get_public_url(
    file_path: str | None,
    asset_name: str,
) -> str:
    """
    로컬 파일(SNS용 썸네일, 숏폼 영상 등)을 GitHub Release 자산으로
    업로드하고 누구나(Meta 서버 포함) 접근 가능한 공개 다운로드 URL을
    반환합니다.

    Threads API(media_type=IMAGE/VIDEO), Instagram Graph API(REELS),
    Facebook Reels Publishing API는 전부 "공개적으로 접근 가능한 URL"만 받고
    로컬 파일 직접 업로드는 지원하지 않습니다.

    이 함수는 _upload_asset()을 발행 파이프라인에서
    "발행 전"에 미리 호출할 수 있도록 외부에 노출하는 공개 래퍼입니다.

    실패 시 빈 문자열을 반환합니다.
    """
    return _upload_asset(file_path, asset_name)


# ── HTML 면책조항 처리 ──────────────────────────────────────────────────────

def _normalize_disclaimer_html(html: str) -> str:
    """
    블로그 원고 HTML의 면책조항을 티스토리용 style3 blockquote로 통일합니다.

    처리 규칙:

    1. 기존 면책조항이
       <blockquote>...</blockquote>
       형태라면
       <blockquote data-ke-style="style3">...</blockquote>
       로 통일합니다.

    2. 기존 blockquote 안에 다른 data-ke-style이 있더라도
       면책조항에 해당하는 blockquote는 style3으로 통일합니다.

    3. 면책조항이 일반 <p> 또는 텍스트 형태로 존재한다면
       blockquote data-ke-style="style3"으로 감쌉니다.

    4. 면책조항이 전혀 없다면 기본 면책조항을 원고 마지막에 추가합니다.

    5. 일반적인 본문 blockquote까지 무조건 style3으로 변경하지 않습니다.
       면책조항으로 판단되는 blockquote만 변경합니다.
    """

    if html is None:
        html = ""

    html = str(html)

    # BOM 제거 및 앞뒤 공백 정리
    html = html.replace("\ufeff", "").strip()

    # ---------------------------------------------------------
    # 기본 면책조항
    # ---------------------------------------------------------
    default_disclaimer = (
        '<blockquote data-ke-style="style3">'
        '<p>'
        '본 콘텐츠는 제공된 정보를 바탕으로 작성되었으며, '
        '투자 권유를 목적으로 하지 않습니다. '
        '투자 결정에 따른 책임은 본인에게 있습니다.'
        '</p>'
        '</blockquote>'
    )

    # ---------------------------------------------------------
    # 면책조항으로 판단할 수 있는 주요 표현
    # ---------------------------------------------------------
    disclaimer_patterns = [
        r"면책\s*조항",
        r"투자\s*권유",
        r"투자\s*판단",
        r"투자\s*결정",
        r"투자\s*책임",
        r"투자\s*손실",
        r"본\s*콘텐츠는",
        r"본\s*글은",
        r"투자\s*조언",
        r"투자\s*자문",
    ]

    def contains_disclaimer(text: str) -> bool:
        if not text:
            return False

        return any(
            re.search(
                pattern,
                text,
                flags=re.IGNORECASE | re.DOTALL,
            )
            for pattern in disclaimer_patterns
        )

    # ---------------------------------------------------------
    # 1. 기존 blockquote를 검사
    #
    # 일반적인 본문 인용문은 그대로 유지하고,
    # 면책조항에 해당하는 blockquote만 style3으로 변경합니다.
    # ---------------------------------------------------------
    blockquote_pattern = re.compile(
        r"<blockquote\b([^>]*)>(.*?)</blockquote>",
        flags=re.IGNORECASE | re.DOTALL,
    )

    disclaimer_found = False

    def normalize_blockquote(match: re.Match) -> str:
        nonlocal disclaimer_found

        attrs = match.group(1) or ""
        inner = match.group(2) or ""

        # 이미 style3이면 그대로 사용하되
        # 면책조항 여부는 다시 기록합니다.
        is_style3 = re.search(
            r'data-ke-style\s*=\s*["\']style3["\']',
            attrs,
            flags=re.IGNORECASE,
        )

        is_disclaimer = contains_disclaimer(
            re.sub(r"<[^>]+>", " ", inner)
        )

        # 면책조항이거나 이미 style3인 경우
        if is_disclaimer or is_style3:
            disclaimer_found = True

            return (
                '<blockquote data-ke-style="style3">'
                + inner.strip()
                + "</blockquote>"
            )

        # 일반 blockquote는 건드리지 않음
        return match.group(0)

    html = blockquote_pattern.sub(
        normalize_blockquote,
        html,
    )

    # ---------------------------------------------------------
    # 2. blockquote가 아닌 형태의 면책조항 처리
    #
    # 예:
    # <p>면책조항</p>
    # <p>본 콘텐츠는 투자 권유를 목적으로 하지 않습니다.</p>
    # ---------------------------------------------------------
    if not disclaimer_found:

        # HTML paragraph 단위로 검색
        paragraph_pattern = re.compile(
            r"<p\b[^>]*>(.*?)</p>",
            flags=re.IGNORECASE | re.DOTALL,
        )

        def normalize_paragraph(match: re.Match) -> str:
            nonlocal disclaimer_found

            inner = match.group(1) or ""

            text_only = re.sub(
                r"<[^>]+>",
                " ",
                inner,
            )

            if contains_disclaimer(text_only):
                disclaimer_found = True

                return (
                    '<blockquote data-ke-style="style3">'
                    "<p>"
                    + inner.strip()
                    + "</p>"
                    "</blockquote>"
                )

            return match.group(0)

        html = paragraph_pattern.sub(
            normalize_paragraph,
            html,
        )

    # ---------------------------------------------------------
    # 3. 그래도 발견되지 않았다면 일반 텍스트 형태 검사
    #
    # Gemini가 HTML 내부에 면책조항을 일반 텍스트로 작성한 경우를
    # 최대한 처리합니다.
    # ---------------------------------------------------------
    if not disclaimer_found:
        text_only_html = re.sub(
            r"<[^>]+>",
            " ",
            html,
        )

        if contains_disclaimer(text_only_html):

            # 이미 처리 가능한 paragraph가 아닌 경우
            # 해당 문구 전체를 별도의 면책조항으로 추가하지 않고
            # 기본 면책조항을 추가합니다.
            #
            # 기존 본문을 임의로 잘라내거나 변형하지 않기 위한
            # 안전한 fallback입니다.
            html = (
                html.rstrip()
                + "\n\n"
                + default_disclaimer
            )

            disclaimer_found = True

    # ---------------------------------------------------------
    # 4. 면책조항 자체가 전혀 없는 경우
    # ---------------------------------------------------------
    if not disclaimer_found:
        if html:
            html = (
                html.rstrip()
                + "\n\n"
                + default_disclaimer
            )
        else:
            html = default_disclaimer

    # ---------------------------------------------------------
    # 5. 최종 안전장치
    #
    # 반드시 style3 면책조항이 존재하는지 확인합니다.
    # ---------------------------------------------------------
    if not re.search(
        r'<blockquote\s+data-ke-style\s*=\s*["\']style3["\']',
        html,
        flags=re.IGNORECASE,
    ):
        html = (
            html.rstrip()
            + "\n\n"
            + default_disclaimer
        )

    return html.strip()


# ── 대시보드로 메타데이터 전송 ──────────────────────────────────────────────

def push_content(
    post_date: str,
    mode: str,
    title: str,
    content: str,
    tags: list[str],
    image_path: str | None = None,
) -> bool:
    """
    블로그 본문 + 썸네일을 대시보드(D1)에 업로드합니다.

    원고 HTML은 대시보드로 전송하기 전에
    _normalize_disclaimer_html()을 통과시킵니다.

    따라서 morning/evening 포스팅 모두 최종적으로
    다음 형태의 티스토리용 면책조항을 갖게 됩니다.

    <blockquote data-ke-style="style3">
    <p>
    본 콘텐츠는 제공된 정보를 바탕으로 작성되었으며,
    투자 권유를 목적으로 하지 않습니다.
    투자 결정에 따른 책임은 본인에게 있습니다.
    </p>
    </blockquote>
    """

    base = _dashboard_base_url()

    if not base:
        logger.info(
            "DASHBOARD_API_URL 미설정 — 대시보드 업로드 건너뜀"
        )
        return False

    # ---------------------------------------------------------
    # 원고 HTML 면책조항 정규화
    #
    # 실제 API 전송 직전에 처리하여
    # morning/evening 모두 동일한 형식을 사용하도록 합니다.
    # ---------------------------------------------------------
    normalized_content = _normalize_disclaimer_html(content)

    thumbnail_url = ""

    if image_path:
        ext = os.path.splitext(image_path)[1] or ".jpg"

        thumbnail_url = _upload_asset(
            image_path,
            f"thumb_{post_date}_{mode}{ext}",
        )

    payload = {
        "post_date": post_date,
        "mode": mode,
        "title": title,
        "content": normalized_content,
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

        logger.info(
            f"대시보드 콘텐츠 업로드 완료: "
            f"{post_date} {mode}"
        )

        return True

    except Exception as e:
        logger.warning(
            "대시보드 콘텐츠 업로드 실패 "
            f"(파이프라인은 계속 진행): {e}"
        )
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
    blog_url: str = "",
) -> bool:
    """
    플랫폼별 마케팅 발행 결과를 대시보드(D1)에 업로드합니다.
    미디어는 GitHub Release로 저장합니다.

    blog_url이 전달되면 posts.blog_url도 함께 갱신합니다.

    main.py는 실제 티스토리 발행 URL을 모르지만,
    마케팅 파이프라인의 tistory_crawler는 RSS로 이 URL을 알고 있으므로
    여기서 대시보드에 채워 넣습니다.
    """

    base = _dashboard_base_url()

    if not base:
        logger.info(
            "DASHBOARD_API_URL 미설정 — 대시보드 업로드 건너뜀"
        )
        return False

    thumbnail_url = ""

    if thumbnail_path:
        ext = os.path.splitext(thumbnail_path)[1] or ".jpg"

        thumbnail_url = _upload_asset(
            thumbnail_path,
            f"thumb_{post_date}_{mode}_{platform}{ext}",
        )

    video_url = ""

    if video_path:
        video_url = _upload_asset(
            video_path,
            f"video_{post_date}_{mode}_{platform}.mp4",
        )

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
        "blog_url": blog_url,
    }

    try:
        resp = requests.post(
            f"{base}/api/ingest/marketing-result",
            json=payload,
            headers=_dashboard_headers(),
            timeout=30,
        )

        resp.raise_for_status()

        logger.info(
            f"대시보드 마케팅 결과 업로드 완료: "
            f"{platform}"
        )

        return True

    except Exception as e:
        logger.warning(
            "대시보드 마케팅 결과 업로드 실패 "
            f"(계속 진행, {platform}): {e}"
        )
        return False
