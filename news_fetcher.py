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

        # Fear & Greed 지수 (대안: CNN API)
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
        """CNN Fear & Greed 지수를 가져옵니다 (비공식 API)."""
        try:
            url = "https://production.dataviz.cnn.io/index/fearandgreed/graphdata"
            resp = requests.get(url, timeout=8, headers={"User-Agent": "Mozilla/5.0"})
            data = resp.json()
            score = data["fear_and_greed"]["score"]
            rating = data["fear_and_greed"]["rating"]
            return {"score": round(score), "rating": rating}
        except Exception as e:
            logger.warning(f"Fear & Greed 지수 조회 실패: {e}")
            return {"score": None, "rating": "N/A"}
