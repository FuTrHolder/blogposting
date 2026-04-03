"""
플랫폼 발행 모듈 v2
- TikTok 자동 업로드 제거 (API 제한으로 불가)
- 대체: 생성된 숏폼 영상 파일을 이메일로 발송 (수동 업로드용)
- YouTube, Facebook, Instagram, Threads, X, 카카오 유지

보안 원칙:
- 모든 API 키/토큰은 환경변수로만 관리
- OAuth 토큰은 GitHub Secrets에 저장
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
        """
        content: ContentAdapter.generate_all() 반환값
        media_paths: {facebook: path, instagram: path, video: path, ...}
        반환: {"status": "ok"|"skip"|"error", "url": "...", "message": "..."}
        """
        ...


# ─────────────────────────────────────────────────────────────────────────────
# 영상 이메일 발송 (TikTok 대체)
# ─────────────────────────────────────────────────────────────────────────────

class VideoEmailPublisher(PlatformPublisher):
    """
    생성된 숏폼 영상 파일을 이메일로 발송합니다.
    TikTok, Instagram Reels 등 수동 업로드가 필요한 플랫폼을 위한 대체 수단입니다.

    이메일에 포함되는 내용:
      - 숏폼 영상 파일 첨부 (MP4)
      - 플랫폼별 SNS 게시물 텍스트 (TikTok, Instagram, X 카피)
      - 썸네일 이미지 첨부

    필요 환경변수:
      GMAIL_ADDRESS       : 발신 Gmail 주소
      GMAIL_APP_PASSWORD  : Gmail 앱 비밀번호 (2단계 인증 필요)
      RECIPIENT_EMAIL     : 수신 이메일 주소
    """

    def __init__(self):
        self.sender    = os.environ.get("GMAIL_ADDRESS", "")
        self.password  = os.environ.get("GMAIL_APP_PASSWORD", "")
        self.recipient = os.environ.get("RECIPIENT_EMAIL", "")

    def publish(self, content: dict, media_paths: dict) -> dict:
        if not self.sender or not self.recipient:
            return {"status": "skip", "message": "Gmail 설정 미완료"}

        video_path = media_paths.get("video")
        if not video_path or not Path(video_path).exists():
            return {"status": "skip", "message": "영상 파일 없음 - 이메일 건너뜀"}

        blog_title = content.get("blog_title", "미국 증시 분석")
        blog_url   = content.get("blog_url", "")
        mode       = content.get("mode", "morning")
        mode_label = "전일 마감 리뷰" if mode == "morning" else "프리마켓 & 이슈"
        now_kst    = datetime.now(KST).strftime("%Y-%m-%d %H:%M KST")

        # ── 플랫폼별 업로드용 텍스트 정리 ────────────────────────────────────
        tiktok_text    = content.get("tiktok_script", content.get("youtube_script", []))
        instagram_post = content.get("instagram_post", "")
        x_post         = content.get("x_post", "")
        threads_post   = content.get("threads_post", "")
        kakao_post     = content.get("kakao_post", "")

        # 스크립트 → 텍스트 변환
        script_text = ""
        if isinstance(tiktok_text, list):
            for slide in tiktok_text:
                script_text += (
                    f"[{slide.get('slide', '?')}장면] "
                    f"{slide.get('title', '')} / "
                    f"{slide.get('body', '')}\n"
                )

        # ── HTML 이메일 본문 ─────────────────────────────────────────────────
        html = f"""<!DOCTYPE html>
<html lang="ko">
<head><meta charset="UTF-8">
<style>
  body {{ font-family: -apple-system, BlinkMacSystemFont, sans-serif;
          line-height: 1.8; color: #333; max-width: 680px; margin: 0 auto; padding: 20px; }}
  .header {{ background: linear-gradient(135deg, #1a1a2e, #16213e);
             color: white; padding: 28px 32px; border-radius: 12px; margin-bottom: 24px; }}
  .header h1 {{ margin: 0 0 8px; font-size: 20px; }}
  .header p  {{ margin: 0; color: #94a3b8; font-size: 13px; }}
  .badge {{ display: inline-block; background: #3b82f6; color: white;
            padding: 4px 14px; border-radius: 20px; font-size: 12px; font-weight: 600;
            margin-bottom: 12px; }}
  .section {{ background: #f8fafc; border-left: 4px solid #3b82f6;
              padding: 16px 20px; margin: 16px 0; border-radius: 0 8px 8px 0; }}
  .section h3 {{ margin: 0 0 10px; color: #1e40af; font-size: 15px; }}
  .section pre {{ margin: 0; white-space: pre-wrap; font-size: 13px; color: #475569; }}
  .highlight {{ background: #fef9c3; border-left-color: #f59e0b; }}
  .tiktok    {{ background: #fdf2f8; border-left-color: #ec4899; }}
  .instagram {{ background: #f5f3ff; border-left-color: #8b5cf6; }}
  .x         {{ background: #f0f9ff; border-left-color: #0ea5e9; }}
  .footer    {{ margin-top: 32px; padding-top: 16px; border-top: 1px solid #e2e8f0;
                font-size: 12px; color: #94a3b8; text-align: center; }}
  .btn       {{ display: inline-block; background: #3b82f6; color: white;
                padding: 10px 24px; border-radius: 8px; text-decoration: none;
                font-weight: 600; font-size: 14px; margin: 8px 0; }}
  .upload-guide {{ background: #fff7ed; border: 1px solid #fed7aa;
                   padding: 16px; border-radius: 8px; margin: 16px 0; }}
  .upload-guide h3 {{ margin: 0 0 10px; color: #c2410c; font-size: 15px; }}
  .upload-guide ol {{ margin: 0; padding-left: 20px; color: #7c2d12; font-size: 13px; }}
</style>
</head>
<body>
<div class="header">
  <div class="badge">🎬 숏폼 영상 발송</div>
  <h1>📊 {blog_title}</h1>
  <p>{mode_label} | 생성 시각: {now_kst}</p>
</div>

<div class="upload-guide">
  <h3>📋 수동 업로드 가이드</h3>
  <ol>
    <li>첨부된 MP4 파일을 저장하세요.</li>
    <li><strong>TikTok</strong>: TikTok 앱 → 업로드 → 아래 텍스트 복사 붙여넣기</li>
    <li><strong>Instagram Reels</strong>: 인스타그램 앱 → 릴스 → 업로드</li>
    <li>아래 각 플랫폼 전용 텍스트를 복사해서 게시물에 붙여넣으세요.</li>
  </ol>
</div>

<p><a href="{blog_url}" class="btn">📖 블로그 원문 보기</a></p>

<!-- TikTok 대본 -->
<div class="section tiktok">
  <h3>🎵 TikTok 업로드용 텍스트 (장면 대본)</h3>
  <pre>{script_text or "(스크립트 없음)"}</pre>
</div>

<!-- Instagram -->
<div class="section instagram">
  <h3>📸 Instagram Reels 캡션</h3>
  <pre>{instagram_post or "(없음)"}</pre>
</div>

<!-- X (Twitter) -->
<div class="section x">
  <h3>🐦 X (트위터) 텍스트</h3>
  <pre>{x_post or "(없음)"}</pre>
</div>

<!-- Threads -->
<div class="section">
  <h3>🧵 Threads 텍스트</h3>
  <pre>{threads_post or "(없음)"}</pre>
</div>

<!-- Kakao -->
<div class="section highlight">
  <h3>💛 카카오 스토리채널 텍스트</h3>
  <pre>{kakao_post or "(없음)"}</pre>
</div>

<div class="footer">
  ⚠️ 이 이메일은 자동으로 생성되었습니다. 투자 권유가 아닌 정보 제공 목적입니다.<br>
  © 미국 증시 블로그 자동화 | seedsup.tistory.com
</div>
</body></html>"""

        # ── 이메일 구성 ──────────────────────────────────────────────────────
        msg             = MIMEMultipart("mixed")
        msg["Subject"]  = f"[🎬 숏폼 영상] {blog_title} ({mode_label})"
        msg["From"]     = self.sender
        msg["To"]       = self.recipient

        alt = MIMEMultipart("alternative")
        alt.attach(MIMEText(html, "html", "utf-8"))
        msg.attach(alt)

        # ── 영상 파일 첨부 ───────────────────────────────────────────────────
        video_size_mb = Path(video_path).stat().st_size / (1024 * 1024)
        if video_size_mb > 24:
            logger.warning(f"영상 파일 크기 {video_size_mb:.1f}MB > 24MB. Gmail 첨부 한도 초과 가능.")

        try:
            with open(video_path, "rb") as f:
                video_data  = f.read()
            video_part = MIMEBase("video", "mp4")
            video_part.set_payload(video_data)
            encoders.encode_base64(video_part)
            video_part.add_header(
                "Content-Disposition",
                "attachment",
                filename=Path(video_path).name,
            )
            msg.attach(video_part)
            logger.info(f"영상 첨부 완료: {Path(video_path).name} ({video_size_mb:.1f}MB)")
        except Exception as e:
            logger.warning(f"영상 첨부 실패: {e}")

        # ── 썸네일 이미지 첨부 (있는 경우) ──────────────────────────────────
        for platform in ["instagram", "facebook", "kakao"]:
            thumb_path = media_paths.get(platform)
            if thumb_path and Path(thumb_path).exists():
                try:
                    with open(thumb_path, "rb") as f:
                        img_data = f.read()
                    img_part = MIMEBase("image", "jpeg")
                    img_part.set_payload(img_data)
                    encoders.encode_base64(img_part)
                    img_part.add_header(
                        "Content-Disposition",
                        "attachment",
                        filename=f"thumbnail_{platform}.jpg",
                    )
                    msg.attach(img_part)
                    logger.info(f"썸네일 첨부: {platform}")
                except Exception as e:
                    logger.warning(f"썸네일 첨부 실패 ({platform}): {e}")
                break  # 첫 번째 성공한 썸네일만 첨부

        # ── 발송 ─────────────────────────────────────────────────────────────
        try:
            with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
                server.login(self.sender, self.password)
                server.sendmail(self.sender, self.recipient, msg.as_string())
            logger.info(f"영상 이메일 발송 완료: {self.recipient}")
            return {
                "status": "ok",
                "url": "",
                "message": f"영상 이메일 발송 완료 ({video_size_mb:.1f}MB) → {self.recipient}",
            }
        except smtplib.SMTPException as e:
            if "size" in str(e).lower() or "552" in str(e):
                # 파일 크기 초과 시 영상 없이 텍스트만 재발송
                logger.warning("파일 크기 초과. 텍스트만 재발송합니다.")
                return self._send_text_only(msg, blog_title, mode_label, video_path)
            logger.error(f"이메일 발송 실패: {e}")
            return {"status": "error", "message": str(e)}
        except Exception as e:
            logger.error(f"이메일 발송 실패: {e}")
            return {"status": "error", "message": str(e)}

    def _send_text_only(
        self,
        original_msg: MIMEMultipart,
        blog_title: str,
        mode_label: str,
        video_path: str,
    ) -> dict:
        """영상이 너무 커서 첨부 불가 시, 텍스트 + 영상 경로만 발송."""
        msg2            = MIMEMultipart("alternative")
        msg2["Subject"] = f"[🎬 숏폼 텍스트] {blog_title} ({mode_label}) ※영상 첨부 생략"
        msg2["From"]    = self.sender
        msg2["To"]      = self.recipient

        notice = (
            f"<p style='color:orange'><strong>⚠️ 영상 파일이 너무 커서 첨부되지 않았습니다.</strong><br>"
            f"파일 경로: <code>{video_path}</code></p>"
        )
        # 원본 HTML에 경고 삽입 (간단히 텍스트 버전)
        alt_text = (
            f"[영상 첨부 생략] 파일이 너무 큽니다.\n"
            f"경로: {video_path}\n\n"
            f"플랫폼별 텍스트는 본문을 확인하세요."
        )
        msg2.attach(MIMEText(alt_text, "plain", "utf-8"))
        try:
            with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
                server.login(self.sender, self.password)
                server.sendmail(self.sender, self.recipient, msg2.as_string())
            return {
                "status": "ok",
                "url": "",
                "message": f"텍스트 전용 이메일 발송 (영상 첨부 생략) → {self.recipient}",
            }
        except Exception as e:
            return {"status": "error", "message": str(e)}


# ─────────────────────────────────────────────────────────────────────────────
# YouTube Shorts
# ─────────────────────────────────────────────────────────────────────────────

class YouTubePublisher(PlatformPublisher):
    """
    필요 환경변수:
      YOUTUBE_CLIENT_ID, YOUTUBE_CLIENT_SECRET,
      YOUTUBE_REFRESH_TOKEN  (최초 1회 OAuth 인증 후 발급)
    """

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
            media   = MediaFileUpload(
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
    """
    필요 환경변수:
      FACEBOOK_PAGE_ID
      FACEBOOK_PAGE_ACCESS_TOKEN  (Meta Graph API, 무료)
    """

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
    필요 환경변수:
      THREADS_USER_ID
      THREADS_ACCESS_TOKEN  (Meta Threads API, 무료)
    """

    GRAPH_API = "https://graph.threads.net/v1.0"

    def __init__(self):
        self.user_id      = os.environ.get("THREADS_USER_ID", "")
        self.access_token = os.environ.get("THREADS_ACCESS_TOKEN", "")

    def publish(self, content: dict, media_paths: dict) -> dict:
        if not self.user_id or not self.access_token:
            return {"status": "skip", "message": "Threads 설정 미완료"}

        text = content.get("threads_post", "")

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
            logger.info(f"Threads 게시 완료: {post_id}")
            return {"status": "ok", "url": "https://www.threads.net",
                    "message": f"게시 성공 (ID: {post_id})"}

        except Exception as e:
            logger.error(f"Threads 게시 실패: {e}")
            return {"status": "error", "message": str(e)}


# ─────────────────────────────────────────────────────────────────────────────
# X (Twitter) — 유료 API ($100/월~)
# ─────────────────────────────────────────────────────────────────────────────

class XPublisher(PlatformPublisher):
    """
    주의: X API Basic Tier 최소 $100/월 필요.
    X_ENABLED=true 환경변수 설정 시 활성화.
    """

    def __init__(self):
        self.enabled            = os.environ.get("X_ENABLED", "false").lower() == "true"
        self.api_key            = os.environ.get("X_API_KEY", "")
        self.api_secret         = os.environ.get("X_API_SECRET", "")
        self.access_token       = os.environ.get("X_ACCESS_TOKEN", "")
        self.access_token_secret = os.environ.get("X_ACCESS_TOKEN_SECRET", "")

    def publish(self, content: dict, media_paths: dict) -> dict:
        if not self.enabled:
            return {"status": "skip",
                    "message": "X API 비활성 (X_ENABLED=true 설정 필요, 유료 $100/월~)"}

        try:
            import tweepy
            client     = tweepy.Client(
                consumer_key=self.api_key,
                consumer_secret=self.api_secret,
                access_token=self.access_token,
                access_token_secret=self.access_token_secret,
            )
            tweet_text = content.get("x_post", "")[:280]
            response   = client.create_tweet(text=tweet_text)
            tweet_id   = response.data["id"]
            url        = f"https://x.com/i/web/status/{tweet_id}"
            logger.info(f"X 트윗 완료: {url}")
            return {"status": "ok", "url": url, "message": "트윗 성공"}
        except ImportError:
            return {"status": "skip", "message": "tweepy 패키지 미설치"}
        except Exception as e:
            logger.error(f"X 트윗 실패: {e}")
            return {"status": "error", "message": str(e)}


# ─────────────────────────────────────────────────────────────────────────────
# 카카오 스토리채널 — 이메일 발송 대체
# ─────────────────────────────────────────────────────────────────────────────

class KakaoStoryPublisher(PlatformPublisher):
    """
    카카오 스토리채널은 공식 자동화 API 없음. Gmail로 원고 발송 → 수동 업로드.
    필요 환경변수: GMAIL_ADDRESS, GMAIL_APP_PASSWORD, RECIPIENT_EMAIL
    """

    def __init__(self):
        self.gmail     = os.environ.get("GMAIL_ADDRESS", "")
        self.password  = os.environ.get("GMAIL_APP_PASSWORD", "")
        self.recipient = os.environ.get("RECIPIENT_EMAIL", "")

    def publish(self, content: dict, media_paths: dict) -> dict:
        if not self.gmail or not self.recipient:
            return {"status": "skip", "message": "Gmail 설정 미완료"}

        kakao_text  = content.get("kakao_post", "")
        blog_url    = content.get("blog_url", "")
        blog_title  = content.get("blog_title", "")
        thumb_path  = media_paths.get("kakao") or media_paths.get("facebook")

        subject = f"[카카오 스토리채널 원고] {blog_title}"
        html    = f"""<html><body style="font-family:sans-serif;line-height:1.8;max-width:600px;margin:0 auto;">
<h2 style="color:#FAE100">📣 카카오 스토리채널 업로드 원고</h2>
<div style="background:#fff9c4;padding:20px;border-radius:8px;white-space:pre-wrap;">{kakao_post if (kakao_post := kakao_text) else "(없음)"}</div>
<p style="margin-top:20px"><strong>블로그 링크:</strong> <a href="{blog_url}">{blog_url}</a></p>
<p style="color:#888;font-size:12px">※ 카카오 스토리채널에 수동으로 업로드해 주세요.</p>
</body></html>"""

        try:
            msg             = MIMEMultipart("related")
            msg["Subject"]  = subject
            msg["From"]     = self.gmail
            msg["To"]       = self.recipient

            alt = MIMEMultipart("alternative")
            alt.attach(MIMEText(html, "html", "utf-8"))
            msg.attach(alt)

            if thumb_path and Path(thumb_path).exists():
                from email.mime.image import MIMEImage
                with open(thumb_path, "rb") as f:
                    img = MIMEImage(f.read())
                    img.add_header("Content-Disposition", "attachment",
                                   filename="kakao_thumbnail.jpg")
                msg.attach(img)

            with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
                server.login(self.gmail, self.password)
                server.sendmail(self.gmail, self.recipient, msg.as_string())

            logger.info(f"카카오 스토리채널 원고 이메일 발송: {self.recipient}")
            return {"status": "ok", "url": "",
                    "message": f"원고 이메일 발송 완료 → {self.recipient}"}
        except Exception as e:
            logger.error(f"카카오 이메일 발송 실패: {e}")
            return {"status": "error", "message": str(e)}


# ─────────────────────────────────────────────────────────────────────────────
# Instagram (Meta Graph API)
# ─────────────────────────────────────────────────────────────────────────────

class InstagramPublisher(PlatformPublisher):
    """
    필요 환경변수:
      INSTAGRAM_ACCOUNT_ID, INSTAGRAM_ACCESS_TOKEN
    이미지 업로드는 공개 URL 필요. 로컬 파일 직접 업로드 불가.
    """

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
            return {"status": "skip",
                    "message": "Instagram 설정 미완료 (INSTAGRAM_ACCOUNT_ID, INSTAGRAM_ACCESS_TOKEN 필요)"}

        caption = content.get("instagram_post", content.get("threads_post", ""))

        if self.video_url:
            result = self._publish_reels(caption)
            if result["status"] == "ok":
                return result
            logger.warning(f"Reels 실패, 이미지 피드로 전환: {result['message']}")

        image_url = self._resolve_image_url(content)
        if image_url:
            return self._publish_image(caption, image_url)

        return {"status": "skip",
                "message": "Instagram은 이미지 없이 텍스트만 게시 불가"}

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
# PublisherDispatcher
# ─────────────────────────────────────────────────────────────────────────────

class PublisherDispatcher:
    """
    모든 플랫폼 발행을 순서대로 실행합니다.
    TikTok → VideoEmailPublisher (영상 이메일 발송)로 대체.
    """

    def __init__(self):
        self.publishers: dict[str, PlatformPublisher] = {
            "youtube":     YouTubePublisher(),
            # TikTok 제거 → 영상 이메일 발송으로 대체
            "video_email": VideoEmailPublisher(),
            "facebook":    FacebookPublisher(),
            "instagram":   InstagramPublisher(),
            "threads":     ThreadsPublisher(),
            "x":           XPublisher(),
            "kakao":       KakaoStoryPublisher(),
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
