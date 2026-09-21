"""
fact_reference.py

블로그 원고에 주입할 "검증된 사실 기준표(Fact Reference)"를 구성합니다.

데이터 우선순위
==============

① 미국 경제지표 / 이벤트
   1순위: Cloudflare Calendar API
          https://blogposting.pages.dev/api/calendar?days=7

          응답 데이터는 한국시간(KST) 기준으로 제공되므로
          date_kst / time_kst 값을 미국시간으로 재변환하지 않습니다.

   2순위: 기존 코드에 포함된 공식/고정 경제지표 일정
          - FOMC
          - 고용보고서
          - CPI
          - PPI
          - ECI

② 기업 실적 발표
   1순위: Earnings Dashboard
          https://earnings-dashboard.dmenc-hjw.workers.dev/

          실제 JSON 스키마:
            id
            ticker
            fiscal_quarter
            report_date
            report_time
            eps_estimate
            eps_actual
            revenue_estimate
            revenue_actual
            source_agreement_count
            name_ko
            name_en
            market_cap

          report_date는 대시보드에서 사용하는 날짜를 그대로 사용합니다.
          임의로 미국시간 → KST 변환하지 않습니다.

          report_time:
            bmo = 장 시작 전
            amc = 장 마감 후
            ""  = 발표 시점 미정

   2순위: Alpha Vantage EARNINGS_CALENDAR
          기존 fallback 로직

③ 모든 외부 API 실패
   → 원고 생성 파이프라인을 중단하지 않습니다.
   → 확인 가능한 기존 하드코딩 일정만 사용합니다.
   → 확인되지 않은 기업 실적 발표일은 구체적으로 생성하지 않도록
      Fact Reference에서 명시합니다.

중요
====

이 모듈은 "정답을 추정"하지 않습니다.

특히 기업 실적의 report_date는
미국 현지시간 기준 날짜라고 가정하여 +9시간 등의 변환을 하지 않습니다.

Earnings Dashboard가 제공한 report_date를 그대로 사용합니다.
"""

import csv
import io
import logging
import re
from datetime import datetime, date, timedelta, timezone

import requests


logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# 공통
# ─────────────────────────────────────────────────────────────────────────────

KST = timezone(timedelta(hours=9))

CALENDAR_API_URL = (
    "https://blogposting.pages.dev/api/calendar?days=7"
)

EARNINGS_DASHBOARD_URL = (
    "https://earnings-dashboard.dmenc-hjw.workers.dev/"
)

DEFAULT_HTTP_TIMEOUT = 20


# ─────────────────────────────────────────────────────────────────────────────
# ① 기존 공식/고정 경제지표 일정
#
# 새로운 Calendar API가 실패했을 때 사용하는 fallback입니다.
# ─────────────────────────────────────────────────────────────────────────────

_LAST_VERIFIED = "2026-07-22"


# FOMC 회의
# 성명 발표일 = 이틀째 날
FOMC_2026 = [
    {
        "start": "2026-01-27",
        "statement_date": "2026-01-28",
        "label": "1월 FOMC",
    },
    {
        "start": "2026-03-17",
        "statement_date": "2026-03-18",
        "label": "3월 FOMC",
    },
    {
        "start": "2026-04-28",
        "statement_date": "2026-04-29",
        "label": "4월 FOMC",
    },
    {
        "start": "2026-06-16",
        "statement_date": "2026-06-17",
        "label": "6월 FOMC",
    },
    {
        "start": "2026-07-28",
        "statement_date": "2026-07-29",
        "label": "7월 FOMC",
    },
    {
        "start": "2026-09-15",
        "statement_date": "2026-09-16",
        "label": "9월 FOMC",
    },
    {
        "start": "2026-10-27",
        "statement_date": "2026-10-28",
        "label": "10월 FOMC",
    },
    {
        "start": "2026-12-08",
        "statement_date": "2026-12-09",
        "label": "12월 FOMC",
    },
]


EMPLOYMENT_SITUATION_2026 = [
    "2026-01-09",
    "2026-02-06",
    "2026-03-06",
    "2026-04-03",
    "2026-05-08",
    "2026-06-05",
    "2026-07-02",
    "2026-08-07",
    "2026-09-04",
    "2026-10-02",
    "2026-11-06",
    "2026-12-04",
]


CPI_2026 = [
    "2026-01-13",
    "2026-02-11",
    "2026-03-11",
    "2026-04-10",
    "2026-05-12",
    "2026-06-10",
    "2026-07-14",
    "2026-08-12",
    "2026-09-11",
    "2026-10-14",
    "2026-11-10",
    "2026-12-10",
]


PPI_2026 = [
    "2026-01-14",
    "2026-02-12",
    "2026-03-12",
    "2026-04-14",
    "2026-05-13",
    "2026-06-11",
    "2026-07-15",
    "2026-08-13",
    "2026-09-10",
    "2026-10-15",
    "2026-11-13",
    "2026-12-15",
]


ECI_2026 = [
    "2026-01-30",
    "2026-04-30",
    "2026-07-31",
    "2026-10-30",
]


FALLBACK_MACRO_INDICATORS = [
    {
        "name": "FOMC 회의(금리 결정)",
        "keywords": [
            "FOMC",
            "연준 회의",
            "금리 결정",
            "연방공개시장위원회",
        ],
        "dates": [
            item["statement_date"]
            for item in FOMC_2026
        ],
    },
    {
        "name": "고용보고서(비농업고용지표)",
        "keywords": [
            "고용보고서",
            "비농업고용",
            "비농업 고용",
            "실업률 발표",
            "고용지표",
        ],
        "dates": EMPLOYMENT_SITUATION_2026,
    },
    {
        "name": "CPI(소비자물가지수)",
        "keywords": [
            "CPI",
            "소비자물가지수",
            "소비자물가",
        ],
        "dates": CPI_2026,
    },
    {
        "name": "PPI(생산자물가지수)",
        "keywords": [
            "PPI",
            "생산자물가지수",
            "생산자물가",
        ],
        "dates": PPI_2026,
    },
    {
        "name": "고용비용지수",
        "keywords": [
            "고용비용지수",
            "ECI",
        ],
        "dates": ECI_2026,
    },
]


# 기존 코드와의 호환성을 위해 공개 이름으로 유지합니다.
MACRO_INDICATORS = FALLBACK_MACRO_INDICATORS


# ─────────────────────────────────────────────────────────────────────────────
# ② 기업 워치리스트
# ─────────────────────────────────────────────────────────────────────────────

WATCHLIST_ALIASES = {
    "NVDA": [
        "엔비디아",
        "NVIDIA",
        "Nvidia",
    ],
    "AAPL": [
        "애플",
        "Apple",
    ],
    "MSFT": [
        "마이크로소프트",
        "Microsoft",
    ],
    "GOOGL": [
        "구글",
        "알파벳",
        "Google",
        "Alphabet",
    ],
    "AMZN": [
        "아마존",
        "Amazon",
    ],
    "META": [
        "메타",
        "Meta",
    ],
    "TSLA": [
        "테슬라",
        "Tesla",
    ],
    "AVGO": [
        "브로드컴",
        "Broadcom",
    ],
    "AMD": [
        "AMD",
        "에이엠디",
    ],
    "NFLX": [
        "넷플릭스",
        "Netflix",
    ],
    "ORCL": [
        "오라클",
        "Oracle",
    ],
    "CRM": [
        "세일즈포스",
        "Salesforce",
    ],
    "ADBE": [
        "어도비",
        "Adobe",
    ],
    "INTC": [
        "인텔",
        "Intel",
    ],
    "QCOM": [
        "퀄컴",
        "Qualcomm",
    ],
    "PYPL": [
        "페이팔",
        "PayPal",
    ],
    "DIS": [
        "디즈니",
        "Disney",
    ],
    "JPM": [
        "JP모건",
        "JP모건체이스",
        "JPMorgan",
    ],
    "V": [
        "비자카드",
        "Visa",
    ],
    "MA": [
        "마스터카드",
        "Mastercard",
    ],
    "COST": [
        "코스트코",
        "Costco",
    ],
    "WMT": [
        "월마트",
        "Walmart",
    ],
    "HD": [
        "홈디포",
        "Home Depot",
    ],
    "UNH": [
        "유나이티드헬스",
        "UnitedHealth",
    ],
    "XOM": [
        "엑슨모빌",
        "ExxonMobil",
        "엑손모빌",
    ],
    "BA": [
        "보잉",
        "Boeing",
    ],
}


# fact_checker.py가 직접 import하므로 이름을 유지합니다.
EARNINGS_KEYWORDS = [
    "실적",
    "어닝스",
    "earnings",
    "실적 발표",
    "실적발표",
]


IMMINENT_WORDS = [
    "오늘",
    "오늘 밤",
    "오늘밤",
    "당일",
    "이번 주",
    "이번주",
    "장 마감 후",
    "장마감 후",
    "정규장 마감 후",
    "곧",
    "임박",
]


# ─────────────────────────────────────────────────────────────────────────────
# [신규] 주요 인물/직책 참조표
#
# LLM(Gemini)은 학습 데이터에 오래 각인된 인물(예: "연준 의장 = 파월")을
# 실제로 교체된 뒤에도 관성적으로 계속 언급하는 경향이 있습니다. 날짜와
# 달리 이런 "인물 정보"는 코드 어디에도 하드코딩되어 있지 않고 전적으로
# Gemini의 사전지식에 의존하기 때문에, 실제 교체가 있었는데도 원고에서
# 계속 옛 인물 이름이 나오는 문제가 발생할 수 있습니다 (2026-09-16 확인:
# 연준 의장이 케빈 워시로 교체되었는데도 원고에 "파월 의장"이 언급됨).
#
# 이 표는 그 문제를 막기 위한 참조 데이터입니다. 블로그 본문
# (content_generator.py), SNS 콘텐츠(marketing/content_adapter/adapter.py),
# 영상 나레이션(marketing/video_generator/generator.py) 3곳 모두 이 표
# 하나를 공유해서 프롬프트에 안내문을 넣고, 발행 직전에는
# scrub_outdated_officials()로 이전 인물 이름이 남아있지 않은지 최종
# 교정합니다.
#
# 업데이트 방법: 직책이 바뀌면 이 표에 항목만 추가/수정하면 됩니다.
# outdated_aliases에는 "의장"/"체어" 같은 직책 접미사를 붙이지 말고
# 이름만 넣으세요 (예: "파월 의장"이 아니라 "파월") — 그래야
# scrub_outdated_officials()가 이름만 바꿔서 "파월 의장" → "케빈 워시
# 의장"처럼 문장 구조를 그대로 살릴 수 있습니다.
# ─────────────────────────────────────────────────────────────────────────────

_LAST_VERIFIED_OFFICIALS = "2026-09-16"

CURRENT_OFFICIALS = {
    "fed_chair": {
        "role_ko": "연준(Fed) 의장",
        "current_name": "케빈 워시",
        "current_aliases": ["케빈 워시", "Kevin Warsh"],
        "outdated_aliases": ["제롬 파월", "파월", "Jerome Powell", "Powell"],
    },
}


def officials_reference_text() -> str:
    """
    프롬프트에 주입할 '현재 주요 인물' 안내문을 만듭니다. 외부 API 호출이
    전혀 없는 순수 하드코딩 조회라 부담 없이 매 호출마다 사용할 수 있습니다.
    """
    lines = ["[현재 주요 인물 — 반드시 이 이름을 사용, 이전 인물 언급 금지]"]
    for info in CURRENT_OFFICIALS.values():
        outdated = ", ".join(info["outdated_aliases"][:2])
        lines.append(
            f"  - {info['role_ko']}: {info['current_name']} "
            f"(※ '{outdated}' 등은 이미 교체된 이전 인물이므로 현재 시점 "
            f"기사에서 언급하지 마세요)"
        )
    return "\n".join(lines)


def _has_batchim(word: str) -> bool:
    """
    단어 마지막 글자에 받침이 있는지 확인합니다 (조사 은/는 등 선택에 사용).
    한글 음절이 아니면(영문 등) 받침이 없다고 간주합니다.
    """
    if not word:
        return False

    ch = word[-1]
    code = ord(ch)

    if 0xAC00 <= code <= 0xD7A3:
        return (code - 0xAC00) % 28 != 0

    return False


# 받침 유무에 따라 짝을 이루는 조사들. scrub_outdated_officials()가 이름
# 뒤에 바로 붙은 조사를 새 이름의 받침 유무에 맞게 함께 교정하는 데 사용
# (예: "파월은" → "케빈 워시는" — 받침 없는 '워시'에는 '는'이 맞음).
_PARTICLE_PAIRS = {
    "은": ("은", "는"),
    "는": ("은", "는"),
    "이": ("이", "가"),
    "가": ("이", "가"),
    "을": ("을", "를"),
    "를": ("을", "를"),
    "과": ("과", "와"),
    "와": ("과", "와"),
}


# 이름 주변에 이 단어들이 있으면 "현재 인물로 잘못 언급"이 아니라 "전임자에
# 대한 정당한 역사적 언급"으로 판단해 치환하지 않습니다. fact_checker.py가
# IMMINENT_WORDS로 "임박 표현"을 감지하는 것과 동일한 문맥 창(window) 방식을
# 사용합니다 — 형태소 분석기 없이도 오탐을 크게 줄일 수 있는 실용적인 절충안.
HISTORICAL_CONTEXT_MARKERS = [
    "전임", "전직", "전(前)", "前 ",
    "전 의장", "전 연준",
    "이었던", "였던", "이었습니다", "였습니다",
    "물러난", "물러났", "사임했", "사임한",
    "퇴임했", "퇴임한", "퇴임 후",
    "역대", "역임했", "역임한",
    "재임 시절", "재임 당시", "재임 기간", "재임했",
    "이끌던", "이끌었던", "지냈던",
]

# 별칭 앞뒤로 이만큼의 글자를 문맥으로 봅니다 (fact_checker.py의
# _CONTEXT_WINDOW=40과 비슷한 취지이나, 조사 하나 차이로 오탐이 늘지 않게
# 조금 더 좁게 잡았습니다).
_HISTORICAL_CONTEXT_WINDOW = 20


def _looks_historical(text: str, start: int, end: int) -> bool:
    """
    별칭이 발견된 위치(start~end) 앞뒤 문맥에 '전임/과거' 신호가 있는지
    확인합니다. 있으면 이름을 그대로 두는 것이 맞습니다 — 예를 들어
    "전임 연준 의장이었던 파월은 2018년부터 재임했습니다"를 "케빈 워시는
    2018년부터 재임했습니다"로 바꿔버리면, 교정 전보다 더 나쁜(엉뚱한
    사람에게 재임 이력을 붙이는) 오류가 됩니다.
    """
    lo = max(0, start - _HISTORICAL_CONTEXT_WINDOW)
    hi = min(len(text), end + _HISTORICAL_CONTEXT_WINDOW)
    window = text[lo:hi]
    return any(marker in window for marker in HISTORICAL_CONTEXT_MARKERS)


def scrub_outdated_officials(text: str) -> tuple[str, list[dict]]:
    """
    생성된 텍스트에서 이미 교체된 인물의 이름을 발견하면 현재 인물 이름으로
    치환합니다. Gemini가 프롬프트 지시를 놓쳐도 항상 적용되는 결정론적
    최종 안전망입니다 (날짜 자동교정과 동일한 철학 — 그럴듯하게 문장을
    새로 만들지 않고 이름만 정확히 치환).

    다만 무조건 치환하지는 않습니다. "전임 연준 의장 파월"처럼 이미 과거
    인물로 정확하게 언급된 경우까지 이름을 바꾸면, 원래 오류(현재 인물처럼
    잘못 언급)보다 더 나쁜 오류(전임자의 이력을 현재 인물에게 잘못 붙이는
    것)가 됩니다. 그래서 이름 주변 문맥에 HISTORICAL_CONTEXT_MARKERS(전임,
    재임 시절 등)가 있으면 치환하지 않고 그대로 둡니다 — 형태소 분석 없이
    적용 가능한 실용적 절충안이며, 완벽하지 않을 수 있습니다(예: 신호
    단어 없이 자연스럽게 과거를 서술한 문장은 여전히 치환될 수 있습니다).

    긴 별칭부터 치환해 "제롬 파월"이 "파월"보다 먼저 처리되도록 합니다.
    이름 뒤에 직책이 붙는 경우("파월 의장" → "케빈 워시 의장")는 그대로
    자연스럽게 이어지지만, 조사가 이름에 바로 붙는 경우("파월은" →
    "케빈 워시는")는 받침 유무가 달라지면 어색해질 수 있어 조사(은/는,
    이/가, 을/를, 과/와)도 새 이름에 맞게 함께 교정합니다.

    Returns:
        (교정된 텍스트, 적용된 교정 내역 리스트). 각 내역은 action이
        "corrected"(실제 치환) 또는 "preserved_historical"(역사적 언급으로
        판단해 보존)입니다. 빈 텍스트를 넣으면 그대로 반환합니다.
    """
    if not text:
        return text, []

    applied: list[dict] = []

    for info in CURRENT_OFFICIALS.values():
        new_name = info["current_name"]
        new_batchim = _has_batchim(new_name)

        for alias in sorted(info["outdated_aliases"], key=len, reverse=True):
            if alias not in text:
                continue

            pattern = re.compile(
                re.escape(alias) + r"(은|는|이|가|을|를|과|와)?"
            )

            source_text = text
            counts = {"corrected": 0, "preserved": 0}

            def _replace(
                m: "re.Match",
                _source: str = source_text,
                _counts: dict = counts,
            ) -> str:
                if _looks_historical(_source, m.start(), m.end()):
                    _counts["preserved"] += 1
                    return m.group(0)

                _counts["corrected"] += 1
                particle = m.group(1)
                if not particle:
                    return new_name
                with_batchim, without_batchim = _PARTICLE_PAIRS[particle]
                return new_name + (with_batchim if new_batchim else without_batchim)

            text = pattern.sub(_replace, text)

            if counts["corrected"]:
                applied.append({
                    "role": info["role_ko"],
                    "from": alias,
                    "to": new_name,
                    "action": "corrected",
                    "count": counts["corrected"],
                })

            if counts["preserved"]:
                applied.append({
                    "role": info["role_ko"],
                    "from": alias,
                    "to": alias,
                    "action": "preserved_historical",
                    "count": counts["preserved"],
                })

    return text, applied


def format_official_fix_log(fix: dict) -> str:
    """
    scrub_outdated_officials()가 반환한 교정 내역 한 건을 로그 메시지로
    변환합니다. action("corrected"/"preserved_historical")에 따라 메시지를
    구분해서, 블로그 본문·SNS 콘텐츠·영상 나레이션 3곳의 파이프라인이 동일한
    로그 형식을 공유하도록 합니다.
    """
    count = fix.get("count", 1)
    count_suffix = f" ({count}건)" if count > 1 else ""

    if fix.get("action") == "preserved_historical":
        return (
            f"[인물 정보] '{fix['from']}' 언급이 과거/전임 맥락으로 판단되어 "
            f"그대로 유지함{count_suffix} ({fix['role']}) — 오탐이면(실제로는 "
            f"현재 인물을 가리킨 것이면) 원문을 확인하세요"
        )

    return (
        f"[인물 정보 자동 교정] '{fix['from']}' → '{fix['to']}'{count_suffix} "
        f"({fix['role']})"
    )


# ─────────────────────────────────────────────────────────────────────────────
# 공통 유틸
# ─────────────────────────────────────────────────────────────────────────────

def _parse_iso(value: str) -> date | None:
    """
    YYYY-MM-DD 문자열을 date로 변환합니다.
    """
    try:
        return datetime.strptime(
            str(value)[:10],
            "%Y-%m-%d",
        ).date()
    except (
        ValueError,
        TypeError,
    ):
        return None


def _safe_float(value):
    try:
        return float(value)
    except (
        TypeError,
        ValueError,
    ):
        return None


def _safe_int(value):
    try:
        return int(value)
    except (
        TypeError,
        ValueError,
    ):
        return None


def _upcoming(
    dates: list[str],
    today: date,
    window_days: int,
) -> list[str]:
    """
    today 기준 -3일 ~ +window_days일 사이의 날짜만 추립니다.

    -3일을 허용하는 이유:
    API 지연이나 포스팅 작성 시점의 경계 문제 때문에
    직전 며칠의 발표일도 fact checker에서 확인할 수 있도록 하기 위함입니다.
    """
    out = []

    for value in dates:
        parsed = _parse_iso(value)

        if parsed is None:
            continue

        delta = (
            parsed - today
        ).days

        if -3 <= delta <= window_days:
            out.append(
                parsed.isoformat()
            )

    return sorted(set(out))


def _format_report_time(report_time: str) -> str:
    """
    Earnings Dashboard의 실제 report_time 값을
    사람이 읽는 한국어 표현으로 변환합니다.

    bmo = before market open
    amc = after market close
    ""  = 시간 미확인
    """
    value = str(
        report_time or ""
    ).strip().lower()

    if value == "bmo":
        return "장 시작 전"

    if value == "amc":
        return "장 마감 후"

    return "발표 시점 미정"


# ─────────────────────────────────────────────────────────────────────────────
# ③ Cloudflare Calendar API
# ─────────────────────────────────────────────────────────────────────────────

def fetch_macro_calendar(
    timeout: int = DEFAULT_HTTP_TIMEOUT,
) -> list[dict]:
    """
    Cloudflare Calendar API에서 향후 7일 경제지표/이벤트를 가져옵니다.

    실제 응답 예:

    {
        "from": "2026-08-26",
        "to": "2026-09-01",
        "count": 30,
        "fetched_at": "...",
        "cached": false,
        "source": "kr.investing.com",
        "events": [
            {
                "datetime_utc": "...",
                "time_kst": "21:30",
                "date_kst": "2026-08-26",
                "weekday_kst": "수",
                "currency": "USD",
                "importance": 3,
                "event": "GDP (QoQ) (2분기)",
                "actual": null,
                "forecast": "1.5%",
                "previous": "2.1%"
            }
        ]
    }

    중요:
    date_kst / time_kst는 이미 한국시간 기준이므로
    UTC 값을 다시 계산하여 사용하지 않습니다.

    실패 시 예외를 밖으로 던지지 않고 빈 list를 반환합니다.
    호출 실패는 전체 원고 생성 실패로 이어지지 않습니다.
    """

    try:
        response = requests.get(
            CALENDAR_API_URL,
            timeout=timeout,
            headers={
                "Accept": "application/json",
                "User-Agent": (
                    "Mozilla/5.0 "
                    "(compatible; blogposting-fact-reference/1.0)"
                ),
            },
        )

        response.raise_for_status()

        payload = response.json()

        if not isinstance(payload, dict):
            logger.warning(
                "경제지표 Calendar API 응답이 객체가 아님"
            )
            return []

        raw_events = payload.get("events")

        if not isinstance(raw_events, list):
            logger.warning(
                "경제지표 Calendar API에 events 배열이 없음"
            )
            return []

        normalized = []

        for event in raw_events:
            if not isinstance(event, dict):
                continue

            currency = str(
                event.get("currency") or ""
            ).strip().upper()

            # 미국 경제지표만 사용합니다.
            if currency != "USD":
                continue

            date_kst = str(
                event.get("date_kst") or ""
            ).strip()

            parsed_date = _parse_iso(date_kst)

            if parsed_date is None:
                continue

            event_name = str(
                event.get("event") or ""
            ).strip()

            if not event_name:
                continue

            time_kst = str(
                event.get("time_kst") or ""
            ).strip()

            weekday_kst = str(
                event.get("weekday_kst") or ""
            ).strip()

            importance = _safe_int(
                event.get("importance")
            )

            normalized.append(
                {
                    "name": event_name,
                    "keywords": [
                        event_name,
                    ],
                    "date": parsed_date.isoformat(),
                    "time_kst": time_kst,
                    "date_kst": parsed_date.isoformat(),
                    "weekday_kst": weekday_kst,
                    "currency": currency,
                    "importance": importance,
                    "actual": event.get("actual"),
                    "forecast": event.get("forecast"),
                    "previous": event.get("previous"),
                    "datetime_utc": event.get(
                        "datetime_utc"
                    ),
                    "source": (
                        "Cloudflare Calendar API"
                    ),
                }
            )

        # 같은 날짜/시간/이벤트가 중복으로 들어오는 경우 제거합니다.
        deduplicated = []
        seen = set()

        for event in normalized:
            key = (
                event["date"],
                event["time_kst"],
                event["name"],
            )

            if key in seen:
                continue

            seen.add(key)
            deduplicated.append(event)

        deduplicated.sort(
            key=lambda item: (
                item["date"],
                item["time_kst"],
                item["name"],
            )
        )

        logger.info(
            "Cloudflare 경제지표 API 조회 성공: %d개 USD 이벤트",
            len(deduplicated),
        )

        return deduplicated

    except Exception as exc:
        logger.warning(
            "Cloudflare 경제지표 API 조회 실패 "
            "(기존 경제지표 fallback 사용): %s",
            exc,
        )
        return []


# ─────────────────────────────────────────────────────────────────────────────
# Cloudflare Calendar API → fact reference 형태
# ─────────────────────────────────────────────────────────────────────────────

def _build_macro_from_calendar(
    events: list[dict],
    today: date,
    window_days: int,
) -> list[dict]:
    """
    Calendar API의 개별 이벤트를 fact_checker.py가 사용하는
    macro indicator 구조로 변환합니다.

    각 이벤트를 별도 indicator로 취급합니다.

    이렇게 하는 이유:
    같은 날짜에 여러 경제지표가 발표될 수 있고,
    실제 원고에 어떤 지표가 언급됐는지를 정확하게 대조하기 위해서입니다.
    """

    result = []

    for event in events:
        event_date = _parse_iso(
            event.get("date")
        )

        if event_date is None:
            continue

        delta = (
            event_date - today
        ).days

        if not (
            -3 <= delta <= window_days
        ):
            continue

        event_name = str(
            event.get("name") or ""
        ).strip()

        if not event_name:
            continue

        # 실제 이벤트명을 keyword로 사용합니다.
        # 너무 짧은 일반적인 이름은 별도 keyword를 추가하지 않습니다.
        keywords = [
            event_name,
        ]

        result.append(
            {
                "name": event_name,
                "keywords": keywords,
                "dates": [
                    event_date.isoformat()
                ],
                "times": {
                    event_date.isoformat(): event.get(
                        "time_kst"
                    ) or ""
                },
                "weekday_kst": {
                    event_date.isoformat(): event.get(
                        "weekday_kst"
                    ) or ""
                },
                "importance": event.get(
                    "importance"
                ),
                "currency": event.get(
                    "currency"
                ),
                "forecast": event.get(
                    "forecast"
                ),
                "previous": event.get(
                    "previous"
                ),
                "actual": event.get(
                    "actual"
                ),
                "source": event.get(
                    "source"
                ),
            }
        )

    return result


# ─────────────────────────────────────────────────────────────────────────────
# 기존 하드코딩 일정 → fact reference
# ─────────────────────────────────────────────────────────────────────────────

def _build_macro_from_fallback(
    today: date,
    window_days: int,
) -> list[dict]:
    """
    Cloudflare Calendar API 실패 시 기존 하드코딩 일정을 사용합니다.
    """

    result = []

    for indicator in FALLBACK_MACRO_INDICATORS:
        upcoming_dates = _upcoming(
            indicator["dates"],
            today,
            window_days,
        )

        if not upcoming_dates:
            continue

        result.append(
            {
                "name": indicator["name"],
                "keywords": indicator["keywords"],
                "dates": upcoming_dates,
                "source": "hardcoded fallback",
            }
        )

    return result


# ─────────────────────────────────────────────────────────────────────────────
# ④ Earnings Dashboard
# ─────────────────────────────────────────────────────────────────────────────

def fetch_earnings_dashboard(
    timeout: int = DEFAULT_HTTP_TIMEOUT,
) -> dict:
    """
    Earnings Dashboard에서 실제 JSON 데이터를 가져옵니다.

    실제 JSON은 배열입니다.

    [
        {
            "id": "BKR-2026-Q2",
            "ticker": "BKR",
            "fiscal_quarter": "2026-Q2",
            "report_date": "2026-07-26",
            "report_time": "amc",
            "eps_estimate": 0.5073,
            "eps_actual": 0.64,
            "revenue_estimate": 6589803640,
            "revenue_actual": 6742000000,
            "source_agreement_count": 1,
            "name_ko": null,
            "name_en": "Baker Hughes Co",
            "market_cap": 61099089148.274895
        }
    ]

    반환값:

    {
        "NVDA": {
            "date": "...",
            "report_time": "amc",
            ...
        }
    }

    실패 시 {} 반환.
    """

    try:
        response = requests.get(
            EARNINGS_DASHBOARD_URL,
            timeout=timeout,
            headers={
                "Accept": "application/json",
                "User-Agent": (
                    "Mozilla/5.0 "
                    "(compatible; blogposting-fact-reference/1.0)"
                ),
            },
        )

        response.raise_for_status()

        payload = response.json()

        if not isinstance(payload, list):
            logger.warning(
                "Earnings Dashboard 응답이 배열이 아님"
            )
            return {}

        result = {}

        for item in payload:
            if not isinstance(item, dict):
                continue

            ticker = str(
                item.get("ticker") or ""
            ).strip().upper()

            if not ticker:
                continue

            # 블로그 주요 기업만 사용합니다.
            if ticker not in WATCHLIST_ALIASES:
                continue

            report_date = str(
                item.get("report_date") or ""
            ).strip()[:10]

            if _parse_iso(report_date) is None:
                continue

            report_time = str(
                item.get("report_time") or ""
            ).strip().lower()

            # 실제 Dashboard에서 확인된 값만 허용합니다.
            if report_time not in (
                "",
                "bmo",
                "amc",
            ):
                logger.warning(
                    "알 수 없는 report_time 무시: %s / %s",
                    ticker,
                    report_time,
                )
                report_time = ""

            name_ko = item.get(
                "name_ko"
            )

            name_en = item.get(
                "name_en"
            )

            name_ko_text = (
                str(name_ko).strip()
                if name_ko
                else ""
            )

            name_en_text = (
                str(name_en).strip()
                if name_en
                else ""
            )

            name = (
                name_ko_text
                or name_en_text
                or ticker
            )

            normalized = {
                "id": item.get("id"),
                "ticker": ticker,
                "fiscal_quarter": item.get(
                    "fiscal_quarter"
                ),
                "date": report_date,
                "report_date": report_date,
                "report_time": report_time,
                "report_time_label": _format_report_time(
                    report_time
                ),
                "eps_estimate": item.get(
                    "eps_estimate"
                ),
                "eps_actual": item.get(
                    "eps_actual"
                ),
                "revenue_estimate": item.get(
                    "revenue_estimate"
                ),
                "revenue_actual": item.get(
                    "revenue_actual"
                ),
                "source_agreement_count": item.get(
                    "source_agreement_count"
                ),
                "name_ko": name_ko,
                "name_en": name_en,
                "name": name,
                "market_cap": item.get(
                    "market_cap"
                ),
                "source": (
                    "earnings-dashboard"
                ),
            }

            # 같은 종목이 여러 회계분기/레코드로 존재할 수 있으므로
            # 나중에 선택할 수 있도록 목록으로 저장합니다.
            result.setdefault(
                ticker,
                []
            ).append(normalized)

        # 각 티커별로 날짜순 정렬
        for ticker in result:
            result[ticker].sort(
                key=lambda item: (
                    item["date"],
                    item.get("fiscal_quarter") or "",
                )
            )

        logger.info(
            "Earnings Dashboard 조회 성공: "
            "%d개 워치리스트 종목",
            len(result),
        )

        return result

    except Exception as exc:
        logger.warning(
            "Earnings Dashboard 조회 실패 "
            "(Alpha Vantage fallback 사용): %s",
            exc,
        )
        return {}


# ─────────────────────────────────────────────────────────────────────────────
# Earnings Dashboard에서 "현재 기준 가장 적절한 일정" 선택
# ─────────────────────────────────────────────────────────────────────────────

def _select_upcoming_earnings(
    dashboard_data: dict,
    today: date,
    max_days: int = 120,
) -> dict:
    """
    티커별 Earnings Dashboard 레코드 중
    현재 기준 가장 가까운 실적 발표일을 선택합니다.

    과거에 발표된 오래된 레코드가 dashboard에 함께 존재하더라도
    미래의 가장 가까운 일정을 우선합니다.

    단, 데이터 지연 등을 고려해 -3일까지 허용합니다.
    """

    result = {}

    for ticker, records in dashboard_data.items():
        if not isinstance(records, list):
            continue

        candidates = []

        for record in records:
            parsed = _parse_iso(
                record.get("date")
            )

            if parsed is None:
                continue

            delta = (
                parsed - today
            ).days

            if -3 <= delta <= max_days:
                candidates.append(
                    (
                        delta,
                        parsed,
                        record,
                    )
                )

        if not candidates:
            continue

        candidates.sort(
            key=lambda item: (
                item[0],
                item[1],
            )
        )

        result[ticker] = candidates[0][2]

    return result


# ─────────────────────────────────────────────────────────────────────────────
# ⑤ Alpha Vantage 실적 캘린더 fallback
# ─────────────────────────────────────────────────────────────────────────────

def fetch_earnings_calendar(
    alpha_vantage_key: str,
    horizon: str = "3month",
) -> dict:
    """
    Earnings Dashboard 실패 시 기존 Alpha Vantage
    EARNINGS_CALENDAR을 fallback으로 사용합니다.

    Returns:

        {
            "NVDA": {
                "date": "2026-08-26",
                "name": "NVIDIA Corp",
                "report_time": "",
                "report_time_label": "발표 시점 미정",
                "source": "alpha-vantage",
            }
        }

    주의:
    Alpha Vantage fallback에는 Earnings Dashboard처럼
    확정된 KST 시간이 없을 수 있으므로
    report_time은 임의로 생성하지 않습니다.
    """

    if not alpha_vantage_key:
        logger.warning(
            "ALPHA_VANTAGE_API_KEY 없음 "
            "— Alpha Vantage 실적 fallback 건너뜀"
        )
        return {}

    try:
        response = requests.get(
            "https://www.alphavantage.co/query",
            params={
                "function": "EARNINGS_CALENDAR",
                "horizon": horizon,
                "apikey": alpha_vantage_key,
            },
            timeout=DEFAULT_HTTP_TIMEOUT,
        )

        response.raise_for_status()

        text = response.text

        # 오류/한도 초과 시 JSON 응답이 반환될 수 있습니다.
        if text.strip().startswith("{"):
            logger.warning(
                "Alpha Vantage 실적 응답이 CSV가 아님 "
                "(한도 초과/오류 가능성): %s",
                text[:200],
            )
            return {}

        reader = csv.DictReader(
            io.StringIO(text)
        )

        result = {}

        for row in reader:
            symbol = str(
                row.get("symbol") or ""
            ).strip().upper()

            report_date = str(
                row.get("reportDate") or ""
            ).strip()

            if (
                symbol not in WATCHLIST_ALIASES
                or not report_date
            ):
                continue

            if _parse_iso(report_date) is None:
                continue

            candidate = {
                "id": None,
                "ticker": symbol,
                "fiscal_quarter": None,
                "date": report_date,
                "report_date": report_date,
                "report_time": "",
                "report_time_label": "발표 시점 미정",
                "eps_estimate": None,
                "eps_actual": None,
                "revenue_estimate": None,
                "revenue_actual": None,
                "source_agreement_count": None,
                "name_ko": None,
                "name_en": (
                    row.get("name") or ""
                ).strip(),
                "name": (
                    row.get("name") or symbol
                ).strip(),
                "market_cap": None,
                "source": "alpha-vantage",
            }

            # 같은 종목이 여러 번 나오면 더 가까운 날짜를 우선합니다.
            if (
                symbol not in result
                or report_date
                < result[symbol]["date"]
            ):
                result[symbol] = candidate

        logger.info(
            "Alpha Vantage 실적 fallback 조회 완료: "
            "%d개 워치리스트 종목",
            len(result),
        )

        return result

    except Exception as exc:
        logger.warning(
            "Alpha Vantage 실적 fallback 실패 "
            "(계속 진행): %s",
            exc,
        )
        return {}


# ─────────────────────────────────────────────────────────────────────────────
# ⑥ 실적 데이터 선택
# ─────────────────────────────────────────────────────────────────────────────

def _get_earnings_reference(
    alpha_vantage_key: str,
    today: date,
) -> tuple[dict, str]:
    """
    실적 데이터의 최종 source를 결정합니다.

    1. Earnings Dashboard
    2. Alpha Vantage
    3. 빈 dict

    반환:
        (earnings_lookup, source_name)
    """

    dashboard_raw = fetch_earnings_dashboard()

    if dashboard_raw:
        selected = _select_upcoming_earnings(
            dashboard_raw,
            today=today,
            max_days=120,
        )

        if selected:
            logger.info(
                "기업 실적: Earnings Dashboard 사용 "
                "(%d개 종목)",
                len(selected),
            )
            return (
                selected,
                "earnings-dashboard",
            )

    logger.warning(
        "Earnings Dashboard에서 사용 가능한 "
        "향후 실적 일정이 없어 Alpha Vantage fallback 시도"
    )

    alpha_raw = fetch_earnings_calendar(
        alpha_vantage_key
    )

    if alpha_raw:
        selected = {}

        for ticker, record in alpha_raw.items():
            parsed = _parse_iso(
                record.get("date")
            )

            if parsed is None:
                continue

            delta = (
                parsed - today
            ).days

            if -3 <= delta <= 120:
                selected[ticker] = record

        if selected:
            logger.info(
                "기업 실적: Alpha Vantage fallback 사용 "
                "(%d개 종목)",
                len(selected),
            )
            return (
                selected,
                "alpha-vantage",
            )

    logger.warning(
        "기업 실적: 모든 외부 데이터 소스 실패"
    )

    return {}, "none"


# ─────────────────────────────────────────────────────────────────────────────
# ⑦ Fact Reference 생성
# ─────────────────────────────────────────────────────────────────────────────

def build_fact_reference(
    alpha_vantage_key: str,
    now_kst: datetime,
    window_days: int = 45,
) -> tuple[str, dict]:
    """
    프롬프트에 주입할 Fact Reference와
    생성 후 fact_checker.py에서 사용할 lookup을 반환합니다.

    Returns:

        (
            fact_block,
            {
                "earnings": {...},
                "macro": [...],
                "reference_date": "2026-08-26",
            }
        )

    데이터 우선순위:

        경제지표
        ├─ Cloudflare Calendar API
        └─ hardcoded fallback

        기업실적
        ├─ Earnings Dashboard
        ├─ Alpha Vantage
        └─ 빈 결과

    어떤 API가 실패하더라도 전체 원고 생성은 중단하지 않습니다.
    """

    if now_kst.tzinfo is None:
        logger.warning(
            "now_kst에 timezone 정보가 없습니다. "
            "KST 기준으로 해석합니다."
        )
        now_kst = now_kst.replace(
            tzinfo=KST
        )

    today = now_kst.astimezone(
        KST
    ).date()

    # ─────────────────────────────────────────────────────────────────────
    # 경제지표
    # ─────────────────────────────────────────────────────────────────────

    calendar_events = fetch_macro_calendar()

    if calendar_events:
        macro_upcoming = _build_macro_from_calendar(
            calendar_events,
            today=today,
            window_days=window_days,
        )

        macro_source = (
            "Cloudflare Calendar API"
        )
    else:
        macro_upcoming = _build_macro_from_fallback(
            today=today,
            window_days=window_days,
        )

        macro_source = (
            "hardcoded fallback"
        )

    # ─────────────────────────────────────────────────────────────────────
    # 기업 실적
    # ─────────────────────────────────────────────────────────────────────

    earnings_raw, earnings_source = (
        _get_earnings_reference(
            alpha_vantage_key,
            today,
        )
    )

    earnings_lookup = {}
    earnings_lines = []

    for symbol, info in sorted(
        earnings_raw.items(),
        key=lambda item: (
            item[1].get("date") or "",
            item[0],
        ),
    ):
        aliases = WATCHLIST_ALIASES.get(
            symbol,
            [symbol],
        )

        report_date = info.get(
            "date"
        )

        parsed = _parse_iso(
            report_date
        )

        if parsed is None:
            continue

        delta = (
            parsed - today
        ).days

        report_time = str(
            info.get("report_time") or ""
        ).strip().lower()

        report_time_label = (
            info.get("report_time_label")
            or _format_report_time(
                report_time
            )
        )

        earnings_lookup[symbol] = {
            "date": parsed.isoformat(),
            "aliases": aliases,
            "name": info.get(
                "name",
                symbol,
            ),
            "ticker": symbol,
            "fiscal_quarter": info.get(
                "fiscal_quarter"
            ),
            "report_time": report_time,
            "report_time_label": (
                report_time_label
            ),
            "eps_estimate": info.get(
                "eps_estimate"
            ),
            "eps_actual": info.get(
                "eps_actual"
            ),
            "revenue_estimate": info.get(
                "revenue_estimate"
            ),
            "revenue_actual": info.get(
                "revenue_actual"
            ),
            "market_cap": info.get(
                "market_cap"
            ),
            "source": info.get(
                "source",
                earnings_source,
            ),
        }

        # 향후 120일 내 일정만 prompt에 표시합니다.
        if -3 <= delta <= 120:
            line = (
                f"  - "
                f"{aliases[0]}({symbol}): "
                f"{parsed.isoformat()}"
            )

            if report_time:
                line += (
                    f" / {report_time_label}"
                )

            if info.get(
                "fiscal_quarter"
            ):
                line += (
                    f" / "
                    f"{info['fiscal_quarter']}"
                )

            line += (
                f" (D{delta:+d})"
            )

            earnings_lines.append(
                line
            )

    # ─────────────────────────────────────────────────────────────────────
    # Fact Reference 텍스트
    # ─────────────────────────────────────────────────────────────────────

    lines = []

    lines.append(
        "────────────────────────────────────────"
    )
    lines.append(
        "검증된 사실 정보 (Fact Reference) — 반드시 준수"
    )
    lines.append(
        "────────────────────────────────────────"
    )

    lines.append(
        "아래 정보는 블로그 원고에서 경제지표 및 "
        "기업 실적 발표 일정을 언급할 때 사용하는 "
        "검증 기준입니다."
    )

    lines.append(
        "경제지표는 한국시간(KST) 기준 데이터를 우선 사용하며, "
        "기업 실적 발표일은 Earnings Dashboard가 제공한 "
        "report_date를 그대로 사용합니다."
    )

    lines.append(
        "AI가 미국시간과 한국시간의 시차를 임의로 계산하거나 "
        "API에 없는 발표 시간을 추정해서는 안 됩니다."
    )

    lines.append("")

    # ─────────────────────────────────────────────────────────────────────
    # [신규] 주요 인물/직책
    # ─────────────────────────────────────────────────────────────────────

    lines.append(officials_reference_text())

    lines.append("")

    # ─────────────────────────────────────────────────────────────────────
    # 경제지표
    # ─────────────────────────────────────────────────────────────────────

    if macro_upcoming:
        if macro_source == "Cloudflare Calendar API":
            lines.append(
                "[향후 주요 미국 경제지표/이벤트 "
                "— 한국시간(KST) 기준]"
            )
        else:
            lines.append(
                "[향후 주요 미국 경제지표/이벤트 "
                "— 기존 fallback 일정]"
            )

        for indicator in macro_upcoming:
            dates = indicator.get(
                "dates",
                [],
            )

            for d in dates:
                parsed = _parse_iso(d)

                if parsed is None:
                    continue

                delta = (
                    parsed - today
                ).days

                name = indicator.get(
                    "name",
                    "",
                )

                line = (
                    f"  - {name}: "
                    f"{parsed.isoformat()}"
                )

                times = indicator.get(
                    "times",
                    {},
                )

                time_kst = (
                    times.get(
                        parsed.isoformat()
                    )
                    if isinstance(
                        times,
                        dict,
                    )
                    else None
                )

                if time_kst:
                    line += (
                        f" {time_kst} KST"
                    )

                weekday_map = indicator.get(
                    "weekday_kst",
                    {},
                )

                weekday = (
                    weekday_map.get(
                        parsed.isoformat()
                    )
                    if isinstance(
                        weekday_map,
                        dict,
                    )
                    else None
                )

                if weekday:
                    line += (
                        f" ({weekday})"
                    )

                line += (
                    f" / D{delta:+d}"
                )

                forecast = indicator.get(
                    "forecast"
                )

                previous = indicator.get(
                    "previous"
                )

                if (
                    forecast is not None
                    or previous is not None
                ):
                    details = []

                    if forecast is not None:
                        details.append(
                            f"예상 {forecast}"
                        )

                    if previous is not None:
                        details.append(
                            f"이전 {previous}"
                        )

                    if details:
                        line += (
                            " / "
                            + ", ".join(
                                details
                            )
                        )

                lines.append(line)

        lines.append(
            "  ※ D+0은 포스팅 작성일인 오늘을 의미합니다."
        )

        lines.append(
            "  ※ 경제지표의 날짜와 시간은 API의 "
            "date_kst/time_kst 값을 그대로 사용합니다."
        )

        lines.append(
            "  ※ datetime_utc를 이용해 한국시간을 "
            "다시 계산하지 마세요."
        )

        lines.append(
            "  ※ 목록에 없는 경제지표의 정확한 발표일/시간을 "
            "추정하지 마세요."
        )

    else:
        lines.append(
            "[향후 주요 경제지표/이벤트] "
            "확인된 항목 없음"
        )

    lines.append("")

    # ─────────────────────────────────────────────────────────────────────
    # 기업 실적
    # ─────────────────────────────────────────────────────────────────────

    if earnings_lines:
        lines.append(
            "[확인된 주요 기업 실적 발표일]"
        )

        for line in earnings_lines:
            lines.append(line)

        lines.append(
            "  ※ Earnings Dashboard의 "
            "report_date를 그대로 사용합니다."
        )

        lines.append(
            "  ※ report_time='bmo'는 장 시작 전, "
            "'amc'는 장 마감 후를 의미합니다."
        )

        lines.append(
            "  ※ report_time이 비어 있으면 "
            "구체적인 발표 시간을 작성하지 마세요."
        )

        lines.append(
            "  ※ report_date에 미국시간/KST 변환을 "
            "추가로 적용하지 마세요."
        )

    else:
        lines.append(
            "[확인된 주요 기업 실적 발표일] "
            "조회 실패 또는 확인된 항목 없음"
        )

        lines.append(
            "  ※ 이 경우 기업의 구체적인 실적 발표일, "
            "'오늘', '오늘 밤', '장 마감 후' 등의 "
            "임박 표현을 임의로 작성하지 마세요."
        )

    lines.append("")

    # ─────────────────────────────────────────────────────────────────────
    # 데이터 출처/실패 상태
    # ─────────────────────────────────────────────────────────────────────

    lines.append(
        "[Fact Reference 데이터 출처]"
    )

    lines.append(
        f"  - 경제지표: {macro_source}"
    )

    lines.append(
        f"  - 기업 실적: {earnings_source}"
    )

    lines.append(
        f"  - 기준일: {today.isoformat()} KST"
    )

    lines.append(
        "  - API 실패 시 기존 fallback 로직으로 계속 진행"
    )

    lines.append(
        "────────────────────────────────────────"
    )

    fact_block = "\n".join(
        lines
    )

    # ─────────────────────────────────────────────────────────────────────
    # fact_checker.py용 lookup
    # ─────────────────────────────────────────────────────────────────────

    fact_lookup = {
        "earnings": earnings_lookup,
        "macro": macro_upcoming,
        "reference_date": today.isoformat(),
        "macro_source": macro_source,
        "earnings_source": earnings_source,
    }

    return (
        fact_block,
        fact_lookup,
    )