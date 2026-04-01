"""
멀티플랫폼 콘텐츠 어댑터
Gemini를 사용해 블로그 원문에서 각 플랫폼 전용 콘텐츠를 생성합니다.

출력:
  - youtube_script  : Shorts 자막 슬라이드 (6~8장면, JSON)
  - tiktok_script   : TikTok 쇼츠 스크립트 (동일 포맷 재활용)
  - facebook_post   : 페이지 게시물 (200자 + 해시태그)
  - threads_post    : Threads 게시물 (150자 이내 + 해시태그)
  - instagram_post  : Instagram 피드 캡션 (220자 이내 + 해시태그 최대 30개)
  - x_post          : X 트윗 (140자 이내 + 해시태그)
  - kakao_post      : 카카오 스토리채널용 (이메일 수동 업로드용)
  - thumbnail_prompt: SD/HF용 이미지 프롬프트
"""

import json
import logging
import time
import urllib.request
import urllib.error

logger = logging.getLogger(__name__)

GEMINI_MODEL = "gemini-2.5-flash-lite"
GEMINI_API_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    f"{GEMINI_MODEL}:generateContent"
)

SYSTEM_PROMPT = """당신은 금융 블로그 콘텐츠를 SNS 마케팅용으로 변환하는 전문가입니다.
미국 증시 블로그 포스팅을 읽고, 각 플랫폼에 최적화된 콘텐츠를 생성하세요.

공통 규칙:
- 핵심 정보만 추려 간결하게
- 투자 권유 아닌 정보 제공 목적 명시
- 각 플랫폼 특성에 맞는 톤과 길이
- 해시태그는 한글+영문 혼합으로 클릭률 높게

YouTube Shorts / TikTok 스크립트 규칙:
- 총 6~8개 슬라이드 장면
- 각 장면: 제목(10자 이내) + 본문(40자 이내) + 화면 설명(narrator용)
- 첫 장면: 후킹 (오늘 미국 증시 한 줄 요약)
- 마지막 장면: CTA (블로그 링크 클릭 유도)
- 총 재생 시간 목표: 30~45초

Instagram 게시물 규칙:
- 캡션 220자 이내 (본문) + 해시태그 구역 분리 (빈 줄 후 해시태그)
- 해시태그: 한글 10개 + 영문 10개 혼합, 최대 20개 (도달률 최적화)
- 첫 줄이 핵심 후킹 문장 (더보기 펼치기 전 노출)
- 이모지 적극 활용, 친근하고 정보성 톤

반드시 아래 JSON 형식으로만 응답 (마크다운 코드블록 없이):
{
  "youtube_script": [
    {"slide": 1, "title": "제목", "body": "본문", "visual": "화면 설명"},
    ...
  ],
  "facebook_post": "페이스북 게시물 (200자 이내, 해시태그 포함)",
  "threads_post": "Threads 게시물 (150자 이내, 해시태그 포함)",
  "instagram_post": "인스타그램 캡션 (220자 이내 본문 + 빈줄 + 해시태그 20개)",
  "x_post": "X 트윗 (140자 이내, 해시태그 2~3개)",
  "kakao_post": "카카오 스토리채널 게시물 (250자 이내, 이모지 활용, 해시태그)",
  "thumbnail_prompt": "SNS 썸네일용 Stable Diffusion 영문 프롬프트"
}"""


class ContentAdapter:
    def __init__(self, api_key: str):
        self.api_key = api_key

    def generate_all(self, post: dict) -> dict:
        """블로그 포스트 dict → 플랫폼별 콘텐츠 dict 반환."""
        prompt = self._build_prompt(post)
        raw = self._call_gemini(prompt)
        result = self._parse_json(raw)

        # TikTok은 YouTube Shorts와 동일 스크립트 재활용
        result["tiktok_script"] = result.get("youtube_script", [])
        # Instagram Reels도 동일 영상 스크립트 재활용
        result["instagram_script"] = result.get("youtube_script", [])
        result["blog_url"] = post.get("url", "")
        result["blog_title"] = post.get("title", "")
        result["mode"] = post.get("mode", "morning")

        logger.info(f"콘텐츠 어댑터 완료: {len(result.get('youtube_script', []))}개 슬라이드")
        return result

    def _build_prompt(self, post: dict) -> str:
        return (
            f"블로그 제목: {post['title']}\n"
            f"URL: {post['url']}\n"
            f"모드: {post['mode']} ({'전일 마감 리뷰' if post['mode'] == 'morning' else '프리마켓 & 이슈'})\n"
            f"태그: {', '.join(post.get('tags', []))}\n\n"
            f"본문 요약:\n{post['summary']}\n\n"
            f"본문 전체 (일부):\n{post['full_text'][:2000]}\n\n"
            "위 블로그 포스팅을 각 플랫폼에 맞게 변환해주세요. "
            "JSON 형식으로만 응답하세요."
        )

    def _call_gemini(self, prompt: str, max_retries: int = 3) -> str:
        url = f"{GEMINI_API_URL}?key={self.api_key}"
        payload = {
            "system_instruction": {"parts": [{"text": SYSTEM_PROMPT}]},
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {
                "temperature": 0.75,
                "maxOutputTokens": 2048,
                "responseMimeType": "application/json",
            },
        }
        data = json.dumps(payload).encode("utf-8")

        for attempt in range(1, max_retries + 1):
            try:
                req = urllib.request.Request(
                    url, data=data,
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=60) as resp:
                    result = json.loads(resp.read().decode("utf-8"))
                return result["candidates"][0]["content"]["parts"][0]["text"].strip()
            except urllib.error.HTTPError as e:
                if e.code == 429:
                    wait = 30 * attempt
                    logger.warning(f"Gemini 한도 초과. {wait}초 대기...")
                    time.sleep(wait)
                else:
                    raise
            except Exception as e:
                logger.warning(f"Gemini 호출 실패 (시도 {attempt}): {e}")
                if attempt < max_retries:
                    time.sleep(10)
                else:
                    raise

        raise RuntimeError("Gemini API 최대 재시도 초과")

    def _parse_json(self, raw: str) -> dict:
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
