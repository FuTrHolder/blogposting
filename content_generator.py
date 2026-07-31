"""
블로그 글 생성 모듈
Google Gemini 2.5 Flash API를 사용해 3,000자 내외의 한글 블로그 포스팅을 생성합니다.

모드:
  morning : 미국 전일 증시 마감 리뷰 (오전 9시 포스팅)
  evening : 당일 프리마켓 이슈 + 한국 시간 당일 주요 이슈 정리 (저녁 9시 포스팅)
"""

import html as _html_module
import json
import logging
import re
import time
import urllib.request
import urllib.error


def _clean_post_text(text: str) -> str:
    """
    Gemini JSON 파싱 직후 HTML 엔티티를 실제 문자로 변환합니다.
    &middot; → · / &amp; → & / &ndash; → – / &nbsp; → 공백 등
    제목(title)과 본문(content) 모두에 적용합니다.
    """
    text = _html_module.unescape(text)
    text = re.sub(r"&#(\d+);", lambda m: chr(int(m.group(1))), text)
    text = re.sub(r"&#x([0-9a-fA-F]+);", lambda m: chr(int(m.group(1), 16)), text)
    return text

logger = logging.getLogger(__name__)

GEMINI_MODEL = "gemini-2.5-flash-lite"
GEMINI_API_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    f"{GEMINI_MODEL}:generateContent"
)

# ── 공통 제목 전략 (두 모드 공유) ────────────────────────────────────────────
_TITLE_STRATEGY = """
────────────────────────────────────────
제목 작성 전략 (매우 중요)
────────────────────────────────────────
제목은 아래 4가지 요소를 모두 충족해야 합니다.

① 날짜 포함 (SEO 필수)  예: "2026년 3월 19일"
② 핵심 키워드 포함: 미국증시, S&P500, 나스닥, 다우존스, 주가, 증시 중 적합한 것 선택
③ 후킹 요소 — 아래 패턴 중 하나 반드시 사용
   [숫자/수치]  "나스닥 3% 폭락, 지금 팔아야 할까?"
   [반전/의외성]  "악재 속에서도 오른 종목들"
   [긴급성]  "오늘 반드시 알아야 할 미국 증시 변수"
   [궁금증 유발]  "연준 발언 이후 시장이 간 곳은?"
   [공감/감성]  "투자자들이 밤새 불안했던 이유"
④ 60자 이내, 자연스러운 구어체

Good 예시:
  ✅ "2026년 3월 19일 미국 증시: 나스닥 2% 급락, 공포 속에서도 담아야 할 종목은?"
  ✅ "오늘 미국 증시 총정리 (3/19): S&P500 반등 신호, 믿어도 될까?"
Bad 예시:
  ❌ "2026년 3월 19일 미국 증시 분석"  (후킹 없음)
  ❌ "오늘의 증시 업데이트"  (구체성 없음)
────────────────────────────────────────
"""

# ── 오전 시스템 프롬프트 (전일 마감 리뷰) ────────────────────────────────────
SYSTEM_MORNING = (
    """당신은 미국 주식 시장을 분석하는 전문 블로그 작가입니다.
이 포스팅은 **오전 9시 발행** 원고로, 미국 전일 증시 마감을 리뷰하는 내용입니다.

작성 규칙:
- 전체 글자 수: 2,800~3,200자 (공백 포함)
- 어조: 부드럽고 자연스러운 한국어. 독자와 대화하듯 친근하게 작성
- 형식: 마크다운 사용 (##, ###, **굵게**, > 인용구 등)
- 구조: 반드시 아래 섹션 포함
  1. 서론: 어제 미국 증시를 한 줄로 요약하는 분위기 묘사
  2. ## 주요 지수 마감 결과 (S&P500·나스닥·다우 수치 포함)
  3. ## 어제의 핵심 뉴스 & 시장 반응
  4. ## 섹터별 마감 흐름
  5. ## 투자자 심리 & Fear & Greed 지수
  6. ## 오늘(미국 시간) 주목해야 할 포인트
  7. 따뜻한 마무리 한 문단
- 숫자·데이터를 자연스럽게 녹여낼 것
- 초보자도 이해할 수 있도록 쉽게 설명
- 투자 권유가 아닌 정보 제공 목적 유지
- 글 말미에 면책 조항 한 줄 추가
"""
    + _TITLE_STRATEGY
    + """
반드시 아래 JSON 형식으로만 응답하세요 (마크다운 코드블록 없이 순수 JSON만):
{
  "title": "후킹 요소가 포함된 SEO 최적화 제목 (날짜 포함, 60자 이내)",
  "content": "마크다운 형식의 전체 블로그 본문",
  "tags": ["태그1", "태그2", "태그3", "태그4", "태그5"],
  "image_prompt": "Stable Diffusion용 영문 이미지 프롬프트 (마감 후 조용한 월스트리트 분위기)"
}"""
)

# ── 저녁 시스템 프롬프트 (당일 프리마켓 & 이슈) ──────────────────────────────
SYSTEM_EVENING = (
    """당신은 미국 주식 시장을 분석하는 전문 블로그 작가입니다.
이 포스팅은 **저녁 9시 발행** 원고로, 두 가지 내용을 담습니다.
  ① 미국 당일 프리마켓 & 오픈 전 주요 이슈 (지표 발표 예정, 실적 발표 등)
  ② 한국 시간 기준 오늘 아침부터 저녁 9시까지 발생한 증시 관련 이슈 정리

작성 규칙:
- 전체 글자 수: 2,800~3,200자 (공백 포함)
- 어조: 긴장감 있고 실용적인 톤. 오늘 밤~내일 새벽 시장을 앞둔 투자자 관점으로 작성
- 형식: 마크다운 사용 (##, ###, **굵게**, > 인용구 등)
- 구조: 반드시 아래 섹션 포함
  1. 서론: 오늘 밤 미국 시장 개장을 앞둔 분위기 한 문단
  2. ## 오늘 밤 주목할 경제 지표 & 이벤트 (발표 시간 포함)
  3. ## 실적 발표 예정 기업 & 기대치
  4. ## 오늘 하루 주요 글로벌 이슈 정리 (한국·아시아·유럽 시장 동향 포함)
  5. ## 프리마켓 동향 & 선물 지수
  6. ## 오늘 밤 시나리오: 강세 vs 약세
  7. 마무리: 오늘 밤 대응 전략 한 문단
- 숫자·데이터를 자연스럽게 녹여낼 것
- 초보자도 이해할 수 있도록 쉽게 설명
- 투자 권유가 아닌 정보 제공 목적 유지
- 글 말미에 면책 조항 한 줄 추가
"""
    + _TITLE_STRATEGY
    + """
반드시 아래 JSON 형식으로만 응답하세요 (마크다운 코드블록 없이 순수 JSON만):
{
  "title": "후킹 요소가 포함된 SEO 최적화 제목 (날짜 포함, 60자 이내)",
  "content": "마크다운 형식의 전체 블로그 본문",
  "tags": ["태그1", "태그2", "태그3", "태그4", "태그5"],
  "image_prompt": "Stable Diffusion용 영문 이미지 프롬프트 (활기찬 프리마켓 트레이딩 분위기)"
}"""
)


def _build_morning_prompt(date: str, market_data: dict, news_list: list[dict]) -> str:
    """오전 포스팅용 프롬프트: 전일 마감 데이터 중심."""
    market_text = f"📅 미국 증시 마감 날짜: {date}\n\n[전일 마감 지수]\n"
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

    news_text = "\n[전일 주요 뉴스]\n"
    for i, news in enumerate(news_list, 1):
        sentiment = news.get("sentiment", "")
        suffix = f" [{sentiment}]" if sentiment else ""
        news_text += f"{i}. {news['title']}{suffix}\n"
        if news.get("summary"):
            news_text += f"   → {news['summary'][:200].replace(chr(10), ' ')}\n"

    directions = [
        v["direction"] for k, v in market_data.items()
        if k != "fear_greed" and v and "direction" in v
    ]
    up_count = sum(1 for d in directions if "상승" in d)
    down_count = sum(1 for d in directions if "하락" in d)
    if up_count > down_count:
        hint = "전일 미국 시장은 전반적으로 상승 마감했습니다."
    elif down_count > up_count:
        hint = "전일 미국 시장은 전반적으로 하락 마감했습니다."
    else:
        hint = "전일 미국 시장은 혼조세로 마감했습니다."

    return (
        f"{market_text}{news_text}\n"
        f"[시장 요약] {hint}\n\n"
        "위 데이터를 바탕으로 전일 미국 증시 마감 리뷰 블로그 포스팅을 작성하고, "
        "지정된 JSON 형식으로만 응답해주세요.\n"
        "제목은 반드시 전날 마감 결과(지수 수치·등락 방향 등)를 구체적으로 담아 "
        "독자가 클릭하고 싶게 만들어 주세요."
    )


def _build_evening_prompt(date: str, market_data: dict, news_list: list[dict]) -> str:
    """저녁 포스팅용 프롬프트: 프리마켓 & 당일 이슈 중심."""
    market_text = f"📅 기준 날짜: {date} (한국 시간)\n\n[현재 선물/프리마켓 지수]\n"
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

    news_text = "\n[오늘 수집된 주요 뉴스 & 이슈]\n"
    for i, news in enumerate(news_list, 1):
        sentiment = news.get("sentiment", "")
        suffix = f" [{sentiment}]" if sentiment else ""
        news_text += f"{i}. {news['title']}{suffix}\n"
        if news.get("summary"):
            news_text += f"   → {news['summary'][:200].replace(chr(10), ' ')}\n"

    return (
        f"{market_text}{news_text}\n"
        "위 데이터를 바탕으로 오늘 밤 미국 증시 개장 전 프리뷰 블로그 포스팅을 작성하고, "
        "지정된 JSON 형식으로만 응답해주세요.\n"
        "제목은 오늘 밤 시장에서 주목해야 할 핵심 이슈를 구체적으로 담아 "
        "독자가 클릭하고 싶게 만들어 주세요."
    )


class ContentGenerator:
    def __init__(self, api_key: str):
        self.api_key = api_key

    def _call_gemini(self, system: str, prompt: str, max_retries: int = 3) -> str:
        url = f"{GEMINI_API_URL}?key={self.api_key}"
        payload = {
            "system_instruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {
                "temperature": 0.85,
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
                raw = result["candidates"][0]["content"]["parts"][0]["text"].strip()
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

    def generate_post(
        self,
        date: str,
        market_data: dict,
        news_list: list[dict],
        mode: str = "morning",
    ) -> dict:
        if mode == "evening":
            system = SYSTEM_EVENING
            prompt = _build_evening_prompt(date, market_data, news_list)
        else:
            system = SYSTEM_MORNING
            prompt = _build_morning_prompt(date, market_data, news_list)

        logger.info(f"Gemini API 호출 중 (모델: {GEMINI_MODEL}, 모드: {mode})...")
        raw = self._call_gemini(system, prompt)

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

        post = json.loads(raw)
        # JSON 파싱 직후 HTML 엔티티 정제
        # Gemini가 &middot; &amp; 등 HTML 엔티티를 그대로 출력하는 경우 방지
        post["title"]   = _clean_post_text(post.get("title", ""))
        post["content"] = _clean_post_text(post.get("content", ""))
        logger.info(f"생성된 글자 수: {len(post.get('content', ''))}자")
        logger.info(f"생성된 제목: {post.get('title', '')}")
        return post
