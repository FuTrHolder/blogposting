"""
SNS 썸네일 생성기 v2
각 플랫폼 특성에 맞는 고품질 썸네일을 생성합니다.

레퍼런스 스타일 (첨부 이미지):
  - 배경 사진 위에 반투명 둥근 사각형 카드 오버레이
  - 굵은 한글 타이포그래피 (날짜 + 제목 키워드)
  - 플랫폼별 분위기/색상/레이아웃 차별화

플랫폼별 특성:
  facebook  : 1200×630, 정보 전달형, 신뢰감 있는 뉴스 카드 스타일
  threads   : 1080×1080, 미니멀·심플, 텍스트 중심, 여백 강조
  instagram : 1080×1080 (피드) / 1080×1350 (세로), 비주얼 임팩트, 감성적
  kakao     : 1200×630, 따뜻하고 친근한 톤, 이모지 활용, 노란 포인트

의존성: Pillow>=10.3.0, requests>=2.31.0
"""

import logging
import math
import os
import textwrap
import urllib.request
from datetime import datetime
from io import BytesIO
from pathlib import Path

import requests
from PIL import Image, ImageDraw, ImageFilter, ImageFont

logger = logging.getLogger(__name__)

OUTPUT_DIR = "images"
FONT_DIR = Path("fonts")

# ── 시스템 폰트 후보 (GitHub Actions ubuntu-latest + fonts-noto-cjk) ────────
FONT_BOLD_CANDIDATES = [
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Black.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Bold.ttc",
    str(FONT_DIR / "NanumGothicBold.ttf"),
]
FONT_REGULAR_CANDIDATES = [
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    str(FONT_DIR / "NanumGothic.ttf"),
]
NANUM_BOLD_URL = "https://github.com/naver/nanumfont/raw/master/fonts/NanumGothicBold.ttf"
NANUM_REGULAR_URL = "https://github.com/naver/nanumfont/raw/master/fonts/NanumGothic.ttf"


# ════════════════════════════════════════════════════════════════════════════
# 플랫폼별 디자인 설정
# ════════════════════════════════════════════════════════════════════════════

PLATFORM_CONFIGS = {

    # ── Facebook ─────────────────────────────────────────────────────────
    # 분위기: 신뢰감 있는 뉴스/정보 카드. 파란 계열 다크.
    # 레이아웃: 하단 카드 오버레이, 좌측 컬러 바, 날짜 배지 우상단
    "facebook": {
        "size": (1200, 630),
        "style": "news_card",
        "overlay_color": (8, 20, 45),          # 딥 네이비
        "overlay_alpha": 0.70,
        "card_color": (8, 20, 45),
        "card_alpha": 210,                      # 카드 배경 불투명도
        "card_radius": 24,
        "card_position": "bottom",              # 카드 위치: bottom / center
        "card_height_ratio": 0.52,              # 카드 높이 / 전체 높이
        "accent": (56, 189, 248),               # 스카이 블루
        "title_color": (255, 255, 255),
        "subtitle_color": (148, 163, 184),
        "date_bg": (56, 189, 248),
        "date_fg": (8, 20, 45),
        "bar_color": (56, 189, 248),
        "url_color": (56, 189, 248),
        "title_max_chars": 14,                  # 줄당 최대 글자
        "blur_radius": 2,
    },

    # ── Threads ───────────────────────────────────────────────────────────
    # 분위기: 미니멀, 여백, 모노크롬. 인스타 계열이지만 더 텍스트 중심.
    # 레이아웃: 중앙 카드, 흰 배경, 심플한 타이포
    "threads": {
        "size": (1080, 1080),
        "style": "minimal_card",
        "overlay_color": (5, 5, 8),
        "overlay_alpha": 0.78,
        "card_color": (255, 255, 255),
        "card_alpha": 22,                       # 거의 투명한 흰색 카드
        "card_radius": 40,
        "card_position": "center",
        "card_height_ratio": 0.55,
        "accent": (255, 255, 255),
        "title_color": (255, 255, 255),
        "subtitle_color": (180, 180, 190),
        "date_bg": (255, 255, 255),
        "date_fg": (5, 5, 8),
        "bar_color": (255, 255, 255),
        "url_color": (160, 160, 170),
        "title_max_chars": 12,
        "blur_radius": 4,
        "monochrome_bg": True,                  # 배경을 흑백 처리
    },

    # ── Instagram ─────────────────────────────────────────────────────────
    # 분위기: 비주얼 임팩트, 감성적, 색감 강조. 그라디언트 포인트.
    # 레이아웃: 중앙 카드, 퍼플/핑크 그라디언트 액센트
    "instagram": {
        "size": (1080, 1080),
        "style": "gradient_card",
        "overlay_color": (15, 8, 35),
        "overlay_alpha": 0.72,
        "card_color": (20, 10, 50),
        "card_alpha": 200,
        "card_radius": 36,
        "card_position": "center",
        "card_height_ratio": 0.58,
        "accent": (192, 132, 252),              # 퍼플
        "accent2": (236, 72, 153),              # 핑크 (그라디언트용)
        "title_color": (255, 255, 255),
        "subtitle_color": (216, 180, 254),
        "date_bg": (192, 132, 252),
        "date_fg": (15, 8, 35),
        "bar_color": (192, 132, 252),
        "url_color": (192, 132, 252),
        "title_max_chars": 12,
        "blur_radius": 3,
        "gradient_bar": True,                   # 상단 그라디언트 바
    },

    # ── Instagram 세로 (portrait) ──────────────────────────────────────────
    "instagram_portrait": {
        "size": (1080, 1350),
        "style": "gradient_card",
        "overlay_color": (15, 8, 35),
        "overlay_alpha": 0.72,
        "card_color": (20, 10, 50),
        "card_alpha": 200,
        "card_radius": 36,
        "card_position": "center",
        "card_height_ratio": 0.50,
        "accent": (192, 132, 252),
        "accent2": (236, 72, 153),
        "title_color": (255, 255, 255),
        "subtitle_color": (216, 180, 254),
        "date_bg": (192, 132, 252),
        "date_fg": (15, 8, 35),
        "bar_color": (192, 132, 252),
        "url_color": (192, 132, 252),
        "title_max_chars": 12,
        "blur_radius": 3,
        "gradient_bar": True,
    },

    # ── Kakao ─────────────────────────────────────────────────────────────
    # 분위기: 따뜻하고 친근함, 노란 포인트, 이모지·구어체 텍스트
    # 레이아웃: 중앙 카드, 카카오 옐로우 강조
    "kakao": {
        "size": (1200, 630),
        "style": "warm_card",
        "overlay_color": (30, 20, 5),
        "overlay_alpha": 0.68,
        "card_color": (25, 16, 4),
        "card_alpha": 215,
        "card_radius": 28,
        "card_position": "center",
        "card_height_ratio": 0.70,
        "accent": (254, 229, 0),                # 카카오 옐로우
        "title_color": (255, 255, 255),
        "subtitle_color": (254, 229, 0),
        "date_bg": (254, 229, 0),
        "date_fg": (25, 16, 4),
        "bar_color": (254, 229, 0),
        "url_color": (254, 229, 0),
        "title_max_chars": 14,
        "blur_radius": 2,
        "warm_tint": True,                      # 배경에 따뜻한 황금빛 틴트
    },
}


# ════════════════════════════════════════════════════════════════════════════
# 유틸리티
# ════════════════════════════════════════════════════════════════════════════

def _get_font(bold: bool = True, size: int = 60) -> ImageFont.FreeTypeFont:
    """한글 지원 폰트 로드. 없으면 NanumGothic 자동 다운로드."""
    candidates = FONT_BOLD_CANDIDATES if bold else FONT_REGULAR_CANDIDATES
    for path in candidates:
        if Path(path).exists():
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                continue
    # 폴백: NanumGothic 다운로드
    FONT_DIR.mkdir(exist_ok=True)
    url = NANUM_BOLD_URL if bold else NANUM_REGULAR_URL
    dest = FONT_DIR / ("NanumGothicBold.ttf" if bold else "NanumGothic.ttf")
    if not dest.exists():
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                dest.write_bytes(resp.read())
        except Exception as e:
            logger.warning(f"폰트 다운로드 실패: {e}")
            return ImageFont.load_default()
    try:
        return ImageFont.truetype(str(dest), size)
    except Exception:
        return ImageFont.load_default()


def _draw_rounded_rect(draw: ImageDraw.Draw, xy, radius: int, fill):
    """PIL에서 모서리가 둥근 사각형을 RGBA fill로 그립니다."""
    x0, y0, x1, y1 = xy
    r = min(radius, (x1 - x0) // 2, (y1 - y0) // 2)
    draw.rounded_rectangle([x0, y0, x1, y1], radius=r, fill=fill)


def _draw_text_centered(
    draw: ImageDraw.Draw,
    text: str,
    font: ImageFont.FreeTypeFont,
    color: tuple,
    cx: int,
    y: int,
    shadow: bool = True,
    shadow_color=(0, 0, 0, 140),
) -> int:
    """텍스트를 cx 기준 가운데 정렬로 그리고 다음 y좌표 반환."""
    bbox = draw.textbbox((0, 0), text, font=font)
    w = bbox[2] - bbox[0]
    x = cx - w // 2
    if shadow:
        draw.text((x + 3, y + 3), text, font=font, fill=shadow_color)
    draw.text((x, y), text, font=font, fill=color)
    return y + (bbox[3] - bbox[1]) + 16


def _apply_warm_tint(img: Image.Image, strength: float = 0.25) -> Image.Image:
    """이미지에 따뜻한 황금빛 틴트 적용 (카카오용)."""
    tint = Image.new("RGBA", img.size, (255, 180, 50, int(255 * strength)))
    result = Image.alpha_composite(img.convert("RGBA"), tint)
    return result.convert("RGB")


def _apply_monochrome(img: Image.Image, strength: float = 0.85) -> Image.Image:
    """이미지를 부분 흑백 처리 (Threads 미니멀용)."""
    gray = img.convert("L").convert("RGB")
    return Image.blend(img.convert("RGB"), gray, strength)


def _prepare_background(
    bg_img: Image.Image | None,
    width: int,
    height: int,
    cfg: dict,
) -> Image.Image:
    """배경 이미지 처리: 리사이즈 + 블러 + 오버레이 + 플랫폼별 효과."""
    if bg_img:
        # 커버 크롭 (Fill 방식)
        src_ratio = bg_img.width / bg_img.height
        dst_ratio = width / height
        if src_ratio > dst_ratio:
            new_h = bg_img.height
            new_w = int(new_h * dst_ratio)
            offset = (bg_img.width - new_w) // 2
            bg = bg_img.crop((offset, 0, offset + new_w, new_h))
        else:
            new_w = bg_img.width
            new_h = int(new_w / dst_ratio)
            offset = (bg_img.height - new_h) // 3  # 상단 1/3 위치
            bg = bg_img.crop((0, offset, new_w, offset + new_h))
        bg = bg.resize((width, height), Image.LANCZOS)

        # 플랫폼별 배경 효과
        if cfg.get("monochrome_bg"):
            bg = _apply_monochrome(bg, strength=0.88)
        if cfg.get("warm_tint"):
            bg = _apply_warm_tint(bg, strength=0.20)

        # 가우시안 블러
        blur_r = cfg.get("blur_radius", 2)
        if blur_r > 0:
            bg = bg.filter(ImageFilter.GaussianBlur(radius=blur_r))

        # 다크 오버레이
        oc = cfg["overlay_color"]
        oa = int(cfg["overlay_alpha"] * 255)
        overlay = Image.new("RGBA", (width, height), (*oc, oa))
        result = Image.alpha_composite(bg.convert("RGBA"), overlay)
        return result.convert("RGB")
    else:
        return Image.new("RGB", (width, height), cfg["overlay_color"])


def _extract_date_from_title(title: str) -> str:
    """제목에서 날짜 패턴 추출. 없으면 오늘 날짜."""
    import re
    # "25.10.14" / "2025.10.14" / "25년 10월" 등
    m = re.search(r"(\d{2,4})[.\-/년](\d{1,2})[.\-/월](\d{0,2})", title)
    if m:
        y, mo, d = m.group(1), m.group(2), m.group(3)
        if len(y) == 2:
            y = "20" + y
        return f"{y}.{mo.zfill(2)}.{d.zfill(2)}" if d else f"{y}.{mo.zfill(2)}"
    return datetime.now().strftime("%y.%m.%d")


def _make_subtitle_from_content(
    platform: str,
    post_text: str,
    title: str,
) -> str:
    """
    각 플랫폼 특성에 맞는 서브타이틀 텍스트 생성.
    post_text: 해당 플랫폼의 SNS 게시물 텍스트 (Gemini 생성)
    """
    # 첫 줄 또는 첫 30자 이내의 핵심 문구 추출
    first_line = post_text.strip().split("\n")[0] if post_text else ""
    # 해시태그 이전 텍스트만
    clean = first_line.split("#")[0].strip()
    # 이모지 이전 핵심 문구
    max_len = {
        "facebook": 28,
        "threads": 22,
        "instagram": 22,
        "instagram_portrait": 22,
        "kakao": 28,
    }.get(platform, 24)
    return clean[:max_len] if clean else ""


# ════════════════════════════════════════════════════════════════════════════
# 플랫폼별 썸네일 렌더러
# ════════════════════════════════════════════════════════════════════════════

def _render_thumbnail(
    platform: str,
    bg_img: Image.Image | None,
    title: str,
    subtitle: str,
    date_str: str,
    blog_url: str,
    mode: str,
) -> Image.Image:
    """
    단일 플랫폼 썸네일 렌더링.
    레퍼런스 이미지 스타일: 배경 사진 + 반투명 둥근 카드 + 굵은 한글
    """
    cfg = PLATFORM_CONFIGS[platform]
    W, H = cfg["size"]

    # ── 배경 준비 ──────────────────────────────────────────────────────
    base = _prepare_background(bg_img, W, H, cfg)
    canvas = base.convert("RGBA")
    draw = ImageDraw.Draw(canvas, "RGBA")

    # ── 폰트 크기 계산 ─────────────────────────────────────────────────
    # 이미지 너비에 비례해서 폰트 크기 결정
    scale = W / 1200
    f_date = _get_font(bold=True,  size=int(38 * scale))
    f_title = _get_font(bold=True,  size=int(72 * scale))
    f_sub = _get_font(bold=False, size=int(36 * scale))
    f_url = _get_font(bold=False, size=int(28 * scale))

    # ── 카드 영역 계산 ─────────────────────────────────────────────────
    card_margin_x = int(W * 0.065)
    card_h = int(H * cfg["card_height_ratio"])
    card_radius = cfg["card_radius"]

    if cfg["card_position"] == "bottom":
        card_y0 = H - card_h - int(H * 0.04)
        card_y1 = H - int(H * 0.04)
    else:  # center
        card_y0 = (H - card_h) // 2
        card_y1 = card_y0 + card_h

    card_x0 = card_margin_x
    card_x1 = W - card_margin_x

    # ── 카드 배경 그리기 ───────────────────────────────────────────────
    cc = cfg["card_color"]
    ca = cfg["card_alpha"]
    _draw_rounded_rect(draw, (card_x0, card_y0, card_x1, card_y1),
                       card_radius, (*cc, ca))

    # ── 좌측 액센트 바 (카드 내부) ────────────────────────────────────
    bar_w = int(8 * scale)
    bar_inset = int(card_radius * 0.5)
    accent = cfg["accent"]
    draw.rounded_rectangle(
        [card_x0 + bar_inset, card_y0 + bar_inset,
         card_x0 + bar_inset + bar_w, card_y1 - bar_inset],
        radius=bar_w // 2,
        fill=(*accent, 230)
    )

    # ── 카드 내부 텍스트 시작 y 위치 ──────────────────────────────────
    text_x0 = card_x0 + bar_inset + bar_w + int(24 * scale)
    text_x1 = card_x1 - int(24 * scale)
    text_cx = (text_x0 + text_x1) // 2
    inner_top = card_y0 + int(30 * scale)
    y = inner_top

    # ── 날짜 배지 ─────────────────────────────────────────────────────
    date_pad_x, date_pad_y = int(18 * scale), int(8 * scale)
    date_bbox = draw.textbbox((0, 0), date_str, font=f_date)
    date_badge_w = (date_bbox[2] - date_bbox[0]) + date_pad_x * 2
    date_badge_h = (date_bbox[3] - date_bbox[1]) + date_pad_y * 2
    date_x = card_x1 - int(card_margin_x * 0.3) - date_badge_w
    date_y = card_y0 + int(20 * scale)

    dbg = cfg["date_bg"]
    draw.rounded_rectangle(
        [date_x, date_y, date_x + date_badge_w, date_y + date_badge_h],
        radius=date_badge_h // 2,
        fill=(*dbg, 240)
    )
    draw.text(
        (date_x + date_pad_x, date_y + date_pad_y),
        date_str, font=f_date, fill=cfg["date_fg"]
    )
    y = max(y, date_y + date_badge_h + int(20 * scale))

    # ── 제목 텍스트 (굵은 한글) ────────────────────────────────────────
    # 레퍼런스 이미지처럼 제목 키워드를 크게, 줄 바꿔서 표시
    max_chars = cfg["title_max_chars"]
    # 날짜 패턴이 제목에 있으면 제거 후 핵심 키워드만
    import re
    clean_title = re.sub(r"\d{2,4}[.\-/]\d{1,2}[.\-/]\d{0,2}\s*", "", title).strip()
    if not clean_title:
        clean_title = title

    title_lines = textwrap.wrap(clean_title, width=max_chars)[:3]  # 최대 3줄
    for line in title_lines:
        bbox = draw.textbbox((0, 0), line, font=f_title)
        lw = bbox[2] - bbox[0]
        lx = text_cx - lw // 2
        # 그림자
        draw.text((lx + 3, y + 3), line, font=f_title, fill=(0, 0, 0, 160))
        draw.text((lx, y), line, font=f_title, fill=cfg["title_color"])
        y += (bbox[3] - bbox[1]) + int(12 * scale)

    # ── 구분선 ────────────────────────────────────────────────────────
    y += int(12 * scale)
    line_x0 = text_x0 + int(20 * scale)
    line_x1 = text_x1 - int(20 * scale)
    draw.rectangle(
        [line_x0, y, line_x1, y + int(3 * scale)],
        fill=(*accent, 160)
    )
    y += int(20 * scale)

    # ── 서브타이틀 ────────────────────────────────────────────────────
    if subtitle:
        sub_lines = textwrap.wrap(subtitle, width=int(max_chars * 1.5))[:2]
        for line in sub_lines:
            bbox = draw.textbbox((0, 0), line, font=f_sub)
            lw = bbox[2] - bbox[0]
            lx = text_cx - lw // 2
            draw.text((lx + 2, y + 2), line, font=f_sub, fill=(0, 0, 0, 100))
            draw.text((lx, y), line, font=f_sub, fill=cfg["subtitle_color"])
            y += (bbox[3] - bbox[1]) + int(8 * scale)

    # ── 하단 URL ──────────────────────────────────────────────────────
    url_text = f"seedsup.tistory.com"
    url_y = card_y1 - int(40 * scale)
    bbox = draw.textbbox((0, 0), url_text, font=f_url)
    uw = bbox[2] - bbox[0]
    draw.text(
        (text_cx - uw // 2, url_y),
        url_text, font=f_url, fill=(*cfg["url_color"], 200)
    )

    # ── 플랫폼별 추가 장식 ────────────────────────────────────────────

    # Instagram / Instagram Portrait: 퍼플-핑크 그라디언트 상단 바
    if cfg.get("gradient_bar") and "accent2" in cfg:
        bar_h_px = int(10 * scale)
        accent2 = cfg["accent2"]
        for px in range(W):
            t = px / W
            r = int(accent[0] * (1 - t) + accent2[0] * t)
            g = int(accent[1] * (1 - t) + accent2[1] * t)
            b = int(accent[2] * (1 - t) + accent2[2] * t)
            draw.line([(px, 0), (px, bar_h_px)], fill=(r, g, b, 255))

    # Threads: 상단 미니멀 바 (흰색)
    if cfg.get("monochrome_bg"):
        draw.rectangle([(0, 0), (W, int(6 * scale))], fill=(255, 255, 255, 180))

    # Facebook: 상단 진행/브랜드 바
    if platform == "facebook":
        draw.rectangle([(0, 0), (W, int(8 * scale))], fill=(*accent, 255))

    # Kakao: 상단 노란 바 + 카카오 로고 텍스트
    if platform == "kakao":
        draw.rectangle([(0, 0), (W, int(10 * scale))], fill=(*accent, 255))
        f_logo = _get_font(bold=True, size=int(22 * scale))
        draw.text(
            (int(W * 0.5), int(H * 0.97)),
            "📊 카카오 스토리채널",
            font=f_logo,
            fill=(*accent, 200),
            anchor="mm",
        )

    return canvas.convert("RGB")


# ════════════════════════════════════════════════════════════════════════════
# SNSThumbnailGenerator (메인 클래스)
# ════════════════════════════════════════════════════════════════════════════

class SNSThumbnailGenerator:
    """
    플랫폼별 맞춤 SNS 썸네일 생성기.
    generate_all() 호출 시 facebook / threads / instagram / kakao 썸네일을
    각 플랫폼 특성에 맞는 스타일로 생성합니다.
    """

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
        content: dict | None = None,  # Gemini 생성 플랫폼별 텍스트 (옵션)
    ) -> dict[str, str]:
        """
        모든 플랫폼 썸네일 생성 → {platform: 파일경로} 반환.

        content dict 키: facebook_post, threads_post, instagram_post, kakao_post
        (없으면 title에서 자동 추출)
        """
        if not timestamp:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M")
        content = content or {}

        # ── 배경 이미지 로드 ──────────────────────────────────────────
        bg_img = self._load_bg_image(thumbnail_url)

        # ── 날짜 추출 ─────────────────────────────────────────────────
        date_str = _extract_date_from_title(title)

        # ── 플랫폼별 생성 ─────────────────────────────────────────────
        platforms_to_generate = [
            "facebook",
            "threads",
            "instagram",
            "instagram_portrait",
            "kakao",
        ]

        # 플랫폼별 서브타이틀 텍스트 매핑
        subtitle_map = {
            "facebook": _make_subtitle_from_content(
                "facebook", content.get("facebook_post", ""), title),
            "threads": _make_subtitle_from_content(
                "threads", content.get("threads_post", ""), title),
            "instagram": _make_subtitle_from_content(
                "instagram", content.get("instagram_post", ""), title),
            "instagram_portrait": _make_subtitle_from_content(
                "instagram", content.get("instagram_post", ""), title),
            "kakao": _make_subtitle_from_content(
                "kakao", content.get("kakao_post", ""), title),
        }

        paths = {}
        for platform in platforms_to_generate:
            try:
                logger.info(f"[{platform}] 썸네일 생성 중...")
                img = _render_thumbnail(
                    platform=platform,
                    bg_img=bg_img,
                    title=title,
                    subtitle=subtitle_map.get(platform, ""),
                    date_str=date_str,
                    blog_url=blog_url,
                    mode=mode,
                )
                filename = f"thumb_{platform}_{mode}_{timestamp}.jpg"
                path = os.path.join(self.output_dir, filename)
                img.save(path, "JPEG", quality=93, optimize=True)
                paths[platform] = path
                logger.info(f"  → 저장: {path}")
            except Exception as e:
                logger.error(f"[{platform}] 썸네일 생성 실패: {e}")

        return paths

    def _load_bg_image(self, thumbnail_url: str) -> Image.Image | None:
        """배경 이미지 로드."""
        if not thumbnail_url:
            return None
        try:
            resp = requests.get(
                thumbnail_url,
                timeout=15,
                headers={"User-Agent": "Mozilla/5.0"},
            )
            resp.raise_for_status()
            img = Image.open(BytesIO(resp.content)).convert("RGB")
            logger.info(f"배경 이미지 로드 완료: {thumbnail_url} ({img.size})")
            return img
        except Exception as e:
            logger.warning(f"배경 이미지 로드 실패: {e}")
            return None
