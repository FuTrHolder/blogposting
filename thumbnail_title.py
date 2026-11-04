"""
thumbnail_title.py
썸네일 중앙 오버레이에 표시할 제목을 정리합니다.
- 날짜, '미국 증시', '마감:', '전망:' 같은 말머리를 제거해 핵심 문구만 남깁니다.
- 말머리는 아래 화이트리스트에 있는 단어일 때만 제거하므로
  '나스닥 급락: 이유는 금리' 같은 의미 있는 콜론 문구는 보존됩니다.
- 제거 후 너무 짧아지면 원본 제목을 그대로 사용합니다.
"""
import html
import re

_LABEL = (
    r"(?:전일\s*마감\s*리뷰|전일\s*마감|마감\s*리뷰|마감\s*분석|장\s*마감|마감|"
    r"프리마켓\s*브리핑|프리마켓\s*프리뷰|프리마켓|개장\s*전|"
    r"시황\s*브리핑|시황\s*분석|시황|브리핑|프리뷰|리뷰|전망|분석|"
    r"이슈\s*체크|이슈|총정리|정리|체크포인트|체크)"
)
_SEP = r"[:：\-–—|]"

_DATE_RE = re.compile(
    r"^\s*(?:\d{4}\s*[년.\-/]\s*\d{1,2}\s*[월.\-/]\s*\d{1,2}\s*일?)\s*"
)
_MARKET_RE = re.compile(r"^\s*(?:미국\s*증시|미국증시|뉴욕\s*증시)\s*(?:" + _SEP + r")?\s*")
_LABEL_RE = re.compile(r"^\s*(?:" + _LABEL + r"\s*)+" + _SEP + r"\s*")
_LEAD_PUNCT_RE = re.compile(r"^[\s:：\-–—|,·]+")

_MIN_REMAIN = 6


def clean_overlay_title(title: str) -> str:
    original = html.unescape(str(title or "")).strip()
    text = original
    for _ in range(3):
        before = text
        text = _DATE_RE.sub("", text, count=1)
        text = _MARKET_RE.sub("", text, count=1)
        text = _LABEL_RE.sub("", text, count=1)
        text = _LEAD_PUNCT_RE.sub("", text)
        if text == before:
            break
    text = text.strip()
    if len(text.replace(" ", "")) < _MIN_REMAIN:
        return original
    return text
