"""
SNS 썸네일 생성기 v5
=====================================
개선사항:
  - 썸네일 이미지 품질 대폭 개선:
      1순위: Gemini Imagen API (gemini-nano-banana 모델)로 카툰 스타일 이미지 생성
      2순위: Pexels/Pixabay/Unsplash 무료 이미지 + 블로그 제목/키워드 오버레이
      3순위: 그라디언트 배경 + 키워드 텍스트
  - 쓰레드 썸네일 업로드 방식 개선 (이미지 URL 직접 업로드 지원)
  - 이모지 제거로 텍스트 깨짐 방지
"""

import logging
import os
import re
import time
from datetime import datetime
from io import BytesIO
from pathlib import Path

import requests
from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageEnhance

logger = logging.getLogger(__name__)

OUTPUT_DIR = "images"

# ── 시스템 폰트 ──────────────────────────────────────────────────────────────
_FONT_BLACK   = "/usr/share/fonts/opentype/noto/NotoSansCJK-Black.ttc"
_FONT_BOLD    = "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc"
_FONT_REGULAR = "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"

# ── 이모지 제거 (한글 보존) ──────────────────────────────────────────────────
# 주의: \U000024C2-\U0001F251 범위는 한글(AC00-D7FF)을 포함하므로 사용 금지
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
# 폰트 헬퍼
# ═══════════════════════════════════════════════════════════════════════════════

def _font(size: int, weight: str = "bold") -> ImageFont.FreeTypeFont:
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

def _pixel_wrap(text: str, fnt, max_px: int, max_lines: int = 3) -> list[str]:
    _img  = Image.new("RGB", (10, 10))
    _draw = ImageDraw.Draw(_img)

    def _w(t):
        return _draw.textbbox((0, 0), t, font=fnt)[2]

    words  = text.split()
    lines  = []
    cur    = ""
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
    bb = draw.textbbox((0, 0), text, font=fnt)
    w  = bb[2] - bb[0]
    h  = bb[3] - bb[1]
    _outlined(draw, (cx - w // 2, y), text, fnt, fill, outline, ow)
    return h


# ═══════════════════════════════════════════════════════════════════════════════
# 이미지 소스 1: Gemini Imagen (gemini-2.0-flash-exp 이미지 생성)
# ═══════════════════════════════════════════════════════════════════════════════

def _generate_with_gemini_imagen(prompt: str, gemini_key: str) -> Image.Image | None:
    """
    Gemini API의 이미지 생성 기능으로 카툰 스타일 썸네일 생성.
    현재 무료 사용 가능한 gemini-2.0-flash-exp-image-generation 모델 사용.
    """
    if not gemini_key:
        return None

    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash-exp-image-generation:generateContent?key={gemini_key}"

    # 카툰/일러스트 스타일 프롬프트
    full_prompt = (
        f"{prompt}, "
        "cartoon style illustration, flat design, vibrant colors, "
        "financial news cartoon, clean and professional, "
        "no text, no watermark, 16:9 aspect ratio"
    )

    payload = {
        "contents": [{"parts": [{"text": full_prompt}]}],
        "generationConfig": {"responseModalities": ["IMAGE", "TEXT"]},
    }

    try:
        resp = requests.post(url, json=payload, timeout=60)
        if resp.status_code != 200:
            logger.warning(f"Gemini Imagen 실패 ({resp.status_code}): {resp.text[:200]}")
            return None

        data = resp.json()
        for part in data.get("candidates", [{}])[0].get("content", {}).get("parts", []):
            if part.get("inlineData", {}).get("mimeType", "").startswith("image/"):
                import base64
                img_bytes = base64.b64decode(part["inlineData"]["data"])
                img       = Image.open(BytesIO(img_bytes)).convert("RGB")
                logger.info(f"Gemini Imagen 생성 성공: {img.size}")
                return img

        logger.warning("Gemini Imagen 응답에 이미지 없음")
        return None
    except Exception as e:
        logger.warning(f"Gemini Imagen 오류: {e}")
        return None


# ═══════════════════════════════════════════════════════════════════════════════
# 이미지 소스 2: Pexels 무료 이미지
# ═══════════════════════════════════════════════════════════════════════════════

def _fetch_pexels_image(query: str, W: int, H: int, pexels_key: str) -> Image.Image | None:
    if not pexels_key:
        return None
    orient = "landscape" if W > H else "portrait"
    try:
        r = requests.get(
            "https://api.pexels.com/v1/search",
            headers={"Authorization": pexels_key},
            params={"query": query, "per_page": 8, "orientation": orient},
            timeout=12,
        )
        r.raise_for_status()
        photos = r.json().get("photos", [])
        if not photos:
            return None
        idx = int(time.time() / 86400) % len(photos)
        ir  = requests.get(photos[idx]["src"]["large2x"], timeout=20)
        ir.raise_for_status()
        return Image.open(BytesIO(ir.content)).convert("RGB")
    except Exception as e:
        logger.warning(f"Pexels 실패 ({query}): {e}")
        return None


# ═══════════════════════════════════════════════════════════════════════════════
# 이미지 소스 3: Pixabay 무료 이미지 (API 키 필요)
# ═══════════════════════════════════════════════════════════════════════════════

def _fetch_pixabay_image(query: str, W: int, H: int, pixabay_key: str) -> Image.Image | None:
    if not pixabay_key:
        return None
    orient = "horizontal" if W > H else "vertical"
    try:
        r = requests.get(
            "https://pixabay.com/api/",
            params={
                "key": pixabay_key,
                "q": query,
                "image_type": "photo",
                "orientation": orient,
                "per_page": 10,
                "safesearch": "true",
                "category": "business",
            },
            timeout=12,
        )
        r.raise_for_status()
        hits = r.json().get("hits", [])
        if not hits:
            return None
        idx = int(time.time() / 86400) % len(hits)
        ir  = requests.get(hits[idx]["largeImageURL"], timeout=20)
        ir.raise_for_status()
        return Image.open(BytesIO(ir.content)).convert("RGB")
    except Exception as e:
        logger.warning(f"Pixabay 실패 ({query}): {e}")
        return None


# ═══════════════════════════════════════════════════════════════════════════════
# 이미지 소스 4: Unsplash Source API (무료, API 키 불필요)
# ═══════════════════════════════════════════════════════════════════════════════

def _fetch_unsplash_image(query: str, W: int, H: int, mode: str) -> Image.Image | None:
    """
    Unsplash source API (source.unsplash.com) - 무료, API 키 불필요.
    날짜 기반 sig로 매일 다른 이미지.
    """
    today_sig = datetime.now().strftime(f"%Y%m%d_{mode}")
    kw        = query.replace(" ", ",")
    url       = f"https://source.unsplash.com/{W}x{H}/?{kw}&sig={today_sig}"
    try:
        r = requests.get(url, timeout=20, headers={"User-Agent": "Mozilla/5.0"},
                         allow_redirects=True)
        if r.status_code == 200 and len(r.content) > 10000:
            return Image.open(BytesIO(r.content)).convert("RGB")
    except Exception as e:
        logger.warning(f"Unsplash 실패: {e}")
    return None


# ═══════════════════════════════════════════════════════════════════════════════
# 이미지 크롭/리사이즈
# ═══════════════════════════════════════════════════════════════════════════════

def _crop_fit(img: Image.Image, W: int, H: int) -> Image.Image:
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
# 그라디언트 배경 (fallback)
# ═══════════════════════════════════════════════════════════════════════════════

def _make_gradient(W: int, H: int, colors: list[tuple]) -> Image.Image:
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


# ═══════════════════════════════════════════════════════════════════════════════
# 키워드 추출 (이미지 검색용)
# ═══════════════════════════════════════════════════════════════════════════════

def _extract_image_query(title: str, mode: str, content: dict) -> str:
    """블로그 제목에서 이미지 검색 키워드 추출."""
    keyword_map = {
        "나스닥": "nasdaq stock market trading",
        "S&P": "SP500 wall street finance",
        "반도체": "semiconductor chip technology",
        "빅테크": "big tech silicon valley",
        "연준": "federal reserve bank economy",
        "금리": "interest rate finance banking",
        "AI": "artificial intelligence technology",
        "엔비디아": "nvidia technology semiconductor",
        "애플": "apple technology innovation",
        "테슬라": "electric vehicle technology",
        "유가": "oil energy market",
        "급락": "stock market crash red",
        "반등": "stock market recovery green",
        "상승": "bull market stock exchange",
        "하락": "bear market finance",
        "고용": "employment jobs economy",
        "인플레": "inflation economy money",
        "FOMC": "federal reserve meeting economy",
    }

    for ko, en in keyword_map.items():
        if ko in title:
            return en

    if mode == "morning":
        return "wall street stock exchange morning finance"
    return "stock market trading floor night city"


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
# 핵심 키워드 추출 (제목 오버레이용)
# ═══════════════════════════════════════════════════════════════════════════════

def _extract_headline(title: str, mode: str) -> tuple[str, str]:
    """제목에서 헤드라인 키워드와 서브텍스트 추출."""
    # 날짜 패턴 제거
    clean = re.sub(r"\d{4}년\s*\d{1,2}월\s*\d{1,2}일\s*", "", title)
    clean = re.sub(r"\d{1,2}/\d{1,2}\s*", "", clean)
    clean = re.sub(r"미국\s*증시\s*[:：]?\s*", "", clean)
    clean = _strip_emoji(clean).strip()

    # 숫자/수치 추출
    num_m = re.search(r"[+-]?\d+(?:\.\d+)?%", clean)

    if mode == "evening":
        sub = "오늘 밤 프리마켓"
    else:
        sub = "전일 마감 분석"

    if num_m:
        return num_m.group(), clean[:22] if len(clean) > 4 else sub
    elif len(clean) > 4:
        # 훅 키워드 (물음표, 느낌표 앞)
        hook = re.search(r"([가-힣a-zA-Z0-9 ]{3,12})[?!！？]", clean)
        if hook:
            return hook.group(1).strip()[:10], clean[:22]
        return clean[:10], sub
    return "증시 분석", sub


# ═══════════════════════════════════════════════════════════════════════════════
# 이미지에 텍스트 오버레이 추가 (고품질)
# ═══════════════════════════════════════════════════════════════════════════════

def _overlay_text_on_image(
    img: Image.Image,
    title: str,
    mode: str,
    platform: str,
    date_str: str,
    blog_url: str = "seedsup.tistory.com",
) -> Image.Image:
    """
    이미지 위에 블로그 제목 / 날짜 / URL을 오버레이.
    이미지 하단 1/3에 반투명 그라디언트 + 텍스트.
    """
    W, H   = img.size
    result = img.copy().convert("RGBA")
    draw   = ImageDraw.Draw(result)

    accent = (37, 150, 255) if mode == "morning" else (140, 100, 220)
    hl     = (254, 211, 48)

    # 하단 그라디언트 오버레이 (텍스트 가독성)
    grad = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    gd   = ImageDraw.Draw(grad)
    grad_start = int(H * 0.45)
    for y in range(grad_start, H):
        alpha = int(220 * ((y - grad_start) / (H - grad_start)) ** 0.7)
        gd.line([(0, y), (W, y)], fill=(6, 10, 30, min(alpha, 225)))
    result = Image.alpha_composite(result, grad)
    draw   = ImageDraw.Draw(result)

    # 상단 컬러 바
    draw.rectangle([(0, 0), (W, 8)], fill=(*accent, 255))

    # 폰트
    f_headline = _font(int(H * 0.07), "black")
    f_sub      = _font(int(H * 0.038), "bold")
    f_small    = _font(int(H * 0.028), "regular")
    f_date     = _font(int(H * 0.032), "bold")

    CX   = W // 2
    WRAPW = int(W * 0.88)

    # 날짜 뱃지 (상단 우측)
    db  = draw.textbbox((0, 0), date_str, font=f_date)
    dw  = db[2] - db[0] + 24
    dh  = db[3] - db[1] + 14
    draw.rounded_rectangle([(W - 20 - dw, 18), (W - 20, 18 + dh)],
                            radius=dh // 2, fill=(*accent, 230))
    draw.text((W - 20 - dw + 12, 18 + 7), date_str, font=f_date, fill=(255, 255, 255))

    # 모드 뱃지 (상단 좌측)
    mode_txt = "마감 리뷰" if mode == "morning" else "프리마켓"
    mb       = draw.textbbox((0, 0), mode_txt, font=f_date)
    mw       = mb[2] - mb[0] + 24
    mh       = mb[3] - mb[1] + 14
    draw.rounded_rectangle([(20, 18), (20 + mw, 18 + mh)],
                            radius=mh // 2, fill=(*hl, 230))
    draw.text((32, 18 + 7), mode_txt, font=f_date, fill=(20, 20, 20))

    # 제목 (하단 영역)
    title_clean = _strip_emoji(title)
    # 날짜 제거
    title_clean = re.sub(r"\d{4}년\s*\d{1,2}월\s*\d{1,2}일\s*", "", title_clean)
    title_clean = re.sub(r"미국\s*증시\s*[:：]?\s*", "", title_clean).strip()

    text_y = int(H * 0.52)
    lines  = _pixel_wrap(title_clean, f_headline, WRAPW, max_lines=3)
    for line in lines:
        h = _center_text(draw, CX, text_y, line, f_headline, (255, 255, 255), ow=4)
        text_y += h + 6

    # 구분선
    draw.rectangle([(int(W * 0.1), text_y + 8), (int(W * 0.9), text_y + 11)],
                   fill=(*accent, 160))
    text_y += 20

    # URL
    wbb = draw.textbbox((0, 0), blog_url, font=f_small)
    draw.text((CX - (wbb[2] - wbb[0]) // 2, text_y + 4),
              blog_url, font=f_small, fill=(*accent, 200))

    return result.convert("RGB")


# ═══════════════════════════════════════════════════════════════════════════════
# 플랫폼별 썸네일 생성
# ═══════════════════════════════════════════════════════════════════════════════

def _get_platform_size(platform: str) -> tuple[int, int]:
    sizes = {
        "facebook":           (1200, 630),
        "threads":            (1080, 1080),
        "instagram":          (1080, 1080),
        "instagram_portrait": (1080, 1350),
        "kakao":              (1200, 630),
    }
    return sizes.get(platform, (1200, 630))


def _build_thumbnail(
    bg_image: Image.Image | None,
    title: str,
    mode: str,
    platform: str,
    date_str: str,
    blog_url: str,
) -> Image.Image:
    """
    배경 이미지 + 텍스트 오버레이로 고품질 썸네일 생성.
    배경 없을 시 그라디언트로 대체.
    """
    W, H = _get_platform_size(platform)

    accent = (37, 150, 255) if mode == "morning" else (140, 100, 220)

    if bg_image:
        img = _crop_fit(bg_image.copy(), W, H)
        # 색감 약간 보정 (선명도 향상)
        img = ImageEnhance.Contrast(img).enhance(1.1)
        img = ImageEnhance.Color(img).enhance(1.15)   # Saturation → Color (올바른 Pillow API)
    else:
        if mode == "morning":
            img = _make_gradient(W, H, [(6, 20, 60), (15, 50, 110), (6, 20, 60)])
        else:
            img = _make_gradient(W, H, [(20, 5, 55), (50, 15, 100), (20, 5, 55)])

    # 텍스트 오버레이
    result = _overlay_text_on_image(img, title, mode, platform, date_str, blog_url)
    return result


# ═══════════════════════════════════════════════════════════════════════════════
# SNSThumbnailGenerator 메인 클래스
# ═══════════════════════════════════════════════════════════════════════════════

class SNSThumbnailGenerator:
    def __init__(self, hf_token: str = "", output_dir: str = OUTPUT_DIR):
        self.output_dir  = output_dir
        self.hf_token    = hf_token
        self.gemini_key  = os.environ.get("GEMINI_API_KEY", "")
        self.pexels_key  = os.environ.get("PEXELS_API_KEY", "")
        self.pixabay_key = os.environ.get("PIXABAY_API_KEY", "")
        os.makedirs(output_dir, exist_ok=True)

    def _acquire_base_image(
        self,
        title: str,
        mode: str,
        thumbnail_url: str,
        content: dict,
        W: int,
        H: int,
    ) -> Image.Image | None:
        """
        우선순위에 따라 배경 이미지 취득:
        1. 티스토리 썸네일
        2. Pexels
        3. Pixabay
        4. Unsplash Source
        5. None (그라디언트 fallback)
        """
        # 1. 티스토리 썸네일
        if thumbnail_url:
            try:
                r = requests.get(thumbnail_url, timeout=15,
                                 headers={"User-Agent": "Mozilla/5.0"})
                r.raise_for_status()
                if len(r.content) > 10000:
                    img = Image.open(BytesIO(r.content)).convert("RGB")
                    logger.info("티스토리 썸네일 사용")
                    return img
            except Exception as e:
                logger.warning(f"티스토리 썸네일 실패: {e}")

        # 2. Pexels
        query = _extract_image_query(title, mode, content)
        img   = _fetch_pexels_image(query, W, H, self.pexels_key)
        if img:
            return img

        # 3. Pixabay
        img = _fetch_pixabay_image(query, W, H, self.pixabay_key)
        if img:
            return img

        # 4. Unsplash Source (무료, API 키 불필요)
        img = _fetch_unsplash_image(query, W, H, mode)
        if img:
            return img

        logger.warning("모든 이미지 소스 실패 → 그라디언트 사용")
        return None

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
        content  = content or {}
        date_str = _extract_date(title)

        platform_list = ["facebook", "threads", "instagram", "instagram_portrait", "kakao"]
        paths: dict[str, str] = {}

        for platform in platform_list:
            try:
                logger.info(f"[{platform}] 썸네일 생성 중...")
                W, H = _get_platform_size(platform)

                # 배경 이미지 취득
                bg_img = self._acquire_base_image(title, mode, thumbnail_url, content, W, H)

                # 썸네일 생성
                img = _build_thumbnail(bg_img, title, mode, platform, date_str, blog_url)

                # 저장
                filename = f"thumb_{platform}_{mode}_{timestamp}.jpg"
                path     = os.path.join(self.output_dir, filename)
                img.save(path, "JPEG", quality=95, optimize=True)
                paths[platform] = path
                logger.info(f"  → 저장: {path} ({img.size})")

            except Exception as e:
                logger.error(f"[{platform}] 썸네일 생성 실패: {e}", exc_info=True)

        return paths
