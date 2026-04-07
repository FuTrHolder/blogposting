"""
content_adapter/adapter.py
Gemini API를 사용해 티스토리 블로그 포스팅을 각 SNS 플랫폼에 맞게 재가공합니다.
"""

import logging
import time
import json
import requests

logger = logging.getLogger(__name__)

GEMINI_MODEL = "gemini-2.5-flash"
GEMINI_API_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    f"{GEMINI_MODEL}:generateContent"
)

SYSTEM_PROMPT = """당신은 SNS 마케팅 전문가입니다.
주어진 블로그 포스팅 내용을 각 플랫폼에 최적화된 형식으로 재가공해주세요.

반드시 아래 JSON 형식으로만 응답하세요 (마크다운 코드블록 없이 순수 JSON만):
{
  "blog_title": "원본 블로그 제목",
  "blog_url": "원본 블로그 URL",
  "mode": "morning 또는 evening",
  "youtube_script": [
    {
      "slide": 1,
      "title": "훅 제목 (15자 이내)",
      "body": "슬라이드 본문 (50자 이내)"
    }
  ],
  "facebook_post": "페이스북 게시글 (이모지 포함, 300자 이내, 해시태그 5개)",
  "instagram_post": "인스타그램 게시글 (이모지 풍부, 200자 이내, 해시태그 10개)",
  "threads_post": "쓰레드 게시글 (간결, 150자 이내, 해시태그 3개)",
  "x_post": "X 트윗 (280자 이내, 핵심만)",
  "kakao_post": "카카오 스토리 게시글 (친근한 어투, 200자 이내)",
  "tiktok_script": [
    {
      "slide": 1,
      "title": "훅 제목",
      "body": "본문"
    }
  ],
  "thumbnail_copy": "썸네일 메인 카피 (8자 이내)\\n서브 카피 (20자 이내)",
  "thumbnail_prompt": "SNS 썸네일용 Stable Diffusion 영문 프롬프트"
}

thumbnail_copy 작성 규칙:
- 첫 줄: 숫자/수치 또는 핵심 훅 키워드 (8자 이내, 한글 기준)
  예: "-2.3% 급락", "나스닥 반등?", "연준 충격"
- 둘째 줄: 클릭 유도 서브 카피 (20자 이내)
  예: "지금 사야 할까?", "오늘 밤 대응 전략은"

youtube_script / tiktok_script 작성 규칙:
- 총 5~7장면
- 1장면: 강렬한 훅 (궁금증 유발)
- 2~마지막-1장면: 핵심 정보 전달
- 마지막 장면: CTA (블로그 방문 유도)
"""


class ContentAdapter:
    def __init__(self, api_key: str):
        self.api_key = api_key

    def _call_gemini(self, prompt: str, max_retries: int = 3) -> dict:
        url = f"{GEMINI_API_URL}?key={self.api_key}"
        payload = {
            "system_instruction": {"parts": [{"text": SYSTEM_PROMPT}]},
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {
                "temperature": 0.75,
                "maxOutputTokens": 4096,
                "responseMimeType": "application/json",
            },
        }

        # 재시도 가능한 HTTP 상태코드 (일시적 서버 장애)
        RETRYABLE_STATUS = {429, 500, 502, 503, 504}

        for attempt in range(1, max_retries + 1):
            try:
                resp = requests.post(url, json=payload, timeout=60)
                resp.raise_for_status()
                data = resp.json()
                raw = data["candidates"][0]["content"]["parts"][0]["text"].strip()

                # JSON 파싱 (코드블록 방어)
                if "```" in raw:
                    for part in raw.split("```"):
                        part = part.strip()
                        if part.startswith("json"):
                            part = part[4:].strip()
                        try:
                            return json.loads(part)
                        except json.JSONDecodeError:
                            continue

                return json.loads(raw)

            except requests.exceptions.HTTPError as e:
                status = resp.status_code
                if status in RETRYABLE_STATUS:
                    # 지수 백오프: 429는 30s 기준, 나머지는 10s 기준
                    base = 30 if status == 429 else 10
                    wait = base * (2 ** (attempt - 1))  # 10s → 20s → 40s
                    if attempt < max_retries:
                        logger.warning(
                            f"Gemini API {status} 오류 (시도 {attempt}/{max_retries}). "
                            f"{wait}초 후 재시도..."
                        )
                        time.sleep(wait)
                    else:
                        logger.error(f"Gemini API {status} 오류: 최대 재시도 초과")
                        raise
                else:
                    # 400, 401, 403 등 재시도 불가 오류는 즉시 실패
                    logger.error(f"Gemini API 오류 ({status}): {e}")
                    raise
            except (requests.exceptions.ConnectionError,
                    requests.exceptions.Timeout) as e:
                wait = 10 * (2 ** (attempt - 1))
                if attempt < max_retries:
                    logger.warning(
                        f"Gemini 네트워크 오류 (시도 {attempt}/{max_retries}). "
                        f"{wait}초 후 재시도... ({e})"
                    )
                    time.sleep(wait)
                else:
                    logger.error(f"Gemini 네트워크 오류: 최대 재시도 초과")
                    raise
            except Exception as e:
                logger.warning(f"Gemini 호출 실패 (시도 {attempt}/{max_retries}): {e}")
                if attempt < max_retries:
                    time.sleep(10)
                else:
                    raise

        raise RuntimeError("Gemini API 최대 재시도 초과")

    def generate_all(self, post: dict) -> dict:
        """
        티스토리 블로그 포스팅을 각 SNS 플랫폼용 콘텐츠로 변환합니다.

        Args:
            post: TistoryCrawler.get_post_as_dict() 반환값
                  (title, url, summary, full_text, tags, mode 등 포함)

        Returns:
            플랫폼별 콘텐츠 딕셔너리
        """
        title     = post.get("title", "")
        url       = post.get("url", "")
        summary   = post.get("summary", "")
        full_text = post.get("full_text", "")
        tags      = post.get("tags", [])
        mode      = post.get("mode", "morning")

        tags_str = ", ".join(tags) if tags else "미국증시, 주식"

        prompt = f"""아래 티스토리 블로그 포스팅을 각 SNS 플랫폼에 맞게 재가공해주세요.

[블로그 정보]
제목: {title}
URL: {url}
모드: {mode} (morning=전일 마감 리뷰 / evening=프리마켓 & 이슈)
태그: {tags_str}

[포스팅 요약]
{summary}

[포스팅 전문 (앞부분)]
{full_text[:2000]}

위 내용을 바탕으로 각 SNS 플랫폼에 최적화된 콘텐츠를 JSON 형식으로 생성해주세요.
blog_title과 blog_url, mode 필드도 반드시 포함해주세요.
"""

        logger.info(f"ContentAdapter: Gemini API 호출 중 (모드: {mode})...")
        result = self._call_gemini(prompt)

        # 필수 필드 보정 (Gemini가 누락할 경우 대비)
        result.setdefault("blog_title", title)
        result.setdefault("blog_url", url)
        result.setdefault("mode", mode)

        # youtube_script가 없으면 tiktok_script로 대체
        if not result.get("youtube_script") and result.get("tiktok_script"):
            result["youtube_script"] = result["tiktok_script"]
        elif not result.get("youtube_script"):
            result["youtube_script"] = [
                {"slide": 1, "title": title[:15], "body": summary[:50]},
                {"slide": 2, "title": "자세한 분석", "body": "블로그에서 확인하세요!"},
            ]

        if not result.get("tiktok_script"):
            result["tiktok_script"] = result.get("youtube_script", [])

        logger.info(
            f"ContentAdapter 완료: "
            f"YouTube {len(result.get('youtube_script', []))}장, "
            f"플랫폼 텍스트 생성됨"
        )
        return result
