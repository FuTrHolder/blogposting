"""
YouTube Shorts 영상 생성기 v3 - 고성과 쇼츠 공식 적용
슬라이드 스크립트 JSON → MP4 (세로형 9:16, 1080x1920)

[고성과 쇼츠 핵심 공식]
  - 3초 훅: 가장 자극적인 키워드로 시작, 질문/충격 요법
  - 공백 제거: 문장 간 공백 최소화, 나래이션 1.1~1.2배속
  - 5초마다 화면 전환: 정적 배경 금지, 슬라이드당 짧은 듀레이션
  - 무한 루프 유도: 마지막 장면이 첫 장면과 이어지도록
  - 핵심 수치 강조: 노란색/빨간색 텍스트 강조
  - 투자 포인트 코멘트: 마지막에 한 줄 요약

TTS: edge-tts ko-KR-InJoonNeural (젊은 남성, 에너지 있는 목소리)
BGM: CC0 라이선스 무료 루프 자동 믹싱
ffmpeg: 슬라이드→영상 합성

의존성: edge-tts>=6.1.9, requests>=2.31.0, Pillow>=10.3.0
시스템: ffmpeg, Noto Sans CJK 폰트 (ubuntu-latest 기본 포함)
"""

import asyncio
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import textwrap
import time
import urllib.parse
import urllib.request
from pathlib import Path

import requests
from PIL import Image, ImageDraw, ImageFilter, ImageFont

logger = logging.getLogger(__name__)

# ─── 영상 규격 ───────────────────────────────────────────────────────────────
VIDEO_W, VIDEO_H = 1080, 1920
# 고성과 공식: 슬라이드당 최대 5초 (5초마다 화면 전환)
SLIDE_DURATION   = 5.0
MIN_SLIDE_SEC    = 3.0
MAX_SLIDE_SEC    = 5.5   # 5초 룰 엄수 (최대 5.5초)
OUTPUT_DIR = "videos"
FONT_DIR   = Path("fonts")

# ─── 색상 테마 (고성과 공식: 신뢰색 + 강렬한 강조색) ────────────────────────
THEMES = {
    "morning": {
        "overlay_color": (8, 15, 35),
        "overlay_alpha": 0.75,
        "accent": (56, 189, 248),          # 스카이 블루
        "accent_alt": (254, 211, 48),      # 강조 노란색 (수치 강조)
        "alert_color": (239, 68, 68),      # 하락/경고 빨간색
        "title_fg": (255, 255, 255),
        "body_fg": (203, 213, 225),
        "highlight_fg": (254, 211, 48),    # 핵심 수치 강조색
        "tag_bg": (56, 189, 248),
        "tag_fg": (8, 15, 35),
        "progress_bg": (255, 255, 255, 40),
        "hook_color": (254, 211, 48),      # 훅 문구 강조색
        "cta_bg": (56, 189, 248),
        "cta_fg": (8, 15, 35),
    },
    "evening": {
        "overlay_color": (18, 5, 40),
        "overlay_alpha": 0.78,
        "accent": (167, 139, 250),         # 퍼플
        "accent_alt": (251, 191, 36),      # 강조 노란색
        "alert_color": (239, 68, 68),      # 하락/경고 빨간색
        "title_fg": (255, 255, 255),
        "body_fg": (216, 180, 254),
        "highlight_fg": (251, 191, 36),    # 핵심 수치 강조색
        "tag_bg": (167, 139, 250),
        "tag_fg": (18, 5, 40),
        "progress_bg": (255, 255, 255, 40),
        "hook_color": (251, 191, 36),
        "cta_bg": (167, 139, 250),
        "cta_fg": (18, 5, 40),
    },
}

# ─── BGM (CC0) ────────────────────────────────────────────────────────────────
BGM_URLS = {
    "morning": [
        "https://cdn.pixabay.com/download/audio/2022/10/25/audio_058a2434c2.mp3",
        "https://cdn.pixabay.com/download/audio/2022/05/27/audio_1808fbf07a.mp3",
    ],
    "evening": [
        "https://cdn.pixabay.com/download/audio/2022/08/02/audio_884fe92c21.mp3",
        "https://cdn.pixabay.com/download/audio/2021/11/01/audio_10bdb52c2f.mp3",
    ],
}
BGM_VOLUME = 0.10

# ─── TTS 설정 (고성과 공식: 에너지 있는 목소리, 1.2배속) ────────────────────
TTS_VOICE = "ko-KR-InJoonNeural"
TTS_RATE  = "+20%"    # 1.2배속 효과 (공백 없는 빠른 템포)
TTS_PITCH = "-2Hz"

# ─── Unsplash 키워드 ─────────────────────────────────────────────────────────
KEYWORD_MAP = {
    "morning": ["stock market", "nasdaq", "financial district", "wall street", "trading"],
    "evening": ["city night", "new york night", "financial market", "stock exchange"],
}

# ─── 폰트 ────────────────────────────────────────────────────────────────────
SYSTEM_FONT_CANDIDATES = [
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    str(FONT_DIR / "NanumGothicBold.ttf"),
    str(FONT_DIR / "NanumGothic.ttf"),
]
NANUM_BOLD_URL    = "https://github.com/naver/nanumfont/raw/master/fonts/NanumGothicBold.ttf"
NANUM_REGULAR_URL = "https://github.com/naver/nanumfont/raw/master/fonts/NanumGothic.ttf"


# ════════════════════════════════════════════════════════════════════════════
# 유틸리티
# ════════════════════════════════════════════════════════════════════════════

def _run(cmd: list, check=True, **kwargs) -> subprocess.CompletedProcess:
    logger.debug("$ " + " ".join(str(c) for c in cmd))
    return subprocess.run(cmd, check=check, capture_output=True, text=True, **kwargs)


def _get_font_path(bold: bool = True) -> str:
    candidates = (
        [c for c in SYSTEM_FONT_CANDIDATES if "Bold" in c or "bold" in c]
        + [c for c in SYSTEM_FONT_CANDIDATES if "Bold" not in c and "bold" not in c]
    ) if bold else SYSTEM_FONT_CANDIDATES

    for path in candidates:
        if Path(path).exists():
            return path

    FONT_DIR.mkdir(exist_ok=True)
    url  = NANUM_BOLD_URL if bold else NANUM_REGULAR_URL
    dest = FONT_DIR / ("NanumGothicBold.ttf" if bold else "NanumGothic.ttf")
    if not dest.exists():
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                dest.write_bytes(resp.read())
        except Exception as e:
            logger.warning(f"폰트 다운로드 실패: {e}")
            return ""
    return str(dest)


def _download_bgm(mode: str, dest: Path) -> bool:
    for url in BGM_URLS.get(mode, BGM_URLS["morning"]):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                dest.write_bytes(resp.read())
            return True
        except Exception as e:
            logger.warning(f"BGM 다운로드 실패 ({url}): {e}")
    return False


def _download_bg_image(keywords: list[str], dest: Path, width=1080, height=1920) -> bool:
    kw = ",".join(keywords[:3])
    urls_to_try = [
        f"https://source.unsplash.com/random/{width}x{height}/?{urllib.parse.quote(kw)}",
        f"https://source.unsplash.com/random/{width}x{height}/?finance,market",
        f"https://picsum.photos/{width}/{height}",
    ]
    for url in urls_to_try:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=20) as resp:
                dest.write_bytes(resp.read())
            img = Image.open(dest)
            img.verify()
            return True
        except Exception as e:
            logger.warning(f"배경 이미지 다운로드 실패 ({url}): {e}")
            if dest.exists():
                dest.unlink()
    return False


# ════════════════════════════════════════════════════════════════════════════
# 숫자/수치 강조 파서
# ════════════════════════════════════════════════════════════════════════════

def _parse_highlight_segments(text: str) -> list[tuple[str, bool]]:
    """
    텍스트에서 숫자/퍼센트/종목명 등 강조할 세그먼트를 파싱합니다.
    반환: [(텍스트, 강조여부), ...]
    """
    # 숫자, %, +, - 포함 패턴 강조
    pattern = re.compile(
        r"([+-]?\d+(?:\.\d+)?%|[+-]?\d{1,3}(?:,\d{3})*(?:\.\d+)?포인트?|"
        r"[+-]?\d+(?:\.\d+)?달러?|S&P\s*500|나스닥|다우|FOMC|Fed|연준)"
    )
    segments = []
    last = 0
    for m in pattern.finditer(text):
        if m.start() > last:
            segments.append((text[last:m.start()], False))
        segments.append((m.group(), True))
        last = m.end()
    if last < len(text):
        segments.append((text[last:], False))
    return segments if segments else [(text, False)]


# ════════════════════════════════════════════════════════════════════════════
# 슬라이드 이미지 생성 (고성과 쇼츠 공식 적용)
# ════════════════════════════════════════════════════════════════════════════

def _make_slide_image(
    slide_data: dict,
    theme: dict,
    slide_num: int,
    total: int,
    bg_image_path: str | None,
    blog_url: str = "",
    font_bold: str = "",
    font_regular: str = "",
    is_hook: bool = False,      # 첫 슬라이드 (훅) 여부
    is_cta: bool = False,       # 마지막 슬라이드 (CTA/루프) 여부
) -> Image.Image:
    W, H = VIDEO_W, VIDEO_H

    # ── 배경 처리 ────────────────────────────────────────────────────────────
    if bg_image_path and Path(bg_image_path).exists():
        try:
            bg = Image.open(bg_image_path).convert("RGB")
            # 슬라이드별로 구도를 달리해 역동성 부여
            src_ratio = bg.width / bg.height
            dst_ratio = W / H
            if src_ratio > dst_ratio:
                new_h = bg.height
                new_w = int(new_h * dst_ratio)
                # 슬라이드마다 x 오프셋 이동 → 화면 전환 효과
                max_offset = bg.width - new_w
                offset_x   = int(max_offset * ((slide_num - 1) / max(total - 1, 1)))
                bg = bg.crop((offset_x, 0, offset_x + new_w, new_h))
            else:
                new_w = bg.width
                new_h = int(new_w / dst_ratio)
                offset_y = int((bg.height - new_h) * 0.25)
                bg = bg.crop((0, offset_y, new_w, offset_y + new_h))
            bg = bg.resize((W, H), Image.LANCZOS)

            # 훅 슬라이드: 더 강한 블러로 텍스트 가독성 극대화
            blur_r = 5 if is_hook else 3
            bg = bg.filter(ImageFilter.GaussianBlur(radius=blur_r))
        except Exception as e:
            logger.warning(f"배경 이미지 처리 실패: {e}")
            bg = Image.new("RGB", (W, H), theme["overlay_color"])
    else:
        bg = Image.new("RGB", (W, H), theme["overlay_color"])

    # ── 오버레이 ─────────────────────────────────────────────────────────────
    oc    = theme["overlay_color"]
    alpha = int(theme["overlay_alpha"] * 255)
    # 훅/CTA 슬라이드는 더 어두운 오버레이
    if is_hook or is_cta:
        alpha = min(alpha + 30, 255)

    overlay  = Image.new("RGBA", (W, H), (*oc, alpha))
    gradient = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    grad_draw = ImageDraw.Draw(gradient)
    for y in range(H // 3, H):
        a = int(200 * ((y - H // 3) / (H * 2 / 3)) ** 1.8)
        grad_draw.line([(0, y), (W, y)], fill=(0, 0, 0, a))

    result = Image.alpha_composite(bg.convert("RGBA"), overlay)
    result = Image.alpha_composite(result, gradient)
    img    = result.convert("RGB")
    draw   = ImageDraw.Draw(img)

    # ── 폰트 ─────────────────────────────────────────────────────────────────
    def _font(path, size):
        try:
            if path:
                return ImageFont.truetype(path, size)
        except Exception:
            pass
        return ImageFont.load_default()

    f_hook    = _font(font_bold, 92)   # 훅 문구용 초대형
    f_title   = _font(font_bold, 78)
    f_body    = _font(font_regular or font_bold, 54)
    f_tag     = _font(font_regular or font_bold, 38)
    f_small   = _font(font_regular or font_bold, 32)
    f_num     = _font(font_bold, 100)  # 핵심 수치 강조용

    accent      = theme["accent"]
    highlight   = theme["highlight_fg"]
    alert       = theme["alert_color"]
    hook_color  = theme["hook_color"]

    # ── 상단 진행 바 ─────────────────────────────────────────────────────────
    bar_h = 10
    bar_w = int(W * slide_num / total)
    draw.rectangle([(0, 0), (W, bar_h)], fill=(*theme["progress_bg"][:3], theme["progress_bg"][3]))
    draw.rectangle([(0, 0), (bar_w, bar_h)], fill=(*accent, 240))

    # ── 슬라이드 번호 배지 ───────────────────────────────────────────────────
    tag_text = f"{slide_num} / {total}"
    tag_pad  = 18
    tag_bbox = draw.textbbox((0, 0), tag_text, font=f_tag)
    tag_w    = tag_bbox[2] - tag_bbox[0] + tag_pad * 2
    tag_h    = tag_bbox[3] - tag_bbox[1] + tag_pad
    draw.rounded_rectangle([(40, 36), (40 + tag_w, 36 + tag_h)],
                            radius=10, fill=(*theme["tag_bg"], 230))
    draw.text((40 + tag_pad, 36 + tag_pad // 2), tag_text,
              font=f_tag, fill=theme["tag_fg"])

    # ── 훅 슬라이드 특별 처리 ────────────────────────────────────────────────
    if is_hook:
        # "HOOK" 뱃지
        hook_badge = "🔥 오늘의 핵심"
        hb_bbox    = draw.textbbox((0, 0), hook_badge, font=f_tag)
        hb_w       = hb_bbox[2] - hb_bbox[0] + 30
        hb_h       = hb_bbox[3] - hb_bbox[1] + 18
        hb_x       = (W - hb_w) // 2
        hb_y       = H // 4 - 60
        draw.rounded_rectangle(
            [(hb_x, hb_y), (hb_x + hb_w, hb_y + hb_h)],
            radius=hb_h // 2,
            fill=(*hook_color[:3], 220) if len(hook_color) == 3 else (*hook_color, 220),
        )
        draw.text(
            (hb_x + 15, hb_y + 9),
            hook_badge, font=f_tag,
            fill=(8, 15, 35),
        )

        # 훅 제목 (크고 강렬하게)
        title   = slide_data.get("title", "")
        t_lines = textwrap.wrap(title, width=10)[:3]
        y_t     = H // 4 + 20
        for line in t_lines:
            bbox = draw.textbbox((0, 0), line, font=f_hook)
            lw   = bbox[2] - bbox[0]
            lx   = (W - lw) // 2
            # 두꺼운 그림자
            draw.text((lx + 4, y_t + 4), line, font=f_hook, fill=(0, 0, 0, 200))
            draw.text((lx, y_t), line, font=f_hook,
                      fill=(*hook_color[:3], 255) if len(hook_color) == 3 else hook_color)
            y_t += bbox[3] - bbox[1] + 16

        # 구분선
        draw.rectangle([(80, y_t + 20), (W - 80, y_t + 24)], fill=(*accent, 200))

        # 훅 본문 (질문/충격 요법)
        body    = slide_data.get("body", "")
        b_lines = textwrap.wrap(body, width=16)[:4]
        y_b     = y_t + 50
        for line in b_lines:
            bbox = draw.textbbox((0, 0), line, font=f_body)
            lw   = bbox[2] - bbox[0]
            lx   = (W - lw) // 2
            draw.text((lx + 2, y_b + 2), line, font=f_body, fill=(0, 0, 0, 140))
            draw.text((lx, y_b), line, font=f_body, fill=theme["title_fg"])
            y_b += bbox[3] - bbox[1] + 10

        # 하단 "계속 보기" 유도 화살표
        arrow_y = H - 260
        draw.text((W // 2 - 30, arrow_y), "▼  계속 보기", font=f_tag,
                  fill=(*accent, 200))

    # ── CTA / 루프 슬라이드 특별 처리 ────────────────────────────────────────
    elif is_cta:
        # 루프 유도: 첫 훅으로 이어지는 멘트
        cta_badge = "💡 투자 포인트"
        cb_bbox   = draw.textbbox((0, 0), cta_badge, font=f_tag)
        cb_w      = cb_bbox[2] - cb_bbox[0] + 30
        cb_h      = cb_bbox[3] - cb_bbox[1] + 18
        cb_x      = (W - cb_w) // 2
        cb_y      = H // 5
        draw.rounded_rectangle(
            [(cb_x, cb_y), (cb_x + cb_w, cb_y + cb_h)],
            radius=cb_h // 2, fill=(*highlight[:3], 220),
        )
        draw.text((cb_x + 15, cb_y + 9), cta_badge, font=f_tag,
                  fill=(8, 15, 35))

        # 투자 포인트 제목
        title   = slide_data.get("title", "")
        t_lines = textwrap.wrap(title, width=11)[:2]
        y_t     = H // 5 + cb_h + 30
        for line in t_lines:
            bbox = draw.textbbox((0, 0), line, font=f_title)
            lw   = bbox[2] - bbox[0]
            lx   = (W - lw) // 2
            draw.text((lx + 3, y_t + 3), line, font=f_title, fill=(0, 0, 0, 180))
            draw.text((lx, y_t), line, font=f_title, fill=theme["title_fg"])
            y_t += bbox[3] - bbox[1] + 14

        # 본문 (핵심 포인트)
        body    = slide_data.get("body", "")
        b_lines = textwrap.wrap(body, width=17)[:4]
        y_b     = y_t + 30
        draw.rectangle([(80, y_b - 10), (W - 80, y_b - 6)], fill=(*accent, 180))
        y_b += 10
        for line in b_lines:
            bbox = draw.textbbox((0, 0), line, font=f_body)
            lw   = bbox[2] - bbox[0]
            lx   = (W - lw) // 2
            draw.text((lx + 2, y_b + 2), line, font=f_body, fill=(0, 0, 0, 120))
            draw.text((lx, y_b), line, font=f_body, fill=theme["body_fg"])
            y_b += bbox[3] - bbox[1] + 10

        # CTA 버튼 (블로그 링크 클릭 유도)
        cta_btn_y = H - 350
        cta_btn_h = 110
        draw.rounded_rectangle(
            [(80, cta_btn_y), (W - 80, cta_btn_y + cta_btn_h)],
            radius=20, fill=(*theme["cta_bg"], 245),
        )
        cta_text = "📊 전체 분석 보기 →"
        cb2      = draw.textbbox((0, 0), cta_text, font=f_body)
        draw.text(
            ((W - (cb2[2] - cb2[0])) // 2,
             cta_btn_y + (cta_btn_h - (cb2[3] - cb2[1])) // 2),
            cta_text, font=f_body, fill=theme["cta_fg"],
        )

        # 루프 유도 화살표 (툭 끊기듯 루프)
        loop_y = H - 210
        draw.text((W // 2 - 60, loop_y), "🔁 다시 보기", font=f_tag,
                  fill=(*hook_color[:3], 180) if len(hook_color) == 3 else (*hook_color, 180))

    # ── 일반 슬라이드 ─────────────────────────────────────────────────────────
    else:
        title   = slide_data.get("title", "")
        t_lines = textwrap.wrap(title, width=11)[:2]
        y_t     = int(H * 0.22)
        for line in t_lines:
            bbox = draw.textbbox((0, 0), line, font=f_title)
            lw   = bbox[2] - bbox[0]
            lx   = (W - lw) // 2
            draw.text((lx + 3, y_t + 3), line, font=f_title, fill=(0, 0, 0, 180))
            draw.text((lx, y_t), line, font=f_title, fill=theme["title_fg"])
            y_t += bbox[3] - bbox[1] + 14

        # 구분선
        line_y = y_t + 20
        draw.rectangle([(80, line_y), (W - 80, line_y + 5)], fill=(*accent, 200))

        # 본문 (강조 세그먼트 분리 렌더링)
        body    = slide_data.get("body", "")
        b_lines = textwrap.wrap(body, width=17)[:5]
        y_b     = line_y + 40
        for line in b_lines:
            segs = _parse_highlight_segments(line)
            # 줄 전체 너비 계산
            total_w = sum(
                draw.textbbox((0, 0), seg, font=f_body)[2]
                - draw.textbbox((0, 0), seg, font=f_body)[0]
                for seg, _ in segs
            )
            x_cursor = (W - total_w) // 2
            line_h   = 0
            for seg_text, is_hl in segs:
                color = (*highlight[:3], 255) if is_hl else theme["body_fg"]
                font  = f_body
                bbox  = draw.textbbox((0, 0), seg_text, font=font)
                seg_w = bbox[2] - bbox[0]
                seg_h = bbox[3] - bbox[1]
                draw.text((x_cursor + 2, y_b + 2), seg_text, font=font,
                          fill=(0, 0, 0, 120))
                draw.text((x_cursor, y_b), seg_text, font=font, fill=color)
                x_cursor += seg_w
                line_h    = max(line_h, seg_h)
            y_b += line_h + 12

    # ── 하단 워터마크 ─────────────────────────────────────────────────────────
    wm   = "미국증시 분석 | seedsup.tistory.com"
    bbox = draw.textbbox((0, 0), wm, font=f_small)
    draw.text(
        ((W - (bbox[2] - bbox[0])) // 2, H - 80),
        wm, font=f_small, fill=(*accent, 160),
    )

    return img


# ════════════════════════════════════════════════════════════════════════════
# TTS 생성
# ════════════════════════════════════════════════════════════════════════════

async def _tts_async(text: str, output_path: str, voice: str = TTS_VOICE):
    import edge_tts
    communicate = edge_tts.Communicate(
        text=text, voice=voice, rate=TTS_RATE, pitch=TTS_PITCH,
    )
    await communicate.save(output_path)


def _generate_tts(text: str, output_path: str) -> bool:
    try:
        asyncio.run(_tts_async(text, output_path))
        return Path(output_path).exists() and Path(output_path).stat().st_size > 0
    except ImportError:
        logger.warning("edge-tts 미설치. TTS 건너뜀.")
        return False
    except Exception as e:
        logger.warning(f"TTS 생성 실패: {e}")
        return False


def _get_audio_duration(path: str) -> float:
    try:
        result = _run([
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "json", path,
        ])
        data = json.loads(result.stdout)
        return float(data["format"]["duration"])
    except Exception:
        return 0.0


# ════════════════════════════════════════════════════════════════════════════
# ffmpeg 합성
# ════════════════════════════════════════════════════════════════════════════

def _image_to_video_clip(image_path: str, duration: float, output_path: str):
    """정지 이미지 → 지정 길이 MP4 (줌인 효과로 역동성 추가)."""
    # Ken Burns 효과: 서서히 줌인
    vf = (
        f"scale={VIDEO_W * 2}:{VIDEO_H * 2},"
        f"zoompan=z='min(zoom+0.0015,1.5)':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'"
        f":d={int(duration * 30)}:s={VIDEO_W}x{VIDEO_H}:fps=30,"
        f"fade=t=in:st=0:d=0.2,"
        f"fade=t=out:st={duration - 0.2:.2f}:d=0.2"
    )
    _run([
        "ffmpeg", "-y",
        "-loop", "1", "-i", image_path,
        "-t", str(duration),
        "-vf", vf,
        "-c:v", "libx264", "-preset", "fast",
        "-crf", "22", "-pix_fmt", "yuv420p", "-r", "30",
        output_path,
    ])


def _concat_clips(clip_paths: list[str], output_path: str):
    with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
        for p in clip_paths:
            f.write(f"file '{os.path.abspath(p)}'\n")
        list_path = f.name
    try:
        _run([
            "ffmpeg", "-y",
            "-f", "concat", "-safe", "0",
            "-i", list_path, "-c", "copy", output_path,
        ])
    finally:
        os.unlink(list_path)


def _mix_audio_to_video(
    video_path: str,
    tts_segments: list[dict],
    bgm_path: str | None,
    total_duration: float,
    output_path: str,
):
    inputs       = ["-i", video_path]
    filter_parts = []
    audio_labels = []

    for i, seg in enumerate(tts_segments):
        inputs += ["-i", seg["path"]]
        idx      = i + 1
        delay_ms = int(seg["start"] * 1000)
        filter_parts.append(
            f"[{idx}:a]adelay={delay_ms}|{delay_ms},"
            f"apad=whole_dur={total_duration}[tts{i}]"
        )
        audio_labels.append(f"[tts{i}]")

    bgm_idx = len(tts_segments) + 1
    has_bgm = bgm_path and Path(bgm_path).exists()
    if has_bgm:
        inputs += ["-i", bgm_path]
        filter_parts.append(
            f"[{bgm_idx}:a]aloop=loop=-1:size=2e+09,"
            f"atrim=0:{total_duration},volume={BGM_VOLUME}[bgm]"
        )
        audio_labels.append("[bgm]")

    n_audio = len(audio_labels)
    if n_audio == 0:
        filter_parts.append(f"aevalsrc=0:duration={total_duration}[silent]")
        mix_label         = "[silent]"
        final_audio_filter = ";".join(filter_parts)
    elif n_audio == 1:
        mix_label         = audio_labels[0]
        final_audio_filter = ";".join(filter_parts)
    else:
        joined             = "".join(audio_labels)
        filter_parts.append(
            f"{joined}amix=inputs={n_audio}:duration=first:normalize=0[aout]"
        )
        mix_label         = "[aout]"
        final_audio_filter = ";".join(filter_parts)

    _run([
        "ffmpeg", "-y",
        *inputs,
        "-filter_complex", final_audio_filter,
        "-map", "0:v", "-map", mix_label,
        "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
        "-shortest", output_path,
    ])


# ════════════════════════════════════════════════════════════════════════════
# ContentAdapter 연동: 쇼츠 스크립트 품질 향상 프롬프트 주입
# ════════════════════════════════════════════════════════════════════════════

def enhance_script_with_hook_formula(script: list[dict], mode: str) -> list[dict]:
    """
    Gemini가 생성한 스크립트에 고성과 공식을 적용합니다.
    - 첫 슬라이드: 훅 강화 (질문/충격 요법)
    - 마지막 슬라이드: CTA + 루프 유도 문구 추가
    - 중간 슬라이드: 핵심만 남기고 문장 압축
    """
    if not script:
        return script

    enhanced = []
    for i, slide in enumerate(script):
        s = dict(slide)
        is_first = (i == 0)
        is_last  = (i == len(script) - 1)

        if is_first:
            # 훅 슬라이드: 제목에 질문 요소 추가
            title = s.get("title", "")
            if not any(c in title for c in ["?", "!", "왜", "어디", "어떻게"]):
                s["title"] = title + "?"
            body = s.get("body", "")
            # 본문 압축 (30자 이내)
            if len(body) > 40:
                s["body"] = body[:38] + "…"

        elif is_last:
            # CTA 슬라이드: 루프 유도 멘트 추가
            body = s.get("body", "")
            mode_label = "오늘 밤" if mode == "evening" else "지금 바로"
            if "seedsup" not in body and "블로그" not in body:
                s["body"] = body + f" {mode_label} 전체 분석 확인하세요."
        else:
            # 중간 슬라이드: 본문 35자 이내로 압축
            body = s.get("body", "")
            if len(body) > 45:
                s["body"] = body[:43] + "…"

        enhanced.append(s)

    return enhanced


# ════════════════════════════════════════════════════════════════════════════
# VideoGenerator 메인 클래스
# ════════════════════════════════════════════════════════════════════════════

class VideoGenerator:
    def __init__(self, output_dir: str = OUTPUT_DIR):
        self.output_dir = output_dir
        os.makedirs(output_dir, exist_ok=True)
        self.font_bold    = _get_font_path(bold=True)
        self.font_regular = _get_font_path(bold=False)
        logger.info(f"폰트: bold={self.font_bold}, regular={self.font_regular}")

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

        # 고성과 공식 적용
        script = enhance_script_with_hook_formula(script, mode)

        theme    = THEMES.get(mode, THEMES["morning"])
        keywords = bg_keywords or KEYWORD_MAP.get(mode, KEYWORD_MAP["morning"])
        final_output = os.path.join(self.output_dir, filename)

        with tempfile.TemporaryDirectory(prefix="shorts_") as tmp_str:
            tmp = Path(tmp_str)

            # 1. 배경 이미지 다운로드
            bg_img_path = None
            if thumbnail_url:
                dest_bg = tmp / "background.jpg"
                try:
                    req = urllib.request.Request(
                        thumbnail_url, headers={"User-Agent": "Mozilla/5.0"})
                    with urllib.request.urlopen(req, timeout=15) as resp:
                        dest_bg.write_bytes(resp.read())
                    Image.open(dest_bg).verify()
                    bg_img_path = dest_bg
                except Exception as e:
                    logger.warning(f"썸네일 다운로드 실패, Unsplash 사용: {e}")
            if bg_img_path is None:
                dest_bg = tmp / "background.jpg"
                ok = _download_bg_image(keywords, dest_bg)
                bg_img_path = dest_bg if ok else None

            # 2. BGM 다운로드
            bgm_path = tmp / "bgm.mp3"
            bgm_ok   = _download_bgm(mode, bgm_path)
            bgm_path_str = str(bgm_path) if bgm_ok else None

            # 3. 슬라이드별 TTS + 이미지 생성
            slide_clips   = []
            tts_segments  = []
            current_time  = 0.0
            total_slides  = len(script)

            for i, slide_data in enumerate(script, 1):
                logger.info(f"슬라이드 {i}/{total_slides} 처리 중...")

                is_hook = (i == 1)
                is_cta  = (i == total_slides)

                # TTS 텍스트 (훅 슬라이드: 더 강렬한 억양 유도)
                if is_hook:
                    tts_text = f"{slide_data.get('title', '')}! {slide_data.get('body', '')}"
                elif is_cta:
                    tts_text = f"{slide_data.get('title', '')}. {slide_data.get('body', '')} 전체 분석은 링크에서!"
                else:
                    tts_text = f"{slide_data.get('title', '')}. {slide_data.get('body', '')}"

                tts_path = str(tmp / f"tts_{i:02d}.mp3")
                tts_ok   = _generate_tts(tts_text, tts_path)

                if tts_ok:
                    tts_dur   = _get_audio_duration(tts_path)
                    # 고성과 공식: MAX 5.5초 엄수
                    slide_dur = max(MIN_SLIDE_SEC, min(MAX_SLIDE_SEC, tts_dur + 0.5))
                else:
                    tts_dur   = 0.0
                    slide_dur = SLIDE_DURATION

                # 슬라이드 이미지 생성
                slide_img  = _make_slide_image(
                    slide_data    = slide_data,
                    theme         = theme,
                    slide_num     = i,
                    total         = total_slides,
                    bg_image_path = str(bg_img_path) if bg_img_path else None,
                    blog_url      = blog_url,
                    font_bold     = self.font_bold,
                    font_regular  = self.font_regular,
                    is_hook       = is_hook,
                    is_cta        = is_cta,
                )
                img_path  = str(tmp / f"slide_{i:02d}.png")
                slide_img.save(img_path, "PNG")

                clip_path = str(tmp / f"clip_{i:02d}.mp4")
                _image_to_video_clip(img_path, slide_dur, clip_path)
                slide_clips.append(clip_path)

                if tts_ok:
                    tts_segments.append({
                        "start":    current_time + 0.25,   # 공백 최소화
                        "path":     tts_path,
                        "duration": tts_dur,
                    })

                current_time += slide_dur

            total_duration = current_time
            logger.info(f"총 영상 길이: {total_duration:.1f}초")

            # 4. 비디오 클립 합치기
            logger.info("비디오 클립 합치는 중...")
            silent_video = str(tmp / "silent_video.mp4")
            _concat_clips(slide_clips, silent_video)

            # 5. 오디오 믹싱
            logger.info("오디오 믹싱 중...")
            _mix_audio_to_video(
                video_path     = silent_video,
                tts_segments   = tts_segments,
                bgm_path       = bgm_path_str,
                total_duration = total_duration,
                output_path    = final_output,
            )

            logger.info(f"영상 생성 완료: {final_output} ({total_duration:.1f}초)")
            return final_output

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
            return self.generate(
                script=script, mode=mode, filename=filename,
                thumbnail_url=thumbnail_url, blog_url=blog_url,
                bg_keywords=bg_keywords,
            )
        except Exception as e:
            logger.error(f"고급 영상 생성 실패, 폴백: {e}")
            return self._fallback_generate(script, mode, filename, thumbnail_url, blog_url)

    def _fallback_generate(self, script, mode, filename, thumbnail_url, blog_url) -> str:
        logger.info("폴백 영상 생성 시작...")
        try:
            from moviepy.editor import ImageClip, concatenate_videoclips
        except ImportError:
            raise RuntimeError("moviepy도 설치되지 않음. 영상 생성 불가.")

        theme  = THEMES.get(mode, THEMES["morning"])
        output = os.path.join(self.output_dir, filename)
        clips  = []

        for i, slide_data in enumerate(script, 1):
            slide_img = _make_slide_image(
                slide_data=slide_data, theme=theme,
                slide_num=i, total=len(script),
                bg_image_path=None, blog_url=blog_url,
                font_bold=self.font_bold, font_regular=self.font_regular,
                is_hook=(i == 1), is_cta=(i == len(script)),
            )
            with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
                tmp_path = f.name
            slide_img.save(tmp_path)
            clips.append(ImageClip(tmp_path, duration=SLIDE_DURATION))

        final = concatenate_videoclips(clips, method="compose")
        final.write_videofile(
            output, fps=30, codec="libx264",
            audio=False, preset="ultrafast", logger=None,
        )
        return output
