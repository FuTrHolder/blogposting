"""
fact_reference.py
"검증된 사실 기준표(Fact Reference)"를 구성하는 모듈.

배경:
  Gemini가 기업 실적 발표일이나 미국 경제지표 발표일을 스스로 "추정"해서
  본문에 구체적인 날짜로 적어버리는 경우가 있었습니다.
  (예: 2026-07-21 저녁 포스팅에서 "오늘 장 마감 후 엔비디아 실적 발표"라고
   서술했으나, 실제 엔비디아 실적 발표일은 2026-08-26으로 확인됨 — 약 5주 차이)

  블로그의 신뢰도에서 실적/지표 발표일 같은 "구체적 사실"이 틀리는 것은
  치명적이므로, AI가 스스로 재계산·추측하지 못하게 하고 아래 두 축의
  "이미 확정된 사실"만 프롬프트에 명시적으로 주입합니다.

  ① 미국 정부/연준이 공식적으로 사전 발표한 고정 일정
     - FOMC 회의 일정 (federalreserve.gov, 2025년에 2026년 일정 발표)
     - BLS/OMB 경제지표 발표 일정표
       (Schedule of Release Dates for Principal Federal Economic Indicators,
        White House OMB 발간, 매년 하반기에 다음 해 일정을 미리 공개)
     => 정부가 미리 공표한 값이라 신뢰도가 매우 높고, 연중 거의 바뀌지 않습니다.
     => 코드에 하드코딩하되, 최종 확인 시점(_LAST_VERIFIED)을 남겨 두어
        다음 해로 넘어갈 때 갱신 필요 여부를 코드만 봐도 알 수 있게 합니다.

  ② 개별 기업의 실적 발표일 (수시로 확정/변경됨)
     - Alpha Vantage EARNINGS_CALENDAR 엔드포인트 (무료 티어 포함, CSV 반환)
       기존에 news_fetcher.py가 이미 쓰고 있는 ALPHA_VANTAGE_API_KEY를
       그대로 재사용하므로 별도 신청/비용이 전혀 없습니다.
     - symbol 파라미터 없이 1회만 호출하면 전체 시장의 예정된 실적 발표를
       모두 받아오므로, 종목별로 나눠 호출할 필요가 없어 무료 한도
       (25회/일)를 거의 소모하지 않습니다. (실행당 1회, 하루 2회 실행 시 2회)

사용 방법 (main.py에서):
    from fact_reference import build_fact_reference
    fact_block, fact_lookup = build_fact_reference(
        alpha_vantage_key=os.environ["ALPHA_VANTAGE_API_KEY"],
        now_kst=now_kst,
    )
    post = generator.generate_post(..., fact_reference_block=fact_block, fact_lookup=fact_lookup)
"""

import csv
import io
import logging
from datetime import datetime, date, timedelta, timezone

import requests

logger = logging.getLogger(__name__)

KST = timezone(timedelta(hours=9))

# ─────────────────────────────────────────────────────────────────────────────
# ① 정부 공식 고정 일정 (하드코딩)
# ─────────────────────────────────────────────────────────────────────────────
# 마지막 확인: 2026-07-22
# 출처:
#   FOMC   - https://www.federalreserve.gov/newsevents/pressreleases/monetary20250905a.htm 등
#            federalreserve.gov 2026년 일정 공지
#   BLS/OMB- https://www.whitehouse.gov/wp-content/uploads/2025/09/pfei_schedule_release_dates_cy2026.pdf
# 매년 하반기, 다음 해 일정이 새로 공지되면 이 표를 갱신해야 합니다.
_LAST_VERIFIED = "2026-07-22"

# FOMC 회의 (성명 발표일 = 이틀째 날, 미국 동부시간 오후 2시)
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

# BLS 고용보고서(비농업고용지표, Employment Situation) — 전월 데이터, 매월 발표
EMPLOYMENT_SITUATION_2026 = [
    "2026-01-09", "2026-02-06", "2026-03-06", "2026-04-03",
    "2026-05-08", "2026-06-05", "2026-07-02", "2026-08-07",
    "2026-09-04", "2026-10-02", "2026-11-06", "2026-12-04",
]

# CPI(소비자물가지수)
CPI_2026 = [
    "2026-01-13", "2026-02-11", "2026-03-11", "2026-04-10",
    "2026-05-12", "2026-06-10", "2026-07-14", "2026-08-12",
    "2026-09-11", "2026-10-14", "2026-11-10", "2026-12-10",
]

# PPI(생산자물가지수)
PPI_2026 = [
    "2026-01-14", "2026-02-12", "2026-03-12", "2026-04-14",
    "2026-05-13", "2026-06-11", "2026-07-15", "2026-08-13",
    "2026-09-10", "2026-10-15", "2026-11-13", "2026-12-15",
]

# 고용비용지수 (분기)
ECI_2026 = ["2026-01-30", "2026-04-30", "2026-07-31", "2026-10-30"]

MACRO_INDICATORS = [
    {"name": "FOMC 회의(금리 결정)", "keywords": ["FOMC", "연준 회의", "금리 결정", "연방공개시장위원회"],
     "dates": [m["statement_date"] for m in FOMC_2026]},
    {"name": "고용보고서(비농업고용지표)", "keywords": ["고용보고서", "비농업고용", "비농업 고용", "실업률 발표", "고용지표"],
     "dates": EMPLOYMENT_SITUATION_2026},
    {"name": "CPI(소비자물가지수)", "keywords": ["CPI", "소비자물가지수", "소비자물가"],
     "dates": CPI_2026},
    {"name": "PPI(생산자물가지수)", "keywords": ["PPI", "생산자물가지수", "생산자물가"],
     "dates": PPI_2026},
    {"name": "고용비용지수", "keywords": ["고용비용지수", "ECI"],
     "dates": ECI_2026},
]

# ─────────────────────────────────────────────────────────────────────────────
# ② 종목 워치리스트 (한글 별칭 → 티커) — 실적 발표일 대조용
# ─────────────────────────────────────────────────────────────────────────────
# 이 블로그(미국 증시 리뷰/프리뷰)에서 자주 언급되는 대형주 위주로 구성.
# 별칭이 본문에 등장하면 "실적" 관련 문맥에서 날짜를 대조합니다.
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

# 아래 두 목록은 fact_checker.py에서도 사용하므로 밑줄 없이 공개 이름으로 둡니다.
EARNINGS_KEYWORDS = ["실적", "어닝스", "earnings", "실적 발표", "실적발표"]
IMMINENT_WORDS = [
    "오늘", "오늘 밤", "오늘밤", "당일", "이번 주", "이번주",
    "장 마감 후", "장마감 후", "정규장 마감 후", "곧", "임박",
]


# ─────────────────────────────────────────────────────────────────────────────
# Alpha Vantage 실적 캘린더 조회 (무료 티어, 1회 호출)
# ─────────────────────────────────────────────────────────────────────────────

def fetch_earnings_calendar(alpha_vantage_key: str, horizon: str = "3month") -> dict:
    """
    Alpha Vantage EARNINGS_CALENDAR를 1회 호출해 워치리스트 종목의
    예정된 실적 발표일만 추려서 반환합니다.

    Returns:
        {"NVDA": {"date": "2026-08-26", "name": "NVIDIA Corp"}, ...}

    실패 시(네트워크 오류, 키 미설정, 한도 초과 등) 빈 dict를 반환하며,
    이 경우 실적 관련 사실 검증은 건너뛰고 매크로 지표 검증만 수행됩니다
    (파이프라인 전체를 중단시키지 않음).
    """
    if not alpha_vantage_key:
        logger.warning("ALPHA_VANTAGE_API_KEY 없음 — 실적 캘린더 조회 건너뜀")
        return {}

    try:
        resp = requests.get(
            "https://www.alphavantage.co/query",
            params={
                "function": "EARNINGS_CALENDAR",
                "horizon": horizon,
                "apikey": alpha_vantage_key,
            },
            timeout=20,
        )
        resp.raise_for_status()
        text = resp.text

        # 한도 초과/오류 시 CSV 대신 JSON 오류 메시지가 올 수 있음 (예: {"Note": ...})
        if text.strip().startswith("{"):
            logger.warning(f"실적 캘린더 응답이 CSV가 아님(한도 초과 가능성): {text[:200]}")
            return {}

        reader = csv.DictReader(io.StringIO(text))
        result = {}
        for row in reader:
            symbol = (row.get("symbol") or "").strip().upper()
            report_date = (row.get("reportDate") or "").strip()
            if symbol in WATCHLIST_ALIASES and report_date:
                # 같은 종목이 여러 번 나오면(드묾) 더 이른 날짜를 우선
                if symbol not in result or report_date < result[symbol]["date"]:
                    result[symbol] = {
                        "date": report_date,
                        "name": (row.get("name") or "").strip(),
                    }

        logger.info(f"실적 캘린더 조회 완료: 워치리스트 중 {len(result)}개 종목 확인됨")
        return result

    except Exception as e:
        logger.warning(f"실적 캘린더 조회 실패 (계속 진행): {e}")
        return {}


# ─────────────────────────────────────────────────────────────────────────────
# 프롬프트 주입용 텍스트 블록 + 검증용 lookup 구성
# ─────────────────────────────────────────────────────────────────────────────

def _parse_iso(d: str) -> date | None:
    try:
        return datetime.strptime(d, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


def _upcoming(dates: list[str], today: date, window_days: int) -> list[str]:
    """today 기준 -3일 ~ +window_days일 사이의 날짜만 추림 (과거 근접 포함, 너무 먼 미래 제외)."""
    out = []
    for d in dates:
        parsed = _parse_iso(d)
        if parsed is None:
            continue
        delta = (parsed - today).days
        if -3 <= delta <= window_days:
            out.append(d)
    return out


def build_fact_reference(
    alpha_vantage_key: str,
    now_kst: datetime,
    window_days: int = 45,
) -> tuple[str, dict]:
    """
    프롬프트에 주입할 "검증된 사실 정보" 텍스트 블록과,
    생성 후 팩트체크에 사용할 lookup 딕셔너리를 함께 만들어 반환합니다.

    Returns:
        (fact_block: str, fact_lookup: dict)

        fact_lookup 구조:
        {
            "earnings": {"NVDA": {"date": "2026-08-26", "aliases": [...], "name": "..."}, ...},
            "macro": [
                {"name": "...", "keywords": [...], "dates": ["2026-07-29", ...]},
                ...
            ],
        }
    """
    today = now_kst.date()

    # ── 매크로 지표: 향후 window_days일 이내 항목만 프롬프트에 노출 ──────────
    macro_upcoming = []
    for ind in MACRO_INDICATORS:
        upcoming_dates = _upcoming(ind["dates"], today, window_days)
        if upcoming_dates:
            macro_upcoming.append({
                "name": ind["name"],
                "keywords": ind["keywords"],
                "dates": upcoming_dates,
            })

    # ── 실적 캘린더: Alpha Vantage 1회 호출 ──────────────────────────────────
    earnings_raw = fetch_earnings_calendar(alpha_vantage_key)
    earnings_lookup = {}
    earnings_lines = []
    for symbol, info in sorted(earnings_raw.items(), key=lambda kv: kv[1]["date"]):
        aliases = WATCHLIST_ALIASES.get(symbol, [symbol])
        earnings_lookup[symbol] = {
            "date": info["date"],
            "aliases": aliases,
            "name": info.get("name", symbol),
        }
        # 프롬프트에는 앞으로 다가올(오늘 이후) 것 위주로 넉넉히(120일) 보여줌
        # → "아직 멀었다"는 것을 AI가 알 수 있게 하여 "오늘/이번 주" 같은
        #    임박 표현을 함부로 쓰지 않도록 함
        delta = (_parse_iso(info["date"]) - today).days if _parse_iso(info["date"]) else None
        if delta is not None and -3 <= delta <= 120:
            earnings_lines.append(f"  - {aliases[0]}({symbol}): {info['date']} (D{delta:+d})")

    # ── 텍스트 블록 조립 ──────────────────────────────────────────────────
    lines = []
    lines.append("────────────────────────────────────────")
    lines.append("검증된 사실 정보 (Fact Reference) — 반드시 준수")
    lines.append("────────────────────────────────────────")
    lines.append(
        "아래는 정부 공식 발표 일정과 실시간 조회된 실적 캘린더에서 확인된 "
        "'검증된 사실'입니다. 기업 실적 발표일이나 경제지표 발표일을 본문에 "
        "구체적인 날짜/요일/'오늘', '이번 주', '장 마감 후'처럼 언급할 때는 "
        "반드시 아래 목록에 근거해야 합니다."
    )
    lines.append(
        "아래 목록에 없는 기업의 실적 발표일이나, 목록에 있지만 날짜가 다른 "
        "경우는 절대로 임의로 추정하거나 재계산하지 마세요. 확실한 근거가 "
        "없다면 '정확한 일정은 아직 확인되지 않았습니다', '조만간 발표될 "
        "예정입니다'처럼 모호하게 표현하거나 해당 문장을 생략하세요."
    )
    lines.append("")

    if macro_upcoming:
        lines.append("[다가오는 주요 경제지표/이벤트 발표일]")
        for ind in macro_upcoming:
            lines.append(f"  - {ind['name']}: {', '.join(ind['dates'])}")
    else:
        lines.append("[다가오는 주요 경제지표/이벤트 발표일] 확인된 항목 없음")

    lines.append("")
    if earnings_lines:
        lines.append("[확인된 주요 기업 실적 발표일 — D-day는 포스팅 작성일 기준]")
        lines.extend(earnings_lines)
    else:
        lines.append(
            "[확인된 주요 기업 실적 발표일] 조회 실패 또는 확인된 항목 없음 "
            "— 이 경우 어떤 기업의 실적 발표일도 구체적으로 언급하지 마세요."
        )

    lines.append("────────────────────────────────────────")
    fact_block = "\n".join(lines)

    fact_lookup = {
        "earnings": earnings_lookup,
        "macro": macro_upcoming,
        "reference_date": today.isoformat(),
    }

    return fact_block, fact_lookup
