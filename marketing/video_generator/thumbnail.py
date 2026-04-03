"""
SNS 썸네일 생성기 v4 — 전면 재작성
=====================================
주요 변경사항:
  - 폰트: GitHub raw URL 제거 → 시스템 Noto CJK 직접 사용 (index=0 명시)
  - 임팩트 카피: 정규식 기반 → content_adapter의 thumbnail_copy 필드 우선 사용
  - 배경 이미지: Pexels API 무료 + 그라디언트 fallback (단색 박스 제거)
  - 플랫폼별 레이아웃 구조 차별화
      Facebook  : 와이드 뉴스 카드 (1200×630)
      Threads   : 미니멀 흑백 스퀘어 (1080×1080)
      Instagram : 비주얼 임팩트 스퀘어 (1080×1080) — 퍼플 그라디언트
      Instagram Portrait: 세로 피드 (1080×1350)
      Kakao     : 옐로우 스토리 카드 (1200×630)
  - 텍스트 렌더링: 픽셀 기반 줄바꿈 + 8방향 외곽선
  - 이미지 없을 때: 그라디언트 배경 생성 (단색 박스 불가)
"""

import logging
import os
import re
import time
from datetime import datetime
from io import BytesIO
from math import sin, pi
from pathlib import Path
from struct import pack

import requests
from PIL import Image, ImageDraw, ImageFilter, ImageFont

logger = logging.getLogger(__name__)

OUTPUT_DIR = "images"

# ── 시스템 폰트 (ubuntu-latest 정확한 경로) ──────────────────────────────────
_FONT_BLACK   = "/usr/share/fonts/opentype/noto/NotoSansCJK-Black.ttc"
_FONT_BOLD    = "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc"
_FONT_REGULAR = "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"


# ═══════════════════════════════════════════════════════════════════════════════
# 폰트 헬퍼
# ═══════════════════════════════════════════════════════════════════════════════

def _font(size: int, weight: str = "bold") -> ImageFont.FreeTypeFont:
    """weight: 'black' | 'bold' | 'regular'"""
    if weight == "black":
        paths = [_FONT_BLACK, _FONT_BOLD]
    elif weight == "bold":
        paths = [_FONT_BOLD, _FONT_BLACK]
    else:
        paths = [_FONT_REGULAR, _FONT_BOLD]
    for p in paths:
        if Path(p).exists():
            try:
                return ImageFont.truetype(p, size, index=0)
            except Exception:
                continue
    return ImageFont.load_default()


# ═══════════════════════════════════════════════════════════════════════════════
# 픽셀 기반 줄바꿈
# ═══════════════════════════════════════════════════════════════════════════════

def _pixel_wrap(text: str, fnt: ImageFont.FreeTypeFont, max_px: int,
                max_lines: int = 3) -> list[str]:
    _img  = Image.new("RGB", (10, 10))
    _draw = ImageDraw.Draw(_img)

    def _w(t):
        return _draw.textbbox((0, 0), t, font=fnt)[2]

    words = text.split()
    lines, cur = [], ""
    for word in words:
        if len(lines) >= max_lines:
            break
        sep  = "" if not cur else " "
        cand = cur + sep + word
        if _w(cand) <= max_px:
            cur = cand
        else:
            if cur:
                lines.append(cur)
            if len(lines) >= max_lines:
                break
            if _w(word) > max_px:
                chunk = ""
                for ch in word:
                    if _w(chunk + ch) > max_px and chunk:
                        lines.append(chunk)
                        chunk = ch
                        if len(lines) >= max_lines:
                            break
                    else:
                        chunk += ch
                cur = chunk
            else:
                cur = word
    if cur and len(lines) < max_lines:
        lines.append(cur)
    return lines or [text[:20]]


# ═══════════════════════════════════════════════════════════════════════════════
# 외곽선 텍스트
# ═══════════════════════════════════════════════════════════════════════════════

def _outlined(draw, pos, text, fnt, fill, outline=(0, 0, 0), ow=3):
    x, y = pos
    for dx, dy in [(-ow, 0), (ow, 0), (0, -ow), (0, ow),
                   (-ow, -ow), (ow, -ow), (-ow, ow), (ow, ow)]:
        draw.text((x + dx, y + dy), text, font=fnt, fill=(*outline, 200))
    draw.text((x, y), text, font=fnt, fill=fill)


def _center_text(draw, cx, y, text, fnt, fill, outline=(0, 0, 0), ow=3) -> int:
    """중앙 정렬 외곽선 텍스트. 렌더 높이 반환."""
    bb = draw.textbbox((0, 0), text, font=fnt)
    w  = bb[2] - bb[0]
    h  = bb[3] - bb[1]
    _outlined(draw, (cx - w // 2, y), text, fnt, fill, outline, ow)
    return h


# ═══════════════════════════════════════════════════════════════════════════════
# 수치 강조 파싱
# ═══════════════════════════════════════════════════════════════════════════════

_NUM_RE = re.compile(
    r"([+-]?\d+(?:\.\d+)?%"
    r"|[+-]?\d+(?:\.\d+)?(?:포인트|달러|원|억|조|만)"
    r"|S&P\s*500|나스닥|다우|FOMC|Fed|연준)"
)


def _num_segs(text: str) -> list[tuple[str, bool]]:
    segs, last = [], 0
    for m in _NUM_RE.finditer(text):
        if m.start() > last:
            segs.append((text[last:m.start()], False))
        segs.append((m.group(), True))
        last = m.end()
    if last < len(text):
        segs.append((text[last:], False))
    return segs or [(text, False)]


# ═══════════════════════════════════════════════════════════════════════════════
# 배경 이미지 처리
# ═══════════════════════════════════════════════════════════════════════════════

def _make_gradient(W: int, H: int, colors: list[tuple]) -> Image.Image:
    """수직 그라디언트 배경 (2~3색)."""
    img  = Image.new("RGB", (W, H))
    d    = ImageDraw.Draw(img)
    n    = len(colors) - 1
    step = H // n
    for i in range(n):
        c0, c1 = colors[i], colors[i + 1]
        y0, y1 = i * step, (i + 1) * step
        for y in range(y0, y1):
            t = (y - y0) / (y1 - y0)
            r = int(c0[0] * (1 - t) + c1[0] * t)
            g = int(c0[1] * (1 - t) + c1[1] * t)
            b = int(c0[2] * (1 - t) + c1[2] * t)
            d.line([(0, y), (W, y)], fill=(r, g, b))
    return img


def _load_bg_image(url: str) -> Image.Image | None:
    if not url:
        return None
    try:
        r = requests.get(url, timeout=15, headers={"User-Agent": "Mozilla/5.0 (compatible)"})
        r.raise_for_status()
        img = Image.open(BytesIO(r.content)).convert("RGB")
        logger.info(f"배경 이미지 로드: {img.size}")
        return img
    except Exception as e:
        logger.warning(f"배경 이미지 로드 실패: {e}")
        return None


def _fetch_pexels_bg(query: str, W: int, H: int) -> Image.Image | None:
    key = os.environ.get("PEXELS_API_KEY", "")
    if not key:
        return None
    orient = "landscape" if W > H else "portrait"
    try:
        r = requests.get(
            "https://api.pexels.com/v1/search",
            headers={"Authorization": key},
            params={"query": query, "per_page": 5, "orientation": orient},
            timeout=12,
        )
        r.raise_for_status()
        photos = r.json().get("photos", [])
        if not photos:
            return None
        idx     = int(time.time() / 86400) % len(photos)
        img_url = photos[idx]["src"]["large2x"]
        ir      = requests.get(img_url, timeout=20)
        ir.raise_for_status()
        return Image.open(BytesIO(ir.content)).convert("RGB")
    except Exception as e:
        logger.warning(f"Pexels 썸네일 배경 실패 ({query}): {e}")
        return None


def _crop_fit(img: Image.Image, W: int, H: int) -> Image.Image:
    """비율 유지 중앙 크롭."""
    sw, sh = img.size
    if sw / sh > W / H:
        nh = sh; nw = int(nh * W / H)
        ox = (sw - nw) // 2
        img = img.crop((ox, 0, ox + nw, nh))
    else:
        nw = sw; nh = int(nw * H / W)
        oy = (sh - nh) // 3
        img = img.crop((0, oy, nw, oy + nh))
    return img.resize((W, H), Image.LANCZOS)


# ═══════════════════════════════════════════════════════════════════════════════
# 날짜 추출
# ═══════════════════════════════════════════════════════════════════════════════

def _extract_date(title: str) -> str:
    m = re.search(r"(\d{2,4})[.\-/년](\d{1,2})[.\-/월](\d{0,2})", title)
    if m:
        y, mo, d = m.group(1), m.group(2), m.group(3)
        if len(y) == 2:
            y = "20" + y
        return f"{y}.{mo.zfill(2)}.{d.zfill(2)}" if d else f"{y}.{mo.zfill(2)}"
    return datetime.now().strftime("%y.%m.%d")


# ═══════════════════════════════════════════════════════════════════════════════
# 임팩트 카피 생성
# ═══════════════════════════════════════════════════════════════════════════════

def _build_copy(title: str, platform_post: str, thumbnail_copy: str,
                platform: str) -> tuple[str, str]:
    """
    우선순위:
    1. content_adapter의 thumbnail_copy (Gemini가 직접 생성)
    2. 정규식으로 제목에서 수치/후킹 추출
    3. 제목 앞부분 잘라내기
    """
    MAX = {"facebook": 9, "threads": 8, "instagram": 8,
           "instagram_portrait": 8, "kakao": 10}
    max_main = MAX.get(platform, 8)

    # 1순위: thumbnail_copy
    if thumbnail_copy:
        parts = thumbnail_copy.strip().split("\n", 1)
        main  = parts[0].strip()[:max_main]
        sub   = parts[1].strip()[:22] if len(parts) > 1 else ""
        if not sub and platform_post:
            sub = platform_post.split("\n")[0].split("#")[0].strip()[:22]
        return main, sub

    # 2순위: 수치 패턴
    num_m = re.search(r"[+-]?\d+(?:\.\d+)?%", title)
    if num_m and len(num_m.group()) <= max_main:
        main = num_m.group()
    else:
        # 날짜·"미국증시" 등 제거
        clean = re.sub(r"\d{4}년\s*\d{1,2}월\s*\d{1,2}일\s*", "", title)
        clean = re.sub(r"미국\s*증시\s*[:：]?\s*", "", clean).strip()
        # 물음표/느낌표 앞 핵심어
        hook  = re.search(r"([가-힣a-zA-Z0-9 ]{3,12})[?!！？]", clean)
        main  = hook.group(1).strip()[:max_main] if hook else clean[:max_main]

    # 서브 카피
    sub = ""
    if platform_post:
        sub = platform_post.split("\n")[0].split("#")[0].strip()[:22]
    if not sub:
        sub = title[len(main):][:22].strip(" :：-")

    return main or title[:max_main], sub


# ═══════════════════════════════════════════════════════════════════════════════
# 플랫폼별 렌더러
# ═══════════════════════════════════════════════════════════════════════════════

def _render_facebook(
    bg: Image.Image | None,
    title: str, main_copy: str, sub_copy: str,
    date_str: str, mode: str,
) -> Image.Image:
    """1200×630 뉴스 카드. 하단 카드에 텍스트 집중."""
    W, H   = 1200, 630
    accent = (37, 150, 255) if mode == "morning" else (140, 100, 220)
    hl     = (254, 211, 48)

    if bg:
        bg_r = _crop_fit(bg, W, H)
        bg_r = bg_r.filter(ImageFilter.GaussianBlur(radius=5))
        img  = Image.alpha_composite(
            bg_r.convert("RGBA"),
            Image.new("RGBA", (W, H), (*((6, 12, 35)), 170)),
        ).convert("RGB")
    else:
        img = _make_gradient(W, H, [(6, 12, 35), (15, 30, 70), (6, 12, 35)])

    draw = ImageDraw.Draw(img)

    # 상단 컬러 바
    draw.rectangle([(0, 0), (W, 10)], fill=accent)

    # 하단 카드
    card_y = int(H * 0.38)
    draw.rounded_rectangle([(30, card_y), (W - 30, H - 20)],
                            radius=18, fill=(6, 12, 35, 220))

    # 좌측 액센트 바
    draw.rounded_rectangle([(42, card_y + 12), (50, H - 32)],
                            radius=4, fill=(*accent, 230))

    CX = W // 2
    WRAP_W = W - 160

    f_main = _font(int(90 * W / 1200), "black")
    f_sub  = _font(int(42 * W / 1200), "bold")
    f_tag  = _font(int(34 * W / 1200), "bold")
    f_sm   = _font(int(26 * W / 1200), "regular")

    # 날짜 뱃지
    db   = draw.textbbox((0, 0), date_str, font=f_tag)
    dw   = db[2] - db[0] + 30
    dh   = db[3] - db[1] + 18
    draw.rounded_rectangle([(W - 60 - dw, card_y + 18), (W - 30, card_y + 18 + dh)],
                            radius=dh // 2, fill=(*accent, 240))
    draw.text((W - 45 - (db[2] - db[0]), card_y + 27), date_str,
              font=f_tag, fill=(255, 255, 255))

    y = card_y + 30

    # 메인 카피
    has_num = bool(re.search(r"[+-]?\d", main_copy))
    mc_col  = (*hl, 255) if has_num else (255, 255, 255, 255)
    lines   = _pixel_wrap(main_copy, f_main, WRAP_W, max_lines=2)
    for line in lines:
        h = _center_text(draw, CX, y, line, f_main, mc_col[:3], ow=4)
        y += h + 8

    # 구분선
    draw.rectangle([(120, y + 10), (W - 120, y + 14)], fill=(*accent, 160))
    y += 30

    # 서브 카피 (수치 강조)
    sc_lines = _pixel_wrap(sub_copy, f_sub, WRAP_W, max_lines=2)
    for line in sc_lines:
        segs = _num_segs(line)
        total_w = sum(draw.textbbox((0, 0), s, font=f_sub)[2] for s, _ in segs)
        xc = CX - total_w // 2
        max_h = 0
        for st, is_hl in segs:
            c  = (*hl, 255) if is_hl else (200, 215, 235, 230)
            bb = draw.textbbox((0, 0), st, font=f_sub)
            sw = bb[2] - bb[0]
            sh = bb[3] - bb[1]
            _outlined(draw, (xc, y), st, f_sub, c[:3], ow=2)
            xc   += sw
            max_h = max(max_h, sh)
        y += max_h + 6

    # URL
    wm  = "seedsup.tistory.com"
    wbb = draw.textbbox((0, 0), wm, font=f_sm)
    draw.text((CX - (wbb[2] - wbb[0]) // 2, H - 50), wm,
              font=f_sm, fill=(*accent, 160))

    return img


def _render_threads(
    bg: Image.Image | None,
    title: str, main_copy: str, sub_copy: str,
    date_str: str, mode: str,
) -> Image.Image:
    """1080×1080 미니멀 흑백. 텍스트 중심."""
    W, H   = 1080, 1080
    accent = (255, 255, 255)
    hl     = (254, 211, 48)

    if bg:
        bg_r = _crop_fit(bg, W, H)
        # 흑백 처리
        bg_r = bg_r.convert("L").convert("RGB")
        bg_r = bg_r.filter(ImageFilter.GaussianBlur(radius=7))
        img  = Image.alpha_composite(
            bg_r.convert("RGBA"),
            Image.new("RGBA", (W, H), (4, 4, 8, 210)),
        ).convert("RGB")
    else:
        img = _make_gradient(W, H, [(4, 4, 8), (20, 20, 35), (4, 4, 8)])

    draw = ImageDraw.Draw(img)
    draw.rectangle([(0, 0), (W, 10)], fill=accent)

    CX     = W // 2
    WRAP_W = W - 160

    f_main = _font(int(100), "black")
    f_sub  = _font(int(48), "bold")
    f_tag  = _font(int(36), "bold")
    f_sm   = _font(int(28), "regular")

    # 날짜 뱃지
    db  = draw.textbbox((0, 0), date_str, font=f_tag)
    dw  = db[2] - db[0] + 30
    dh  = db[3] - db[1] + 18
    draw.rounded_rectangle([(CX - dw // 2, 30), (CX + dw // 2, 30 + dh)],
                            radius=dh // 2, fill=(255, 255, 255, 230))
    draw.text((CX - (db[2] - db[0]) // 2, 39), date_str,
              font=f_tag, fill=(4, 4, 8))

    # 중앙 카드
    card_h = int(H * 0.58)
    card_y = (H - card_h) // 2
    draw.rounded_rectangle([(40, card_y), (W - 40, card_y + card_h)],
                            radius=36, fill=(255, 255, 255, 18))

    y = card_y + 50

    has_num = bool(re.search(r"[+-]?\d", main_copy))
    mc_col  = (*hl, 255) if has_num else (255, 255, 255, 255)
    lines   = _pixel_wrap(main_copy, f_main, WRAP_W, max_lines=2)
    for line in lines:
        h = _center_text(draw, CX, y, line, f_main, mc_col[:3], ow=5)
        y += h + 10

    draw.rectangle([(120, y + 12), (W - 120, y + 16)], fill=(255, 255, 255, 150))
    y += 40

    sc_lines = _pixel_wrap(sub_copy, f_sub, WRAP_W, max_lines=2)
    for line in sc_lines:
        h = _center_text(draw, CX, y, line, f_sub, (180, 180, 195), ow=2)
        y += h + 8

    wm  = "seedsup.tistory.com"
    wbb = draw.textbbox((0, 0), wm, font=f_sm)
    draw.text((CX - (wbb[2] - wbb[0]) // 2, H - 55), wm,
              font=f_sm, fill=(160, 160, 175, 180))

    return img


def _render_instagram(
    bg: Image.Image | None,
    title: str, main_copy: str, sub_copy: str,
    date_str: str, mode: str, W: int = 1080, H: int = 1080,
) -> Image.Image:
    """Instagram 스퀘어/세로. 퍼플-핑크 그라디언트 강조."""
    accent = (192, 132, 252) if mode == "morning" else (167, 100, 240)
    hl     = (254, 211, 48)
    pink   = (244, 63, 94)

    if bg:
        bg_r = _crop_fit(bg, W, H)
        bg_r = bg_r.filter(ImageFilter.GaussianBlur(radius=6))
        img  = Image.alpha_composite(
            bg_r.convert("RGBA"),
            Image.new("RGBA", (W, H), (12, 5, 30, 185)),
        ).convert("RGB")
    else:
        img = _make_gradient(W, H, [(12, 5, 30), (30, 10, 60), (15, 5, 40)])

    draw = ImageDraw.Draw(img)

    # 그라디언트 상단 바 (퍼플→핑크)
    for px in range(W):
        t = px / W
        r = int(accent[0] * (1 - t) + pink[0] * t)
        g = int(accent[1] * (1 - t) + pink[1] * t)
        b = int(accent[2] * (1 - t) + pink[2] * t)
        draw.line([(px, 0), (px, 12)], fill=(r, g, b))

    CX     = W // 2
    WRAP_W = W - 140

    f_main = _font(int(100 * W / 1080), "black")
    f_sub  = _font(int(48 * W / 1080), "bold")
    f_tag  = _font(int(36 * W / 1080), "bold")
    f_sm   = _font(int(28 * W / 1080), "regular")

    # 날짜 뱃지
    db  = draw.textbbox((0, 0), date_str, font=f_tag)
    dw  = db[2] - db[0] + 30
    dh  = db[3] - db[1] + 18
    draw.rounded_rectangle([(CX - dw // 2, 30), (CX + dw // 2, 30 + dh)],
                            radius=dh // 2, fill=(*accent, 230))
    draw.text((CX - (db[2] - db[0]) // 2, 39), date_str,
              font=f_tag, fill=(12, 5, 30))

    # 중앙 카드
    card_h = int(H * 0.60)
    card_y = (H - card_h) // 2
    draw.rounded_rectangle([(35, card_y), (W - 35, card_y + card_h)],
                            radius=36, fill=(18, 8, 50, 210))

    # 좌측 그라디언트 액센트 바
    for py in range(card_y + 20, card_y + card_h - 20):
        t = (py - card_y) / card_h
        r = int(accent[0] * (1 - t) + pink[0] * t)
        g = int(accent[1] * (1 - t) + pink[1] * t)
        b = int(accent[2] * (1 - t) + pink[2] * t)
        draw.line([(48, py), (58, py)], fill=(r, g, b, 230))

    y = card_y + 40

    has_num = bool(re.search(r"[+-]?\d", main_copy))
    mc_col  = (*hl, 255) if has_num else (255, 255, 255, 255)
    lines   = _pixel_wrap(main_copy, f_main, WRAP_W, max_lines=2)
    for line in lines:
        h = _center_text(draw, CX, y, line, f_main, mc_col[:3], ow=5)
        y += h + 10

    draw.rectangle([(120, y + 14), (W - 120, y + 18)], fill=(*accent, 160))
    y += 40

    sc_lines = _pixel_wrap(sub_copy, f_sub, WRAP_W, max_lines=2)
    for line in sc_lines:
        segs    = _num_segs(line)
        total_w = sum(draw.textbbox((0, 0), s, font=f_sub)[2] for s, _ in segs)
        xc = CX - total_w // 2
        max_h = 0
        for st, is_hl in segs:
            c   = (*hl, 255) if is_hl else (216, 180, 254, 230)
            bb  = draw.textbbox((0, 0), st, font=f_sub)
            sw  = bb[2] - bb[0]
            sh  = bb[3] - bb[1]
            _outlined(draw, (xc, y), st, f_sub, c[:3], ow=2)
            xc   += sw
            max_h = max(max_h, sh)
        y += max_h + 8

    wm  = "seedsup.tistory.com"
    wbb = draw.textbbox((0, 0), wm, font=f_sm)
    draw.text((CX - (wbb[2] - wbb[0]) // 2, H - 55), wm,
              font=f_sm, fill=(*accent, 160))

    return img


def _render_kakao(
    bg: Image.Image | None,
    title: str, main_copy: str, sub_copy: str,
    date_str: str, mode: str,
) -> Image.Image:
    """1200×630 카카오 옐로우. 따뜻하고 친근한 톤."""
    W, H   = 1200, 630
    yellow = (254, 229, 0)
    dark   = (22, 14, 2)

    if bg:
        bg_r = _crop_fit(bg, W, H)
        # 웜 틴트
        warm = Image.new("RGB", (W, H), (255, 180, 50))
        bg_r = Image.blend(bg_r.convert("RGB"), warm, 0.2)
        bg_r = bg_r.filter(ImageFilter.GaussianBlur(radius=5))
        img  = Image.alpha_composite(
            bg_r.convert("RGBA"),
            Image.new("RGBA", (W, H), (28, 18, 2, 175)),
        ).convert("RGB")
    else:
        img = _make_gradient(W, H, [(28, 18, 2), (55, 35, 5), (28, 18, 2)])

    draw = ImageDraw.Draw(img)
    draw.rectangle([(0, 0), (W, 10)], fill=yellow)

    CX     = W // 2
    WRAP_W = W - 160

    f_main = _font(int(90 * W / 1200), "black")
    f_sub  = _font(int(42 * W / 1200), "bold")
    f_tag  = _font(int(34 * W / 1200), "bold")
    f_sm   = _font(int(26 * W / 1200), "regular")

    # 날짜 뱃지
    db  = draw.textbbox((0, 0), date_str, font=f_tag)
    dw  = db[2] - db[0] + 30
    dh  = db[3] - db[1] + 18
    draw.rounded_rectangle([(W - 60 - dw, 20), (W - 30, 20 + dh)],
                            radius=dh // 2, fill=(*yellow, 240))
    draw.text((W - 45 - (db[2] - db[0]), 29), date_str,
              font=f_tag, fill=dark)

    # 중앙 카드
    card_h = int(H * 0.72)
    card_y = (H - card_h) // 2
    draw.rounded_rectangle([(25, card_y), (W - 25, card_y + card_h)],
                            radius=28, fill=(22, 14, 2, 215))
    draw.rounded_rectangle([(37, card_y + 12), (45, card_y + card_h - 12)],
                            radius=4, fill=(*yellow, 230))

    y = card_y + 30

    has_num = bool(re.search(r"[+-]?\d", main_copy))
    mc_col  = (*yellow, 255) if has_num else (255, 255, 255, 255)
    lines   = _pixel_wrap(main_copy, f_main, WRAP_W, max_lines=2)
    for line in lines:
        h = _center_text(draw, CX, y, line, f_main, mc_col[:3], ow=4)
        y += h + 8

    draw.rectangle([(120, y + 10), (W - 120, y + 14)], fill=(*yellow, 160))
    y += 30

    sc_lines = _pixel_wrap(sub_copy, f_sub, WRAP_W, max_lines=2)
    for line in sc_lines:
        segs    = _num_segs(line)
        total_w = sum(draw.textbbox((0, 0), s, font=f_sub)[2] for s, _ in segs)
        xc  = CX - total_w // 2
        max_h = 0
        for st, is_hl in segs:
            c   = (*yellow, 255) if is_hl else (255, 230, 150, 230)
            bb  = draw.textbbox((0, 0), st, font=f_sub)
            sw  = bb[2] - bb[0]
            sh  = bb[3] - bb[1]
            _outlined(draw, (xc, y), st, f_sub, c[:3], ow=2)
            xc   += sw
            max_h = max(max_h, sh)
        y += max_h + 6

    wm  = "📊 카카오 스토리채널 | seedsup.tistory.com"
    wbb = draw.textbbox((0, 0), wm, font=f_sm)
    draw.text((CX - (wbb[2] - wbb[0]) // 2, H - 48), wm,
              font=f_sm, fill=(*yellow, 160))

    return img


# ═══════════════════════════════════════════════════════════════════════════════
# SNSThumbnailGenerator 메인 클래스
# ═══════════════════════════════════════════════════════════════════════════════

class SNSThumbnailGenerator:
    def __init__(self, hf_token: str = "", output_dir: str = OUTPUT_DIR):
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

        date_str       = _extract_date(title)
        thumbnail_copy = content.get("thumbnail_copy", "")

        # 배경 이미지 로드 (티스토리 → Pexels → None → 그라디언트)
        bg_img = _load_bg_image(thumbnail_url)
        if not bg_img:
            pexels_q = "finance stock market" if mode == "morning" else "city night finance"
            bg_img   = _fetch_pexels_bg(pexels_q, 1200, 630)

        platform_map = {
            "facebook":           "facebook_post",
            "threads":            "threads_post",
            "instagram":          "instagram_post",
            "instagram_portrait": "instagram_post",
            "kakao":              "kakao_post",
        }

        paths: dict[str, str] = {}

        for platform, post_key in platform_map.items():
            try:
                logger.info(f"[{platform}] 썸네일 생성 중...")
                ppost    = content.get(post_key, "")
                main_c, sub_c = _build_copy(title, ppost, thumbnail_copy, platform)
                logger.info(f"  메인: '{main_c}' / 서브: '{sub_c[:20]}'")

                if platform == "facebook":
                    img = _render_facebook(bg_img, title, main_c, sub_c, date_str, mode)
                elif platform == "threads":
                    img = _render_threads(bg_img, title, main_c, sub_c, date_str, mode)
                elif platform == "instagram":
                    img = _render_instagram(bg_img, title, main_c, sub_c, date_str, mode,
                                            1080, 1080)
                elif platform == "instagram_portrait":
                    img = _render_instagram(bg_img, title, main_c, sub_c, date_str, mode,
                                            1080, 1350)
                elif platform == "kakao":
                    img = _render_kakao(bg_img, title, main_c, sub_c, date_str, mode)
                else:
                    continue

                filename = f"thumb_{platform}_{mode}_{timestamp}.jpg"
                path     = os.path.join(self.output_dir, filename)
                img.save(path, "JPEG", quality=95, optimize=True)
                paths[platform] = path
                logger.info(f"  → 저장: {path}")

            except Exception as e:
                logger.error(f"[{platform}] 썸네일 생성 실패: {e}", exc_info=True)

        return paths
