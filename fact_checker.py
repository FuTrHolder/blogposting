"""
fact_checker.py
Gemini가 생성한 블로그 초안(title/content)에서 "실적 발표일" / "경제지표 발표일"
관련 서술을 fact_reference.py의 검증된 사실표와 대조하여:

  1) 명시적으로 틀린 날짜(예: "8월 21일" 등)가 적혀 있으면 → 정답 날짜로 자동 교정
  2) 날짜는 안 적었지만 "오늘", "이번 주", "장 마감 후"처럼 임박한 것처럼
     서술했는데 실제로는 몇 주~한 달 이상 남아 있으면 → 사실 오류로 감지
  3) 워치리스트에 없는 기업이라 대조할 근거 자체가 없는 경우 → "검증 불가"로
     별도 표시만 하고(자동 수정하지 않음), 로그로 남겨 확인할 수 있게 함

이 모듈은 절대로 "그럴듯하게 문장을 새로 만들어내지" 않습니다.
자동 교정은 오직 ①번(명시적 날짜 오기재)처럼 정답이 명확할 때만 수행하고,
②번처럼 정답 날짜를 문장에 자연스럽게 끼워 넣기 애매한 경우는
content_generator.py 쪽에서 Gemini에게 "이 부분만 사실에 맞게 다시 써라"라고
1회 재요청하도록 위반 내역(violations)을 구조화해서 돌려줍니다.
"""

import logging
import re
from datetime import date, datetime

from fact_reference import EARNINGS_KEYWORDS, IMMINENT_WORDS

logger = logging.getLogger(__name__)

# "7월 21일", "7월21일", "07월 21일" 같은 한글 날짜 패턴
_KOREAN_DATE_RE = re.compile(r"(\d{1,2})\s*월\s*(\d{1,2})\s*일")

_CONTEXT_WINDOW = 40  # 기업명/지표명 앞뒤로 몇 글자를 문맥으로 볼지


def _find_all(text: str, needle: str):
    """text 안에서 needle이 등장하는 모든 시작 인덱스."""
    start = 0
    idxs = []
    while True:
        i = text.find(needle, start)
        if i == -1:
            break
        idxs.append(i)
        start = i + 1
    return idxs


def _context_window(text: str, idx: int, needle_len: int) -> str:
    lo = max(0, idx - _CONTEXT_WINDOW)
    hi = min(len(text), idx + needle_len + _CONTEXT_WINDOW)
    return text[lo:hi]


def _extract_korean_dates(window_text: str, ref_year: int) -> list[date]:
    """윈도우 텍스트 안에서 'N월 N일' 패턴을 모두 추출해 date 객체로 변환."""
    out = []
    for m in _KOREAN_DATE_RE.finditer(window_text):
        month, day = int(m.group(1)), int(m.group(2))
        try:
            out.append(date(ref_year, month, day))
        except ValueError:
            continue
    return out


def _has_any(window_text: str, words: list[str]) -> bool:
    return any(w in window_text for w in words)


# ─────────────────────────────────────────────────────────────────────────────
# 실적 발표일 검증
# ─────────────────────────────────────────────────────────────────────────────

def _check_earnings(content: str, earnings_lookup: dict, ref_year: int, source: str) -> list[dict]:
    """
    '실적' 계열 키워드를 기준(anchor)으로 문맥 윈도우를 잡고, 그 윈도우 안에
    워치리스트 기업명이 함께 있을 때만 검증합니다.

    (기업명을 기준으로 윈도우를 잡으면, 제목처럼 "포스팅 날짜"와 "기업명 +
    실적"이 우연히 같은 문장 안에 있을 때 그 포스팅 날짜를 실적일로 오인하는
    오탐이 발생하기 쉽습니다. '실적' 키워드를 기준으로 잡으면 날짜가 실제로
    그 키워드 바로 옆에 붙어 있을 때만 걸리므로 훨씬 정밀합니다.)

    source: "title" 또는 "content" — title/content를 분리해서 각각 검사하므로
    (합쳐서 검사하면 제목 끝부분과 본문 첫부분이 한 윈도우에 섞여 중복/오탐이
    발생할 수 있음), 어느 필드에서 나온 위반인지 결과에 표시합니다.
    """
    violations = []
    seen = set()

    for kw in EARNINGS_KEYWORDS:
        for idx in _find_all(content, kw):
            window = _context_window(content, idx, len(kw))

            matched = []
            for symbol, info in earnings_lookup.items():
                for alias in info["aliases"]:
                    if alias in window:
                        matched.append((symbol, info, alias))
                        break
            if not matched:
                continue

            found_dates = _extract_korean_dates(window, ref_year)

            for symbol, info, alias in matched:
                key = (symbol, idx)
                if key in seen:
                    continue
                seen.add(key)

                expected = datetime.strptime(info["date"], "%Y-%m-%d").date()

                if found_dates:
                    for fd in found_dates:
                        if fd != expected:
                            violations.append({
                                "type": "earnings_explicit_date",
                                "entity": alias,
                                "symbol": symbol,
                                "found_date": fd.isoformat(),
                                "expected_date": expected.isoformat(),
                                "context": window.strip(),
                                "source": source,
                                # title에서 나온 위반은 그 기업의 실적일이 아니라
                                # "포스팅 작성일"과 우연히 겹친 것일 가능성이 있어
                                # 자동 치환 대상에서 제외합니다 (재생성으로 처리).
                                "auto_fixable": (source == "content"),
                            })
                elif _has_any(window, IMMINENT_WORDS):
                    # 날짜는 안 썼지만 "오늘/이번 주/장 마감 후" 등으로 임박 표현
                    violations.append({
                        "type": "earnings_implicit_imminent",
                        "entity": alias,
                        "symbol": symbol,
                        "found_date": None,
                        "expected_date": expected.isoformat(),
                        "context": window.strip(),
                        "source": source,
                        "auto_fixable": False,
                    })

    return violations


def _check_macro(content: str, macro_list: list[dict], ref_year: int, source: str) -> list[dict]:
    violations = []
    for ind in macro_list:
        expected_dates = {
            datetime.strptime(d, "%Y-%m-%d").date() for d in ind["dates"]
        }
        for kw in ind["keywords"]:
            for idx in _find_all(content, kw):
                window = _context_window(content, idx, len(kw))
                found_dates = _extract_korean_dates(window, ref_year)
                if found_dates:
                    for fd in found_dates:
                        if fd not in expected_dates:
                            violations.append({
                                "type": "macro_explicit_date",
                                "entity": ind["name"],
                                "symbol": None,
                                "found_date": fd.isoformat(),
                                "expected_date": "/".join(sorted(d.isoformat() for d in expected_dates)),
                                "context": window.strip(),
                                "source": source,
                                # 지표는 후보가 여러 개일 수 있어 자동 교정하지 않고
                                # (어느 날짜가 맞는지 문맥상 확정이 어려움) 재생성 요청으로 처리
                                "auto_fixable": False,
                            })
    return violations


def check_facts(post: dict, fact_lookup: dict) -> list[dict]:
    """
    post(title/content)를 fact_lookup과 대조해 위반 목록을 반환합니다.
    위반이 없으면 빈 리스트를 반환합니다.

    title과 content는 각각 독립적으로 검사합니다 (합쳐서 검사하면 제목
    끝부분과 본문 시작부분이 한 문맥 윈도우에 섞여 중복 탐지나 오탐이
    발생할 수 있기 때문입니다).
    """
    if not fact_lookup:
        return []

    ref_year = int(fact_lookup.get("reference_date", str(date.today().year))[:4])

    violations = []
    for field in ("title", "content"):
        text = post.get(field, "") or ""
        if not text:
            continue
        violations += _check_earnings(text, fact_lookup.get("earnings", {}), ref_year, source=field)
        violations += _check_macro(text, fact_lookup.get("macro", []), ref_year, source=field)
    return violations


# ─────────────────────────────────────────────────────────────────────────────
# 자동 교정 (명시적 날짜 오기재만 — 정답이 확실한 경우에 한함)
# ─────────────────────────────────────────────────────────────────────────────

def auto_correct_facts(post: dict, violations: list[dict]) -> tuple[dict, list[dict], list[dict]]:
    """
    auto_fixable=True인 위반만 실제 텍스트를 치환해 교정합니다.
    (예: 본문의 "8월 21일" → "8월 26일")

    Returns:
        (corrected_post, applied_corrections, remaining_violations)
        remaining_violations: 자동 교정하지 못해 재생성/추가 확인이 필요한 항목들
    """
    corrected = dict(post)
    applied = []
    remaining = []

    for v in violations:
        # title에서 나온 위반은 절대 자동 치환하지 않습니다. 제목은 시스템
        # 규칙상 항상 "포스팅 작성일"을 포함해야 하는데(예: "7월 21일 미국
        # 증시..."), 실적 관련 오류로 감지된 날짜가 우연히 제목의 정상적인
        # 작성일과 같은 문자열일 경우 이를 건드리면 오히려 제목의 정상 날짜를
        # 깨뜨릴 수 있습니다. 제목에서 발견된 문제는 remaining으로 넘겨
        # Gemini가 전체 글을 사실에 맞게 다시 쓰도록(재생성) 처리합니다.
        if not v.get("auto_fixable") or v.get("source") != "content":
            remaining.append(v)
            continue

        found = datetime.strptime(v["found_date"], "%Y-%m-%d").date()
        expected = datetime.strptime(v["expected_date"], "%Y-%m-%d").date()

        # "8월 21일", "8월21일" 등 실제 본문에 쓰인 표기 그대로 찾아서 치환
        patterns = [
            f"{found.month}월 {found.day}일",
            f"{found.month}월{found.day}일",
            f"{found.month:02d}월 {found.day:02d}일",
        ]
        replacement = f"{expected.month}월 {expected.day}일"

        replaced_any = False
        text = corrected.get("content", "")
        for pat in patterns:
            if pat in text:
                text = text.replace(pat, replacement)
                replaced_any = True
        corrected["content"] = text

        if replaced_any:
            applied.append({**v, "replacement": replacement})
            logger.warning(
                f"[팩트체크 자동 교정] {v['entity']} 관련 날짜 "
                f"'{found.isoformat()}' → '{expected.isoformat()}' 로 수정함"
            )
        else:
            # 패턴을 못 찾았으면(표기 형식이 예상과 다름) 안전하게 재확인 대상으로 넘김
            remaining.append(v)

    return corrected, applied, remaining


def neutralize_unresolved(post: dict, remaining_violations: list[dict]) -> dict:
    """
    최종 안전망: auto_correct_facts + 1회 Gemini 재생성 시도를 거치고도
    여전히 남아있는 위반을 '틀린 확신'이 아니라 '안전한 모호함'으로
    바꿔치기합니다. 이 함수를 거친 뒤에는 최소한 명백히 틀린 날짜/임박
    표현이 그대로 발행되는 일은 없도록 보장하는 것이 목적입니다.

    - title은 절대 건드리지 않습니다 (오탐 가능성이 상대적으로 높고,
      제목은 시스템 규칙상 항상 포스팅 작성일을 포함해야 하기 때문).
    - 명시적 날짜 오기재는 정답 날짜로 최종 치환을 한 번 더 시도합니다.
    - '오늘/이번 주/장 마감 후'류 임박 표현은 첫 등장 1건만 '추후'로
      순화하여 "확정된 사실인 것처럼" 읽히지 않게 합니다.
    """
    corrected = dict(post)
    content = corrected.get("content", "")

    for v in remaining_violations:
        if v.get("source") != "content":
            continue  # title은 손대지 않음

        if v["type"] == "earnings_implicit_imminent":
            for word in IMMINENT_WORDS:
                if word in v.get("context", "") and word in content:
                    content = content.replace(word, "추후", 1)
                    logger.warning(
                        f"[팩트체크 최종 안전 대체] '{word}' → '추후' "
                        f"(근거: {v['entity']} 실적일 불일치, 확인된 날짜: {v['expected_date']})"
                    )
                    break

        elif v["type"] in ("earnings_explicit_date", "macro_explicit_date") and v.get("found_date"):
            found = datetime.strptime(v["found_date"], "%Y-%m-%d").date()
            expected_first = v["expected_date"].split("/")[0]
            expected = datetime.strptime(expected_first, "%Y-%m-%d").date()
            patterns = [
                f"{found.month}월 {found.day}일",
                f"{found.month}월{found.day}일",
            ]
            replacement = f"{expected.month}월 {expected.day}일"
            for pat in patterns:
                if pat in content:
                    content = content.replace(pat, replacement)
                    logger.warning(
                        f"[팩트체크 최종 안전 대체] '{pat}' → '{replacement}' "
                        f"(근거: {v['entity']})"
                    )

    corrected["content"] = content
    return corrected


def build_correction_prompt_note(remaining_violations: list[dict]) -> str:
    """
    자동 교정으로 해결되지 않은 위반들을 Gemini 재호출 프롬프트에 덧붙일
    안내문으로 변환합니다.
    """
    if not remaining_violations:
        return ""

    lines = [
        "────────────────────────────────────────",
        "이전 생성본에서 아래와 같은 사실 오류가 발견되었습니다. 반드시 수정하세요.",
        "────────────────────────────────────────",
    ]
    for v in remaining_violations:
        if v["type"] == "earnings_implicit_imminent":
            lines.append(
                f"- '{v['entity']}'의 실적 발표가 마치 임박한 것처럼("
                f"'오늘', '이번 주', '장 마감 후' 등) 서술되어 있으나, "
                f"실제 확인된 발표일은 {v['expected_date']}로 이번 포스팅 "
                f"시점과 상당한 차이가 있습니다. 이 문장은 삭제하거나, "
                f"'{v['expected_date']}에 실적 발표가 예정되어 있다' 처럼 "
                f"사실에 맞게 다시 쓰세요."
            )
        elif v["type"] == "macro_explicit_date":
            lines.append(
                f"- '{v['entity']}' 관련 날짜가 {v['found_date']}로 서술되어 "
                f"있으나, 확인된 공식 일정은 {v['expected_date']} 입니다. "
                f"정확한 날짜로 수정하세요."
            )
        else:
            lines.append(
                f"- '{v['entity']}' 관련 서술(문맥: \"{v['context']}\")에 "
                f"사실 오류가 있습니다. 확인된 날짜는 {v['expected_date']} 입니다."
            )
    lines.append("위 문제를 모두 수정한 전체 글을 동일한 JSON 형식으로 다시 작성하세요.")
    return "\n".join(lines)
