"""
SNS 썸네일 생성기 v3 - 클릭을 부르는 고성과 디자인

[고성과 썸네일 공식]
  - 5자 내외 임팩트 메인 카피 (질문/감탄사/핵심 단어)
  - 핵심 수치는 노란색/빨간색으로 강조
  - 신뢰색(파랑/네이비) + 강렬한 강조색 대비
  - 인물/사물을 화면 2/3 크기로 크게 배치
  - 모바일에서도 직관적으로 읽히는 크고 굵은 서체
  - 텍스트 뒤 외곽선/그림자로 배경과의 대비 극대화

플랫폼별:
  facebook  : 1200×630, 뉴스 카드 스타일, 파랑 계열
  threads   : 1080×1080, 미니멀 흑백, 텍스트 중심
  instagram : 1080×1080, 퍼플/핑크 그라디언트, 비주얼 임팩트
  kakao     : 1200×630, 카카오 옐로우, 친근한 톤

의존성: Pillow>=10.3.0, requests>=2.31.0
"""

import logging
import os
import re
import textwrap
import urllib.request
from datetime import datetime
from io import BytesIO
from pathlib import Path

import requests
from PIL import Image, ImageDraw, ImageFilter, ImageFont

logger = logging.getLogger(__name__)

OUTPUT_DIR = "images"
FONT_DIR   = Path("fonts")

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
NANUM_BOLD_URL    = "https://github.com/naver/nanumfont/raw/master/fonts/NanumGothicBold.ttf"
NANUM_REGULAR_URL = "https://github.com/naver/nanumfont/raw/master/fonts/NanumGothic.ttf"


# ════════════════════════════════════════════════════════════════════════════
# 플랫폼별 디자인 설정 (고성과 공식 반영)
# ════════════════════════════════════════════════════════════════════════════

PLATFORM_CONFIGS = {

    # ── Facebook ─────────────────────────────────────────────────────────────
    # 신뢰감 뉴스 카드. 하단 2/3에 텍스트 집중.
    "facebook": {
        "size": (1200, 630),
        "overlay_color": (6, 12, 35),
        "overlay_alpha": 0.68,
        "card_color": (6, 12, 35),
        "card_alpha": 215,
        "card_radius": 22,
        "card_position": "bottom",
        "card_height_ratio": 0.56,
        "accent": (37, 150, 255),           # 선명한 파랑
        "accent_highlight": (254, 211, 48), # 강조 노란색
        "alert_color": (239, 68, 68),       # 경고/하락 빨간색
        "title_color": (255, 255, 255),
        "subtitle_color": (148, 163, 184),
        "number_color": (254, 211, 48),     # 수치 강조
        "date_bg": (37, 150, 255),
        "date_fg": (255, 255, 255),
        "bar_color": (37, 150, 255),
        "url_color": (148, 163, 184),
        "main_copy_max": 8,                 # 메인 카피 최대 글자 (모바일 기준)
        "sub_copy_max": 18,
        "blur_radius": 2,
        "top_bar": True,
        "top_bar_color": (37, 150, 255),
    },

    # ── Threads ───────────────────────────────────────────────────────────────
    # 미니멀 흑백. 텍스트가 주인공.
    "threads": {
        "size": (1080, 1080),
        "overlay_color": (4, 4, 8),
        "overlay_alpha": 0.82,
        "card_color": (255, 255, 255),
        "card_alpha": 18,
        "card_radius": 40,
        "card_position": "center",
        "card_height_ratio": 0.60,
        "accent": (255, 255, 255),
        "accent_highlight": (254, 211, 48),
        "alert_color": (239, 68, 68),
        "title_color": (255, 255, 255),
        "subtitle_color": (180, 180, 190),
        "number_color": (254, 211, 48),
        "date_bg": (255, 255, 255),
        "date_fg": (4, 4, 8),
        "bar_color": (255, 255, 255),
        "url_color": (140, 140, 155),
        "main_copy_max": 7,
        "sub_copy_max": 15,
        "blur_radius": 5,
        "monochrome_bg": True,
        "top_bar": True,
        "top_bar_color": (255, 255, 255),
    },

    # ── Instagram ─────────────────────────────────────────────────────────────
    # 퍼플/핑크 그라디언트. 비주얼 임팩트 최우선.
    "instagram": {
        "size": (1080, 1080),
        "overlay_color": (12, 5, 30),
        "overlay_alpha": 0.70,
        "card_color": (18, 8, 50),
        "card_alpha": 205,
        "card_radius": 36,
        "card_position": "center",
        "card_height_ratio": 0.62,
        "accent": (192, 132, 252),          # 퍼플
        "accent2": (244, 63, 94),           # 핑크 (그라디언트)
        "accent_highlight": (254, 211, 48),
        "alert_color": (244, 63, 94),
        "title_color": (255, 255, 255),
        "subtitle_color": (216, 180, 254),
        "number_color": (254, 211, 48),
        "date_bg": (192, 132, 252),
        "date_fg": (12, 5, 30),
        "bar_color": (192, 132, 252),
        "url_color": (192, 132, 252),
        "main_copy_max": 7,
        "sub_copy_max": 14,
        "blur_radius": 3,
        "gradient_bar": True,
    },

    # ── Instagram 세로 ────────────────────────────────────────────────────────
    "instagram_portrait": {
        "size": (1080, 1350),
        "overlay_color": (12, 5, 30),
        "overlay_alpha": 0.70,
        "card_color": (18, 8, 50),
        "card_alpha": 205,
        "card_radius": 36,
        "card_position": "center",
        "card_height_ratio": 0.52,
        "accent": (192, 132, 252),
        "accent2": (244, 63, 94),
        "accent_highlight": (254, 211, 48),
        "alert_color": (244, 63, 94),
        "title_color": (255, 255, 255),
        "subtitle_color": (216, 180, 254),
        "number_color": (254, 211, 48),
        "date_bg": (192, 132, 252),
        "date_fg": (12, 5, 30),
        "bar_color": (192, 132, 252),
        "url_color": (192, 132, 252),
        "main_copy_max": 7,
        "sub_copy_max": 14,
        "blur_radius": 3,
        "gradient_bar": True,
    },

    # ── Kakao ────────────────────────────────────────────────────────────────
    # 카카오 옐로우. 따뜻하고 친근한 톤.
    "kakao": {
        "size": (1200, 630),
        "overlay_color": (28, 18, 2),
        "overlay_alpha": 0.70,
        "card_color": (22, 14, 2),
        "card_alpha": 220,
        "card_radius": 28,
        "card_position": "center",
        "card_height_ratio": 0.72,
        "accent": (254, 229, 0),            # 카카오 옐로우
        "accent_highlight": (255, 255, 255),
        "alert_color": (255, 100, 50),
        "title_color": (255, 255, 255),
        "subtitle_color": (254, 229, 0),
        "number_color": (254, 229, 0),
        "date_bg": (254, 229, 0),
        "date_fg": (22, 14, 2),
        "bar_color": (254, 229, 0),
        "url_color": (254, 229, 0),
        "main_copy_max": 9,
        "sub_copy_max": 18,
        "blur_radius": 2,
        "warm_tint": True,
        "top_bar": True,
        "top_bar_color": (254, 229, 0),
    },
}


# ════════════════════════════════════════════════════════════════════════════
# 유틸리티
# ════════════════════════════════════════════════════════════════════════════

def _get_font(bold: bool = True, size: int = 60) -> ImageFont.FreeTypeFont:
    candidates = FONT_BOLD_CANDIDATES if bold else FONT_REGULAR_CANDIDATES
    for path in candidates:
        if Path(path).exists():
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                continue
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
            return ImageFont.load_default()
    try:
        return ImageFont.truetype(str(dest), size)
    except Exception:
        return ImageFont.load_default()


def _draw_rounded_rect(draw, xy, radius: int, fill):
    x0, y0, x1, y1 = xy
    r = min(radius, (x1 - x0) // 2, (y1 - y0) // 2)
    draw.rounded_rectangle([x0, y0, x1, y1], radius=r, fill=fill)


def _draw_text_with_outline(
    draw,
    text: str,
    font,
    color: tuple,
    x: int,
    y: int,
    outline_color=(0, 0, 0),
    outline_width: int = 3,
):
    """외곽선 있는 텍스트 렌더링 (모바일 가독성 극대화)."""
    for dx in range(-outline_width, outline_width + 1):
        for dy in range(-outline_width, outline_width + 1):
            if dx != 0 or dy != 0:
                draw.text((x + dx, y + dy), text, font=font,
                          fill=(*outline_color, 200))
    draw.text((x, y), text, font=font, fill=color)


def _parse_number_segments(text: str) -> list[tuple[str, bool]]:
    """수치/퍼센트/지수명을 강조 세그먼트로 파싱."""
    pattern = re.compile(
        r"([+-]?\d+(?:\.\d+)?%|[+-]?\d{1,3}(?:,\d{3})*(?:\.\d+)?"
        r"포인트?|S&P\s*500|나스닥|다우|FOMC|연준|Fed)"
    )
    segs = []
    last = 0
    for m in pattern.finditer(text):
        if m.start() > last:
            segs.append((text[last:m.start()], False))
        segs.append((m.group(), True))
        last = m.end()
    if last < len(text):
        segs.append((text[last:], False))
    return segs or [(text, False)]


def _apply_warm_tint(img: Image.Image, strength: float = 0.20) -> Image.Image:
    tint   = Image.new("RGBA", img.size, (255, 175, 40, int(255 * strength)))
    result = Image.alpha_composite(img.convert("RGBA"), tint)
    return result.convert("RGB")


def _apply_monochrome(img: Image.Image, strength: float = 0.88) -> Image.Image:
    gray = img.convert("L").convert("RGB")
    return Image.blend(img.convert("RGB"), gray, strength)


def _prepare_background(
    bg_img: Image.Image | None,
    width: int,
    height: int,
    cfg: dict,
) -> Image.Image:
    if bg_img:
        src_ratio = bg_img.width / bg_img.height
        dst_ratio = width / height
        if src_ratio > dst_ratio:
            new_h    = bg_img.height
            new_w    = int(new_h * dst_ratio)
            offset   = (bg_img.width - new_w) // 2
            bg       = bg_img.crop((offset, 0, offset + new_w, new_h))
        else:
            new_w    = bg_img.width
            new_h    = int(new_w / dst_ratio)
            offset   = (bg_img.height - new_h) // 3
            bg       = bg_img.crop((0, offset, new_w, offset + new_h))
        bg = bg.resize((width, height), Image.LANCZOS)

        if cfg.get("monochrome_bg"):
            bg = _apply_monochrome(bg)
        if cfg.get("warm_tint"):
            bg = _apply_warm_tint(bg)
        blur_r = cfg.get("blur_radius", 2)
        if blur_r > 0:
            bg = bg.filter(ImageFilter.GaussianBlur(radius=blur_r))

        oc      = cfg["overlay_color"]
        oa      = int(cfg["overlay_alpha"] * 255)
        overlay = Image.new("RGBA", (width, height), (*oc, oa))
        result  = Image.alpha_composite(bg.convert("RGBA"), overlay)
        return result.convert("RGB")
    else:
        return Image.new("RGB", (width, height), cfg["overlay_color"])


def _extract_date_from_title(title: str) -> str:
    m = re.search(r"(\d{2,4})[.\-/년](\d{1,2})[.\-/월](\d{0,2})", title)
    if m:
        y, mo, d = m.group(1), m.group(2), m.group(3)
        if len(y) == 2:
            y = "20" + y
        return f"{y}.{mo.zfill(2)}.{d.zfill(2)}" if d else f"{y}.{mo.zfill(2)}"
    return datetime.now().strftime("%y.%m.%d")


def _build_impact_copy(title: str, platform_post: str, platform: str) -> tuple[str, str]:
    """
    고성과 공식: 블로그 제목과 SNS 포스트에서
    임팩트 있는 메인 카피(5-8자)와 서브 카피를 추출합니다.

    반환: (메인_카피, 서브_카피)
    """
    max_main = {"facebook": 8, "threads": 7, "instagram": 7,
                "instagram_portrait": 7, "kakao": 9}.get(platform, 8)
    max_sub  = {"facebook": 18, "threads": 15, "instagram": 14,
                "instagram_portrait": 14, "kakao": 18}.get(platform, 16)

    # 제목에서 핵심 키워드/수치 추출
    # 수치 패턴: +2.3%, -180포인트, 등
    num_match = re.search(r"[+-]?\d+(?:\.\d+)?%|[+-]?\d+(?:\.\d+)?포인트", title)

    # 훅 패턴 추출 (물음표/느낌표 이전 핵심 문구)
    hook_match = re.search(r"[:：\?？!！](.{3,12})", title)

    if num_match and len(num_match.group()) <= max_main:
        main_copy = num_match.group()
    elif hook_match and len(hook_match.group(1).strip()) <= max_main:
        main_copy = hook_match.group(1).strip()
    else:
        # 제목 앞 부분에서 핵심 단어 추출
        clean = re.sub(r"\d{4}년\s*\d{1,2}월\s*\d{1,2}일\s*", "", title).strip()
        clean = re.sub(r"미국\s*증시\s*[:：]?\s*", "", clean).strip()
        main_copy = clean[:max_main]

    # 서브 카피: SNS 포스트 첫 줄 (해시태그 제외)
    if platform_post:
        first_line = platform_post.strip().split("\n")[0]
        sub_copy   = first_line.split("#")[0].strip()[:max_sub]
    else:
        # 제목 뒷부분 활용
        sub_copy = title[len(main_copy):][:max_sub].strip(" :：-")

    return main_copy or title[:max_main], sub_copy or ""


# ════════════════════════════════════════════════════════════════════════════
# 썸네일 렌더러 (고성과 공식)
# ════════════════════════════════════════════════════════════════════════════

def _render_thumbnail(
    platform: str,
    bg_img: Image.Image | None,
    title: str,
    main_copy: str,
    sub_copy: str,
    date_str: str,
    blog_url: str,
    mode: str,
) -> Image.Image:
    cfg   = PLATFORM_CONFIGS[platform]
    W, H  = cfg["size"]
    scale = W / 1200

    base   = _prepare_background(bg_img, W, H, cfg)
    canvas = base.convert("RGBA")
    draw   = ImageDraw.Draw(canvas, "RGBA")

    accent       = cfg["accent"]
    highlight    = cfg["accent_highlight"]
    alert        = cfg["alert_color"]

    # ── 폰트 크기 (모바일 가독성: 더 크게) ─────────────────────────────────
    f_main_copy = _get_font(bold=True,  size=int(96 * scale))   # 메인 카피 (임팩트)
    f_sub_copy  = _get_font(bold=True,  size=int(44 * scale))   # 서브 카피
    f_date      = _get_font(bold=True,  size=int(36 * scale))
    f_small     = _get_font(bold=False, size=int(26 * scale))

    # ── 상단 컬러 바 ─────────────────────────────────────────────────────────
    if cfg.get("top_bar"):
        bar_color = cfg.get("top_bar_color", accent)
        draw.rectangle([(0, 0), (W, int(10 * scale))], fill=(*bar_color, 255))

    # ── Instagram/Instagram Portrait: 퍼플-핑크 그라디언트 상단 바 ────────────
    if cfg.get("gradient_bar") and "accent2" in cfg:
        accent2  = cfg["accent2"]
        bar_h_px = int(12 * scale)
        for px in range(W):
            t = px / W
            r = int(accent[0] * (1 - t) + accent2[0] * t)
            g = int(accent[1] * (1 - t) + accent2[1] * t)
            b = int(accent[2] * (1 - t) + accent2[2] * t)
            draw.line([(px, 0), (px, bar_h_px)], fill=(r, g, b, 255))

    # ── 카드 영역 ─────────────────────────────────────────────────────────────
    card_margin_x = int(W * 0.06)
    card_h        = int(H * cfg["card_height_ratio"])
    card_radius   = cfg["card_radius"]

    if cfg["card_position"] == "bottom":
        card_y0 = H - card_h - int(H * 0.04)
        card_y1 = H - int(H * 0.03)
    else:
        card_y0 = (H - card_h) // 2
        card_y1 = card_y0 + card_h

    card_x0 = card_margin_x
    card_x1 = W - card_margin_x

    cc = cfg["card_color"]
    ca = cfg["card_alpha"]
    _draw_rounded_rect(draw, (card_x0, card_y0, card_x1, card_y1),
                       card_radius, (*cc, ca))

    # 좌측 액센트 바
    bar_w    = int(8 * scale)
    bar_ins  = int(card_radius * 0.5)
    draw.rounded_rectangle(
        [card_x0 + bar_ins, card_y0 + bar_ins,
         card_x0 + bar_ins + bar_w, card_y1 - bar_ins],
        radius=bar_w // 2, fill=(*accent, 235),
    )

    # ── 카드 내부 레이아웃 ───────────────────────────────────────────────────
    text_x0 = card_x0 + bar_ins + bar_w + int(22 * scale)
    text_x1 = card_x1 - int(22 * scale)
    text_cx  = (text_x0 + text_x1) // 2
    inner_top = card_y0 + int(28 * scale)
    y = inner_top

    # ── 날짜 배지 ─────────────────────────────────────────────────────────────
    date_pad_x, date_pad_y = int(16 * scale), int(8 * scale)
    date_bbox   = draw.textbbox((0, 0), date_str, font=f_date)
    badge_w     = (date_bbox[2] - date_bbox[0]) + date_pad_x * 2
    badge_h     = (date_bbox[3] - date_bbox[1]) + date_pad_y * 2
    date_x      = card_x1 - int(card_margin_x * 0.3) - badge_w
    date_y      = card_y0 + int(18 * scale)
    dbg         = cfg["date_bg"]
    dfg         = cfg["date_fg"]
    draw.rounded_rectangle(
        [date_x, date_y, date_x + badge_w, date_y + badge_h],
        radius=badge_h // 2, fill=(*dbg, 245),
    )
    draw.text((date_x + date_pad_x, date_y + date_pad_y),
              date_str, font=f_date, fill=dfg)
    y = max(y, date_y + badge_h + int(18 * scale))

    # ── 메인 카피 (고성과 공식: 크고 임팩트 있게) ────────────────────────────
    # 수치 포함 여부 체크 (수치면 강조색 사용)
    has_number = bool(re.search(r"[+-]?\d+(?:\.\d+)?[%포]", main_copy))
    mc_color   = highlight if has_number else cfg["title_color"]

    mc_lines = textwrap.wrap(main_copy, width=cfg["main_copy_max"])[:2]
    for line in mc_lines:
        mc_bbox = draw.textbbox((0, 0), line, font=f_main_copy)
        mc_w    = mc_bbox[2] - mc_bbox[0]
        mc_x    = text_cx - mc_w // 2
        _draw_text_with_outline(draw, line, f_main_copy, mc_color, mc_x, y,
                                outline_color=(0, 0, 0), outline_width=4)
        y += mc_bbox[3] - mc_bbox[1] + int(10 * scale)

    # ── 구분선 ───────────────────────────────────────────────────────────────
    y += int(10 * scale)
    draw.rectangle(
        [text_x0 + int(10 * scale), y, text_x1 - int(10 * scale), y + int(3 * scale)],
        fill=(*accent, 180),
    )
    y += int(18 * scale)

    # ── 서브 카피 (수치 세그먼트 강조) ────────────────────────────────────────
    if sub_copy:
        sc_lines = textwrap.wrap(sub_copy, width=cfg["sub_copy_max"])[:2]
        for line in sc_lines:
            segs   = _parse_number_segments(line)
            seg_ws = []
            for seg_text, is_hl in segs:
                b = draw.textbbox((0, 0), seg_text, font=f_sub_copy)
                seg_ws.append(b[2] - b[0])
            total_w  = sum(seg_ws)
            x_cursor = text_cx - total_w // 2

            for j, (seg_text, is_hl) in enumerate(segs):
                color = (*cfg["number_color"], 255) if is_hl else (*cfg["subtitle_color"], 230)
                bbox  = draw.textbbox((0, 0), seg_text, font=f_sub_copy)
                seg_h = bbox[3] - bbox[1]
                _draw_text_with_outline(draw, seg_text, f_sub_copy,
                                        color[:3] + (255,), x_cursor, y,
                                        outline_width=2)
                x_cursor += seg_ws[j]
            y += seg_h + int(8 * scale)

    # ── 하단 URL ─────────────────────────────────────────────────────────────
    url_text = "seedsup.tistory.com"
    url_y    = card_y1 - int(38 * scale)
    url_bbox = draw.textbbox((0, 0), url_text, font=f_small)
    url_x    = text_cx - (url_bbox[2] - url_bbox[0]) // 2
    draw.text((url_x, url_y), url_text, font=f_small,
              fill=(*cfg["url_color"], 180))

    # ── Kakao: 하단 채널 텍스트 ──────────────────────────────────────────────
    if platform == "kakao":
        f_logo = _get_font(bold=True, size=int(22 * scale))
        draw.text(
            (W // 2, int(H * 0.96)),
            "📊 카카오 스토리채널",
            font=f_logo,
            fill=(*accent, 200),
            anchor="mm",
        )

    return canvas.convert("RGB")


# ════════════════════════════════════════════════════════════════════════════
# SNSThumbnailGenerator
# ════════════════════════════════════════════════════════════════════════════

class SNSThumbnailGenerator:
    """
    플랫폼별 맞춤 SNS 썸네일 생성기.
    고성과 공식: 임팩트 메인 카피 + 수치 강조 + 외곽선 텍스트.
    """

    def __init__(self, hf_token: str = "", output_dir: str = OUTPUT_DIR):
        self.hf_token   = hf_token
        self.output_dir = output_dir
        os.makedirs(output_dir, exist_ok=True)

    def generate_all(
        self,
        title: str,
        mode: str,
        thumbnail_url: str = "",
        blog_url: str = "seedsup.tistory.com",
        timestamp: str = "",
        content: dict | None = None,
    ) -> dict[str, str]:
        if not timestamp:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M")
        content = content or {}

        bg_img   = self._load_bg_image(thumbnail_url)
        date_str = _extract_date_from_title(title)

        platforms = [
            "facebook",
            "threads",
            "instagram",
            "instagram_portrait",
            "kakao",
        ]

        # 플랫폼별 임팩트 카피 생성
        copy_map: dict[str, tuple[str, str]] = {}
        for platform in platforms:
            post_key = {
                "facebook":           "facebook_post",
                "threads":            "threads_post",
                "instagram":          "instagram_post",
                "instagram_portrait": "instagram_post",
                "kakao":              "kakao_post",
            }.get(platform, "")
            platform_post = content.get(post_key, "")
            copy_map[platform] = _build_impact_copy(title, platform_post, platform)
            logger.info(
                f"[{platform}] 메인카피: '{copy_map[platform][0]}' / "
                f"서브카피: '{copy_map[platform][1][:20]}...'"
            )

        paths: dict[str, str] = {}
        for platform in platforms:
            try:
                logger.info(f"[{platform}] 썸네일 생성 중...")
                main_copy, sub_copy = copy_map[platform]
                img = _render_thumbnail(
                    platform  = platform,
                    bg_img    = bg_img,
                    title     = title,
                    main_copy = main_copy,
                    sub_copy  = sub_copy,
                    date_str  = date_str,
                    blog_url  = blog_url,
                    mode      = mode,
                )
                filename = f"thumb_{platform}_{mode}_{timestamp}.jpg"
                path     = os.path.join(self.output_dir, filename)
                img.save(path, "JPEG", quality=94, optimize=True)
                paths[platform] = path
                logger.info(f"  → 저장: {path}")
            except Exception as e:
                logger.error(f"[{platform}] 썸네일 생성 실패: {e}")

        return paths

    def _load_bg_image(self, thumbnail_url: str) -> Image.Image | None:
        if not thumbnail_url:
            return None
        try:
            resp = requests.get(
                thumbnail_url, timeout=15,
                headers={"User-Agent": "Mozilla/5.0"},
            )
            resp.raise_for_status()
            img = Image.open(BytesIO(resp.content)).convert("RGB")
            logger.info(f"배경 이미지 로드 완료: {img.size}")
            return img
        except Exception as e:
            logger.warning(f"배경 이미지 로드 실패: {e}")
            return None
