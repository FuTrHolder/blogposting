"""
마케팅 자동화 처리 내역 조회 스크립트
Gist에 저장된 처리 완료 내역과 실행 로그를 출력합니다.

사용법:
  python scripts/check_state.py
  python scripts/check_state.py --logs      # 최근 로그 20건 출력
  python scripts/check_state.py --reset     # 상태 초기화 (주의)

환경변수:
  GITHUB_TOKEN, GIST_ID
"""

import os
import sys
import argparse
import json
from datetime import datetime, timezone, timedelta

# 상위 디렉토리의 state_manager 임포트
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from state_manager import GistStateManager

KST = timezone(timedelta(hours=9))


def print_processed_posts(state: GistStateManager):
    """처리 완료 포스트 내역 출력."""
    posts = state.get_processed_posts()
    print(f"\n{'='*70}")
    print(f"처리 완료 포스트 내역 (총 {len(posts)}건)")
    print(f"{'='*70}")

    if not posts:
        print("  (처리된 포스트 없음)")
        return

    for i, post in enumerate(posts, 1):
        print(f"\n[{i}] {post.get('post_title', '제목 없음')}")
        print(f"     URL: {post.get('post_url', '')}")
        print(f"     ID:  {post.get('post_id', '')}")
        print(f"     처리: {post.get('processed_at', '')}")

        results = post.get("results", {})
        ok = [p for p, r in results.items() if r.get("status") == "ok"]
        skip = [p for p, r in results.items() if r.get("status") == "skip"]
        error = [p for p, r in results.items() if r.get("status") == "error"]

        if ok:
            print(f"     ✅ 성공: {', '.join(ok)}")
        if skip:
            print(f"     ⏭️  건너뜀: {', '.join(skip)}")
        if error:
            print(f"     ❌ 실패: {', '.join(error)}")


def print_logs(state: GistStateManager, limit: int = 20):
    """실행 로그 출력."""
    logs = state.get_recent_logs(limit=limit)
    print(f"\n{'='*70}")
    print(f"최근 실행 로그 (최대 {limit}건)")
    print(f"{'='*70}")

    if not logs:
        print("  (로그 없음)")
        return

    for log in logs:
        level = log.get("level", "INFO")
        icon = {"INFO": "ℹ️ ", "WARNING": "⚠️ ", "ERROR": "❌"}.get(level, "  ")
        ts = log.get("timestamp", "")
        event = log.get("event", "")
        msg = log.get("message", "")
        title = log.get("post_title", "")
        title_str = f" [{title}]" if title else ""
        print(f"  {icon} {ts} | {event}{title_str}")
        print(f"       {msg}")


def reset_state(state: GistStateManager):
    """상태 초기화 (확인 후 진행)."""
    confirm = input("\n⚠️  모든 처리 내역과 로그를 초기화합니다. 계속하시겠습니까? (yes/no): ")
    if confirm.strip().lower() != "yes":
        print("취소되었습니다.")
        return

    state._state = {
        "processed_posts": [],
        "last_updated": datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S KST"),
    }
    state._log = []
    state._loaded = True
    state.add_log("RESET", "상태 초기화 완료")
    state.save()
    print("✅ 상태가 초기화되었습니다.")


def main():
    parser = argparse.ArgumentParser(description="마케팅 자동화 처리 내역 조회")
    parser.add_argument("--logs", action="store_true", help="실행 로그 출력")
    parser.add_argument("--reset", action="store_true", help="상태 초기화 (주의)")
    parser.add_argument("--limit", type=int, default=20, help="로그 출력 건수 (기본 20)")
    args = parser.parse_args()

    print(f"조회 시각: {datetime.now(KST).strftime('%Y-%m-%d %H:%M:%S KST')}")
    print(f"GIST_ID: {os.environ.get('GIST_ID', '(미설정 - 로컬 파일 사용)')}")

    state = GistStateManager()
    state.load()

    if args.reset:
        reset_state(state)
        return

    print_processed_posts(state)

    if args.logs:
        print_logs(state, limit=args.limit)

    print(f"\n{'='*70}")
    print("조회 완료")
    print(f"{'='*70}\n")


if __name__ == "__main__":
    main()
