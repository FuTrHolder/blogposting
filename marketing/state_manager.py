"""
마케팅 자동화 상태 관리 모듈 (GitHub Gist 기반 영구 저장)

GitHub Gist를 영구 스토리지로 사용합니다.
- 무료, GitHub Token(GITHUB_TOKEN)만 있으면 됨 (별도 Secrets 불필요)
- GitHub Actions에는 GITHUB_TOKEN이 자동으로 주입됨
- 처리 완료 내역을 JSON으로 저장하여 중복 업로드 완전 방지

Gist 파일 구조:
  marketing_state.json : 처리 완료 포스트 내역 (최근 50건)
  marketing_log.json   : 실행 로그 내역 (최근 100건)
"""

import json
import logging
import os
import time
from datetime import datetime, timezone, timedelta

import requests

logger = logging.getLogger(__name__)

KST = timezone(timedelta(hours=9))

# Gist에 저장할 파일명
GIST_STATE_FILE = "marketing_state.json"
GIST_LOG_FILE = "marketing_log.json"

# 최대 보관 건수
MAX_PROCESSED_POSTS = 50
MAX_LOG_ENTRIES = 100


class GistStateManager:
    """
    GitHub Gist를 사용한 영구 상태 관리.

    필요 환경변수:
      GITHUB_TOKEN  : GitHub Actions에서 자동 제공 (secrets.GITHUB_TOKEN)
      GIST_ID       : 최초 실행 후 생성된 Gist ID (GitHub Secrets에 저장)
                      없으면 자동 생성 후 로그에 출력

    Gist가 없으면 자동 생성합니다.
    생성된 Gist ID를 GitHub Secrets > GIST_ID 에 저장하면
    이후 실행부터 동일 Gist를 재사용합니다.
    """

    API_BASE = "https://api.github.com"

    def __init__(self):
        self.token = os.environ.get("GITHUB_TOKEN", "")
        self.gist_id = os.environ.get("GIST_ID", "")
        self._state: dict = {}
        self._log: list = []
        self._loaded = False

        if not self.token:
            logger.warning("GITHUB_TOKEN 없음. 로컬 파일 폴백 사용.")

    @property
    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    # ── Gist 로드 ──────────────────────────────────────────────────────────

    def load(self):
        """Gist에서 상태와 로그를 로드합니다."""
        if self._loaded:
            return

        if not self.token:
            self._load_local_fallback()
            return

        # Gist ID가 없으면 새로 생성
        if not self.gist_id:
            self._create_gist()

        if not self.gist_id:
            logger.error("Gist ID를 가져올 수 없음. 로컬 폴백 사용.")
            self._load_local_fallback()
            return

        try:
            resp = requests.get(
                f"{self.API_BASE}/gists/{self.gist_id}",
                headers=self._headers,
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()

            files = data.get("files", {})

            # 상태 파일 로드
            state_content = files.get(GIST_STATE_FILE, {}).get("content", "{}")
            self._state = json.loads(state_content) if state_content else {}

            # 로그 파일 로드
            log_content = files.get(GIST_LOG_FILE, {}).get("content", "[]")
            self._log = json.loads(log_content) if log_content else []

            logger.info(f"Gist 상태 로드 완료 (처리된 포스트: {len(self._state.get('processed_posts', []))}건)")
            self._loaded = True

        except Exception as e:
            logger.error(f"Gist 로드 실패: {e}. 로컬 폴백 사용.")
            self._load_local_fallback()

    # ── Gist 저장 ──────────────────────────────────────────────────────────

    def save(self):
        """현재 상태와 로그를 Gist에 저장합니다."""
        if not self.token or not self.gist_id:
            self._save_local_fallback()
            return

        try:
            payload = {
                "files": {
                    GIST_STATE_FILE: {
                        "content": json.dumps(self._state, ensure_ascii=False, indent=2)
                    },
                    GIST_LOG_FILE: {
                        "content": json.dumps(self._log, ensure_ascii=False, indent=2)
                    },
                }
            }
            resp = requests.patch(
                f"{self.API_BASE}/gists/{self.gist_id}",
                headers=self._headers,
                json=payload,
                timeout=15,
            )
            resp.raise_for_status()
            logger.info("Gist 상태 저장 완료")
        except Exception as e:
            logger.error(f"Gist 저장 실패: {e}. 로컬 폴백으로 저장.")
            self._save_local_fallback()

    # ── 중복 체크 ──────────────────────────────────────────────────────────

    def is_already_processed(self, post_id: str) -> bool:
        """이미 처리된 포스트인지 확인합니다."""
        self.load()
        processed = self._state.get("processed_posts", [])
        return post_id in [p["post_id"] for p in processed]

    def mark_as_processed(
        self,
        post_id: str,
        post_title: str,
        post_url: str,
        results: dict,
    ):
        """포스트를 처리 완료로 기록합니다."""
        self.load()
        now_kst = datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S KST")

        processed = self._state.get("processed_posts", [])

        # 이미 있으면 업데이트, 없으면 추가
        existing_idx = next(
            (i for i, p in enumerate(processed) if p["post_id"] == post_id), None
        )

        entry = {
            "post_id": post_id,
            "post_title": post_title,
            "post_url": post_url,
            "processed_at": now_kst,
            "results": {
                platform: {
                    "status": r.get("status"),
                    "url": r.get("url", ""),
                    "message": r.get("message", ""),
                }
                for platform, r in results.items()
            },
        }

        if existing_idx is not None:
            processed[existing_idx] = entry
            logger.info(f"기존 처리 내역 업데이트: {post_title}")
        else:
            processed.insert(0, entry)
            logger.info(f"처리 완료 기록: {post_title}")

        # 최대 보관 건수 제한
        self._state["processed_posts"] = processed[:MAX_PROCESSED_POSTS]
        self._state["last_updated"] = now_kst

    # ── 실행 로그 ──────────────────────────────────────────────────────────

    def add_log(
        self,
        event: str,
        message: str,
        post_id: str = "",
        post_title: str = "",
        level: str = "INFO",
    ):
        """실행 로그를 추가합니다."""
        self.load()
        now_kst = datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S KST")

        entry = {
            "timestamp": now_kst,
            "level": level,
            "event": event,
            "message": message,
        }
        if post_id:
            entry["post_id"] = post_id
        if post_title:
            entry["post_title"] = post_title

        self._log.insert(0, entry)
        self._log = self._log[:MAX_LOG_ENTRIES]

    def get_processed_posts(self) -> list:
        """처리 완료된 포스트 목록을 반환합니다."""
        self.load()
        return self._state.get("processed_posts", [])

    def get_recent_logs(self, limit: int = 20) -> list:
        """최근 실행 로그를 반환합니다."""
        self.load()
        return self._log[:limit]

    # ── Gist 생성 ──────────────────────────────────────────────────────────

    def _create_gist(self):
        """새 Gist를 생성합니다. 생성된 ID를 환경에 설정합니다."""
        try:
            initial_state = {
                "processed_posts": [],
                "last_updated": datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S KST"),
                "created_at": datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S KST"),
            }
            payload = {
                "description": "미국 증시 블로그 마케팅 자동화 상태 관리",
                "public": False,  # 비공개 Gist (보안)
                "files": {
                    GIST_STATE_FILE: {
                        "content": json.dumps(initial_state, ensure_ascii=False, indent=2)
                    },
                    GIST_LOG_FILE: {
                        "content": json.dumps([], ensure_ascii=False, indent=2)
                    },
                },
            }
            resp = requests.post(
                f"{self.API_BASE}/gists",
                headers=self._headers,
                json=payload,
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()
            self.gist_id = data["id"]

            logger.info("=" * 60)
            logger.info(f"✅ 새 Gist 생성 완료!")
            logger.info(f"   GIST_ID = {self.gist_id}")
            logger.info(f"   이 값을 GitHub Secrets > GIST_ID 에 저장하세요.")
            logger.info(f"   저장하지 않으면 매 실행마다 새 Gist가 생성됩니다.")
            logger.info("=" * 60)

            self._state = initial_state
            self._log = []
            self._loaded = True

        except Exception as e:
            logger.error(f"Gist 생성 실패: {e}")

    # ── 로컬 폴백 (Gist 사용 불가 시) ─────────────────────────────────────

    def _load_local_fallback(self):
        """로컬 JSON 파일에서 상태를 로드합니다."""
        state_path = "last_post_state.json"
        log_path = "last_post_log.json"

        if os.path.exists(state_path):
            try:
                with open(state_path, "r", encoding="utf-8") as f:
                    raw = json.load(f)
                # 구 형식 호환 (last_post_ids 배열)
                if "last_post_ids" in raw and "processed_posts" not in raw:
                    self._state = {
                        "processed_posts": [
                            {"post_id": pid, "post_title": "", "post_url": "", "processed_at": ""}
                            for pid in raw["last_post_ids"]
                        ]
                    }
                else:
                    self._state = raw
            except Exception as e:
                logger.warning(f"로컬 상태 파일 로드 실패: {e}")
                self._state = {"processed_posts": []}
        else:
            self._state = {"processed_posts": []}

        if os.path.exists(log_path):
            try:
                with open(log_path, "r", encoding="utf-8") as f:
                    self._log = json.load(f)
            except Exception:
                self._log = []
        else:
            self._log = []

        self._loaded = True

    def _save_local_fallback(self):
        """로컬 JSON 파일에 상태를 저장합니다."""
        try:
            with open("last_post_state.json", "w", encoding="utf-8") as f:
                json.dump(self._state, f, ensure_ascii=False, indent=2)
            with open("last_post_log.json", "w", encoding="utf-8") as f:
                json.dump(self._log, f, ensure_ascii=False, indent=2)
            logger.info("로컬 상태 파일 저장 완료")
        except Exception as e:
            logger.error(f"로컬 상태 파일 저장 실패: {e}")
