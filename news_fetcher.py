"""
뉴스 및 시장 데이터 수집 모듈
- Yahoo Finance RSS: 미국 증시 뉴스
- Alpha Vantage API: 주요 지수 추종 ETF 시세 (SPY, QQQ, DIA)
- Yahoo Finance 차트 API: Alpha Vantage 실패 시 실제 지수(^GSPC, ^IXIC, ^DJI) 대체
"""

import time
from urllib.parse import quote as _urlquote

import feedparser
import requests
import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

# 수집할 Yahoo Finance RSS 피드 목록
YAHOO_FINANCE_FEEDS = [
    "https://feeds.finance.yahoo.com/rss/2.0/headline?s=%5EGSPC,%5EIXIC,%5EDJI&region=US&lang=en-US",
    "https://feeds.finance.yahoo.com/rss/2.0/headline?s=SPY,QQQ,DIA&region=US&lang=en-US",
]

# Alpha Vantage GLOBAL_QUOTE는 주식·ETF 시세용이라 ^GSPC 같은 지수 심볼은
# 빈 응답({})을 돌려줍니다. → 지수를 추종하는 대표 ETF로 조회합니다.
# (등락률은 지수와 거의 같지만, 가격은 지수 값이 아니라 ETF 가격입니다.)
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

# Fear & Greed 엔드포인트 (순서대로 시도)
_FEAR_GREED_URLS = [
    "https://production.dataviz.cnn.io/index/fearandgreed/graphdata",
    "https://fear-and-greed-index.p.rapidapi.com/v1/fgi",  # 비공식 미러 (헤더 없으면 실패 → skip)
]


class NewsFetcher:
    def __init__(self, alpha_vantage_key: str):
        self.av_key = alpha_vantage_key
        self._av_exhausted = False  # 일일 한도 초과 시 이후 AV 호출 생략

    # ── 뉴스 수집 ──────────────────────────────────────────────────────────
    def get_top_news(self, limit: int = 8) -> list[dict]:
        """Yahoo Finance RSS에서 최신 미국 증시 뉴스를 수집합니다."""
        articles = []
        seen_titles = set()

        for feed_url in YAHOO_FINANCE_FEEDS:
            try:
                feed = feedparser.parse(feed_url)
                for entry in feed.entries:
                    title = entry.get("title", "").strip()
                    if not title or title in seen_titles:
                        continue
                    seen_titles.add(title)
                    articles.append({
                        "title": title,
                        "summary": entry.get("summary", ""),
                        "link": entry.get("link", ""),
                        "published": entry.get("published", ""),
                    })
            except Exception as e:
                logger.warning(f"RSS 피드 수집 실패 ({feed_url}): {e}")

        # Alpha Vantage 뉴스 감성 분석 API 추가
        av_news = self._fetch_av_news()
        for item in av_news:
            if item["title"] not in seen_titles:
                seen_titles.add(item["title"])
                articles.append(item)

        logger.info(f"총 {len(articles)}개 뉴스 수집됨")
        return articles[:limit]

    def _fetch_av_news(self) -> list[dict]:
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
                articles.append({
                    "title": item.get("title", ""),
                    "summary": item.get("summary", ""),
                    "link": item.get("url", ""),
                    "published": item.get("time_published", ""),
                    "sentiment": item.get("overall_sentiment_label", "Neutral"),
                    "sentiment_score": item.get("overall_sentiment_score", 0),
                })
            return articles
        except Exception as e:
            # 예외 문자열에 apikey가 담긴 URL이 들어갈 수 있어 타입명만 기록
            logger.warning(f"Alpha Vantage 뉴스 API 실패: {type(e).__name__}")
            return []

    # ── 시장 데이터 수집 ────────────────────────────────────────────────────
    def get_market_summary(self) -> dict:
        """주요 지수 시세를 가져옵니다. (Alpha Vantage ETF → Yahoo 지수 순)"""
        summary = {}
        for index_name, etf in AV_INDEX_ETFS.items():
            data = self._fetch_quote(etf)
            if data:
                # ETF 기준임을 키 이름에 명시 → 프롬프트에도 그대로 전달됨
                summary[f"{index_name} ({etf} ETF 기준)"] = data
            else:
                data = self._fetch_yahoo_quote(YAHOO_INDEX_SYMBOLS[index_name])
                if data:
                    summary[index_name] = data
                else:
                    logger.error(f"{index_name} 시세를 모든 소스에서 가져오지 못했습니다.")
            time.sleep(1.3)  # 무료 한도: 분당 5회

        logger.info(f"지수 데이터 {len(summary)}/{len(AV_INDEX_ETFS)}건 확보")
        summary["fear_greed"] = self._fetch_fear_greed()
        return summary

    def _fetch_quote(self, symbol: str) -> dict | None:
        """Alpha Vantage GLOBAL_QUOTE (주식/ETF). 실패 원인을 로그로 남깁니다."""
        if self._av_exhausted:
            return None

        url = "https://www.alphavantage.co/query"
        params = {"function": "GLOBAL_QUOTE", "symbol": symbol, "apikey": self.av_key}
        try:
            resp = requests.get(url, params=params, timeout=10)
            payload = resp.json()

            # 한도 초과/오류는 {"Information": ...} 등으로 오므로 반드시 구분해서 기록
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
            return {
                "price": f"{price:,.2f}",
                "change": f"{change:+.2f}",
                "change_pct": f"{change_pct:+.2f}%",
                "direction": "상승 📈" if change >= 0 else "하락 📉",
                "as_of": q.get("07. latest trading day", ""),
                "source": "Alpha Vantage",
            }
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
                headers={"User-Agent": "Mozilla/5.0"},
                timeout=10,
            )
            resp.raise_for_status()
            result = resp.json()["chart"]["result"][0]
            closes = [c for c in result["indicators"]["quote"][0]["close"] if c is not None]
            if len(closes) < 2:
                logger.warning(f"Yahoo 종가 데이터 부족 ({symbol})")
                return None

            price, prev = closes[-1], closes[-2]
            change = price - prev
            change_pct = change / prev * 100 if prev else 0.0
            return {
                "price": f"{price:,.2f}",
                "change": f"{change:+.2f}",
                "change_pct": f"{change_pct:+.2f}%",
                "direction": "상승 📈" if change >= 0 else "하락 📉",
                "as_of": "",
                "source": "Yahoo Finance",
            }
        except Exception as e:
            logger.warning(f"Yahoo 시세 조회 실패 ({symbol}): {type(e).__name__}")
            return None

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

                # 빈 응답 방어
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
