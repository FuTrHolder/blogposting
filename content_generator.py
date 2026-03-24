"""
블로그 글 생성 모듈 (완전 무료 버전)
Google Gemini 2.5 Flash API를 사용해 3,000자 내외의 한글 블로그 포스팅을 생성합니다.
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

────────────────────────────────────────
제목 작성 전략 (매우 중요)
────────────────────────────────────────
제목은 아래 4가지 요소를 모두 충족해야 합니다.

① 날짜 포함 (SEO 필수)
   예: "2026년 3월 19일"

② 핵심 키워드 포함
   미국증시, S&P500, 나스닥, 다우존스, 주가, 증시 중 가장 적합한 것 선택

③ 후킹 요소 — 아래 패턴 중 하나를 반드시 사용
   [숫자/수치 활용]
     - "나스닥 3% 폭락, 지금 팔아야 할까?"
     - "S&P500 5,000선 돌파, 다음 목표는?"
   [반전/의외성]
     - "악재 속에서도 오른 종목들"
     - "공포 지수 급등, 오히려 기회?"
   [긴급성/희소성]
     - "오늘 반드시 알아야 할 미국 증시 변수"
     - "지금 당장 확인해야 할 핵심 지표"
   [궁금증 유발]
     - "연준 발언 이후 시장이 간 곳은?"
     - "빅테크가 흔들리는 진짜 이유"
   [공감/감성]
     - "투자자들이 밤새 불안했던 이유"
     - "개인 투자자들이 놓친 오늘의 기회"

④ 60자 이내, 자연스러운 구어체

제목 예시 (Good):
  ✅ "2026년 3월 19일 미국 증시: 나스닥 2% 급락, 공포 속에서도 담아야 할 종목은?"
  ✅ "2026년 3월 19일 미국 증시 마감: 연준 쇼크에 투자자들이 선택한 것"
  ✅ "오늘 미국 증시 총정리 (3/19): S&P500 반등 신호, 믿어도 될까?"

제목 예시 (Bad):
  ❌ "2026년 3월 19일 미국 증시 분석" (후킹 없음, 단순 나열)
  ❌ "미국 주식 시장 동향 및 주요 지수 현황 분석 리포트" (너무 딱딱함)
  ❌ "오늘의 증시 업데이트" (구체성 없음)
────────────────────────────────────────

반드시 아래 JSON 형식으로만 응답하세요 (마크다운 코드블록 없이 순수 JSON만):
{
  "title": "후킹 요소가 포함된 SEO 최적화 제목 (날짜 포함, 60자 이내)",
  "content": "마크다운 형식의 전체 블로그 본문",
  "tags": ["태그1", "태그2", "태그3", "태그4", "태그5"],
  "image_prompt": "Stable Diffusion용 영문 이미지 프롬프트"
}"""


def _build_prompt(date: str, market_data: dict, news_list: list[dict]) -> str:
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

    # 시장 방향 힌트 추가 (제목 생성 도움)
    directions = [
        v["direction"] for k, v in market_data.items()
        if k != "fear_greed" and v and "direction" in v
    ]
    up_count = sum(1 for d in directions if "상승" in d)
    down_count = sum(1 for d in directions if "하락" in d)
    if up_count > down_count:
        market_hint = "오늘 시장은 전반적으로 상승 마감했습니다."
    elif down_count > up_count:
        market_hint = "오늘 시장은 전반적으로 하락 마감했습니다."
    else:
        market_hint = "오늘 시장은 혼조세로 마감했습니다."

    return (
        f"{market_text}{news_text}\n"
        f"[시장 요약] {market_hint}\n\n"
        "위 데이터를 바탕으로 오늘의 미국 증시 분석 블로그 포스팅을 작성하고, "
        "지정된 JSON 형식으로만 응답해주세요.\n"
        "특히 제목은 반드시 후킹 요소(숫자, 반전, 긴급성, 궁금증 유발 등)를 포함해 "
        "독자가 클릭하고 싶게 만들어 주세요."
    )


class ContentGenerator:
    def __init__(self, api_key: str):
        self.api_key = api_key

    def _call_gemini(self, prompt: str, max_retries: int = 3) -> str:
        url = f"{GEMINI_API_URL}?key={self.api_key}"
        payload = {
            "system_instruction": {
                "parts": [{"text": SYSTEM_INSTRUCTION}]
            },
            "contents": [
                {"role": "user", "parts": [{"text": prompt}]}
            ],
            "generationConfig": {
                "temperature": 0.85,   # 제목 창의성을 위해 약간 높임
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
        prompt = _build_prompt(date, market_data, news_list)
        logger.info(f"Gemini API 호출 중 (모델: {GEMINI_MODEL})...")

        raw = self._call_gemini(prompt)

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
        logger.info(f"생성된 제목: {post.get('title', '')}")
        return post
