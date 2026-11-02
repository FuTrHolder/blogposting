"""
fact_reference.py (v2)

블로그 원고에 주입할 "검증된 사실 기준표(Fact Reference)"를 구성합니다.

데이터 우선순위
==============
① 미국 경제지표 / 이벤트
   1순위: Cloudflare Calendar API (https://blogposting.pages.dev/api/calendar?days=7)
          date_kst / time_kst / datetime_utc 제공 (KST 기준)
   2순위: 코드에 포함된 공식/고정 경제지표 일정 (FOMC, 고용보고서, CPI, PPI, ECI)
② 기업 실적 발표
   1순위: Earnings Dashboard (report_date = 미국 현지 날짜, report_time = bmo/amc/"")
   2순위: Alpha Vantage EARNINGS_CALENDAR
③ 모든 외부 API 실패 → 원고 생성은 중단하지 않고, 확인 불가 항목은 구체적으로
   쓰지 않도록 Fact Reference에서 명시

v2 변경 (2026-11-02 저녁 원고 오류 + 썸머타임 해제 점검):
  - [시간대] timezone_reference_text(): 현재 뉴욕 오프셋(EDT/EST)을 zoneinfo로 계산해
    프리마켓/정규장/애프터마켓 시간대를 KST로 환산한 안내문을 만듭니다.
    LLM이 "EDT +13 / EST +14"를 스스로 고르지 않아도 됩니다.
  - ["오늘 밤" 창] 작성 시각 ~ +10시간(TONIGHT_WINDOW_HOURS)을 '오늘 밤'으로 정의하고,
    각 경제지표에 KST·ET 시각과 '오늘 밤 창 내' 여부를 병기합니다.
  - [실적] report_date는 미국 현지 날짜입니다. 코드가 정규장 마감(16:00 ET)/개장
    (09:30 ET) 기준 KST 환산값을 계산해 주입하고, D+n도 미국 현지일 기준으로 표시합니다.
    fact_checker가 '미국 현지 날짜'와 'KST 환산 날짜' 둘 다 정상으로 인정하도록
    accepted_dates를 lookup에 담습니다.
  - fact_lookup에 now_kst / ny_today / tonight_end 와 이벤트별 datetimes_utc,
    et_dates, accepted_dates, event_kst 가 추가되었습니다 (기존 키는 그대로).
"""

import csv
import io
import logging
import re
from datetime import datetime, date, timedelta, timezone
from zoneinfo import ZoneInfo

import requests

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# 공통
# ─────────────────────────────────────────────────────────────────────────────

KST = timezone(timedelta(hours=9))
NY_TZ = ZoneInfo("America/New_York")

CALENDAR_API_URL = "https://blogposting.pages.dev/api/calendar?days=7"
EARNINGS_DASHBOARD_URL = "https://earnings-dashboard.dmenc-hjw.workers.dev/"
DEFAULT_HTTP_TIMEOUT = 20

# '오늘 밤'으로 인정하는 창: 작성 시각 ~ +10시간 (21:00 KST 포스트 → 익일 07:00 KST까지)
TONIGHT_WINDOW_HOURS = 10

_LAST_VERIFIED = "2026-07-22"

# ─────────────────────────────────────────────────────────────────────────────
# ① 공식/고정 경제지표 일정 (Calendar API 실패 시 fallback)
#    ※ 아래 날짜는 '미국 현지 날짜' 기준입니다 (FOMC 성명 = 14:00 ET → KST 익일 새벽).
# ─────────────────────────────────────────────────────────────────────────────

FOMC_2026 = [
    {"start": "2026-01-27", "statement_date": "2026-01-28", "label": "1월 FOMC"},
    {"start": "2026-03-17", "statement_date": "2026-03-18", "label": "3월 FOMC"},
    {"start": "2026-04-28", "statement_date": "2026-04-29", "label": "4월 FOMC"},
    {"start": "2026-06-16", "statement_date": "2026-06-17", "label": "6월 FOMC"},
    {"start": "2026-07-28", "statement_date": "2026-07-29", "label": "7월 FOMC"},
    {"start": "2026-09-15", "statement_date": "2026-09-16", "label": "9월 FOMC"},
    {"start": "2026-10-27", "statement_date": "2026-10-28", "label": "10월 FOMC"},
    {"start": "2026-12-08", "statement_date": "2026-12-09", "label": "12월 FOMC"},
]

EMPLOYMENT_SITUATION_2026 = [
    "2026-01-09", "2026-02-06", "2026-03-06", "2026-04-03", "2026-05-08", "2026-06-05",
    "2026-07-02", "2026-08-07", "2026-09-04", "2026-10-02", "2026-11-06", "2026-12-04",
]

CPI_2026 = [
    "2026-01-13", "2026-02-11", "2026-03-11", "2026-04-10", "2026-05-12", "2026-06-10",
    "2026-07-14", "2026-08-12", "2026-09-11", "2026-10-14", "2026-11-10", "2026-12-10",
]

PPI_2026 = [
    "2026-01-14", "2026-02-12", "2026-03-12", "2026-04-14", "2026-05-13", "2026-06-11",
    "2026-07-15", "2026-08-13", "2026-09-10", "2026-10-15", "2026-11-13", "2026-12-15",
]

ECI_2026 = ["2026-01-30", "2026-04-30", "2026-07-31", "2026-10-30"]

FALLBACK_MACRO_INDICATORS = [
    {
        "name": "FOMC 회의(금리 결정)",
        "keywords": ["FOMC", "연준 회의", "금리 결정", "연방공개시장위원회"],
        "dates": [item["statement_date"] for item in FOMC_2026],
    },
    {
        "name": "고용보고서(비농업고용지표)",
        "keywords": ["고용보고서", "비농업고용", "비농업 고용", "실업률 발표", "고용지표"],
        "dates": EMPLOYMENT_SITUATION_2026,
    },
    {
        "name": "CPI(소비자물가지수)",
        "keywords": ["CPI", "소비자물가지수", "소비자물가"],
        "dates": CPI_2026,
    },
    {
        "name": "PPI(생산자물가지수)",
        "keywords": ["PPI", "생산자물가지수", "생산자물가"],
        "dates": PPI_2026,
    },
    {
        "name": "고용비용지수",
        "keywords": ["고용비용지수", "ECI"],
        "dates": ECI_2026,
    },
]

# 기존 코드와의 호환성을 위해 공개 이름으로 유지합니다.
MACRO_INDICATORS = FALLBACK_MACRO_INDICATORS

# ─────────────────────────────────────────────────────────────────────────────
# ② 기업 워치리스트
# ─────────────────────────────────────────────────────────────────────────────

WATCHLIST_ALIASES = {
    "NVDA": ["엔비디아", "NVIDIA", "Nvidia"],
    "AAPL": ["애플", "Apple"],
    "MSFT": ["마이크로소프트", "Microsoft"],
    "GOOGL": ["구글", "알파벳", "Google", "Alphabet"],
    "AMZN": ["아마존", "Amazon"],
    "META": ["메타", "Meta"],
    "TSLA": ["테슬라", "Tesla"],
    "AVGO": ["브로드컴", "Broadcom"],
    "AMD": ["AMD", "에이엠디"],
    "NFLX": ["넷플릭스", "Netflix"],
    "ORCL": ["오라클", "Oracle"],
    "CRM": ["세일즈포스", "Salesforce"],
    "ADBE": ["어도비", "Adobe"],
    "INTC": ["인텔", "Intel"],
    "QCOM": ["퀄컴", "Qualcomm"],
    "PYPL": ["페이팔", "PayPal"],
    "DIS": ["디즈니", "Disney"],
    "JPM": ["JP모건", "JP모건체이스", "JPMorgan"],
    "V": ["비자카드", "Visa"],
    "MA": ["마스터카드", "Mastercard"],
    "COST": ["코스트코", "Costco"],
    "WMT": ["월마트", "Walmart"],
    "HD": ["홈디포", "Home Depot"],
    "UNH": ["유나이티드헬스", "UnitedHealth"],
    "XOM": ["엑슨모빌", "ExxonMobil", "엑손모빌"],
    "BA": ["보잉", "Boeing"],
}

# fact_checker.py가 직접 import하므로 이름을 유지합니다.
EARNINGS_KEYWORDS = ["실적", "어닝스", "earnings", "실적 발표", "실적발표"]

IMMINENT_WORDS = [
    "오늘", "오늘 밤", "오늘밤", "당일", "이번 주", "이번주",
    "장 마감 후", "장마감 후", "정규장 마감 후", "곧", "임박",
]

# ─────────────────────────────────────────────────────────────────────────────
# 주요 인물/직책 참조표
#
# LLM은 학습 데이터에 각인된 인물(예: 연준 의장 = 파월)을 교체 이후에도 계속
# 언급하는 경향이 있습니다. 블로그 본문(content_generator.py), SNS
# (marketing/content_adapter/adapter.py), 영상 나레이션(generator.py) 3곳이 이
# 표를 공유하고, 발행 직전 scrub_outdated_officials()로 최종 교정합니다.
# 직책이 바뀌면 이 표만 수정하세요. outdated_aliases에는 직책 접미사 없이 이름만.
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
    """프롬프트에 주입할 '현재 주요 인물' 안내문 (외부 호출 없음)."""
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
    """단어 마지막 글자에 받침이 있는지 확인 (조사 은/는 등 선택용)."""
    if not word:
        return False
    code = ord(word[-1])
    if 0xAC00 <= code <= 0xD7A3:
        return (code - 0xAC00) % 28 != 0
    return False


_PARTICLE_PAIRS = {
    "은": ("은", "는"), "는": ("은", "는"),
    "이": ("이", "가"), "가": ("이", "가"),
    "을": ("을", "를"), "를": ("을", "를"),
    "과": ("과", "와"), "와": ("과", "와"),
}

# 이름 주변에 이 단어들이 있으면 '전임자에 대한 정당한 역사적 언급'으로 판단해 치환하지 않습니다.
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

_HISTORICAL_CONTEXT_WINDOW = 20


def _looks_historical(text: str, start: int, end: int) -> bool:
    lo = max(0, start - _HISTORICAL_CONTEXT_WINDOW)
    hi = min(len(text), end + _HISTORICAL_CONTEXT_WINDOW)
    window = text[lo:hi]
    return any(marker in window for marker in HISTORICAL_CONTEXT_MARKERS)


def scrub_outdated_officials(text: str) -> tuple[str, list[dict]]:
    """
    생성된 텍스트에서 이미 교체된 인물의 이름을 현재 인물 이름으로 치환합니다
    (결정론적 최종 안전망). 전임/과거 맥락(HISTORICAL_CONTEXT_MARKERS)이면 보존합니다.
    긴 별칭부터 치환하고, 이름 뒤 조사(은/는, 이/가, 을/를, 과/와)도 새 이름의
    받침 유무에 맞게 교정합니다.

    Returns: (교정된 텍스트, 내역 리스트: action = "corrected" | "preserved_historical")
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

            pattern = re.compile(re.escape(alias) + r"(은|는|이|가|을|를|과|와)?")
            source_text = text
            counts = {"corrected": 0, "preserved": 0}

            def _replace(m: "re.Match", _source: str = source_text, _counts: dict = counts) -> str:
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
                    "role": info["role_ko"], "from": alias, "to": new_name,
                    "action": "corrected", "count": counts["corrected"],
                })
            if counts["preserved"]:
                applied.append({
                    "role": info["role_ko"], "from": alias, "to": alias,
                    "action": "preserved_historical", "count": counts["preserved"],
                })

    return text, applied


def format_official_fix_log(fix: dict) -> str:
    """scrub_outdated_officials() 내역 한 건을 로그 메시지로 변환."""
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
    try:
        return datetime.strptime(str(value)[:10], "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


def _safe_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _upcoming(dates: list[str], today: date, window_days: int) -> list[str]:
    """today 기준 -3일 ~ +window_days일 사이의 날짜만 추립니다 (경계 문제 완충용 -3일)."""
    out = []
    for value in dates:
        parsed = _parse_iso(value)
        if parsed is None:
            continue
        delta = (parsed - today).days
        if -3 <= delta <= window_days:
            out.append(parsed.isoformat())
    return sorted(set(out))


def _format_report_time(report_time: str) -> str:
    value = str(report_time or "").strip().lower()
    if value == "bmo":
        return "장 시작 전"
    if value == "amc":
        return "장 마감 후"
    return "발표 시점 미정"


def _in_tonight(dt: datetime, now_kst: datetime, end_kst: datetime) -> bool:
    """dt가 '오늘 밤' 창(작성 1시간 전 ~ +TONIGHT_WINDOW_HOURS)에 있는지."""
    return now_kst - timedelta(hours=1) <= dt <= end_kst


def _parse_utc_iso(value) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


# ─────────────────────────────────────────────────────────────────────────────
# 시간대 안내 (썸머타임 자동 반영)
# ─────────────────────────────────────────────────────────────────────────────

def timezone_reference_text(now_kst: datetime) -> str:
    """
    현재 시점의 뉴욕 오프셋(EDT/EST)을 zoneinfo로 계산해 프롬프트용 시간대 안내문을
    만듭니다. 모델이 시차를 직접 계산하거나 'EDT +13 / EST +14' 중 고르지 않게 합니다.
    """
    if now_kst.tzinfo is None:
        now_kst = now_kst.replace(tzinfo=KST)
    now_kst = now_kst.astimezone(KST)
    ny_now = now_kst.astimezone(NY_TZ)
    off = int(ny_now.utcoffset().total_seconds() // 3600)
    shift = 9 - off
    abbr = ny_now.tzname() or "ET"
    dst_label = "적용 중(EDT)" if ny_now.dst() else "해제(EST)"
    day = ny_now.date()
    end_kst = now_kst + timedelta(hours=TONIGHT_WINDOW_HOURS)

    def clock(h: int, m: int) -> str:
        dt = datetime(day.year, day.month, day.day, h, m, tzinfo=NY_TZ).astimezone(KST)
        tag = "" if dt.date() == day else "(익일)"
        return f"{dt.strftime('%H:%M')}{tag}"

    lines = [
        "────────────────────────────────────────",
        "시간대 안내 (코드가 계산한 값 — 직접 시차 계산 금지)",
        "────────────────────────────────────────",
        f"- 현재 뉴욕 시간대: {abbr} (UTC{off:+d}), 서머타임 {dst_label} → ET→KST 변환은 +{shift}시간",
        f"- 작성 시각 {now_kst.strftime('%Y-%m-%d %H:%M')} KST = 뉴욕 {ny_now.strftime('%Y-%m-%d %H:%M')} {abbr}",
        f"- 뉴욕 기준 오늘 날짜: {day.isoformat()} (한국 날짜와 다를 수 있음)",
        f"- 미국 프리마켓 04:00~09:30 ET = KST {clock(4, 0)}~{clock(9, 30)}",
        f"- 미국 정규장 09:30~16:00 ET = KST {clock(9, 30)}~{clock(16, 0)}",
        f"- 미국 애프터마켓 16:00~20:00 ET = KST {clock(16, 0)}~{clock(20, 0)}",
        f"- '오늘 밤'이라고 쓸 수 있는 일정: KST {now_kst.strftime('%m/%d %H:%M')} ~ "
        f"{end_kst.strftime('%m/%d %H:%M')} 사이에 발생하는 것 (익일 새벽 지표 포함)",
        "- 기업 실적 report_date는 '미국 현지 날짜'입니다. 장 마감 후(amc) 발표는 한국 날짜로 "
        "다음 날 새벽입니다.",
        "────────────────────────────────────────",
    ]
    return "\n".join(lines)


def _earnings_timing(report_day: date, report_time: str) -> dict:
    """
    실적 발표의 KST 환산 정보를 계산합니다 (추정이 아니라 시장 개장/마감 시각 기준).
      amc → 정규장 마감 16:00 ET 이후 / bmo → 정규장 개장 09:30 ET 이전 / 미정 → 환산 없음
    accepted_dates: 원고에 '정상'으로 인정할 날짜(미국 현지 날짜 + KST 환산 날짜).
    """
    us = report_day.isoformat()
    rt = (report_time or "").strip().lower()

    if rt == "amc":
        dt = datetime(report_day.year, report_day.month, report_day.day, 16, 0, tzinfo=NY_TZ).astimezone(KST)
        label = (f"한국시간 {dt.month}월 {dt.day}일 {dt.strftime('%H:%M')} 이후 "
                 f"(미국 정규장 마감 16:00 ET 기준)")
        return {"event_kst": dt.isoformat(), "kst_label": label,
                "accepted_dates": sorted({us, dt.date().isoformat()})}

    if rt == "bmo":
        dt = datetime(report_day.year, report_day.month, report_day.day, 9, 30, tzinfo=NY_TZ).astimezone(KST)
        label = (f"한국시간 {dt.month}월 {dt.day}일 {dt.strftime('%H:%M')} 이전 "
                 f"(미국 정규장 개장 09:30 ET 이전)")
        return {"event_kst": dt.isoformat(), "kst_label": label,
                "accepted_dates": sorted({us, dt.date().isoformat()})}

    return {"event_kst": None, "kst_label": "",
            "accepted_dates": sorted({us, (report_day + timedelta(days=1)).isoformat()})}


# ─────────────────────────────────────────────────────────────────────────────
# ③ Cloudflare Calendar API
# ─────────────────────────────────────────────────────────────────────────────

def fetch_macro_calendar(timeout: int = DEFAULT_HTTP_TIMEOUT) -> list[dict]:
    """
    Cloudflare Calendar API에서 향후 7일 USD 경제지표/이벤트를 가져옵니다.
    date_kst / time_kst는 이미 한국시간이므로 UTC 값으로 다시 계산하지 않습니다.
    실패 시 예외를 던지지 않고 빈 list를 반환합니다.
    """
    try:
        response = requests.get(
            CALENDAR_API_URL,
            timeout=timeout,
            headers={
                "Accept": "application/json",
                "User-Agent": "Mozilla/5.0 (compatible; blogposting-fact-reference/1.0)",
            },
        )
        response.raise_for_status()
        payload = response.json()

        if not isinstance(payload, dict):
            logger.warning("경제지표 Calendar API 응답이 객체가 아님")
            return []

        raw_events = payload.get("events")
        if not isinstance(raw_events, list):
            logger.warning("경제지표 Calendar API에 events 배열이 없음")
            return []

        normalized = []
        for event in raw_events:
            if not isinstance(event, dict):
                continue
            currency = str(event.get("currency") or "").strip().upper()
            if currency != "USD":
                continue
            parsed_date = _parse_iso(str(event.get("date_kst") or "").strip())
            if parsed_date is None:
                continue
            event_name = str(event.get("event") or "").strip()
            if not event_name:
                continue

            normalized.append({
                "name": event_name,
                "keywords": [event_name],
                "date": parsed_date.isoformat(),
                "time_kst": str(event.get("time_kst") or "").strip(),
                "date_kst": parsed_date.isoformat(),
                "weekday_kst": str(event.get("weekday_kst") or "").strip(),
                "currency": currency,
                "importance": _safe_int(event.get("importance")),
                "actual": event.get("actual"),
                "forecast": event.get("forecast"),
                "previous": event.get("previous"),
                "datetime_utc": event.get("datetime_utc"),
                "source": "Cloudflare Calendar API",
            })

        deduplicated, seen = [], set()
        for event in normalized:
            key = (event["date"], event["time_kst"], event["name"])
            if key in seen:
                continue
            seen.add(key)
            deduplicated.append(event)

        deduplicated.sort(key=lambda it: (it["date"], it["time_kst"], it["name"]))
        logger.info("Cloudflare 경제지표 API 조회 성공: %d개 USD 이벤트", len(deduplicated))
        return deduplicated

    except Exception as exc:
        logger.warning("Cloudflare 경제지표 API 조회 실패 (기존 경제지표 fallback 사용): %s", exc)
        return []


def _build_macro_from_calendar(events: list[dict], today: date, window_days: int) -> list[dict]:
    """
    Calendar API 이벤트를 fact_checker가 쓰는 macro indicator 구조로 변환합니다.
    각 이벤트가 하나의 indicator이며, v2에서 다음 필드가 추가되었습니다.
      datetimes_utc : {KST날짜: datetime_utc ISO}
      et_dates      : 미국 동부 현지 날짜 목록
      accepted_dates: 원고에 정상으로 인정할 날짜 (KST 날짜 + 미국 현지 날짜)
      date_only     : False (정확한 시각 보유)
    """
    result = []
    for event in events:
        event_date = _parse_iso(event.get("date"))
        if event_date is None:
            continue
        delta = (event_date - today).days
        if not (-3 <= delta <= window_days):
            continue
        event_name = str(event.get("name") or "").strip()
        if not event_name:
            continue

        iso_kst = event_date.isoformat()
        datetimes_utc, et_dates = {}, []
        dt_utc = _parse_utc_iso(event.get("datetime_utc"))
        if dt_utc is not None:
            datetimes_utc[iso_kst] = dt_utc.isoformat()
            et_dates.append(dt_utc.astimezone(NY_TZ).date().isoformat())

        result.append({
            "name": event_name,
            "keywords": [event_name],
            "dates": [iso_kst],
            "times": {iso_kst: event.get("time_kst") or ""},
            "weekday_kst": {iso_kst: event.get("weekday_kst") or ""},
            "datetimes_utc": datetimes_utc,
            "et_dates": et_dates,
            "accepted_dates": sorted({iso_kst, *et_dates}),
            "date_only": False,
            "importance": event.get("importance"),
            "currency": event.get("currency"),
            "forecast": event.get("forecast"),
            "previous": event.get("previous"),
            "actual": event.get("actual"),
            "source": event.get("source"),
        })
    return result


def _build_macro_from_fallback(today: date, window_days: int) -> list[dict]:
    """Calendar API 실패 시 고정 일정을 사용합니다 (미국 현지 날짜 → 익일 KST도 정상 인정)."""
    result = []
    for indicator in FALLBACK_MACRO_INDICATORS:
        upcoming_dates = _upcoming(indicator["dates"], today, window_days)
        if not upcoming_dates:
            continue
        accepted = set(upcoming_dates)
        for d in upcoming_dates:
            accepted.add((_parse_iso(d) + timedelta(days=1)).isoformat())
        result.append({
            "name": indicator["name"],
            "keywords": indicator["keywords"],
            "dates": upcoming_dates,
            "accepted_dates": sorted(accepted),
            "datetimes_utc": {},
            "et_dates": list(upcoming_dates),
            "date_only": True,
            "source": "hardcoded fallback",
        })
    return result


# ─────────────────────────────────────────────────────────────────────────────
# ④ Earnings Dashboard
# ─────────────────────────────────────────────────────────────────────────────

def fetch_earnings_dashboard(timeout: int = DEFAULT_HTTP_TIMEOUT) -> dict:
    """
    Earnings Dashboard JSON(배열)에서 워치리스트 종목의 실적 일정을 가져옵니다.
    반환: {ticker: [record, ...]} (날짜순). 실패 시 {}.
    report_date는 미국 현지 날짜 그대로 사용합니다 (여기서 변환하지 않음).
    """
    try:
        response = requests.get(
            EARNINGS_DASHBOARD_URL,
            timeout=timeout,
            headers={
                "Accept": "application/json",
                "User-Agent": "Mozilla/5.0 (compatible; blogposting-fact-reference/1.0)",
            },
        )
        response.raise_for_status()
        payload = response.json()

        if not isinstance(payload, list):
            logger.warning("Earnings Dashboard 응답이 배열이 아님")
            return {}

        result: dict = {}
        for item in payload:
            if not isinstance(item, dict):
                continue
            ticker = str(item.get("ticker") or "").strip().upper()
            if not ticker or ticker not in WATCHLIST_ALIASES:
                continue

            report_date = str(item.get("report_date") or "").strip()[:10]
            if _parse_iso(report_date) is None:
                continue

            report_time = str(item.get("report_time") or "").strip().lower()
            if report_time not in ("", "bmo", "amc"):
                logger.warning("알 수 없는 report_time 무시: %s / %s", ticker, report_time)
                report_time = ""

            name_ko, name_en = item.get("name_ko"), item.get("name_en")
            name = (str(name_ko).strip() if name_ko else "") or \
                   (str(name_en).strip() if name_en else "") or ticker

            result.setdefault(ticker, []).append({
                "id": item.get("id"),
                "ticker": ticker,
                "fiscal_quarter": item.get("fiscal_quarter"),
                "date": report_date,
                "report_date": report_date,
                "report_time": report_time,
                "report_time_label": _format_report_time(report_time),
                "eps_estimate": item.get("eps_estimate"),
                "eps_actual": item.get("eps_actual"),
                "revenue_estimate": item.get("revenue_estimate"),
                "revenue_actual": item.get("revenue_actual"),
                "source_agreement_count": item.get("source_agreement_count"),
                "name_ko": name_ko,
                "name_en": name_en,
                "name": name,
                "market_cap": item.get("market_cap"),
                "source": "earnings-dashboard",
            })

        for ticker in result:
            result[ticker].sort(key=lambda it: (it["date"], it.get("fiscal_quarter") or ""))

        logger.info("Earnings Dashboard 조회 성공: %d개 워치리스트 종목", len(result))
        return result

    except Exception as exc:
        logger.warning("Earnings Dashboard 조회 실패 (Alpha Vantage fallback 사용): %s", exc)
        return {}


def _select_upcoming_earnings(dashboard_data: dict, today: date, max_days: int = 120) -> dict:
    """티스토리별 레코드 중 현재 기준 가장 가까운 일정을 선택 (-3일 ~ +max_days)."""
    result = {}
    for ticker, records in dashboard_data.items():
        if not isinstance(records, list):
            continue
        candidates = []
        for record in records:
            parsed = _parse_iso(record.get("date"))
            if parsed is None:
                continue
            delta = (parsed - today).days
            if -3 <= delta <= max_days:
                candidates.append((delta, parsed, record))
        if not candidates:
            continue
        candidates.sort(key=lambda item: (item[0], item[1]))
        result[ticker] = candidates[0][2]
    return result


# ─────────────────────────────────────────────────────────────────────────────
# ⑤ Alpha Vantage 실적 캘린더 fallback
# ─────────────────────────────────────────────────────────────────────────────

def fetch_earnings_calendar(alpha_vantage_key: str, horizon: str = "3month") -> dict:
    """
    Earnings Dashboard 실패 시 Alpha Vantage EARNINGS_CALENDAR(CSV) fallback.
    발표 시간(bmo/amc)은 제공되지 않으므로 임의로 만들지 않습니다.
    """
    if not alpha_vantage_key:
        logger.warning("ALPHA_VANTAGE_API_KEY 없음 — Alpha Vantage 실적 fallback 건너뜀")
        return {}

    try:
        response = requests.get(
            "https://www.alphavantage.co/query",
            params={"function": "EARNINGS_CALENDAR", "horizon": horizon, "apikey": alpha_vantage_key},
            timeout=DEFAULT_HTTP_TIMEOUT,
        )
        response.raise_for_status()
        text = response.text

        if text.strip().startswith("{"):
            logger.warning("Alpha Vantage 실적 응답이 CSV가 아님 (한도 초과/오류 가능성): %s", text[:200])
            return {}

        result = {}
        for row in csv.DictReader(io.StringIO(text)):
            symbol = str(row.get("symbol") or "").strip().upper()
            report_date = str(row.get("reportDate") or "").strip()
            if symbol not in WATCHLIST_ALIASES or not report_date:
                continue
            if _parse_iso(report_date) is None:
                continue

            candidate = {
                "id": None, "ticker": symbol, "fiscal_quarter": None,
                "date": report_date, "report_date": report_date,
                "report_time": "", "report_time_label": "발표 시점 미정",
                "eps_estimate": None, "eps_actual": None,
                "revenue_estimate": None, "revenue_actual": None,
                "source_agreement_count": None,
                "name_ko": None, "name_en": (row.get("name") or "").strip(),
                "name": (row.get("name") or symbol).strip(),
                "market_cap": None, "source": "alpha-vantage",
            }
            if symbol not in result or report_date < result[symbol]["date"]:
                result[symbol] = candidate

        logger.info("Alpha Vantage 실적 fallback 조회 완료: %d개 워치리스트 종목", len(result))
        return result

    except Exception as exc:
        logger.warning("Alpha Vantage 실적 fallback 실패 (계속 진행): %s", exc)
        return {}


def _get_earnings_reference(alpha_vantage_key: str, today: date) -> tuple[dict, str]:
    """1. Earnings Dashboard → 2. Alpha Vantage → 3. 빈 dict. 반환: (lookup, source)."""
    dashboard_raw = fetch_earnings_dashboard()

    if dashboard_raw:
        selected = _select_upcoming_earnings(dashboard_raw, today=today, max_days=120)
        if selected:
            logger.info("기업 실적: Earnings Dashboard 사용 (%d개 종목)", len(selected))
            return selected, "earnings-dashboard"

    logger.warning("Earnings Dashboard에서 사용 가능한 향후 실적 일정이 없어 Alpha Vantage fallback 시도")

    alpha_raw = fetch_earnings_calendar(alpha_vantage_key)
    if alpha_raw:
        selected = {}
        for ticker, record in alpha_raw.items():
            parsed = _parse_iso(record.get("date"))
            if parsed is None:
                continue
            if -3 <= (parsed - today).days <= 120:
                selected[ticker] = record
        if selected:
            logger.info("기업 실적: Alpha Vantage fallback 사용 (%d개 종목)", len(selected))
            return selected, "alpha-vantage"

    logger.warning("기업 실적: 모든 외부 데이터 소스 실패")
    return {}, "none"


# ─────────────────────────────────────────────────────────────────────────────
# ⑥ Fact Reference 생성
# ─────────────────────────────────────────────────────────────────────────────

def build_fact_reference(
    alpha_vantage_key: str,
    now_kst: datetime,
    window_days: int = 45,
) -> tuple[str, dict]:
    """
    프롬프트에 주입할 Fact Reference와 fact_checker.py용 lookup을 반환합니다.

    Returns: (fact_block, {"earnings", "macro", "reference_date", "macro_source",
                           "earnings_source", "now_kst", "ny_today", "tonight_end"})
    어떤 API가 실패해도 원고 생성은 중단하지 않습니다.
    """
    if now_kst.tzinfo is None:
        logger.warning("now_kst에 timezone 정보가 없습니다. KST 기준으로 해석합니다.")
        now_kst = now_kst.replace(tzinfo=KST)

    now_kst = now_kst.astimezone(KST)
    today = now_kst.date()
    ny_today = now_kst.astimezone(NY_TZ).date()
    tonight_end = now_kst + timedelta(hours=TONIGHT_WINDOW_HOURS)

    # ── 경제지표 ─────────────────────────────────────────────────────────
    calendar_events = fetch_macro_calendar()
    if calendar_events:
        macro_upcoming = _build_macro_from_calendar(calendar_events, today=today, window_days=window_days)
        macro_source = "Cloudflare Calendar API"
    else:
        macro_upcoming = _build_macro_from_fallback(today=today, window_days=window_days)
        macro_source = "hardcoded fallback"

    # ── 기업 실적 ─────────────────────────────────────────────────────────
    earnings_raw, earnings_source = _get_earnings_reference(alpha_vantage_key, today)

    earnings_lookup = {}
    earnings_lines = []

    for symbol, info in sorted(
        earnings_raw.items(),
        key=lambda item: (item[1].get("date") or "", item[0]),
    ):
        aliases = WATCHLIST_ALIASES.get(symbol, [symbol])
        parsed = _parse_iso(info.get("date"))
        if parsed is None:
            continue

        delta_ny = (parsed - ny_today).days      # report_date는 미국 현지 날짜 → 뉴욕 날짜 기준
        report_time = str(info.get("report_time") or "").strip().lower()
        report_time_label = info.get("report_time_label") or _format_report_time(report_time)
        timing = _earnings_timing(parsed, report_time)

        earnings_lookup[symbol] = {
            "date": parsed.isoformat(),
            "aliases": aliases,
            "name": info.get("name", symbol),
            "ticker": symbol,
            "fiscal_quarter": info.get("fiscal_quarter"),
            "report_time": report_time,
            "report_time_label": report_time_label,
            "event_kst": timing["event_kst"],
            "kst_label": timing["kst_label"],
            "accepted_dates": timing["accepted_dates"],
            "eps_estimate": info.get("eps_estimate"),
            "eps_actual": info.get("eps_actual"),
            "revenue_estimate": info.get("revenue_estimate"),
            "revenue_actual": info.get("revenue_actual"),
            "market_cap": info.get("market_cap"),
            "source": info.get("source", earnings_source),
        }

        if -3 <= delta_ny <= 120:
            line = f"  - {aliases[0]}({symbol}): {parsed.isoformat()}(미국 현지일, D{delta_ny:+d})"
            if report_time:
                line += f" / {report_time_label}"
            if info.get("fiscal_quarter"):
                line += f" / {info['fiscal_quarter']}"
            if timing["kst_label"]:
                line += f" / KST 환산: {timing['kst_label']}"
                ev = datetime.fromisoformat(timing["event_kst"])
                if _in_tonight(ev, now_kst, tonight_end):
                    line += " [오늘 밤 창 내]"
            earnings_lines.append(line)

    # ── Fact Reference 텍스트 ────────────────────────────────────────────
    lines = [
        "────────────────────────────────────────",
        "검증된 사실 정보 (Fact Reference) — 반드시 준수",
        "────────────────────────────────────────",
        "아래 정보는 블로그 원고에서 경제지표 및 기업 실적 발표 일정을 언급할 때 사용하는 검증 기준입니다.",
        "경제지표는 한국시간(KST) 기준 데이터를 사용하고, 기업 실적 report_date는 '미국 현지 날짜'입니다.",
        "AI가 미국시간과 한국시간의 시차를 임의로 계산하거나 API에 없는 발표 시간을 추정해서는 안 됩니다. "
        "KST 환산이 필요하면 아래 줄에 코드가 계산해 둔 값만 사용하세요.",
        "",
        officials_reference_text(),
        "",
    ]

    if macro_upcoming:
        if macro_source == "Cloudflare Calendar API":
            lines.append("[향후 주요 미국 경제지표/이벤트 — 한국시간(KST) 기준, 미국 ET 병기]")
        else:
            lines.append("[향후 주요 미국 경제지표/이벤트 — 기존 fallback 일정 (날짜는 미국 현지일 기준)]")

        for indicator in macro_upcoming:
            for d in indicator.get("dates", []):
                parsed = _parse_iso(d)
                if parsed is None:
                    continue
                delta = (parsed - today).days
                line = f"  - {indicator.get('name', '')}: {parsed.isoformat()}"

                times = indicator.get("times", {})
                time_kst = times.get(parsed.isoformat()) if isinstance(times, dict) else None
                if time_kst:
                    line += f" {time_kst} KST"

                weekday_map = indicator.get("weekday_kst", {})
                weekday = weekday_map.get(parsed.isoformat()) if isinstance(weekday_map, dict) else None
                if weekday:
                    line += f" ({weekday})"

                line += f" / D{delta:+d}"

                dt_iso = (indicator.get("datetimes_utc") or {}).get(parsed.isoformat())
                dt_utc = _parse_utc_iso(dt_iso) if dt_iso else None
                if dt_utc is not None:
                    et = dt_utc.astimezone(NY_TZ)
                    line += f" [미국 {et.strftime('%m/%d %H:%M')} {et.tzname()}]"
                    if _in_tonight(dt_utc.astimezone(KST), now_kst, tonight_end):
                        line += " [오늘 밤 창 내]"

                forecast, previous = indicator.get("forecast"), indicator.get("previous")
                if forecast is not None or previous is not None:
                    details = []
                    if forecast is not None:
                        details.append(f"예상 {forecast}")
                    if previous is not None:
                        details.append(f"이전 {previous}")
                    line += " / " + ", ".join(details)

                lines.append(line)

        lines += [
            "  ※ D+0은 포스팅 작성일(KST 기준 오늘)입니다. '[오늘 밤 창 내]' 표시가 있는 항목만 '오늘 밤 발표'라고 쓸 수 있습니다.",
            "  ※ 경제지표의 날짜와 시간은 API의 date_kst/time_kst 값을 그대로 사용합니다.",
            "  ※ datetime_utc를 이용해 한국시간을 다시 계산하지 마세요.",
            "  ※ 목록에 없는 경제지표의 정확한 발표일/시간을 추정하지 마세요.",
        ]
    else:
        lines.append("[향후 주요 경제지표/이벤트] 확인된 항목 없음")

    lines.append("")

    if earnings_lines:
        lines.append("[확인된 주요 기업 실적 발표일]")
        lines += earnings_lines
        lines += [
            "  ※ report_date는 미국 현지 날짜 그대로입니다 (D+n도 뉴욕 날짜 기준).",
            "  ※ report_time='bmo'는 장 시작 전, 'amc'는 장 마감 후를 의미합니다.",
            "  ※ 'KST 환산'은 정규장 마감/개장 시각 기준으로 코드가 계산한 값입니다. 이 값 외의 시각은 쓰지 마세요.",
            "  ※ report_time이 비어 있으면 구체적인 발표 시간을 작성하지 마세요.",
            "  ※ '[오늘 밤 창 내]' 표시가 없는 기업의 실적을 '오늘 밤'에 발표된다고 쓰지 마세요.",
        ]
    else:
        lines += [
            "[확인된 주요 기업 실적 발표일] 조회 실패 또는 확인된 항목 없음",
            "  ※ 이 경우 기업의 구체적인 실적 발표일, '오늘', '오늘 밤', '장 마감 후' 등의 "
            "임박 표현을 임의로 작성하지 마세요.",
        ]

    lines += [
        "",
        "[Fact Reference 데이터 출처]",
        f"  - 경제지표: {macro_source}",
        f"  - 기업 실적: {earnings_source}",
        f"  - 기준일: {today.isoformat()} KST (뉴욕 {ny_today.isoformat()})",
        "  - API 실패 시 기존 fallback 로직으로 계속 진행",
        "────────────────────────────────────────",
    ]

    fact_lookup = {
        "earnings": earnings_lookup,
        "macro": macro_upcoming,
        "reference_date": today.isoformat(),
        "macro_source": macro_source,
        "earnings_source": earnings_source,
        "now_kst": now_kst.isoformat(),
        "ny_today": ny_today.isoformat(),
        "tonight_end": tonight_end.isoformat(),
    }

    return "\n".join(lines), fact_lookup
