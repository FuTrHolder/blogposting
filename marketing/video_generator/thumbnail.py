"""
SNS 썸네일 생성기
기존 ImageGenerator를 확장해 SNS 비율별 썸네일을 생성합니다.

비율:
  facebook  : 1200×630 (1.91:1)
  threads   : 1080×1080 (1:1)
  shorts    : 1280×720 (16:9 커버)
"""

import os
import logging
import requests
import textwrap
from PIL import Image, ImageDraw, ImageFont
from pathlib import Path
from io import BytesIO
import hashlib
from datetime import datetime

logger = logging.getLogger(__name__)

OUTPUT_DIR = "images"

SIZES = {
    "facebook": (1200, 630),
    "threads": (1080, 1080),
    "instagram": (1080, 1080),   # 피드 정방형
    "instagram_portrait": (1080, 1350),  # 피드 세로 (도달률 높음)
    "shorts_cover": (1280, 720),
}

THEMES = {
    "morning": {
        "bg": (15, 23, 42),
        "accent": (56, 189, 248),
        "title_fg": (255, 255, 255),
        "sub_fg": (148, 163, 184),
    },
    "evening": {
        "bg": (23, 7, 48),
        "accent": (167, 139, 250),
        "title_fg": (255, 255, 255),
        "sub_fg": (196, 181, 253),
    },
}

FONT_DIR = Path("fonts")


def _load_fonts(title_size: int = 52, body_size: int = 32):
    bold_path = FONT_DIR / "NanumGothicBold.ttf"
    regular_path = FONT_DIR / "NanumGothic.ttf"
    try:
        return (
            ImageFont.truetype(str(bold_path), title_size),
            ImageFont.truetype(str(regular_path), body_size),
        )
    except Exception:
        default = ImageFont.load_default()
        return default, default


def _draw_thumbnail(
    width: int,
    height: int,
    title: str,
    blog_url: str,
    mode: str,
    bg_img: Image.Image | None = None,
) -> Image.Image:
    """단일 썸네일 이미지를 생성합니다."""
    theme = THEMES.get(mode, THEMES["morning"])

    if bg_img:
        img = bg_img.copy().convert("RGB").resize((width, height), Image.LANCZOS)
        overlay = Image.new("RGBA", (width, height), (*theme["bg"], 190))
        img = Image.alpha_composite(img.convert("RGBA"), overlay).convert("RGB")
    else:
        img = Image.new("RGB", (width, height), theme["bg"])

    draw = ImageDraw.Draw(img)
    font_title, font_body = _load_fonts(
        title_size=max(36, min(64, width // 18)),
        body_size=max(22, min(38, width // 30)),
    )

    # 좌측 악센트 바
    draw.rectangle([(0, 0), (8, height)], fill=theme["accent"])

    # 제목 (중앙 상단)
    wrapped = textwrap.wrap(title, width=max(12, width // 28))
    total_h = len(wrapped) * (font_title.size + 16)
    y = (height - total_h) // 2 - 30

    for line in wrapped:
        bbox = draw.textbbox((0, 0), line, font=font_title)
        w = bbox[2] - bbox[0]
        x = (width - w) // 2
        # 텍스트 그림자
        draw.text((x + 2, y + 2), line, font=font_title, fill=(0, 0, 0, 120))
        draw.text((x, y), line, font=font_title, fill=theme["title_fg"])
        y += font_title.size + 16

    # 하단 URL 배지
    badge_h = max(44, height // 12)
    draw.rectangle(
        [(0, height - badge_h), (width, height)],
        fill=(*theme["accent"], 230),
    )
    draw.text(
        (width // 2, height - badge_h // 2),
        f"📊 {blog_url}",
        font=font_body,
        fill=theme["bg"],
        anchor="mm",
    )

    return img


class SNSThumbnailGenerator:
    def __init__(self, hf_token: str = "", output_dir: str = OUTPUT_DIR):
        self.hf_token = hf_token
        self.output_dir = output_dir
        os.makedirs(output_dir, exist_ok=True)

    def generate_all(
        self,
        title: str,
        mode: str,
        thumbnail_url: str = "",
        blog_url: str = "seedsup.tistory.com",
        timestamp: str = "",
    ) -> dict[str, str]:
        """
        모든 플랫폼 썸네일을 생성하고 경로 dict 반환.
        {facebook: path, threads: path, shorts_cover: path}
        """
        if not timestamp:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M")

        # 배경 이미지 로드 (티스토리 썸네일 재활용)
        bg_img = None
        if thumbnail_url:
            try:
                resp = requests.get(thumbnail_url, timeout=10)
                bg_img = Image.open(BytesIO(resp.content))
                logger.info(f"배경 이미지 로드 완료: {thumbnail_url}")
            except Exception as e:
                logger.warning(f"배경 이미지 로드 실패: {e}")

        paths = {}
        for platform, (w, h) in SIZES.items():
            img = _draw_thumbnail(
                width=w, height=h, title=title,
                blog_url=blog_url, mode=mode, bg_img=bg_img,
            )
            filename = f"thumb_{platform}_{mode}_{timestamp}.jpg"
            path = os.path.join(self.output_dir, filename)
            img.save(path, "JPEG", quality=92)
            paths[platform] = path
            logger.info(f"썸네일 저장: {path}")

        return paths
