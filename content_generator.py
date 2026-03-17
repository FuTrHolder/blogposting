"""
블로그 글 생성 모듈 (완전 무료 버전)
Google Gemini 2.5 Flash API를 사용해 3,000자 내외의 한글 블로그 포스팅을 생성합니다.

무료 한도: 하루 1,000회 요청 (블로그 자동화에 충분)
API 키 발급: https://aistudio.google.com/apikey
"""

import json
import logging
import time
import urllib.request
import urllib.error

logger = logging.getLogger(__name__)

# Gemini API 엔드포인트
GEMINI_MODEL = "gemini-2.5-flash-lite"   # 무료 1,000 RPD 모델
GEMINI_API_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    f"{GEMINI_MODEL}:generateContent"
)

SYSTEM_INSTRUCTION = """당신은 미국 주식 시장을 분석하는 전문 블로그 작가입니다.

작성 규칙:
- 전체 글자 수: 2,800~3,200자 (공백 포함)
- 어조: 부드럽고 자연스러운 한국어. 독자와 대화하듯 친근하게 작성
- 형식: 마크다운 사용 (##, ###, **굵게**, > 인용구 등)
- 구조: 반드시 아래 섹션 포함
  1. 서론 (오늘의 시장 분위기 한 문단)
  2. ## 주요 지수 동향
  3. ## 오늘의 핵심 뉴스
  4. ## 섹터별 흐름
  5. ## 투자자 심리 & Fear & Greed 지수
  6. ## 내일을 위한 관전 포인트
  7. 따뜻한 마무리 한 문단
- 숫자와 데이터를 자연스럽게 녹여낼 것
- 초보자도 이해할 수 있도록 쉽게 설명
- 투자 권유가 아닌 정보 제공 목적 유지
- 글 말미에 면책 조항 한 줄 추가

반드시 아래 JSON 형식으로만 응답하세요 (마크다운 코드블록 없이 순수 JSON만):
{
  "title": "SEO 최적화 블로그 제목 (날짜 포함, 60자 이내)",
  "content": "마크다운 형식의 전체 블로그 본문",
  "tags": ["태그1", "태그2", "태그3", "태그4", "태그5"],
  "image_prompt": "Stable Diffusion용 영문 이미지 프롬프트"
}"""


def _build_prompt(date: str, market_data: dict, news_list: list[dict]) -> str:
    """Gemini에게 전달할 프롬프트를 구성합니다."""
    market_text = f"📅 날짜: {date}\n\n[주요 지수]\n"
    for name, data in market_data.items():
        if name == "fear_greed" or not data:
            continue
        market_text += (
            f"- {name}: {data['price']} "
            f"({data['change']}, {data['change_pct']}) {data['direction']}\n"
        )

    fg = market_data.get("fear_greed", {})
    if fg.get("score"):
        market_text += f"\n[Fear & Greed 지수] {fg['score']}/100 ({fg['rating']})\n"

    news_text = "\n[오늘의 주요 뉴스]\n"
    for i, news in enumerate(news_list, 1):
        sentiment = news.get("sentiment", "")
        suffix = f" [{sentiment}]" if sentiment else ""
        news_text += f"{i}. {news['title']}{suffix}\n"
        if news.get("summary"):
            summary = news["summary"][:200].replace("\n", " ")
            news_text += f"   → {summary}\n"

    return (
        f"{market_text}{news_text}\n"
        "위 데이터를 바탕으로 오늘의 미국 증시 분석 블로그 포스팅을 작성하고, "
        "지정된 JSON 형식으로만 응답해주세요."
    )


class ContentGenerator:
    def __init__(self, api_key: str):
        self.api_key = api_key

    def _call_gemini(self, prompt: str, max_retries: int = 3) -> str:
        """Gemini API를 직접 호출합니다 (urllib 사용, 외부 패키지 불필요)."""
        url = f"{GEMINI_API_URL}?key={self.api_key}"
        payload = {
            "system_instruction": {
                "parts": [{"text": SYSTEM_INSTRUCTION}]
            },
            "contents": [
                {"role": "user", "parts": [{"text": prompt}]}
            ],
            "generationConfig": {
                "temperature": 0.75,
                "maxOutputTokens": 4096,
                "responseMimeType": "application/json",
            },
        }
        data = json.dumps(payload).encode("utf-8")

        for attempt in range(1, max_retries + 1):
            try:
                req = urllib.request.Request(
                    url,
                    data=data,
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=60) as resp:
                    result = json.loads(resp.read().decode("utf-8"))

                # 응답 텍스트 추출
                raw = (
                    result["candidates"][0]["content"]["parts"][0]["text"]
                    .strip()
                )
                return raw

            except urllib.error.HTTPError as e:
                body = e.read().decode("utf-8", errors="replace")
                if e.code == 429:
                    wait = 30 * attempt
                    logger.warning(f"Gemini 한도 초과. {wait}초 대기 후 재시도...")
                    time.sleep(wait)
                else:
                    logger.error(f"Gemini API 오류 {e.code}: {body}")
                    raise
            except Exception as e:
                logger.warning(f"Gemini 호출 실패 (시도 {attempt}): {e}")
                if attempt < max_retries:
                    time.sleep(10)
                else:
                    raise

        raise RuntimeError("Gemini API 최대 재시도 초과")

    def generate_post(self, date: str, market_data: dict, news_list: list[dict]) -> dict:
        """Gemini API를 호출해 블로그 포스팅을 생성합니다."""
        prompt = _build_prompt(date, market_data, news_list)
        logger.info(f"Gemini API 호출 중 (모델: {GEMINI_MODEL})...")

        raw = self._call_gemini(prompt)

        # JSON 파싱 (혹시 코드블록이 포함된 경우 제거)
        if "```" in raw:
            parts = raw.split("```")
            for part in parts:
                part = part.strip()
                if part.startswith("json"):
                    part = part[4:].strip()
                try:
                    return json.loads(part)
                except json.JSONDecodeError:
                    continue

        post = json.loads(raw)
        logger.info(f"생성된 글자 수: {len(post.get('content', ''))}자")
        return post
