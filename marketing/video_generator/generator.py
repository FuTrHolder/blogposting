"""
YouTube Shorts 영상 생성기 v5
==============================================
개선사항:
  - 이모지 텍스트 깨짐 수정: 이모지 제거 후 텍스트로 대체
  - TTS 음성 중복 수정: BGM과 TTS를 정확히 1개 트랙으로 믹싱
  - 배경 오버레이 밝기 개선: 어두운 오버레이 완화
  - 음성 중복 원인 제거: amix 필터 정리

TTS: edge-tts ko-KR-InJoonNeural (젊은 남성)
규격: 1080×1920, 30fps, H.264
"""

import asyncio
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import time
import urllib.parse
import urllib.request
from math import sin, pi
from pathlib import Path
from struct import pack

import requests
from PIL import Image, ImageDraw, ImageFilter, ImageFont

logger = logging.getLogger(__name__)

# ── 규격 ─────────────────────────────────────────────────────────────────────
VIDEO_W, VIDEO_H = 1080, 1920
MIN_SLIDE_SEC    = 3.0
MAX_SLIDE_SEC    = 5.5
OUTPUT_DIR       = "videos"

# ── 시스템 폰트 경로 (ubuntu-latest 실제 경로) ───────────────────────────────
_FONT_BOLD    = "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc"
_FONT_BLACK   = "/usr/share/fonts/opentype/noto/NotoSansCJK-Black.ttc"
_FONT_REGULAR = "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"

# ── 색상 테마 ─────────────────────────────────────────────────────────────────
THEMES = {
    "morning": {
        "overlay": (8, 15, 35, 140),       # ← 투명도 140 (기존 195에서 완화)
        "accent": (56, 189, 248),
        "highlight": (254, 211, 48),
        "alert": (239, 68, 68),
        "title": (255, 255, 255),
        "body": (220, 230, 245),
        "tag_bg": (56, 189, 248),
        "tag_fg": (8, 15, 35),
        "hook_color": (254, 211, 48),
        "cta_bg": (56, 189, 248),
        "cta_fg": (8, 15, 35),
        "progress": (56, 189, 248),
    },
    "evening": {
        "overlay": (18, 5, 40, 145),       # ← 투명도 145 (기존 200에서 완화)
        "accent": (167, 139, 250),
        "highlight": (251, 191, 36),
        "alert": (239, 68, 68),
        "title": (255, 255, 255),
        "body": (225, 200, 255),
        "tag_bg": (167, 139, 250),
        "tag_fg": (18, 5, 40),
        "hook_color": (251, 191, 36),
        "cta_bg": (167, 139, 250),
        "cta_fg": (18, 5, 40),
        "progress": (167, 139, 250),
    },
}

# ── Pexels 무료 API 키워드 매핑 ───────────────────────────────────────────────
PEXELS_KEYWORDS = {
    "morning": ["wall street morning", "stock market finance", "financial district dawn",
                "new york finance", "nasdaq trading"],
    "evening": ["city night finance", "new york night skyline", "stock exchange night",
                "wall street night", "financial market evening"],
}

BGM_VOLUME = 0.06   # ← 볼륨 약간 낮춤 (TTS와 충돌 최소화)

TTS_VOICE = "ko-KR-InJoonNeural"
TTS_RATE  = "+18%"
TTS_PITCH = "-2Hz"

# ── 이모지 제거 패턴 ──────────────────────────────────────────────────────────
_EMOJI_RE = re.compile(
    "["
    "\U0001F600-\U0001F64F"
    "\U0001F300-\U0001F5FF"
    "\U0001F680-\U0001F6FF"
    "\U0001F1E0-\U0001F1FF"
    "\U00002600-\U000027BF"
    "\U0001F900-\U0001F9FF"
    "\U00002702-\U000027B0"
    "\U000024C2-\U0001F251"
    "]+",
    flags=re.UNICODE,
)

def _strip_emoji(text: str) -> str:
    """PIL에서 렌더링 안 되는 이모지 제거."""
    return _EMOJI_RE.sub("", text).strip()


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
    logger.warning(f"시스템 CJK 폰트 없음 — default 폰트 사용 (size={size})")
    return ImageFont.load_default()


# ═══════════════════════════════════════════════════════════════════════════════
# 픽셀 기반 한글 줄바꿈
# ═══════════════════════════════════════════════════════════════════════════════

def _pixel_wrap(text: str, font: ImageFont.FreeTypeFont, max_px: int) -> list[str]:
    _img  = Image.new("RGB", (10, 10))
    _draw = ImageDraw.Draw(_img)

    def _w(t):
        return _draw.textbbox((0, 0), t, font=font)[2]

    words   = text.split()
    lines   = []
    current = ""

    for word in words:
        sep       = "" if not current else " "
        candidate = current + sep + word
        if _w(candidate) <= max_px:
            current = candidate
        else:
            if current:
                lines.append(current)
            if _w(word) > max_px:
                chunk = ""
                for ch in word:
                    if _w(chunk + ch) > max_px:
                        if chunk:
                            lines.append(chunk)
                        chunk = ch
                    else:
                        chunk += ch
                current = chunk
            else:
                current = word

    if current:
        lines.append(current)
    return lines if lines else [text]


# ═══════════════════════════════════════════════════════════════════════════════
# 배경 이미지 처리
# ═══════════════════════════════════════════════════════════════════════════════

def _download_bg_pexels(keywords: list[str], dest: Path, pexels_key: str) -> bool:
    if not pexels_key:
        return False
    query     = keywords[0] if keywords else "finance"
    today_sig = int(time.time() / 86400)

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
        photo   = photos[today_sig % len(photos)]
        img_url = photo["src"]["large2x"]
        ir      = requests.get(img_url, timeout=30)
        ir.raise_for_status()
        dest.write_bytes(ir.content)
        Image.open(dest).verify()
        logger.info(f"Pexels 배경 다운로드 완료: {query}")
        return True
    except Exception as e:
        logger.warning(f"Pexels 실패 ({query}): {e}")
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


def _prepare_bg(path, overlay_color: tuple, mode: str) -> Image.Image:
    """배경 이미지 → 1080×1920 RGB (오버레이 완화)."""
    W, H = VIDEO_W, VIDEO_H

    if path and Path(path).exists():
        try:
            bg = Image.open(path).convert("RGB")
            # 비율 유지 크롭 (세로 중심)
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
            # ← 블러 약화 (배경이 더 잘 보이도록)
            bg = bg.filter(ImageFilter.GaussianBlur(radius=4))
        except Exception as e:
            logger.warning(f"배경 처리 실패: {e}")
            bg = _make_gradient_bg(overlay_color[:3], mode)
    else:
        bg = _make_gradient_bg(overlay_color[:3], mode)

    # 오버레이 적용 (투명도 완화)
    overlay = Image.new("RGBA", (W, H), overlay_color)
    result  = Image.alpha_composite(bg.convert("RGBA"), overlay)

    # 하단 그라디언트 (텍스트 가독성 확보 - 하단만)
    grad = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d    = ImageDraw.Draw(grad)
    for y in range(int(H * 0.55), H):
        a = int(120 * ((y - H * 0.55) / (H * 0.45)) ** 1.2)
        d.line([(0, y), (W, y)], fill=(0, 0, 0, min(a, 120)))
    result = Image.alpha_composite(result, grad)
    return result.convert("RGB")


def _make_gradient_bg(base_color: tuple, mode: str) -> Image.Image:
    W, H = VIDEO_W, VIDEO_H
    img  = Image.new("RGB", (W, H))
    d    = ImageDraw.Draw(img)
    r, g, b = base_color
    for y in range(H):
        t  = y / H
        lr = int(r * (1 - t * 0.5))
        lg = int(g * (1 - t * 0.3))
        lb = int(b + (100 - b) * t * 0.3)
        d.line([(0, y), (W, y)], fill=(max(0, lr), max(0, lg), max(0, min(255, lb))))
    return img


# ═══════════════════════════════════════════════════════════════════════════════
# BGM 생성 (WAV 사인파)
# ═══════════════════════════════════════════════════════════════════════════════

def _make_bgm_wav(dest: Path, duration: float, mode: str):
    sr    = 44100
    n     = int(sr * duration)
    amp   = 6000   # ← 진폭 줄임

    if mode == "evening":
        freqs = [220.0, 261.63, 329.63]
    else:
        freqs = [261.63, 329.63, 392.0]

    samples = []
    fade_s  = int(sr * 2.0)
    for i in range(n):
        t   = i / sr
        val = sum(sin(2 * pi * f * t) for f in freqs) / len(freqs)
        if i < fade_s:
            val *= i / fade_s
        elif i > n - fade_s:
            val *= (n - i) / fade_s
        samples.append(int(val * amp * BGM_VOLUME))

    data_size   = n * 2
    header_size = 44
    with open(dest, "wb") as f:
        f.write(b"RIFF")
        f.write(pack("<I", header_size - 8 + data_size))
        f.write(b"WAVE")
        f.write(b"fmt ")
        f.write(pack("<IHHIIHH", 16, 1, 1, sr, sr * 2, 2, 16))
        f.write(b"data")
        f.write(pack("<I", data_size))
        for s in samples:
            s = max(-32768, min(32767, s))
            f.write(pack("<h", s))
    logger.info(f"BGM WAV 생성: {dest} ({duration:.1f}초)")


# ═══════════════════════════════════════════════════════════════════════════════
# TTS
# ═══════════════════════════════════════════════════════════════════════════════

async def _tts_async(text: str, path: str):
    import edge_tts
    comm = edge_tts.Communicate(text=text, voice=TTS_VOICE, rate=TTS_RATE, pitch=TTS_PITCH)
    await comm.save(path)


def _generate_tts(text: str, path: str) -> bool:
    try:
        asyncio.run(_tts_async(text, path))
        return Path(path).exists() and Path(path).stat().st_size > 1000
    except ImportError:
        logger.warning("edge-tts 미설치 — TTS 건너뜀")
        return False
    except Exception as e:
        logger.warning(f"TTS 실패: {e}")
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
# 수치 강조 세그먼트 파싱
# ═══════════════════════════════════════════════════════════════════════════════

_HIGHLIGHT_RE = re.compile(
    r"([+-]?\d+(?:\.\d+)?%"
    r"|[+-]?\d+(?:\.\d+)?(?:포인트|달러|원|억|조|만)"
    r"|[+-]?\d{1,3}(?:,\d{3})+"
    r"|S&P\s*500|나스닥|다우|FOMC|Fed|연준|엔비디아|애플|테슬라|마이크로소프트)"
)


def _highlight_segs(text: str) -> list[tuple[str, bool]]:
    segs, last = [], 0
    for m in _HIGHLIGHT_RE.finditer(text):
        if m.start() > last:
            segs.append((text[last:m.start()], False))
        segs.append((m.group(), True))
        last = m.end()
    if last < len(text):
        segs.append((text[last:], False))
    return segs or [(text, False)]


# ═══════════════════════════════════════════════════════════════════════════════
# 외곽선 텍스트
# ═══════════════════════════════════════════════════════════════════════════════

def _draw_outlined(draw, pos, text, font, fill, outline=(0, 0, 0), ow=3):
    x, y = pos
    dirs = [(-ow, 0), (ow, 0), (0, -ow), (0, ow),
            (-ow, -ow), (ow, -ow), (-ow, ow), (ow, ow)]
    for dx, dy in dirs:
        draw.text((x + dx, y + dy), text, font=font, fill=(*outline, 210))
    draw.text((x, y), text, font=font, fill=fill)


def _draw_text_centered(draw, cx, y, text, font, fill, outline=(0, 0, 0), ow=3) -> int:
    bbox = draw.textbbox((0, 0), text, font=font)
    w    = bbox[2] - bbox[0]
    h    = bbox[3] - bbox[1]
    _draw_outlined(draw, (cx - w // 2, y), text, font, fill, outline, ow)
    return h


# ═══════════════════════════════════════════════════════════════════════════════
# 슬라이드 이미지 생성
# ═══════════════════════════════════════════════════════════════════════════════

def _make_slide(slide_data, theme, slide_num, total, bg, is_hook, is_cta, blog_url=""):
    W, H   = VIDEO_W, VIDEO_H
    img    = bg.copy()
    draw   = ImageDraw.Draw(img)

    accent    = theme["accent"]
    highlight = theme["highlight"]
    title_c   = theme["title"]
    body_c    = theme["body"]
    hook_c    = theme["hook_color"]

    f_huge  = _load_font(90, bold=True)
    f_title = _load_font(68, bold=True)
    f_body  = _load_font(50, bold=False)
    f_tag   = _load_font(34, bold=True)
    f_small = _load_font(28, bold=False)

    CX      = W // 2
    WRAP_PX = W - 120

    # ── 진행 바 ─────────────────────────────────────────────────────────────
    bar_w = int(W * slide_num / total)
    draw.rectangle([(0, 0), (W, 10)], fill=(255, 255, 255, 40))
    draw.rectangle([(0, 0), (bar_w, 10)], fill=(*accent, 240))

    # ── 슬라이드 번호 뱃지 ──────────────────────────────────────────────────
    badge = f"{slide_num} / {total}"
    bb    = draw.textbbox((0, 0), badge, font=f_tag)
    bw    = bb[2] - bb[0] + 36
    bh    = bb[3] - bb[1] + 20
    draw.rounded_rectangle([(40, 30), (40 + bw, 30 + bh)], radius=bh // 2,
                            fill=(*theme["tag_bg"], 230))
    draw.text((40 + 18, 30 + 10), badge, font=f_tag, fill=theme["tag_fg"])

    # ── 훅 슬라이드 ─────────────────────────────────────────────────────────
    if is_hook:
        # 이모지 대신 텍스트 뱃지 사용 (이모지 제거로 깨짐 방지)
        hook_badge = _strip_emoji("오늘의 핵심")
        hb  = draw.textbbox((0, 0), hook_badge, font=f_tag)
        hw  = hb[2] - hb[0] + 40
        hh  = hb[3] - hb[1] + 24
        hx  = CX - hw // 2
        hy  = H // 4 - 80
        draw.rounded_rectangle([(hx, hy), (hx + hw, hy + hh)],
                                radius=hh // 2, fill=(*hook_c, 220))
        draw.text((hx + 20, hy + 12), hook_badge, font=f_tag, fill=(20, 20, 20))

        title = _strip_emoji(slide_data.get("title", ""))
        lines = _pixel_wrap(title, f_huge, WRAP_PX)[:3]
        y     = hy + hh + 30
        for line in lines:
            h = _draw_text_centered(draw, CX, y, line, f_huge, (*hook_c, 255), ow=4)
            y += h + 12

        draw.rectangle([(100, y + 20), (W - 100, y + 24)], fill=(*accent, 180))

        body  = _strip_emoji(slide_data.get("body", ""))
        lines = _pixel_wrap(body, f_body, WRAP_PX)[:4]
        y     = y + 50
        for line in lines:
            h = _draw_text_centered(draw, CX, y, line, f_body, title_c, ow=2)
            y += h + 10

        # 이모지 없이 텍스트로 유도
        cont_text = _strip_emoji("▼  계속 보기")
        draw.text((CX - 80, H - 280), cont_text, font=f_tag, fill=(*accent, 200))

    # ── CTA 슬라이드 ────────────────────────────────────────────────────────
    elif is_cta:
        cta_badge = _strip_emoji("투자 포인트")
        cb   = draw.textbbox((0, 0), cta_badge, font=f_tag)
        cw   = cb[2] - cb[0] + 40
        ch   = cb[3] - cb[1] + 24
        cx_b = CX - cw // 2
        cy_b = H // 5
        draw.rounded_rectangle([(cx_b, cy_b), (cx_b + cw, cy_b + ch)],
                                radius=ch // 2, fill=(*highlight, 220))
        draw.text((cx_b + 20, cy_b + 12), cta_badge, font=f_tag, fill=(20, 20, 20))

        title = _strip_emoji(slide_data.get("title", ""))
        lines = _pixel_wrap(title, f_title, WRAP_PX)[:2]
        y     = cy_b + ch + 30
        for line in lines:
            h = _draw_text_centered(draw, CX, y, line, f_title, title_c, ow=3)
            y += h + 10

        body  = _strip_emoji(slide_data.get("body", ""))
        lines = _pixel_wrap(body, f_body, WRAP_PX)[:4]
        draw.rectangle([(100, y + 10), (W - 100, y + 14)], fill=(*accent, 160))
        y    += 30
        for line in lines:
            h = _draw_text_centered(draw, CX, y, line, f_body, body_c, ow=2)
            y += h + 8

        btn_y = H - 380
        draw.rounded_rectangle([(80, btn_y), (W - 80, btn_y + 110)],
                                radius=22, fill=(*theme["cta_bg"], 245))
        _draw_text_centered(draw, CX, btn_y + 30,
                            _strip_emoji("전체 분석 보기"), f_body, theme["cta_fg"])

    # ── 일반 슬라이드 ────────────────────────────────────────────────────────
    else:
        title = _strip_emoji(slide_data.get("title", ""))
        lines = _pixel_wrap(title, f_title, WRAP_PX)[:2]
        y     = int(H * 0.22)
        for line in lines:
            h = _draw_text_centered(draw, CX, y, line, f_title, title_c, ow=3)
            y += h + 12

        draw.rectangle([(100, y + 18), (W - 100, y + 22)], fill=(*accent, 180))
        y += 50

        body  = _strip_emoji(slide_data.get("body", ""))
        lines = _pixel_wrap(body, f_body, WRAP_PX)[:5]
        for line in lines:
            segs    = _highlight_segs(line)
            total_w = sum(draw.textbbox((0, 0), s, font=f_body)[2] for s, _ in segs)
            x_cur   = CX - total_w // 2
            max_h   = 0
            for seg_text, is_hl in segs:
                color = (*highlight, 255) if is_hl else (*body_c, 230)
                bbox  = draw.textbbox((0, 0), seg_text, font=f_body)
                sw    = bbox[2] - bbox[0]
                sh    = bbox[3] - bbox[1]
                _draw_outlined(draw, (x_cur, y), seg_text, f_body, color[:3], ow=2)
                x_cur += sw
                max_h  = max(max_h, sh)
            y += max_h + 10

    # ── 워터마크 ─────────────────────────────────────────────────────────────
    wm  = "미국증시 분석 | seedsup.tistory.com"
    wbb = draw.textbbox((0, 0), wm, font=f_small)
    draw.text((CX - (wbb[2] - wbb[0]) // 2, H - 70), wm,
              font=f_small, fill=(*accent, 150))
    return img


# ═══════════════════════════════════════════════════════════════════════════════
# 스크립트 개선
# ═══════════════════════════════════════════════════════════════════════════════

def _enhance_script(script: list[dict], mode: str) -> list[dict]:
    enhanced = []
    for i, slide in enumerate(script):
        s       = dict(slide)
        is_last = (i == len(script) - 1)
        if is_last:
            b     = _strip_emoji(s.get("body", ""))
            label = "오늘 밤" if mode == "evening" else "지금 바로"
            if "seedsup" not in b and "블로그" not in b:
                s["body"] = b + f" {label} 전체 분석 확인!"
        enhanced.append(s)
    return enhanced


# ═══════════════════════════════════════════════════════════════════════════════
# ffmpeg 헬퍼
# ═══════════════════════════════════════════════════════════════════════════════

def _run(cmd: list, **kwargs) -> subprocess.CompletedProcess:
    logger.debug("$ " + " ".join(str(c) for c in cmd))
    return subprocess.run(cmd, check=True, capture_output=True, text=True, **kwargs)


def _image_to_clip(img_path: str, duration: float, out_path: str):
    """PNG → MP4 클립. fade in/out 포함."""
    fade_d = min(0.2, duration * 0.06)
    vf     = (
        f"fade=t=in:st=0:d={fade_d:.2f},"
        f"fade=t=out:st={duration - fade_d:.2f}:d={fade_d:.2f}"
    )
    _run([
        "ffmpeg", "-y",
        "-loop", "1", "-i", img_path,
        "-t", str(duration),
        "-vf", vf,
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


def _mix_audio(video: str, tts_segs: list[dict], bgm: str,
               total_dur: float, out: str):
    """
    TTS + BGM → 최종 영상 합성.
    ※ 음성 중복 방지: 각 TTS 세그먼트를 하나의 concat 오디오로 먼저 합치고,
       BGM과 2-트랙 amix (normalize=0) 적용.
    """
    if not tts_segs:
        # TTS 없을 때 BGM만
        if bgm and Path(bgm).exists():
            _run([
                "ffmpeg", "-y", "-i", video, "-i", bgm,
                "-filter_complex",
                f"[1:a]aloop=loop=-1:size=2e+09,atrim=0:{total_dur}[bgm];"
                f"[bgm]volume=0.06[aout]",
                "-map", "0:v", "-map", "[aout]",
                "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
                "-shortest", out,
            ])
        else:
            _run([
                "ffmpeg", "-y", "-i", video,
                "-c:v", "copy", "-an", out,
            ])
        return

    # TTS 세그먼트를 지연 후 합쳐 단일 오디오로 만들기
    # 각 세그먼트: adelay → concat
    n_tts = len(tts_segs)

    # filter_complex 구성
    inputs  = ["-i", video]
    for seg in tts_segs:
        inputs += ["-i", seg["path"]]

    has_bgm = bgm and Path(bgm).exists()
    if has_bgm:
        inputs += ["-i", bgm]

    fc_parts = []
    tts_labels = []
    for i, seg in enumerate(tts_segs):
        delay = int(seg["start"] * 1000)
        fc_parts.append(
            f"[{i+1}:a]adelay={delay}|{delay}[d{i}]"
        )
        tts_labels.append(f"[d{i}]")

    # TTS 모두 amix (normalize=0 으로 볼륨 유지)
    if n_tts == 1:
        fc_parts.append(f"{tts_labels[0]}apad=whole_dur={total_dur}[tts_mixed]")
    else:
        fc_parts.append(
            "".join(tts_labels)
            + f"amix=inputs={n_tts}:duration=longest:normalize=0,"
            f"apad=whole_dur={total_dur}[tts_mixed]"
        )

    if has_bgm:
        bgm_idx = len(tts_segs) + 1
        fc_parts.append(
            f"[{bgm_idx}:a]aloop=loop=-1:size=2e+09,"
            f"atrim=0:{total_dur},"
            f"volume=0.06[bgm_loop]"
        )
        fc_parts.append(
            "[tts_mixed][bgm_loop]amix=inputs=2:duration=first:normalize=0[aout]"
        )
        mix_label = "[aout]"
    else:
        mix_label = "[tts_mixed]"

    fc = ";".join(fc_parts)

    _run([
        "ffmpeg", "-y",
        *inputs,
        "-filter_complex", fc,
        "-map", "0:v",
        "-map", mix_label,
        "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
        "-shortest", out,
    ])


# ═══════════════════════════════════════════════════════════════════════════════
# VideoGenerator 메인 클래스
# ═══════════════════════════════════════════════════════════════════════════════

class VideoGenerator:
    def __init__(self, output_dir: str = OUTPUT_DIR):
        self.output_dir = output_dir
        self.pexels_key = os.environ.get("PEXELS_API_KEY", "")
        os.makedirs(output_dir, exist_ok=True)

    def generate(
        self,
        script: list[dict],
        mode: str,
        filename: str,
        thumbnail_url: str = "",
        blog_url: str = "",
        bg_keywords: list[str] | None = None,
    ) -> str:
        if not script:
            raise ValueError("스크립트가 비어 있습니다.")

        script = _enhance_script(script, mode)
        theme  = THEMES.get(mode, THEMES["morning"])
        kws    = bg_keywords or PEXELS_KEYWORDS.get(mode, PEXELS_KEYWORDS["morning"])
        out    = os.path.join(self.output_dir, filename)

        with tempfile.TemporaryDirectory(prefix="shorts_") as tmp_s:
            tmp = Path(tmp_s)

            # 1. 배경 이미지 확보
            bg_path = tmp / "bg.jpg"
            bg_ok   = False

            if thumbnail_url:
                try:
                    r = requests.get(thumbnail_url, timeout=15,
                                     headers={"User-Agent": "Mozilla/5.0 (compatible)"})
                    r.raise_for_status()
                    bg_path.write_bytes(r.content)
                    Image.open(bg_path).verify()
                    bg_ok = True
                    logger.info("티스토리 썸네일 로드 성공")
                except Exception as e:
                    logger.warning(f"썸네일 로드 실패: {e}")

            if not bg_ok:
                bg_ok = _download_bg_pexels(kws, bg_path, self.pexels_key)
            if not bg_ok:
                seed  = int(hashlib.md5(f"{mode}{filename}".encode()).hexdigest()[:8], 16)
                bg_ok = _download_bg_picsum(bg_path, seed % 1000)

            bg_img = _prepare_bg(bg_path if bg_ok else None, theme["overlay"], mode)

            # 2. BGM (WAV 생성) - 단일 BGM 파일
            bgm_path = tmp / "bgm.wav"
            total_duration_est = len(script) * 4.5
            _make_bgm_wav(bgm_path, total_duration_est + 5, mode)

            # 3. 슬라이드별 처리
            slide_clips  = []
            tts_segments = []
            current_time = 0.0
            total        = len(script)

            for i, slide in enumerate(script, 1):
                logger.info(f"슬라이드 {i}/{total} 처리 중...")
                is_hook = (i == 1)
                is_cta  = (i == total)

                # TTS 텍스트 (이모지 제거)
                title_txt = _strip_emoji(slide.get("title", ""))
                body_txt  = _strip_emoji(slide.get("body", ""))

                if is_hook:
                    tts_text = f"{title_txt}! {body_txt}"
                elif is_cta:
                    tts_text = f"{title_txt}. {body_txt} 링크에서 확인하세요!"
                else:
                    tts_text = f"{title_txt}. {body_txt}"

                tts_path = str(tmp / f"tts_{i:02d}.mp3")
                tts_ok   = _generate_tts(tts_text, tts_path)

                tts_dur   = _audio_duration(tts_path) if tts_ok else 0.0
                slide_dur = max(MIN_SLIDE_SEC, min(MAX_SLIDE_SEC, tts_dur + 0.6))

                # 슬라이드 이미지
                slide_img = _make_slide(
                    slide, theme, i, total, bg_img,
                    is_hook, is_cta, blog_url,
                )
                img_path  = str(tmp / f"slide_{i:02d}.png")
                slide_img.save(img_path, "PNG", optimize=False)

                clip_path = str(tmp / f"clip_{i:02d}.mp4")
                _image_to_clip(img_path, slide_dur, clip_path)
                slide_clips.append(clip_path)

                if tts_ok:
                    tts_segments.append({
                        "path":  tts_path,
                        "start": current_time + 0.2,
                    })
                current_time += slide_dur

            total_duration = current_time
            logger.info(f"총 영상 길이: {total_duration:.1f}초")

            # 4. 클립 합치기
            silent_video = str(tmp / "silent.mp4")
            _concat_clips(slide_clips, silent_video)

            # 5. 오디오 믹싱 (TTS 중복 없이 단일 트랙)
            _mix_audio(silent_video, tts_segments,
                       str(bgm_path), total_duration, out)

            logger.info(f"영상 완료: {out} ({total_duration:.1f}초)")
            return out

    def generate_with_text_only_fallback(
        self,
        script: list[dict],
        mode: str,
        filename: str,
        thumbnail_url: str = "",
        blog_url: str = "",
        bg_keywords: list[str] | None = None,
    ) -> str:
        try:
            return self.generate(script, mode, filename,
                                 thumbnail_url, blog_url, bg_keywords)
        except Exception as e:
            logger.error(f"영상 생성 실패: {e}")
            raise
