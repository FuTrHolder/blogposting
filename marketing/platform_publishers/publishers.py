"""
플랫폼 발행 모듈 v4
변경사항 v4:
  - ThreadsPublisher: 이미지 URL 우선순위 개선
      1순위: THREADS_IMAGE_URL 환경변수 (수동 지정)
      2순위: blog_thumbnail_url (티스토리 썸네일 — 이미 공개 URL)
      3순위: INSTAGRAM_IMAGE_URL 환경변수
      4순위: 텍스트 전용 게시
    → 별도 업로드 없이 티스토리 썸네일을 Threads 이미지로 자동 활용
"""

import os
import time
import logging
import smtplib
import requests
import json
from abc import ABC, abstractmethod
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email import encoders
from email.mime.image import MIMEImage
from pathlib import Path
from datetime import datetime, timezone, timedelta
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

logger = logging.getLogger(__name__)

KST = timezone(timedelta(hours=9))


# ─────────────────────────────────────────────────────────────────────────────
# Base class
# ─────────────────────────────────────────────────────────────────────────────

class PlatformPublisher(ABC):
    @abstractmethod
    def publish(self, content: dict, media_paths: dict) -> dict:
        ...


# ─────────────────────────────────────────────────────────────────────────────
# YouTube Shorts
# ─────────────────────────────────────────────────────────────────────────────

class YouTubePublisher(PlatformPublisher):
    def __init__(self):
        self.client_id     = os.environ.get("YOUTUBE_CLIENT_ID", "")
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

        title       = content["blog_title"][:100]
        description = (
            f"{content.get('facebook_post', '')}\n\n"
            f"🔗 전체 분석: {content['blog_url']}\n\n"
            "⚠️ 투자 권유가 아닌 정보 제공 목적입니다."
        )
        tags = ["미국증시", "주식", "나스닥", "S&P500", "증시분석"]

        try:
            service = self._get_service()
            body    = {
                "snippet": {
                    "title": title,
                    "description": description,
                    "tags": tags,
                    "categoryId": "25",
                    "defaultLanguage": "ko",
                },
                "status": {
                    "privacyStatus": "public",
                    "selfDeclaredMadeForKids": False,
                },
            }
            media    = MediaFileUpload(
                video_path, chunksize=1024 * 1024,
                resumable=True, mimetype="video/mp4",
            )
            request  = service.videos().insert(
                part="snippet,status", body=body, media_body=media,
            )
            response = None
            while response is None:
                status, response = request.next_chunk()
                if status:
                    logger.info(f"YouTube 업로드 {int(status.progress() * 100)}%")

            video_id = response["id"]
            url      = f"https://www.youtube.com/shorts/{video_id}"
            logger.info(f"YouTube Shorts 업로드 완료: {url}")
            return {"status": "ok", "url": url, "message": "업로드 성공"}
        except Exception as e:
            logger.error(f"YouTube 업로드 실패: {e}")
            return {"status": "error", "message": str(e)}


# ─────────────────────────────────────────────────────────────────────────────
# Facebook Page
# ─────────────────────────────────────────────────────────────────────────────

class FacebookPublisher(PlatformPublisher):
    GRAPH_API = "https://graph.facebook.com/v18.0"

    def __init__(self):
        self.page_id      = os.environ.get("FACEBOOK_PAGE_ID", "")
        self.access_token = os.environ.get("FACEBOOK_PAGE_ACCESS_TOKEN", "")

    def publish(self, content: dict, media_paths: dict) -> dict:
        if not self.page_id or not self.access_token:
            return {"status": "skip", "message": "Facebook 설정 미완료"}

        caption    = content.get("facebook_post", "")
        thumb_path = media_paths.get("facebook")

        try:
            if thumb_path and Path(thumb_path).exists():
                with open(thumb_path, "rb") as f:
                    resp = requests.post(
                        f"{self.GRAPH_API}/{self.page_id}/photos",
                        data={"caption": caption, "access_token": self.access_token},
                        files={"source": f},
                        timeout=60,
                    )
            else:
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
            url     = f"https://www.facebook.com/{post_id}"
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
    Threads API 이미지 업로드 방식:
    - 로컬 파일 직접 업로드 불가 → 공개 URL 필요
    - 이미지 URL 우선순위 (C안: 티스토리 썸네일 활용):
        1순위: THREADS_IMAGE_URL 환경변수 (수동 지정)
        2순위: content["blog_thumbnail_url"] — 티스토리 대표 이미지 (이미 공개 URL)
        3순위: INSTAGRAM_IMAGE_URL 환경변수
        4순위: 이미지 없이 텍스트 전용 게시
    - [블로그 URL] 플레이스홀더를 실제 URL로 자동 치환
    """
    GRAPH_API = "https://graph.threads.net/v1.0"

    def __init__(self):
        self.user_id      = os.environ.get("THREADS_USER_ID", "")
        self.access_token = os.environ.get("THREADS_ACCESS_TOKEN", "")
        # 수동 지정 공개 URL (선택사항 — 없어도 티스토리 썸네일로 자동 대체)
        self.image_url    = os.environ.get("THREADS_IMAGE_URL",
                            os.environ.get("INSTAGRAM_IMAGE_URL", ""))

    def _replace_placeholder(self, text: str, blog_url: str) -> str:
        """[블로그 URL] 플레이스홀더를 실제 URL로 치환."""
        if blog_url:
            text = text.replace("[블로그 URL]", blog_url)
            text = text.replace("[Blog URL]", blog_url)
            text = text.replace("[블로그url]", blog_url)
        else:
            import re
            text = re.sub(r"\[블로그\s*URL\]", "", text)
            text = re.sub(r"\[Blog\s*URL\]", "", text)
        return text.strip()

    def _resolve_image_url(self, content: dict) -> str:
        # 1순위: 환경변수 수동 지정
        if self.image_url:
            logger.info("Threads 이미지: 환경변수 URL 사용")
            return self.image_url

        # 2순위: 티스토리 썸네일
        # → 다음 CDN(daumcdn.net)은 Threads 서버에서 접근 불가하므로 제외
        tistory_thumb = content.get("blog_thumbnail_url", "")
        if tistory_thumb and tistory_thumb.startswith("http"):
            if "daumcdn.net" in tistory_thumb:
                logger.warning(
                    "Threads 이미지: 다음 CDN URL은 Threads 서버 접근 불가 → 텍스트 전용으로 전환"
                )
            else:
                logger.info(f"Threads 이미지: 티스토리 썸네일 사용 → {tistory_thumb[:60]}...")
                return tistory_thumb

        # 3순위: Instagram URL 환경변수
        insta_url = os.environ.get("INSTAGRAM_IMAGE_URL", "")
        if insta_url:
            logger.info("Threads 이미지: INSTAGRAM_IMAGE_URL 사용")
            return insta_url

        logger.info("Threads 이미지: 사용 가능한 공개 URL 없음 → 텍스트 전용")
        return ""

    def publish(self, content: dict, media_paths: dict) -> dict:
        if not self.user_id or not self.access_token:
            return {"status": "skip", "message": "Threads 설정 미완료"}

        blog_url   = content.get("blog_url", "")
        text       = self._replace_placeholder(
            content.get("threads_post", ""), blog_url
        )

        # 이미지 URL 결정 (우선순위 적용)
        image_url = self._resolve_image_url(content)

        if image_url:
            result = self._publish_with_image(text, image_url)
            if result["status"] == "ok":
                return result
            logger.warning(f"Threads 이미지 게시 실패, 텍스트 전용으로 전환: {result['message']}")

        return self._publish_text(text)

    def _publish_text(self, text: str) -> dict:
        try:
            resp1 = requests.post(
                f"{self.GRAPH_API}/{self.user_id}/threads",
                params={
                    "access_token": self.access_token,
                    "text": text,
                    "media_type": "TEXT",
                },
                timeout=30,
            )
            data1 = resp1.json()
            if "error" in data1:
                return {"status": "error", "message": data1["error"]["message"]}

            creation_id = data1["id"]
            resp2 = requests.post(
                f"{self.GRAPH_API}/{self.user_id}/threads_publish",
                params={"creation_id": creation_id, "access_token": self.access_token},
                timeout=30,
            )
            data2 = resp2.json()
            if "error" in data2:
                return {"status": "error", "message": data2["error"]["message"]}

            post_id = data2.get("id", "")
            logger.info(f"Threads 텍스트 게시 완료: {post_id}")
            return {"status": "ok", "url": "https://www.threads.net",
                    "message": f"텍스트 게시 성공 (ID: {post_id})"}
        except Exception as e:
            logger.error(f"Threads 게시 실패: {e}")
            return {"status": "error", "message": str(e)}

    def _publish_with_image(self, text: str, image_url: str) -> dict:
        """이미지 URL로 Threads 이미지 포함 게시."""
        try:
            resp1 = requests.post(
                f"{self.GRAPH_API}/{self.user_id}/threads",
                params={
                    "access_token": self.access_token,
                    "text": text,
                    "media_type": "IMAGE",
                    "image_url": image_url,
                },
                timeout=30,
            )
            data1 = resp1.json()
            if "error" in data1:
                return {"status": "error", "message": data1["error"].get("message", str(data1))}

            creation_id = data1.get("id")
            if not creation_id:
                return {"status": "error", "message": "creation_id 없음"}

            # 처리 대기
            time.sleep(5)

            resp2 = requests.post(
                f"{self.GRAPH_API}/{self.user_id}/threads_publish",
                params={"creation_id": creation_id, "access_token": self.access_token},
                timeout=30,
            )
            data2 = resp2.json()
            if "error" in data2:
                return {"status": "error", "message": data2["error"].get("message", str(data2))}

            post_id = data2.get("id", "")
            logger.info(f"Threads 이미지 게시 완료: {post_id}")
            return {"status": "ok", "url": "https://www.threads.net",
                    "message": f"이미지 게시 성공 (ID: {post_id})"}
        except Exception as e:
            return {"status": "error", "message": str(e)}


# ─────────────────────────────────────────────────────────────────────────────
# Instagram (Meta Graph API)
# ─────────────────────────────────────────────────────────────────────────────

class InstagramPublisher(PlatformPublisher):
    GRAPH_API = "https://graph.facebook.com/v18.0"

    def __init__(self):
        self.account_id   = os.environ.get("INSTAGRAM_ACCOUNT_ID", "")
        self.access_token = os.environ.get(
            "INSTAGRAM_ACCESS_TOKEN",
            os.environ.get("FACEBOOK_PAGE_ACCESS_TOKEN", ""),
        )
        self.video_url    = os.environ.get("INSTAGRAM_VIDEO_URL", "")

    def publish(self, content: dict, media_paths: dict) -> dict:
        if not self.account_id or not self.access_token:
            return {"status": "skip", "message": "Instagram 설정 미완료"}

        caption   = content.get("instagram_post", content.get("threads_post", ""))
        blog_url  = content.get("blog_url", "")
        if blog_url:
            caption = caption.replace("[블로그 URL]", blog_url)

        if self.video_url:
            result = self._publish_reels(caption)
            if result["status"] == "ok":
                return result
            logger.warning(f"Reels 실패, 이미지 피드로 전환: {result['message']}")

        image_url = self._resolve_image_url(content)
        if image_url:
            return self._publish_image(caption, image_url)

        return {"status": "skip", "message": "Instagram은 이미지 없이 텍스트만 게시 불가"}

    def _resolve_image_url(self, content: dict) -> str:
        env_url   = os.environ.get("INSTAGRAM_IMAGE_URL", "")
        if env_url:
            return env_url
        thumb_url = content.get("blog_thumbnail_url", "")
        if thumb_url and thumb_url.startswith("http"):
            return thumb_url
        return ""

    def _publish_image(self, caption: str, image_url: str) -> dict:
        try:
            resp1 = requests.post(
                f"{self.GRAPH_API}/{self.account_id}/media",
                params={"image_url": image_url, "caption": caption,
                        "access_token": self.access_token},
                timeout=30,
            )
            data1 = resp1.json()
            if "error" in data1:
                return {"status": "error",
                        "message": f"컨테이너 생성 실패: {data1['error']['message']}"}

            creation_id = data1.get("id")
            if not creation_id:
                return {"status": "error", "message": "컨테이너 ID 없음"}

            if not self._wait_for_container(creation_id):
                return {"status": "error", "message": "컨테이너 준비 타임아웃"}

            resp2 = requests.post(
                f"{self.GRAPH_API}/{self.account_id}/media_publish",
                params={"creation_id": creation_id, "access_token": self.access_token},
                timeout=30,
            )
            data2 = resp2.json()
            if "error" in data2:
                return {"status": "error",
                        "message": f"게시 실패: {data2['error']['message']}"}

            post_id = data2.get("id", "")
            url     = f"https://www.instagram.com/p/{post_id}/"
            logger.info(f"Instagram 이미지 게시 완료: {url}")
            return {"status": "ok", "url": url, "message": "이미지 피드 게시 성공"}
        except Exception as e:
            logger.error(f"Instagram 이미지 게시 실패: {e}")
            return {"status": "error", "message": str(e)}

    def _publish_reels(self, caption: str) -> dict:
        try:
            resp1 = requests.post(
                f"{self.GRAPH_API}/{self.account_id}/media",
                params={
                    "media_type": "REELS",
                    "video_url": self.video_url,
                    "caption": caption,
                    "share_to_feed": "true",
                    "access_token": self.access_token,
                },
                timeout=30,
            )
            data1 = resp1.json()
            if "error" in data1:
                return {"status": "error",
                        "message": f"Reels 컨테이너 생성 실패: {data1['error']['message']}"}

            creation_id = data1.get("id")
            if not self._wait_for_container(creation_id, max_wait=120):
                return {"status": "error", "message": "Reels 처리 타임아웃"}

            resp2 = requests.post(
                f"{self.GRAPH_API}/{self.account_id}/media_publish",
                params={"creation_id": creation_id, "access_token": self.access_token},
                timeout=30,
            )
            data2 = resp2.json()
            if "error" in data2:
                return {"status": "error",
                        "message": f"Reels 게시 실패: {data2['error']['message']}"}

            post_id = data2.get("id", "")
            url     = f"https://www.instagram.com/reel/{post_id}/"
            logger.info(f"Instagram Reels 게시 완료: {url}")
            return {"status": "ok", "url": url, "message": "Reels 게시 성공"}
        except Exception as e:
            logger.error(f"Instagram Reels 게시 실패: {e}")
            return {"status": "error", "message": str(e)}

    def _wait_for_container(self, creation_id: str, max_wait: int = 30) -> bool:
        for _ in range(max_wait // 5):
            time.sleep(5)
            try:
                resp   = requests.get(
                    f"{self.GRAPH_API}/{creation_id}",
                    params={"fields": "status_code", "access_token": self.access_token},
                    timeout=10,
                )
                status = resp.json().get("status_code", "")
                if status == "FINISHED":
                    return True
                if status == "ERROR":
                    return False
                logger.info(f"컨테이너 상태: {status}")
            except Exception as e:
                logger.warning(f"컨테이너 상태 확인 실패: {e}")
        return False


# ─────────────────────────────────────────────────────────────────────────────
# 카카오 스토리채널 + 숏폼 영상 메일 발송 (통합)
# ─────────────────────────────────────────────────────────────────────────────

class KakaoStoryPublisher(PlatformPublisher):
    """
    카카오 스토리채널 텍스트 + 썸네일 + 숏폼 영상을 이메일로 발송.
    """

    def __init__(self):
        self.gmail     = os.environ.get("GMAIL_ADDRESS", "")
        self.password  = os.environ.get("GMAIL_APP_PASSWORD", "")
        self.recipient = os.environ.get("RECIPIENT_EMAIL", "")

    def publish(self, content: dict, media_paths: dict) -> dict:
        if not self.gmail or not self.recipient:
            return {"status": "skip", "message": "Gmail 설정 미완료"}

        kakao_text = content.get("kakao_post", "")
        blog_url   = content.get("blog_url", "")
        blog_title = content.get("blog_title", "")
        mode       = content.get("mode", "morning")
        mode_label = "전일 마감 리뷰" if mode == "morning" else "프리마켓 & 이슈"
        now_kst    = datetime.now(KST).strftime("%Y-%m-%d %H:%M KST")

        if blog_url:
            kakao_text = kakao_text.replace("[블로그 URL]", blog_url)

        thumb_path = (
            media_paths.get("kakao")
            or media_paths.get("facebook")
            or media_paths.get("instagram")
        )
        video_path = media_paths.get("video")

        html = f"""<!DOCTYPE html>
<html lang="ko">
<head><meta charset="UTF-8">
<style>
  body {{ font-family: -apple-system, BlinkMacSystemFont, sans-serif;
          line-height: 1.8; color: #333; max-width: 620px;
          margin: 0 auto; padding: 20px; background: #fafafa; }}
  .container {{ background: #fff; border-radius: 12px;
                padding: 32px 36px; box-shadow: 0 2px 10px rgba(0,0,0,0.08); }}
  .header {{ background: linear-gradient(135deg, #FAE100, #F5C400);
             padding: 22px 28px; border-radius: 10px; margin-bottom: 24px; }}
  .header h1 {{ margin: 0 0 6px; font-size: 19px; color: #1a1200; }}
  .header p  {{ margin: 0; color: #5a4800; font-size: 13px; }}
  .badge {{ display: inline-block; background: #1a1200; color: #FAE100;
            padding: 3px 12px; border-radius: 20px;
            font-size: 12px; font-weight: 700; margin-bottom: 10px; }}
  .kakao-box {{ background: #FFFDE7; border: 2px solid #FAE100;
                border-radius: 10px; padding: 20px 22px;
                margin: 20px 0; white-space: pre-wrap;
                font-size: 15px; color: #2c2000; line-height: 1.9; }}
  .guide {{ background: #fff8e1; border-left: 4px solid #FAE100;
            padding: 14px 18px; border-radius: 0 8px 8px 0; margin: 16px 0; }}
  .guide h3 {{ margin: 0 0 8px; color: #c97d00; font-size: 14px; }}
  .guide ol  {{ margin: 0; padding-left: 18px; font-size: 13px; color: #5a3a00; }}
  .btn {{ display: inline-block; background: #FAE100; color: #1a1200;
          padding: 10px 22px; border-radius: 8px; text-decoration: none;
          font-weight: 700; font-size: 14px; margin: 10px 0; }}
  .footer {{ margin-top: 28px; padding-top: 14px; border-top: 1px solid #f0e8b0;
             font-size: 12px; color: #aaa; text-align: center; }}
</style>
</head>
<body>
<div class="container">
  <div class="header">
    <div class="badge">카카오 스토리채널</div>
    <h1>📣 {blog_title}</h1>
    <p>{mode_label} | 생성: {now_kst}</p>
  </div>

  <div class="guide">
    <h3>📋 수동 업로드 안내</h3>
    <ol>
      <li>아래 텍스트를 카카오 스토리채널에 복사 붙여넣기</li>
      <li>첨부된 썸네일 이미지를 함께 업로드</li>
      <li>숏폼 영상(MP4)이 있으면 함께 업로드하거나 TikTok/Reels에 활용</li>
    </ol>
  </div>

  <p><strong>카카오 스토리채널 게시물</strong></p>
  <div class="kakao-box">{kakao_text or "(내용 없음)"}</div>

  <p><a href="{blog_url}" class="btn">📖 블로그 원문 보기</a></p>

  <div class="footer">
    ⚠️ 투자 권유가 아닌 정보 제공 목적입니다.<br>
    © 미국 증시 블로그 자동화 | seedsup.tistory.com
  </div>
</div>
</body></html>"""

        msg            = MIMEMultipart("mixed")
        msg["Subject"] = f"[카카오 스토리채널] {blog_title} ({mode_label})"
        msg["From"]    = self.gmail
        msg["To"]      = self.recipient

        alt = MIMEMultipart("alternative")
        alt.attach(MIMEText(html, "html", "utf-8"))
        msg.attach(alt)

        attached_files = []

        if thumb_path and Path(thumb_path).exists():
            try:
                with open(thumb_path, "rb") as f:
                    img = MIMEImage(f.read())
                    img.add_header("Content-Disposition", "attachment",
                                   filename="kakao_thumbnail.jpg")
                msg.attach(img)
                attached_files.append(f"썸네일({Path(thumb_path).name})")
                logger.info(f"카카오 썸네일 첨부: {thumb_path}")
            except Exception as e:
                logger.warning(f"썸네일 첨부 실패: {e}")

        video_attached = False
        if video_path and Path(video_path).exists():
            video_size_mb = Path(video_path).stat().st_size / (1024 * 1024)
            if video_size_mb <= 24:
                try:
                    with open(video_path, "rb") as f:
                        video_data = f.read()
                    video_part = MIMEBase("video", "mp4")
                    video_part.set_payload(video_data)
                    encoders.encode_base64(video_part)
                    video_part.add_header(
                        "Content-Disposition", "attachment",
                        filename=Path(video_path).name,
                    )
                    msg.attach(video_part)
                    attached_files.append(f"영상({video_size_mb:.1f}MB)")
                    video_attached = True
                    logger.info(f"숏폼 영상 첨부: {video_path} ({video_size_mb:.1f}MB)")
                except Exception as e:
                    logger.warning(f"영상 첨부 실패: {e}")
            else:
                logger.warning(f"영상 크기 초과({video_size_mb:.1f}MB) — 첨부 생략")
                attached_files.append(f"영상 생략({video_size_mb:.1f}MB 초과)")

        try:
            with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
                server.login(self.gmail, self.password)
                server.sendmail(self.gmail, self.recipient, msg.as_string())
            msg_detail = ", ".join(attached_files) if attached_files else "텍스트만"
            logger.info(f"카카오 메일 발송 완료: {self.recipient} [{msg_detail}]")
            return {
                "status": "ok",
                "url": "",
                "message": f"메일 발송 완료 [{msg_detail}] → {self.recipient}",
            }
        except Exception as e:
            logger.error(f"카카오 메일 발송 실패: {e}")
            return {"status": "error", "message": str(e)}


# ─────────────────────────────────────────────────────────────────────────────
# PublisherDispatcher
# ─────────────────────────────────────────────────────────────────────────────

class PublisherDispatcher:
    def __init__(self):
        self.publishers: dict[str, PlatformPublisher] = {
            "youtube":   YouTubePublisher(),
            "facebook":  FacebookPublisher(),
            "instagram": InstagramPublisher(),
            "threads":   ThreadsPublisher(),
            "kakao":     KakaoStoryPublisher(),
        }

    def publish_all(self, content: dict, media_paths: dict) -> dict[str, dict]:
        results: dict[str, dict] = {}
        for name, publisher in self.publishers.items():
            logger.info(f"[{name.upper()}] 발행 시도 중...")
            try:
                result        = publisher.publish(content, media_paths)
                results[name] = result
                status        = result.get("status", "?")
                msg           = result.get("message", "")
                logger.info(f"[{name.upper()}] {status}: {msg}")
            except Exception as e:
                results[name] = {"status": "error", "message": str(e)}
                logger.error(f"[{name.upper()}] 예외 발생: {e}")
            time.sleep(1)
        return results
