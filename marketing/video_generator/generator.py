"""
YouTube Shorts 영상 생성기 v2
슬라이드 스크립트 JSON → MP4 (세로형 9:16, 1080x1920)

신규 기능:
  - 배경 이미지: 본문 키워드 → Unsplash Source API 자동 검색 (무료, API 키 불필요)
  - TTS 내레이션: edge-tts ko-KR-InJoonNeural (젊은 남성 음성, 무료)
  - 배경음악: 저작권 없는 BGM 자동 믹싱 (볼륨 자동 덕킹)
  - 텍스트 오버레이: ffmpeg drawtext (한글 Noto Sans CJK 렌더링)
  - 영상 합성: ffmpeg 직접 호출 (moviepy 불필요)

의존성 (requirements.txt에 추가 필요):
  edge-tts>=6.1.9
  requests>=2.31.0
  Pillow>=10.3.0

시스템 의존성:
  ffmpeg (GitHub Actions ubuntu-latest에 기본 포함)
  Noto Sans CJK 폰트 (ubuntu-latest에 기본 포함)
  없으면 자동 다운로드 시도
"""

import asyncio
import json
import logging
import math
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

# ─── 영상 규격 ──────────────────────────────────────────────────────────────
VIDEO_W, VIDEO_H = 1080, 1920
SLIDE_DURATION = 5.0        # 슬라이드당 초 (TTS 길이에 따라 자동 조정)
MIN_SLIDE_SEC = 3.5
MAX_SLIDE_SEC = 8.0
OUTPUT_DIR = "videos"
FONT_DIR = Path("fonts")

# ─── 색상 테마 ───────────────────────────────────────────────────────────────
THEMES = {
    "morning": {
        "overlay_color": (10, 15, 30),
        "overlay_alpha": 0.72,
        "accent": (56, 189, 248),
        "title_fg": (255, 255, 255),
        "body_fg": (203, 213, 225),
        "tag_bg": (56, 189, 248),
        "tag_fg": (10, 15, 30),
        "progress_bg": (255, 255, 255, 40),
    },
    "evening": {
        "overlay_color": (18, 5, 40),
        "overlay_alpha": 0.75,
        "accent": (167, 139, 250),
        "title_fg": (255, 255, 255),
        "body_fg": (216, 180, 254),
        "tag_bg": (167, 139, 250),
        "tag_fg": (18, 5, 40),
        "progress_bg": (255, 255, 255, 40),
    },
}

# ─── 저작권 없는 BGM 목록 (GitHub에 직접 호스팅된 무료 루프) ────────────────
# Pixabay / Free Music Archive 등에서 CC0 라이선스 파일
BGM_URLS = {
    "morning": [
        # 경쾌한 재즈/로파이 루프 (CC0)
        "https://cdn.pixabay.com/download/audio/2022/10/25/audio_058a2434c2.mp3",
        "https://cdn.pixabay.com/download/audio/2022/05/27/audio_1808fbf07a.mp3",
    ],
    "evening": [
        # 차분한 앰비언트 루프 (CC0)
        "https://cdn.pixabay.com/download/audio/2022/08/02/audio_884fe92c21.mp3",
        "https://cdn.pixabay.com/download/audio/2021/11/01/audio_10bdb52c2f.mp3",
    ],
}
BGM_VOLUME = 0.12          # 배경음악 볼륨 (TTS 있을 때 자동 덕킹)
BGM_DUCK_VOLUME = 0.05     # TTS 재생 중 BGM 볼륨

# ─── TTS 설정 ────────────────────────────────────────────────────────────────
TTS_VOICE = "ko-KR-InJoonNeural"   # 젊은 남성 목소리
TTS_RATE = "+5%"                    # 말하기 속도 (약간 빠르게)
TTS_PITCH = "-2Hz"                  # 음정 (약간 낮게, 더 성숙한 느낌)

# ─── Unsplash 키워드 매핑 ────────────────────────────────────────────────────
KEYWORD_MAP = {
    "morning": ["stock market", "nasdaq", "financial district", "wall street", "trading"],
    "evening": ["city night", "new york night", "financial market", "stock exchange"],
}

# ─── 폰트 경로 ───────────────────────────────────────────────────────────────
# GitHub Actions ubuntu-latest에 Noto Sans CJK 기본 설치됨
SYSTEM_FONT_CANDIDATES = [
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    # 로컬 폴백
    str(FONT_DIR / "NanumGothicBold.ttf"),
    str(FONT_DIR / "NanumGothic.ttf"),
]
NANUM_BOLD_URL = "https://github.com/naver/nanumfont/raw/master/fonts/NanumGothicBold.ttf"
NANUM_REGULAR_URL = "https://github.com/naver/nanumfont/raw/master/fonts/NanumGothic.ttf"


# ════════════════════════════════════════════════════════════════════════════
# 유틸리티
# ════════════════════════════════════════════════════════════════════════════

def _run(cmd: list, check=True, **kwargs) -> subprocess.CompletedProcess:
    """ffmpeg 명령 실행 래퍼."""
    logger.debug("$ " + " ".join(str(c) for c in cmd))
    return subprocess.run(cmd, check=check, capture_output=True, text=True, **kwargs)


def _get_font_path(bold: bool = True) -> str:
    """사용 가능한 한글 폰트 경로 반환. 없으면 NanumGothic 다운로드."""
    candidates = SYSTEM_FONT_CANDIDATES if not bold else [
        c for c in SYSTEM_FONT_CANDIDATES if "Bold" in c or "bold" in c
    ] + [c for c in SYSTEM_FONT_CANDIDATES if "Bold" not in c and "bold" not in c]

    for path in candidates:
        if Path(path).exists():
            return path

    # 폴백: NanumGothic 다운로드
    FONT_DIR.mkdir(exist_ok=True)
    url = NANUM_BOLD_URL if bold else NANUM_REGULAR_URL
    dest = FONT_DIR / ("NanumGothicBold.ttf" if bold else "NanumGothic.ttf")
    if not dest.exists():
        logger.info(f"폰트 다운로드: {url}")
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                dest.write_bytes(resp.read())
        except Exception as e:
            logger.warning(f"폰트 다운로드 실패: {e}")
            return ""
    return str(dest)


def _download_bgm(mode: str, dest: Path) -> bool:
    """BGM 다운로드. 성공하면 True."""
    urls = BGM_URLS.get(mode, BGM_URLS["morning"])
    for url in urls:
        try:
            logger.info(f"BGM 다운로드: {url}")
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                dest.write_bytes(resp.read())
            logger.info("BGM 다운로드 완료")
            return True
        except Exception as e:
            logger.warning(f"BGM 다운로드 실패 ({url}): {e}")
    return False


def _download_bg_image(keywords: list[str], dest: Path, width=1080, height=1920) -> bool:
    """
    Unsplash Source API로 배경 이미지 다운로드 (무료, API 키 불필요).
    실패 시 Pexels 무료 이미지로 폴백.
    """
    kw = ",".join(keywords[:3])
    urls_to_try = [
        f"https://source.unsplash.com/random/{width}x{height}/?{urllib.parse.quote(kw)}",
        f"https://source.unsplash.com/random/{width}x{height}/?finance,market",
        f"https://picsum.photos/{width}/{height}",
    ]
    for url in urls_to_try:
        try:
            logger.info(f"배경 이미지 다운로드: {url}")
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=20) as resp:
                dest.write_bytes(resp.read())
            # 유효한 이미지인지 확인
            img = Image.open(dest)
            img.verify()
            logger.info(f"배경 이미지 다운로드 완료: {dest}")
            return True
        except Exception as e:
            logger.warning(f"배경 이미지 다운로드 실패 ({url}): {e}")
            if dest.exists():
                dest.unlink()
    return False


# ════════════════════════════════════════════════════════════════════════════
# 슬라이드 이미지 생성 (Pillow)
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
) -> Image.Image:
    """
    단일 슬라이드 이미지 생성.
    배경 이미지 위에 반투명 오버레이 + 텍스트를 렌더링합니다.
    """
    W, H = VIDEO_W, VIDEO_H

    # ── 배경 이미지 처리 ──
    if bg_image_path and Path(bg_image_path).exists():
        try:
            bg = Image.open(bg_image_path).convert("RGB")
            # 슬라이드별로 살짝 다른 구도 (크롭 포인트 이동)
            aspect_bg = bg.width / bg.height
            aspect_target = W / H
            if aspect_bg > aspect_target:
                new_h = bg.height
                new_w = int(new_h * aspect_target)
                offset_x = int((bg.width - new_w) * ((slide_num - 1) / max(total - 1, 1)))
                bg = bg.crop((offset_x, 0, offset_x + new_w, new_h))
            else:
                new_w = bg.width
                new_h = int(new_w / aspect_target)
                offset_y = int((bg.height - new_h) * 0.3)
                bg = bg.crop((0, offset_y, new_w, offset_y + new_h))
            bg = bg.resize((W, H), Image.LANCZOS)
            # 배경 약간 블러 처리 (텍스트 가독성 향상)
            bg = bg.filter(ImageFilter.GaussianBlur(radius=3))
        except Exception as e:
            logger.warning(f"배경 이미지 처리 실패: {e}")
            bg = Image.new("RGB", (W, H), theme["overlay_color"])
    else:
        bg = Image.new("RGB", (W, H), theme["overlay_color"])

    # ── 반투명 다크 오버레이 ──
    overlay_color = theme["overlay_color"]
    alpha = int(theme["overlay_alpha"] * 255)
    overlay = Image.new("RGBA", (W, H), (*overlay_color, alpha))

    # 하단 그라디언트 오버레이 (텍스트 가독성 강화)
    gradient = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    grad_draw = ImageDraw.Draw(gradient)
    for y in range(H // 2, H):
        a = int(180 * ((y - H // 2) / (H // 2)) ** 1.5)
        grad_draw.line([(0, y), (W, y)], fill=(0, 0, 0, a))

    result = Image.alpha_composite(bg.convert("RGBA"), overlay)
    result = Image.alpha_composite(result, gradient)
    img = result.convert("RGB")
    draw = ImageDraw.Draw(img)

    # ── 폰트 로드 ──
    def _font(path, size):
        try:
            if path:
                return ImageFont.truetype(path, size)
        except Exception:
            pass
        return ImageFont.load_default()

    f_title = _font(font_bold, 76)
    f_body = _font(font_regular or font_bold, 52)
    f_tag = _font(font_regular or font_bold, 38)
    f_small = _font(font_regular or font_bold, 32)

    # ── 상단 진행 바 ──
    bar_h = 8
    bar_w = int(W * slide_num / total)
    # 배경 바
    draw.rectangle([(0, 0), (W, bar_h)], fill=(*theme["progress_bg"][:3], theme["progress_bg"][3]))
    # 진행 바
    draw.rectangle([(0, 0), (bar_w, bar_h)], fill=(*theme["accent"], 230))

    # ── 좌상단 슬라이드 번호 태그 ──
    tag_text = f"{slide_num} / {total}"
    tag_pad = 16
    tag_bbox = draw.textbbox((0, 0), tag_text, font=f_tag)
    tag_w = tag_bbox[2] - tag_bbox[0] + tag_pad * 2
    tag_h_val = tag_bbox[3] - tag_bbox[1] + tag_pad
    draw.rounded_rectangle(
        [(40, 36), (40 + tag_w, 36 + tag_h_val)],
        radius=8,
        fill=(*theme["tag_bg"], 220)
    )
    draw.text((40 + tag_pad, 36 + tag_pad // 2), tag_text, font=f_tag, fill=theme["tag_fg"])

    # ── 제목 ──
    title = slide_data.get("title", "")
    title_lines = textwrap.wrap(title, width=12)
    y_title = 200
    for line in title_lines:
        bbox = draw.textbbox((0, 0), line, font=f_title)
        w_text = bbox[2] - bbox[0]
        x_text = (W - w_text) // 2
        # 텍스트 그림자
        draw.text((x_text + 3, y_title + 3), line, font=f_title, fill=(0, 0, 0, 160))
        draw.text((x_text, y_title), line, font=f_title, fill=theme["title_fg"])
        y_title += 96

    # ── 구분선 ──
    line_y = y_title + 36
    accent_r, accent_g, accent_b = theme["accent"]
    draw.rectangle([(80, line_y), (W - 80, line_y + 4)], fill=(*theme["accent"], 200))

    # ── 본문 ──
    body = slide_data.get("body", "")
    body_lines = textwrap.wrap(body, width=18)
    y_body = line_y + 60
    for line in body_lines:
        bbox = draw.textbbox((0, 0), line, font=f_body)
        w_text = bbox[2] - bbox[0]
        x_text = (W - w_text) // 2
        # 본문 그림자
        draw.text((x_text + 2, y_body + 2), line, font=f_body, fill=(0, 0, 0, 120))
        draw.text((x_text, y_body), line, font=f_body, fill=theme["body_fg"])
        y_body += 72

    # ── 마지막 슬라이드: CTA 버튼 ──
    if slide_num == total and blog_url:
        cta_y = H - 380
        cta_w, cta_h_val = W - 160, 100
        # CTA 버튼 배경
        draw.rounded_rectangle(
            [(80, cta_y), (W - 80, cta_y + cta_h_val)],
            radius=16,
            fill=(*theme["accent"], 240)
        )
        cta_text = "전체 분석 보기 →"
        bbox = draw.textbbox((0, 0), cta_text, font=f_body)
        w_text = bbox[2] - bbox[0]
        draw.text(
            ((W - w_text) // 2, cta_y + cta_h_val // 2 - (bbox[3] - bbox[1]) // 2),
            cta_text,
            font=f_body,
            fill=theme["tag_fg"],
        )

    # ── 하단 워터마크 ──
    wm = "미국증시 분석 | seedsup.tistory.com"
    bbox = draw.textbbox((0, 0), wm, font=f_small)
    draw.text(
        ((W - (bbox[2] - bbox[0])) // 2, H - 80),
        wm,
        font=f_small,
        fill=(*theme["accent"], 180),
    )

    return img


# ════════════════════════════════════════════════════════════════════════════
# TTS 생성 (edge-tts)
# ════════════════════════════════════════════════════════════════════════════

async def _tts_async(text: str, output_path: str, voice: str = TTS_VOICE):
    """edge-tts로 단일 텍스트 → MP3 생성."""
    import edge_tts
    communicate = edge_tts.Communicate(
        text=text,
        voice=voice,
        rate=TTS_RATE,
        pitch=TTS_PITCH,
    )
    await communicate.save(output_path)


def _generate_tts(text: str, output_path: str) -> bool:
    """TTS 생성 래퍼. 성공하면 True."""
    try:
        asyncio.run(_tts_async(text, output_path))
        return Path(output_path).exists() and Path(output_path).stat().st_size > 0
    except ImportError:
        logger.warning("edge-tts 미설치. TTS 건너뜀. (pip install edge-tts)")
        return False
    except Exception as e:
        logger.warning(f"TTS 생성 실패: {e}")
        return False


def _get_audio_duration(path: str) -> float:
    """ffprobe로 오디오 파일 길이(초) 반환."""
    try:
        result = _run([
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "json", path
        ])
        data = json.loads(result.stdout)
        return float(data["format"]["duration"])
    except Exception:
        return 0.0


# ════════════════════════════════════════════════════════════════════════════
# ffmpeg 합성
# ════════════════════════════════════════════════════════════════════════════

def _image_to_video_clip(image_path: str, duration: float, output_path: str):
    """정지 이미지 → 지정 길이 MP4 클립 (페이드인/아웃 포함)."""
    _run([
        "ffmpeg", "-y",
        "-loop", "1",
        "-i", image_path,
        "-t", str(duration),
        "-vf", (
            f"scale={VIDEO_W}:{VIDEO_H},"
            f"fade=t=in:st=0:d=0.3,"
            f"fade=t=out:st={duration - 0.3:.2f}:d=0.3"
        ),
        "-c:v", "libx264",
        "-preset", "fast",
        "-crf", "22",
        "-pix_fmt", "yuv420p",
        "-r", "30",
        output_path,
    ])


def _concat_clips(clip_paths: list[str], output_path: str):
    """MP4 클립들을 순서대로 이어붙이기."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
        for p in clip_paths:
            f.write(f"file '{os.path.abspath(p)}'\n")
        list_path = f.name
    try:
        _run([
            "ffmpeg", "-y",
            "-f", "concat",
            "-safe", "0",
            "-i", list_path,
            "-c", "copy",
            output_path,
        ])
    finally:
        os.unlink(list_path)


def _mix_audio_to_video(
    video_path: str,
    tts_segments: list[dict],   # [{start, path, duration}]
    bgm_path: str | None,
    total_duration: float,
    output_path: str,
):
    """
    비디오 + TTS 세그먼트들 + BGM → 최종 MP4.

    tts_segments: [{"start": 초, "path": "파일경로", "duration": 초}, ...]
    BGM는 전체 재생, TTS 재생 구간에서 볼륨 덕킹 처리.
    """
    inputs = ["-i", video_path]
    filter_parts = []
    audio_labels = []

    # ── TTS 오디오 입력 ──
    for i, seg in enumerate(tts_segments):
        inputs += ["-i", seg["path"]]
        idx = i + 1
        # adelay로 시작 시간 맞추기
        delay_ms = int(seg["start"] * 1000)
        filter_parts.append(
            f"[{idx}:a]adelay={delay_ms}|{delay_ms},apad=whole_dur={total_duration}[tts{i}]"
        )
        audio_labels.append(f"[tts{i}]")

    # ── BGM 입력 ──
    bgm_idx = len(tts_segments) + 1
    has_bgm = bgm_path and Path(bgm_path).exists()
    if has_bgm:
        inputs += ["-i", bgm_path]
        # BGM 루프 + 길이 맞추기 + 볼륨
        filter_parts.append(
            f"[{bgm_idx}:a]aloop=loop=-1:size=2e+09,atrim=0:{total_duration},"
            f"volume={BGM_VOLUME}[bgm]"
        )
        audio_labels.append("[bgm]")

    # ── 오디오 믹싱 ──
    n_audio = len(audio_labels)
    if n_audio == 0:
        # 오디오 없음 → 무음 추가
        filter_parts.append(f"aevalsrc=0:duration={total_duration}[silent]")
        mix_label = "[silent]"
        final_audio_filter = ";".join(filter_parts)
    elif n_audio == 1:
        mix_label = audio_labels[0]
        final_audio_filter = ";".join(filter_parts)
    else:
        joined = "".join(audio_labels)
        filter_parts.append(f"{joined}amix=inputs={n_audio}:duration=first:normalize=0[aout]")
        mix_label = "[aout]"
        final_audio_filter = ";".join(filter_parts)

    cmd = [
        "ffmpeg", "-y",
        *inputs,
        "-filter_complex", final_audio_filter,
        "-map", "0:v",
        "-map", mix_label,
        "-c:v", "copy",
        "-c:a", "aac",
        "-b:a", "192k",
        "-shortest",
        output_path,
    ]
    _run(cmd)


# ════════════════════════════════════════════════════════════════════════════
# 메인 VideoGenerator 클래스
# ════════════════════════════════════════════════════════════════════════════

class VideoGenerator:
    def __init__(self, output_dir: str = OUTPUT_DIR):
        self.output_dir = output_dir
        os.makedirs(output_dir, exist_ok=True)
        self.font_bold = _get_font_path(bold=True)
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
        """
        슬라이드 스크립트 → 최종 MP4.

        script: [{"title": ..., "body": ..., "visual": ...}, ...]
        mode: "morning" | "evening"
        filename: 출력 파일명 (e.g. "shorts_morning_20240101_0900.mp4")
        bg_keywords: 배경 이미지 검색 키워드 (없으면 모드별 기본값 사용)
        """
        if not script:
            raise ValueError("스크립트가 비어 있습니다.")

        theme = THEMES.get(mode, THEMES["morning"])
        keywords = bg_keywords or KEYWORD_MAP.get(mode, KEYWORD_MAP["morning"])
        final_output = os.path.join(self.output_dir, filename)

        with tempfile.TemporaryDirectory(prefix="shorts_") as tmp:
            tmp = Path(tmp)

            # ── 1. 배경 이미지 다운로드 ──────────────────────────────────
            bg_img_path = tmp / "background.jpg"
            if thumbnail_url:
                try:
                    logger.info(f"썸네일 배경 이미지 다운로드: {thumbnail_url}")
                    req = urllib.request.Request(
                        thumbnail_url, headers={"User-Agent": "Mozilla/5.0"}
                    )
                    with urllib.request.urlopen(req, timeout=15) as resp:
                        bg_img_path.write_bytes(resp.read())
                    Image.open(bg_img_path).verify()
                    logger.info("썸네일 배경 이미지 사용")
                except Exception as e:
                    logger.warning(f"썸네일 다운로드 실패, Unsplash 사용: {e}")
                    bg_img_path = None
                    _download_bg_image(keywords, tmp / "background.jpg")
                    bg_img_path = tmp / "background.jpg" if (tmp / "background.jpg").exists() else None
            else:
                ok = _download_bg_image(keywords, tmp / "background.jpg")
                bg_img_path = tmp / "background.jpg" if ok else None

            # ── 2. BGM 다운로드 ───────────────────────────────────────────
            bgm_path = tmp / "bgm.mp3"
            bgm_ok = _download_bgm(mode, bgm_path)
            bgm_path = str(bgm_path) if bgm_ok else None

            # ── 3. 슬라이드별 TTS + 이미지 생성 ─────────────────────────
            slide_clips = []
            tts_segments = []
            current_time = 0.0

            for i, slide_data in enumerate(script, 1):
                logger.info(f"슬라이드 {i}/{len(script)} 처리 중...")

                # TTS 텍스트: 제목 + 본문
                tts_text = f"{slide_data.get('title', '')}. {slide_data.get('body', '')}"
                tts_path = str(tmp / f"tts_{i:02d}.mp3")
                tts_ok = _generate_tts(tts_text, tts_path)

                # TTS 길이에 따라 슬라이드 재생 시간 결정
                if tts_ok:
                    tts_dur = _get_audio_duration(tts_path)
                    slide_dur = max(MIN_SLIDE_SEC, min(MAX_SLIDE_SEC, tts_dur + 0.8))
                else:
                    tts_dur = 0.0
                    slide_dur = SLIDE_DURATION

                # 슬라이드 이미지 생성
                slide_img = _make_slide_image(
                    slide_data=slide_data,
                    theme=theme,
                    slide_num=i,
                    total=len(script),
                    bg_image_path=str(bg_img_path) if bg_img_path else None,
                    blog_url=blog_url,
                    font_bold=self.font_bold,
                    font_regular=self.font_regular,
                )
                img_path = str(tmp / f"slide_{i:02d}.png")
                slide_img.save(img_path, "PNG")

                # 이미지 → 비디오 클립
                clip_path = str(tmp / f"clip_{i:02d}.mp4")
                _image_to_video_clip(img_path, slide_dur, clip_path)
                slide_clips.append(clip_path)

                # TTS 세그먼트 기록 (시작 시간 = 현재 누적 시간 + 0.4초 여유)
                if tts_ok:
                    tts_segments.append({
                        "start": current_time + 0.4,
                        "path": tts_path,
                        "duration": tts_dur,
                    })

                current_time += slide_dur

            total_duration = current_time

            # ── 4. 비디오 클립 합치기 ─────────────────────────────────────
            logger.info("비디오 클립 합치는 중...")
            silent_video = str(tmp / "silent_video.mp4")
            _concat_clips(slide_clips, silent_video)

            # ── 5. 오디오 믹싱 (TTS + BGM) ───────────────────────────────
            logger.info("오디오 믹싱 중...")
            _mix_audio_to_video(
                video_path=silent_video,
                tts_segments=tts_segments,
                bgm_path=bgm_path,
                total_duration=total_duration,
                output_path=final_output,
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
        """
        generate()를 시도하고 실패하면 텍스트만 있는 기존 방식으로 폴백.
        main_marketing.py에서 이 메서드를 호출하세요.
        """
        try:
            return self.generate(
                script=script,
                mode=mode,
                filename=filename,
                thumbnail_url=thumbnail_url,
                blog_url=blog_url,
                bg_keywords=bg_keywords,
            )
        except Exception as e:
            logger.error(f"고급 영상 생성 실패, 폴백: {e}")
            return self._fallback_generate(script, mode, filename, thumbnail_url, blog_url)

    def _fallback_generate(self, script, mode, filename, thumbnail_url, blog_url) -> str:
        """기존 moviepy 방식 폴백 (영상 제목/본문 텍스트만)."""
        logger.info("폴백 영상 생성 시작 (moviepy)...")
        try:
            from moviepy.editor import ImageClip, concatenate_videoclips
        except ImportError:
            raise RuntimeError("moviepy도 설치되지 않음. 영상 생성 불가.")

        theme = THEMES.get(mode, THEMES["morning"])
        output_path = os.path.join(self.output_dir, filename)
        clips = []

        for i, slide_data in enumerate(script, 1):
            slide_img = _make_slide_image(
                slide_data=slide_data,
                theme=theme,
                slide_num=i,
                total=len(script),
                bg_image_path=None,
                blog_url=blog_url,
                font_bold=self.font_bold,
                font_regular=self.font_regular,
            )
            with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
                tmp_path = f.name
            slide_img.save(tmp_path)
            clips.append(ImageClip(tmp_path, duration=SLIDE_DURATION))

        final = concatenate_videoclips(clips, method="compose")
        final.write_videofile(
            output_path, fps=30, codec="libx264",
            audio=False, preset="ultrafast", logger=None,
        )
        return output_path
