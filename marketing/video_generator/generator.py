"""
YouTube Shorts 영상 생성기 v6
==============================================
변경사항 (v6):
  - 블로그 본문 기반 59초 이하 나래이션 스크립트 자동 생성 (Gemini API)
  - 나래이션 시간과 동기화된 키워드 + 부연설명 자막 오버레이
  - BGM 완전 제거 (TTS 나래이션 단독)
  - TTS 속도 빠르게 (+28%), 목소리 확실히 출력
  - 배경 이미지 투명도 절반으로 낮춤 (배경 잘 보이도록)
  - 키워드 강조 + 부연설명 텍스트 오버레이 추가

TTS: edge-tts ko-KR-InJoonNeural (젊은 남성)
규격: 1080×1920, 30fps, H.264
최대 영상 길이: 59초
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
from struct import pack

import requests
from PIL import Image, ImageDraw, ImageFilter, ImageFont

logger = logging.getLogger(__name__)

# ── 규격 ─────────────────────────────────────────────────────────────────────
VIDEO_W, VIDEO_H = 1080, 1920
OUTPUT_DIR       = "videos"
MAX_VIDEO_SEC    = 58.0   # 59초 이하 보장

# ── TTS 설정 (빠른 속도) ─────────────────────────────────────────────────────
TTS_VOICE = "ko-KR-InJoonNeural"
TTS_RATE  = "+28%"   # 빠른 속도 (기존 +18% → +28%)
TTS_PITCH = "-2Hz"

# ── 시스템 폰트 경로 ─────────────────────────────────────────────────────────
_FONT_BOLD    = "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc"
_FONT_BLACK   = "/usr/share/fonts/opentype/noto/NotoSansCJK-Black.ttc"
_FONT_REGULAR = "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"

# ── 배경 오버레이 (투명도 절반으로 낮춤) ────────────────────────────────────
THEMES = {
    "morning": {
        "overlay":    (8, 15, 35, 70),       # 기존 140 → 70 (절반)
        "accent":     (56, 189, 248),
        "highlight":  (254, 211, 48),
        "keyword_bg": (56, 189, 248),
        "keyword_fg": (8, 15, 35),
        "desc_bg":    (0, 0, 0, 160),
        "title_c":    (255, 255, 255),
        "progress":   (56, 189, 248),
    },
    "evening": {
        "overlay":    (18, 5, 40, 75),        # 기존 145 → 75 (절반)
        "accent":     (167, 139, 250),
        "highlight":  (251, 191, 36),
        "keyword_bg": (167, 139, 250),
        "keyword_fg": (18, 5, 40),
        "desc_bg":    (0, 0, 0, 160),
        "title_c":    (255, 255, 255),
        "progress":   (167, 139, 250),
    },
}

# ── Pexels 키워드 ────────────────────────────────────────────────────────────
PEXELS_KEYWORDS = {
    "morning": ["wall street morning", "stock market finance", "financial district dawn"],
    "evening": ["city night finance", "new york night skyline", "stock exchange night"],
}

# ── 이모지 제거 (한글 보존) ──────────────────────────────────────────────────
# 주의: \U000024C2-\U0001F251 범위는 한글(AC00-D7FF)을 포함하므로 사용 금지
_EMOJI_RE = re.compile(
    "["
    "\U0001F600-\U0001F64F"   # 이모티콘
    "\U0001F300-\U0001F5FF"   # 기호/픽토그램
    "\U0001F680-\U0001F6FF"   # 교통/지도
    "\U0001F1E0-\U0001F1FF"   # 국기
    "\U0001F900-\U0001F9FF"   # 추가 이모지
    "\U0001FA00-\U0001FA6F"   # 추가 이모지
    "\U0001FA70-\U0001FAFF"   # 추가 이모지
    "\U00002702-\U00002705"   # 가위 등 (딩뱃 일부)
    "\U00002708-\U0000270D"   # 비행기/손 등
    "\U0000270F"
    "\U00002712"
    "\U00002714"
    "\U00002716"
    "\U0000271D"
    "\U00002721"
    "\U00002728"
    "\U00002733-\U00002734"
    "\U00002744"
    "\U00002747"
    "\U0000274C"
    "\U0000274E"
    "\U00002753-\U00002755"
    "\U00002757"
    "\U00002763-\U00002764"
    "\U00002795-\U00002797"
    "\U000027A1"
    "\U000027B0"
    "\U000027BF"
    "]+",
    flags=re.UNICODE,
)

def _strip_emoji(text: str) -> str:
    return _EMOJI_RE.sub("", text).strip()


# ═══════════════════════════════════════════════════════════════════════════════
# Gemini API: 나래이션 스크립트 생성
# ═══════════════════════════════════════════════════════════════════════════════

GEMINI_MODEL   = "gemini-2.5-flash-lite"
GEMINI_API_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    f"{GEMINI_MODEL}:generateContent"
)

NARRATION_SYSTEM = """당신은 유튜브 쇼츠 나래이션 작가입니다.
블로그 본문을 읽고, 핵심 내용을 요약한 나래이션 스크립트를 작성합니다.

규칙:
- 전체 나래이션을 빠르게 읽으면 반드시 55초 이하여야 합니다
- 자연스러운 구어체로 작성 (문어체, 이모지 금지)
- 총 5~7개 세그먼트
- 각 세그먼트: 나래이션(읽을 텍스트) + 화면에 표시할 요약 문장
- 나래이션은 8~12초 분량 (약 45~70음절)
- 화면 요약 문장은 20~35자, 핵심 수치/정보 포함
- 첫 번째 세그먼트: 강렬한 훅 (시청자 주의 끌기)
- 마지막 세그먼트: 블로그 방문 유도 CTA

반드시 아래 JSON 형식으로만 응답하세요 (마크다운 코드블록 없이):
{
  "segments": [
    {
      "narration": "실제 읽을 나래이션 텍스트 (구어체, 45~70음절, 이모지 없음)",
      "headline": "화면 상단 굵은 제목 (10~15자, 핵심 수치 포함)",
      "summary": "화면 중앙 요약 문장 (20~35자, 본문 핵심 내용)"
    }
  ]
}"""


def generate_narration_script(blog_content: str, title: str, mode: str, api_key: str) -> list[dict]:
    """
    블로그 본문을 기반으로 나래이션 스크립트 생성.
    Returns list of {narration, keyword, description} dicts.
    """
    if not api_key:
        logger.warning("GEMINI_API_KEY 없음 — 기본 스크립트 사용")
        return _fallback_script(title, mode)

    mode_label = "전일 마감 리뷰" if mode == "morning" else "프리마켓 & 이슈"
    prompt = (
        f"블로그 제목: {title}\n"
        f"포스팅 모드: {mode_label}\n\n"
        f"블로그 본문:\n{blog_content[:3000]}\n\n"
        "위 내용을 바탕으로 유튜브 쇼츠용 나래이션 스크립트를 JSON으로 작성해주세요.\n"
        "전체 나래이션을 빠르게 읽으면 55초 이하가 되어야 합니다."
    )

    url     = f"{GEMINI_API_URL}?key={api_key}"
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

            # JSON 파싱
            if "```" in raw:
                for part in raw.split("```"):
                    part = part.strip()
                    if part.startswith("json"):
                        part = part[4:].strip()
                    try:
                        parsed = json.loads(part)
                        return parsed.get("segments", [])
                    except json.JSONDecodeError:
                        continue

            parsed = json.loads(raw)
            segs   = parsed.get("segments", [])
            if segs:
                logger.info(f"나래이션 스크립트 생성 완료: {len(segs)}개 세그먼트")
                return segs

        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 504):
                wait = (30 if e.code == 429 else 10) * (2 ** (attempt - 1))
                logger.warning(f"Gemini 나래이션 API {e.code} — {wait}초 후 재시도...")
                time.sleep(wait)
            else:
                logger.warning(f"Gemini 나래이션 API 오류 {e.code}")
                break
        except Exception as e:
            logger.warning(f"나래이션 스크립트 생성 실패 (시도 {attempt}): {e}")
            if attempt < 3:
                time.sleep(10)

    logger.warning("나래이션 생성 실패 — 기본 스크립트 사용")
    return _fallback_script(title, mode)


def _fallback_script(title: str, mode: str) -> list[dict]:
    """API 실패 시 기본 스크립트 (headline/summary 포함)."""
    clean_title = _strip_emoji(title)
    if mode == "morning":
        return [
            {"narration": f"안녕하세요! 오늘의 미국 증시 마감 분석입니다.",
             "headline": "미국 증시 마감", "summary": clean_title[:35]},
            {"narration": "주요 지수 흐름과 핵심 이슈를 빠르게 정리해드립니다.",
             "headline": "지수 동향", "summary": "S&P500, 나스닥, 다우 등락 현황"},
            {"narration": "오늘 시장에 영향을 준 경제 지표와 뉴스를 살펴보겠습니다.",
             "headline": "경제 지표", "summary": "발표된 주요 경제 데이터 분석"},
            {"narration": "더 자세한 분석은 블로그에서 확인하세요. 구독과 좋아요 부탁드립니다!",
             "headline": "블로그 방문", "summary": "seedsup.tistory.com"},
        ]
    else:
        return [
            {"narration": "오늘 밤 미국 증시 개장 전 핵심 이슈를 정리했습니다.",
             "headline": "프리마켓 분석", "summary": clean_title[:35]},
            {"narration": "오늘 발표 예정인 경제 지표와 실적 발표를 확인해보겠습니다.",
             "headline": "경제 지표", "summary": "오늘 밤 주요 발표 일정"},
            {"narration": "프리마켓 분위기와 오늘 밤 시장 시나리오를 분석해드립니다.",
             "headline": "시장 전망", "summary": "강세 vs 약세 시나리오 분석"},
            {"narration": "전체 분석은 블로그를 방문해주세요. 구독과 좋아요 감사합니다!",
             "headline": "블로그 방문", "summary": "seedsup.tistory.com"},
        ]


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
            current = word if _w(word) <= max_px else word
    if current:
        lines.append(current)
    return lines or [text]


# ═══════════════════════════════════════════════════════════════════════════════
# 배경 이미지 처리 (투명도 절반으로 낮춤)
# ═══════════════════════════════════════════════════════════════════════════════

def _prepare_bg(path, overlay_color: tuple, mode: str) -> Image.Image:
    """배경 이미지 → 1080×1920 RGB (낮은 오버레이로 배경 잘 보이게)."""
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
            bg = bg.filter(ImageFilter.GaussianBlur(radius=2))  # 블러 약화
        except Exception as e:
            logger.warning(f"배경 처리 실패: {e}")
            bg = _make_gradient_bg(overlay_color[:3], mode)
    else:
        bg = _make_gradient_bg(overlay_color[:3], mode)

    # 오버레이 (낮은 투명도)
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
# TTS (빠른 속도)
# ═══════════════════════════════════════════════════════════════════════════════

async def _tts_async(text: str, path: str):
    import edge_tts
    comm = edge_tts.Communicate(text=text, voice=TTS_VOICE, rate=TTS_RATE, pitch=TTS_PITCH)
    await comm.save(path)


def _sanitize_tts_text(text: str) -> str:
    """TTS 전달 전 텍스트 정제: 이모지 제거, 특수문자 정리, 최소 길이 보장."""
    text = _strip_emoji(text).strip()
    # 괄호류 제거 (TTS 오류 유발 가능)
    text = re.sub(r"[<>【】\[\]『』「」]", "", text)
    # 연속 공백 정리
    text = re.sub(r"\s+", " ", text).strip()
    # 최소 10자 미만이면 패딩 (TTS가 너무 짧은 텍스트를 거부함)
    if len(text) < 10:
        text = text + ". 자세한 내용은 블로그를 확인해주세요."
    return text


def _generate_tts(text: str, path: str, max_retries: int = 3) -> bool:
    """TTS 생성. 실패 시 최대 max_retries회 재시도."""
    clean = _sanitize_tts_text(text)
    if not clean:
        logger.error("TTS 텍스트가 비어있음")
        return False

    for attempt in range(1, max_retries + 1):
        try:
            asyncio.run(_tts_async(clean, path))
            if Path(path).exists() and Path(path).stat().st_size > 1000:
                return True
            logger.warning(f"TTS 파일 크기 불충분 (시도 {attempt}/{max_retries})")
        except ImportError:
            logger.error("edge-tts 미설치 — pip install edge-tts 필요")
            return False
        except Exception as e:
            logger.warning(f"TTS 생성 오류 (시도 {attempt}/{max_retries}): {e}")

        if attempt < max_retries:
            time.sleep(2 * attempt)

    logger.error(f"TTS 최대 재시도 초과: {clean[:30]}...")
    return False


def _audio_duration(path: str) -> float:
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "json", path],
            capture_output=True, text=True, check=True,
        )
        return float(json.loads(r.stdout)["format"]["duration"])
    except Exception:
        return 0.0


# ═══════════════════════════════════════════════════════════════════════════════
# 슬라이드 이미지 생성 (키워드 강조 + 부연설명 텍스트)
# ═══════════════════════════════════════════════════════════════════════════════

def _draw_outlined(draw, pos, text, font, fill, outline=(0, 0, 0), ow=3):
    x, y = pos
    for dx, dy in [(-ow, 0), (ow, 0), (0, -ow), (0, ow),
                   (-ow, -ow), (ow, -ow), (-ow, ow), (ow, ow)]:
        draw.text((x + dx, y + dy), text, font=font, fill=(*outline, 220))
    draw.text((x, y), text, font=font, fill=fill)


def _draw_text_centered(draw, cx, y, text, font, fill, outline=(0, 0, 0), ow=3) -> int:
    bbox = draw.textbbox((0, 0), text, font=font)
    w    = bbox[2] - bbox[0]
    h    = bbox[3] - bbox[1]
    _draw_outlined(draw, (cx - w // 2, y), text, font, fill, outline, ow)
    return h


def _make_slide(
    narration: str,
    headline: str,
    summary: str,
    theme: dict,
    slide_num: int,
    total: int,
    bg: Image.Image,
    is_hook: bool,
    is_cta: bool,
) -> Image.Image:
    """
    슬라이드 이미지 생성 (v7 — 요약 중심 레이아웃):
    - 상단: 진행 바 + 슬라이드 번호
    - 중앙 상부: 굵은 헤드라인 (핵심 수치/포인트)
    - 중앙: 요약 문장 (본문 내용, 읽기 편한 크기)
    - 하단: 나래이션 자막 (반투명 배경)
    - 최하단: 워터마크
    """
    W, H   = VIDEO_W, VIDEO_H
    img    = bg.copy()
    draw   = ImageDraw.Draw(img)

    accent    = theme["accent"]
    highlight = theme["highlight"]
    title_c   = theme["title_c"]
    CX        = W // 2
    WRAP_PX   = W - 100

    # 폰트
    f_badge    = _load_font(34, bold=True)
    f_headline = _load_font(88, bold=True)   # 헤드라인: 크고 굵게
    f_summary  = _load_font(54, bold=False)  # 요약: 읽기 편한 크기
    f_narr     = _load_font(42, bold=False)  # 자막: 하단
    f_wm       = _load_font(30, bold=False)

    # ── 상단 진행 바 ─────────────────────────────────────────────────────────
    bar_w = int(W * slide_num / total)
    draw.rectangle([(0, 0), (W, 10)], fill=(255, 255, 255, 40))
    draw.rectangle([(0, 0), (bar_w, 10)], fill=(*accent, 255))

    # ── 슬라이드 번호 뱃지 ──────────────────────────────────────────────────
    badge = f"{slide_num} / {total}"
    bb    = draw.textbbox((0, 0), badge, font=f_badge)
    bw, bh = bb[2] - bb[0] + 36, bb[3] - bb[1] + 20
    draw.rounded_rectangle([(40, 28), (40 + bw, 28 + bh)],
                            radius=bh // 2, fill=(*accent, 200))
    draw.text((40 + 18, 28 + 10), badge, font=f_badge, fill=(10, 10, 30))

    # ── 훅/CTA 배너 ──────────────────────────────────────────────────────────
    banner_txt = ("오늘의 핵심 분석" if is_hook else
                  "전체 분석 보기" if is_cta else None)
    if banner_txt:
        hb  = draw.textbbox((0, 0), banner_txt, font=f_badge)
        hw  = hb[2] - hb[0] + 48
        hh  = hb[3] - hb[1] + 24
        hx  = CX - hw // 2
        hy  = 28
        draw.rounded_rectangle([(hx, hy), (hx + hw, hy + hh)],
                                radius=hh // 2, fill=(*highlight, 240))
        draw.text((hx + 24, hy + 12), banner_txt, font=f_badge, fill=(20, 20, 20))

    # ── 헤드라인 (화면 40% 지점) ─────────────────────────────────────────────
    hl_clean  = _strip_emoji(headline)
    hl_lines  = _pixel_wrap(hl_clean, f_headline, WRAP_PX)[:2]
    hl_line_h = 100
    hl_total  = len(hl_lines) * hl_line_h
    hl_y      = int(H * 0.32) - hl_total // 2

    # 헤드라인 배경 (강조 바)
    bar_h = hl_total + 40
    bar_x = 60
    bar_w2 = W - 120
    hl_bg = Image.new("RGBA", (bar_w2, bar_h), (*accent, 220))
    img_rgba = img.convert("RGBA")
    img_rgba.paste(hl_bg, (bar_x, hl_y - 20), hl_bg)
    img  = img_rgba.convert("RGB")
    draw = ImageDraw.Draw(img)

    for line in hl_lines:
        lb = draw.textbbox((0, 0), line, font=f_headline)
        lw = lb[2] - lb[0]
        draw.text((CX - lw // 2, hl_y), line, font=f_headline, fill=(10, 10, 30))
        hl_y += hl_line_h

    # ── 구분선 ───────────────────────────────────────────────────────────────
    sep_y = hl_y + 24
    draw.rectangle([(CX - 160, sep_y), (CX + 160, sep_y + 4)],
                   fill=(*highlight, 200))

    # ── 요약 문장 (구분선 아래) ───────────────────────────────────────────────
    sm_clean = _strip_emoji(summary)
    sm_lines = _pixel_wrap(sm_clean, f_summary, WRAP_PX - 60)[:3]
    sm_line_h = 68
    sm_total  = len(sm_lines) * sm_line_h + 36
    sm_y      = sep_y + 28

    # 요약 반투명 배경
    sm_bg = Image.new("RGBA", (W - 80, sm_total), (0, 0, 0, 170))
    img_rgba = img.convert("RGBA")
    img_rgba.paste(sm_bg, (40, sm_y - 12), sm_bg)
    img  = img_rgba.convert("RGB")
    draw = ImageDraw.Draw(img)

    for line in sm_lines:
        lb = draw.textbbox((0, 0), line, font=f_summary)
        lw = lb[2] - lb[0]
        _draw_outlined(draw, (CX - lw // 2, sm_y), line, f_summary,
                       (255, 255, 255), ow=2)
        sm_y += sm_line_h

    # ── 나래이션 자막 (하단 고정) ─────────────────────────────────────────────
    narr_clean = _strip_emoji(narration)
    narr_lines = _pixel_wrap(narr_clean, f_narr, WRAP_PX - 20)[:4]
    narr_line_h = 54
    narr_total  = len(narr_lines) * narr_line_h + 40
    narr_y      = H - narr_total - 56

    narr_bg = Image.new("RGBA", (W, narr_total + 10), (0, 0, 0, 195))
    img_rgba = img.convert("RGBA")
    img_rgba.paste(narr_bg, (0, narr_y - 10), narr_bg)
    img  = img_rgba.convert("RGB")
    draw = ImageDraw.Draw(img)

    ny = narr_y + 8
    for line in narr_lines:
        lb = draw.textbbox((0, 0), line, font=f_narr)
        lw = lb[2] - lb[0]
        _draw_outlined(draw, (CX - lw // 2, ny), line, f_narr,
                       (255, 255, 220), ow=2)
        ny += narr_line_h

    # ── 워터마크 ─────────────────────────────────────────────────────────────
    wm  = "seedsup.tistory.com"
    wbb = draw.textbbox((0, 0), wm, font=f_wm)
    ww  = wbb[2] - wbb[0]
    draw.text((CX - ww // 2, H - 38), wm, font=f_wm, fill=(*accent, 150))

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
    """PNG → MP4 클립 (페이드 없이 깔끔하게)."""
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
    BGM 없이 TTS 나래이션만 사용.
    각 TTS가 반드시 출력되도록 처리.
    """
    if not tts_segments:
        # TTS 없으면 무음 영상 출력
        _run(["ffmpeg", "-y", "-i", video, "-c:v", "copy", "-an", out])
        logger.warning("TTS 세그먼트 없음 — 무음 영상 출력")
        return

    # filter_complex: 각 TTS를 지연 후 믹싱
    inputs   = ["-i", video]
    fc_parts = []
    tts_labels = []

    for i, seg in enumerate(tts_segments):
        inputs += ["-i", seg["path"]]
        delay   = int(seg["start"] * 1000)
        label   = f"[d{i}]"
        fc_parts.append(f"[{i+1}:a]adelay={delay}|{delay},apad=whole_dur={total_dur}{label}")
        tts_labels.append(label)

    n = len(tts_labels)
    if n == 1:
        audio_out = tts_labels[0]
        # apad으로 이미 처리됨
        fc_parts[-1] = fc_parts[-1].replace(f"{tts_labels[0]}", f"[aout]")
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
        """
        숏폼 영상 생성.

        script: ContentAdapter에서 생성된 youtube_script (fallback용)
        blog_content: 블로그 본문 전문 (나래이션 생성에 사용)
        blog_title: 블로그 제목
        """
        theme = THEMES.get(mode, THEMES["morning"])
        kws   = bg_keywords or PEXELS_KEYWORDS.get(mode, PEXELS_KEYWORDS["morning"])
        out   = os.path.join(self.output_dir, filename)

        # 1. 나래이션 스크립트 생성 (블로그 본문 기반)
        logger.info("나래이션 스크립트 생성 중 (Gemini API)...")
        if blog_content and self.gemini_key:
            narration_segments = generate_narration_script(
                blog_content, blog_title, mode, self.gemini_key
            )
        else:
            # blog_content 없으면 기존 script에서 변환
            narration_segments = self._convert_script_to_narration(script, mode, blog_title)

        if not narration_segments:
            narration_segments = _fallback_script(blog_title, mode)

        logger.info(f"나래이션 세그먼트: {len(narration_segments)}개")

        with tempfile.TemporaryDirectory(prefix="shorts_v6_") as tmp_s:
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

            # 3. TTS 생성 및 슬라이드 처리
            slide_clips   = []
            tts_segments  = []
            current_time  = 0.0
            total         = len(narration_segments)
            total_tts_dur = 0.0

            for i, seg in enumerate(narration_segments, 1):
                narration = _strip_emoji(seg.get("narration", ""))
                headline  = _strip_emoji(seg.get("headline", seg.get("keyword", "분석")))
                summary   = _strip_emoji(seg.get("summary", seg.get("description", "")))

                is_hook = (i == 1)
                is_cta  = (i == total)

                logger.info(f"슬라이드 {i}/{total}: [{headline}] {narration[:30]}...")

                # TTS 생성 (나래이션)
                tts_path = str(tmp / f"tts_{i:02d}.mp3")
                tts_ok   = _generate_tts(narration, tts_path)

                if tts_ok:
                    tts_dur = _audio_duration(tts_path)
                    if tts_dur < 0.5:   # ffprobe가 0을 반환하는 경우 방어
                        tts_dur = 4.0
                else:
                    tts_dur = 5.0  # fallback: TTS 실패 시 기본 5초 (4초에서 상향)
                    logger.warning(f"슬라이드 {i} TTS 실패 — {tts_dur}초 무음으로 대체")

                # 59초 초과 방지
                remaining = MAX_VIDEO_SEC - current_time
                if remaining < 2.0:
                    logger.info(f"59초 한계 도달 — {i-1}개 슬라이드로 종료")
                    break

                slide_dur = min(tts_dur + 0.5, remaining)
                total_tts_dur += tts_dur

                # 슬라이드 이미지 생성
                slide_img = _make_slide(
                    narration, headline, summary,
                    theme, i, total, bg_img,
                    is_hook, is_cta,
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
                        "start": current_time + 0.15,  # 약간의 딜레이
                    })

                current_time += slide_dur

            if not slide_clips:
                raise RuntimeError("생성된 슬라이드 클립이 없습니다.")

            total_duration = current_time
            logger.info(f"총 영상 길이: {total_duration:.1f}초 (TTS 합계: {total_tts_dur:.1f}초)")

            # 4. 클립 합치기
            silent_video = str(tmp / "silent.mp4")
            _concat_clips(slide_clips, silent_video)

            # 5. 오디오 합성 (TTS만, BGM 없음)
            _merge_audio_to_video(silent_video, tts_segments, total_duration, out)

            logger.info(f"영상 완료: {out} ({total_duration:.1f}초)")
            return out

    def _convert_script_to_narration(
        self, script: list[dict], mode: str, title: str
    ) -> list[dict]:
        """기존 youtube_script를 나래이션 형식으로 변환 (blog_content 없을 때 fallback)."""
        if not script:
            return _fallback_script(title, mode)

        result = []
        for seg in script:
            seg_title = _strip_emoji(seg.get("title", ""))
            seg_body  = _strip_emoji(seg.get("body", ""))
            narration = f"{seg_title}. {seg_body}" if seg_body else seg_title
            headline  = seg_title[:15] if seg_title else "분석"
            summary   = seg_body[:35] if seg_body else "자세한 내용 확인"

            result.append({
                "narration": narration,
                "headline":  headline,
                "summary":   summary,
            })
        return result

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
