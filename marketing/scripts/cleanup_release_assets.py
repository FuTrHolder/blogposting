"""
marketing/scripts/cleanup_release_assets.py

GitHub Release("dashboard-assets") 자산 정리 스크립트.
대시보드가 썸네일/영상을 저장하는 릴리스에 파일이 무한정 쌓이는 것을 막기 위해,
생성된 지 일정 기간(기본 7일)이 지난 자산을 자동으로 삭제합니다.

주의:
  삭제된 파일의 URL은 더 이상 유효하지 않으므로, Cloudflare D1에 저장된
  과거(7일 이전) 포스트/마케팅 결과의 이미지·영상 링크는 이 스크립트 실행 후
  깨진 링크로 남습니다. 대시보드는 최근 결과를 복사해 쓰는 용도라 실무에는
  영향이 없지만, 오래된 기록을 다시 열어볼 계획이 있다면 보관 기간을 늘리세요.

사용법 (GitHub Actions에서 자동 실행, 수동 실행도 가능):
  python marketing/scripts/cleanup_release_assets.py

환경변수:
  GITHUB_TOKEN       : GitHub Actions가 자동 제공 (release 자산 삭제 권한 필요,
                       워크플로우에 permissions: contents: write 선언 필요)
  GITHUB_REPOSITORY  : GitHub Actions가 자동 제공 ("owner/repo" 형식)
  GITHUB_OWNER / GITHUB_REPO : 위 값을 대신 명시적으로 지정하고 싶을 때 (선택)
  RETENTION_DAYS     : 보관 기간(일 단위, 기본 7일)
"""

import logging
import os
import sys
from datetime import datetime, timezone, timedelta

import requests

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)

RELEASE_TAG = "dashboard-assets"
_GITHUB_API = "https://api.github.com"


def _gh_owner_repo() -> tuple[str, str]:
    owner = os.environ.get("GITHUB_OWNER", "")
    repo = os.environ.get("GITHUB_REPO", "")
    if owner and repo:
        return owner, repo
    full = os.environ.get("GITHUB_REPOSITORY", "")
    if "/" in full:
        o, r = full.split("/", 1)
        return o, r
    return "", ""


def _gh_headers() -> dict:
    token = os.environ.get("GITHUB_TOKEN", "") or os.environ.get("GH_PAT", "")
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "blogposting-asset-cleanup",
    }


def _parse_github_timestamp(ts: str) -> datetime:
    # GitHub API 타임스탬프 형식: "2026-07-17T12:05:00Z"
    return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def cleanup(retention_days: int = 7) -> dict:
    owner, repo = _gh_owner_repo()
    if not owner or not repo:
        logger.error("GITHUB_OWNER/GITHUB_REPO(또는 GITHUB_REPOSITORY)를 확인할 수 없습니다.")
        return {"deleted": 0, "kept": 0, "failed": 0, "error": "저장소 정보 없음"}

    cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
    logger.info(
        f"저장소: {owner}/{repo} | 보관 기간: {retention_days}일 | "
        f"기준 시각(UTC): {cutoff.isoformat()}"
    )

    resp = requests.get(
        f"{_GITHUB_API}/repos/{owner}/{repo}/releases/tags/{RELEASE_TAG}",
        headers=_gh_headers(),
        timeout=15,
    )
    if resp.status_code == 404:
        logger.info(f"'{RELEASE_TAG}' 릴리스가 아직 없습니다. 삭제할 자산 없음. 종료.")
        return {"deleted": 0, "kept": 0, "failed": 0}

    resp.raise_for_status()
    release = resp.json()
    assets = release.get("assets", [])
    logger.info(f"전체 자산 수: {len(assets)}개")

    deleted = kept = failed = 0

    for asset in assets:
        name = asset.get("name", "")
        created_at_str = asset.get("created_at", "")

        try:
            created_at = _parse_github_timestamp(created_at_str)
        except Exception as e:
            logger.warning(f"생성 시각 파싱 실패, 안전하게 건너뜀 ({name}): {e}")
            kept += 1
            continue

        if created_at >= cutoff:
            kept += 1
            continue

        age_days = (datetime.now(timezone.utc) - created_at).days
        asset_id = asset.get("id")

        try:
            del_resp = requests.delete(
                f"{_GITHUB_API}/repos/{owner}/{repo}/releases/assets/{asset_id}",
                headers=_gh_headers(),
                timeout=15,
            )
            if del_resp.status_code == 204:
                logger.info(f"삭제 완료 ({age_days}일 경과): {name}")
                deleted += 1
            else:
                logger.warning(
                    f"삭제 실패 ({del_resp.status_code}): {name} — {del_resp.text[:200]}"
                )
                failed += 1
        except Exception as e:
            logger.warning(f"삭제 요청 중 오류: {name} — {e}")
            failed += 1

    logger.info(f"정리 완료 — 삭제: {deleted}건, 유지: {kept}건, 실패: {failed}건")
    return {"deleted": deleted, "kept": kept, "failed": failed}


def main():
    retention_days = int(os.environ.get("RETENTION_DAYS", "7"))
    result = cleanup(retention_days=retention_days)
    if result.get("failed", 0) > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
