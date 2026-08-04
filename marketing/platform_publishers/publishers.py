"""
플랫폼 발행 모듈 v6
변경사항 v6:
  - [버그 수정] Threads 썸네일 미업로드 문제 해결
      기존(v4)에는 Threads가 쓸 수 있는 이미지 URL이 "티스토리 썸네일(대부분
      다음 CDN → 접근 차단됨)" 아니면 수동 환경변수뿐이라, 실제로는 거의
      항상 텍스트 전용으로 전환되고 있었습니다. SNSThumbnailGenerator가
      Threads 전용으로 만들어둔 고품질 썸네일은 로컬 파일이라 애초에 API가
      쓸 수 없었습니다 (media_paths가 ThreadsPublisher.publish()에 전혀
      쓰이지 않고 있었음).
      → main_marketing.py가 발행 "직전"에 그 로컬 썸네일을 GitHub Release로
        업로드해 공개 URL(content["threads_thumbnail_url"])로 만들고,
        ThreadsPublisher._resolve_image_url()이 이를 최우선으로 사용하도록
        수정. Instagram의 이미지 폴백 로직에도 동일 패턴 적용.

  - [기능 추가] 숏폼 영상을 Facebook·Instagram·Threads에 '릴스(영상)'로
    추가 발행 (기존 썸네일+캡션 발행은 그대로 유지 — 추가 발행이지 대체 아님)
      - FacebookPublisher.publish_reels()  : Reels Publishing API
        (video_reels: start → 호스팅 URL 업로드 → finish)
      - InstagramPublisher.publish_reels() : media_type=REELS + video_url
        (기존 로직을 환경변수 고정 URL 방식에서 동적 인자 방식으로 리팩터링)
      - ThreadsPublisher.publish_reels()   : media_type=VIDEO + video_url
    Meta Graph API/Threads API에는 "한 번의 호출로 3개 플랫폼에 동시 게시"
    하는 기능이 없어(모바일 앱의 '함께 공유하기'는 앱 내부 기능일 뿐 공개
    API로 노출되지 않음), 각 플랫폼별로 독립 호출합니다.
      → PublisherDispatcher.publish_all()이 content["video_public_url"]
        (main_marketing.py가 영상을 GitHub Release에 업로드해 확보) 이
        있을 때만 위 3개 플랫폼에 대해 추가로 publish_reels()를 호출하고,
        결과는 "facebook_reels"/"instagram_reels"/"threads_reels" 키로
        기존 결과와 별도로 저장합니다.
"""

import os
import time
import logging
import requests
import json
from abc import ABC, abstractmethod
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

        title    = content["blog_title"][:100]
        blog_url = content.get("blog_url", "")

        # 설명란: kakao_post(친근한 어투 텍스트)를 재사용
        # ([블로그 URL] 플레이스홀더를 실제 URL로 치환)
        description_text = content.get("kakao_post", "")
        if blog_url:
            description_text = description_text.replace("[블로그 URL]", blog_url)
            description_text = description_text.replace("[Blog URL]", blog_url)

        description = (
            f"{description_text}\n\n"
            f"🔗 전체 분석: {blog_url}\n\n"
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
            err_str = str(e)
            if "invalid_grant" in err_str:
                diag = (
                    "리프레시 토큰이 거부되었습니다(invalid_grant). 주로 다음 중 하나가 원인입니다: "
                    "① OAuth 동의 화면이 '테스트' 상태 → 리프레시 토큰이 7일 후 자동 만료됨 "
                    "(Google Cloud Console에서 동의 화면을 '프로덕션'으로 게시하거나, "
                    "OAuth Playground로 토큰을 재발급해 YOUTUBE_REFRESH_TOKEN을 갱신하세요). "
                    "② GitHub Secret에 저장된 토큰 값에 공백/개행이 섞였을 가능성. "
                    "③ Google 계정 보안 설정에서 앱 접근 권한이 취소되었을 가능성."
                )
                logger.error(f"YouTube 업로드 실패 (invalid_grant): {diag}")
                return {"status": "error", "message": diag}
            logger.error(f"YouTube 업로드 실패: {e}")
            return {"status": "error", "message": err_str}


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

    def publish_reels(self, content: dict, video_url: str) -> dict:
        """
        숏폼 영상을 Facebook 페이지에 '릴스'로 발행합니다 (기존 photos/feed 발행과는
        별개의 추가 발행 — 썸네일 게시물은 그대로 유지됩니다).

        Facebook Reels Publishing API 3단계:
          1) POST /{page_id}/video_reels?upload_phase=start        → video_id 발급
          2) POST https://rupload.facebook.com/video-upload/{video_id}
             헤더 file_url=<공개 영상 URL> (파일 바이너리 대신 호스팅 URL 방식)
          3) POST /{page_id}/video_reels?upload_phase=finish&video_state=PUBLISHED

        영상 처리(인코딩)에 다소 시간이 걸릴 수 있어, finish 호출이 "아직 처리
        중"이라는 오류를 반환하면 짧게 대기 후 최대 5회까지 재시도합니다
        (Meta 공식 문서상 상태 확인은 선택 단계이므로, 별도 폴링 대신 발행
        재시도 방식으로 처리 — 존재 여부가 불확실한 상태 필드명에 의존하지 않음).

        영상 요건(Meta 공식): 9:16, 해상도 540x960 이상, 4~60초, mp4/mov.
        이 채널의 숏폼 생성기는 1080x1920 · 58초 이하로 제작되어 요건을 충족합니다.
        """
        if not self.page_id or not self.access_token:
            return {"status": "skip", "message": "Facebook 설정 미완료"}
        if not video_url:
            return {"status": "skip", "message": "공개 영상 URL 없음(GitHub Release 업로드 실패 가능성)"}

        caption  = content.get("facebook_post", "") or content.get("blog_title", "")
        blog_url = content.get("blog_url", "")
        if blog_url:
            caption = caption.replace("[블로그 URL]", blog_url)
        title = content.get("blog_title", "")[:100]

        try:
            # 1) 업로드 세션 시작
            resp1 = requests.post(
                f"{self.GRAPH_API}/{self.page_id}/video_reels",
                params={"access_token": self.access_token, "upload_phase": "start"},
                timeout=30,
            )
            data1 = resp1.json()
            if "error" in data1:
                return {"status": "error",
                        "message": f"업로드 세션 시작 실패: {data1['error'].get('message', str(data1))}"}
            video_id = data1.get("video_id")
            if not video_id:
                return {"status": "error", "message": f"video_id 없음: {data1}"}

            # 2) 호스팅된 영상 URL 업로드 (파일 바이너리 대신 file_url 헤더 사용
            #    → 로컬 mp4를 그대로 서버로 전송할 필요 없이, 이미 공개 URL로
            #    올려둔 영상을 Facebook이 직접 가져가게 함)
            upload_resp = requests.post(
                f"https://rupload.facebook.com/video-upload/v20.0/{video_id}",
                headers={
                    "Authorization": f"OAuth {self.access_token}",
                    "file_url": video_url,
                },
                timeout=120,
            )
            if upload_resp.status_code != 200:
                return {"status": "error",
                        "message": f"영상 업로드 실패 ({upload_resp.status_code}): {upload_resp.text[:200]}"}

            # 3) 발행 (처리 중이면 재시도)
            last_err = ""
            for attempt in range(1, 6):
                resp2 = requests.post(
                    f"{self.GRAPH_API}/{self.page_id}/video_reels",
                    params={
                        "access_token": self.access_token,
                        "video_id": video_id,
                        "upload_phase": "finish",
                        "video_state": "PUBLISHED",
                        "description": caption[:2000],
                        "title": title,
                    },
                    timeout=30,
                )
                data2 = resp2.json()
                if "error" not in data2:
                    url = f"https://www.facebook.com/reel/{video_id}"
                    logger.info(f"Facebook 릴스 게시 완료: {url}")
                    return {"status": "ok", "url": url, "message": "릴스 게시 성공"}

                last_err = data2["error"].get("message", str(data2))
                if attempt < 5:
                    wait = 15 * attempt
                    logger.info(f"Facebook 릴스 처리 대기 중 — {wait}초 후 재시도 ({last_err})")
                    time.sleep(wait)

            return {"status": "error", "message": f"릴스 발행 실패(재시도 초과): {last_err}"}
        except Exception as e:
            logger.error(f"Facebook 릴스 게시 실패: {e}")
            return {"status": "error", "message": str(e)}


# ─────────────────────────────────────────────────────────────────────────────
# Threads
# ─────────────────────────────────────────────────────────────────────────────

class ThreadsPublisher(PlatformPublisher):
    """
    Threads API 이미지/영상 업로드 방식:
    - 로컬 파일 직접 업로드 불가 → 반드시 공개 URL 필요
    - 이미지 URL 우선순위 (v5 — 자동 생성 썸네일 우선):
        1순위: THREADS_IMAGE_URL 환경변수 (수동 지정)
        2순위: content["threads_thumbnail_url"] — SNSThumbnailGenerator가 만든
               Threads 전용 썸네일을 main_marketing.py가 발행 전에 GitHub
               Release로 업로드해 확보한 공개 URL (신규, v5)
        3순위: content["blog_thumbnail_url"] — 티스토리 대표 이미지
               (다음(Daum) CDN 호스팅인 경우가 많아 Threads 서버가 접근 못 함 →
                이 경우 자동으로 건너뜀)
        4순위: INSTAGRAM_IMAGE_URL 환경변수
        5순위: 이미지 없이 텍스트 전용 게시

      기존 v4에서는 2순위가 티스토리 썸네일이었는데, 실제로는 대부분 다음
      CDN URL이라 접근이 막혀 항상 "텍스트 전용"으로 전환되는 문제가 있었습니다.
      v5는 우리가 직접 생성해 GitHub Release(공개, CORS/CDN 제약 없음)에 올린
      Threads 전용 썸네일을 그 자리에 우선 배치해 해결합니다.
    - [블로그 URL] 플레이스홀더를 실제 URL로 자동 치환
    """
    GRAPH_API = "https://graph.threads.net/v1.0"

    def __init__(self):
        self.user_id      = os.environ.get("THREADS_USER_ID", "")
        self.access_token = os.environ.get("THREADS_ACCESS_TOKEN", "")
        # 수동 지정 공개 URL (선택사항 — 없어도 자동 생성 썸네일로 대체됨)
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

        # 2순위 (신규): main_marketing.py가 발행 전 GitHub Release로 업로드해둔
        # Threads 전용 자동 생성 썸네일. Daum CDN 문제 없이 항상 공개 접근 가능.
        auto_url = content.get("threads_thumbnail_url", "")
        if auto_url:
            logger.info(f"Threads 이미지: 자동 생성 썸네일(GitHub Release) 사용 → {auto_url[:70]}...")
            return auto_url

        # 3순위: 티스토리 썸네일
        # → 다음 CDN(daumcdn.net)은 Threads 서버에서 접근 불가하므로 제외
        tistory_thumb = content.get("blog_thumbnail_url", "")
        if tistory_thumb and tistory_thumb.startswith("http"):
            if "daumcdn.net" in tistory_thumb:
                logger.warning(
                    "Threads 이미지: 다음 CDN URL은 Threads 서버 접근 불가 → 건너뜀"
                )
            else:
                logger.info(f"Threads 이미지: 티스토리 썸네일 사용 → {tistory_thumb[:60]}...")
                return tistory_thumb

        # 4순위: Instagram URL 환경변수
        insta_url = os.environ.get("INSTAGRAM_IMAGE_URL", "")
        if insta_url:
            logger.info("Threads 이미지: INSTAGRAM_IMAGE_URL 사용")
            return insta_url

        logger.info("Threads 이미지: 사용 가능한 공개 URL 없음 → 텍스트 전용")
        return ""

    def _wait_for_container(self, creation_id: str, max_wait: int = 90) -> bool:
        """
        영상 컨테이너 처리 완료 대기. Threads는 Instagram과 동일한 컨테이너
        모델을 사용하므로 status 필드를 폴링합니다. 필드명이 문서마다 조금씩
        달라 확신할 수 없는 부분은 안전하게 처리합니다 — 타임아웃돼도 False가
        아닌 True를 반환해 일단 발행을 시도하고, 실제 처리 여부는 이어지는
        threads_publish 재시도 로직(에러 시 재시도)에서 최종 확인됩니다.
        """
        for _ in range(max_wait // 5):
            time.sleep(5)
            try:
                resp = requests.get(
                    f"{self.GRAPH_API}/{creation_id}",
                    params={"fields": "status", "access_token": self.access_token},
                    timeout=10,
                )
                status = resp.json().get("status", "")
                if status == "FINISHED":
                    return True
                if status == "ERROR":
                    return False
            except Exception as e:
                logger.warning(f"Threads 컨테이너 상태 확인 실패(무시하고 진행): {e}")
        return True

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
            # ↓ 추가: 응답 전체를 로그에 출력
            logger.info(f"Threads 컨테이너 응답 (status={resp1.status_code}): {data1}")
          
            if "error" in data1:
                return {"status": "error", "message": data1["error"]["message"]}

            creation_id = data1["id"]
            resp2 = requests.post(
                f"{self.GRAPH_API}/{self.user_id}/threads_publish",
                params={"creation_id": creation_id, "access_token": self.access_token},
                timeout=30,
            )
            data2 = resp2.json()
            # ↓ 추가: 응답 전체를 로그에 출력
            logger.info(f"Threads 발행 응답 (status={resp2.status_code}): {data2}")
          
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

    def publish_reels(self, content: dict, video_url: str) -> dict:
        """
        숏폼 영상을 Threads에 영상 게시물로 발행합니다 (기존 텍스트/이미지
        게시와는 별개의 추가 발행 — 썸네일 게시물은 그대로 유지됩니다).

        Threads API: media_type=VIDEO + video_url (IMAGE와 동일한
        컨테이너 생성 → 발행 2단계 흐름). 지원 규격: MP4/MOV, H264/HEVC,
        23~60fps, 최대 5분, 최대 1GB — 이 채널의 숏폼(1080x1920, 58초
        이하, H.264)은 규격을 충분히 만족합니다.
        """
        if not self.user_id or not self.access_token:
            return {"status": "skip", "message": "Threads 설정 미완료"}
        if not video_url:
            return {"status": "skip", "message": "공개 영상 URL 없음(GitHub Release 업로드 실패 가능성)"}

        blog_url = content.get("blog_url", "")
        text     = self._replace_placeholder(content.get("threads_post", ""), blog_url)

        try:
            resp1 = requests.post(
                f"{self.GRAPH_API}/{self.user_id}/threads",
                params={
                    "access_token": self.access_token,
                    "text": text,
                    "media_type": "VIDEO",
                    "video_url": video_url,
                },
                timeout=30,
            )
            data1 = resp1.json()
            if "error" in data1:
                return {"status": "error", "message": data1["error"].get("message", str(data1))}

            creation_id = data1.get("id")
            if not creation_id:
                return {"status": "error", "message": "creation_id 없음"}

            # 영상은 이미지보다 처리 시간이 길어 컨테이너 상태를 먼저 대기
            self._wait_for_container(creation_id, max_wait=90)

            # 아직 처리 중일 수 있으므로 발행 실패 시 짧게 대기 후 재시도
            last_err = ""
            for attempt in range(1, 6):
                resp2 = requests.post(
                    f"{self.GRAPH_API}/{self.user_id}/threads_publish",
                    params={"creation_id": creation_id, "access_token": self.access_token},
                    timeout=30,
                )
                data2 = resp2.json()
                if "error" not in data2:
                    post_id = data2.get("id", "")
                    logger.info(f"Threads 영상(릴스) 게시 완료: {post_id}")
                    return {"status": "ok", "url": "https://www.threads.net",
                            "message": f"영상 게시 성공 (ID: {post_id})"}

                last_err = data2["error"].get("message", str(data2))
                if attempt < 5:
                    wait = 15 * attempt
                    logger.info(f"Threads 영상 처리 대기 중 — {wait}초 후 재시도 ({last_err})")
                    time.sleep(wait)

            return {"status": "error", "message": f"영상 발행 실패(재시도 초과): {last_err}"}
        except Exception as e:
            logger.error(f"Threads 영상 게시 실패: {e}")
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
        # 수동 지정 공개 영상 URL (선택사항 — 없으면 main_marketing.py가 자동
        # 업로드한 숏폼 영상의 GitHub Release URL을 publish_reels() 인자로 사용)
        self.video_url    = os.environ.get("INSTAGRAM_VIDEO_URL", "")

    def publish(self, content: dict, media_paths: dict) -> dict:
        """
        썸네일(이미지) + 캡션 게시. v6부터는 영상(릴스) 발행이 publish_reels()로
        분리되어, 이 메서드는 항상 이미지 게시물만 담당합니다 (릴스가 성공하든
        실패하든 이 게시물은 그대로 유지 — 썸네일+릴스 둘 다 발행하는 것이 목적).
        """
        if not self.account_id or not self.access_token:
            return {"status": "skip", "message": "Instagram 설정 미완료"}

        caption   = content.get("instagram_post", content.get("threads_post", ""))
        blog_url  = content.get("blog_url", "")
        if blog_url:
            caption = caption.replace("[블로그 URL]", blog_url)

        image_url = self._resolve_image_url(content)
        if image_url:
            return self._publish_image(caption, image_url)

        return {"status": "skip", "message": "Instagram은 이미지 없이 텍스트만 게시 불가"}

    def _resolve_image_url(self, content: dict) -> str:
        # 1순위: 환경변수 수동 지정
        env_url = os.environ.get("INSTAGRAM_IMAGE_URL", "")
        if env_url:
            return env_url

        # 2순위 (신규): main_marketing.py가 발행 전 GitHub Release로 업로드해둔
        # Instagram 전용 자동 생성 썸네일
        auto_url = content.get("instagram_thumbnail_url", "")
        if auto_url:
            return auto_url

        # 3순위: 티스토리 썸네일 (다음 CDN이라도 Instagram Graph API는 대체로
        # 접근 가능하지만, 실패 시 위 두 우선순위로 미리 대체되도록 함)
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

    def publish_reels(self, content: dict, video_url: str) -> dict:
        """
        숏폼 영상을 Instagram에 '릴스'로 발행합니다 (publish()의 썸네일 게시와는
        별개의 추가 발행 — 썸네일 게시물은 그대로 유지됩니다).

        video_url: main_marketing.py가 로컬 mp4를 GitHub Release에 올려 확보한
        공개 URL. INSTAGRAM_VIDEO_URL 환경변수가 설정돼 있으면 그 값을 우선
        사용합니다(수동 오버라이드 — 예: 특정 영상만 지정하고 싶을 때).
        """
        if not self.account_id or not self.access_token:
            return {"status": "skip", "message": "Instagram 설정 미완료"}

        final_video_url = self.video_url or video_url
        if not final_video_url:
            return {"status": "skip", "message": "공개 영상 URL 없음(GitHub Release 업로드 실패 가능성)"}

        caption  = content.get("instagram_post", content.get("threads_post", ""))
        blog_url = content.get("blog_url", "")
        if blog_url:
            caption = caption.replace("[블로그 URL]", blog_url)

        return self._publish_reels_video(caption, final_video_url)

    def _publish_reels_video(self, caption: str, video_url: str) -> dict:
        try:
            resp1 = requests.post(
                f"{self.GRAPH_API}/{self.account_id}/media",
                params={
                    "media_type": "REELS",
                    "video_url": video_url,
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
    # 썸네일 발행(publish)과 별개로 릴스(영상) 추가 발행(publish_reels)을
    # 지원하는 플랫폼. YouTube는 이미 영상 자체가 본 발행물입니다.
    REELS_CAPABLE_PLATFORMS = ("facebook", "instagram", "threads")

    def __init__(self):
        self.publishers: dict[str, PlatformPublisher] = {
            "youtube":   YouTubePublisher(),
            "facebook":  FacebookPublisher(),
            "instagram": InstagramPublisher(),
            "threads":   ThreadsPublisher(),
        }

    def publish_all(self, content: dict, media_paths: dict) -> dict[str, dict]:
        results: dict[str, dict] = {}

        # 1) 기존 발행 (썸네일/텍스트 — 플랫폼별 1건씩)
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

        # 2) 릴스(영상) 추가 발행 — 공개 영상 URL이 확보된 경우에만 진행.
        #    main_marketing.py가 발행 전에 content["video_public_url"]에
        #    GitHub Release 공개 URL을 채워 넣습니다. 없으면(업로드 실패 등)
        #    조용히 건너뛰고 1)의 썸네일 발행 결과만 남습니다.
        video_url = content.get("video_public_url", "")
        if not video_url:
            logger.info("영상 공개 URL 없음 — 릴스 추가 발행 건너뜀(썸네일 발행 결과만 사용)")
            return results

        for name in self.REELS_CAPABLE_PLATFORMS:
            publisher = self.publishers.get(name)
            if publisher is None or not hasattr(publisher, "publish_reels"):
                continue
            key = f"{name}_reels"
            logger.info(f"[{key.upper()}] 릴스 발행 시도 중...")
            try:
                result       = publisher.publish_reels(content, video_url)
                results[key] = result
                status       = result.get("status", "?")
                msg          = result.get("message", "")
                logger.info(f"[{key.upper()}] {status}: {msg}")
            except Exception as e:
                results[key] = {"status": "error", "message": str(e)}
                logger.error(f"[{key.upper()}] 예외 발생: {e}")
            time.sleep(1)

        return results
