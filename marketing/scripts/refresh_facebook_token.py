"""
Facebook 장기 액세스 토큰 갱신 스크립트
Facebook 페이지 토큰은 기본 1~2시간이지만 장기 토큰으로 교환하면 60일 유효합니다.
GitHub Actions에서 주기적으로 실행해 Secrets를 자동 갱신합니다.

사용법:
  python scripts/refresh_facebook_token.py

또는 GitHub Actions에서 월 1회 자동 실행 (아래 workflow 추가 권장):
  on:
    schedule:
      - cron: "0 0 1 * *"  # 매월 1일
"""

import os
import requests
import subprocess
import sys

APP_ID = os.environ.get("FACEBOOK_APP_ID", "")
APP_SECRET = os.environ.get("FACEBOOK_APP_SECRET", "")
SHORT_TOKEN = os.environ.get("FACEBOOK_SHORT_TOKEN", "")

if not all([APP_ID, APP_SECRET, SHORT_TOKEN]):
    print("환경변수 미설정: FACEBOOK_APP_ID, FACEBOOK_APP_SECRET, FACEBOOK_SHORT_TOKEN")
    sys.exit(1)

# 단기 토큰 → 장기 토큰 교환
resp = requests.get(
    "https://graph.facebook.com/oauth/access_token",
    params={
        "grant_type": "fb_exchange_token",
        "client_id": APP_ID,
        "client_secret": APP_SECRET,
        "fb_exchange_token": SHORT_TOKEN,
    },
)
data = resp.json()
if "error" in data:
    print(f"토큰 갱신 실패: {data['error']['message']}")
    sys.exit(1)

long_token = data["access_token"]
print(f"✅ 장기 토큰 발급 완료 (유효기간: {data.get('expires_in', 'N/A')}초)")

# GitHub CLI로 Secrets 자동 업데이트 (gh 설치 필요)
try:
    result = subprocess.run(
        ["gh", "secret", "set", "FACEBOOK_PAGE_ACCESS_TOKEN", "--body", long_token],
        capture_output=True, text=True, check=True,
    )
    print("✅ GitHub Secret FACEBOOK_PAGE_ACCESS_TOKEN 업데이트 완료")
except FileNotFoundError:
    print(f"\n수동으로 GitHub Secrets에 저장하세요:")
    print(f"  FACEBOOK_PAGE_ACCESS_TOKEN = {long_token}")
except subprocess.CalledProcessError as e:
    print(f"GitHub Secret 업데이트 실패: {e.stderr}")
    print(f"수동 저장: FACEBOOK_PAGE_ACCESS_TOKEN = {long_token}")
