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

# ── TTS 속도 단계 (기본 → 최고속) ───────────────────────────────────────────
# budget 초과 시 순서대로 시도. 더 빠른 속도로도 budget 안에 못 들어오면
# 마지막 속도(최고속)로 TTS를 생성하고, 슬라이드 시간을 TTS 길이에 맞춤.
TTS_RATE_STEPS = ["+28%", "+40%", "+52%", "+64%", "+75%"]
TTS_VOICE      = "ko-KR-InJoonNeural"
TTS_PITCH      = "-2Hz"
# 슬라이드 끝에 여유 시간 (TTS 끝나고 다음 슬라이드로 넘어가기 전 숨 쉬는 시간)
SLIDE_TAIL_SEC = 0.4

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

반드시 아래 JSON 형식으로만 응답하세요 (마크다운 코드블록 없이):
{
  "segments": [
    {
      "narration": "실제 읽을 나래이션 텍스트 (구어체, 40~65음절)",
      "keyword": "핵심 키워드 (3~6자)",
      "description": "키워드 부연설명 (15~25자)"
    }
  ]
}"""


# ── 틱톡 전용 나래이션 시스템 프롬프트 (쇼츠보다 세그먼트 많고 분량 넉넉) ────
NARRATION_SYSTEM_TIKTOK = """당신은 틱톡 금융 시황 채널의 나래이션 작가입니다.
블로그 본문 전체를 꼼꼼하게 읽고, 자연스러운 말하기 속도(초당 약 5~6음절)로
읽었을 때 전체 길이가 1분 30초~2분이 되도록 나래이션 스크립트를 작성합니다.

규칙:
- 나래이션은 자연스러운 구어체 (문어체, 이모지 금지)
- 블로그의 핵심 내용을 세그먼트별로 빠짐없이 담을 것 (단순 요약 금지)
- 세그먼트당 나래이션은 10~18초 분량 (약 55~100 음절)
- 총 8~12개 세그먼트
- 각 세그먼트에 키워드(3~8자)와 부연설명(15~30자) 포함
- 첫 번째 세그먼트: 강력한 훅 (인사말 없이 오늘의 핵심 수치/이슈로 시작)
- 중간 세그먼트: 지수 동향 → 핵심 뉴스 → 섹터 흐름 → 경제 지표 순으로 전개
- 마지막 세그먼트: 오늘의 핵심 포인트 정리 + 블로그 방문 / 팔로우 유도 CTA

반드시 아래 JSON 형식으로만 응답하세요 (마크다운 코드블록 없이):
{
  "segments": [
    {
      "narration": "실제 읽을 나래이션 텍스트 (구어체, 55~100음절)",
      "keyword": "핵심 키워드 (3~8자)",
      "description": "키워드 부연설명 (15~30자)"
    }
  ]
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
) -> list[dict]:
    """
    틱톡용 나래이션 스크립트 생성.
    쇼츠용보다 세그먼트 수가 많고(8~12개), 세그먼트당 분량도 충분히 길어서
    자연스러운 속도(+28%)로 읽어도 1분 30초~2분 분량이 나옵니다.
    """
    if not api_key:
        logger.warning("GEMINI_API_KEY 없음 — 틱톡 기본 스크립트 사용")
        return _fallback_script_tiktok(title, mode)

    mode_label = "전일 마감 리뷰" if mode == "morning" else "프리마켓 & 이슈"
    prompt = (
        f"블로그 제목: {title}\n"
        f"포스팅 모드: {mode_label}\n\n"
        f"블로그 본문 전문:\n{blog_content[:5000]}\n\n"
        "위 내용을 바탕으로 틱톡용 나래이션 스크립트를 JSON으로 작성해주세요.\n"
        "세그먼트는 8~12개, 자연스러운 속도로 읽으면 전체 1분 30초~2분이 되어야 합니다.\n"
        "블로그의 핵심 내용을 최대한 자세히 담아주세요."
    )

    for model in [GEMINI_MODEL, GEMINI_FALLBACK_MODEL]:
        url     = f"{GEMINI_BASE_URL}{model}:generateContent?key={api_key}"
        payload = {
            "system_instruction": {"parts": [{"text": NARRATION_SYSTEM_TIKTOK}]},
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {
                "temperature": 0.7,
                "maxOutputTokens": 4096,
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

                if "```" in raw:
                    for part in raw.split("```"):
                        part = part.strip()
                        if part.startswith("json"):
                            part = part[4:].strip()
                        try:
                            parsed = json.loads(part)
                            segs = parsed.get("segments", [])
                            if segs:
                                logger.info(f"틱톡 나래이션 스크립트 생성 완료: {len(segs)}개")
                                return segs
                        except json.JSONDecodeError:
                            continue

                parsed = json.loads(raw)
                segs   = parsed.get("segments", [])
                if segs:
                    logger.info(f"틱톡 나래이션 스크립트 생성 완료: {len(segs)}개")
                    return segs

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
    return _fallback_script_tiktok(title, mode)


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
    """틱톡용 API 실패 시 기본 스크립트 (8개 세그먼트, 1분 30초+ 분량)."""
    clean_title = _strip_emoji(title)
    if mode == "morning":
        return [
            {"narration": f"오늘 미국 증시 마감 결과를 정리해드립니다. {clean_title[:25]}",
             "keyword": "마감 결과", "description": "미국 전일 증시 마감 총정리"},
            {"narration": "S&P500, 나스닥, 다우존스 주요 지수의 등락 현황을 살펴보겠습니다. 어제 시장의 전반적인 분위기와 거래량 변화도 함께 짚어보겠습니다.",
             "keyword": "지수 동향", "description": "S&P500·나스닥·다우 등락률"},
            {"narration": "오늘 시장을 움직인 핵심 뉴스를 전해드립니다. 연준의 발언과 경제 지표 결과가 시장 심리에 어떤 영향을 미쳤는지 분석했습니다.",
             "keyword": "핵심 뉴스", "description": "시장 움직임의 핵심 원인"},
            {"narration": "기술주, 에너지, 금융, 헬스케어 등 섹터별 마감 흐름도 정리해드립니다. 어느 섹터가 강세였고 어디가 약세였는지 한눈에 보여드리겠습니다.",
             "keyword": "섹터 분석", "description": "업종별 강세·약세 현황"},
            {"narration": "공포탐욕지수와 투자자 심리 현황도 확인했습니다. 현재 시장이 과매수 구간인지 과매도 구간인지 판단하는 데 도움이 될 것입니다.",
             "keyword": "투자 심리", "description": "공포·탐욕지수 및 시장 심리"},
            {"narration": "오늘 발표된 주요 경제 지표 결과를 요약해드립니다. 고용 지표, 소비자물가지수 등 매크로 데이터가 시장에 미친 영향을 설명드리겠습니다.",
             "keyword": "경제 지표", "description": "발표된 주요 경제 데이터"},
            {"narration": "내일 미국 시장에서 주목해야 할 포인트를 미리 정리해드립니다. 예정된 경제 지표 발표와 기업 실적 발표 일정도 함께 안내드립니다.",
             "keyword": "내일 주목", "description": "다음 거래일 핵심 이벤트"},
            {"narration": "오늘 분석의 핵심을 한 줄로 정리하면, 시장은 여전히 변수가 많습니다. 더 자세한 분석은 블로그 seedsup.tistory.com에서 확인하시고 팔로우도 부탁드립니다.",
             "keyword": "블로그 방문", "description": "seedsup.tistory.com"},
        ]
    else:
        return [
            {"narration": f"오늘 밤 미국 증시 개장 전 꼭 알아야 할 이슈를 정리했습니다. {clean_title[:20]}",
             "keyword": "프리마켓", "description": "미국 장 개장 전 핵심 이슈"},
            {"narration": "전일 미국 정규장 마감 결과부터 간략히 짚어보겠습니다. 어제 마감 이후 발생한 애프터마켓 주요 움직임도 함께 살펴보겠습니다.",
             "keyword": "전일 마감", "description": "정규장 마감 및 애프터마켓"},
            {"narration": "오늘 밤 개장 전 현재 선물 시장 동향을 전해드립니다. S&P500, 나스닥 선물이 어느 방향을 가리키는지 확인해보겠습니다.",
             "keyword": "선물 동향", "description": "S&P500·나스닥 선물 방향"},
            {"narration": "오늘 밤 미국 시간으로 발표되는 경제 지표 일정을 안내드립니다. 각 지표의 시장 예상치와 이전 수치를 함께 알려드리겠습니다.",
             "keyword": "지표 발표", "description": "오늘 밤 예정된 경제 데이터"},
            {"narration": "오늘 장 마감 후 또는 개장 전 실적을 발표하는 기업들을 정리했습니다. 시장 예상치 대비 어떤 결과가 나올지 투자자들이 주목하고 있습니다.",
             "keyword": "실적 발표", "description": "오늘 예정된 기업 실적"},
            {"narration": "연준 인사 발언과 지정학적 리스크 등 오늘 밤 시장에 영향을 줄 수 있는 변수들을 종합했습니다.",
             "keyword": "시장 변수", "description": "오늘 밤 주요 리스크 요인"},
            {"narration": "오늘 밤 강세와 약세 두 가지 시나리오를 분석했습니다. 각각 어떤 조건이 충족되어야 하는지, 투자자 입장에서 어떻게 대응해야 하는지 설명해드립니다.",
             "keyword": "시나리오", "description": "강세 vs 약세 대응 전략"},
            {"narration": "오늘 분석의 핵심 포인트를 정리해드렸습니다. 더 자세한 내용은 블로그 seedsup.tistory.com에서 확인하시고 팔로우도 부탁드립니다.",
             "keyword": "블로그 방문", "description": "seedsup.tistory.com"},
        ]


# ═══════════════════════════════════════════════════════════════════════════════
# TTS: 속도 자동 조정 (핵심 변경 — v7)
# ═══════════════════════════════════════════════════════════════════════════════

async def _tts_async(text: str, path: str, rate: str):
    """지정된 속도로 TTS 생성."""
    import edge_tts
    comm = edge_tts.Communicate(text=text, voice=TTS_VOICE, rate=rate, pitch=TTS_PITCH)
    await comm.save(path)


def _generate_tts_with_rate(text: str, path: str, rate: str) -> bool:
    """지정 속도로 TTS 파일 생성. 성공 여부 반환."""
    try:
        asyncio.run(_tts_async(text, path, rate))
        ok = Path(path).exists() and Path(path).stat().st_size > 500
        if not ok:
            logger.warning(f"TTS 파일 생성 실패 또는 너무 작음: {path}")
        return ok
    except ImportError:
        logger.error("edge-tts 미설치 — pip install edge-tts 필요")
        return False
    except Exception as e:
        logger.error(f"TTS 생성 오류 (rate={rate}): {e}")
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
) -> Image.Image:
    """
    슬라이드 이미지 생성:
    - 상단: 진행 바 + 슬라이드 번호 + (속도 가속 배지, rate가 기본이 아닐 때만)
    - 중앙: 키워드 강조 박스 (크고 눈에 띄게)
    - 키워드 아래: 부연설명 텍스트 (반투명 배경)
    - 하단: 나래이션 자막 (반투명 배경)
    - 최하단: 워터마크
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
    draw.text((40 + 20, 30 + 11), badge, font=f_badge, fill=kw_fg)

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
        draw.text((W - 40 - sw + 18, 30 + 9), spd_txt, font=f_badge, fill=(255, 255, 255))

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
        draw.text((hx + 24, hy + 13), banner_txt, font=f_badge, fill=(20, 20, 20))

    # ── 키워드 강조 박스 (화면 중앙) ─────────────────────────────────────────
    kw_clean = _strip_emoji(keyword)
    kw_bb    = draw.textbbox((0, 0), kw_clean, font=f_keyword)
    kw_tw    = kw_bb[2] - kw_bb[0]
    kw_th    = kw_bb[3] - kw_bb[1]
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
    # 키워드 텍스트
    draw.text((CX - kw_tw // 2, kw_y + pad_y), kw_clean, font=f_keyword, fill=kw_fg)

    # 하단 장식 라인
    line_y = kw_y + kw_box_h + 18
    draw.rectangle([(CX - 130, line_y), (CX + 130, line_y + 6)], fill=(*highlight, 220))

    # ── 부연설명 (키워드 아래) ────────────────────────────────────────────────
    desc_clean  = _strip_emoji(description)
    desc_lines  = _pixel_wrap(desc_clean, f_desc, WRAP_PX - 80)
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

    # ── 나래이션 자막 (최하단) ───────────────────────────────────────────────
    narr_clean  = _strip_emoji(narration)
    narr_lines  = _pixel_wrap(narr_clean, f_narr, WRAP_PX - 40)[:4]
    narr_line_h = 55
    narr_total  = len(narr_lines) * narr_line_h + 36
    narr_y      = H - narr_total - 54

    narr_bg = Image.new("RGBA", (W, narr_total + 16), (0, 0, 0, 190))
    img_rgba = img.convert("RGBA")
    img_rgba.paste(narr_bg, (0, narr_y - 8), narr_bg)
    img  = img_rgba.convert("RGB")
    draw = ImageDraw.Draw(img)

    ny = narr_y + 6
    for line in narr_lines:
        lb = draw.textbbox((0, 0), line, font=f_narr)
        lw = lb[2] - lb[0]
        _draw_outlined(draw, (CX - lw // 2, ny), line, f_narr, (255, 255, 255), ow=2)
        ny += narr_line_h

    # ── 워터마크 ─────────────────────────────────────────────────────────────
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
    BGM 없이 TTS 나래이션만, 각 TTS는 sldie 시작 후 0.15초 딜레이.
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
        틱톡 수익화 조건(1분 이상) 충족 전용 영상 생성.

        쇼츠(generate)와의 차이:
          - MAX_VIDEO_SEC 제한 없음 — 세그먼트 수/TTS 길이에 따라 자연스럽게 결정
          - 고정 속도 +28% (속도 자동 조정 없음, 자연스러운 말하기 속도 유지)
          - 모든 TTS가 끝난 후 영상 종료 (슬라이드 = TTS 길이 + SLIDE_TAIL_SEC)
          - 8~12개 세그먼트 스크립트로 1분 30초~2분 내외 분량 생성
        """
        theme = THEMES.get(mode, THEMES["morning"])
        kws   = bg_keywords or PEXELS_KEYWORDS.get(mode, PEXELS_KEYWORDS["morning"])
        out   = os.path.join(self.output_dir, filename)

        # 1. 틱톡 전용 나래이션 스크립트 생성 (8~12개 세그먼트)
        logger.info("틱톡 나래이션 스크립트 생성 중 (Gemini API)...")
        if blog_content and self.gemini_key:
            narration_segments = generate_narration_script_tiktok(
                blog_content, blog_title, mode, self.gemini_key
            )
        else:
            narration_segments = self._convert_script_to_narration(script, mode, blog_title)

        if not narration_segments:
            narration_segments = _fallback_script_tiktok(blog_title, mode)

        logger.info(f"틱톡 나래이션 세그먼트: {len(narration_segments)}개")

        with tempfile.TemporaryDirectory(prefix="tiktok_v8_") as tmp_s:
            tmp = Path(tmp_s)

            # 2. 배경 이미지 확보 (쇼츠와 동일 로직)
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
                    logger.info("틱톡 배경: 티스토리 썸네일 로드 성공")
                except Exception as e:
                    logger.warning(f"썸네일 로드 실패: {e}")

            if not bg_ok:
                bg_ok = _download_bg_pexels(kws, bg_path, self.pexels_key)
            if not bg_ok:
                import hashlib
                seed  = int(hashlib.md5(f"tiktok{mode}{filename}".encode()).hexdigest()[:8], 16)
                bg_ok = _download_bg_picsum(bg_path, seed % 1000)

            bg_img = _prepare_bg(bg_path if bg_ok else None, theme["overlay"], mode)

            # 3. 각 세그먼트: 고정 속도 +28%, 시간 제한 없이 모든 세그먼트 처리
            slide_clips  = []
            tts_segments = []
            current_time = 0.0
            total        = len(narration_segments)
            tiktok_rate  = TTS_RATE_STEPS[0]  # +28% 고정

            for i, seg in enumerate(narration_segments, 1):
                narration   = _strip_emoji(seg.get("narration", ""))
                keyword     = _strip_emoji(seg.get("keyword", "분석"))
                description = _strip_emoji(seg.get("description", ""))

                is_hook = (i == 1)
                is_cta  = (i == total)

                logger.info(
                    f"[틱톡] 슬라이드 {i}/{total}: [{keyword}] "
                    f"누적={current_time:.2f}s | {narration[:30]}..."
                )

                # TTS 생성 — 고정 속도, 시간 제한 없음
                tts_path = str(tmp / f"tts_{i:02d}.mp3")
                tts_ok   = _generate_tts_with_rate(narration, tts_path, tiktok_rate)

                if tts_ok:
                    tts_dur = _audio_duration(tts_path)
                    if tts_dur <= 0:
                        logger.warning(f"[틱톡] 슬라이드 {i} TTS 길이 0 — 4.0초 fallback")
                        tts_dur = 4.0
                        tts_ok  = False
                else:
                    tts_dur = 4.0
                    logger.warning(f"[틱톡] 슬라이드 {i} TTS 생성 실패 — 4.0초 fallback")

                # 슬라이드 표시 시간 = TTS 길이 + tail (시간 제한 없이 그대로)
                slide_dur = tts_dur + SLIDE_TAIL_SEC

                # 슬라이드 이미지 생성 (속도 배지 없음 — 기본 속도이므로)
                slide_img = _make_slide(
                    narration, keyword, description,
                    theme, i, total, bg_img,
                    is_hook, is_cta,
                    tts_rate=tiktok_rate,  # 항상 기본 속도 → 배지 미표시
                )
                img_path  = str(tmp / f"slide_{i:02d}.png")
                slide_img.save(img_path, "PNG", optimize=False)

                # 이미지 → MP4 클립
                clip_path = str(tmp / f"clip_{i:02d}.mp4")
                _image_to_clip(img_path, slide_dur, clip_path)
                slide_clips.append(clip_path)

                if tts_ok:
                    tts_segments.append({
                        "path":  tts_path,
                        "start": current_time + 0.15,
                    })

                logger.info(
                    f"  → tts={tts_dur:.2f}s, slide={slide_dur:.2f}s, "
                    f"누적={current_time + slide_dur:.2f}s"
                )
                current_time += slide_dur

            if not slide_clips:
                raise RuntimeError("[틱톡] 생성된 슬라이드 클립이 없습니다.")

            total_duration = current_time
            logger.info(
                f"[틱톡] 총 영상 길이: {total_duration:.2f}초 "
                f"({len(slide_clips)}/{total} 슬라이드) "
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
