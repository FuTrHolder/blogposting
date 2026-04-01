"""
YouTube OAuth 최초 인증 스크립트
1회만 실행하면 refresh_token을 발급받아 GitHub Secrets에 저장할 수 있습니다.

사용법:
  1. Google Cloud Console에서 OAuth 2.0 클라이언트 ID 생성
     (앱 유형: 데스크탑 앱, YouTube Data API v3 활성화)
  2. 아래 변수 입력 후 실행:
     python scripts/auth_youtube.py
  3. 브라우저에서 인증 후 출력된 REFRESH_TOKEN을 
     GitHub Secrets > YOUTUBE_REFRESH_TOKEN 에 저장
"""

import os
import json
from google_auth_oauthlib.flow import InstalledAppFlow
from google.oauth2.credentials import Credentials

SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]

CLIENT_ID = input("YOUTUBE_CLIENT_ID: ").strip()
CLIENT_SECRET = input("YOUTUBE_CLIENT_SECRET: ").strip()

client_config = {
    "installed": {
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",
        "redirect_uris": ["urn:ietf:wg:oauth:2.0:oob", "http://localhost"],
    }
}

flow = InstalledAppFlow.from_client_config(client_config, SCOPES)
creds = flow.run_local_server(port=0)

print("\n✅ 인증 완료!")
print(f"\n아래 값을 GitHub Secrets에 저장하세요:")
print(f"  YOUTUBE_CLIENT_ID     = {CLIENT_ID}")
print(f"  YOUTUBE_CLIENT_SECRET = {CLIENT_SECRET}")
print(f"  YOUTUBE_REFRESH_TOKEN = {creds.refresh_token}")
