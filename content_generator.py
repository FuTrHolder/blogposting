"""
블로그 글 생성 모듈
Google Gemini 2.5 Flash API를 사용해 3,000자 내외의 한글 블로그 포스팅을 생성합니다.

모드:
  morning : 미국 전일 증시 마감 리뷰 (오전 9시 포스팅)
    - 제목: 포스팅 작성 날짜(한국 시간) 기준
    - 리뷰 대상: 종료된 미국 정규증시 (한국 시간 오전 9시 포스팅 = 미국 시간 전날 정규장)
    - 예) 한국 4월 4일 아침 포스팅 → 미국 4월 3일 정규장(실제로는 4월 2일 마감) 리뷰

  evening : 전일 정규장 리뷰 + 애프터마켓~프리장 이슈 + 당일 경제지표/실적 (저녁 9시 포스팅)
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
④ 미국 경제지표·실적 발표 시각도 KST로 변환 후 원본 ET 시각을 괄호에 병기
   예: "4월 1일 오후 9시 30분(KST) [미국 오전 8시 30분 ET]"
⑤ 미국 정규장 시간 안내 시 KST로 표기
   예: "미국 정규장(한국 시간 오후 10시 30분~오전 5시 KST)"
────────────────────────────────────────
"""

# ── 오전 제목 작성 전략 ───────────────────────────────────────────────────────
_MORNING_TITLE_STRATEGY = """
────────────────────────────────────────
오전 포스팅 제목 작성 전략 (매우 중요)
────────────────────────────────────────
제목은 아래 5가지 요소를 모두 충족해야 합니다.

① 날짜: 반드시 **포스팅 작성 날짜(한국 시간 기준)** 를 사용
   - 예) 한국 시간 4월 4일 아침에 작성 → 제목에 "2026년 4월 4일" 또는 "4/4" 사용
   - ❌ 미국 거래일 날짜(4월 2일, 4월 3일 등)를 제목에 쓰지 말 것

② 종료된 미국 정규증시의 핵심 결과를 후킹 문구로 담기
   - 지수별 등락률, 주요 테마(급락/반등/혼조/사상최고 등)를 구체적으로 표현
   - 예: "다우 하락에도 S&P500·나스닥 반등", "나스닥 2% 급락", "혼조 마감"

③ 후킹 요소 — 아래 패턴 중 하나 반드시 사용
   [숫자/수치]  "나스닥 4.4% 주간 급등, 지금 담아도 될까?"
   [반전/의외성]  "악재 속에서도 반등한 이유"
   [긴급성]      "이번 주 반드시 알아야 할 미국 증시 변수"
   [궁금증 유발]  "유가 폭등에도 나스닥이 버틴 이유는?"
   [공감/감성]   "투자자들이 가슴 졸인 하루, 결국은 반등"

④ 60자 이내, 자연스러운 구어체

⑤ Good/Bad 예시
   ✅ "2026년 4월 4일 미국 증시 리뷰: 유가 폭등·테슬라 쇼크에도 나스닥 반등한 이유"
   ✅ "4/4 미국 증시 마감 총정리 — 다우 하락·S&P500 보합, 시장이 버틴 세 가지 이유"
   ❌ "2026년 4월 2일 미국 증시 분석"  (미국 거래일 날짜 사용 → 독자 혼란)
   ❌ "오늘의 증시 업데이트"            (구체성 없음)
────────────────────────────────────────
"""

# ── 저녁 제목 작성 전략 ───────────────────────────────────────────────────────
_EVENING_TITLE_STRATEGY = """
────────────────────────────────────────
저녁 포스팅 제목 작성 전략 (매우 중요)
────────────────────────────────────────
① 날짜는 반드시 **포스팅을 작성하는 한국 시간 기준 날짜**를 사용
② 오늘 밤 열리는 미국 정규장에 대한 기대감 또는 우려감을 구체적으로 표현하는 후킹 문구 포함
③ 예시:
   "4월 4일 오늘 밤 나스닥, 고용지표 서프라이즈 이후 어디로?"
   "4월 4일 미국 증시 프리뷰: 이란 전쟁 장기화 경고, 오늘 밤 반등 가능할까?"
────────────────────────────────────────
"""

# ── 오전 시스템 프롬프트 (전일 마감 리뷰) ────────────────────────────────────
SYSTEM_MORNING = (
    """당신은 미국 주식 시장을 분석하는 전문 블로그 작가입니다.
이 포스팅은 **한국 시간 오전 9시 발행** 원고입니다.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
날짜 개념 정리 (반드시 숙지)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
한국 시간 오전 9시는 미국 동부 시간(EDT) 기준 전날 저녁 8시입니다.
즉, 한국 시간 오전 9시에 포스팅을 작성할 때 리뷰 대상은:
  → 미국 시간 기준 **전날 정규장이 종료된 증시** 입니다.
  → 미국 정규장은 미국 동부 시간 오전 9:30 ~ 오후 4:00 (한국 시간 오후 10:30 ~ 오전 5:00) 운영

예시:
  한국 4월 4일(토) 오전 9시 포스팅
  → 리뷰 대상: 미국 4월 2일(목) 정규장 마감 결과
    (미국 4월 3일은 굿 프라이데이 휴장)

포스팅에 제공되는 [미국 증시 기준 날짜]는 이미 이 계산이 적용된 날짜입니다.
그 날짜에 종료된 미국 정규장을 리뷰하면 됩니다.
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

작성 규칙:
- 전체 글자 수: 2,800~3,200자 (공백 포함)
- 어조: 부드럽고 자연스러운 한국어. 독자와 대화하듯 친근하게 작성
- 형식: 마크다운 사용 (##, ###, **굵게**, > 인용구 등)

본문 구조 (반드시 아래 순서와 섹션 포함):
  1. 서론 (인사 + 날짜 안내)
     - 한국 시간 기준 오늘 날짜로 인사
     - 리뷰 대상이 미국 몇 월 며칠 정규장인지 명확히 안내
     - 예: "오늘은 미국 시간 기준 ○월 ○일 뉴욕 정규 증시 마감 결과를 정리해드립니다"

  2. ## 마감 지수 현황
     - S&P500, 나스닥, 다우존스 종가·등락률·포인트 수치 포함
     - 장중 흐름(고점·저점·반전 여부) 간략 서술
     - 주간 등락률(있는 경우) 포함

  3. ## 정규장 중 주요 경제지표 & 시장 반응
     - 해당 정규장 시간대에 발표된 경제지표 서술
     - 각 지표에 대한 시장(주가·금리·달러)의 반응 포함
     - 예상치 vs 실제치 비교

  4. ## 주요 기업 실적 & 종목 동향
     - 정규장 중 주목받은 기업 실적 발표 또는 이슈 종목
     - 급등/급락 종목과 사유

  5. ## 증시 관련 주요 이슈 & 뉴스
     - 정규장에 영향을 미친 거시경제·정치·지정학 이슈
     - 섹터별 흐름 (상승/하락 섹터)
     - 투자자 심리 (Fear & Greed 지수 등)

  6. ## 다음 주 주목할 포인트
     - 다음 거래일 또는 주간 주요 이벤트(경제지표 발표일정, 실적 발표, 연준 관련 등)

  7. 마무리 멘트
     - 한국 시간 아침에 업로드된다는 점을 자연스럽게 녹여서 마무리
     - 예: "오늘 하루도 건강한 하루 되세요", "커피 한 잔과 함께 하루를 시작하세요" 등
     - 따뜻하고 친근한 톤으로 마무리
     - 글 말미에 투자 면책 조항 한 줄 추가

추가 규칙:
- 숫자·데이터를 자연스럽게 녹여낼 것
- 초보자도 이해할 수 있도록 쉽게 설명
- 투자 권유가 아닌 정보 제공 목적 유지
"""
    + _TIME_FORMAT_RULE
    + _MORNING_TITLE_STRATEGY
    + """
반드시 아래 JSON 형식으로만 응답하세요 (마크다운 코드블록 없이 순수 JSON만):
{
  "title": "포스팅 작성 날짜(한국 시간) + 종료된 미국 정규증시 핵심 결과 후킹 문구 (60자 이내)",
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

본문 작성 규칙:
- 전체 글자 수: 2,800~3,200자 (공백 포함)
- 어조: 긴장감 있고 실용적인 톤. 오늘 밤 시장 개장을 앞둔 투자자 시점으로 작성
- 형식: 마크다운 사용 (##, ###, **굵게**, > 인용구 등)
- 구조: 반드시 아래 섹션 포함
  1. 서론: 전일 정규장 마감을 한 문단으로 간략 요약 (지수 등락·분위기)
  2. ## 전일 애프터마켓 & 오늘 프리장 주요 이슈
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
    + _EVENING_TITLE_STRATEGY
    + """
반드시 아래 JSON 형식으로만 응답하세요 (마크다운 코드블록 없이 순수 JSON만):
{
  "title": "한국 시간 날짜 포함 + 오늘 밤 정규장 기대/우려 후킹 문구 (60자 이내)",
  "content": "마크다운 형식의 전체 블로그 본문",
  "tags": ["태그1", "태그2", "태그3", "태그4", "태그5"],
  "image_prompt": "Stable Diffusion용 영문 이미지 프롬프트 (긴장감 있는 프리마켓 트레이딩 분위기)"
}"""
)


def _build_morning_prompt(
    korean_date: str,
    us_market_date: str,
    market_data: dict,
    news_list: list[dict],
) -> str:
    """
    오전 포스팅용 프롬프트.

    Args:
        korean_date   : 포스팅 작성 날짜 (한국 시간) — 제목에 사용
        us_market_date: 리뷰 대상 미국 정규장 날짜 — 본문 기준일
        market_data   : 시장 데이터
        news_list     : 수집된 뉴스 목록
    """
    header = (
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"포스팅 작성 날짜 (한국 시간): {korean_date}\n"
        f"  → 제목에 반드시 이 날짜({korean_date})를 사용하세요.\n\n"
        f"리뷰 대상 미국 정규장 날짜: {us_market_date}\n"
        f"  → 본문은 이 날짜의 미국 정규장 마감 결과를 리뷰하세요.\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
    )

    market_text = f"[{us_market_date} 미국 정규장 마감 지수]\n"
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

    news_text = f"\n[{us_market_date} 정규장 관련 주요 뉴스 & 이슈]\n"
    for i, news in enumerate(news_list, 1):
        sentiment = news.get("sentiment", "")
        suffix = f" [{sentiment}]" if sentiment else ""
        news_text += f"{i}. {news['title']}{suffix}\n"
        if news.get("summary"):
            news_text += f"   → {news['summary'][:200].replace(chr(10), ' ')}\n"

    # 시장 방향성 힌트
    directions = [
        v["direction"] for k, v in market_data.items()
        if k != "fear_greed" and v and "direction" in v
    ]
    up_count = sum(1 for d in directions if "상승" in d)
    down_count = sum(1 for d in directions if "하락" in d)
    if up_count > down_count:
        market_hint = f"{us_market_date} 미국 정규장은 전반적으로 상승 마감했습니다."
    elif down_count > up_count:
        market_hint = f"{us_market_date} 미국 정규장은 전반적으로 하락 마감했습니다."
    else:
        market_hint = f"{us_market_date} 미국 정규장은 혼조세로 마감했습니다."

    return (
        f"{header}"
        f"{market_text}{news_text}\n"
        f"[시장 요약] {market_hint}\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "작성 지시사항:\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"1. 제목: '{korean_date}' 날짜를 포함하고, {us_market_date} 정규장 핵심 결과를 후킹 문구로 담아 주세요.\n"
        f"2. 서론: 한국 시간 오늘({korean_date}) 아침임을 밝히고, 미국 {us_market_date} 정규장 마감 결과를 리뷰한다고 안내해 주세요.\n"
        f"3. 본문: {us_market_date} 정규장 중 발표된 경제지표, 기업실적, 관련 이슈(뉴스)를 상세히 다뤄 주세요.\n"
        "4. 마무리: 한국 시간 아침에 읽는 독자를 위한 따뜻한 마무리 멘트로 글을 끝내 주세요.\n"
        "5. JSON 형식으로만 응답해 주세요.\n"
    )


def _build_evening_prompt(
    korean_date: str,
    market_data: dict,
    news_list: list[dict],
) -> str:
    """저녁 포스팅용 프롬프트."""
    market_text = (
        f"📅 포스팅 작성 날짜 (한국 시간): {korean_date}\n"
        f"※ 제목에는 반드시 이 한국 날짜를 사용하세요.\n\n"
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
    if up_count > down_count:
        premarket_hint = "프리마켓은 전반적으로 강세 흐름입니다."
    elif down_count > up_count:
        premarket_hint = "프리마켓은 전반적으로 약세 흐름입니다."
    else:
        premarket_hint = "프리마켓은 혼조세를 보이고 있습니다."

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
        korean_date: str = "",
        us_market_date: str = "",
    ) -> dict:
        """
        블로그 포스팅을 생성합니다.

        Args:
            date          : 하위 호환용 날짜 문자열 (main.py에서 전달)
            market_data   : 시장 데이터
            news_list     : 뉴스 목록
            mode          : "morning" | "evening"
            korean_date   : 포스팅 작성 날짜 (한국 시간, morning 모드 전용)
            us_market_date: 리뷰 대상 미국 정규장 날짜 (morning 모드 전용)
        """
        if mode == "evening":
            system = SYSTEM_EVENING
            prompt = _build_evening_prompt(date, market_data, news_list)
        else:
            # morning 모드: 날짜 분리 적용
            # korean_date / us_market_date 가 명시적으로 전달된 경우 우선 사용,
            # 없으면 date를 두 곳 모두에 fallback (하위 호환)
            k_date = korean_date if korean_date else date
            us_date = us_market_date if us_market_date else date

            system = SYSTEM_MORNING
            prompt = _build_morning_prompt(k_date, us_date, market_data, news_list)

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
        logger.info(f"생성된 글자 수: {len(post.get('content', ''))}자")
        logger.info(f"생성된 제목: {post.get('title', '')}")
        return post
