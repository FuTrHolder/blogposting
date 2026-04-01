"""
플랫폼 발행 모듈
각 플랫폼 API를 통해 콘텐츠를 자동 업로드합니다.

보안 원칙:
- 모든 API 키/토큰은 환경변수로만 관리 (코드에 직접 기재 금지)
- OAuth 토큰은 GitHub Secrets에 저장
"""

import os
import time
import logging
import requests
import json
from abc import ABC, abstractmethod
from pathlib import Path
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Base class
# ─────────────────────────────────────────────────────────────────────────────

class PlatformPublisher(ABC):
    @abstractmethod
    def publish(self, content: dict, media_paths: dict) -> dict:
        """
        content: ContentAdapter.generate_all() 반환값
        media_paths: {facebook: path, threads: path, shorts_cover: path, video: path}
        반환: {"status": "ok"|"skip"|"error", "url": "...", "message": "..."}
        """
        ...


# ─────────────────────────────────────────────────────────────────────────────
# YouTube Shorts
# ─────────────────────────────────────────────────────────────────────────────

class YouTubePublisher(PlatformPublisher):
    """
    필요 환경변수:
      YOUTUBE_CLIENT_ID, YOUTUBE_CLIENT_SECRET,
      YOUTUBE_REFRESH_TOKEN  (최초 1회 OAuth 인증 후 발급)

    OAuth 최초 인증: scripts/auth_youtube.py 실행
    """

    def __init__(self):
        self.client_id = os.environ.get("YOUTUBE_CLIENT_ID", "")
        self.client_secret = os.environ.get("YOUTUBE_CLIENT_SECRET", "")
        self.refresh_token = os.environ.get("YOUTUBE_REFRESH_TOKEN", "")

    def _get_service(self):
        creds = Credentials(
            token=None,
            refresh_token=self.refresh_token,
            token_uri="https://oauth2.googleapis.com/token",
            client_id=self.client_id,
            client_secret=self.client_secret,
        )
        return build("youtube", "v3", credentials=creds)

    def publish(self, content: dict, media_paths: dict) -> dict:
        video_path = media_paths.get("video")
        if not video_path or not Path(video_path).exists():
            return {"status": "skip", "message": "영상 파일 없음"}

        if not all([self.client_id, self.client_secret, self.refresh_token]):
            return {"status": "skip", "message": "YouTube OAuth 미설정"}

        title = content["blog_title"][:100]
        description = (
            f"{content.get('facebook_post', '')}\n\n"
            f"🔗 전체 분석: {content['blog_url']}\n\n"
            "⚠️ 투자 권유가 아닌 정보 제공 목적입니다."
        )
        tags = ["미국증시", "주식", "나스닥", "S&P500", "증시분석"]

        try:
            service = self._get_service()
            body = {
                "snippet": {
                    "title": title,
                    "description": description,
                    "tags": tags,
                    "categoryId": "25",  # News & Politics
                    "defaultLanguage": "ko",
                },
                "status": {
                    "privacyStatus": "public",
                    "selfDeclaredMadeForKids": False,
                },
            }
            media = MediaFileUpload(
                video_path,
                chunksize=1024 * 1024,
                resumable=True,
                mimetype="video/mp4",
            )
            request = service.videos().insert(
                part="snippet,status",
                body=body,
                media_body=media,
            )
            response = None
            while response is None:
                status, response = request.next_chunk()
                if status:
                    logger.info(f"YouTube 업로드 {int(status.progress() * 100)}%")

            video_id = response["id"]
            url = f"https://www.youtube.com/shorts/{video_id}"
            logger.info(f"YouTube Shorts 업로드 완료: {url}")
            return {"status": "ok", "url": url, "message": "업로드 성공"}

        except Exception as e:
            logger.error(f"YouTube 업로드 실패: {e}")
            return {"status": "error", "message": str(e)}


# ─────────────────────────────────────────────────────────────────────────────
# TikTok
# ─────────────────────────────────────────────────────────────────────────────

class TikTokPublisher(PlatformPublisher):
    """
    필요 환경변수:
      TIKTOK_ACCESS_TOKEN  (TikTok for Developers 비즈니스 계정 필요)
      TIKTOK_OPEN_ID       (계정 고유 ID)

    주의: 일반 개인 계정은 Content Posting API 접근 불가.
    비즈니스 계정 심사 필요 (https://developers.tiktok.com)
    """

    API_BASE = "https://open.tiktokapis.com/v2"

    def __init__(self):
        self.access_token = os.environ.get("TIKTOK_ACCESS_TOKEN", "")
        self.open_id = os.environ.get("TIKTOK_OPEN_ID", "")

    @property
    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.access_token}",
            "Content-Type": "application/json; charset=UTF-8",
        }

    def publish(self, content: dict, media_paths: dict) -> dict:
        video_path = media_paths.get("video")
        if not video_path or not Path(video_path).exists():
            return {"status": "skip", "message": "영상 파일 없음"}

        if not self.access_token:
            return {"status": "skip", "message": "TikTok 액세스 토큰 미설정 (비즈니스 계정 필요)"}

        caption = content.get("x_post", content.get("threads_post", ""))[:2200]

        try:
            # 1단계: 업로드 초기화
            init_resp = requests.post(
                f"{self.API_BASE}/post/publish/video/init/",
                headers=self._headers,
                json={
                    "post_info": {
                        "title": caption,
                        "privacy_level": "PUBLIC_TO_EVERYONE",
                        "disable_duet": False,
                        "disable_comment": False,
                        "disable_stitch": False,
                    },
                    "source_info": {
                        "source": "FILE_UPLOAD",
                        "video_size": Path(video_path).stat().st_size,
                        "chunk_size": Path(video_path).stat().st_size,
                        "total_chunk_count": 1,
                    },
                },
                timeout=30,
            )
            init_data = init_resp.json()
            if "error" in init_data and init_data["error"]["code"] != "ok":
                return {"status": "error", "message": init_data["error"]["message"]}

            upload_url = init_data["data"]["upload_url"]
            publish_id = init_data["data"]["publish_id"]
            video_size = Path(video_path).stat().st_size

            # 2단계: 영상 업로드
            with open(video_path, "rb") as f:
                upload_resp = requests.put(
                    upload_url,
                    data=f,
                    headers={
                        "Content-Range": f"bytes 0-{video_size - 1}/{video_size}",
                        "Content-Type": "video/mp4",
                    },
                    timeout=120,
                )
            if upload_resp.status_code not in (200, 201):
                return {"status": "error", "message": f"업로드 실패: {upload_resp.status_code}"}

            logger.info(f"TikTok 업로드 완료 (publish_id: {publish_id})")
            return {"status": "ok", "url": "https://www.tiktok.com", "message": f"업로드 성공 (ID: {publish_id})"}

        except Exception as e:
            logger.error(f"TikTok 업로드 실패: {e}")
            return {"status": "error", "message": str(e)}


# ─────────────────────────────────────────────────────────────────────────────
# Facebook Page
# ─────────────────────────────────────────────────────────────────────────────

class FacebookPublisher(PlatformPublisher):
    """
    필요 환경변수:
      FACEBOOK_PAGE_ID
      FACEBOOK_PAGE_ACCESS_TOKEN  (Meta Graph API, 무료)

    토큰 발급: https://developers.facebook.com/tools/explorer/
    장기 토큰으로 교환 후 GitHub Secrets에 저장 (60일 유효, 자동 갱신 스크립트 포함)
    """

    GRAPH_API = "https://graph.facebook.com/v18.0"

    def __init__(self):
        self.page_id = os.environ.get("FACEBOOK_PAGE_ID", "")
        self.access_token = os.environ.get("FACEBOOK_PAGE_ACCESS_TOKEN", "")

    def publish(self, content: dict, media_paths: dict) -> dict:
        if not self.page_id or not self.access_token:
            return {"status": "skip", "message": "Facebook 설정 미완료"}

        caption = content.get("facebook_post", "")
        thumb_path = media_paths.get("facebook")

        try:
            if thumb_path and Path(thumb_path).exists():
                # 이미지 포함 게시물
                with open(thumb_path, "rb") as f:
                    resp = requests.post(
                        f"{self.GRAPH_API}/{self.page_id}/photos",
                        data={
                            "caption": caption,
                            "access_token": self.access_token,
                        },
                        files={"source": f},
                        timeout=60,
                    )
            else:
                # 텍스트만
                resp = requests.post(
                    f"{self.GRAPH_API}/{self.page_id}/feed",
                    json={
                        "message": caption,
                        "link": content.get("blog_url", ""),
                        "access_token": self.access_token,
                    },
                    timeout=30,
                )

            data = resp.json()
            if "error" in data:
                return {"status": "error", "message": data["error"]["message"]}

            post_id = data.get("id", "")
            url = f"https://www.facebook.com/{post_id}"
            logger.info(f"Facebook 게시 완료: {url}")
            return {"status": "ok", "url": url, "message": "게시 성공"}

        except Exception as e:
            logger.error(f"Facebook 게시 실패: {e}")
            return {"status": "error", "message": str(e)}


# ─────────────────────────────────────────────────────────────────────────────
# Threads
# ─────────────────────────────────────────────────────────────────────────────

class ThreadsPublisher(PlatformPublisher):
    """
    필요 환경변수:
      THREADS_USER_ID
      THREADS_ACCESS_TOKEN  (Meta Threads API, 무료)

    토큰 발급: https://developers.facebook.com/docs/threads
    Instagram 계정 연결 필요
    """

    GRAPH_API = "https://graph.threads.net/v1.0"

    def __init__(self):
        self.user_id = os.environ.get("THREADS_USER_ID", "")
        self.access_token = os.environ.get("THREADS_ACCESS_TOKEN", "")

    def publish(self, content: dict, media_paths: dict) -> dict:
        if not self.user_id or not self.access_token:
            return {"status": "skip", "message": "Threads 설정 미완료"}

        text = content.get("threads_post", "")
        thumb_path = media_paths.get("threads")

        try:
            # 1단계: 미디어 컨테이너 생성
            container_params: dict = {
                "access_token": self.access_token,
                "text": text,
            }

            if thumb_path and Path(thumb_path).exists():
                # 이미지 업로드는 공개 URL이 필요 → 이미지 없이 텍스트만 게시
                # (이미지 URL이 있으면 image_url 파라미터로 전달 가능)
                container_params["media_type"] = "TEXT"
            else:
                container_params["media_type"] = "TEXT"

            resp1 = requests.post(
                f"{self.GRAPH_API}/{self.user_id}/threads",
                params=container_params,
                timeout=30,
            )
            data1 = resp1.json()
            if "error" in data1:
                return {"status": "error", "message": data1["error"]["message"]}

            creation_id = data1["id"]

            # 2단계: 게시
            resp2 = requests.post(
                f"{self.GRAPH_API}/{self.user_id}/threads_publish",
                params={
                    "creation_id": creation_id,
                    "access_token": self.access_token,
                },
                timeout=30,
            )
            data2 = resp2.json()
            if "error" in data2:
                return {"status": "error", "message": data2["error"]["message"]}

            post_id = data2.get("id", "")
            logger.info(f"Threads 게시 완료: {post_id}")
            return {"status": "ok", "url": "https://www.threads.net", "message": f"게시 성공 (ID: {post_id})"}

        except Exception as e:
            logger.error(f"Threads 게시 실패: {e}")
            return {"status": "error", "message": str(e)}


# ─────────────────────────────────────────────────────────────────────────────
# X (Twitter) — 유료 API ($100/월~)
# ─────────────────────────────────────────────────────────────────────────────

class XPublisher(PlatformPublisher):
    """
    필요 환경변수:
      X_API_KEY, X_API_SECRET
      X_ACCESS_TOKEN, X_ACCESS_TOKEN_SECRET
      X_BEARER_TOKEN

    주의: X API Basic Tier 최소 $100/월 필요.
    무료 티어는 읽기 전용. 쓰기 불가.
    활성화하려면 X_ENABLED=true 환경변수 설정 필요.
    """

    def __init__(self):
        self.enabled = os.environ.get("X_ENABLED", "false").lower() == "true"
        self.api_key = os.environ.get("X_API_KEY", "")
        self.api_secret = os.environ.get("X_API_SECRET", "")
        self.access_token = os.environ.get("X_ACCESS_TOKEN", "")
        self.access_token_secret = os.environ.get("X_ACCESS_TOKEN_SECRET", "")

    def publish(self, content: dict, media_paths: dict) -> dict:
        if not self.enabled:
            return {"status": "skip", "message": "X API 비활성 (X_ENABLED=true 설정 필요, 유료 $100/월~)"}

        try:
            import tweepy
            client = tweepy.Client(
                consumer_key=self.api_key,
                consumer_secret=self.api_secret,
                access_token=self.access_token,
                access_token_secret=self.access_token_secret,
            )
            tweet_text = content.get("x_post", "")[:280]
            response = client.create_tweet(text=tweet_text)
            tweet_id = response.data["id"]
            url = f"https://x.com/i/web/status/{tweet_id}"
            logger.info(f"X 트윗 완료: {url}")
            return {"status": "ok", "url": url, "message": "트윗 성공"}
        except ImportError:
            return {"status": "skip", "message": "tweepy 패키지 미설치 (pip install tweepy)"}
        except Exception as e:
            logger.error(f"X 트윗 실패: {e}")
            return {"status": "error", "message": str(e)}


# ─────────────────────────────────────────────────────────────────────────────
# 카카오 스토리채널 — 공식 API 없음 → 이메일 발송으로 대체
# ─────────────────────────────────────────────────────────────────────────────

class KakaoStoryPublisher(PlatformPublisher):
    """
    카카오 스토리채널은 공식 자동화 API가 없습니다.
    대안: Gmail로 원고 발송 → 수동 업로드

    필요 환경변수:
      GMAIL_ADDRESS, GMAIL_APP_PASSWORD, RECIPIENT_EMAIL  (기존 email_sender.py와 동일)
    """

    def __init__(self):
        self.gmail = os.environ.get("GMAIL_ADDRESS", "")
        self.password = os.environ.get("GMAIL_APP_PASSWORD", "")
        self.recipient = os.environ.get("RECIPIENT_EMAIL", "")

    def publish(self, content: dict, media_paths: dict) -> dict:
        if not self.gmail or not self.recipient:
            return {"status": "skip", "message": "Gmail 설정 미완료"}

        import smtplib
        from email.mime.multipart import MIMEMultipart
        from email.mime.text import MIMEText
        from email.mime.image import MIMEImage

        kakao_text = content.get("kakao_post", "")
        blog_url = content.get("blog_url", "")
        blog_title = content.get("blog_title", "")
        thumb_path = media_paths.get("facebook")  # 1200×630 썸네일 재활용

        subject = f"[카카오 스토리채널 원고] {blog_title}"
        html = f"""<html><body style="font-family:sans-serif;line-height:1.8;max-width:600px;margin:0 auto;">
<h2 style="color:#FAE100">📣 카카오 스토리채널 업로드 원고</h2>
<div style="background:#fff9c4;padding:20px;border-radius:8px;white-space:pre-wrap;">{kakao_text}</div>
<p style="margin-top:20px"><strong>블로그 링크:</strong> <a href="{blog_url}">{blog_url}</a></p>
<p style="color:#888;font-size:12px">※ 카카오 스토리채널에 수동으로 업로드해 주세요. 첨부 이미지를 썸네일로 사용하세요.</p>
</body></html>"""

        try:
            msg = MIMEMultipart("related")
            msg["Subject"] = subject
            msg["From"] = self.gmail
            msg["To"] = self.recipient

            alt = MIMEMultipart("alternative")
            alt.attach(MIMEText(html, "html", "utf-8"))
            msg.attach(alt)

            if thumb_path and Path(thumb_path).exists():
                with open(thumb_path, "rb") as f:
                    img = MIMEImage(f.read())
                    img.add_header("Content-Disposition", "attachment",
                                   filename="kakao_thumbnail.jpg")
                msg.attach(img)

            with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
                server.login(self.gmail, self.password)
                server.sendmail(self.gmail, self.recipient, msg.as_string())

            logger.info(f"카카오 스토리채널 원고 이메일 발송: {self.recipient}")
            return {"status": "ok", "url": "", "message": f"원고 이메일 발송 완료 → {self.recipient}"}
        except Exception as e:
            logger.error(f"카카오 이메일 발송 실패: {e}")
            return {"status": "error", "message": str(e)}



# ─────────────────────────────────────────────────────────────────────────────
# Instagram (Meta Graph API) — 무료
# ─────────────────────────────────────────────────────────────────────────────

class InstagramPublisher(PlatformPublisher):
    """
    Meta Graph API로 Instagram 비즈니스/크리에이터 계정에 자동 업로드합니다.
    피드 이미지 포스트와 Reels(영상) 두 가지를 모두 지원합니다.

    필요 환경변수:
      INSTAGRAM_ACCOUNT_ID      Instagram 비즈니스 계정 ID
                                 (Graph API Explorer → /me/accounts → instagram_business_account.id)
      INSTAGRAM_ACCESS_TOKEN    Facebook 페이지 장기 토큰 (Facebook 페이지와 연결된 것)
                                 FACEBOOK_PAGE_ACCESS_TOKEN과 동일 토큰을 재사용 가능

    사전 조건:
      1. Instagram 계정을 비즈니스 또는 크리에이터 계정으로 전환
      2. Facebook 페이지와 Instagram 계정 연결
         (Facebook 페이지 설정 → Instagram → 계정 연결)
      3. Meta for Developers 앱에 instagram_basic, instagram_content_publish 권한 승인
         (앱 검수 없이 개발자 계정 본인은 바로 사용 가능)

    이미지 업로드 방식:
      - Instagram Graph API는 로컬 파일 직접 업로드를 지원하지 않습니다.
      - 공개 접근 가능한 이미지 URL이 필요합니다.
      - 이 구현에서는 두 가지 전략을 순서대로 시도합니다:
        1순위: 티스토리 원글의 썸네일 URL (이미 공개된 URL)
        2순위: GitHub Actions artifact URL (영상의 경우)
        → 이미지 URL이 없으면 텍스트 전용 대체 전략으로 Threads API 호환 엔드포인트 사용

    Reels 업로드:
      - 영상은 공개 URL이 필요하므로, GitHub Releases나 별도 스토리지 없이는
        Reels 자동 업로드가 어렵습니다.
      - 이 구현에서는 이미지 피드 포스트를 기본으로 하고,
        INSTAGRAM_VIDEO_URL 환경변수가 있을 때만 Reels를 시도합니다.
    """

    GRAPH_API = "https://graph.facebook.com/v18.0"

    def __init__(self):
        self.account_id = os.environ.get("INSTAGRAM_ACCOUNT_ID", "")
        self.access_token = os.environ.get(
            "INSTAGRAM_ACCESS_TOKEN",
            os.environ.get("FACEBOOK_PAGE_ACCESS_TOKEN", ""),  # 페이스북 토큰 폴백
        )
        # 외부 영상 URL (선택): GitHub Release, S3, CDN 등 공개 URL
        self.video_url = os.environ.get("INSTAGRAM_VIDEO_URL", "")

    def publish(self, content: dict, media_paths: dict) -> dict:
        if not self.account_id or not self.access_token:
            return {"status": "skip", "message": "Instagram 설정 미완료 (INSTAGRAM_ACCOUNT_ID, INSTAGRAM_ACCESS_TOKEN 필요)"}

        caption = content.get("instagram_post", content.get("threads_post", ""))

        # Reels 시도 (외부 영상 URL이 있을 때)
        if self.video_url:
            result = self._publish_reels(caption)
            if result["status"] == "ok":
                return result
            logger.warning(f"Reels 실패, 이미지 피드로 전환: {result['message']}")

        # 이미지 피드 포스트
        image_url = self._resolve_image_url(content, media_paths)
        if image_url:
            return self._publish_image(caption, image_url)

        # 이미지 URL 없음 → 캐러셀 없이 텍스트+링크 게시 불가 (Instagram 정책)
        return {
            "status": "skip",
            "message": "Instagram은 이미지 없이 텍스트만 게시 불가 (공개 이미지 URL 필요)",
        }

    def _resolve_image_url(self, content: dict, media_paths: dict) -> str:
        """
        공개 이미지 URL을 찾습니다.
        Instagram API는 로컬 파일 업로드를 지원하지 않아 공개 URL이 필수입니다.
        1순위: 티스토리 원글 썸네일 URL (이미 공개된 URL)
        2순위: INSTAGRAM_IMAGE_URL 환경변수 (수동 지정)
        """
        # 환경변수로 명시적 지정된 경우
        env_url = os.environ.get("INSTAGRAM_IMAGE_URL", "")
        if env_url:
            return env_url

        # 티스토리 원글 썸네일 URL 재활용 (크롤러가 추출한 값)
        # content dict에 blog_thumbnail_url 키가 있으면 사용
        thumb_url = content.get("blog_thumbnail_url", "")
        if thumb_url and thumb_url.startswith("http"):
            logger.info(f"티스토리 썸네일 URL 사용: {thumb_url}")
            return thumb_url

        return ""

    def _publish_image(self, caption: str, image_url: str) -> dict:
        """이미지 피드 포스트를 게시합니다 (2단계: 컨테이너 생성 → 게시)."""
        try:
            # 1단계: 미디어 컨테이너 생성
            resp1 = requests.post(
                f"{self.GRAPH_API}/{self.account_id}/media",
                params={
                    "image_url": image_url,
                    "caption": caption,
                    "access_token": self.access_token,
                },
                timeout=30,
            )
            data1 = resp1.json()
            if "error" in data1:
                return {"status": "error", "message": f"컨테이너 생성 실패: {data1['error']['message']}"}

            creation_id = data1.get("id")
            if not creation_id:
                return {"status": "error", "message": "컨테이너 ID 없음"}

            # 컨테이너 상태 확인 (최대 30초 대기)
            if not self._wait_for_container(creation_id):
                return {"status": "error", "message": "컨테이너 준비 타임아웃"}

            # 2단계: 게시
            resp2 = requests.post(
                f"{self.GRAPH_API}/{self.account_id}/media_publish",
                params={
                    "creation_id": creation_id,
                    "access_token": self.access_token,
                },
                timeout=30,
            )
            data2 = resp2.json()
            if "error" in data2:
                return {"status": "error", "message": f"게시 실패: {data2['error']['message']}"}

            post_id = data2.get("id", "")
            url = f"https://www.instagram.com/p/{post_id}/"
            logger.info(f"Instagram 이미지 게시 완료: {url}")
            return {"status": "ok", "url": url, "message": "이미지 피드 게시 성공"}

        except Exception as e:
            logger.error(f"Instagram 이미지 게시 실패: {e}")
            return {"status": "error", "message": str(e)}

    def _publish_reels(self, caption: str) -> dict:
        """Reels(영상)를 게시합니다. 공개 video_url이 필요합니다."""
        try:
            # 1단계: Reels 컨테이너 생성
            resp1 = requests.post(
                f"{self.GRAPH_API}/{self.account_id}/media",
                params={
                    "media_type": "REELS",
                    "video_url": self.video_url,
                    "caption": caption,
                    "share_to_feed": "true",   # 피드에도 동시 노출
                    "access_token": self.access_token,
                },
                timeout=30,
            )
            data1 = resp1.json()
            if "error" in data1:
                return {"status": "error", "message": f"Reels 컨테이너 생성 실패: {data1['error']['message']}"}

            creation_id = data1.get("id")
            if not creation_id:
                return {"status": "error", "message": "Reels 컨테이너 ID 없음"}

            # 영상 처리 대기 (최대 2분)
            if not self._wait_for_container(creation_id, max_wait=120):
                return {"status": "error", "message": "Reels 처리 타임아웃 (영상이 너무 크거나 URL 접근 불가)"}

            # 2단계: 게시
            resp2 = requests.post(
                f"{self.GRAPH_API}/{self.account_id}/media_publish",
                params={
                    "creation_id": creation_id,
                    "access_token": self.access_token,
                },
                timeout=30,
            )
            data2 = resp2.json()
            if "error" in data2:
                return {"status": "error", "message": f"Reels 게시 실패: {data2['error']['message']}"}

            post_id = data2.get("id", "")
            url = f"https://www.instagram.com/reel/{post_id}/"
            logger.info(f"Instagram Reels 게시 완료: {url}")
            return {"status": "ok", "url": url, "message": "Reels 게시 성공"}

        except Exception as e:
            logger.error(f"Instagram Reels 게시 실패: {e}")
            return {"status": "error", "message": str(e)}

    def _wait_for_container(self, creation_id: str, max_wait: int = 30) -> bool:
        """미디어 컨테이너가 FINISHED 상태가 될 때까지 폴링합니다."""
        for _ in range(max_wait // 5):
            time.sleep(5)
            try:
                resp = requests.get(
                    f"{self.GRAPH_API}/{creation_id}",
                    params={
                        "fields": "status_code",
                        "access_token": self.access_token,
                    },
                    timeout=10,
                )
                status = resp.json().get("status_code", "")
                if status == "FINISHED":
                    return True
                if status == "ERROR":
                    logger.warning(f"컨테이너 처리 오류 (status: {status})")
                    return False
                logger.info(f"컨테이너 상태: {status} (대기 중...)")
            except Exception as e:
                logger.warning(f"컨테이너 상태 확인 실패: {e}")
        return False




class PublisherDispatcher:
    """모든 플랫폼 발행을 순서대로 실행하고 결과를 반환합니다."""

    def __init__(self):
        self.publishers: dict[str, PlatformPublisher] = {
            "youtube": YouTubePublisher(),
            "tiktok": TikTokPublisher(),
            "facebook": FacebookPublisher(),
            "instagram": InstagramPublisher(),
            "threads": ThreadsPublisher(),
            "x": XPublisher(),
            "kakao": KakaoStoryPublisher(),
        }

    def publish_all(self, content: dict, media_paths: dict) -> dict[str, dict]:
        """모든 플랫폼에 발행하고 결과를 반환합니다."""
        results = {}
        for name, publisher in self.publishers.items():
            logger.info(f"[{name.upper()}] 발행 시도 중...")
            try:
                result = publisher.publish(content, media_paths)
                results[name] = result
                status = result.get("status", "?")
                msg = result.get("message", "")
                logger.info(f"[{name.upper()}] {status}: {msg}")
            except Exception as e:
                results[name] = {"status": "error", "message": str(e)}
                logger.error(f"[{name.upper()}] 예외 발생: {e}")
            time.sleep(1)  # API 레이트 리밋 방지
        return results
