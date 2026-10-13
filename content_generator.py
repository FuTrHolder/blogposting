"""
블로그 글 생성 모듈
Google Gemini API를 사용해 3,000자 내외의 한글 블로그 포스팅을 생성합니다.

모드:
  morning : 미국 전일 증시 마감 리뷰 (오전 9시 포스팅)
  evening : 전일 정규장 리뷰 + 애프터마켓~프리장 이슈 + 당일 경제지표/실적

v2: Gemini 호출을 gemini_client.py로 통일 (API 키 헤더 전송, 429/5xx/MAX_TOKENS 통합 처리)
"""

import json
import logging
import re

from gemini_client import call_gemini_text, models_from_env

logger = logging.getLogger(__name__)


# ── Gemini 모델 우선순위 ─────────────────────────────────────────────────────
# 환경변수 GEMINI_MODELS="모델1,모델2,..."로 코드 수정 없이 교체 가능
GEMINI_MODELS = [
    "gemini-3.6-flash",
    "gemini-3.5-flash",
    "gemini-2.5-flash",
]


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
DISCLAIMER_HTML = (
    '<blockquote data-ke-style="style3">'
    '<p>본 콘텐츠는 공개된 정보를 바탕으로 작성된 참고 자료이며, 어떠한 투자 권유도 포함하지 않습니다. 투자 판단 및 그에 따른 손익에 대한 책임은 전적으로 투자자 본인에게 귀속됩니다.</p>'
    '</blockquote>'
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

<blockquote data-ke-style="style3"><p>본 콘텐츠는 공개된 정보를 바탕으로 작성된 참고 자료이며, 어떠한 투자 권유도 포함하지 않습니다. 투자 판단 및 그에 따른 손익에 대한 책임은 전적으로 투자자 본인에게 귀속됩니다.</p></blockquote>

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


_JSON_OUTPUT_RULE = """
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
    + _JSON_OUTPUT_RULE
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
    + _JSON_OUTPUT_RULE
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


def _pick_mixed_market_phrase(korean_date: str, prefix: str) -> str:
    idx = sum(ord(c) for c in korean_date) % len(_MIXED_MARKET_SYNONYMS)
    return f"{prefix} {_MIXED_MARKET_SYNONYMS[idx]}"


def _format_market_lines(market_data: dict) -> str:
    text = ""
    for name, data in market_data.items():
        if name == "fear_greed" or not data:
            continue
        text += (
            f"- {name}: {data.get('price', '')} "
            f"({data.get('change', '')}, "
            f"{data.get('change_pct', '')}) "
            f"{data.get('direction', '')}\n"
        )
    return text


def _format_fear_greed(market_data: dict) -> str:
    fg = market_data.get("fear_greed", {})
    if fg.get("score") is not None:
        return (
            f"\n[Fear & Greed 지수] "
            f"{fg.get('score')}/100 "
            f"({fg.get('rating', '')})\n"
        )
    return ""


def _format_news(news_list: list[dict], header: str) -> str:
    text = f"\n{header}\n"
    for i, news in enumerate(news_list, 1):
        sentiment = news.get("sentiment", "")
        suffix = f" [{sentiment}]" if sentiment else ""
        text += f"{i}. {news.get('title', '')}{suffix}\n"
        if news.get("summary"):
            summary = news["summary"][:200].replace("\n", " ")
            text += f"   → {summary}\n"
    return text


def _count_directions(market_data: dict) -> tuple[int, int]:
    directions = [
        v.get("direction", "")
        for k, v in market_data.items()
        if k != "fear_greed" and v and isinstance(v, dict)
    ]
    up = sum(1 for d in directions if "상승" in d)
    down = sum(1 for d in directions if "하락" in d)
    return up, down


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
    market_text += _format_market_lines(market_data)
    market_text += _format_fear_greed(market_data)

    news_text = _format_news(news_list, "[전일 주요 뉴스]")

    up_count, down_count = _count_directions(market_data)

    if up_count > down_count:
        hint = "전일 미국 시장은 전반적으로 상승 마감했습니다."
    elif down_count > up_count:
        hint = "전일 미국 시장은 전반적으로 하락 마감했습니다."
    else:
        hint = _pick_mixed_market_phrase(korean_date, "전일 미국 시장은")

    fact_block_text = f"\n{fact_reference_block}\n" if fact_reference_block else ""

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
    market_text += _format_market_lines(market_data)
    market_text += _format_fear_greed(market_data)

    news_text = _format_news(news_list, "[수집된 주요 뉴스 & 이슈]")

    up_count, down_count = _count_directions(market_data)

    if up_count > down_count:
        premarket_hint = "프리마켓은 전반적으로 강세 흐름입니다."
    elif down_count > up_count:
        premarket_hint = "프리마켓은 전반적으로 약세 흐름입니다."
    else:
        premarket_hint = _pick_mixed_market_phrase(korean_date, "프리마켓은")

    fact_block_text = f"\n{fact_reference_block}\n" if fact_reference_block else ""

    return (
        f"{market_text}"
        f"{news_text}\n"
        f"[프리마켓 요약] {premarket_hint}\n"
        f"{fact_block_text}\n"
        "위 데이터를 바탕으로 저녁 9시 블로그 포스팅을 "
        "작성하세요.\n\n"
        "작성 흐름:\n"
        "1) 전일 미국 정규장 마감 리뷰\n"
        "2) 애프터마켓 이후 현재 프리장까지 주요 이슈\n"
        "3) 오늘 밤 발표 예정 경제지표·기업실적\n"
        "4) 현재 프리마켓/선물 동향\n"
        "5) 오늘 밤 강세·약세 시나리오\n\n"
        "D+0이 아닌 항목은 오늘 밤 발표 예정이라고 "
        "표현하지 마세요.\n"
        "지정된 JSON 형식으로만 응답하세요."
    )


class ContentGenerator:

    def __init__(self, api_key: str):
        self.api_key = api_key

    def _call_gemini(
        self,
        system: str,
        prompt: str,
        max_retries: int = 3,
    ) -> str:
        """공용 gemini_client 사용 (키 헤더 전송, 429/5xx/MAX_TOKENS 통합 처리)."""
        models = models_from_env("GEMINI_MODELS", GEMINI_MODELS)

        return call_gemini_text(
            self.api_key,
            system,
            prompt,
            models,
            temperature=0.85,
            max_output_tokens=16384,
            timeout=120,
            max_retries=2,
        )

    def generate_post(
        self,
        date: str,
        market_data: dict,
        news_list: list[dict],
        mode: str = "morning",
        korean_date: str | None = None,
        us_market_date: str | None = None,
        korean_datetime_str: str | None = None,
        ny_reference_str: str | None = None,
        fact_reference_block: str = "",
        fact_lookup: dict | None = None,
    ) -> dict:

        _korean_date = korean_date or date
        _us_market_date = us_market_date or date
        _korean_datetime_str = korean_datetime_str or ""
        _ny_reference_str = ny_reference_str or ""

        if mode == "evening":
            system = SYSTEM_EVENING
            prompt = _build_evening_prompt(
                _korean_date,
                _us_market_date,
                market_data,
                news_list,
                _korean_datetime_str,
                _ny_reference_str,
                fact_reference_block,
            )
        else:
            system = SYSTEM_MORNING
            prompt = _build_morning_prompt(
                _korean_date,
                _us_market_date,
                market_data,
                news_list,
                _korean_datetime_str,
                _ny_reference_str,
                fact_reference_block,
            )

        logger.info(f"Gemini API 호출 중 (모드: {mode})...")

        max_json_retries = 2
        last_error = None

        for json_attempt in range(1, max_json_retries + 2):

            try:
                raw = self._call_gemini(system, prompt)

                post = self._parse_json_response(raw)
                post = self._strip_image_tags(post)
                post = self._ensure_required_fields(post, mode)
                post = self._normalize_disclaimer(post)

                logger.info(f"생성된 글자 수: {len(post.get('content', ''))}자")
                logger.info(f"생성된 제목: {post.get('title', '')}")

                post = self._fact_check_and_correct(
                    post,
                    system,
                    prompt,
                    fact_lookup,
                    mode,
                )

                post = self._normalize_disclaimer(post)

                # 팩트체크/후처리 이후에도 본문이 비어 있거나 잘렸으면
                # 절대 발행하지 않음
                final_content = (post.get("content", "") or "").strip()

                ContentGenerator._validate_content_integrity(
                    final_content,
                    "최종 원고",
                )

                post["content"] = final_content

                # 최종 안전망: 이미 교체된 인물(예: 전임 연준 의장)의 이름이
                # 제목/본문 어디에 남아있든 현재 인물 이름으로 자동 교정합니다
                # (fact_reference.CURRENT_OFFICIALS 참고).
                import fact_reference

                post["title"], _title_official_fixes = (
                    fact_reference.scrub_outdated_officials(post.get("title", ""))
                )
                post["content"], _content_official_fixes = (
                    fact_reference.scrub_outdated_officials(post["content"])
                )

                for _fix in _title_official_fixes + _content_official_fixes:
                    logger.warning(fact_reference.format_official_fix_log(_fix))

                post = self._prepend_toc(post)

                return post

            except (json.JSONDecodeError, ValueError) as e:
                last_error = e

                logger.warning(
                    "Gemini 원고 검증 실패 "
                    f"(시도 {json_attempt}/{max_json_retries + 1}): {e}"
                )

                if json_attempt <= max_json_retries:
                    logger.info(
                        "본문이 정상적으로 복구되지 않아 Gemini를 재호출합니다..."
                    )

        raise RuntimeError(
            "Gemini에서 유효한 블로그 본문을 생성하지 못했습니다. "
            f"{max_json_retries + 1}회 모두 실패했습니다: {last_error}"
        )

    def _fact_check_and_correct(
        self,
        post: dict,
        system: str,
        original_prompt: str,
        fact_lookup: dict | None,
        mode: str = "morning",
    ) -> dict:

        if not fact_lookup:
            return post

        try:
            import fact_checker

            violations = fact_checker.check_facts(post, fact_lookup)

        except Exception as e:
            logger.warning(f"팩트체크 검사 중 오류 (원본 유지): {e}")
            return post

        if not violations:
            logger.info("팩트체크: 실적/지표 발표일 관련 사실 오류 없음")
            return post

        logger.warning(f"팩트체크: {len(violations)}건의 사실 오류 후보 발견")

        for v in violations:
            logger.warning(
                f"  - [{v['type']}/{v['source']}] "
                f"{v['entity']}: "
                f"발견='{v.get('found_date')}' "
                f"기대='{v.get('expected_date')}' "
                f"(자동교정={'가능' if v.get('auto_fixable') else '불가'})"
            )

        post, applied, remaining = fact_checker.auto_correct_facts(post, violations)

        if applied:
            logger.warning(f"팩트체크: {len(applied)}건 자동 교정 완료")

        if not remaining:
            return self._normalize_disclaimer(post)

        logger.warning(f"팩트체크: {len(remaining)}건 Gemini 재생성 요청")

        correction_note = fact_checker.build_correction_prompt_note(remaining)
        corrected_prompt = original_prompt + "\n\n" + correction_note

        try:
            raw2 = self._call_gemini(system, corrected_prompt)

            post2 = self._parse_json_response(raw2)
            post2 = self._strip_image_tags(post2)
            post2 = self._ensure_required_fields(post2, mode)

            post2_content = (post2.get("content", "") or "").strip()

            if not post2_content:
                raise ValueError("팩트체크 재생성본의 content가 비어 있습니다.")

            if len(re.sub(r"<[^>]+>", "", post2_content).strip()) < 300:
                raise ValueError("팩트체크 재생성본의 본문이 비정상적으로 짧습니다.")

            post2 = self._normalize_disclaimer(post2)

        except Exception as e:
            logger.warning(f"팩트체크 재생성 실패 (원본 유지): {e}")

            post = fact_checker.neutralize_unresolved(post, remaining)
            return self._normalize_disclaimer(post)

        violations2 = fact_checker.check_facts(post2, fact_lookup)

        if not violations2:
            logger.info("팩트체크: 재생성 후 모든 사실 오류 해결")
            return self._normalize_disclaimer(post2)

        post2, applied2, remaining2 = fact_checker.auto_correct_facts(post2, violations2)

        if applied2:
            logger.warning(f"팩트체크: 재생성본에서 {len(applied2)}건 추가 교정")

        if remaining2:
            logger.error(
                f"팩트체크: 재생성 후에도 {len(remaining2)}건 미해결 → 안전 대체"
            )
            post2 = fact_checker.neutralize_unresolved(post2, remaining2)

        return self._normalize_disclaimer(post2)

    @staticmethod
    def _normalize_disclaimer(post: dict) -> dict:
        """
        면책조항의 티스토리 HTML 형식을 최종 단계에서 강제합니다.

        <blockquote><p>...</p></blockquote> 로 반환돼도
        <blockquote data-ke-style="style3"> 형태로 보정하며,
        면책조항이 없으면 마지막에 고정 면책조항을 추가합니다.
        """
        post = dict(post)

        content = post.get("content", "") or ""

        if not content:
            post["content"] = DISCLAIMER_HTML
            return post

        # 기존 blockquote의 모든 속성을 제거하고 티스토리 style3으로 통일
        content = re.sub(
            r"<blockquote\b[^>]*>",
            '<blockquote data-ke-style="style3">',
            content,
            flags=re.IGNORECASE,
        )

        if "<blockquote" in content.lower():
            # 마지막 </blockquote> 이후의 불필요한 텍스트를 제거해
            # 면책조항이 마지막 HTML 요소가 되도록 합니다.
            matches = list(
                re.finditer(r"</blockquote>", content, flags=re.IGNORECASE)
            )
            if matches:
                content = content[:matches[-1].end()].strip()
        else:
            # 면책조항이 아예 누락된 경우 고정 면책조항 추가
            content = content.rstrip() + "\n" + DISCLAIMER_HTML

        # 빈 blockquote가 생성됐으면 고정 면책조항으로 교체
        content = re.sub(
            r'<blockquote data-ke-style="style3">\s*</blockquote>',
            DISCLAIMER_HTML,
            content,
            flags=re.IGNORECASE,
        )

        post["content"] = content
        return post

    @staticmethod
    def _prepend_toc(post: dict) -> dict:
        """본문 최상단에 티스토리 목차를 삽입합니다."""
        post = dict(post)

        content = post.get("content", "") or ""

        if 'id="toc"' in content:
            return post

        post["content"] = f"{TOC_HTML_SNIPPET}\n{content}"
        return post

    @staticmethod
    def _ensure_required_fields(post: dict, mode: str) -> dict:
        """
        Gemini 응답의 필수 필드를 검증합니다.
        content가 비어 있거나 너무 짧으면 정상 결과로 취급하지 않습니다.
        """
        post = dict(post)

        if not post.get("title"):
            logger.warning("Gemini 응답에 title 필드 누락 — 기본값으로 대체")
            post["title"] = "미국 증시 브리핑"

        content = post.get("content")
        if not isinstance(content, str):
            content = ""
        content = content.strip()

        if not content:
            raise ValueError(
                "Gemini 응답의 content가 비어 있습니다. "
                "빈 본문을 발행하지 않기 위해 생성을 실패 처리합니다."
            )

        if len(re.sub(r"<[^>]+>", "", content).strip()) < 300:
            raise ValueError(
                "Gemini 응답의 content가 비정상적으로 짧습니다. "
                f"(HTML 포함 {len(content)}자)"
            )

        post["content"] = content

        if not isinstance(post.get("tags"), list) or not post.get("tags"):
            logger.warning("Gemini 응답에 tags 필드 누락 — 기본 태그로 대체")
            post["tags"] = ["미국증시", "주식", "나스닥", "S&P500", "증시분석"]

        if not post.get("image_prompt"):
            logger.warning("Gemini 응답에 image_prompt 필드 누락 — 기본 프롬프트로 대체")
            post["image_prompt"] = FALLBACK_IMAGE_PROMPT.get(
                mode,
                FALLBACK_IMAGE_PROMPT["morning"],
            )

        return post

    @staticmethod
    def _strip_image_tags(post: dict) -> dict:
        """본문에 삽입된 이미지 태그를 제거합니다."""
        content = post.get("content", "") or ""

        if not content:
            return post

        before_len = len(content)

        # 티스토리 이미지 태그
        content = re.sub(r"\[##_Image\|[^\]]*_##\]", "", content)
        # Markdown 이미지
        content = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", content)
        # HTML img
        content = re.sub(r"<img\b[^>]*>", "", content, flags=re.IGNORECASE)
        # 빈 줄 정리
        content = re.sub(r"\n{3,}", "\n\n", content)
        content = content.strip()

        if len(content) != before_len:
            logger.warning(
                f"본문에서 이미지 태그 제거함 ({before_len - len(content)}자 감소)"
            )

        post = dict(post)
        post["content"] = content
        return post

    @staticmethod
    def _parse_json_response(raw: str) -> dict:
        """
        Gemini 응답을 안전하게 JSON으로 복원합니다.

        처리 순서:
        1. Markdown 코드블록 제거
        2. 일반 json.loads
        3. JSON 객체 범위 추출
        4. JSONDecoder.raw_decode
        5. 필드별 안전 추출

        content 내부의 큰따옴표를 JSON 종료 문자로 오인하지 않으며,
        본문이 중간에서 잘린 경우 정상 응답으로 처리하지 않습니다.
        """

        if not raw:
            raise json.JSONDecodeError("빈 Gemini 응답", "", 0)

        candidate = raw.strip()

        # 1. Markdown 코드블록 제거
        blocks = re.findall(
            r"```(?:json)?\s*(.*?)```",
            candidate,
            flags=re.DOTALL | re.IGNORECASE,
        )

        if blocks:
            for block in blocks:
                try:
                    result = json.loads(block.strip())
                    if isinstance(result, dict):
                        return result
                except json.JSONDecodeError:
                    pass

            candidate = blocks[0].strip()

        candidate = re.sub(r"^```(?:json)?\s*", "", candidate, flags=re.IGNORECASE)
        candidate = re.sub(r"\s*```$", "", candidate, flags=re.IGNORECASE).strip()

        # 2. 정상 JSON
        try:
            result = json.loads(candidate)
            if isinstance(result, dict):
                return result
        except json.JSONDecodeError:
            pass

        # 3. JSON 객체 범위 추출
        start = candidate.find("{")
        end = candidate.rfind("}")

        if start >= 0 and end > start:
            json_candidate = candidate[start:end + 1]

            try:
                result = json.loads(json_candidate)
                if isinstance(result, dict):
                    return result
            except json.JSONDecodeError:
                pass

            # 4. raw_decode
            try:
                result, _ = json.JSONDecoder().raw_decode(json_candidate)
                if isinstance(result, dict):
                    return result
            except (json.JSONDecodeError, ValueError):
                pass

        logger.warning("JSON 파싱 전략 1~4 실패 — 필드별 안전 복구를 시작합니다.")

        # 5. 필드 위치 찾기
        def find_field_start(text: str, field: str) -> int:
            match = re.compile(
                rf'"{re.escape(field)}"\s*:\s*"',
                flags=re.DOTALL,
            ).search(text)
            return match.end() if match else -1

        # 일반 문자열 필드 추출 (title / image_prompt 등)
        def extract_simple_string(text: str, field: str) -> str:
            start_pos = find_field_start(text, field)

            if start_pos < 0:
                return ""

            chars = []
            i = start_pos
            length = len(text)

            while i < length:
                ch = text[i]

                if ch == "\\" and i + 1 < length:
                    next_ch = text[i + 1]

                    if next_ch in ('"', "\\", "/", "b", "f", "n", "r", "t"):
                        chars.append(ch)
                        chars.append(next_ch)
                        i += 2
                        continue

                    if next_ch == "u" and i + 5 < length:
                        hex_part = text[i + 2:i + 6]
                        if re.fullmatch(r"[0-9a-fA-F]{4}", hex_part):
                            chars.append(text[i:i + 6])
                            i += 6
                            continue

                if ch == '"':
                    value = "".join(chars)
                    try:
                        return json.loads('"' + value + '"').strip()
                    except Exception:
                        return value.strip()

                chars.append(ch)
                i += 1

            return "".join(chars).strip()

        # content 전용 복구 — 다음 JSON 필드(tags/image_prompt)를 기준으로
        # content의 실제 끝을 판단합니다.
        def extract_content_field(text: str) -> str:
            start_pos = find_field_start(text, "content")

            if start_pos < 0:
                return ""

            chars = []
            i = start_pos
            length = len(text)

            while i < length:
                ch = text[i]

                # Escape sequence
                if ch == "\\" and i + 1 < length:
                    next_ch = text[i + 1]

                    if next_ch in ('"', "\\", "/", "b", "f", "n", "r", "t"):
                        chars.append(ch)
                        chars.append(next_ch)
                        i += 2
                        continue

                    if next_ch == "u" and i + 5 < length:
                        hex_part = text[i + 2:i + 6]
                        if re.fullmatch(r"[0-9a-fA-F]{4}", hex_part):
                            chars.append(text[i:i + 6])
                            i += 6
                            continue

                    # 알 수 없는 escape는 백슬래시를 보존
                    chars.append("\\")
                    i += 1
                    continue

                # 큰따옴표 처리
                if ch == '"':
                    rest = text[i + 1:]

                    boundary_match = re.match(
                        r'\s*,\s*"(?:tags|image_prompt)"\s*:',
                        rest,
                        flags=re.IGNORECASE,
                    )

                    if boundary_match:
                        raw_value = "".join(chars)

                        try:
                            return json.loads('"' + raw_value + '"').strip()
                        except Exception:
                            # content 내부 escape가 일부 깨진 경우 안전 복구
                            return (
                                raw_value
                                .replace(r"\\", "\\")
                                .replace(r"\"", '"')
                                .replace(r"\n", "\n")
                                .replace(r"\r", "\r")
                                .replace(r"\t", "\t")
                                .strip()
                            )

                    # boundary가 아니면 본문 내부의 큰따옴표로 간주
                    chars.append('"')
                    i += 1
                    continue

                # 실제 줄바꿈
                if ch == "\r":
                    if i + 1 < length and text[i + 1] == "\n":
                        chars.append("\n")
                        i += 2
                        continue
                    chars.append("\n")
                    i += 1
                    continue

                if ch == "\n":
                    chars.append("\n")
                    i += 1
                    continue

                chars.append(ch)
                i += 1

            # JSON이 끝까지 닫히지 않은 경우 — 정상 성공으로 취급하면 안 됨
            value = "".join(chars).strip()

            if value:
                raise json.JSONDecodeError(
                    "content 문자열이 정상적으로 종료되지 않았습니다.",
                    raw,
                    0,
                )

            return ""

        # tags 추출
        def extract_tags(text: str) -> list:
            match = re.search(r'"tags"\s*:\s*\[(.*?)\]', text, flags=re.DOTALL)

            if not match:
                return []

            tags = []

            for match_tag in re.finditer(
                r'"((?:\\.|[^"\\])*)"',
                match.group(1),
                flags=re.DOTALL,
            ):
                value = match_tag.group(1)

                try:
                    value = json.loads('"' + value + '"')
                except Exception:
                    value = value.replace(r"\"", '"').replace(r"\\", "\\")

                if value:
                    tags.append(value)

            return tags

        # 필드 복구
        title = extract_simple_string(candidate, "title")
        content = extract_content_field(candidate)
        image_prompt = extract_simple_string(candidate, "image_prompt")
        tags = extract_tags(candidate)

        # content 필수 검증
        if not content:
            logger.error("Gemini 응답에서 content를 복구하지 못했습니다.")
            raise json.JSONDecodeError("Gemini content 필드 추출 실패", raw, 0)

        # HTML 본문 최소 검증
        text_only = re.sub(r"<[^>]+>", "", content).strip()

        if len(text_only) < 300:
            logger.error(
                f"복구된 Gemini content가 비정상적으로 짧습니다: {len(text_only)}자"
            )
            raise json.JSONDecodeError(
                "Gemini content가 비정상적으로 짧습니다.", raw, 0
            )

        # 본문이 미완성 문장으로 끝나는지 검사
        def looks_truncated(html_content: str) -> bool:
            plain = re.sub(r"<[^>]+>", "", html_content)
            plain = re.sub(r"\s+", " ", plain).strip()

            if not plain:
                return True

            if plain.count("<") > plain.count(">"):
                return True

            truncated_endings = (
                "은", "는", "이", "가", "을", "를", "에", "의", "로", "으로",
                "와", "과", "하고", "하며", "때문에", "따라서", "하지만",
                "그리고", "즉", "또한", "다만", "특히", "반면",
            )

            last_word = plain.split()[-1]

            if len(last_word) <= 8 and last_word.endswith(truncated_endings):
                return True

            if len(plain) > 500:
                if not re.search(r"[.!?。！？다요죠됨임음함함니다요]$", plain):
                    return True

            return False

        if looks_truncated(content):
            logger.error("Gemini content가 미완성 문장으로 종료된 것으로 판단됩니다.")
            raise json.JSONDecodeError(
                "Gemini content가 중간에서 잘린 것으로 판단됩니다.", raw, 0
            )

        logger.warning(
            "필드별 JSON 복구 성공 "
            f"(title={title[:30]}..., content={len(content)}자)"
        )

        return {
            "title": title,
            "content": content,
            "tags": tags,
            "image_prompt": image_prompt,
        }

    @staticmethod
    def _validate_content_integrity(content: str, stage: str = "본문") -> None:
        """본문이 비어 있거나 중간에서 잘린 것으로 보이면 ValueError를 발생시킵니다."""

        if not isinstance(content, str):
            raise ValueError(f"{stage}이 문자열이 아닙니다.")

        content = content.strip()

        if not content:
            raise ValueError(f"{stage}이 비어 있습니다.")

        plain = re.sub(r"<[^>]+>", "", content)
        plain = re.sub(r"\s+", " ", plain).strip()

        if len(plain) < 300:
            raise ValueError(f"{stage}이 비정상적으로 짧습니다. ({len(plain)}자)")

        if plain.count("<") > plain.count(">"):
            raise ValueError(f"{stage}의 HTML 구조가 완전히 닫히지 않았습니다.")

        truncated_endings = (
            "은", "는", "이", "가", "을", "를", "에", "의", "로", "으로",
            "와", "과", "하고", "하며", "때문에", "따라서", "하지만",
            "그리고", "즉", "또한", "다만", "특히", "반면",
        )

        last_word = plain.split()[-1]

        if len(last_word) <= 8 and last_word.endswith(truncated_endings):
            raise ValueError(
                f"{stage}이 문장 중간에서 종료된 것으로 판단됩니다. "
                f"마지막 표현: '{last_word}'"
            )