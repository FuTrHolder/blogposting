"""
숏폼 & 틱톡 영상 생성기 v8
==============================================
변경사항 (v8 — 틱톡 1분+ 영상 추가):
  - generate_tiktok(): 틱톡 수익화 조건(1분 이상) 충족 전용 생성 메서드 추가
      · 시간 제한 없음 (MAX_VIDEO_SEC 미적용)
      · 고정 속도 +28% (속도 자동 조정 없이 자연스러운 속도 유지)
      · 모든 세그먼트의 TTS가 끝난 뒤 영상 종료 (음성 잘림 없음)
      · 블로그 전체 내용을 더 풍부하게 커버하는 8~12개 세그먼트 스크립트
  - generate_tiktok_with_fallback(): 예외 처리 래퍼

v7 유지:
  - generate(): 쇼츠/릴스용 — 58초 제한, 속도 자동 조정(+28~75%)
  - TTS 길이 > budget 시 속도 단계적 상향, 슬라이드 = TTS 길이에 정확히 맞춤
  - 마지막 슬라이드 음성 잘림 완전 제거

공통:
  - 블로그 본문 기반 나래이션 스크립트 자동 생성 (Gemini API)
  - 키워드 강조 박스 + 부연설명 텍스트 오버레이
  - BGM 없음, TTS 나래이션 단독
  - 배경 이미지 투명도 절반

TTS: edge-tts ko-KR-InJoonNeural (젊은 남성)
규격: 1080×1920, 30fps, H.264
"""

import asyncio
import json
import logging
import os
import re
import subprocess
import tempfile
import time
import urllib.request
import urllib.error
from pathlib import Path

import requests
from PIL import Image, ImageDraw, ImageFilter, ImageFont

logger = logging.getLogger(__name__)

# ── 규격 ─────────────────────────────────────────────────────────────────────
VIDEO_W, VIDEO_H = 1080, 1920
OUTPUT_DIR       = "videos"
MAX_VIDEO_SEC    = 58.0   # 59초 이하 보장

# ── TTS 속도 단계 (쇼츠/릴스용 — 기본 → 최고속) ─────────────────────────────
# budget 초과 시 순서대로 시도. 더 빠른 속도로도 budget 안에 못 들어오면
# 마지막 속도(최고속)로 TTS를 생성하고, 슬라이드 시간을 TTS 길이에 맞춤.
# 정보 전달 속도를 높이기 위해 기본 시작 속도를 +28% → +34%로 상향.
TTS_RATE_STEPS = ["+34%", "+45%", "+56%", "+66%", "+75%"]
TTS_VOICE      = "ko-KR-InJoonNeural"   # 남성 — 쇼츠/릴스용
TTS_PITCH      = "-2Hz"
# 슬라이드 끝에 여유 시간 (TTS 끝나고 다음 슬라이드로 넘어가기 전 숨 쉬는 시간)
SLIDE_TAIL_SEC = 0.4

# ── TTS 설정 (틱톡 전용 — 젊고 밝은 여성 목소리, 빠른 속도) ─────────────────
# 이탈률 개선을 위해 쇼츠보다 더 활기차고 빠른 톤으로 차별화.
# ko-KR-SunHiNeural: 밝고 또렷한 젊은 여성 음색 (edge-tts 한국어 여성 보이스)
TTS_VOICE_TIKTOK = "ko-KR-SunHiNeural"
TTS_RATE_TIKTOK  = "+38%"   # 쇼츠 기본(+28%)보다 빠르게 — 밝고 경쾌한 텐션 유지
TTS_PITCH_TIKTOK = "+8Hz"   # 톤을 살짝 높여 더 젊고 밝은 느낌
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
# (틱톡은 빠른 스크롤 환경이라 정적인 사무실/야경보다 움직임이 느껴지는
# 비주얼이 시청 지속률에 유리)
PEXELS_KEYWORDS_TIKTOK = {
    "morning": ["stock trading screens closeup", "financial data dashboard dynamic", "trading floor energy"],
    "evening": ["stock market chart neon", "trading screens night dynamic", "financial data glow"],
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
# Gemini API: 나래이션 스크립트 생성
# ═══════════════════════════════════════════════════════════════════════════════

GEMINI_MODEL   = "gemini-2.5-flash-lite"
GEMINI_FALLBACK_MODEL = "gemini-2.5-flash"
GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/models/"

NARRATION_SYSTEM = """당신은 유튜브 쇼츠 나래이션 작가입니다.
블로그 본문을 읽고, 빠른 속도로 읽어도 55초 이하가 되는 나래이션 스크립트를 작성합니다.
한국어 발화 속도를 기준으로 세그먼트당 나래이션을 적절히 조절하세요.

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


# ── 틱톡 전용 나래이션 시스템 프롬프트 (이탈률 개선 — 짧고 강한 훅 중심) ────
NARRATION_SYSTEM_TIKTOK = """당신은 틱톡 금융 시황 채널의 바이럴 콘텐츠 작가입니다.
틱톡 알고리즘은 "첫 1~2초 이탈 여부"로 확산을 결정합니다. 시청 데이터를 보면
영상 길이가 길고 도입부가 약할수록 시청자가 2~3초 만에 스와이프하고 나갑니다.
이 문제를 해결하기 위해, 아래 규칙을 반드시 지키는 짧고 강렬한 스크립트를 작성합니다.

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
- "더 궁금하면 팔로우 하세요", "매일 새벽 이 시간에 업데이트됩니다" 같은 재방문 유도 문구 포함

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

    for model in [GEMINI_MODEL, GEMINI_FALLBACK_MODEL]:
        url     = f"{GEMINI_BASE_URL}{model}:generateContent?key={api_key}"
        payload = {
            "system_instruction": {"parts": [{"text": NARRATION_SYSTEM}]},
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {
                "temperature": 0.7,
                "maxOutputTokens": 2048,
                "responseMimeType": "application/json",
            },
        }
        data = json.dumps(payload).encode("utf-8")

        for attempt in range(1, 4):
            try:
                req = urllib.request.Request(
                    url, data=data,
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=60) as resp:
                    result = json.loads(resp.read().decode("utf-8"))
                raw = result["candidates"][0]["content"]["parts"][0]["text"].strip()

                # JSON 파싱 (코드블록 방어)
                if "```" in raw:
                    for part in raw.split("```"):
                        part = part.strip()
                        if part.startswith("json"):
                            part = part[4:].strip()
                        try:
                            parsed = json.loads(part)
                            segs = parsed.get("segments", [])
                            if segs:
                                logger.info(f"나래이션 스크립트 생성 완료: {len(segs)}개")
                                return segs
                        except json.JSONDecodeError:
                            continue

                parsed = json.loads(raw)
                segs   = parsed.get("segments", [])
                if segs:
                    logger.info(f"나래이션 스크립트 생성 완료: {len(segs)}개")
                    return segs

            except urllib.error.HTTPError as e:
                if e.code == 429:
                    time.sleep(30 * attempt)
                elif e.code in (500, 503):
                    time.sleep(10 * attempt)
                else:
                    logger.warning(f"Gemini 나래이션 API {e.code} ({model})")
                    break
            except Exception as e:
                logger.warning(f"나래이션 생성 실패 (시도 {attempt}, {model}): {e}")
                if attempt < 3:
                    time.sleep(10)
        # 모델 실패 → 다음 모델로

    logger.warning("나래이션 생성 전체 실패 — 기본 스크립트 사용")
    return _fallback_script(title, mode)


def generate_narration_script_tiktok(
    blog_content: str, title: str, mode: str, api_key: str
) -> tuple[list[dict], list[str]]:
    """
    틱톡용 나래이션 스크립트 생성 (이탈률 개선 버전).

    쇼츠와의 차이:
      - 세그먼트 5~7개 (기존 8~12개에서 축소 — 늘어지는 영상은 이탈률 상승 원인)
      - 전체 55~70초 (기존 90초+에서 단축 — 완주율 개선 목적)
      - 첫 세그먼트 훅 강화 규칙 명시 (인사말 금지, 충격적 수치/반전으로 시작)
      - 해시태그 4~6개도 함께 생성해 검색 유입 개선

    Returns:
        (segments, hashtags) 튜플. 실패 시에도 fallback으로 항상 유효한 값 반환.
    """
    if not api_key:
        logger.warning("GEMINI_API_KEY 없음 — 틱톡 기본 스크립트 사용")
        return _fallback_script_tiktok(title, mode), _fallback_hashtags_tiktok(mode)

    mode_label = "전일 마감 리뷰" if mode == "morning" else "프리마켓 & 이슈"
    prompt = (
        f"블로그 제목: {title}\n"
        f"포스팅 모드: {mode_label}\n\n"
        f"블로그 본문 전문:\n{blog_content[:5000]}\n\n"
        "위 내용을 바탕으로 틱톡용 나래이션 스크립트를 JSON으로 작성해주세요.\n"
        "세그먼트는 6~7개, 전체 길이는 65초~80초여야 합니다 (실제 음성 재생은 "
        "이보다 빨라지므로 목표를 넉넉히 잡아야 최종 영상이 60초를 넘습니다).\n"
        "블로그의 핵심 내용 중 가장 임팩트 있는 것만 선별하고, 다 담으려 하지 마세요.\n"
        "1번 세그먼트는 반드시 강력한 훅으로 시작해야 합니다 (인사말 절대 금지)."
    )

    for model in [GEMINI_MODEL, GEMINI_FALLBACK_MODEL]:
        url     = f"{GEMINI_BASE_URL}{model}:generateContent?key={api_key}"
        payload = {
            "system_instruction": {"parts": [{"text": NARRATION_SYSTEM_TIKTOK}]},
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {
                "temperature": 0.75,
                "maxOutputTokens": 3072,
                "responseMimeType": "application/json",
            },
        }
        data = json.dumps(payload).encode("utf-8")

        for attempt in range(1, 4):
            try:
                req = urllib.request.Request(
                    url, data=data,
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=90) as resp:
                    result = json.loads(resp.read().decode("utf-8"))
                raw = result["candidates"][0]["content"]["parts"][0]["text"].strip()

                parsed = None
                if "```" in raw:
                    for part in raw.split("```"):
                        part = part.strip()
                        if part.startswith("json"):
                            part = part[4:].strip()
                        try:
                            parsed = json.loads(part)
                            break
                        except json.JSONDecodeError:
                            continue
                if parsed is None:
                    parsed = json.loads(raw)

                segs = parsed.get("segments", [])
                tags = parsed.get("hashtags", [])
                if segs:
                    logger.info(
                        f"틱톡 나래이션 스크립트 생성 완료: {len(segs)}개 세그먼트, "
                        f"해시태그 {len(tags)}개"
                    )
                    if not tags:
                        tags = _fallback_hashtags_tiktok(mode)
                    return segs, tags

            except urllib.error.HTTPError as e:
                if e.code == 429:
                    time.sleep(30 * attempt)
                elif e.code in (500, 503):
                    time.sleep(10 * attempt)
                else:
                    logger.warning(f"Gemini 틱톡 나래이션 API {e.code} ({model})")
                    break
            except Exception as e:
                logger.warning(f"틱톡 나래이션 생성 실패 (시도 {attempt}, {model}): {e}")
                if attempt < 3:
                    time.sleep(10)

    logger.warning("틱톡 나래이션 생성 전체 실패 — 기본 스크립트 사용")
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
# generate_narration_script_tiktok()이 만든 스크립트를 다 읽어도 60초에
# 못 미치면, 내용을 새로 만들지 않고 이 풀에서 순서대로 골라 이어 붙입니다.
# 매번 같은 문구가 반복되지 않도록 여러 버전을 준비해 두고 필요한 만큼만
# 사용합니다 (append_engagement_cta_segments 참고).
_ENGAGEMENT_CTA_POOL = [
    {"narration": "이 영상이 도움이 되셨다면 좋아요 눌러주세요. 다음 소식도 놓치지 않게 팔로우까지 부탁드려요.",
     "keyword": "좋아요 부탁", "description": "매일 업데이트되는 시황 브리핑"},
    {"narration": "주변에 투자 정보가 필요한 분이 있다면 이 영상 공유해주세요. 함께 보면 더 좋아요.",
     "keyword": "공유하기", "description": "투자 정보가 필요한 분께 공유"},
    {"narration": "다음 브리핑도 궁금하시다면 리포스트로 저장해두세요. 놓치지 않고 바로 확인하실 수 있어요.",
     "keyword": "리포스트", "description": "다음 브리핑 놓치지 않기"},
    {"narration": "매일 새벽 업데이트되는 시황, 팔로우 한 번이면 계속 받아보실 수 있습니다.",
     "keyword": "팔로우 필수", "description": "매일 업데이트되는 시황 브리핑"},
]


def append_engagement_cta_segments(
    segments: list[dict], mode: str, count: int = 1
) -> list[dict]:
    """
    engagement CTA 세그먼트를 count개만큼 풀에서 순서대로 골라 뒤에 덧붙입니다.
    같은 스크립트 안에서 중복 문구가 나오지 않도록 이미 쓰인 인덱스를 건너뜁니다.
    """
    if count <= 0:
        return segments

    extended = list(segments)
    pool_len = len(_ENGAGEMENT_CTA_POOL)
    for i in range(count):
        cta = dict(_ENGAGEMENT_CTA_POOL[i % pool_len])
        extended.append(cta)
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
            {"narration": "오늘 핵심은 이것 하나입니다. 더 자세한 분석 궁금하면 팔로우 해주세요.",
             "keyword": "팔로우", "description": "매일 업데이트되는 시황 브리핑"},
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
            {"narration": "오늘 밤 강세일까 약세일까, 핵심만 정리했습니다. 팔로우하면 매일 받아보실 수 있어요.",
             "keyword": "팔로우", "description": "매일 업데이트되는 시황 브리핑"},
        ]


# ═══════════════════════════════════════════════════════════════════════════════
# TTS: 속도 자동 조정 (핵심 변경 — v7)
# ═══════════════════════════════════════════════════════════════════════════════

async def _tts_async(text: str, path: str, rate: str, voice: str = TTS_VOICE, pitch: str = TTS_PITCH):
    """지정된 속도/보이스/피치로 TTS 생성."""
    import edge_tts
    comm = edge_tts.Communicate(text=text, voice=voice, rate=rate, pitch=pitch)
    await comm.save(path)


def _generate_tts_with_rate(
    text: str, path: str, rate: str,
    voice: str = TTS_VOICE, pitch: str = TTS_PITCH,
) -> bool:
    """지정 속도/보이스/피치로 TTS 파일 생성. 성공 여부 반환."""
    try:
        asyncio.run(_tts_async(text, path, rate, voice, pitch))
        ok = Path(path).exists() and Path(path).stat().st_size > 500
        if not ok:
            logger.warning(f"TTS 파일 생성 실패 또는 너무 작음: {path}")
        return ok
    except ImportError:
        logger.error("edge-tts 미설치 — pip install edge-tts 필요")
        return False
    except Exception as e:
        logger.error(f"TTS 생성 오류 (voice={voice}, rate={rate}): {e}")
        return False


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


def _fit_tts_to_budget(
    text: str,
    base_path: str,
    budget_sec: float,
    tmp_dir: Path,
    seg_idx: int,
) -> tuple[str, float, str]:
    """
    TTS를 생성하고, 길이가 budget_sec을 초과하면 속도를 단계적으로 올려
    budget 안에 들어오도록 재생성합니다.

    budget_sec: 이 세그먼트에 허용된 최대 TTS 시간 (슬라이드 tail 제외)
    반환: (사용된 tts 파일 경로, 실제 tts 길이, 사용된 rate 문자열)

    - 어떤 속도로도 budget을 맞추지 못하면, 마지막 속도(최고속)로 생성된
      파일을 그대로 반환하고 호출부에서 슬라이드 시간을 TTS 길이에 맞춤
      (절대 음성이 잘리지 않도록 보장).
    """
    best_path = base_path
    best_dur  = 0.0
    best_rate = TTS_RATE_STEPS[0]

    for rate in TTS_RATE_STEPS:
        candidate_path = str(tmp_dir / f"tts_{seg_idx:02d}_{rate.replace('+','p').replace('%','pct')}.mp3")
        ok = _generate_tts_with_rate(text, candidate_path, rate)
        if not ok:
            logger.warning(f"TTS 생성 실패 (rate={rate}, seg={seg_idx})")
            continue

        dur = _audio_duration(candidate_path)
        if dur <= 0:
            continue

        best_path = candidate_path
        best_dur  = dur
        best_rate = rate

        if dur <= budget_sec:
            logger.info(
                f"  [seg {seg_idx}] TTS 확정: rate={rate}, "
                f"dur={dur:.2f}s, budget={budget_sec:.2f}s ✓"
            )
            break
        else:
            logger.info(
                f"  [seg {seg_idx}] TTS {dur:.2f}s > budget {budget_sec:.2f}s "
                f"→ rate {rate} 초과, 다음 속도로 재시도"
            )

    if best_dur <= 0:
        # 모든 시도 실패 → fallback 길이 반환
        logger.error(f"  [seg {seg_idx}] 모든 TTS 시도 실패 — 4.0초 fallback")
        best_dur  = 4.0
        best_rate = TTS_RATE_STEPS[-1]

    return best_path, best_dur, best_rate


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
    상단 오프셋(bbox[1])은 폰트마다 다른 글리프 상단 여백으로, 이 값을
    무시하고 y좌표만으로 배치하면 텍스트가 박스 안에서 아래로 치우쳐
    보이는 원인이 됩니다 (예: 도형 안 키워드 텍스트가 세로 중앙이 아니라
    아래쪽에 쏠려 보이는 현상). 세로 중앙 정렬 시 이 오프셋을 반드시
    빼줘야 실제 글리프가 박스 정중앙에 오게 됩니다.
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
    """
    주어진 박스 영역(box_top ~ box_top+box_height) 안에서 텍스트를 정확히
    세로 중앙 정렬하고, 가로로는 center_x를 기준으로 가운데 정렬합니다.
    """
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
    - 중앙: 키워드 강조 박스 (크고 눈에 띄게)
    - 키워드 아래: 부연설명 텍스트 (반투명 배경)
    - 나래이션 자막: safe_bottom_zone=False → 화면 최하단
                     safe_bottom_zone=True  → 화면 하단 22% 지점 (TikTok 자체
                     캡션·프로필·버튼 UI가 차지하는 하단 영역과 겹치지 않도록
                     위로 이동. 틱톡 앱은 계정명·캡션·해시태그를 화면 하단
                     ~18~22% 구간에 렌더링하므로, 우리 자막이 그 위쪽에서
                     끝나야 서로 겹치지 않음)
    - safe_bottom_zone=True일 때만: 나래이션 자막 아래 해시태그 배지 표시
    - 최하단(safe_bottom_zone=False일 때만): 워터마크
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

    # 폰트
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
    # 자동 줄바꿈 폭을 화면 폭의 약 78%로 좁혀, 긴 설명도 자연스럽게
    # 2~3줄로 나뉘어 시각적으로 편안하게 읽히도록 함
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
    narr_left_x = 48  # 왼쪽 정렬 시작 x좌표 (배경 패딩 고려)

    if safe_bottom_zone:
        # 틱톡 모드: TikTok 자체 UI(계정명·캡션·좋아요/공유 버튼)가 차지하는
        # 하단 ~20% 영역을 피해서, 자막 블록의 "아래쪽 끝"이 H*0.76 지점에
        # 오도록 배치 (화면 최하단이 아니라 중하단에서 자막이 끝남)
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
        # 나래이션은 가독성을 위해 왼쪽 정렬 (기존 가운데 정렬에서 변경)
        _draw_outlined(draw, (narr_left_x, ny), line, f_narr, (255, 255, 255), ow=2)
        ny += narr_line_h

    if safe_bottom_zone:
        # ── 해시태그 배지 (자막 바로 아래, 여전히 TikTok UI 영역보다 위) ────
        if hashtags:
            tag_text = "  ".join(f"#{t}" for t in hashtags[:4])
            tb = draw.textbbox((0, 0), tag_text, font=f_tag)
            tw = tb[2] - tb[0]
            tag_y = narr_y + narr_total + 14
            _draw_outlined(
                draw, (CX - tw // 2, tag_y), tag_text, f_tag,
                (*highlight, 255), outline=(0, 0, 0), ow=2
            )
        # 틱톡 모드에서는 하단 20%를 완전히 비워두므로 워터마크 생략
        # (TikTok 자체 UI와 겹칠 수 있는 최하단 텍스트를 추가하지 않음)
    else:
        # ── 워터마크 (쇼츠/릴스 모드에서만 최하단에 표시) ────────────────────
        wm  = "seedsup.tistory.com"
        wbb = draw.textbbox((0, 0), wm, font=f_wm)
        ww  = wbb[2] - wbb[0]
        draw.text((CX - ww // 2, H - 36), wm, font=f_wm, fill=(*accent, 155))

    return img


# ═══════════════════════════════════════════════════════════════════════════════
# 배경 이미지 다운로드
# ═══════════════════════════════════════════════════════════════════════════════

def _download_bg_pexels(keywords: list[str], dest: Path, pexels_key: str) -> bool:
    if not pexels_key:
        return False
    query = keywords[0] if keywords else "finance"
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
            return False
        idx     = int(time.time() / 86400) % len(photos)
        img_url = photos[idx]["src"]["large2x"]
        ir      = requests.get(img_url, timeout=30)
        ir.raise_for_status()
        dest.write_bytes(ir.content)
        Image.open(dest).verify()
        return True
    except Exception as e:
        logger.warning(f"Pexels 실패: {e}")
        return False


def _download_bg_picsum(dest: Path, seed: int = 0) -> bool:
    try:
        url  = f"https://picsum.photos/seed/{seed}/1080/1920"
        resp = requests.get(url, timeout=20, allow_redirects=True)
        resp.raise_for_status()
        if len(resp.content) < 10_000:
            return False
        dest.write_bytes(resp.content)
        Image.open(dest).verify()
        return True
    except Exception as e:
        logger.warning(f"picsum 실패: {e}")
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
        self.gemini_key = os.environ.get("GEMINI_API_KEY", "")
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

        with tempfile.TemporaryDirectory(prefix="shorts_v7_") as tmp_s:
            tmp = Path(tmp_s)

            # 2. 배경 이미지 확보
            bg_path = tmp / "bg.jpg"
            bg_ok   = False

            if thumbnail_url:
                try:
                    r = requests.get(thumbnail_url, timeout=15,
                                     headers={"User-Agent": "Mozilla/5.0"})
                    r.raise_for_status()
                    bg_path.write_bytes(r.content)
                    Image.open(bg_path).verify()
                    bg_ok = True
                    logger.info("티스토리 썸네일 배경 로드 성공")
                except Exception as e:
                    logger.warning(f"썸네일 로드 실패: {e}")

            if not bg_ok:
                bg_ok = _download_bg_pexels(kws, bg_path, self.pexels_key)
            if not bg_ok:
                import hashlib
                seed  = int(hashlib.md5(f"{mode}{filename}".encode()).hexdigest()[:8], 16)
                bg_ok = _download_bg_picsum(bg_path, seed % 1000)

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
                # tail(여유) 포함 슬라이드 최대 = remaining 전체
                # TTS budget = remaining - SLIDE_TAIL_SEC
                tts_budget = remaining - SLIDE_TAIL_SEC

                logger.info(
                    f"슬라이드 {i}/{total}: [{keyword}] "
                    f"budget={tts_budget:.2f}s | {narration[:30]}..."
                )

                # TTS 생성 (속도 자동 조정 — v7 핵심)
                tts_path, tts_dur, used_rate = _fit_tts_to_budget(
                    narration,
                    str(tmp / f"tts_{i:02d}.mp3"),
                    tts_budget,
                    tmp,
                    i,
                )

                # 슬라이드 표시 시간 = TTS 실제 길이 + tail
                # (remaining을 넘지 않도록 클리핑)
                slide_dur = min(tts_dur + SLIDE_TAIL_SEC, remaining)

                # 슬라이드 이미지 생성 (사용된 속도를 배지로 전달)
                slide_img = _make_slide(
                    narration, keyword, description,
                    theme, i, total, bg_img,
                    is_hook, is_cta,
                    tts_rate=used_rate,
                )
                img_path  = str(tmp / f"slide_{i:02d}.png")
                slide_img.save(img_path, "PNG", optimize=False)

                # 이미지 → MP4 클립 (슬라이드 표시 시간)
                clip_path = str(tmp / f"clip_{i:02d}.mp4")
                _image_to_clip(img_path, slide_dur, clip_path)
                slide_clips.append(clip_path)

                # TTS 오디오 배치 (슬라이드 시작 후 0.15초 딜레이)
                if tts_dur > 0:
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
          - MAX_VIDEO_SEC 제한 없음 — 세그먼트 수/TTS 길이에 따라 자연스럽게 결정
          - 밝고 빠른 젊은 여성 목소리(TTS_VOICE_TIKTOK, +38%) — 쇼츠와 차별화된
            텐션으로 이탈률 개선
          - 세그먼트 5~7개, 전체 55~70초 (기존 8~12개/90초+에서 축소 —
            늘어지는 러닝타임 자체가 이탈의 주요 원인이었음)
          - 나래이션 자막을 화면 하단 안전 영역(safe_bottom_zone)에 배치해
            TikTok 자체 캡션·계정명 UI와 겹치지 않도록 함
          - 세그먼트 간 TTS 이어붙임 gap을 최소화(TTS_GAP_SEC_TIKTOK)해
            음성 끊김 체감 완화
          - 해시태그를 화면에도 표시하고, 대시보드/발행용 콘텐츠에도 반환
          - [1분 미달 방지] 실제 TTS 합성 결과가 60초 미만이면, 스크립트를
            처음부터 다시 만들지 않고 "좋아요/팔로우/공유" 유도 세그먼트를
            뒤에 추가로 이어 붙여 60초를 넘길 때까지 보강합니다(최대 3회 시도).
            빠른 여성 목소리(+38%)는 같은 텍스트도 남성 기본 속도보다 짧게
            끝나므로, 스크립트 생성만으로는 60초를 못 채우는 경우가 있어
            이 런타임 보강 단계가 최종 안전장치 역할을 합니다.
        """
        theme = THEMES.get(mode, THEMES["morning"])
        # bg_keywords가 명시적으로 전달되지 않았으면 틱톡 전용(더 역동적인) 키워드 사용
        kws   = bg_keywords or PEXELS_KEYWORDS_TIKTOK.get(mode, PEXELS_KEYWORDS_TIKTOK["morning"])
        out   = os.path.join(self.output_dir, filename)

        # 1. 틱톡 전용 나래이션 스크립트 생성 (5~7개 세그먼트 + 해시태그)
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

        # 이탈률 방지를 위해 세그먼트 수를 강제로도 상한선 적용 (최대 7개).
        # 단, 이 상한은 "본편" 세그먼트에만 적용 — 60초 미달 시 뒤에 붙는
        # 참여 유도 CTA 세그먼트는 이 개수에 포함되지 않습니다.
        if len(narration_segments) > 7:
            logger.warning(
                f"틱톡 세그먼트 {len(narration_segments)}개 → 7개로 축소 (이탈률 방지)"
            )
            narration_segments = narration_segments[:7]

        logger.info(
            f"틱톡 나래이션 세그먼트: {len(narration_segments)}개, "
            f"해시태그: {hashtags}"
        )
        self.last_tiktok_hashtags = hashtags  # 발행 파이프라인에서 참조 가능하도록 보관

        with tempfile.TemporaryDirectory(prefix="tiktok_v9_") as tmp_s:
            tmp = Path(tmp_s)

            # 2. 배경 이미지 확보 (쇼츠와 차별화 — 틱톡은 역동적인 Pexels 배경을
            #    우선 시도하고, 실패 시에만 쇼츠와 동일한 티스토리 썸네일로 폴백.
            #    쇼츠(generate())는 반대로 티스토리 썸네일을 최우선으로 씁니다 —
            #    두 채널이 항상 같은 배경을 쓰지 않도록 우선순위를 분리했습니다.)
            bg_path = tmp / "bg.jpg"
            bg_ok   = _download_bg_pexels(kws, bg_path, self.pexels_key)

            if not bg_ok and thumbnail_url:
                try:
                    r = requests.get(thumbnail_url, timeout=15,
                                     headers={"User-Agent": "Mozilla/5.0"})
                    r.raise_for_status()
                    bg_path.write_bytes(r.content)
                    Image.open(bg_path).verify()
                    bg_ok = True
                    logger.info("틱톡 배경: 티스토리 썸네일로 폴백")
                except Exception as e:
                    logger.warning(f"썸네일 로드 실패: {e}")

            if not bg_ok:
                import hashlib
                seed  = int(hashlib.md5(f"tiktok{mode}{filename}".encode()).hexdigest()[:8], 16)
                bg_ok = _download_bg_picsum(bg_path, seed % 1000)

            bg_img = _prepare_bg(bg_path if bg_ok else None, theme["overlay"], mode)

            # 3. 세그먼트 렌더링 (본편 + 필요 시 참여 유도 CTA 추가 보강)
            slide_clips  = []
            tts_segments = []
            current_time = 0.0
            all_segments = list(narration_segments)
            rendered_count = 0  # all_segments 중 이미 렌더링된 개수
            cta_rounds = 0
            MAX_CTA_ROUNDS = 3  # 무한 루프 방지 (최대 3회, 라운드당 1개씩 추가)

            while True:
                total = len(all_segments)
                # 아직 렌더링하지 않은 세그먼트만 순서대로 처리
                for i in range(rendered_count + 1, total + 1):
                    seg = all_segments[i - 1]
                    narration   = _strip_emoji(seg.get("narration", ""))
                    keyword     = _strip_emoji(seg.get("keyword", "분석"))
                    description = _strip_emoji(seg.get("description", ""))

                    is_hook = (i == 1)
                    # 해시태그/CTA 배너는 "현재 라운드의 마지막"이 아니라 "이후에
                    # CTA가 더 붙을 가능성이 있는지"를 감안해서 판단해야 합니다.
                    # cta_rounds가 이미 MAX_CTA_ROUNDS에 도달했거나 이번 세그먼트
                    # 렌더링 후 60초를 넘길 게 확실하지 않다면, 안전하게 "60초를
                    # 넘긴 이후에 마지막으로 렌더링되는 세그먼트에만" 표시되도록
                    # 실제 최종 여부는 라운드 종료 후 별도 후처리로 확정합니다.
                    is_cta  = False  # 아래 최종 후처리 단계에서 마지막 슬라이드에만 다시 그림

                    logger.info(
                        f"[틱톡] 슬라이드 {i}/{total}: [{keyword}] "
                        f"누적={current_time:.2f}s | {narration[:30]}..."
                    )

                    # TTS 생성 — 젊은 여성 목소리, 빠른 속도, 시간 제한 없음
                    tts_path = str(tmp / f"tts_{i:02d}.mp3")
                    tts_ok   = _generate_tts_with_rate(
                        narration, tts_path, TTS_RATE_TIKTOK,
                        voice=TTS_VOICE_TIKTOK, pitch=TTS_PITCH_TIKTOK,
                    )

                    if tts_ok:
                        tts_dur = _audio_duration(tts_path)
                        if tts_dur <= 0:
                            logger.warning(f"[틱톡] 슬라이드 {i} TTS 길이 0 — 4.0초 fallback")
                            tts_dur = 4.0
                            tts_ok  = False
                    else:
                        tts_dur = 4.0
                        logger.warning(f"[틱톡] 슬라이드 {i} TTS 생성 실패 — 4.0초 fallback")

                    # 슬라이드 표시 시간 = TTS 길이 + 최소 여유(gap).
                    slide_dur = tts_dur + TTS_GAP_SEC_TIKTOK

                    # 슬라이드 이미지 생성 (safe_bottom_zone=True — TikTok UI 회피,
                    # 마지막 세그먼트에서만 해시태그 표시)
                    slide_img = _make_slide(
                        narration, keyword, description,
                        theme, i, total, bg_img,
                        is_hook, is_cta,
                        tts_rate=TTS_RATE_STEPS[0],  # 속도 배지 로직 재사용 안 함(항상 미표시)
                        safe_bottom_zone=True,
                        hashtags=hashtags if is_cta else None,
                    )
                    img_path  = str(tmp / f"slide_{i:02d}.png")
                    slide_img.save(img_path, "PNG", optimize=False)

                    # 이미지 → MP4 클립
                    clip_path = str(tmp / f"clip_{i:02d}.mp4")
                    _image_to_clip(img_path, slide_dur, clip_path)
                    slide_clips.append(clip_path)

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

                # ── 1분 미달 여부 확인 → 미달이면 CTA 세그먼트 1개 추가 후 재시도 ──
                if current_time >= 60.0:
                    break
                if cta_rounds >= MAX_CTA_ROUNDS:
                    logger.warning(
                        f"[틱톡] {MAX_CTA_ROUNDS}회 보강 후에도 "
                        f"{current_time:.2f}초로 1분 미달 — 더 이상 보강하지 않고 종료"
                    )
                    break

                cta_rounds += 1
                logger.warning(
                    f"[틱톡] 현재 {current_time:.2f}초로 1분 미달 — "
                    f"참여 유도 CTA 세그먼트 추가 (보강 {cta_rounds}/{MAX_CTA_ROUNDS})"
                )
                all_segments = append_engagement_cta_segments(all_segments, mode, count=1)

            if not slide_clips:
                raise RuntimeError("[틱톡] 생성된 슬라이드 클립이 없습니다.")

            # ── 최종 마지막 슬라이드만 is_cta=True로 다시 렌더링 ───────────────
            # 위 루프에서는 몇 개의 세그먼트가 추가될지 미리 알 수 없어 모든
            # 세그먼트를 is_cta=False로 그렸습니다. 이제 전체 세그먼트 수가
            # 확정됐으므로, 실제 마지막 슬라이드(해시태그·CTA 배너가 보여야
            # 하는 화면)만 다시 그려서 해당 클립 하나만 교체합니다. TTS는
            # 이미 생성된 것을 재사용하므로 음성/타이밍에는 영향이 없습니다.
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

            # 마지막 클립의 재생 시간(slide_dur)은 그대로 유지하고 이미지만 교체
            last_tts_dur = _audio_duration(tts_segments[-1]["path"]) if tts_segments else 4.0
            last_slide_dur = max(last_tts_dur + TTS_GAP_SEC_TIKTOK, 1.0)
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

            # 4. 클립 합치기
            silent_video = str(tmp / "silent.mp4")
            _concat_clips(slide_clips, silent_video)

            # 5. 오디오 합성 (TTS만, BGM 없음)
            _merge_audio_to_video(silent_video, tts_segments, total_duration, out)

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
