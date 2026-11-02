"""
뉴스 및 시장 데이터 수집 모듈 (v2 — 신선도/세션 메타데이터 추가)

- Yahoo Finance RSS: 미국 증시 뉴스 (발행 시각 포함)
- Alpha Vantage API: 주요 지수 추종 ETF 정규장 종가 (SPY, QQQ, DIA)
- Yahoo Finance 차트 API: Alpha Vantage 실패 시 실제 지수(^GSPC, ^IXIC, ^DJI) 대체
- [신규] evening 모드: Yahoo 차트 API(includePrePost=true)로 ETF 프리마켓 시세,
  ES=F / NQ=F / YM=F 선물 시세를 추가 수집 (키 불필요·무료, 비공식 엔드포인트)

v2 변경 이유 (2026-11-02 저녁 원고 오류):
  Alpha Vantage GLOBAL_QUOTE는 '마지막 정규장 종가/등락률'만 제공합니다. 저녁 9시(KST)
  시점에는 직전 거래일(금요일) 종가 값인데, 이것이 '현재 프리마켓/선물'로 라벨링되어
  프롬프트에 들어갔습니다. 이제 모든 시세 항목은 아래 메타데이터를 가집니다.
    kind    : "regular"(정규장 종가) | "premarket"(프리마켓·장중) | "futures"(선물)
    session : 데이터가 속한 세션
    basis   : 사람이 읽는 기준 설명 (프롬프트에 그대로 표기)
    as_of / as_of_kst : 데이터 시각
    stale   : 예상 거래일과 다른 낡은 데이터인지
    usable  : 프롬프트에 사용해도 되는지

호출 호환성: get_market_summary()는 기존처럼 인자 없이 호출해도 됩니다.
mode를 생략하면 환경변수 POST_MODE → (없으면) KST 15시 기준으로 자동 판별합니다.
기존 키(price/change/change_pct/direction/source)는 그대로 유지됩니다.
"""

import logging
import os
import time
from datetime import date, datetime, timedelta, timezone
from urllib.parse import quote as _urlquote
from zoneinfo import ZoneInfo

import feedparser
import requests

logger = logging.getLogger(__name__)

KST = timezone(timedelta(hours=9))
NY_TZ = ZoneInfo("America/New_York")

# 수집할 Yahoo Finance RSS 피드 목록
YAHOO_FINANCE_FEEDS = [
    "https://feeds.finance.yahoo.com/rss/2.0/headline?s=%5EGSPC,%5EIXIC,%5EDJI&region=US&lang=en-US",
    "https://feeds.finance.yahoo.com/rss/2.0/headline?s=SPY,QQQ,DIA&region=US&lang=en-US",
]

# Alpha Vantage GLOBAL_QUOTE는 주식·ETF 시세용이라 ^GSPC 같은 지수 심볼은
# 빈 응답({})을 돌려줍니다. → 지수를 추종하는 대표 ETF로 조회합니다.
AV_INDEX_ETFS = {
    "S&P 500": "SPY",
    "NASDAQ": "QQQ",       # 나스닥100 추종 (나스닥 종합과 약간 다름)
    "DOW JONES": "DIA",
}

# Alpha Vantage 실패 시 대체 소스 (Yahoo Finance 차트 API, 키 불필요·무료)
YAHOO_INDEX_SYMBOLS = {
    "S&P 500": "^GSPC",
    "NASDAQ": "^IXIC",
    "DOW JONES": "^DJI",
}

# evening 모드 전용: 선물 심볼 (Yahoo Finance)
YAHOO_FUTURES_SYMBOLS = {
    "S&P 500": "ES=F",
    "NASDAQ": "NQ=F",
    "DOW JONES": "YM=F",
}

# 프리마켓/선물 마지막 봉이 이보다 오래되면 '낡은 데이터'로 보고 버립니다(분).
INTRADAY_MAX_AGE_MIN = {"pre": 180, "futures": 90}

# main.py의 US_MARKET_HOLIDAYS_2026과 동일 (연말에 다음 해 값으로 갱신 필요)
_US_MARKET_HOLIDAYS = {
    (1, 1), (1, 19), (2, 16), (4, 3), (5, 25),
    (6, 19), (7, 3), (9, 7), (11, 26), (12, 25),
}

# Fear & Greed 엔드포인트 (순서대로 시도)
_FEAR_GREED_URLS = [
    "https://production.dataviz.cnn.io/index/fearandgreed/graphdata",
    "https://fear-and-greed-index.p.rapidapi.com/v1/fgi",  # 비공식 미러 (헤더 없으면 실패 → skip)
]

_YAHOO_UA = {"User-Agent": "Mozilla/5.0"}


# ─────────────────────────────────────────────────────────────────────────────
# 시간/세션 유틸 (zoneinfo 사용 → 썸머타임 자동 반영)
# ─────────────────────────────────────────────────────────────────────────────

def _resolve_mode(mode: str | None) -> str:
    m = (mode or os.environ.get("POST_MODE", "")).strip().lower()
    if m in ("morning", "evening"):
        return m
    return "morning" if datetime.now(KST).hour < 15 else "evening"


def _session_of(ts_utc: datetime) -> str:
    """UTC 시각이 미국 주식 시장의 어느 세션인지 반환 (pre/regular/post/closed)."""
    et = ts_utc.astimezone(NY_TZ)
    if et.weekday() >= 5:
        return "closed"
    t = et.hour * 60 + et.minute
    if 4 * 60 <= t < 9 * 60 + 30:
        return "pre"
    if 9 * 60 + 30 <= t < 16 * 60:
        return "regular"
    if 16 * 60 <= t < 20 * 60:
        return "post"
    return "closed"


def _fmt_kst(dt: datetime) -> str:
    return dt.astimezone(KST).strftime("%m-%d %H:%M KST")


def _fmt_et(dt: datetime) -> str:
    et = dt.astimezone(NY_TZ)
    return et.strftime("%m-%d %H:%M ") + (et.tzname() or "ET")


def _is_non_trading_day(d: date) -> bool:
    return d.weekday() >= 5 or (d.month, d.day) in _US_MARKET_HOLIDAYS


def _expected_last_session_date(now_utc: datetime) -> date:
    """지금 시점에서 '가장 최근에 마감된 정규장' 날짜."""
    et = now_utc.astimezone(NY_TZ)
    after_close = (et.hour * 60 + et.minute) >= 16 * 60
    cand = et.date() if after_close else et.date() - timedelta(days=1)
    for _ in range(10):
        if not _is_non_trading_day(cand):
            return cand
        cand -= timedelta(days=1)
    return cand


def _entry(
    price: float, change: float, pct: float, *,
    kind: str, session: str, source: str, basis: str,
    as_of: str = "", as_of_kst: str = "", stale: bool = False, note: str = "",
) -> dict:
    return {
        "price": f"{price:,.2f}",
        "change": f"{change:+.2f}",
        "change_pct": f"{pct:+.2f}%",
        "direction": "상승 📈" if change >= 0 else "하락 📉",
        "kind": kind,
        "session": session,
        "source": source,
        "basis": basis,
        "as_of": as_of,
        "as_of_kst": as_of_kst,
        "stale": stale,
        "usable": True,
        "note": note,
    }


def _prev_regular_close(meta: dict, bars: list, day: date, session: str) -> float | None:
    """
    프리마켓/장중 등락률의 기준가(직전 정규장 종가)를 구합니다.
    1순위: meta.regularMarketPrice (단, 그 시각이 day 이전 거래일일 때만 — 프리마켓
           시간대에는 직전 정규장 종가를 가리킴)
    2순위: 5분봉 중 정규장 구간(09:30~16:00 ET)의 마지막 봉 종가
    """
    if session == "pre":
        rt, rp = meta.get("regularMarketTime"), meta.get("regularMarketPrice")
        if rt and rp:
            try:
                rdt = datetime.fromtimestamp(int(rt), tz=timezone.utc)
                if rdt.astimezone(NY_TZ).date() < day:
                    return float(rp)
            except Exception:
                pass

    cands = [
        c for ts, c in bars
        if _session_of(ts) == "regular" and ts.astimezone(NY_TZ).date() < day
    ]
    return cands[-1] if cands else None


def _futures_reference(bars: list, last_ts: datetime) -> float | None:
    """선물 등락 기준가: 직전 정규장 마감 시각(16:00 ET) 직전 봉(15:50~16:00 ET)의 종가."""
    for ts, close in reversed(bars):
        if ts >= last_ts:
            continue
        et = ts.astimezone(NY_TZ)
        t = et.hour * 60 + et.minute
        if et.weekday() < 5 and 15 * 60 + 50 <= t < 16 * 60:
            return close
    return None


def _pub_info(published_utc: datetime | None, now_utc: datetime) -> dict:
    if published_utc is None:
        return {"published_utc": "", "published_kst": "", "age_hours": None}
    age = max(0.0, (now_utc - published_utc).total_seconds() / 3600.0)
    return {
        "published_utc": published_utc.isoformat(),
        "published_kst": published_utc.astimezone(KST).strftime("%m/%d %H:%M KST"),
        "age_hours": round(age, 1),
    }


class NewsFetcher:
    def __init__(self, alpha_vantage_key: str):
        self.av_key = alpha_vantage_key
        self._av_exhausted = False  # 일일 한도 초과 시 이후 AV 호출 생략

    # ── 뉴스 수집 ──────────────────────────────────────────────────────────
    def get_top_news(self, limit: int = 8) -> list[dict]:
        """
        Yahoo Finance RSS + Alpha Vantage 뉴스. 기사마다 발행 시각(published_utc/
        published_kst)과 경과 시간(age_hours)을 붙이고 최신순으로 정렬합니다.
        """
        now_utc = datetime.now(timezone.utc)
        articles: list[dict] = []
        seen_titles = set()

        for feed_url in YAHOO_FINANCE_FEEDS:
            try:
                feed = feedparser.parse(feed_url)
                for entry in feed.entries:
                    title = entry.get("title", "").strip()
                    if not title or title in seen_titles:
                        continue
                    seen_titles.add(title)

                    pub_dt = None
                    pp = entry.get("published_parsed") or entry.get("updated_parsed")
                    if pp:
                        try:
                            pub_dt = datetime(*pp[:6], tzinfo=timezone.utc)
                        except Exception:
                            pub_dt = None

                    articles.append({
                        "title": title,
                        "summary": entry.get("summary", ""),
                        "link": entry.get("link", ""),
                        "published": entry.get("published", ""),
                        **_pub_info(pub_dt, now_utc),
                    })
            except Exception as e:
                logger.warning(f"RSS 피드 수집 실패 ({feed_url}): {e}")

        for item in self._fetch_av_news(now_utc):
            if item["title"] not in seen_titles:
                seen_titles.add(item["title"])
                articles.append(item)

        # 최신순 정렬 (발행 시각을 알 수 없는 기사는 뒤로)
        articles.sort(key=lambda a: a.get("published_utc") or "", reverse=True)

        old = sum(1 for a in articles[:limit] if (a.get("age_hours") or 0) > 36)
        logger.info(
            f"총 {len(articles)}개 뉴스 수집됨 (상위 {limit}건 중 36시간 초과 기사 {old}건)"
        )
        return articles[:limit]

    def _fetch_av_news(self, now_utc: datetime) -> list[dict]:
        """Alpha Vantage 뉴스 감성 분석 API로 추가 뉴스를 수집합니다."""
        if self._av_exhausted:
            return []

        url = "https://www.alphavantage.co/query"
        params = {
            "function": "NEWS_SENTIMENT",
            "tickers": "SPY,QQQ,AAPL,MSFT,NVDA",
            "limit": 10,
            "apikey": self.av_key,
        }
        try:
            resp = requests.get(url, params=params, timeout=10)
            data = resp.json()

            notice = data.get("Information") or data.get("Note")
            if notice:
                logger.warning(f"Alpha Vantage 뉴스 응답 안내: {str(notice)[:200]}")

            articles = []
            for item in data.get("feed", []):
                pub_dt = None
                raw = item.get("time_published", "")
                try:
                    pub_dt = datetime.strptime(raw, "%Y%m%dT%H%M%S").replace(tzinfo=timezone.utc)
                except Exception:
                    pub_dt = None
                articles.append({
                    "title": item.get("title", ""),
                    "summary": item.get("summary", ""),
                    "link": item.get("url", ""),
                    "published": raw,
                    "sentiment": item.get("overall_sentiment_label", "Neutral"),
                    "sentiment_score": item.get("overall_sentiment_score", 0),
                    **_pub_info(pub_dt, now_utc),
                })
            return articles
        except Exception as e:
            # 예외 문자열에 apikey가 담긴 URL이 들어갈 수 있어 타입명만 기록
            logger.warning(f"Alpha Vantage 뉴스 API 실패: {type(e).__name__}")
            return []

    # ── 시장 데이터 수집 ────────────────────────────────────────────────────
    def get_market_summary(self, mode: str | None = None) -> dict:
        """
        주요 지수 시세를 가져옵니다.
          - 공통: 정규장 종가 (Alpha Vantage ETF → Yahoo 지수 순), kind="regular"
          - evening: 프리마켓(ETF, kind="premarket") + 선물(kind="futures") 추가
        """
        mode = _resolve_mode(mode)
        now_utc = datetime.now(timezone.utc)
        expected = _expected_last_session_date(now_utc)
        summary: dict = {}

        for index_name, etf in AV_INDEX_ETFS.items():
            used_av = not self._av_exhausted
            key = f"{index_name} ({etf} ETF 기준)"
            data = self._fetch_quote(etf)
            if not data:
                key = index_name
                data = self._fetch_yahoo_quote(YAHOO_INDEX_SYMBOLS[index_name])

            if data:
                self._mark_regular_freshness(data, expected, key)
                summary[key] = data
            else:
                logger.error(f"{index_name} 시세를 모든 소스에서 가져오지 못했습니다.")

            if used_av:
                time.sleep(1.3)  # 무료 한도: 분당 5회

        if mode == "evening":
            pre_cnt = fut_cnt = 0
            for index_name, etf in AV_INDEX_ETFS.items():
                data = self._fetch_premarket_quote(etf)
                if data:
                    summary[f"{index_name} ({etf} ETF 프리마켓)"] = data
                    pre_cnt += 1
                time.sleep(0.3)
            for index_name, sym in YAHOO_FUTURES_SYMBOLS.items():
                data = self._fetch_futures_quote(sym)
                if data:
                    summary[f"{index_name} 선물 ({sym})"] = data
                    fut_cnt += 1
                time.sleep(0.3)

            logger.info(f"프리마켓 {pre_cnt}/3건, 선물 {fut_cnt}/3건 확보")
            if pre_cnt == 0 and fut_cnt == 0:
                logger.warning(
                    "프리마켓/선물 수치를 하나도 확보하지 못했습니다 → 원고에는 "
                    "'전일 정규장 종가 기준'으로만 표기되고 프리마켓 방향은 단정하지 않습니다."
                )

        reg_cnt = sum(1 for v in summary.values() if v.get("kind") == "regular")
        logger.info(f"정규장 지수 데이터 {reg_cnt}/{len(AV_INDEX_ETFS)}건 확보")
        summary["fear_greed"] = self._fetch_fear_greed()
        return summary

    @staticmethod
    def _mark_regular_freshness(data: dict, expected: date, label: str) -> None:
        """정규장 종가의 기준일이 예상 거래일과 다르면 stale로 표시합니다."""
        as_of = data.get("as_of", "")
        if not as_of:
            return
        if as_of != expected.isoformat():
            data["stale"] = True
            logger.warning(
                f"[신선도] {label}: 기준일 {as_of} ≠ 예상 최근 거래일 {expected.isoformat()} "
                f"(휴장/데이터 지연 가능) — 원고에 기준일을 명시합니다."
            )

    def _fetch_quote(self, symbol: str) -> dict | None:
        """Alpha Vantage GLOBAL_QUOTE (주식/ETF 정규장 종가). 실패 원인을 로그로 남깁니다."""
        if self._av_exhausted:
            return None

        url = "https://www.alphavantage.co/query"
        params = {"function": "GLOBAL_QUOTE", "symbol": symbol, "apikey": self.av_key}
        try:
            resp = requests.get(url, params=params, timeout=10)
            payload = resp.json()

            notice = (payload.get("Information") or payload.get("Note")
                      or payload.get("Error Message"))
            if notice:
                text = str(notice)
                logger.warning(f"Alpha Vantage 응답 오류 ({symbol}): {text[:200]}")
                low = text.lower()
                if "rate limit" in low or "requests per day" in low or "25 requests" in low:
                    self._av_exhausted = True
                return None

            q = payload.get("Global Quote") or {}
            if not q.get("05. price"):
                logger.warning(f"Alpha Vantage Global Quote 비어 있음 ({symbol})")
                return None

            price = float(q.get("05. price", 0))
            change = float(q.get("09. change", 0))
            change_pct = float(str(q.get("10. change percent", "0%")).replace("%", "") or 0)
            as_of = q.get("07. latest trading day", "")
            return _entry(
                price, change, change_pct,
                kind="regular", session="regular_close", source="Alpha Vantage",
                basis=f"정규장 종가 ({as_of})" if as_of else "정규장 종가",
                as_of=as_of,
            )
        except Exception as e:
            # 예외 문자열에 apikey가 담긴 URL이 들어갈 수 있어 타입명만 기록
            logger.warning(f"시세 조회 실패 ({symbol}): {type(e).__name__}")
            return None

    def _fetch_yahoo_quote(self, symbol: str) -> dict | None:
        """Yahoo Finance 차트 API로 실제 지수(^GSPC 등)의 최근 2거래일 종가 기준 등락을 계산."""
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{_urlquote(symbol, safe='')}"
        try:
            resp = requests.get(
                url,
                params={"range": "5d", "interval": "1d"},
                headers=_YAHOO_UA,
                timeout=10,
            )
            resp.raise_for_status()
            result = resp.json()["chart"]["result"][0]
            ts_list = result.get("timestamp") or []
            raw_closes = result["indicators"]["quote"][0]["close"]
            pairs = [(t, c) for t, c in zip(ts_list, raw_closes) if c is not None]
            if len(pairs) < 2:
                logger.warning(f"Yahoo 종가 데이터 부족 ({symbol})")
                return None

            last_t, price = pairs[-1]
            prev = pairs[-2][1]
            change = price - prev
            change_pct = change / prev * 100 if prev else 0.0
            as_of = datetime.fromtimestamp(int(last_t), tz=timezone.utc).astimezone(NY_TZ).date().isoformat()
            return _entry(
                price, change, change_pct,
                kind="regular", session="regular_close", source="Yahoo Finance",
                basis=f"정규장 종가 ({as_of})", as_of=as_of,
            )
        except Exception as e:
            logger.warning(f"Yahoo 시세 조회 실패 ({symbol}): {type(e).__name__}")
            return None

    # ── 프리마켓 / 선물 (evening 전용) ───────────────────────────────────────
    def _fetch_intraday(self, symbol: str) -> tuple[dict, list]:
        """Yahoo 차트 API 5분봉(프리/애프터 포함). (meta, [(datetime_utc, close), ...])"""
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{_urlquote(symbol, safe='')}"
        resp = requests.get(
            url,
            params={"range": "5d", "interval": "5m", "includePrePost": "true"},
            headers=_YAHOO_UA,
            timeout=15,
        )
        resp.raise_for_status()
        result = resp.json()["chart"]["result"][0]
        meta = result.get("meta") or {}
        ts_list = result.get("timestamp") or []
        quote = ((result.get("indicators") or {}).get("quote") or [{}])[0]
        closes = quote.get("close") or []
        bars = [
            (datetime.fromtimestamp(int(t), tz=timezone.utc), float(c))
            for t, c in zip(ts_list, closes) if c is not None
        ]
        bars.sort(key=lambda x: x[0])
        return meta, bars

    def _fetch_premarket_quote(self, symbol: str) -> dict | None:
        """ETF 프리마켓 시세 (직전 정규장 종가 대비). 낡았거나 세션이 맞지 않으면 None."""
        try:
            meta, bars = self._fetch_intraday(symbol)
        except Exception as e:
            logger.warning(f"프리마켓 조회 실패 ({symbol}): {type(e).__name__}")
            return None
        if not bars:
            logger.warning(f"프리마켓 봉 데이터 없음 ({symbol})")
            return None

        last_ts, last_px = bars[-1]
        age_min = (datetime.now(timezone.utc) - last_ts).total_seconds() / 60
        session = _session_of(last_ts)

        if age_min > INTRADAY_MAX_AGE_MIN["pre"]:
            logger.warning(
                f"프리마켓 데이터 낡음 ({symbol}): 마지막 봉 {_fmt_et(last_ts)} "
                f"({age_min:.0f}분 전) → 사용 안 함"
            )
            return None
        if session not in ("pre", "regular"):
            logger.warning(
                f"프리마켓 세션 아님 ({symbol}): 마지막 봉 {_fmt_et(last_ts)} session={session} → 사용 안 함"
            )
            return None

        day = last_ts.astimezone(NY_TZ).date()
        ref = _prev_regular_close(meta, bars, day, session)
        if not ref:
            logger.warning(f"프리마켓 기준가(직전 정규장 종가)를 구하지 못함 ({symbol}) → 사용 안 함")
            return None

        change = last_px - ref
        pct = change / ref * 100
        label = "프리마켓" if session == "pre" else "정규장 진행 중"
        logger.info(
            f"[프리마켓 검증] {symbol}: 현재 {last_px:.2f} / 기준(직전 정규장 종가) {ref:.2f} "
            f"→ {pct:+.2f}% | 마지막 봉 {_fmt_et(last_ts)} ({_fmt_kst(last_ts)})"
        )
        return _entry(
            last_px, change, pct,
            kind="premarket", session=session, source="Yahoo Finance",
            basis=f"{label} (직전 정규장 종가 대비)",
            as_of=_fmt_et(last_ts), as_of_kst=_fmt_kst(last_ts),
        )

    def _fetch_futures_quote(self, symbol: str) -> dict | None:
        """지수 선물 시세 (직전 정규장 마감 시각 선물가 대비). 낡았으면 None."""
        try:
            meta, bars = self._fetch_intraday(symbol)
        except Exception as e:
            logger.warning(f"선물 조회 실패 ({symbol}): {type(e).__name__}")
            return None
        if not bars:
            logger.warning(f"선물 봉 데이터 없음 ({symbol})")
            return None

        last_ts, last_px = bars[-1]
        age_min = (datetime.now(timezone.utc) - last_ts).total_seconds() / 60
        if age_min > INTRADAY_MAX_AGE_MIN["futures"]:
            logger.warning(
                f"선물 데이터 낡음 ({symbol}): 마지막 봉 {_fmt_et(last_ts)} "
                f"({age_min:.0f}분 전) → 사용 안 함"
            )
            return None

        ref = _futures_reference(bars, last_ts)
        if not ref:
            logger.warning(f"선물 기준가(16:00 ET 직전 봉)를 구하지 못함 ({symbol}) → 사용 안 함")
            return None

        change = last_px - ref
        pct = change / ref * 100
        logger.info(
            f"[선물 검증] {symbol}: 현재 {last_px:.2f} / 기준(16:00 ET 직전 봉) {ref:.2f} "
            f"→ {pct:+.2f}% | 마지막 봉 {_fmt_et(last_ts)} ({_fmt_kst(last_ts)})"
        )
        return _entry(
            last_px, change, pct,
            kind="futures", session="futures", source="Yahoo Finance",
            basis="선물 (직전 정규장 마감 시각 선물가 대비)",
            as_of=_fmt_et(last_ts), as_of_kst=_fmt_kst(last_ts),
            note="선물 가격 수준은 ETF/지수와 다르므로 등락률 위주로만 서술",
        )

    def _fetch_fear_greed(self) -> dict:
        """
        CNN Fear & Greed 지수를 가져옵니다.
        - 응답이 비어 있거나 파싱 실패해도 {"score": None, "rating": "N/A"} 반환
        - 여러 User-Agent로 재시도
        """
        headers_list = [
            {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                ),
                "Accept": "application/json, text/plain, */*",
                "Referer": "https://edition.cnn.com/markets/fear-and-greed",
            },
            {"User-Agent": "Mozilla/5.0"},
        ]

        url = _FEAR_GREED_URLS[0]
        for headers in headers_list:
            try:
                resp = requests.get(url, timeout=10, headers=headers)

                if not resp.content or resp.status_code != 200:
                    logger.warning(
                        f"Fear & Greed 응답 불량 (status={resp.status_code}, "
                        f"len={len(resp.content)})"
                    )
                    continue

                data = resp.json()
                fg = data.get("fear_and_greed", {})
                score = fg.get("score")
                rating = fg.get("rating", "N/A")

                if score is None:
                    logger.warning("Fear & Greed JSON에 score 없음")
                    continue

                logger.info(f"Fear & Greed 지수: {round(score)} ({rating})")
                return {"score": round(score), "rating": rating}

            except requests.exceptions.JSONDecodeError as e:
                logger.warning(f"Fear & Greed JSON 파싱 실패: {e}")
            except Exception as e:
                logger.warning(f"Fear & Greed 조회 실패: {e}")

        logger.warning("Fear & Greed 지수 조회 최종 실패 → N/A 반환")
        return {"score": None, "rating": "N/A"}
