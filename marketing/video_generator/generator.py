"""
YouTube Shorts / TikTok 영상 생성기
슬라이드 스크립트 JSON → MP4 (세로형 9:16, 1080x1920)

의존성: Pillow, moviepy==1.0.3, requests
무료 폰트: NanumGothic (Google Fonts) 자동 다운로드
"""

import os
import logging
import requests
import textwrap
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

# moviepy 1.x / 2.x 모두 호환
try:
    from moviepy.editor import ImageClip, concatenate_videoclips  # 1.x
    from moviepy.video.fx.fadein import fadein
    from moviepy.video.fx.fadeout import fadeout

    def _apply_fade(clip, duration):
        return fadeout(fadein(clip, duration), duration)

except ImportError:
    from moviepy import ImageClip, concatenate_videoclips          # 2.x

    def _apply_fade(clip, duration):
        try:
            from moviepy.video.fx import FadeIn, FadeOut
            return clip.with_effects([FadeIn(duration), FadeOut(duration)])
        except Exception:
            return clip

logger = logging.getLogger(__name__)

# 영상 규격
VIDEO_W, VIDEO_H = 1080, 1920
SLIDE_DURATION = 4.5   # 슬라이드당 초
FADE_DURATION = 0.3
OUTPUT_DIR = "videos"

# 모드별 색상 테마
THEMES = {
    "morning": {
        "bg": (15, 23, 42),
        "accent": (56, 189, 248),
        "title_fg": (255, 255, 255),
        "body_fg": (203, 213, 225),
        "overlay": (30, 64, 175, 180),
    },
    "evening": {
        "bg": (23, 7, 48),
        "accent": (167, 139, 250),
        "title_fg": (255, 255, 255),
        "body_fg": (216, 180, 254),
        "overlay": (88, 28, 135, 180),
    },
}

FONT_DIR = Path("fonts")
FONT_URL_BOLD = "https://github.com/naver/nanumfont/raw/master/fonts/NanumGothicBold.ttf"
FONT_URL_REGULAR = "https://github.com/naver/nanumfont/raw/master/fonts/NanumGothic.ttf"


def _ensure_fonts():
    """NanumGothic 폰트를 자동으로 다운로드합니다."""
    FONT_DIR.mkdir(exist_ok=True)
    bold_path = FONT_DIR / "NanumGothicBold.ttf"
    regular_path = FONT_DIR / "NanumGothic.ttf"

    for path, url in [(bold_path, FONT_URL_BOLD), (regular_path, FONT_URL_REGULAR)]:
        if not path.exists():
            logger.info(f"폰트 다운로드 중: {url}")
            try:
                resp = requests.get(url, timeout=30)
                resp.raise_for_status()
                path.write_bytes(resp.content)
                logger.info(f"폰트 저장 완료: {path}")
            except Exception as e:
                logger.warning(f"폰트 다운로드 실패 ({url}): {e}")

    return bold_path, regular_path


def _load_fonts():
    bold_path, regular_path = _ensure_fonts()
    try:
        font_title = ImageFont.truetype(str(bold_path), 72)
        font_body = ImageFont.truetype(str(regular_path), 48)
        font_tag = ImageFont.truetype(str(regular_path), 36)
    except Exception:
        logger.warning("커스텀 폰트 로드 실패. 기본 폰트 사용.")
        font_title = ImageFont.load_default()
        font_body = font_title
        font_tag = font_title
    return font_title, font_body, font_tag


def _make_slide(
    slide_data: dict,
    theme: dict,
    slide_num: int,
    total: int,
    thumbnail_img: Image.Image | None = None,
    blog_url: str = "",
) -> Image.Image:
    """단일 슬라이드 이미지를 생성합니다."""
    img = Image.new("RGB", (VIDEO_W, VIDEO_H), theme["bg"])
    draw = ImageDraw.Draw(img, "RGBA")

    font_title, font_body, font_tag = _load_fonts()

    # 배경 이미지 (썸네일이 있으면 블러 배경으로 사용)
    if thumbnail_img and slide_num == 1:
        try:
            bg = thumbnail_img.copy().convert("RGB")
            bg = bg.resize((VIDEO_W, VIDEO_H), Image.LANCZOS)
            overlay = Image.new("RGBA", (VIDEO_W, VIDEO_H), theme["overlay"])
            img = Image.alpha_composite(bg.convert("RGBA"), overlay).convert("RGB")
            draw = ImageDraw.Draw(img, "RGBA")
        except Exception:
            pass

    # 상단 진행 바
    bar_w = int(VIDEO_W * slide_num / total)
    draw.rectangle([(0, 0), (bar_w, 8)], fill=theme["accent"])

    # 슬라이드 번호
    draw.text((54, 40), f"{slide_num}/{total}", font=font_tag, fill=theme["accent"])

    # 제목
    title = slide_data.get("title", "")
    title_wrapped = textwrap.wrap(title, width=14)
    y_title = 200
    for line in title_wrapped:
        bbox = draw.textbbox((0, 0), line, font=font_title)
        w = bbox[2] - bbox[0]
        draw.text(((VIDEO_W - w) / 2, y_title), line, font=font_title, fill=theme["title_fg"])
        y_title += 90

    # 구분선
    line_y = y_title + 40
    draw.rectangle([(80, line_y), (VIDEO_W - 80, line_y + 4)], fill=theme["accent"])

    # 본문
    body = slide_data.get("body", "")
    body_wrapped = textwrap.wrap(body, width=22)
    y_body = line_y + 80
    for line in body_wrapped:
        bbox = draw.textbbox((0, 0), line, font=font_body)
        w = bbox[2] - bbox[0]
        draw.text(((VIDEO_W - w) / 2, y_body), line, font=font_body, fill=theme["body_fg"])
        y_body += 70

    # 마지막 슬라이드: CTA
    if slide_num == total and blog_url:
        cta_y = VIDEO_H - 300
        draw.rectangle([(80, cta_y), (VIDEO_W - 80, cta_y + 120)], fill=theme["accent"])
        draw.text(
            (VIDEO_W // 2, cta_y + 60),
            "자세히 보기 →",
            font=font_body,
            fill=(15, 23, 42),
            anchor="mm",
        )
        draw.text(
            (VIDEO_W // 2, cta_y + 160),
            "seedsup.tistory.com",
            font=font_tag,
            fill=theme["accent"],
            anchor="mm",
        )

    # 워터마크
    draw.text(
        (VIDEO_W // 2, VIDEO_H - 80),
        "미국증시 분석 | seedsup.tistory.com",
        font=font_tag,
        fill=(*theme["accent"][:3], 180),
        anchor="mm",
    )

    return img


class VideoGenerator:
    def __init__(self, output_dir: str = OUTPUT_DIR):
        self.output_dir = output_dir
        os.makedirs(output_dir, exist_ok=True)

    def generate(
        self,
        script: list[dict],
        mode: str,
        filename: str,
        thumbnail_url: str = "",
        blog_url: str = "",
    ) -> str:
        """슬라이드 스크립트 → MP4 파일 생성."""
        if not script:
            raise ValueError("스크립트가 비어 있습니다.")

        theme = THEMES.get(mode, THEMES["morning"])
        output_path = os.path.join(self.output_dir, filename)

        # 썸네일 이미지 다운로드
        thumbnail_img = None
        if thumbnail_url:
            try:
                from io import BytesIO
                resp = requests.get(thumbnail_url, timeout=10)
                thumbnail_img = Image.open(BytesIO(resp.content))
            except Exception as e:
                logger.warning(f"썸네일 다운로드 실패: {e}")

        logger.info(f"슬라이드 {len(script)}장 영상 생성 중...")

        clips = []
        for i, slide_data in enumerate(script, 1):
            img = _make_slide(
                slide_data=slide_data,
                theme=theme,
                slide_num=i,
                total=len(script),
                thumbnail_img=thumbnail_img if i == 1 else None,
                blog_url=blog_url,
            )
            tmp_path = os.path.join(self.output_dir, f"_slide_{i:02d}.png")
            img.save(tmp_path, "PNG")

            clip = ImageClip(tmp_path, duration=SLIDE_DURATION)
            clip = _apply_fade(clip, FADE_DURATION)
            clips.append(clip)

        # 영상 합치기
        final = concatenate_videoclips(clips, method="compose")
        final.write_videofile(
            output_path,
            fps=30,
            codec="libx264",
            audio=False,
            preset="ultrafast",
            ffmpeg_params=["-crf", "23"],
            logger=None,
        )

        # 임시 파일 정리
        for i in range(1, len(script) + 1):
            tmp = os.path.join(self.output_dir, f"_slide_{i:02d}.png")
            if os.path.exists(tmp):
                os.remove(tmp)

        logger.info(f"영상 저장 완료: {output_path}")
        return output_path
