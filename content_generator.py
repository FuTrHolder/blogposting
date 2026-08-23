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
    "gemini-2.0-flash",        # 3순위: 최종 폴백
]

# 재시도 가능한 HTTP 상태코드 (일시적 서버 오류·과부하 포함)
_RETRYABLE_CODES = {429, 500, 502, 503, 504}

# Gemini 응답에서 image_prompt 필드가 누락됐을 때 사용할 안전한 기본값
# (main.py가 post["image_prompt"]로 dict 접근을 하므로, 키 자체가 없으면
# KeyError로 파이프라인 전체가 죽는 것을 방지)
FALLBACK_IMAGE_PROMPT = {
    "morning": "quiet Wall Street financial district after market close, "
               "calm analytical mood, stock exchange building, soft morning light",
    "evening": "tense premarket trading floor, New York financial district at night, "
               "dynamic energetic mood, city skyline with stock market data overlay",
}


def _gemini_url(model: str) -> str:
    return (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"{model}:generateContent"
    )


# ── 공통 HTML 형식 규칙 (두 모드 공유) ───────────────────────────────────────
_HTML_FORMAT_RULE = """
────────────────────────────────────────
HTML 형식 규칙 (반드시 준수)
────────────────────────────────────────
본문(content)은 마크다운이 아니라 순수 HTML로 작성합니다. 티스토리
HTML 편집기에 그대로 붙여넣을 수 있는 완전한 HTML 조각이어야 합니다.

① 문서 뼈대 태그(<html>, <head>, <body>)는 쓰지 않습니다. 본문 내용에
   해당하는 태그(<h2>, <p>, <ul> 등)만 나열합니다.

② 큰 제목은 <h2>, 소제목은 <h3>를 사용합니다. <h1>은 쓰지 않습니다.
   예: <h2>주요 지수 마감 결과</h2>

③ 모든 본문 단락은 <p>...</p>로 감쌉니다.
   예: <p>전일 미국 증시 마감 결과는 다음과 같습니다.</p>

④ 리스트는 <ul><li>...</li></ul>를 사용합니다. 항목 안의 굵은 글씨는
   <strong>으로 감쌉니다.
   예: <ul><li><strong>S&P 500:</strong> 0.15% 상승</li></ul>

⑤ 뉴스·이슈 항목은 <h3> 소제목 + 바로 다음 <p> 본문으로 처리합니다.
   예:
   <h3>FOMC 금리 동결, 시장 반응은?</h3>
   <p>연준은 이번 회의에서 금리를 동결했습니다...</p>

⑥ 이미지: 본문 안에 <img> 태그나 이미지 관련 구문을 절대 삽입하지 않습니다.
   썸네일은 대시보드에서 별도로 관리되므로, 본문(content)은 순수 텍스트
   콘텐츠에만 집중합니다.

⑦ 면책 조항은 글 말미 마무리 문단 바로 다음에 <blockquote><p>...</p></blockquote>로
   작성합니다.
   예:
   <blockquote><p>본 콘텐츠는 제공된 정보를 바탕으로 작성되었으며, 투자 권유를
   목적으로 하지 않습니다. 투자 결정에 따른 책임은 본인에게 있습니다.</p></blockquote>

⑧ 사진 제공 크레딧은 별도로 추가하지 않습니다. 면책 조항 blockquote
   하나로 글을 마무리합니다.

⑨ 시장이 상승도 하락도 아닌 애매한 상태를 표현할 때 "혼조" "혼조세"만 반복해서
   쓰지 말고, "방향성 없이 엇갈린 흐름", "업종별로 희비가 갈리는 모습",
   "뚜렷한 방향 없이 등락을 거듭", "종목별로 온도차를 보임" 등 다양한 표현을
   상황에 맞게 섞어 쓰세요. 제목과 본문 안에서도 같은 표현을 두 번 이상
   반복하지 않도록 주의하세요.

⑩ 태그는 서로 줄바꿈 없이 정확한 개폐 구조(예: <p>...</p>)로 작성하고,
   허용된 태그(<h2> <h3> <p> <ul> <li> <strong> <blockquote>) 외의
   임의 class/style/속성은 추가하지 않습니다.
────────────────────────────────────────
"""

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
- 형식: 순수 HTML 사용 (<h2>, <h3>, <p>, <strong>, <blockquote> 등)
- 구조: 반드시 아래 섹션 포함
  1. 서론: 어제 미국 증시를 한 줄로 요약하는 분위기 묘사
  2. <h2>주요 지수 마감 결과</h2> (S&P500·나스닥·다우 수치 포함)
  3. <h2>어제의 핵심 뉴스 & 시장 반응</h2>
  4. <h2>섹터별 마감 흐름</h2>
  5. <h2>투자자 심리 & Fear & Greed 지수</h2>
  6. <h2>오늘(미국 시간) 주목해야 할 포인트</h2>
  7. 따뜻한 마무리 한 문단
- 숫자·데이터를 자연스럽게 녹여낼 것
- 초보자도 이해할 수 있도록 쉽게 설명
- 투자 권유가 아닌 정보 제공 목적 유지
- 글 말미에 면책 조항 한 줄 추가
"""
    + _HTML_FORMAT_RULE
    + _TIME_FORMAT_RULE
    + _TITLE_STRATEGY
    + """
반드시 아래 JSON 형식으로만 응답하세요 (마크다운 코드블록 없이 순수 JSON만):
{
  "title": "후킹 요소가 포함된 SEO 최적화 제목 (날짜 포함, 60자 이내)",
  "content": "HTML 형식의 전체 블로그 본문 (예: <h2>...</h2><p>...</p>)",
  "tags": ["태그1", "태그2", "태그3", "태그4", "태그5"],
  "image_prompt": "Stable Diffusion용 영문 이미지 프롬프트 (마감 후 조용한 월스트리트 분위기)"
}

JSON 출력 시 반드시 지켜야 할 규칙:
- content 필드는 HTML 태그를 포함하므로 그 안의 큰따옴표(")는 반드시 \\" 로
  이스케이프하세요 (예: <a href=\\"...\\">). 이스케이프를 빠뜨리면 JSON 전체가
  깨집니다.
- content 필드 안의 줄바꿈은 반드시 \\n 으로 이스케이프하세요 (리터럴 줄바꿈 금지)
- 백슬래시(\\)는 \\\\ 로 이스케이프하세요
- JSON 전체가 단 하나의 유효한 JSON 객체여야 합니다"""
)

# ── 저녁 시스템 프롬프트 (전일 리뷰 + 애프터마켓~프리장 이슈 + 당일 지표) ────────
SYSTEM_EVENING = (
    """당신은 미국 주식 시장을 분석하는 전문 블로그 작가입니다.
이 포스팅은 **한국 시간 저녁 9시 발행** 원고입니다.

포스팅의 흐름은 다음 순서를 반드시 따르세요:
  ① 전일 미국 정규장 마감 결과 간략 리뷰 (분위기·지수 등락 요약)
  ② 전일 미국 애프터마켓(시간외) 이후 현재 프리장까지 발생한 주요 이슈
     (기업 실적 발표·가이던스, 연준 인사 발언, 지정학적 이슈, 매크로 변화 등)
  ③ 오늘 밤(= 위 [분석 기준 시각 - 미국 뉴욕]의 그 날짜) 정규장에서 발표 예정인
     경제지표 & 기업 실적만 다룹니다. 며칠 뒤에 예정된 지표를 '오늘'로 앞당겨
     서술하지 마세요 — 아직 오지 않은 날짜라면 "D+n일 후 발표 예정"처럼 정확히
     표현하세요.
  ④ 현재 프리마켓/선물 동향과 오늘 밤 정규장 시나리오

제목 작성 규칙:
- 날짜는 반드시 **포스팅을 작성하는 한국 시간 기준 날짜**를 사용 (예: "4월 1일")
- 오늘 밤 열리는 미국 정규장에 대한 기대감 또는 우려감을 구체적으로 표현하는 후킹 문구 포함
- 예시: "4월 1일 오늘 밤 나스닥, 고용지표 발표 앞두고 어디로?"
         "4월 1일 미국 증시 프리뷰: 엔비디아 실적 충격, 오늘 밤 반등 가능할까?"

본문 작성 규칙:
- 전체 글자 수: 2,800~3,200자 (공백 포함)
- 어조: 긴장감 있고 실용적인 톤. 오늘 밤 시장 개장을 앞둔 투자자 시점으로 작성
- 형식: 순수 HTML 사용 (<h2>, <h3>, <p>, <strong>, <blockquote> 등)
- 구조: 반드시 아래 섹션 포함
  1. 서론: 전일 정규장 마감을 한 문단으로 간략 요약 (지수 등락·분위기)
  2. <h2>전일 애프터마켓 & 오늘 프리장 주요 이슈</h2>
     (시간외 급등락 종목, 실적 발표 결과, 주요 발언, 매크로 뉴스 등)
  3. <h2>오늘 밤 주목할 경제지표 & 이벤트 (KST 시각 포함)</h2>
  4. <h2>오늘 실적 발표 예정 기업 & 시장 기대치</h2>
  5. <h2>현재 프리마켓·선물 동향</h2>
  6. <h2>오늘 밤 시나리오: 강세 vs 약세</h2>
  7. 마무리: 오늘 밤 대응 포인트 한 문단
- 숫자·데이터를 자연스럽게 녹여낼 것
- 초보자도 이해할 수 있도록 쉽게 설명
- 투자 권유가 아닌 정보 제공 목적 유지
- 글 말미에 면책 조항 한 줄 추가
"""
    + _HTML_FORMAT_RULE
    + _TIME_FORMAT_RULE
    + _TITLE_STRATEGY
    + """
반드시 아래 JSON 형식으로만 응답하세요 (마크다운 코드블록 없이 순수 JSON만):
{
  "title": "한국 시간 날짜 포함 + 오늘 밤 정규장 기대/우려 후킹 문구 (60자 이내)",
  "content": "HTML 형식의 전체 블로그 본문 (예: <h2>...</h2><p>...</p>)",
  "tags": ["태그1", "태그2", "태그3", "태그4", "태그5"],
  "image_prompt": "Stable Diffusion용 영문 이미지 프롬프트 (긴장감 있는 프리마켓 트레이딩 분위기)"
}

JSON 출력 시 반드시 지켜야 할 규칙:
- content 필드는 HTML 태그를 포함하므로 그 안의 큰따옴표(")는 반드시 \\" 로
  이스케이프하세요 (예: <a href=\\"...\\">). 이스케이프를 빠뜨리면 JSON 전체가
  깨집니다.
- content 필드 안의 줄바꿈은 반드시 \\n 으로 이스케이프하세요 (리터럴 줄바꿈 금지)
- 백슬래시(\\)는 \\\\ 로 이스케이프하세요
- JSON 전체가 단 하나의 유효한 JSON 객체여야 합니다"""
)



def _reference_time_block(korean_datetime_str: str, ny_reference_str: str) -> str:
    if not korean_datetime_str or not ny_reference_str:
        return ""
    # ny_reference_str 예: "2026-08-11 08:00 EDT" → 날짜 부분만 추출해
    # "오늘"을 숫자 하나로 명확히 못박습니다 (자연어 설명보다 훨씬 안전).
    ny_date_only = ny_reference_str.split(" ")[0] if ny_reference_str else ""
    return (
        "────────────────────────────────────────\n"
        "기준 시각 (아래 값을 그대로 사용하세요 — 직접 재계산 금지)\n"
        "────────────────────────────────────────\n"
        f"[글 작성 시각 - 한국(KST)]   {korean_datetime_str}\n"
        f"[분석 기준 시각 - 미국 뉴욕] {ny_reference_str}\n\n"
        f"오늘 날짜는 미국 뉴욕 기준으로 정확히 {ny_date_only} 입니다. "
        f"이 날짜가 유일한 '오늘'이며, 이후 다가올 예정된 이벤트(경제지표 발표일,"
        f" 실적 발표일 등)의 날짜를 '오늘'이나 '오늘 밤'으로 착각해서는 안 됩니다.\n"
        "본문에서 \"오늘\", \"어제\", \"전일\", \"이번 주\" 등 상대적 시제 표현을 쓸 때는 "
        f"반드시 {ny_date_only}을 기준으로만 판단하세요.\n"
        "한국 시간(KST)은 이 글이 발행되는 시각을 나타낼 뿐이며, "
        "시황 분석의 '오늘/어제' 판단 기준으로는 절대 사용하지 마세요.\n"
        "위 [분석 기준 시각]의 날짜가 이미 정규장 마감(16:00 ET) 이후라면, "
        "그 날짜의 세션은 '오늘'이 아니라 이미 '마감된' 세션입니다.\n"
        "'검증된 사실 정보'에 나오는 경제지표 날짜는 D-day가 함께 표기되어 "
        "있습니다. D+0(오늘)이 아닌 항목을 '오늘' 또는 '오늘 밤'이라고 쓰지 마세요 — "
        "'며칠 뒤인 D+n일에 발표 예정'처럼 정확한 시점으로 서술하세요.\n"
        "────────────────────────────────────────\n\n"
    )


_MIXED_MARKET_SYNONYMS = [
    "혼조세를 보였습니다.",
    "방향성 없이 엇갈린 흐름을 나타냈습니다.",
    "업종별로 희비가 갈리는 모습이었습니다.",
    "뚜렷한 방향 없이 등락을 거듭했습니다.",
    "종목·업종별로 온도차를 보였습니다.",
]


def _pick_mixed_market_phrase(korean_date: str, prefix: str) -> str:
    """
    시장이 상승도 하락도 아닌 애매한 상태일 때 쓰는 문구를, 매번 같은 단어
    ("혼조세")로 고정하지 않고 날짜 기반으로 여러 동의 표현 중 하나를
    골라 사용합니다 (포스팅마다 표현이 반복되는 것을 완화).
    """
    idx = sum(ord(c) for c in korean_date) % len(_MIXED_MARKET_SYNONYMS)
    return f"{prefix} {_MIXED_MARKET_SYNONYMS[idx]}"


def _build_morning_prompt(
    korean_date: str,
    us_market_date: str,
    market_data: dict,
    news_list: list[dict],
    korean_datetime_str: str = "",
    ny_reference_str: str = "",
    fact_reference_block: str = "",
) -> str:
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
        _pick_mixed_market_phrase(korean_date, "전일 미국 시장은")
    )

    fact_block_text = f"\n{fact_reference_block}\n" if fact_reference_block else ""

    return (
        f"{market_text}{news_text}\n"
        f"[시장 요약] {hint}\n"
        f"{fact_block_text}\n"
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
    fact_reference_block: str = "",
) -> str:
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
        _pick_mixed_market_phrase(korean_date, "프리마켓은")
    )

    fact_block_text = f"\n{fact_reference_block}\n" if fact_reference_block else ""

    return (
        f"{market_text}{news_text}\n"
        f"[프리마켓 요약] {premarket_hint}\n"
        f"{fact_block_text}\n"
        "위 데이터를 바탕으로 저녁 9시 블로그 포스팅을 작성하고, "
        "지정된 JSON 형식으로만 응답해주세요.\n\n"
        "작성 시 반드시 아래 흐름을 따르세요:\n"
        "1) 서론: 전일 미국 정규장 마감을 한 문단으로 간략 리뷰\n"
        "2) 전일 애프터마켓 이후 ~ 현재 프리장까지 발생한 주요 이슈 상세 서술\n"
        "3) 오늘 밤(위 [분석 기준 시각 - 미국 뉴욕] 날짜, D+0 항목만 해당) 발표 예정 "
        "경제지표·기업실적 중심으로 서술 (단, 실적/지표 발표일을 구체적으로 언급할 때는 "
        "반드시 위 '검증된 사실 정보'에 근거해야 하며, D+0이 아닌 항목은 '오늘'이 아니라 "
        "'D+n일 후 예정'처럼 정확한 시점으로 표현하고, 목록에 없는 기업의 실적일은 "
        "추측해서 쓰지 마세요)\n"
        "4) 현재 프리마켓/선물 동향 및 오늘 밤 강세/약세 시나리오\n\n"
        "제목은 반드시 한국 시간 날짜를 포함하고, "
        "오늘 밤 정규장에 대한 기대감 또는 우려감을 후킹 문구로 표현해주세요."
    )


class ContentGenerator:
    def __init__(self, api_key: str):
        self.api_key = api_key


    def _call_gemini(self, system: str, prompt: str, max_retries: int = 3) -> str:
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

        for model in GEMINI_MODELS:
            url = f"{_gemini_url(model)}?key={self.api_key}"
            logger.info(f"Gemini 모델 시도: {model}")

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
                    raw = "".join(p.get("text", "") for p in parts).strip()

                    if finish_reason == "MAX_TOKENS":
                        logger.warning(
                            f"Gemini 응답이 MAX_TOKENS로 잘림 (모델: {model}, "
                            f"시도 {attempt}/{max_retries})"
                        )
                        if attempt < max_retries:
                            time.sleep(5)
                            continue
                        break

                    if not raw:
                        logger.warning(
                            f"Gemini 빈 응답 (모델: {model}, "
                            f"finishReason: {finish_reason}, 시도 {attempt}/{max_retries})"
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
        fact_reference_block: str = "",
        fact_lookup: dict | None = None,
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
                _korean_datetime_str, _ny_reference_str, fact_reference_block,
            )
        else:
            system = SYSTEM_MORNING
            prompt = _build_morning_prompt(
                _korean_date, _us_market_date, market_data, news_list,
                _korean_datetime_str, _ny_reference_str, fact_reference_block,
            )

        logger.info(f"Gemini API 호출 중 (모드: {mode})...")

        max_json_retries = 2
        last_error: Exception | None = None

        for json_attempt in range(1, max_json_retries + 2):
            raw = self._call_gemini(system, prompt)
            try:
                post = self._parse_json_response(raw)
                post = self._strip_image_tags(post)
                post = self._ensure_required_fields(post, mode)
                logger.info(f"생성된 글자 수: {len(post.get('content', ''))}자")
                logger.info(f"생성된 제목: {post.get('title', '')}")
                post = self._fact_check_and_correct(post, system, prompt, fact_lookup, mode)
                return post
            except json.JSONDecodeError as e:
                last_error = e
                logger.warning(
                    f"JSON 파싱 실패 (시도 {json_attempt}/{max_json_retries + 1}): {e}"
                )
                if json_attempt <= max_json_retries:
                    logger.info("Gemini를 재호출해 다시 시도합니다...")

        raise RuntimeError(
            f"Gemini 응답을 JSON으로 파싱하는 데 {max_json_retries + 1}회 모두 실패했습니다: {last_error}"
        )

    def _fact_check_and_correct(
        self,
        post: dict,
        system: str,
        original_prompt: str,
        fact_lookup: dict | None,
        mode: str = "morning",
    ) -> dict:
        if not fact_lookup:
            return post

        try:
            import fact_checker
            violations = fact_checker.check_facts(post, fact_lookup)
        except Exception as e:
            logger.warning(f"팩트체크 검사 중 오류(무시하고 원본 유지): {e}")
            return post

        if not violations:
            logger.info("팩트체크: 실적/지표 발표일 관련 사실 오류 없음")
            return post

        logger.warning(f"팩트체크: {len(violations)}건의 사실 오류 후보 발견")
        for v in violations:
            logger.warning(
                f"  - [{v['type']}/{v['source']}] {v['entity']}: "
                f"발견='{v.get('found_date')}' 기대='{v['expected_date']}' "
                f"(자동교정={'가능' if v.get('auto_fixable') else '불가'})"
            )

        post, applied, remaining = fact_checker.auto_correct_facts(post, violations)
        if applied:
            logger.warning(f"팩트체크: {len(applied)}건 자동 교정 완료")

        if not remaining:
            return post

        logger.warning(f"팩트체크: {len(remaining)}건 Gemini 재생성 요청 (1회 한정)")
        correction_note = fact_checker.build_correction_prompt_note(remaining)
        corrected_prompt = original_prompt + "\n\n" + correction_note

        try:
            raw2 = self._call_gemini(system, corrected_prompt)
            post2 = self._parse_json_response(raw2)
            post2 = self._strip_image_tags(post2)
            post2 = self._ensure_required_fields(post2, mode)
        except Exception as e:
            logger.warning(f"팩트체크 재생성 실패(원본 유지): {e}")
            return fact_checker.neutralize_unresolved(post, remaining)

        violations2 = fact_checker.check_facts(post2, fact_lookup)
        if not violations2:
            logger.info("팩트체크: 재생성 후 모든 사실 오류 해결")
            return post2

        post2, applied2, remaining2 = fact_checker.auto_correct_facts(post2, violations2)
        if applied2:
            logger.warning(f"팩트체크: 재생성본에서 {len(applied2)}건 추가 교정")

        if remaining2:
            logger.error(f"팩트체크: 재생성 후에도 {len(remaining2)}건 미해결 → 안전 대체")
            post2 = fact_checker.neutralize_unresolved(post2, remaining2)

        return post2

    @staticmethod
    def _ensure_required_fields(post: dict, mode: str) -> dict:
        """
        generate_post()가 반환하기 직전, 파이프라인 뒤쪽(main.py의
        img_gen.generate(prompt=post["image_prompt"], ...) 등)에서 KeyError로
        죽지 않도록 필수 필드(title/content/tags/image_prompt)의 존재를
        보장합니다.

        이 검증이 필요한 이유: JSON 파싱 자체는 성공했더라도(1~3단계 중 하나가
        유효한 JSON을 반환), Gemini가 응답에서 특정 키를 통째로 빠뜨리는
        경우가 실제로 발생합니다. 특히 3단계(리터럴 줄바꿈 정규화) 정규식은
        content 값 안에 콜론(:)이나 따옴표가 포함된 문장이 있으면 필드 경계를
        잘못 판단해 다음 필드(image_prompt 등)까지 흡수해버릴 수 있어, 결과
        JSON 자체는 유효하지만 image_prompt 키가 사라진 상태로 파싱될 수
        있습니다. post["image_prompt"]처럼 dict 접근을 그대로 쓰는 호출부가
        있으므로, 여기서 항상 안전한 기본값을 채워 KeyError를 원천 차단합니다.
        """
        post = dict(post)

        if not post.get("title"):
            logger.warning("Gemini 응답에 title 필드 누락 — 기본값으로 대체")
            post["title"] = "미국 증시 브리핑"

        if not post.get("content"):
            logger.warning("Gemini 응답에 content 필드 누락 — 빈 문자열로 대체")
            post["content"] = ""

        if not isinstance(post.get("tags"), list) or not post.get("tags"):
            logger.warning("Gemini 응답에 tags 필드 누락(또는 리스트 아님) — 기본 태그로 대체")
            post["tags"] = ["미국증시", "주식", "나스닥", "S&P500", "증시분석"]

        if not post.get("image_prompt"):
            logger.warning(
                "Gemini 응답에 image_prompt 필드 누락 — 기본 프롬프트로 대체"
            )
            default_prompt = FALLBACK_IMAGE_PROMPT.get(mode, FALLBACK_IMAGE_PROMPT["morning"])
            post["image_prompt"] = default_prompt

        return post

    @staticmethod
    def _strip_image_tags(post: dict) -> dict:
        """
        본문(content)에 혹시라도 남아있는 이미지 삽입 구문을 안전하게 제거합니다.

        시스템 프롬프트에서 이미지 태그를 쓰지 말라고 지시하지만, Gemini가
        과거 학습 데이터의 습관대로 [##_Image...] 태그, <img> 태그, 마크다운
        이미지 문법(![]())을 넣을 가능성에 대비한 안전망입니다.
        썸네일은 대시보드에서 별도(thumbnail_url)로 관리되므로, 본문에
        이런 태그가 남아있으면 텍스트 사이에 불필요한 줄이 섞여 보입니다.
        """
        import re
        content = post.get("content", "") or ""
        if not content:
            return post

        before_len = len(content)

        # 티스토리 [##_Image|...|{...}_##] 형태
        content = re.sub(r"\[##_Image\|[^\]]*_##\]", "", content)
        # 마크다운 이미지 문법 ![alt](url)
        content = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", content)
        # HTML <img ...> 태그 (자체 종료/미종료 모두)
        content = re.sub(r"<img\b[^>]*>", "", content, flags=re.IGNORECASE)
        # 남은 빈 줄이 3개 이상 연속되면 2개로 축소
        content = re.sub(r"\n{3,}", "\n\n", content)
        content = content.strip()

        if len(content) != before_len:
            logger.warning(
                f"본문에서 이미지 태그 제거함 ({before_len - len(content)}자 감소)"
            )

        post = dict(post)
        post["content"] = content
        return post

    @staticmethod
    def _parse_json_response(raw: str) -> dict:
        """
        Gemini 응답을 JSON으로 파싱합니다.

        단계별 파싱 전략:
          1) 코드블록(```) 제거 후 json.loads
          2) 원본 그대로 json.loads
          3) 줄바꿈·탭 정규화 후 json.loads
          4) content/title/tags/image_prompt 필드를 정규식으로 직접 추출
             (Gemini가 content 필드 안에 이스케이프되지 않은 큰따옴표·줄바꿈을
              넣어 JSON 구조가 깨질 때 최후 수단으로 사용)
        """
        import re

        # ── 1단계: 코드블록 제거 ────────────────────────────────────────────
        candidate = raw
        if "```" in raw:
            for part in raw.split("```"):
                part = part.strip()
                if part.startswith("json"):
                    part = part[4:].strip()
                try:
                    return json.loads(part)
                except json.JSONDecodeError:
                    pass
            # 코드블록 내용만 추출 실패 시 코드블록 제거 후 이어서 시도
            candidate = re.sub(r"```(?:json)?", "", raw).strip()

        # ── 2단계: 직접 파싱 ────────────────────────────────────────────────
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            pass

        # ── 3단계: 줄바꿈/탭 정규화 후 파싱 ────────────────────────────────
        # Gemini가 JSON 문자열 값 안에 리터럴 줄바꿈을 넣는 경우
        # "content": "줄1\n줄2" 가 아니라 "content": "줄1
        # 줄2" 처럼 생성하는 버그 대응.
        #
        # 주의: 이전 버전은 `: "..."(?="[,}])` 형태의 non-greedy 정규식으로
        # 필드 값의 끝을 판단했는데, content 값 안에 콜론+따옴표가 우연히
        # 등장하면(예: '9시: "발표"' 같은 인용구) 그 지점에서 필드가 끝났다고
        # 오판해 뒤따르는 tags/image_prompt 필드까지 content 안으로 흡수해
        # 통째로 유실시키는 문제가 있었습니다. 알려진 필드 이름(title/content/
        # tags/image_prompt) 바로 앞에서만 값이 끝난다고 판단하도록 경계
        # 기준을 명확히 해서 이 문제를 방지합니다.
        try:
            known_fields = ("title", "content", "tags", "image_prompt")
            next_field_pattern = "|".join(re.escape(f) for f in known_fields)
            # 각 "필드": 뒤에 오는 값을, 다음 필드 이름이 나오기 직전까지로 확정
            field_value_re = re.compile(
                rf'"({next_field_pattern})"\s*:\s*"(.*?)"\s*(?=,\s*"(?:{next_field_pattern})"\s*:|\s*}})',
                re.DOTALL,
            )

            def _escape_value(m: "re.Match") -> str:
                field, value = m.group(1), m.group(2)
                escaped = (
                    value.replace("\\", "\\\\")
                    .replace("\n", "\\n")
                    .replace("\t", "\\t")
                    .replace('"', '\\"')
                )
                # 위에서 백슬래시를 먼저 이스케이프했으므로, 이미 유효했던
                # \\n, \\" 같은 시퀀스가 이중 이스케이프되지 않도록 원복
                escaped = escaped.replace("\\\\n", "\\n").replace('\\\\"', '\\"')
                return f'"{field}": "{escaped}"'

            normalized = field_value_re.sub(_escape_value, candidate)
            return json.loads(normalized)
        except (json.JSONDecodeError, Exception):
            pass

        # ── 4단계: 필드별 정규식 직접 추출 (최후 수단) ──────────────────────
        # content 필드는 "content": "..." 구조에서 가장 긴 문자열을 추출
        logger.warning("JSON 파싱 전략 1~3 실패 — 정규식 필드 추출 시도 (4단계)")

        def _extract_field(text: str, field: str) -> str:
            # "field": "값" 패턴에서 값 추출 (내부 따옴표 포함 가능)
            pattern = rf'"{re.escape(field)}"\s*:\s*"(.*?)"(?=\s*[,}}])'
            m = re.search(pattern, text, re.DOTALL)
            if m:
                val = m.group(1)
                # 이미 이스케이프된 \\n 은 \n 으로, 리터럴 줄바꿈은 \n으로 정규화
                val = val.replace("\\n", "\n")
                return val
            return ""

        def _extract_tags(text: str) -> list:
            m = re.search(r'"tags"\s*:\s*\[(.*?)\]', text, re.DOTALL)
            if m:
                tags_raw = m.group(1)
                return [t.strip().strip('"') for t in tags_raw.split(",") if t.strip().strip('"')]
            return []

        title        = _extract_field(candidate, "title")
        image_prompt = _extract_field(candidate, "image_prompt")
        tags         = _extract_tags(candidate)

        # content 는 가장 긴 "content": "..." 블록으로 추출
        content_m = re.search(r'"content"\s*:\s*"(.*)', candidate, re.DOTALL)
        content = ""
        if content_m:
            raw_content = content_m.group(1)
            # 닫는 따옴표 위치: "tags" 키가 나오기 직전까지
            end_m = re.search(r'",\s*"(?:tags|image_prompt)"', raw_content, re.DOTALL)
            if end_m:
                content = raw_content[:end_m.start()]
            else:
                # 마지막 " 바로 앞까지
                last_q = raw_content.rfind('"')
                content = raw_content[:last_q] if last_q > 0 else raw_content
            content = content.replace("\\n", "\n").replace('\\"', '"')

        if title and content:
            logger.warning(f"4단계 정규식 추출 성공 (title={title[:30]}..., content={len(content)}자)")
            return {
                "title":        title,
                "content":      content,
                "tags":         tags,
                "image_prompt": image_prompt,
            }

        # 4단계도 실패하면 원래 json.loads 예외를 다시 발생
        return json.loads(raw)
