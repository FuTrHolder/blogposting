"""
블로그 글 생성 모듈
Google Gemini API를 사용해 3,000자 내외의 한글 블로그 포스팅을 생성합니다.

모드:
  morning : 미국 전일 증시 마감 리뷰 (오전 9시 포스팅)
  evening : 전일 정규장 리뷰 + 애프터마켓~프리장 이슈 + 당일 경제지표/실적
"""

import json
import logging
import time
import urllib.request
import urllib.error
import re

logger = logging.getLogger(__name__)


# ── Gemini 모델 우선순위 ─────────────────────────────────────────────────────
#
# gemini-2.0-flash는 더 이상 사용하지 않습니다.
# 최신 모델부터 순서대로 시도하고, 일시적인 429/5xx 오류가 발생하면
# 같은 모델에서 재시도한 뒤 다음 모델로 넘어갑니다.
#
GEMINI_MODELS = [
    "gemini-3.6-flash",
    "gemini-3.5-flash",
    "gemini-2.5-flash",
]


# 재시도 가능한 HTTP 상태코드
_RETRYABLE_CODES = {429, 500, 502, 503, 504}


# Gemini 응답에서 image_prompt 필드가 누락됐을 때 사용할 안전한 기본값
FALLBACK_IMAGE_PROMPT = {
    "morning": (
        "quiet Wall Street financial district after market close, "
        "calm analytical mood, stock exchange building, soft morning light"
    ),
    "evening": (
        "tense premarket trading floor, New York financial district at night, "
        "dynamic energetic mood, city skyline with stock market data overlay"
    ),
}


# ── 목차(TOC) 플레이스홀더 ──────────────────────────────────────────────────
TOC_HTML_SNIPPET = (
    '<div class="index_toc">\n'
    '<p data-ke-size="size16">목차</p>\n'
    '<ul id="toc" style="list-style-type: disc;" '
    'data-ke-list-type="disc"></ul>\n'
    '</div>'
)


# ── 고정 면책조항 ────────────────────────────────────────────────────────────
#
# Gemini가 어떤 표현을 생성하더라도 최종 HTML에서는 이 형식을 유지합니다.
#
DISCLAIMER_HTML = (
    '<blockquote data-ke-style="style3">'
    '<p>본 콘텐츠는 제공된 정보를 바탕으로 작성되었으며, '
    '투자 권유를 목적으로 하지 않습니다. '
    '투자 결정에 따른 책임은 본인에게 있습니다.</p>'
    '</blockquote>'
)


def _gemini_url(model: str) -> str:
    return (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"{model}:generateContent"
    )


# ── 공통 HTML 형식 규칙 ─────────────────────────────────────────────────────
_HTML_FORMAT_RULE = """
────────────────────────────────────────
HTML 형식 규칙 (반드시 준수)
────────────────────────────────────────
본문(content)은 마크다운이 아니라 순수 HTML로 작성합니다.
티스토리 HTML 편집기에 그대로 붙여넣을 수 있는 HTML 조각이어야 합니다.

① 문서 뼈대 태그(<html>, <head>, <body>)는 쓰지 않습니다.

② 큰 제목은 <h2>, 소제목은 <h3>를 사용합니다.
<h1>은 쓰지 않습니다.

예:
<h2>주요 지수 마감 결과</h2>

③ 모든 본문 단락은 <p>...</p>로 감쌉니다.

④ 리스트는 <ul><li>...</li></ul>을 사용합니다.
항목 안의 굵은 글씨는 <strong>으로 감쌉니다.

예:
<ul>
<li><strong>S&P 500:</strong> 0.15% 상승</li>
</ul>

⑤ 뉴스·이슈 항목은 <h3> 소제목 + 바로 다음 <p> 본문으로 처리합니다.

⑥ 본문 안에 <img> 태그를 절대 삽입하지 않습니다.
마크다운 이미지 문법도 사용하지 않습니다.

⑦ 면책 조항은 글의 마지막 마무리 문단 바로 다음에 반드시 아래 형식으로
작성합니다.

<blockquote data-ke-style="style3"><p>본 콘텐츠는 제공된 정보를 바탕으로 작성되었으며,
투자 권유를 목적으로 하지 않습니다. 투자 결정에 따른 책임은 본인에게 있습니다.</p></blockquote>

중요:
- 반드시 data-ke-style="style3" 속성을 포함합니다.
- 일반적인 <blockquote>를 사용하지 않습니다.
- 다른 data-ke-style 값을 사용하지 않습니다.
- 면책조항은 반드시 글의 마지막 HTML 요소여야 합니다.

⑧ 사진 제공 크레딧은 추가하지 않습니다.

⑨ 시장이 상승도 하락도 아닌 애매한 상태를 표현할 때
"혼조" "혼조세"만 반복하지 않습니다.

다음 표현을 상황에 맞게 다양하게 사용하세요.

- 방향성 없이 엇갈린 흐름
- 업종별로 희비가 갈리는 모습
- 뚜렷한 방향 없이 등락을 거듭
- 종목별로 온도차를 보임

⑩ 허용 HTML 태그는 다음과 같습니다.

<h2>
<h3>
<p>
<ul>
<li>
<strong>
<blockquote data-ke-style="style3">

임의의 class/style/속성을 추가하지 않습니다.

단, 면책조항의 <blockquote>에는 반드시
data-ke-style="style3"을 사용합니다.

⑪ 본문 최상단에 목차 마크업이 코드에서 자동 삽입됩니다.
직접 "목차" 또는 <ul id="toc">를 작성하지 마세요.

────────────────────────────────────────
"""


# ── 시간 표기 규칙 ──────────────────────────────────────────────────────────
_TIME_FORMAT_RULE = """
────────────────────────────────────────
시간 표기 규칙 (반드시 준수)
────────────────────────────────────────
본문에 등장하는 모든 시간은 한국 시간(KST) 기준으로 변환합니다.

① 미국 동부시간(ET) → KST
- EDT: +13시간
- EST: +14시간

② UTC → KST: +9시간

③ 표기 형식:
"날짜 시간(KST)"

예:
"4월 1일 오후 11시 30분(KST)"
"4월 2일 오전 3시(KST)"

④ 날짜가 바뀌는 경우 날짜를 반드시 명시합니다.

⑤ 미국 경제지표·실적 발표 시각은 KST로 변환하고
원본 ET 시각을 괄호에 병기합니다.

예:
"4월 1일 오후 9시 30분(KST) [미국 오전 8시 30분 ET]"

⑥ 애프터마켓·프리마켓 시간대도 KST 기준으로 작성합니다.

────────────────────────────────────────
"""


# ── 제목 전략 ───────────────────────────────────────────────────────────────
_TITLE_STRATEGY = """
────────────────────────────────────────
제목 작성 전략
────────────────────────────────────────
제목은 아래 조건을 모두 충족해야 합니다.

① 날짜 포함
② 미국증시, S&P500, 나스닥, 다우존스, 주가, 증시 등 핵심 키워드 포함
③ 후킹 요소 포함
④ 60자 이내

후킹 유형:
- 숫자/수치
- 반전/의외성
- 긴급성
- 궁금증
- 투자자 관심사

예:
"2026년 3월 19일 미국 증시: 나스닥 급락, 반등 가능할까?"

────────────────────────────────────────
"""


# ── 오전 시스템 프롬프트 ────────────────────────────────────────────────────
SYSTEM_MORNING = (
    """
당신은 미국 주식 시장을 분석하는 전문 블로그 작가입니다.

이 포스팅은 오전 9시 발행 원고로,
미국 전일 증시 마감을 리뷰하는 내용입니다.

작성 규칙:
- 전체 글자 수: 2,800~3,200자
- 어조: 부드럽고 자연스러운 한국어
- 초보자도 이해하기 쉽게 작성
- 투자 권유가 아닌 정보 제공 목적 유지

구조:
1. 서론
2. <h2>주요 지수 마감 결과</h2>
3. <h2>어제의 핵심 뉴스 & 시장 반응</h2>
4. <h2>섹터별 마감 흐름</h2>
5. <h2>투자자 심리 & Fear & Greed 지수</h2>
6. <h2>오늘(미국 시간) 주목해야 할 포인트</h2>
7. 마무리

마지막에는 반드시 면책조항을 작성합니다.
"""
    + _HTML_FORMAT_RULE
    + _TIME_FORMAT_RULE
    + _TITLE_STRATEGY
    + """
반드시 아래 JSON 형식으로만 응답하세요.

{
  "title": "제목",
  "content": "HTML 본문",
  "tags": ["태그1", "태그2", "태그3", "태그4", "태그5"],
  "image_prompt": "Stable Diffusion용 영문 이미지 프롬프트"
}

JSON 출력 규칙:
- 마크다운 코드블록을 사용하지 않습니다.
- content 내부의 큰따옴표는 JSON 규칙에 맞게 이스케이프합니다.
- content 내부 줄바꿈은 \\n으로 처리합니다.
- JSON 전체가 하나의 유효한 객체여야 합니다.
"""
)


# ── 저녁 시스템 프롬프트 ────────────────────────────────────────────────────
SYSTEM_EVENING = (
    """
당신은 미국 주식 시장을 분석하는 전문 블로그 작가입니다.

이 포스팅은 한국 시간 저녁 9시 발행 원고입니다.

반드시 다음 순서로 작성합니다.

1. 전일 미국 정규장 마감 결과
2. 전일 애프터마켓 이후 현재 프리장까지의 주요 이슈
3. 오늘 밤 발표 예정인 경제지표 및 기업 실적
4. 현재 프리마켓/선물 동향
5. 오늘 밤 강세·약세 시나리오
6. 마무리

중요:
- 오늘의 날짜는 [분석 기준 시각 - 미국 뉴욕]을 기준으로 판단합니다.
- D+0이 아닌 경제지표나 실적을 오늘 발표 예정이라고 표현하지 않습니다.
- 제공된 사실 데이터에 없는 기업의 실적 발표일을 추측하지 않습니다.
- 투자 권유가 아닌 정보 제공 목적을 유지합니다.

마지막에는 반드시 면책조항을 작성합니다.
"""
    + _HTML_FORMAT_RULE
    + _TIME_FORMAT_RULE
    + _TITLE_STRATEGY
    + """
반드시 아래 JSON 형식으로만 응답하세요.

{
  "title": "제목",
  "content": "HTML 본문",
  "tags": ["태그1", "태그2", "태그3", "태그4", "태그5"],
  "image_prompt": "Stable Diffusion용 영문 이미지 프롬프트"
}

JSON 출력 규칙:
- 마크다운 코드블록을 사용하지 않습니다.
- content 내부의 큰따옴표는 JSON 규칙에 맞게 이스케이프합니다.
- content 내부 줄바꿈은 \\n으로 처리합니다.
- JSON 전체가 하나의 유효한 객체여야 합니다.
"""
)


def _reference_time_block(
    korean_datetime_str: str,
    ny_reference_str: str,
) -> str:

    if not korean_datetime_str or not ny_reference_str:
        return ""

    ny_date_only = ny_reference_str.split(" ")[0]

    return (
        "────────────────────────────────────────\n"
        "기준 시각\n"
        "────────────────────────────────────────\n"
        f"[글 작성 시각 - 한국(KST)] {korean_datetime_str}\n"
        f"[분석 기준 시각 - 미국 뉴욕] {ny_reference_str}\n\n"
        f"미국 뉴욕 기준 오늘 날짜는 {ny_date_only}입니다.\n"
        "상대적 날짜 표현은 반드시 이 날짜를 기준으로 판단합니다.\n"
        "────────────────────────────────────────\n\n"
    )


_MIXED_MARKET_SYNONYMS = [
    "혼조세를 보였습니다.",
    "방향성 없이 엇갈린 흐름을 나타냈습니다.",
    "업종별로 희비가 갈리는 모습이었습니다.",
    "뚜렷한 방향 없이 등락을 거듭했습니다.",
    "종목·업종별로 온도차를 보였습니다.",
]


def _pick_mixed_market_phrase(
    korean_date: str,
    prefix: str,
) -> str:

    idx = (
        sum(ord(c) for c in korean_date)
        % len(_MIXED_MARKET_SYNONYMS)
    )

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
        f"포스팅 작성 날짜(한국 시간): {korean_date}\n"
        f"리뷰 대상 미국 증시 마감 날짜: {us_market_date}\n\n"
        "[전일 마감 지수]\n"
    )

    for name, data in market_data.items():

        if name == "fear_greed" or not data:
            continue

        market_text += (
            f"- {name}: {data.get('price', '')} "
            f"({data.get('change', '')}, "
            f"{data.get('change_pct', '')}) "
            f"{data.get('direction', '')}\n"
        )

    fg = market_data.get("fear_greed", {})

    if fg.get("score") is not None:
        market_text += (
            f"\n[Fear & Greed 지수] "
            f"{fg.get('score')}/100 "
            f"({fg.get('rating', '')})\n"
        )

    news_text = "\n[전일 주요 뉴스]\n"

    for i, news in enumerate(news_list, 1):

        sentiment = news.get("sentiment", "")
        suffix = f" [{sentiment}]" if sentiment else ""

        news_text += (
            f"{i}. {news.get('title', '')}{suffix}\n"
        )

        if news.get("summary"):
            summary = (
                news["summary"][:200]
                .replace("\n", " ")
            )

            news_text += f"   → {summary}\n"

    directions = [
        v.get("direction", "")
        for k, v in market_data.items()
        if k != "fear_greed"
        and v
        and isinstance(v, dict)
    ]

    up_count = sum(
        1 for d in directions
        if "상승" in d
    )

    down_count = sum(
        1 for d in directions
        if "하락" in d
    )

    if up_count > down_count:
        hint = "전일 미국 시장은 전반적으로 상승 마감했습니다."
    elif down_count > up_count:
        hint = "전일 미국 시장은 전반적으로 하락 마감했습니다."
    else:
        hint = _pick_mixed_market_phrase(
            korean_date,
            "전일 미국 시장은",
        )

    fact_block_text = (
        f"\n{fact_reference_block}\n"
        if fact_reference_block
        else ""
    )

    return (
        f"{market_text}"
        f"{news_text}\n"
        f"[시장 요약] {hint}\n"
        f"{fact_block_text}\n"
        "위 데이터를 바탕으로 전일 미국 증시 마감 리뷰 "
        "블로그 포스팅을 작성하세요.\n"
        "반드시 지정된 JSON 형식으로만 응답하세요."
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
        f"포스팅 작성 날짜(한국 시간): {korean_date}\n"
        f"전일 미국 정규장 날짜: {us_market_date}\n\n"
        "[현재 선물/프리마켓 지수]\n"
    )

    for name, data in market_data.items():

        if name == "fear_greed" or not data:
            continue

        market_text += (
            f"- {name}: {data.get('price', '')} "
            f"({data.get('change', '')}, "
            f"{data.get('change_pct', '')}) "
            f"{data.get('direction', '')}\n"
        )

    fg = market_data.get("fear_greed", {})

    if fg.get("score") is not None:
        market_text += (
            f"\n[Fear & Greed 지수] "
            f"{fg.get('score')}/100 "
            f"({fg.get('rating', '')})\n"
        )

    news_text = "\n[수집된 주요 뉴스 & 이슈]\n"

    for