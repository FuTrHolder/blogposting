"""
뉴스 및 시장 데이터 수집 모듈
- Yahoo Finance RSS: 미국 증시 뉴스
- Alpha Vantage API: 주요 지수 데이터 (S&P500, NASDAQ, DOW)
"""

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

# Alpha Vantage 주요 지수 심볼
INDEX_SYMBOLS = {
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
            logger.warning(f"Alpha Vantage 뉴스 API 실패: {e}")
            return []

    # ── 시장 데이터 수집 ────────────────────────────────────────────────────
    def get_market_summary(self) -> dict:
        """Alpha Vantage에서 주요 지수의 당일 데이터를 가져옵니다."""
        summary = {}
        for name, symbol in INDEX_SYMBOLS.items():
            data = self._fetch_quote(symbol)
            if data:
                summary[name] = data

        summary["fear_greed"] = self._fetch_fear_greed()
        return summary

    def _fetch_quote(self, symbol: str) -> dict | None:
        """단일 종목/지수의 시세를 조회합니다."""
        url = "https://www.alphavantage.co/query"
        params = {
            "function": "GLOBAL_QUOTE",
            "symbol": symbol,
            "apikey": self.av_key,
        }
        try:
            resp = requests.get(url, params=params, timeout=10)
            quote = resp.json().get("Global Quote", {})
            if not quote:
                return None
            price = float(quote.get("05. price", 0))
            change = float(quote.get("09. change", 0))
            change_pct = quote.get("10. change percent", "0%").replace("%", "")
            return {
                "price": f"{price:,.2f}",
                "change": f"{change:+.2f}",
                "change_pct": f"{float(change_pct):+.2f}%",
                "direction": "상승 📈" if change >= 0 else "하락 📉",
            }
        except Exception as e:
            logger.warning(f"시세 조회 실패 ({symbol}): {e}")
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
