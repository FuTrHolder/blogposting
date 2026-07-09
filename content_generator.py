"""
블로그 글 생성 모듈
Google Gemini API를 사용해 3,000자 내외의 한글 블로그 포스팅을 생성합니다.

모드:
  morning : 미국 전일 증시 마감 리뷰 (오전 9시 포스팅)
  evening : 전일 정규장 리뷰 + 애프터마켓~프리장 이슈 + 당일 경제지표/실적 (저녁 9시 포스팅)
"""

import json
import logging
import time
import urllib.request
import urllib.error

logger = logging.getLogger(__name__)

# ── 모델 우선순위: 과부하/불가 시 순서대로 폴백 ─────────────────────────────
GEMINI_MODELS = [
    "gemini-2.5-flash-lite",   # 1순위: 경량·빠름
    "gemini-2.5-flash",        # 2순위: 표준
    "gemini-3.1-flash-lite",        # 3순위: 최종 폴백
]

# 재시도 가능한 HTTP 상태코드 (일시적 서버 오류·과부하 포함)
_RETRYABLE_CODES = {429, 500, 502, 503, 504}


def _gemini_url(model: str) -> str:
    return (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"{model}:generateContent"
    )


# ── 공통 시간 표기 규칙 (두 모드 공유) ──────────────────────────────────────
_TIME_FORMAT_RULE = """
────────────────────────────────────────
시간 표기 규칙 (반드시 준수)
────────────────────────────────────────
본문에 등장하는 모든 시간은 아래 규칙을 따르세요.

① 시간대는 반드시 **한국 시간(KST)** 기준으로 변환하여 표기
   - 미국 동부시간(ET) → KST 변환: EDT(서머타임) +13시간 / EST(겨울) +14시간
   - UTC → KST: +9시간
② 표기 형식: "날짜 시간(KST)"
   예: "4월 1일 오후 11시 30분(KST)", "4월 2일 오전 3시(KST)"
③ 날짜가 바뀌는 경우 날짜를 반드시 명시
   예: 미국 오후 4시 마감 → "4월 2일 오전 5시(KST)"처럼 날짜 포함
④ 미국 경제지표·실적 발표 시각도 KST로 변환 후 원본 ET 시각을 괄호에 병기
   예: "4월 1일 오후 9시 30분(KST) [미국 오전 8시 30분 ET]"
⑤ 애프터마켓·프리마켓 시간대 안내 시에도 동일하게 KST로 표기
   예: "애프터마켓(한국 시간 4월 1일 오전 5시~9시 30분 KST)"
────────────────────────────────────────
"""

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
    + _TIME_FORMAT_RULE
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

# ── 저녁 시스템 프롬프트 (전일 리뷰 + 애프터마켓~프리장 이슈 + 당일 지표) ────────
SYSTEM_EVENING = (
    """당신은 미국 주식 시장을 분석하는 전문 블로그 작가입니다.
이 포스팅은 **한국 시간 저녁 9시 발행** 원고입니다.

포스팅의 흐름은 다음 순서를 반드시 따르세요:
  ① 전일 미국 정규장 마감 결과 간략 리뷰 (분위기·지수 등락 요약)
  ② 전일 미국 애프터마켓(시간외) 이후 현재 프리장까지 발생한 주요 이슈
     (기업 실적 발표·가이던스, 연준 인사 발언, 지정학적 이슈, 매크로 변화 등)
  ③ 당일(미국 날짜 기준) 정규장에서 발표 예정인 경제지표 & 기업 실적
  ④ 현재 프리마켓/선물 동향과 오늘 밤 정규장 시나리오

제목 작성 규칙:
- 날짜는 반드시 **포스팅을 작성하는 한국 시간 기준 날짜**를 사용 (예: "4월 1일")
- 오늘 밤 열리는 미국 정규장에 대한 기대감 또는 우려감을 구체적으로 표현하는 후킹 문구 포함
- 예시: "4월 1일 오늘 밤 나스닥, 고용지표 발표 앞두고 어디로?"
         "4월 1일 미국 증시 프리뷰: 엔비디아 실적 충격, 오늘 밤 반등 가능할까?"

본문 작성 규칙:
- 전체 글자 수: 2,800~3,200자 (공백 포함)
- 어조: 긴장감 있고 실용적인 톤. 오늘 밤 시장 개장을 앞둔 투자자 시점으로 작성
- 형식: 마크다운 사용 (##, ###, **굵게**, > 인용구 등)
- 구조: 반드시 아래 섹션 포함
  1. 서론: 전일 정규장 마감을 한 문단으로 간략 요약 (지수 등락·분위기)
  2. ## 전일 애프터마켓 & 오늘 프리장 주요 이슈
     (시간외 급등락 종목, 실적 발표 결과, 주요 발언, 매크로 뉴스 등)
  3. ## 오늘 밤 주목할 경제지표 & 이벤트 (KST 시각 포함)
  4. ## 오늘 실적 발표 예정 기업 & 시장 기대치
  5. ## 현재 프리마켓·선물 동향
  6. ## 오늘 밤 시나리오: 강세 vs 약세
  7. 마무리: 오늘 밤 대응 포인트 한 문단
- 숫자·데이터를 자연스럽게 녹여낼 것
- 초보자도 이해할 수 있도록 쉽게 설명
- 투자 권유가 아닌 정보 제공 목적 유지
- 글 말미에 면책 조항 한 줄 추가
"""
    + _TIME_FORMAT_RULE
    + _TITLE_STRATEGY
    + """
반드시 아래 JSON 형식으로만 응답하세요 (마크다운 코드블록 없이 순수 JSON만):
{
  "title": "한국 시간 날짜 포함 + 오늘 밤 정규장 기대/우려 후킹 문구 (60자 이내)",
  "content": "마크다운 형식의 전체 블로그 본문",
  "tags": ["태그1", "태그2", "태그3", "태그4", "태그5"],
  "image_prompt": "Stable Diffusion용 영문 이미지 프롬프트 (긴장감 있는 프리마켓 트레이딩 분위기)"
}"""
)


def _reference_time_block(korean_datetime_str: str, ny_reference_str: str) -> str:
    """
    글 작성 시각(KST)과 분석 기준 시각(뉴욕)을 명시적으로 프롬프트에 박아 넣는 블록.
    AI가 매번 시간대를 스스로 계산하게 하면 오류가 잦으므로,
    이미 정확히 계산된 값을 "사실"로 제공하고 재계산을 금지시킵니다.
    """
    if not korean_datetime_str or not ny_reference_str:
        return ""
    return (
        "────────────────────────────────────────\n"
        "기준 시각 (아래 값을 그대로 사용하세요 — 직접 재계산 금지)\n"
        "────────────────────────────────────────\n"
        f"[글 작성 시각 - 한국(KST)]   {korean_datetime_str}\n"
        f"[분석 기준 시각 - 미국 뉴욕] {ny_reference_str}\n\n"
        "본문에서 \"오늘\", \"어제\", \"전일\", \"이번 주\" 등 상대적 시제 표현을 쓸 때는 "
        "반드시 위 [분석 기준 시각 - 미국 뉴욕]을 기준으로만 판단하세요.\n"
        "한국 시간(KST)은 이 글이 발행되는 시각을 나타낼 뿐이며, "
        "시황 분석의 '오늘/어제' 판단 기준으로는 절대 사용하지 마세요.\n"
        "위 [분석 기준 시각]의 날짜가 이미 정규장 마감(16:00 ET) 이후라면, "
        "그 날짜의 세션은 '오늘'이 아니라 이미 '마감된' 세션입니다.\n"
        "────────────────────────────────────────\n\n"
    )


def _build_morning_prompt(
    korean_date: str,
    us_market_date: str,
    market_data: dict,
    news_list: list[dict],
    korean_datetime_str: str = "",
    ny_reference_str: str = "",
) -> str:
    """오전 포스팅용 프롬프트: 전일 마감 데이터 중심.

    korean_date          : 포스팅 작성 날짜 (한국 시간) — 제목에 사용
    us_market_date       : 리뷰 대상 미국 정규장 마감 날짜 — 본문 지수 데이터에 사용
    korean_datetime_str  : 포스팅 작성 시각 전체 (예: '2026-07-09 09:00 KST')
    ny_reference_str     : 뉴욕 기준 시각 전체 (예: '2026-07-08 20:00 EDT')
    """
    market_text = (
        f"{_reference_time_block(korean_datetime_str, ny_reference_str)}"
        f"📅 포스팅 작성 날짜 (한국 시간): {korean_date}\n"
        f"📅 리뷰 대상 미국 증시 마감 날짜: {us_market_date}\n"
        f"※ 제목에는 반드시 한국 날짜({korean_date})를 사용하고, "
        f"본문의 '전일 마감'은 미국 날짜({us_market_date}) 기준임을 명확히 하세요.\n\n"
        f"[전일 마감 지수]\n"
    )
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
    hint = (
        "전일 미국 시장은 전반적으로 상승 마감했습니다." if up_count > down_count else
        "전일 미국 시장은 전반적으로 하락 마감했습니다." if down_count > up_count else
        "전일 미국 시장은 혼조세로 마감했습니다."
    )

    return (
        f"{market_text}{news_text}\n"
        f"[시장 요약] {hint}\n\n"
        "위 데이터를 바탕으로 전일 미국 증시 마감 리뷰 블로그 포스팅을 작성하고, "
        "지정된 JSON 형식으로만 응답해주세요.\n"
        "제목은 반드시 전날 마감 결과(지수 수치·등락 방향 등)를 구체적으로 담아 "
        "독자가 클릭하고 싶게 만들어 주세요."
    )


def _build_evening_prompt(
    korean_date: str,
    us_market_date: str,
    market_data: dict,
    news_list: list[dict],
    korean_datetime_str: str = "",
    ny_reference_str: str = "",
) -> str:
    """저녁 포스팅용 프롬프트: 전일 정규장 리뷰 + 애프터마켓~프리장 이슈 + 당일 지표 중심.

    korean_date          : 포스팅 작성 날짜 (한국 시간) — 제목에 사용
    us_market_date       : 가장 최근에 마감된 미국 정규장 날짜 ('전일 마감' 리뷰 대상)
    korean_datetime_str  : 포스팅 작성 시각 전체
    ny_reference_str     : 뉴욕 기준 현재 시각 전체 (보통 프리마켓 시간대)
    """
    market_text = (
        f"{_reference_time_block(korean_datetime_str, ny_reference_str)}"
        f"📅 포스팅 작성 날짜 (한국 시간): {korean_date}\n"
        f"📅 전일 마감 미국 정규장 날짜: {us_market_date}\n"
        f"※ 제목에는 반드시 한국 날짜({korean_date})를 사용하고, "
        f"본문의 '전일 마감'은 미국 날짜({us_market_date}) 기준임을 명확히 하세요.\n\n"
        f"[현재 선물/프리마켓 지수]\n"
    )
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

    news_text = "\n[수집된 주요 뉴스 & 이슈]\n"
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
    premarket_hint = (
        "프리마켓은 전반적으로 강세 흐름입니다." if up_count > down_count else
        "프리마켓은 전반적으로 약세 흐름입니다." if down_count > up_count else
        "프리마켓은 혼조세를 보이고 있습니다."
    )

    return (
        f"{market_text}{news_text}\n"
        f"[프리마켓 요약] {premarket_hint}\n\n"
        "위 데이터를 바탕으로 저녁 9시 블로그 포스팅을 작성하고, "
        "지정된 JSON 형식으로만 응답해주세요.\n\n"
        "작성 시 반드시 아래 흐름을 따르세요:\n"
        "1) 서론: 전일 미국 정규장 마감을 한 문단으로 간략 리뷰\n"
        "2) 전일 애프터마켓 이후 ~ 현재 프리장까지 발생한 주요 이슈 상세 서술\n"
        "3) 오늘 밤(미국 시간 기준 당일) 발표 예정 경제지표·기업실적 중심으로 서술\n"
        "4) 현재 프리마켓/선물 동향 및 오늘 밤 강세/약세 시나리오\n\n"
        "제목은 반드시 한국 시간 날짜를 포함하고, "
        "오늘 밤 정규장에 대한 기대감 또는 우려감을 후킹 문구로 표현해주세요."
    )


class ContentGenerator:
    def __init__(self, api_key: str):
        self.api_key = api_key

    def _call_gemini(self, system: str, prompt: str, max_retries: int = 3) -> str:
        """
        Gemini API 호출.
        - 503/429/500/502/504 → 지수 백오프 재시도
        - 모델 과부하 지속 시 GEMINI_MODELS 순서대로 자동 폴백
        - timeout: 90초 (안정성 확보)
        - 2.5 계열 모델은 thinking(추론)이 출력 토큰 예산을 함께 소모하므로 비활성화
        - MAX_TOKENS로 잘리거나 빈 응답이 오면 자동 재시도
        """
        for model in GEMINI_MODELS:
            url = f"{_gemini_url(model)}?key={self.api_key}"
            logger.info(f"Gemini 모델 시도: {model}")

            generation_config = {
                "temperature": 0.85,
                "maxOutputTokens": 8192,
                "responseMimeType": "application/json",
            }
            
            payload = {
                "system_instruction": {"parts": [{"text": system}]},
                "contents": [{"role": "user", "parts": [{"text": prompt}]}],
                "generationConfig": generation_config,
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
                    with urllib.request.urlopen(req, timeout=90) as resp:
                        result = json.loads(resp.read().decode("utf-8"))

                    candidate = result["candidates"][0]
                    finish_reason = candidate.get("finishReason", "")
                    parts = candidate.get("content", {}).get("parts", [])
                    # 멀티파트 응답 방어: 모든 파트의 텍스트를 이어붙임
                    raw = "".join(p.get("text", "") for p in parts).strip()

                    if finish_reason == "MAX_TOKENS":
                        logger.warning(
                            f"Gemini 응답이 MAX_TOKENS로 잘림 (모델: {model}, "
                            f"시도 {attempt}/{max_retries}, 응답 길이: {len(raw)}자)"
                        )
                        if attempt < max_retries:
                            time.sleep(5)
                            continue
                        break

                    if not raw:
                        logger.warning(
                            f"Gemini 빈 응답 (모델: {model}, finishReason: {finish_reason}, "
                            f"시도 {attempt}/{max_retries})"
                        )
                        if attempt < max_retries:
                            time.sleep(5)
                            continue
                        break

                    logger.info(f"Gemini 응답 성공 (모델: {model}, {len(raw)}자)")
                    return raw

                except urllib.error.HTTPError as e:
                    body = e.read().decode("utf-8", errors="replace")
                    if e.code in _RETRYABLE_CODES:
                        base_wait = 30 if e.code == 429 else 20
                        wait = min(base_wait * attempt, 120)
                        logger.warning(
                            f"Gemini {e.code} (모델: {model}, "
                            f"시도 {attempt}/{max_retries}). {wait}초 대기 후 재시도..."
                        )
                        time.sleep(wait)
                    else:
                        logger.error(
                            f"Gemini API 오류 {e.code} (모델: {model}): {body[:300]}"
                        )
                        raise

                except urllib.error.URLError as e:
                    wait = min(15 * attempt, 60)
                    logger.warning(
                        f"Gemini 네트워크 오류 (모델: {model}, "
                        f"시도 {attempt}/{max_retries}): {e}. {wait}초 대기..."
                    )
                    time.sleep(wait)

                except Exception as e:
                    logger.warning(
                        f"Gemini 호출 실패 (모델: {model}, "
                        f"시도 {attempt}/{max_retries}): {e}"
                    )
                    if attempt < max_retries:
                        time.sleep(10)
                    else:
                        break

            logger.warning(f"모델 {model} 모든 시도 실패 → 다음 모델로 폴백")

        raise RuntimeError(
            f"모든 Gemini 모델({', '.join(GEMINI_MODELS)}) 호출 실패. "
            "잠시 후 다시 시도해주세요."
        )

    def generate_post(
        self,
        date: str,
        market_data: dict,
        news_list: list[dict],
        mode: str = "morning",
        korean_date: str | None = None,
        us_market_date: str | None = None,
        korean_datetime_str: str | None = None,
        ny_reference_str: str | None = None,
    ) -> dict:
        # korean_date/us_market_date가 전달되지 않으면 하위 호환을 위해 date로 대체
        _korean_date = korean_date or date
        _us_market_date = us_market_date or date
        _korean_datetime_str = korean_datetime_str or ""
        _ny_reference_str = ny_reference_str or ""

        if mode == "evening":
            system = SYSTEM_EVENING
            prompt = _build_evening_prompt(
                _korean_date, _us_market_date, market_data, news_list,
                _korean_datetime_str, _ny_reference_str,
            )
        else:
            system = SYSTEM_MORNING
            prompt = _build_morning_prompt(
                _korean_date, _us_market_date, market_data, news_list,
                _korean_datetime_str, _ny_reference_str,
            )

        logger.info(f"Gemini API 호출 중 (모드: {mode})...")

        max_json_retries = 2  # JSON 파싱 실패 시 Gemini 재호출 횟수
        last_error: Exception | None = None

        for json_attempt in range(1, max_json_retries + 2):  # 최초 1회 + 재시도
            raw = self._call_gemini(system, prompt)
            try:
                post = self._parse_json_response(raw)
                logger.info(f"생성된 글자 수: {len(post.get('content', ''))}자")
                logger.info(f"생성된 제목: {post.get('title', '')}")
                return post
            except json.JSONDecodeError as e:
                last_error = e
                logger.warning(
                    f"JSON 파싱 실패 (시도 {json_attempt}/{max_json_retries + 1}): {e}"
                )
                logger.warning(f"  응답 길이: {len(raw)}자")
                logger.warning(f"  응답 앞부분: {raw[:300]!r}")
                logger.warning(f"  응답 뒷부분: {raw[-300:]!r}")
                if json_attempt <= max_json_retries:
                    logger.info("Gemini를 재호출해 다시 시도합니다...")

        raise RuntimeError(
            f"Gemini 응답을 JSON으로 파싱하는 데 {max_json_retries + 1}회 모두 실패했습니다: {last_error}"
        )

    @staticmethod
    def _parse_json_response(raw: str) -> dict:
        """코드블록 방어 포함 JSON 파싱. 실패 시 json.JSONDecodeError를 발생시킴."""
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
