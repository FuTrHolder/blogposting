"""
숏폼 & 틱톡 영상 생성기 v11
==============================================
변경사항 (v11 — Gemini/TTS 안정화):
  - [Gemini] 나레이션 생성을 repo 루트의 gemini_client.py로 통일
      · API 키를 URL이 아닌 헤더로 전송 (로그 노출 방지)
      · 429/5xx/MAX_TOKENS/JSON 파싱 실패 시 모델 순차 폴백
      · 모델 목록은 환경변수 GEMINI_NARRATION_MODELS로 교체 가능
  - [TTS] edge-tts 호출 횟수 축소 및 안정화
      · 속도 맞춤: 기존 최대 5단계×음성 3개(15회) → 최대 2회
      · 호출 간 간격(0.6초) + 백오프 재시도 + 오류 유형 로그(403 안내)
      · 연속 실패 시 edge-tts를 건너뛰고 무료 gTTS(Google 번역 TTS)로 폴백
  - [정리] 호출되지 않던 _repeat_video_twice / picsum 함수 제거
      (틱톡 영상은 반복 재생으로 길이를 채우지 않습니다)

v10 유지: picsum 제거, Pexels 키워드 전체 순차 시도
v9 유지: 블로그 썸네일을 영상 배경으로 쓰지 않음(제목 텍스트 겹침 방지)
v8 유지: generate_tiktok() — 틱톡 수익화 조건(1분 이상), 60초 미달 시
         CTA 1회 → 확장 나레이션 재생성 → 그래도 미달이면 생성 실패 처리

TTS: edge-tts ko-KR-InJoonNeural(쇼츠) / ko-KR-SunHiNeural(틱톡)
규격: 1080×1920, 30fps, H.264
"""

import asyncio
import base64
import json
import logging
import os
import re
import subprocess
import tempfile
import time
from pathlib import Path

import requests
from PIL import Image, ImageDraw, ImageFilter, ImageFont

# repo 루트의 fact_reference.py / gemini_client.py를 가져옵니다.
# marketing/main_marketing.py가 이 모듈을 임포트하기 전에 sys.path에 repo
# 루트를 이미 추가해두므로(main_marketing.py 상단 참고) 정상 동작합니다.
import fact_reference
from gemini_client import call_gemini_json, models_from_env

logger = logging.getLogger(__name__)

# ── 규격 ─────────────────────────────────────────────────────────────────────
VIDEO_W, VIDEO_H = 1080, 1920
OUTPUT_DIR       = "videos"
MAX_VIDEO_SEC    = 58.0   # 59초 이하 보장

# ── TTS 속도 단계 (쇼츠/릴스용 — 기본 → 최고속) ─────────────────────────────
# 첫 번째 값이 기본 속도, 마지막 값이 속도 상한입니다.
TTS_RATE_STEPS = ["+34%", "+45%", "+56%", "+66%", "+75%"]
TTS_VOICE      = "ko-KR-InJoonNeural"   # 남성 — 쇼츠/릴스용
TTS_PITCH      = "-2Hz"
# 슬라이드 끝에 여유 시간 (TTS 끝나고 다음 슬라이드로 넘어가기 전 숨 쉬는 시간)
SLIDE_TAIL_SEC = 0.4

# ── TTS 설정 (틱톡 전용 — 젊고 밝은 여성 목소리, 빠른 속도) ─────────────────
TTS_VOICE_TIKTOK = "ko-KR-SunHiNeural"
TTS_RATE_TIKTOK  = "+38%"
TTS_PITCH_TIKTOK = "+8Hz"
# 기본 음성이 실패했을 때 순서대로 시도할 대체 음성 (모두 edge-tts 서비스 의존)
TTS_VOICE_FALLBACKS = [
    "ko-KR-SunHiNeural",
    "ko-KR-InJoonNeural",
    "ko-KR-HyunsuMultilingualNeural",
]
# TTS 세그먼트 간 이어붙임 시 자연스러운 흐름을 위한 최소 간격 (끊김 완화)
TTS_GAP_SEC_TIKTOK = 0.08

# ── 시스템 폰트 경로 ─────────────────────────────────────────────────────────
_FONT_BOLD    = "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc"
_FONT_BLACK   = "/usr/share/fonts/opentype/noto/NotoSansCJK-Black.ttc"
_FONT_REGULAR = "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"

# ── 배경 오버레이 (투명도 절반) ───────────────────────────────────────────────
THEMES = {
    "morning": {
        "overlay":    (8, 15, 35, 70),
        "accent":     (56, 189, 248),
        "highlight":  (254, 211, 48),
        "keyword_bg": (56, 189, 248),
        "keyword_fg": (8, 15, 35),
        "progress":   (56, 189, 248),
    },
    "evening": {
        "overlay":    (18, 5, 40, 75),
        "accent":     (167, 139, 250),
        "highlight":  (251, 191, 36),
        "keyword_bg": (167, 139, 250),
        "keyword_fg": (18, 5, 40),
        "progress":   (167, 139, 250),
    },
}

# ── Pexels 키워드 ────────────────────────────────────────────────────────────
PEXELS_KEYWORDS = {
    "morning": ["wall street morning", "stock market finance", "financial district dawn"],
    "evening": ["city night finance", "new york night skyline", "stock exchange night"],
}

# 틱톡 전용 키워드 — 쇼츠/릴스보다 더 역동적이고 화제성 있는 톤으로 차별화
PEXELS_KEYWORDS_TIKTOK = {
    "morning": ["stock trading screens closeup", "financial data dashboard dynamic", "trading floor energy"],
    "evening": ["stock market chart neon", "trading screens night dynamic", "financial data glow"],
}

# ── Cloudflare Workers AI (영상 배경 전용 — 완전 무료, 텍스트 없는 배경 생성) ──
CF_FLUX_MODEL = "@cf/black-forest-labs/flux-1-schnell"
CF_API_BASE = "https://api.cloudflare.com/client/v4/accounts"

FLUX_SUFFIX_VIDEO = {
    "morning": (
        ", Wall Street financial district at dawn, calm after market close, "
        "professional financial photography, cinematic lighting, 8K resolution, "
        "no text, no watermark, no logo, no people"
    ),
    "evening": (
        ", pre-market trading floor at night, dynamic stock exchange screens, "
        "professional financial photography, dramatic lighting, 8K resolution, "
        "no text, no watermark, no logo, no people"
    ),
}

# ── 이모지 제거 (한글 보존) ──────────────────────────────────────────────────
_EMOJI_RE = re.compile(
    "["
    "\U0001F600-\U0001F64F"
    "\U0001F300-\U0001F5FF"
    "\U0001F680-\U0001F6FF"
    "\U0001F1E0-\U0001F1FF"
    "\U0001F900-\U0001F9FF"
    "\U0001FA00-\U0001FA6F"
    "\U0001FA70-\U0001FAFF"
    "\U00002702-\U00002705"
    "\U00002708-\U0000270D"
    "\U0000270F\U00002712\U00002714\U00002716\U0000271D"
    "\U00002721\U00002728\U00002733-\U00002734\U00002744"
    "\U00002747\U0000274C\U0000274E\U00002753-\U00002755"
    "\U00002757\U00002763-\U00002764\U00002795-\U00002797"
    "\U000027A1\U000027B0\U000027BF"
    "]+",
    flags=re.UNICODE,
)


def _strip_emoji(text: str) -> str:
    return _EMOJI_RE.sub("", text).strip()


# ═══════════════════════════════════════════════════════════════════════════════
# Gemini API: 나래이션 스크립트 생성 (gemini_client 사용)
# ═══════════════════════════════════════════════════════════════════════════════

# 나레이션용 모델 순서 (가벼운 모델 우선 → 상위 모델 폴백)
# 환경변수 GEMINI_NARRATION_MODELS="모델1,모델2"로 코드 수정 없이 교체 가능
NARRATION_MODELS = [
    "gemini-3.5-flash-lite",
    "gemini-3.6-flash",
    "gemini-2.5-flash",
]

NARRATION_SYSTEM_INTRO = """당신은 유튜브 쇼츠 나래이션 작가입니다.
블로그 본문을 읽고, 빠른 속도로 읽어도 55초 이하가 되는 나래이션 스크립트를 작성합니다.
한국어 발화 속도를 기준으로 세그먼트당 나래이션을 적절히 조절하세요.
"""

# fact_reference.py의 현재 인물 정보를 그대로 주입합니다. 나레이션은 블로그
# 본문을 참고해 새로 문장을 만들기 때문에, 블로그에 없는 인물 언급을
# 나레이션이 자체적으로 추가하면서 낡은 이름을 쓸 수 있어 동일하게 안내합니다.
NARRATION_OFFICIALS_NOTE = (
    "\n" + fact_reference.officials_reference_text()
    + "\n위 인물 정보는 반드시 지켜야 하는 사실입니다. 학습 시점의 기억에 "
    "의존해 이전 인물 이름을 쓰지 마세요.\n"
)

NARRATION_SYSTEM = (
    NARRATION_SYSTEM_INTRO
    + NARRATION_OFFICIALS_NOTE
    + """
규칙:
- 나래이션은 자연스러운 구어체로 작성 (문어체 금지)
- 각 세그먼트는 핵심 내용 하나만 전달
- 세그먼트당 나래이션은 8~11초 분량 (약 40~65 음절) — 절대 초과 금지
- 총 5~6개 세그먼트 (55초 / 세그먼트당 평균 10초 = 최대 5.5개)
- 각 세그먼트에 키워드(3~6자)와 부연설명(15~25자) 포함
- 첫 번째 세그먼트: 강력한 훅 (시청자 주의 끌기, 인사말 없이 핵심 수치/반전으로 시작)
- 마지막 세그먼트: 블로그 방문 유도 CTA

[자연스러운 어조 — AI 티 나지 않게 쓰는 법]
TTS로 읽었을 때 사람이 직접 브리핑하는 것처럼 들리도록 아래를 지키세요.
- "~했습니다", "~입니다"만 기계적으로 반복하지 말고, "~했어요", "~네요",
  "~거든요", "~더라고요" 같은 어미도 자연스럽게 섞어 리듬을 만드세요
  (단, 과도한 반말이나 지나친 캐주얼함은 피하고 정보 전달에는 신뢰감을 유지)
- 문장 사이에 짧은 접속어("근데", "그리고", "그래서", "특히")를 자연스럽게
  넣어 딱딱하게 끊기지 않고 이어지는 느낌을 주세요
- 숫자를 나열할 때도 "먼저", "다음으로"처럼 기계적인 순서 표현 대신,
  실제 사람이 설명하듯 자연스러운 흐름으로 연결하세요
- 모든 문장을 비슷한 길이·구조로 반복하지 마세요. 짧은 문장과 조금 더
  긴 문장을 섞어 리듬에 변화를 주세요 (AI 생성 티가 나는 가장 큰 원인은
  일정한 문장 길이·패턴의 반복입니다)
- "~라고 볼 수 있습니다", "~라는 분석입니다" 같은 과도하게 격식 있고
  건조한 리포트체 표현은 피하고, 실제 유튜버가 말하듯 표현하세요
- 부자연스러운 한자어·전문 용어를 나열하기보다 쉽게 풀어서 설명하세요

반드시 아래 JSON 형식으로만 응답하세요 (마크다운 코드블록 없이):
{
  "segments": [
    {
      "narration": "실제 읽을 나래이션 텍스트 (구어체, 40~65음절, 자연스러운 어조)",
      "keyword": "핵심 키워드 (3~6자)",
      "description": "키워드 부연설명 (15~25자)"
    }
  ]
}"""
)


# ── 틱톡 전용 나래이션 시스템 프롬프트 (이탈률 개선 — 짧고 강한 훅 중심) ────
NARRATION_SYSTEM_TIKTOK = (
    """당신은 틱톡 금융 시황 채널의 바이럴 콘텐츠 작가입니다.
틱톡 알고리즘은 "첫 1~2초 이탈 여부"로 확산을 결정합니다. 시청 데이터를 보면
영상 길이가 길고 도입부가 약할수록 시청자가 2~3초 만에 스와이프하고 나갑니다.
이 문제를 해결하기 위해, 아래 규칙을 반드시 지키는 짧고 강렬한 스크립트를 작성합니다.
"""
    + NARRATION_OFFICIALS_NOTE
    + """
핵심 원칙:
- 전체 나래이션 길이(자연스러운 속도로 읽었을 때) = 65초~80초
  (실제 음성 합성은 이보다 빠른 속도로 재생되므로, 목표를 넉넉히 잡아야
  최종 영상이 60초 미만이 되는 것을 방지할 수 있습니다)
- 총 6~7개 세그먼트 (기존 8~12개에서 축소하되, 60초 확보를 위해 최소 6개는 유지)
- 세그먼트당 나래이션은 10~15초 분량 (약 55~85 음절)
- 나래이션은 자연스러운 구어체, 밝고 빠른 텐션 (문어체·이모지 금지)
- 블로그의 핵심 내용 중 "가장 임팩트 있는 것"만 우선순위 높게 선별 (전체 요약 금지 — 다 담으려 하지 말 것)

[1번 세그먼트 — 반드시 아래 규칙을 지킬 것]
- 절대 "안녕하세요", "오늘의 시황입니다" 같은 인사말·필러로 시작 금지
- 첫 문장 자체가 훅이어야 함: 가장 충격적인 수치, 반전, 또는 궁금증을 유발하는 질문으로 시작
- 예시 패턴: "나스닥이 오늘 밤 이렇게 될 줄 몰랐습니다", "S&P500 0.4% 상승, 근데 이 종목은 달랐어요",
  "연준 발언 하나로 시장이 뒤집혔습니다"
- 화면에 표시될 keyword는 이 훅의 핵심 단어(숫자/종목명 등)로 강렬하게 설정

[중간 세그먼트]
- 지수 동향 → 가장 화제성 있는 핵심 뉴스 1~2개 → 오늘 주목 포인트 순으로 압축 전개
- 정보를 나열하지 말고, 각 세그먼트가 "다음이 궁금해지는" 흐름으로 이어지게 할 것

[마지막 세그먼트]
- 오늘의 핵심을 한 문장으로 강렬하게 정리 + 팔로우/저장 유도
- 재방문 유도 문구에는 실제 발행 시각을 반영하세요: morning 모드는 "매일 아침
  9시", evening 모드는 "매일 저녁 9시"(또는 "밤 9시")를 사용하고, "매일 새벽"
  같은 실제와 다른 시간 표현은 쓰지 마세요
- 아래처럼 표현을 매번 다르게 바꿔가며 사용하세요 (매 세그먼트 동일 문구 반복 금지)
  · "더 궁금하면 팔로우 하세요, 매일 아침 9시에 찾아옵니다"
  · "매일 저녁 9시, 이 계정만 팔로우하면 놓치지 않아요"
  · "저장해두고 매일 아침 9시에 다시 확인하세요"
  · "팔로우하면 매일 저녁 9시 시황을 가장 먼저 받아보실 수 있어요"

[해시태그]
- 검색 유입을 높일 수 있는 한국어 해시태그 4~6개를 hashtags 배열에 제공
- 니치 태그(예: 미국주식, 나스닥, 주식초보) + 트렌드 태그(예: 재테크, 경제뉴스) 조합
- #는 붙이지 말고 단어만 (예: "미국주식", "나스닥")

반드시 아래 JSON 형식으로만 응답하세요 (마크다운 코드블록 없이):
{
  "segments": [
    {
      "narration": "실제 읽을 나래이션 텍스트 (구어체, 45~75음절, 밝고 빠른 톤)",
      "keyword": "핵심 키워드 (3~8자, 훅의 핵심 단어)",
      "description": "키워드 부연설명 (15~30자)"
    }
  ],
  "hashtags": ["미국주식", "나스닥", "재테크", "주식초보", "경제뉴스"]
}"""
)


def _scrub_segments_officials(segments: list[dict]) -> list[dict]:
    """
    생성된 나레이션 세그먼트에서 이미 교체된 인물의 이름이 남아있으면
    현재 인물 이름으로 자동 교정합니다 (narration/keyword/description 필드 전부).
    """
    fixes: list[dict] = []
    for seg in segments:
        for field in ("narration", "keyword", "description"):
            if seg.get(field):
                fixed, applied = fact_reference.scrub_outdated_officials(seg[field])
                seg[field] = fixed
                fixes.extend(applied)
    for fix in fixes:
        logger.warning(fact_reference.format_official_fix_log(fix))
    return segments


def _validate_segments(obj: dict) -> None:
    segs = obj.get("segments")
    if not isinstance(segs, list) or not segs:
        raise ValueError("segments 배열이 비어 있음")
    for s in segs:
        if not isinstance(s, dict) or not str(s.get("narration", "")).strip():
            raise ValueError("narration이 비어 있는 세그먼트가 있음")


def generate_narration_script(
    blog_content: str, title: str, mode: str, api_key: str
) -> list[dict]:
    """블로그 본문 기반 나래이션 스크립트 생성."""
    if not api_key:
        logger.warning("GEMINI_API_KEY 없음 — 기본 스크립트 사용")
        return _fallback_script(title, mode)

    mode_label = "전일 마감 리뷰" if mode == "morning" else "프리마켓 & 이슈"
    prompt = (
        f"블로그 제목: {title}\n"
        f"포스팅 모드: {mode_label}\n\n"
        f"블로그 본문:\n{blog_content[:3000]}\n\n"
        "위 내용을 바탕으로 유튜브 쇼츠용 나래이션 스크립트를 JSON으로 작성해주세요.\n"
        "각 세그먼트 나래이션은 반드시 65음절 이하, 전체 합산 55초 이하가 되어야 합니다."
    )

    try:
        parsed = call_gemini_json(
            api_key,
            NARRATION_SYSTEM,
            prompt,
            models_from_env("GEMINI_NARRATION_MODELS", NARRATION_MODELS),
            temperature=0.7,
            max_output_tokens=8192,
            timeout=60,
            validate=_validate_segments,
        )
        segs = _scrub_segments_officials(parsed["segments"])
        logger.info(f"나래이션 스크립트 생성 완료: {len(segs)}개")
        return segs
    except Exception as e:
        logger.warning(f"나래이션 생성 전체 실패 — 기본 스크립트 사용: {e}")
        return _fallback_script(title, mode)


def generate_narration_script_tiktok(
    blog_content: str,
    title: str,
    mode: str,
    api_key: str,
    extended: bool = False,
) -> tuple[list[dict], list[str]]:
    """
    틱톡용 나래이션 스크립트 생성 (이탈률 개선 버전).

    Returns:
        (segments, hashtags) 튜플. 실패 시에도 fallback으로 항상 유효한 값 반환.
    """
    if not api_key:
        logger.warning("GEMINI_API_KEY 없음 — 틱톡 기본 스크립트 사용")
        return _fallback_script_tiktok(title, mode), _fallback_hashtags_tiktok(mode)

    mode_label = "전일 마감 리뷰" if mode == "morning" else "프리마켓 & 이슈"
    post_time_label = "매일 아침 9시" if mode == "morning" else "매일 저녁 9시"
    if extended:
        duration_instruction = (
            "이번 요청은 1차 영상이 60초에 미달하여 다시 생성하는 확장 나래이션입니다.\n"
            "기존 내용과 핵심 주제는 유지하되, 새로운 사실을 억지로 추가하지 말고 "
            "기존 핵심 내용을 더 자세하고 자연스럽게 설명하세요.\n"
            "세그먼트는 8~9개로 작성하고, 각 세그먼트는 65~90음절 정도로 충분히 설명하세요.\n"
            "전체 나래이션은 자연스러운 발화 기준 약 90~105초 분량을 목표로 하세요.\n"
            "같은 문장을 반복하거나 의미 없는 문장으로 분량만 늘리지 마세요.\n"
            "각 세그먼트는 서로 다른 정보나 관점을 전달해야 합니다."
        )
    else:
        duration_instruction = (
            "세그먼트는 6~7개, 전체 길이는 65초~80초여야 합니다 "
            "(실제 음성 재생은 이보다 빨라지므로 목표를 넉넉히 잡아야 "
            "최종 영상이 60초를 넘습니다)."
        )

    prompt = (
        f"블로그 제목: {title}\n"
        f"포스팅 모드: {mode_label} (실제 발행 시각: {post_time_label})\n\n"
        f"블로그 본문 전문:\n{blog_content[:5000]}\n\n"
        "위 내용을 바탕으로 틱톡용 나래이션 스크립트를 JSON으로 작성해주세요.\n"
        f"{duration_instruction}\n"
        "블로그의 핵심 내용 중 가장 임팩트 있는 것을 우선적으로 선별하세요.\n"
        "같은 내용을 반복해서 분량을 늘리지 마세요.\n"
        "1번 세그먼트는 반드시 강력한 훅으로 시작해야 합니다 (인사말 절대 금지)."
    )

    system_prompt = NARRATION_SYSTEM_TIKTOK
    if extended:
        system_prompt += """

[확장 나래이션 재생성 모드]
현재 영상이 60초에 미달하여 나래이션을 확장하는 단계입니다.

- 기존 스크립트의 핵심 주제와 사실관계를 유지하세요.
- 단순한 CTA나 좋아요/팔로우 문구를 추가하지 마세요.
- 같은 문장을 반복하지 마세요.
- 기존 내용을 더 구체적으로 설명하거나 배경, 원인, 영향, 전망을 자연스럽게 보강하세요.
- 총 8~9개 세그먼트를 작성하세요.
- 각 세그먼트는 충분한 설명을 포함하고, 전체적으로 자연스러운 발화 기준 90~105초를 목표로 하세요.
- 정보가 없는 부분을 추측해서 만들어내지 마세요.
"""

    try:
        parsed = call_gemini_json(
            api_key,
            system_prompt,
            prompt,
            models_from_env("GEMINI_NARRATION_MODELS", NARRATION_MODELS),
            temperature=0.75,
            max_output_tokens=8192,
            timeout=90,
            validate=_validate_segments,
        )
        segs = _scrub_segments_officials(parsed["segments"])

        raw_tags = parsed.get("hashtags", [])
        tags = (
            [str(t).lstrip("#").strip() for t in raw_tags if str(t).strip()]
            if isinstance(raw_tags, list)
            else []
        )
        if not tags:
            tags = _fallback_hashtags_tiktok(mode)

        logger.info(
            f"틱톡 나래이션 스크립트 생성 완료: {len(segs)}개 세그먼트, "
            f"해시태그 {len(tags)}개"
        )
        return segs, tags
    except Exception as e:
        logger.warning(f"틱톡 나래이션 생성 전체 실패 — 기본 스크립트 사용: {e}")
        return _fallback_script_tiktok(title, mode), _fallback_hashtags_tiktok(mode)


def _fallback_hashtags_tiktok(mode: str) -> list[str]:
    """API 실패 시 기본 해시태그 (검색 유입 목적 니치+트렌드 조합)."""
    base = ["미국주식", "나스닥", "재테크", "주식초보", "경제뉴스"]
    if mode == "evening":
        base.append("프리마켓")
    else:
        base.append("증시마감")
    return base


# ── 1분 미달 시 덧붙이는 참여 유도(좋아요/팔로우/공유) 마무리 세그먼트 ──────
def _engagement_cta_pool(mode: str) -> list[dict]:
    """실제 발행 시각(morning=매일 아침 9시, evening=매일 저녁 9시)에 맞춘
    참여 유도 문구 풀을 반환합니다."""
    time_label = "매일 아침 9시" if mode == "morning" else "매일 저녁 9시"
    return [
        {"narration": "이 영상이 도움이 되셨다면 좋아요 눌러주세요. 다음 소식도 놓치지 않게 팔로우까지 부탁드려요.",
         "keyword": "좋아요 부탁", "description": f"{time_label}에 업데이트되는 시황 브리핑"},
        {"narration": "주변에 투자 정보가 필요한 분이 있다면 이 영상 공유해주세요. 함께 보면 더 좋아요.",
         "keyword": "공유하기", "description": "투자 정보가 필요한 분께 공유"},
        {"narration": "다음 브리핑도 궁금하시다면 리포스트로 저장해두세요. 놓치지 않고 바로 확인하실 수 있어요.",
         "keyword": "리포스트", "description": "다음 브리핑 놓치지 않기"},
        {"narration": f"{time_label}, 이 계정만 팔로우하면 시황을 가장 먼저 받아보실 수 있습니다.",
         "keyword": "팔로우 필수", "description": f"{time_label}에 업데이트되는 시황 브리핑"},
    ]


def append_engagement_cta_segments(
    segments: list[dict], mode: str, count: int = 1
) -> list[dict]:
    """engagement CTA 세그먼트를 count개만큼 풀에서 순서대로 골라 뒤에 덧붙입니다."""
    if count <= 0:
        return segments

    pool = _engagement_cta_pool(mode)
    extended = list(segments)
    for i in range(count):
        extended.append(dict(pool[i % len(pool)]))
    return extended


def _fallback_script(title: str, mode: str) -> list[dict]:
    """API 실패 시 기본 스크립트."""
    clean_title = _strip_emoji(title)
    if mode == "morning":
        return [
            {"narration": f"방금 마감된 미국 증시 핵심만 빠르게 정리합니다. {clean_title[:20]}",
             "keyword": "마감 분석", "description": "미국 전일 증시 마감 결과"},
            {"narration": "주요 지수 흐름과 핵심 이슈를 빠르게 살펴보겠습니다.",
             "keyword": "지수 동향", "description": "S&P500, 나스닥, 다우 등락"},
            {"narration": "오늘 시장에 영향을 준 경제 지표와 뉴스입니다.",
             "keyword": "경제 지표", "description": "발표된 주요 경제 데이터"},
            {"narration": "더 자세한 분석은 블로그에서 확인하세요. 구독과 좋아요 부탁드립니다.",
             "keyword": "블로그 방문", "description": "seedsup.tistory.com"},
        ]
    else:
        return [
            {"narration": f"오늘 밤 미국 증시 개장 전 핵심 이슈입니다. {clean_title[:20]}",
             "keyword": "프리마켓", "description": "미국 장 개장 전 선물 동향"},
            {"narration": "오늘 예정된 경제 지표와 실적 발표를 확인해보겠습니다.",
             "keyword": "경제 지표", "description": "오늘 밤 주요 발표 일정"},
            {"narration": "프리마켓 동향과 오늘 밤 시장 시나리오를 분석합니다.",
             "keyword": "시장 전망", "description": "강세 vs 약세 시나리오"},
            {"narration": "전체 분석은 블로그를 방문해주세요. 구독과 좋아요 감사합니다.",
             "keyword": "블로그 방문", "description": "seedsup.tistory.com"},
        ]


def _fallback_script_tiktok(title: str, mode: str) -> list[dict]:
    """틱톡용 API 실패 시 기본 스크립트 (6개 세그먼트, 60~70초 분량, 훅 강화)."""
    clean_title = _strip_emoji(title)
    if mode == "morning":
        return [
            {"narration": f"방금 마감된 미국 증시, 오늘 밤 이야기가 좀 있습니다. {clean_title[:25]}",
             "keyword": "증시 마감", "description": "미국 전일 증시 마감 총정리"},
            {"narration": "S&P500, 나스닥, 다우존스 주요 지수부터 빠르게 확인하겠습니다.",
             "keyword": "지수 동향", "description": "S&P500·나스닥·다우 등락률"},
            {"narration": "오늘 시장을 움직인 핵심 뉴스, 연준 발언과 경제 지표가 심리에 영향을 줬습니다.",
             "keyword": "핵심 뉴스", "description": "시장을 움직인 결정적 이슈"},
            {"narration": "공포탐욕지수로 본 지금 투자자들의 심리, 과열인지 냉각인지 확인해보겠습니다.",
             "keyword": "투자 심리", "description": "공포·탐욕지수 및 시장 심리"},
            {"narration": "내일 시장에서 반드시 체크해야 할 포인트, 미리 정리해드립니다.",
             "keyword": "내일 체크", "description": "다음 거래일 핵심 이벤트"},
            {"narration": "오늘 핵심은 이것 하나입니다. 매일 아침 9시에 찾아오니 팔로우 해주세요.",
             "keyword": "팔로우", "description": "매일 아침 9시 시황 브리핑"},
        ]
    else:
        return [
            {"narration": f"오늘 밤 미국 증시 개장 전, 지금 이 이슈부터 보셔야 합니다. {clean_title[:20]}",
             "keyword": "프리마켓", "description": "미국 장 개장 전 핵심 이슈"},
            {"narration": "전일 정규장 마감 결과부터 빠르게 짚어보겠습니다.",
             "keyword": "전일 마감", "description": "정규장 마감 및 애프터마켓"},
            {"narration": "지금 선물 시장 동향, S&P500과 나스닥 선물이 어디로 가고 있을까요?",
             "keyword": "선물 동향", "description": "S&P500·나스닥 선물 방향"},
            {"narration": "오늘 밤 발표되는 경제 지표, 시장 예상치와 함께 확인하겠습니다.",
             "keyword": "지표 발표", "description": "오늘 밤 예정된 경제 데이터"},
            {"narration": "연준 인사 발언과 지정학적 리스크까지, 오늘 밤 변수들을 종합했습니다.",
             "keyword": "시장 변수", "description": "오늘 밤 주요 리스크 요인"},
            {"narration": "오늘 밤 강세일까 약세일까, 핵심만 정리했습니다. 매일 저녁 9시, 팔로우하면 가장 먼저 받아보실 수 있어요.",
             "keyword": "팔로우", "description": "매일 저녁 9시 시황 브리핑"},
        ]


# ═══════════════════════════════════════════════════════════════════════════════
# TTS (v11 — 호출 수 축소 + 백오프 + 연속 실패 시 gTTS 무료 폴백)
# ═══════════════════════════════════════════════════════════════════════════════

_TTS_PACING_SEC = 0.6        # 호출 간 최소 간격 (Microsoft 속도 제한 완화)
_EDGE_FAIL_LIMIT = 2         # 연속 완전 실패가 이 횟수에 도달하면 edge-tts 건너뜀
_EDGE_FAIL_STREAK = 0


def _rate_to_factor(rate: str) -> float:
    m = re.fullmatch(r"\s*([+-]\d+)%\s*", rate or "")
    return 1 + int(m.group(1)) / 100 if m else 1.0


async def _tts_async(text: str, path: str, rate: str, voice: str = TTS_VOICE, pitch: str = TTS_PITCH):
    import edge_tts
    comm = edge_tts.Communicate(text=text, voice=voice, rate=rate, pitch=pitch)
    await asyncio.wait_for(comm.save(path), timeout=45)


def _generate_tts_with_rate(
    text: str, path: str, rate: str,
    voice: str = TTS_VOICE, pitch: str = TTS_PITCH, retries: int = 2,
) -> bool:
    """edge-tts로 TTS 생성. 오류 유형을 구분해 로그에 남기고 백오프 재시도."""
    for attempt in range(1, retries + 1):
        try:
            asyncio.run(_tts_async(text, path, rate, voice, pitch))
            if Path(path).exists() and Path(path).stat().st_size > 500:
                time.sleep(_TTS_PACING_SEC)
                return True
            logger.warning(f"[TTS] 파일이 비었거나 너무 작음: voice={voice}")
        except ImportError:
            logger.error("[TTS] edge-tts 미설치 — pip install 'edge-tts>=7.2.7'")
            return False
        except Exception as e:
            msg = str(e)
            logger.warning(
                f"[TTS] edge-tts 실패 voice={voice} rate={rate} "
                f"({attempt}/{retries}): {type(e).__name__}: {msg[:200]}"
            )
            if "403" in msg:
                logger.warning(
                    "[TTS] 403: edge-tts 버전이 낮거나(7.2.7 이상 필요) "
                    "Microsoft 측 요청 제한일 수 있습니다."
                )
        if attempt < retries:
            time.sleep(2 * attempt)
    return False


def _generate_tts_gtts(text: str, path: str, rate: str) -> bool:
    """edge-tts 장애 시 무료 폴백: Google 번역 TTS + ffmpeg atempo로 속도 조정."""
    try:
        from gtts import gTTS
    except ImportError:
        logger.warning("[TTS] gTTS 미설치 — 폴백 불가 (pip install gTTS)")
        return False

    raw = path + ".gtts.mp3"
    try:
        gTTS(text=text, lang="ko").save(raw)
        factor = min(2.0, max(0.5, _rate_to_factor(rate)))
        _run(["ffmpeg", "-y", "-i", raw, "-filter:a", f"atempo={factor:.3f}", "-vn", path])
        ok = Path(path).exists() and Path(path).stat().st_size > 500
        if ok:
            logger.warning("[TTS] gTTS 폴백 사용 (음색이 다른 세그먼트와 다를 수 있음)")
        return ok
    except Exception as e:
        logger.warning(f"[TTS] gTTS 폴백 실패: {type(e).__name__}: {str(e)[:200]}")
        return False
    finally:
        try:
            Path(raw).unlink(missing_ok=True)
        except OSError:
            pass


def _audio_duration(path: str) -> float:
    """ffprobe로 오디오 파일 길이(초) 반환. 실패 시 0.0."""
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "json", path],
            capture_output=True, text=True, check=True,
        )
        return float(json.loads(r.stdout)["format"]["duration"])
    except Exception:
        return 0.0


def _generate_tts_with_voice_fallback(
    text: str, path: str, rate: str, primary_voice: str, pitch: str,
) -> bool:
    """edge-tts(음성 순차 폴백) → 실패 시 gTTS. 연속 실패 시 edge-tts 건너뜀."""
    global _EDGE_FAIL_STREAK

    text = (text or "").strip()
    if not text:
        logger.warning("[TTS] 빈 텍스트 — 건너뜀")
        return False

    try:
        Path(path).unlink(missing_ok=True)
    except OSError:
        pass

    if _EDGE_FAIL_STREAK < _EDGE_FAIL_LIMIT:
        voices = list(dict.fromkeys([primary_voice] + TTS_VOICE_FALLBACKS))
        for voice in voices:
            retries = 2 if voice == primary_voice else 1
            if _generate_tts_with_rate(text, path, rate, voice, pitch, retries=retries):
                if _audio_duration(path) > 0:
                    _EDGE_FAIL_STREAK = 0
                    logger.info(f"[TTS] 생성 성공: voice={voice}, rate={rate}")
                    return True
            try:
                Path(path).unlink(missing_ok=True)
            except OSError:
                pass
        _EDGE_FAIL_STREAK += 1
        logger.warning(
            f"[TTS] edge-tts 전체 실패 (연속 {_EDGE_FAIL_STREAK}/{_EDGE_FAIL_LIMIT})"
        )
    else:
        logger.info("[TTS] edge-tts 연속 실패로 이번 실행에서는 건너뛰고 gTTS 사용")

    if _generate_tts_gtts(text, path, rate) and _audio_duration(path) > 0:
        return True

    logger.error("[TTS] 모든 TTS 수단 실패 — 해당 세그먼트는 무음 처리됩니다.")
    return False


def _fit_tts_to_budget(
    text: str,
    base_path: str,
    budget_sec: float,
    tmp_dir: Path,
    seg_idx: int,
) -> tuple[str | None, float, str]:
    """
    기본 속도로 1회 생성 → 예산 초과 시 길이 비율로 필요한 속도를 계산해 1회만
    재생성합니다 (기존: 최대 5단계 × 음성 3개 = 15회 호출 → 최대 2회).
    반환: (경로 또는 None, 재생 시간, 사용 rate)
    """
    import math

    base_rate = TTS_RATE_STEPS[0]
    path1 = str(tmp_dir / f"tts_{seg_idx:02d}_base.mp3")

    if not _generate_tts_with_voice_fallback(text, path1, base_rate, TTS_VOICE, TTS_PITCH):
        return None, 0.0, base_rate

    dur = _audio_duration(path1)
    if dur <= 0:
        return None, 0.0, base_rate
    if dur <= budget_sec:
        return path1, dur, base_rate

    need = _rate_to_factor(base_rate) * dur / max(budget_sec * 0.97, 0.5)
    target = min(need, _rate_to_factor(TTS_RATE_STEPS[-1]))
    rate2 = f"+{int(math.ceil((target - 1) * 100))}%"
    path2 = str(tmp_dir / f"tts_{seg_idx:02d}_fast.mp3")

    if _generate_tts_with_voice_fallback(text, path2, rate2, TTS_VOICE, TTS_PITCH):
        dur2 = _audio_duration(path2)
        if 0 < dur2 < dur:
            if dur2 > budget_sec:
                logger.warning(
                    f"[TTS] 최고 속도에서도 예산 초과: seg={seg_idx}, "
                    f"{dur2:.2f}s > {budget_sec:.2f}s"
                )
            return path2, dur2, rate2

    logger.warning(f"[TTS] 속도 조정 실패, 기본 속도 음성 사용: seg={seg_idx}, {dur:.2f}s")
    return path1, dur, base_rate


# ═══════════════════════════════════════════════════════════════════════════════
# 폰트 헬퍼
# ═══════════════════════════════════════════════════════════════════════════════

def _load_font(size: int, bold: bool = True) -> ImageFont.FreeTypeFont:
    candidates = [_FONT_BLACK, _FONT_BOLD] if bold else [_FONT_REGULAR, _FONT_BOLD]
    for path in candidates:
        if Path(path).exists():
            try:
                return ImageFont.truetype(path, size, index=0)
            except Exception:
                continue
    return ImageFont.load_default()


# ═══════════════════════════════════════════════════════════════════════════════
# 픽셀 기반 줄바꿈
# ═══════════════════════════════════════════════════════════════════════════════

def _pixel_wrap(text: str, font, max_px: int) -> list[str]:
    _img  = Image.new("RGB", (10, 10))
    _draw = ImageDraw.Draw(_img)

    def _w(t):
        return _draw.textbbox((0, 0), t, font=font)[2]

    words, lines, current = text.split(), [], ""
    for word in words:
        sep       = "" if not current else " "
        candidate = current + sep + word
        if _w(candidate) <= max_px:
            current = candidate
        else:
            if current:
                lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines or [text]


# ═══════════════════════════════════════════════════════════════════════════════
# 배경 이미지 처리
# ═══════════════════════════════════════════════════════════════════════════════

def _prepare_bg(path, overlay_color: tuple, mode: str) -> Image.Image:
    """배경 이미지 → 1080×1920 RGB. 낮은 오버레이로 배경 잘 보이게."""
    W, H = VIDEO_W, VIDEO_H

    if path and Path(path).exists():
        try:
            bg = Image.open(path).convert("RGB")
            src_r = bg.width / bg.height
            dst_r = W / H
            if src_r > dst_r:
                new_h = bg.height
                new_w = int(new_h * dst_r)
                ox    = (bg.width - new_w) // 2
                bg    = bg.crop((ox, 0, ox + new_w, new_h))
            else:
                new_w = bg.width
                new_h = int(new_w / dst_r)
                oy    = int((bg.height - new_h) * 0.3)
                bg    = bg.crop((0, oy, new_w, oy + new_h))
            bg = bg.resize((W, H), Image.LANCZOS)
            bg = bg.filter(ImageFilter.GaussianBlur(radius=2))
        except Exception as e:
            logger.warning(f"배경 처리 실패: {e}")
            bg = _make_gradient_bg(overlay_color[:3], mode)
    else:
        bg = _make_gradient_bg(overlay_color[:3], mode)

    overlay = Image.new("RGBA", (W, H), overlay_color)
    result  = Image.alpha_composite(bg.convert("RGBA"), overlay)
    return result.convert("RGB")


def _make_gradient_bg(base_color: tuple, mode: str) -> Image.Image:
    W, H = VIDEO_W, VIDEO_H
    img  = Image.new("RGB", (W, H))
    d    = ImageDraw.Draw(img)
    r, g, b = base_color
    for y in range(H):
        t  = y / H
        lr = int(r * (1 - t * 0.4))
        lg = int(g * (1 - t * 0.2))
        lb = int(b + (80 - b) * t * 0.3)
        d.line([(0, y), (W, y)], fill=(max(0, lr), max(0, lg), max(0, min(255, lb))))
    return img


# ═══════════════════════════════════════════════════════════════════════════════
# 슬라이드 이미지 생성
# ═══════════════════════════════════════════════════════════════════════════════

def _draw_outlined(draw, pos, text, font, fill, outline=(0, 0, 0), ow=3):
    x, y = pos
    for dx, dy in [(-ow, 0), (ow, 0), (0, -ow), (0, ow),
                   (-ow, -ow), (ow, -ow), (-ow, ow), (ow, ow)]:
        draw.text((x + dx, y + dy), text, font=font, fill=(*outline, 220))
    draw.text((x, y), text, font=font, fill=fill)


def _text_size(draw, text, font) -> tuple[int, int, int]:
    """
    텍스트의 (너비, 높이, 상단 오프셋)을 반환합니다.
    상단 오프셋(bbox[1])은 폰트마다 다른 글리프 상단 여백으로, 세로 중앙 정렬 시
    이 오프셋을 반드시 빼줘야 실제 글리프가 박스 정중앙에 옵니다.
    """
    bbox = draw.textbbox((0, 0), text, font=font)
    width  = bbox[2] - bbox[0]
    height = bbox[3] - bbox[1]
    top_offset = bbox[1]
    return width, height, top_offset


def _draw_text_v_centered(
    draw, text, font, fill,
    box_top: int, box_height: int, center_x: int,
    outline: tuple | None = None, ow: int = 2,
) -> None:
    """박스 영역 안에서 텍스트를 세로 중앙, center_x 기준 가로 중앙 정렬합니다."""
    width, height, top_offset = _text_size(draw, text, font)
    x = center_x - width // 2
    y = box_top + (box_height - height) // 2 - top_offset
    if outline is not None:
        _draw_outlined(draw, (x, y), text, font, fill, outline, ow)
    else:
        draw.text((x, y), text, font=font, fill=fill)


def _make_slide(
    narration: str,
    keyword: str,
    description: str,
    theme: dict,
    slide_num: int,
    total: int,
    bg: Image.Image,
    is_hook: bool,
    is_cta: bool,
    tts_rate: str = TTS_RATE_STEPS[0],
    safe_bottom_zone: bool = False,
    hashtags: list[str] | None = None,
) -> Image.Image:
    """
    슬라이드 이미지 생성:
    - 상단: 진행 바 + 슬라이드 번호 + (속도 가속 배지, rate가 기본이 아닐 때만)
    - 중앙: 키워드 강조 박스 / 키워드 아래: 부연설명
    - 나래이션 자막: safe_bottom_zone=False → 화면 최하단,
                     True → TikTok UI(하단 ~20%)를 피해 중하단
    - safe_bottom_zone=True일 때만: 나래이션 자막 아래 해시태그 표시
    - safe_bottom_zone=False일 때만: 최하단 워터마크
    """
    W, H   = VIDEO_W, VIDEO_H
    img    = bg.copy()
    draw   = ImageDraw.Draw(img)

    accent    = theme["accent"]
    highlight = theme["highlight"]
    kw_bg     = theme["keyword_bg"]
    kw_fg     = theme["keyword_fg"]
    CX        = W // 2
    WRAP_PX   = W - 80

    f_badge    = _load_font(34, bold=True)
    f_keyword  = _load_font(108, bold=True)
    f_desc     = _load_font(50, bold=False)
    f_narr     = _load_font(43, bold=False)
    f_wm       = _load_font(30, bold=False)
    f_tag      = _load_font(32, bold=True)

    # ── 상단 진행 바 ─────────────────────────────────────────────────────────
    bar_w = int(W * slide_num / total)
    draw.rectangle([(0, 0), (W, 12)], fill=(255, 255, 255, 50))
    draw.rectangle([(0, 0), (bar_w, 12)], fill=(*accent, 255))

    # ── 슬라이드 번호 뱃지 ──────────────────────────────────────────────────
    badge = f"{slide_num} / {total}"
    bb    = draw.textbbox((0, 0), badge, font=f_badge)
    bw    = bb[2] - bb[0] + 40
    bh    = bb[3] - bb[1] + 22
    draw.rounded_rectangle([(40, 30), (40 + bw, 30 + bh)], radius=bh // 2,
                            fill=(*kw_bg, 230))
    _draw_text_v_centered(draw, badge, f_badge, kw_fg, 30, bh, 40 + bw // 2)

    # ── 속도 가속 배지 (기본 속도 초과 시에만 표시) ──────────────────────────
    if tts_rate != TTS_RATE_STEPS[0]:
        spd_txt = f"⚡ {tts_rate}"
        sb  = draw.textbbox((0, 0), spd_txt, font=f_badge)
        sw  = sb[2] - sb[0] + 36
        sh  = sb[3] - sb[1] + 18
        draw.rounded_rectangle(
            [(W - 40 - sw, 30), (W - 40, 30 + sh)],
            radius=sh // 2, fill=(255, 80, 80, 200)
        )
        _draw_text_v_centered(
            draw, spd_txt, f_badge, (255, 255, 255),
            30, sh, W - 40 - sw // 2,
        )

    # ── 훅 / CTA 배너 ────────────────────────────────────────────────────────
    banner_txt = ("오늘의 핵심 분석" if is_hook else "전체 분석 보기" if is_cta else None)
    if banner_txt:
        hb  = draw.textbbox((0, 0), banner_txt, font=f_badge)
        hw  = hb[2] - hb[0] + 48
        hh  = hb[3] - hb[1] + 26
        hx  = CX - hw // 2
        hy  = 100
        draw.rounded_rectangle([(hx, hy), (hx + hw, hy + hh)],
                                radius=hh // 2, fill=(*highlight, 240))
        _draw_text_v_centered(draw, banner_txt, f_badge, (20, 20, 20), hy, hh, CX)

    # ── 키워드 강조 박스 (화면 중앙) ─────────────────────────────────────────
    kw_clean = _strip_emoji(keyword)
    kw_tw, kw_th, _ = _text_size(draw, kw_clean, f_keyword)
    pad_x, pad_y = 60, 28
    kw_box_w = kw_tw + pad_x * 2
    kw_box_h = kw_th + pad_y * 2
    kw_x     = CX - kw_box_w // 2
    kw_y     = int(H * 0.30)

    # 그림자
    draw.rounded_rectangle(
        [(kw_x + 8, kw_y + 8), (kw_x + kw_box_w + 8, kw_y + kw_box_h + 8)],
        radius=20, fill=(0, 0, 0, 110)
    )
    # 메인 박스
    draw.rounded_rectangle(
        [(kw_x, kw_y), (kw_x + kw_box_w, kw_y + kw_box_h)],
        radius=20, fill=(*kw_bg, 248)
    )
    # 키워드 텍스트 (박스 안에서 세로·가로 정확히 중앙 정렬)
    _draw_text_v_centered(draw, kw_clean, f_keyword, kw_fg, kw_y, kw_box_h, CX)

    # 하단 장식 라인
    line_y = kw_y + kw_box_h + 18
    draw.rectangle([(CX - 130, line_y), (CX + 130, line_y + 6)], fill=(*highlight, 220))

    # ── 부연설명 (키워드 아래) ────────────────────────────────────────────────
    desc_clean  = _strip_emoji(description)
    desc_wrap_px = int(WRAP_PX * 0.78)
    desc_lines  = _pixel_wrap(desc_clean, f_desc, desc_wrap_px)
    desc_line_h = 60
    desc_y      = line_y + 26
    desc_total  = len(desc_lines) * desc_line_h + 24
    desc_box_x  = 60

    desc_bg_img = Image.new("RGBA", (W - 120, desc_total), (0, 0, 0, 155))
    img_rgba    = img.convert("RGBA")
    img_rgba.paste(desc_bg_img, (desc_box_x, desc_y), desc_bg_img)
    img  = img_rgba.convert("RGB")
    draw = ImageDraw.Draw(img)

    ty = desc_y + 12
    for line in desc_lines:
        lb = draw.textbbox((0, 0), line, font=f_desc)
        lw = lb[2] - lb[0]
        _draw_outlined(draw, (CX - lw // 2, ty), line, f_desc, (255, 255, 220), ow=2)
        ty += desc_line_h

    # ── 나래이션 자막 위치 계산 ──────────────────────────────────────────────
    narr_clean  = _strip_emoji(narration)
    narr_lines  = _pixel_wrap(narr_clean, f_narr, WRAP_PX - 40)[:4]
    narr_line_h = 55
    narr_total  = len(narr_lines) * narr_line_h + 36
    narr_left_x = 48

    if safe_bottom_zone:
        # 틱톡 모드: TikTok 자체 UI가 차지하는 하단 ~20% 영역을 피해서
        # 자막 블록의 "아래쪽 끝"이 H*0.76 지점에 오도록 배치
        narr_y = int(H * 0.76) - narr_total
    else:
        narr_y = H - narr_total - 54

    narr_bg = Image.new("RGBA", (W, narr_total + 16), (0, 0, 0, 190))
    img_rgba = img.convert("RGBA")
    img_rgba.paste(narr_bg, (0, narr_y - 8), narr_bg)
    img  = img_rgba.convert("RGB")
    draw = ImageDraw.Draw(img)

    ny = narr_y + 6
    for line in narr_lines:
        _draw_outlined(draw, (narr_left_x, ny), line, f_narr, (255, 255, 255), ow=2)
        ny += narr_line_h

    if safe_bottom_zone:
        # 해시태그 (자막 바로 아래, 여전히 TikTok UI 영역보다 위)
        if hashtags:
            tag_text = "  ".join(f"#{t}" for t in hashtags[:4])
            tb = draw.textbbox((0, 0), tag_text, font=f_tag)
            tw = tb[2] - tb[0]
            tag_y = narr_y + narr_total + 14
            _draw_outlined(
                draw, (CX - tw // 2, tag_y), tag_text, f_tag,
                (*highlight, 255), outline=(0, 0, 0), ow=2
            )
    else:
        # 워터마크 (쇼츠/릴스 모드에서만 최하단에 표시)
        wm  = "seedsup.tistory.com"
        wbb = draw.textbbox((0, 0), wm, font=f_wm)
        ww  = wbb[2] - wbb[0]
        draw.text((CX - ww // 2, H - 36), wm, font=f_wm, fill=(*accent, 155))

    return img


# ═══════════════════════════════════════════════════════════════════════════════
# 배경 이미지 확보 (Cloudflare Workers AI / Pexels / Pixabay —
# 모두 실패하면 _prepare_bg()가 브랜드 그라디언트로 대체)
# ═══════════════════════════════════════════════════════════════════════════════

def _download_bg_cloudflare(
    keywords: list[str], mode: str, dest: Path,
    cf_account_id: str, cf_api_token: str,
) -> bool:
    """
    Cloudflare Workers AI(FLUX.1-schnell)로 영상 배경을 새로 생성합니다.
    무료 한도(하루 10,000 뉴런, 매일 자정 UTC 초기화) 안에서 신용카드 등록
    없이 사용 가능합니다. 키가 없으면 조용히 False를 반환합니다.
    """
    if not cf_account_id or not cf_api_token:
        return False

    prompt = " ".join(keywords) if keywords else "stock market finance"
    suffix = FLUX_SUFFIX_VIDEO.get(mode, FLUX_SUFFIX_VIDEO["morning"])
    full_prompt = f"{prompt}{suffix}"

    # flux-1-schnell의 공식 입력 스키마는 prompt(필수)와 steps(기본 4, 최대 8)만
    # 받습니다 — width/height/seed는 보내면 400으로 거부됩니다.
    url = f"{CF_API_BASE}/{cf_account_id}/ai/run/{CF_FLUX_MODEL}"
    payload = {"prompt": full_prompt, "steps": 8}

    try:
        resp = requests.post(
            url,
            headers={
                "Authorization": f"Bearer {cf_api_token}",
                "Accept": "application/json",
            },
            json=payload,
            timeout=60,
        )
        if resp.status_code != 200:
            logger.warning(
                f"Cloudflare Workers AI 영상 배경 생성 실패 "
                f"({resp.status_code}): {resp.text[:200]}"
            )
            return False

        content_type = resp.headers.get("Content-Type", "")
        if content_type.startswith("image/"):
            img_bytes = resp.content
        else:
            data = resp.json()
            if not data.get("success", True):
                logger.warning(f"Cloudflare Workers AI 실패: {data.get('errors')}")
                return False
            b64_img = (data.get("result") or {}).get("image", "")
            if not b64_img:
                logger.warning("Cloudflare Workers AI 응답에 image 데이터가 없음")
                return False
            img_bytes = base64.b64decode(b64_img)

        dest.write_bytes(img_bytes)
        Image.open(dest).verify()
        logger.info("Cloudflare Workers AI 영상 배경 생성 성공 (텍스트 없는 새 이미지)")
        return True

    except Exception as e:
        logger.warning(f"Cloudflare Workers AI 영상 배경 생성 오류: {e}")
        return False


def _download_bg_pexels(keywords: list[str], dest: Path, pexels_key: str) -> bool:
    """Pexels에서 영상 배경 사진을 찾습니다. keywords를 순서대로 모두 시도합니다."""
    if not pexels_key:
        return False

    queries = keywords if keywords else ["finance"]
    for query in queries:
        try:
            resp = requests.get(
                "https://api.pexels.com/v1/search",
                headers={"Authorization": pexels_key},
                params={"query": query, "per_page": 10, "orientation": "portrait"},
                timeout=15,
            )
            resp.raise_for_status()
            photos = resp.json().get("photos", [])
            if not photos:
                logger.info(f"Pexels 검색 결과 없음({query}) — 다음 키워드로 재시도")
                continue

            idx     = int(time.time() / 86400) % len(photos)
            img_url = photos[idx]["src"]["large2x"]
            ir      = requests.get(img_url, timeout=30)
            ir.raise_for_status()
            dest.write_bytes(ir.content)
            Image.open(dest).verify()
            logger.info(f"Pexels 배경 확보 성공: '{query}'")
            return True
        except Exception as e:
            logger.warning(f"Pexels 실패 (키워드: {query}): {e}")
            continue

    logger.warning(f"Pexels: 모든 키워드 시도 실패 {queries}")
    return False


def _download_bg_pixabay(keywords: list[str], dest: Path, pixabay_key: str) -> bool:
    """Pexels 실패 시 마지막으로 시도하는 실사진 소스."""
    if not pixabay_key:
        return False
    queries = keywords if keywords else ["finance"]
    for query in queries:
        try:
            resp = requests.get(
                "https://pixabay.com/api/",
                params={
                    "key": pixabay_key, "q": query, "image_type": "photo",
                    "orientation": "vertical", "per_page": 10,
                    "safesearch": "true", "category": "business",
                },
                timeout=15,
            )
            resp.raise_for_status()
            hits = resp.json().get("hits", [])
            if not hits:
                logger.info(f"Pixabay 검색 결과 없음({query}) — 다음 키워드로 재시도")
                continue
            idx = int(time.time() / 86400) % len(hits)
            ir = requests.get(hits[idx]["largeImageURL"], timeout=30)
            ir.raise_for_status()
            dest.write_bytes(ir.content)
            Image.open(dest).verify()
            logger.info(f"Pixabay 배경 확보 성공: '{query}'")
            return True
        except Exception as e:
            logger.warning(f"Pixabay 실패 (키워드: {query}): {e}")
            continue
    logger.warning(f"Pixabay: 모든 키워드 시도 실패 {queries}")
    return False


# ═══════════════════════════════════════════════════════════════════════════════
# ffmpeg 헬퍼
# ═══════════════════════════════════════════════════════════════════════════════

def _run(cmd: list, **kwargs) -> subprocess.CompletedProcess:
    logger.debug("$ " + " ".join(str(c) for c in cmd))
    return subprocess.run(cmd, check=True, capture_output=True, text=True, **kwargs)


def _image_to_clip(img_path: str, duration: float, out_path: str):
    """PNG → MP4 클립."""
    _run([
        "ffmpeg", "-y",
        "-loop", "1", "-i", img_path,
        "-t", str(duration),
        "-c:v", "libx264", "-preset", "veryfast",
        "-crf", "20", "-pix_fmt", "yuv420p",
        "-r", "30", "-movflags", "+faststart",
        out_path,
    ])


def _concat_clips(clip_paths: list[str], out_path: str):
    with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
        for p in clip_paths:
            f.write(f"file '{os.path.abspath(p)}'\n")
        lst = f.name
    try:
        _run(["ffmpeg", "-y", "-f", "concat", "-safe", "0",
              "-i", lst, "-c", "copy", out_path])
    finally:
        os.unlink(lst)


def _merge_audio_to_video(video: str, tts_segments: list[dict], total_dur: float, out: str):
    """
    TTS 세그먼트를 타임라인에 배치하여 영상과 합성.
    BGM 없이 TTS 나래이션만 사용. 각 세그먼트의 시작 딜레이(seg["start"])는
    호출부(generate/generate_tiktok)에서 슬라이드 누적 시간 기준으로 계산되어
    전달됩니다 — 이 함수는 그 값을 그대로 배치할 뿐입니다.
    """
    if not tts_segments:
        _run(["ffmpeg", "-y", "-i", video, "-c:v", "copy", "-an", out])
        logger.warning("TTS 세그먼트 없음 — 무음 영상 출력")
        return

    inputs     = ["-i", video]
    fc_parts   = []
    tts_labels = []

    for i, seg in enumerate(tts_segments):
        inputs += ["-i", seg["path"]]
        delay   = int(seg["start"] * 1000)
        label   = f"[d{i}]"
        fc_parts.append(
            f"[{i+1}:a]adelay={delay}|{delay},apad=whole_dur={total_dur}{label}"
        )
        tts_labels.append(label)

    n = len(tts_labels)
    if n == 1:
        fc_parts[-1] = fc_parts[-1].replace(tts_labels[0], "[aout]")
        audio_out = "[aout]"
    else:
        mix = "".join(tts_labels) + f"amix=inputs={n}:duration=longest:normalize=0[aout]"
        fc_parts.append(mix)
        audio_out = "[aout]"

    fc = ";".join(fc_parts)

    _run([
        "ffmpeg", "-y",
        *inputs,
        "-filter_complex", fc,
        "-map", "0:v",
        "-map", audio_out,
        "-c:v", "copy",
        "-c:a", "aac", "-b:a", "192k",
        "-shortest",
        out,
    ])
    logger.info(f"오디오 합성 완료: {n}개 TTS 세그먼트")


# ═══════════════════════════════════════════════════════════════════════════════
# VideoGenerator 메인 클래스
# ═══════════════════════════════════════════════════════════════════════════════

class VideoGenerator:
    def __init__(self, output_dir: str = OUTPUT_DIR):
        self.output_dir = output_dir
        self.pexels_key = os.environ.get("PEXELS_API_KEY", "")
        self.pixabay_key = os.environ.get("PIXABAY_API_KEY", "").strip()
        self.gemini_key = os.environ.get("GEMINI_API_KEY", "")
        # 영상 배경 전용 Cloudflare Workers AI (텍스트 없는 새 배경 생성)
        self.cf_account_id = os.environ.get("CLOUDFLARE_ACCOUNT_ID", "").strip()
        self.cf_api_token = os.environ.get("CLOUDFLARE_API_TOKEN", "").strip()
        self.last_tiktok_hashtags: list[str] = []  # generate_tiktok() 호출 후 채워짐
        os.makedirs(output_dir, exist_ok=True)

    def generate(
        self,
        script: list[dict],
        mode: str,
        filename: str,
        thumbnail_url: str = "",
        blog_url: str = "",
        bg_keywords: list[str] | None = None,
        blog_content: str = "",
        blog_title: str = "",
    ) -> str:
        theme = THEMES.get(mode, THEMES["morning"])
        kws   = bg_keywords or PEXELS_KEYWORDS.get(mode, PEXELS_KEYWORDS["morning"])
        out   = os.path.join(self.output_dir, filename)

        # 1. 나래이션 스크립트 생성
        logger.info("나래이션 스크립트 생성 중 (Gemini API)...")
        if blog_content and self.gemini_key:
            narration_segments = generate_narration_script(
                blog_content, blog_title, mode, self.gemini_key
            )
        else:
            narration_segments = self._convert_script_to_narration(script, mode, blog_title)

        if not narration_segments:
            narration_segments = _fallback_script(blog_title, mode)

        logger.info(f"나래이션 세그먼트: {len(narration_segments)}개")

        with tempfile.TemporaryDirectory(prefix="shorts_v11_") as tmp_s:
            tmp = Path(tmp_s)

            # 2. 배경 이미지 확보 (blog thumbnail_url은 제목 텍스트가 박혀 있어 사용 안 함)
            #   1순위: Cloudflare Workers AI → 2순위: Pexels → 3순위: Pixabay
            #   모두 실패하면 _prepare_bg()의 브랜드 그라디언트 사용
            bg_path = tmp / "bg.jpg"
            bg_ok   = _download_bg_cloudflare(
                kws, mode, bg_path, self.cf_account_id, self.cf_api_token,
            )
            if not bg_ok:
                bg_ok = _download_bg_pexels(kws, bg_path, self.pexels_key)
            if not bg_ok:
                bg_ok = _download_bg_pixabay(kws, bg_path, self.pixabay_key)

            bg_img = _prepare_bg(bg_path if bg_ok else None, theme["overlay"], mode)

            # 3. 각 세그먼트: TTS 속도 자동 조정 → 슬라이드 시간 결정
            slide_clips  = []
            tts_segments = []
            current_time = 0.0
            total        = len(narration_segments)

            for i, seg in enumerate(narration_segments, 1):
                narration   = _strip_emoji(seg.get("narration", ""))
                keyword     = _strip_emoji(seg.get("keyword", "분석"))
                description = _strip_emoji(seg.get("description", ""))

                is_hook = (i == 1)
                is_cta  = (i == total)

                # 남은 전체 허용 시간 계산
                remaining = MAX_VIDEO_SEC - current_time

                # 슬라이드를 하나라도 더 넣을 수 없으면 중단
                if remaining < 2.5:
                    logger.info(f"남은 시간 {remaining:.2f}s — {i-1}개 슬라이드로 종료")
                    break

                # 이 세그먼트에 할당할 TTS 최대 허용 시간
                tts_budget = remaining - SLIDE_TAIL_SEC

                logger.info(
                    f"슬라이드 {i}/{total}: [{keyword}] "
                    f"budget={tts_budget:.2f}s | {narration[:30]}..."
                )

                # TTS 생성 (속도 자동 조정)
                tts_path, tts_dur, used_rate = _fit_tts_to_budget(
                    narration,
                    str(tmp / f"tts_{i:02d}.mp3"),
                    tts_budget,
                    tmp,
                    i,
                )

                if tts_path is None:
                    logger.error(
                        "[TTS] 세그먼트 %s 생성 실패 — 무음 슬라이드로 처리합니다.", i,
                    )
                    tts_dur = min(2.5, tts_budget)

                # 슬라이드 표시 시간 = TTS 실제 길이 + tail (remaining을 넘지 않도록)
                slide_dur = min(tts_dur + SLIDE_TAIL_SEC, remaining)

                slide_img = _make_slide(
                    narration, keyword, description,
                    theme, i, total, bg_img,
                    is_hook, is_cta,
                    tts_rate=used_rate,
                )
                img_path  = str(tmp / f"slide_{i:02d}.png")
                slide_img.save(img_path, "PNG", optimize=False)

                clip_path = str(tmp / f"clip_{i:02d}.mp4")
                _image_to_clip(img_path, slide_dur, clip_path)
                slide_clips.append(clip_path)

                # TTS 오디오 배치 (슬라이드 시작 후 0.15초 딜레이)
                if tts_path:
                    tts_segments.append({
                        "path":  tts_path,
                        "start": current_time + 0.15,
                    })

                logger.info(
                    f"  → rate={used_rate}, tts={tts_dur:.2f}s, "
                    f"slide={slide_dur:.2f}s, 누적={current_time + slide_dur:.2f}s"
                )
                current_time += slide_dur

            if not slide_clips:
                raise RuntimeError("생성된 슬라이드 클립이 없습니다.")

            total_duration = current_time
            logger.info(
                f"총 영상 길이: {total_duration:.2f}초 "
                f"({len(slide_clips)}/{total} 슬라이드)"
            )

            # 4. 클립 합치기
            silent_video = str(tmp / "silent.mp4")
            _concat_clips(slide_clips, silent_video)

            # 5. 오디오 합성 (TTS만, BGM 없음)
            _merge_audio_to_video(silent_video, tts_segments, total_duration, out)

            logger.info(f"영상 완료: {out} ({total_duration:.2f}초)")
            return out

    def _convert_script_to_narration(
        self, script: list[dict], mode: str, title: str
    ) -> list[dict]:
        """기존 youtube_script → 나래이션 형식 변환 (blog_content 없을 때 fallback)."""
        if not script:
            return _fallback_script(title, mode)

        result = []
        for seg in script:
            seg_title = _strip_emoji(seg.get("title", ""))
            seg_body  = _strip_emoji(seg.get("body", ""))
            narration = f"{seg_title}. {seg_body}" if seg_body else seg_title
            keyword   = seg_title[:6] if seg_title else "분석"
            desc      = seg_body[:25] if seg_body else "자세한 내용 확인"
            result.append({
                "narration":   narration,
                "keyword":     keyword,
                "description": desc,
            })
        return result

    def generate_tiktok(
        self,
        script: list[dict],
        mode: str,
        filename: str,
        thumbnail_url: str = "",
        blog_url: str = "",
        bg_keywords: list[str] | None = None,
        blog_content: str = "",
        blog_title: str = "",
    ) -> str:
        """
        틱톡 수익화 조건(1분 이상) 충족 전용 영상 생성 (이탈률 개선 버전).

        쇼츠(generate)와의 차이:
          - MAX_VIDEO_SEC 제한 없음
          - 밝고 빠른 젊은 여성 목소리(TTS_VOICE_TIKTOK, +38%)
          - 나래이션 자막을 하단 안전 영역(safe_bottom_zone)에 배치
          - 해시태그를 화면에도 표시하고 발행용 콘텐츠에도 반환
          - [1분 미달 방지]
            1차 나래이션이 60초 미만이면 참여 유도 CTA를 1개만 추가합니다.
            그래도 미달이면 CTA를 반복하지 않고 기존 내용을 유지한 채 더 긴
            나래이션을 Gemini로 재생성합니다. 확장 후에도 60초 미만이면
            영상을 반복해 길이를 채우지 않고 생성 실패로 처리합니다.
          - 배경: Pexels(역동적 톤) → Cloudflare Workers AI → Pixabay →
            브랜드 그라디언트 (블로그 썸네일은 제목 텍스트가 있어 사용 안 함)
        """
        theme = THEMES.get(mode, THEMES["morning"])
        kws   = bg_keywords or PEXELS_KEYWORDS_TIKTOK.get(mode, PEXELS_KEYWORDS_TIKTOK["morning"])
        out   = os.path.join(self.output_dir, filename)

        # 1. 틱톡 전용 나래이션 스크립트 생성 (6~7개 세그먼트 + 해시태그)
        logger.info("틱톡 나래이션 스크립트 생성 중 (Gemini API)...")
        if blog_content and self.gemini_key:
            narration_segments, hashtags = generate_narration_script_tiktok(
                blog_content, blog_title, mode, self.gemini_key
            )
        else:
            narration_segments = self._convert_script_to_narration(script, mode, blog_title)
            hashtags = _fallback_hashtags_tiktok(mode)

        if not narration_segments:
            narration_segments = _fallback_script_tiktok(blog_title, mode)
        if not hashtags:
            hashtags = _fallback_hashtags_tiktok(mode)

        # 세그먼트 수 상한 (본편 기준, 이후 붙는 참여 유도 CTA는 포함되지 않음)
        if len(narration_segments) > 9:
            logger.warning(
                f"틱톡 세그먼트 {len(narration_segments)}개 → 9개로 축소"
            )
            narration_segments = narration_segments[:9]

        logger.info(
            f"틱톡 나래이션 세그먼트: {len(narration_segments)}개, "
            f"해시태그: {hashtags}"
        )
        self.last_tiktok_hashtags = hashtags

        with tempfile.TemporaryDirectory(prefix="tiktok_v11_") as tmp_s:
            tmp = Path(tmp_s)

            # 2. 배경 이미지 확보
            bg_path = tmp / "bg.jpg"
            bg_ok   = _download_bg_pexels(kws, bg_path, self.pexels_key)

            if not bg_ok:
                bg_ok = _download_bg_cloudflare(
                    kws, mode, bg_path, self.cf_account_id, self.cf_api_token,
                )
            if not bg_ok:
                bg_ok = _download_bg_pixabay(kws, bg_path, self.pixabay_key)

            bg_img = _prepare_bg(bg_path if bg_ok else None, theme["overlay"], mode)

            # 3. 세그먼트 렌더링 (본편 + 필요 시 참여 유도 CTA 추가 보강)
            slide_clips  = []
            slide_durs   = []  # slide_clips와 1:1 대응하는 실제 재생 시간(초)
            tts_segments = []
            current_time = 0.0
            all_segments = list(narration_segments)
            rendered_count = 0  # all_segments 중 이미 렌더링된 개수
            cta_rounds = 0
            extended_narration_used = False

            while True:
                total = len(all_segments)
                for i in range(rendered_count + 1, total + 1):
                    seg = all_segments[i - 1]
                    narration   = _strip_emoji(seg.get("narration", ""))
                    keyword     = _strip_emoji(seg.get("keyword", "분석"))
                    description = _strip_emoji(seg.get("description", ""))

                    is_hook = (i == 1)
                    # 몇 개의 세그먼트가 추가될지 미리 알 수 없으므로 여기서는
                    # 모두 is_cta=False로 그리고, 최종 마지막 슬라이드만 아래에서
                    # 해시태그·CTA 배너와 함께 다시 그립니다.
                    is_cta  = False

                    logger.info(
                        f"[틱톡] 슬라이드 {i}/{total}: [{keyword}] "
                        f"누적={current_time:.2f}s | {narration[:30]}..."
                    )

                    # TTS 생성 — 젊은 여성 목소리, 빠른 속도, 시간 제한 없음
                    tts_path = str(tmp / f"tts_{i:02d}.mp3")

                    tts_ok = _generate_tts_with_voice_fallback(
                        narration,
                        tts_path,
                        TTS_RATE_TIKTOK,
                        primary_voice=TTS_VOICE_TIKTOK,
                        pitch=TTS_PITCH_TIKTOK,
                    )

                    if tts_ok:
                        tts_dur = _audio_duration(tts_path)

                        if tts_dur <= 0:
                            logger.error(
                                "[틱톡] 슬라이드 %s TTS 파일의 길이를 확인할 수 없습니다.", i,
                            )
                            tts_ok = False

                    if not tts_ok:
                        logger.error(
                            "[틱톡] 슬라이드 %s TTS 생성 실패. 해당 슬라이드는 무음으로 처리됩니다.", i,
                        )
                        tts_dur = 4.0

                    # 슬라이드 표시 시간 = TTS 길이 + 최소 여유(gap)
                    slide_dur = tts_dur + TTS_GAP_SEC_TIKTOK

                    slide_img = _make_slide(
                        narration, keyword, description,
                        theme, i, total, bg_img,
                        is_hook, is_cta,
                        tts_rate=TTS_RATE_STEPS[0],
                        safe_bottom_zone=True,
                        hashtags=None,
                    )
                    img_path  = str(tmp / f"slide_{i:02d}.png")
                    slide_img.save(img_path, "PNG", optimize=False)

                    clip_path = str(tmp / f"clip_{i:02d}.mp4")
                    _image_to_clip(img_path, slide_dur, clip_path)
                    slide_clips.append(clip_path)
                    slide_durs.append(slide_dur)

                    if tts_ok:
                        # 세그먼트 시작 딜레이도 최소화(0.05초)해 음성 사이 공백 축소
                        tts_segments.append({
                            "path":  tts_path,
                            "start": current_time + 0.05,
                        })

                    logger.info(
                        f"  → tts={tts_dur:.2f}s, slide={slide_dur:.2f}s, "
                        f"누적={current_time + slide_dur:.2f}s"
                    )
                    current_time += slide_dur

                rendered_count = total

                if current_time >= 60.0:
                    break

                # CTA는 딱 한 번만 추가한다.
                if cta_rounds == 0:
                    cta_rounds = 1

                    logger.warning(
                        f"[틱톡] 현재 {current_time:.2f}초로 1분 미달 — "
                        "참여 유도 CTA 1개만 추가합니다."
                    )

                    all_segments = append_engagement_cta_segments(
                        all_segments, mode, count=1,
                    )
                    continue

                # CTA 1개를 추가했는데도 60초 미달이면
                # CTA를 또 추가하지 않고, 전체 나래이션을 길게 재생성한다.
                if not extended_narration_used:
                    extended_narration_used = True

                    logger.warning(
                        f"[틱톡] CTA 1회 추가 후에도 {current_time:.2f}초로 1분 미달 — "
                        "나래이션 확장 재생성을 시작합니다."
                    )

                    extended_segments, extended_hashtags = generate_narration_script_tiktok(
                        blog_content,
                        blog_title,
                        mode,
                        self.gemini_key,
                        extended=True,
                    )

                    if extended_segments:
                        narration_segments = extended_segments

                        if extended_hashtags:
                            hashtags = extended_hashtags
                            self.last_tiktok_hashtags = hashtags

                        logger.info(
                            f"[틱톡] 확장 나래이션 재생성 완료: "
                            f"{len(narration_segments)}개 세그먼트"
                        )

                        all_segments = list(narration_segments)

                        # 기존 렌더링 결과를 모두 폐기하고 처음부터 다시 렌더링
                        slide_clips = []
                        slide_durs = []
                        tts_segments = []
                        current_time = 0.0
                        rendered_count = 0
                        cta_rounds = 0

                        continue

                    logger.warning(
                        "[틱톡] 확장 나래이션 재생성 실패 — 기존 영상으로 진행합니다."
                    )

                break

            if not slide_clips:
                raise RuntimeError("[틱톡] 생성된 슬라이드 클립이 없습니다.")

            # TTS 생성 성공률 검사 — 음성 없는 영상이 정상 결과물로 취급되는 것을 방지
            total_segments = len(all_segments)
            successful_tts = len(tts_segments)

            tts_success_rate = (
                successful_tts / total_segments if total_segments > 0 else 0.0
            )

            logger.info(
                "[틱톡] TTS 성공률: %s/%s (%.1f%%)",
                successful_tts, total_segments, tts_success_rate * 100,
            )

            if total_segments == 0 or tts_success_rate < 0.70:
                raise RuntimeError(
                    "[틱톡] TTS 생성 성공률 부족: "
                    f"{successful_tts}/{total_segments} "
                    f"({tts_success_rate * 100:.1f}%). "
                    "음성 서비스 상태를 확인한 후 다시 실행하세요."
                )

            # ── 최종 마지막 슬라이드만 is_cta=True + 해시태그로 다시 렌더링 ────
            # TTS는 이미 생성된 것을 재사용하므로 음성/타이밍에는 영향이 없습니다.
            final_total = len(all_segments)
            final_seg   = all_segments[-1]
            final_narration   = _strip_emoji(final_seg.get("narration", ""))
            final_keyword     = _strip_emoji(final_seg.get("keyword", "분석"))
            final_description = _strip_emoji(final_seg.get("description", ""))

            final_slide_img = _make_slide(
                final_narration, final_keyword, final_description,
                theme, final_total, final_total, bg_img,
                False, True,  # is_hook=False, is_cta=True
                tts_rate=TTS_RATE_STEPS[0],
                safe_bottom_zone=True,
                hashtags=hashtags,
            )
            final_img_path  = str(tmp / f"slide_{final_total:02d}_final.png")
            final_slide_img.save(final_img_path, "PNG", optimize=False)

            # 마지막 클립의 재생 시간은 렌더링 당시 계산된 slide_durs[-1]을 재사용
            # (TTS 성공/실패와 무관하게 세그먼트마다 항상 추가되므로 안전)
            last_slide_dur = slide_durs[-1] if slide_durs else 4.0 + TTS_GAP_SEC_TIKTOK
            final_clip_path = str(tmp / f"clip_{final_total:02d}_final.mp4")
            _image_to_clip(final_img_path, last_slide_dur, final_clip_path)
            slide_clips[-1] = final_clip_path

            total_duration = current_time
            logger.info(
                f"[틱톡] 총 영상 길이: {total_duration:.2f}초 "
                f"({len(slide_clips)}/{final_total} 슬라이드, "
                f"참여 유도 CTA {cta_rounds}개 추가) "
                f"— {'✅ 1분 초과' if total_duration >= 60 else '⚠️ 1분 미달'}"
            )

            # 최종 길이 검증: 영상을 복제해 길이를 채우지 않습니다.
            # (렌더링 비용을 아끼기 위해 합성 전에 먼저 검사)
            if total_duration < 60.0:
                raise RuntimeError(
                    "[틱톡] 영상 길이 부족: "
                    f"{total_duration:.2f}초. "
                    "CTA 추가와 확장 나래이션 생성 후에도 "
                    "60초를 충족하지 못해 결과물을 반환하지 않습니다."
                )

            # 4. 클립 합치기
            silent_video = str(tmp / "silent.mp4")
            _concat_clips(slide_clips, silent_video)

            # 5. 오디오 합성 (TTS만, BGM 없음)
            _merge_audio_to_video(silent_video, tts_segments, total_duration, out)

            logger.info(f"[틱톡] 최종 길이 검증 통과: {total_duration:.2f}초")
            logger.info(f"[틱톡] 영상 완료: {out} ({total_duration:.2f}초)")

            return out

    def generate_tiktok_with_fallback(
        self,
        script: list[dict],
        mode: str,
        filename: str,
        thumbnail_url: str = "",
        blog_url: str = "",
        bg_keywords: list[str] | None = None,
        blog_content: str = "",
        blog_title: str = "",
    ) -> str:
        """틱톡 영상 생성 (예외 처리 래퍼)."""
        try:
            return self.generate_tiktok(
                script, mode, filename,
                thumbnail_url, blog_url, bg_keywords,
                blog_content, blog_title,
            )
        except Exception as e:
            logger.error(f"틱톡 영상 생성 실패: {e}", exc_info=True)
            raise

    def generate_with_text_only_fallback(
        self,
        script: list[dict],
        mode: str,
        filename: str,
        thumbnail_url: str = "",
        blog_url: str = "",
        bg_keywords: list[str] | None = None,
        blog_content: str = "",
        blog_title: str = "",
    ) -> str:
        try:
            return self.generate(
                script, mode, filename,
                thumbnail_url, blog_url, bg_keywords,
                blog_content, blog_title,
            )
        except Exception as e:
            logger.error(f"영상 생성 실패: {e}", exc_info=True)
            raise