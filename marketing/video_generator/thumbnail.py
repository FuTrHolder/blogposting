"""
SNS 썸네일 생성기 v6 — FLUX.1-schnell 기반
=====================================
우선순위 (v5 → v6 변경):
  1순위: HuggingFace FLUX.1-schnell (AI 생성 — 콘텐츠 맞춤형)
  2순위: Pexels API (스톡사진 + 텍스트 오버레이)
  3순위: Pixabay API (스톡사진 + 텍스트 오버레이)
  4순위: gradient fallback + 텍스트 오버레이

v5 → v6 변경 이유:
  - Gemini Imagen(gemini-2.0-flash-exp-image-generation): 2025년 6월 discontinued
    → 항상 실패, 제거
  - Unsplash Source: 2024년 서비스 완전 종료(503 반환), 제거
  - FLUX.1-schnell을 1순위로 배치:
    image_prompt(Gemini 생성)를 그대로 사용 → 콘텐츠와 연계된 이미지
    모든 플랫폼이 동일한 AI 생성 배경을 공유하되,
    텍스트 오버레이(제목/날짜/URL/배지)는 Pillow로 각 플랫폼 규격에 맞게 적용
"""

import logging
import os
import re
import time
import hashlib
from datetime import datetime
from io import BytesIO
from pathlib import Path

import requests
from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageEnhance

logger = logging.getLogger(__name__)

OUTPUT_DIR = "images"

# ── HuggingFace FLUX.1-schnell ────────────────────────────────────────────────
HF_FLUX_URL = (
    "https://router.huggingface.co/hf-inference/models/"
    "black-forest-labs/FLUX.1-schnell"
)

FLUX_SUFFIX = {
    "morning": (
        ", Wall Street financial district at dawn, calm after market close, "
        "professional financial photography, stock market analysis, "
        "blue and gold color palette, cinematic lighting, 8K resolution, "
        "no text, no watermark, no logo"
    ),
    "evening": (
        ", pre-market trading floor at night, dynamic stock exchange screens, "
        "professional financial photography, urgent market atmosphere, "
        "purple and amber color palette, dramatic lighting, 8K resolution, "
        "no text, no watermark, no logo"
    ),
}

# ── 시스템 폰트 ───────────────────────────────────────────────────────────────
_FONT_BLACK   = "/usr/share/fonts/opentype/noto/NotoSansCJK-Black.ttc"
_FONT_BOLD    = "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc"
_FONT_REGULAR = "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"

# ── 이모지 제거 ───────────────────────────────────────────────────────────────
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
    "\U0000270F\U00002712\U00002714\U00002716"
    "\U0000271D\U00002721\U00002728"
    "\U00002733-\U00002734\U00002744\U00002747"
    "\U0000274C\U0000274E\U00002753-\U00002755\U00002757"
    "\U00002763-\U00002764\U00002795-\U00002797"
    "\U000027A1\U000027B0\U000027BF"
    "]+",
    flags=re.UNICODE,
)

def _strip_emoji(text: str) -> str:
    return _EMOJI_RE.sub("", text).strip()


# ── 폰트 헬퍼 ────────────────────────────────────────────────────────────────
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


# ── 픽셀 기반 줄바꿈 ──────────────────────────────────────────────────────────
def _pixel_wrap(text: str, fnt, max_px: int, max_lines: int = 3) -> list[str]:
    _img  = Image.new("RGB", (10, 10))
    _draw = ImageDraw.Draw(_img)
    def _w(t):
        return _draw.textbbox((0, 0), t, font=fnt)[2]
    words, lines, cur = text.split(), [], ""
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


# ── 외곽선 텍스트 ─────────────────────────────────────────────────────────────
def _outlined(draw, pos, text, fnt, fill, outline=(0, 0, 0), ow=3):
    x, y = pos
    for dx, dy in [(-ow,0),(ow,0),(0,-ow),(0,ow),
                   (-ow,-ow),(ow,-ow),(-ow,ow),(ow,ow)]:
        draw.text((x+dx, y+dy), text, font=fnt, fill=(*outline, 200))
    draw.text((x, y), text, font=fnt, fill=fill)

def _center_text(draw, cx, y, text, fnt, fill, outline=(0,0,0), ow=3) -> int:
    bb = draw.textbbox((0, 0), text, font=fnt)
    w  = bb[2] - bb[0]
    h  = bb[3] - bb[1]
    _outlined(draw, (cx - w//2, y), text, fnt, fill, outline, ow)
    return h


# ── 이미지 크롭/리사이즈 ──────────────────────────────────────────────────────
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


# ── gradient fallback 배경 ────────────────────────────────────────────────────
def _make_gradient(W: int, H: int, colors: list[tuple]) -> Image.Image:
    img  = Image.new("RGB", (W, H))
    d    = ImageDraw.Draw(img)
    n    = len(colors) - 1
    step = H // n
    for i in range(n):
        c0, c1 = colors[i], colors[i+1]
        y0, y1 = i * step, (i+1) * step
        for y in range(y0, y1):
            t = (y - y0) / (y1 - y0)
            r = int(c0[0]*(1-t) + c1[0]*t)
            g = int(c0[1]*(1-t) + c1[1]*t)
            b = int(c0[2]*(1-t) + c1[2]*t)
            d.line([(0, y), (W, y)], fill=(r, g, b))
    return img


# ── 날짜 추출 ─────────────────────────────────────────────────────────────────
def _extract_date(title: str) -> str:
    m = re.search(r"(\d{2,4})[.\-/년](\d{1,2})[.\-/월](\d{0,2})", title)
    if m:
        y, mo, d = m.group(1), m.group(2), m.group(3)
        if len(y) == 2:
            y = "20" + y
        return f"{y}.{mo.zfill(2)}.{d.zfill(2)}" if d else f"{y}.{mo.zfill(2)}"
    return datetime.now().strftime("%y.%m.%d")


# ── 텍스트 오버레이 ───────────────────────────────────────────────────────────
def _overlay_text_on_image(
    img: Image.Image,
    title: str,
    mode: str,
    platform: str,
    date_str: str,
    blog_url: str = "seedsup.tistory.com",
) -> Image.Image:
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

    f_headline = _font(int(H * 0.07), "black")
    f_small    = _font(int(H * 0.028), "regular")
    f_date     = _font(int(H * 0.032), "bold")

    CX    = W // 2
    WRAPW = int(W * 0.88)

    # 날짜 뱃지 (상단 우측)
    db  = draw.textbbox((0, 0), date_str, font=f_date)
    dw  = db[2] - db[0] + 24
    dh  = db[3] - db[1] + 14
    draw.rounded_rectangle([(W-20-dw, 18), (W-20, 18+dh)],
                            radius=dh//2, fill=(*accent, 230))
    draw.text((W-20-dw+12, 18+7), date_str, font=f_date, fill=(255,255,255))

    # 모드 뱃지 (상단 좌측)
    mode_txt = "마감 리뷰" if mode == "morning" else "프리마켓"
    mb  = draw.textbbox((0, 0), mode_txt, font=f_date)
    mw  = mb[2] - mb[0] + 24
    mh  = mb[3] - mb[1] + 14
    draw.rounded_rectangle([(20, 18), (20+mw, 18+mh)],
                            radius=mh//2, fill=(*hl, 230))
    draw.text((32, 18+7), mode_txt, font=f_date, fill=(20,20,20))

    # 제목 (하단 영역)
    title_clean = _strip_emoji(title)
    title_clean = re.sub(r"\d{4}년\s*\d{1,2}월\s*\d{1,2}일\s*", "", title_clean)
    title_clean = re.sub(r"미국\s*증시\s*[:：]?\s*", "", title_clean).strip()

    text_y = int(H * 0.52)
    lines  = _pixel_wrap(title_clean, f_headline, WRAPW, max_lines=3)
    for line in lines:
        h = _center_text(draw, CX, text_y, line, f_headline, (255,255,255), ow=4)
        text_y += h + 6

    # 구분선
    draw.rectangle([(int(W*0.1), text_y+8), (int(W*0.9), text_y+11)],
                   fill=(*accent, 160))
    text_y += 20

    # URL
    wbb = draw.textbbox((0, 0), blog_url, font=f_small)
    draw.text((CX-(wbb[2]-wbb[0])//2, text_y+4),
              blog_url, font=f_small, fill=(*accent, 200))

    return result.convert("RGB")


# ── 플랫폼별 크기 ─────────────────────────────────────────────────────────────
def _get_platform_size(platform: str) -> tuple[int, int]:
    sizes = {
        "facebook":           (1200, 630),
        "threads":            (1080, 1080),
        "instagram":          (1080, 1080),
        "instagram_portrait": (1080, 1350),
        "kakao":              (1200, 630),
    }
    return sizes.get(platform, (1200, 630))


# ── 썸네일 조립 ───────────────────────────────────────────────────────────────
def _build_thumbnail(
    bg_image: Image.Image | None,
    title: str,
    mode: str,
    platform: str,
    date_str: str,
    blog_url: str,
) -> Image.Image:
    W, H = _get_platform_size(platform)

    if bg_image:
        img = _crop_fit(bg_image.copy(), W, H)
        img = ImageEnhance.Contrast(img).enhance(1.1)
        img = ImageEnhance.Color(img).enhance(1.15)
    else:
        if mode == "morning":
            img = _make_gradient(W, H, [(6,20,60),(15,50,110),(6,20,60)])
        else:
            img = _make_gradient(W, H, [(20,5,55),(50,15,100),(20,5,55)])

    return _overlay_text_on_image(img, title, mode, platform, date_str, blog_url)


# ═══════════════════════════════════════════════════════════════════════════════
# SNSThumbnailGenerator
# ═══════════════════════════════════════════════════════════════════════════════

class SNSThumbnailGenerator:
    def __init__(self, hf_token: str = "", output_dir: str = OUTPUT_DIR):
        self.output_dir  = output_dir
        self.hf_token    = hf_token
        self.pexels_key  = os.environ.get("PEXELS_API_KEY", "")
        self.pixabay_key = os.environ.get("PIXABAY_API_KEY", "")
        os.makedirs(output_dir, exist_ok=True)

    # ── FLUX.1-schnell 배경 이미지 생성 ──────────────────────────────────────
    def _generate_flux_bg(
        self, prompt: str, mode: str, W: int, H: int, max_retries: int = 3
    ) -> Image.Image | None:
        if not self.hf_token:
            logger.info("HF_API_TOKEN 미설정 — FLUX 건너뜀")
            return None

        suffix = FLUX_SUFFIX.get(mode, FLUX_SUFFIX["morning"])
        full_prompt = f"{prompt}{suffix}"
        seed = int(hashlib.md5(
            f"{datetime.now().strftime('%Y%m%d')}{mode}".encode()
        ).hexdigest()[:8], 16) % (2 ** 32)

        payload = {
            "inputs": full_prompt,
            "parameters": {
                "num_inference_steps": 4,
                "guidance_scale": 0.0,
                "width": 1024,
                "height": 1024,   # 정사각형으로 생성 후 각 플랫폼에 맞게 크롭
                "seed": seed,
            },
        }
        headers = {"Authorization": f"Bearer {self.hf_token}"}

        logger.info(f"FLUX SNS 배경 생성 중 (모드: {mode}, 시드: {seed})...")

        for attempt in range(1, max_retries + 1):
            try:
                resp = requests.post(
                    HF_FLUX_URL, headers=headers, json=payload, timeout=60
                )
                if resp.status_code == 200 and len(resp.content) > 10_000:
                    img = Image.open(BytesIO(resp.content)).convert("RGB")
                    logger.info(f"FLUX SNS 배경 생성 성공: {img.size}")
                    return img

                elif resp.status_code == 503:
                    try:
                        wait = min(resp.json().get("estimated_time", 20), 40)
                    except Exception:
                        wait = 20
                    logger.warning(
                        f"FLUX 모델 로딩 중 (503) — {wait:.0f}초 대기 "
                        f"(시도 {attempt}/{max_retries})..."
                    )
                    time.sleep(wait)

                elif resp.status_code == 429:
                    logger.warning("FLUX 요청 한도 초과 (429)")
                    return None

                else:
                    logger.warning(f"FLUX SNS 실패 ({resp.status_code}): {resp.text[:150]}")
                    return None

            except requests.exceptions.Timeout:
                logger.warning(f"FLUX SNS 타임아웃 (시도 {attempt}/{max_retries})")
                if attempt < max_retries:
                    time.sleep(5)
            except Exception as e:
                logger.warning(f"FLUX SNS 오류: {e}")
                return None

        return None

    # ── Pexels 배경 이미지 ────────────────────────────────────────────────────
    def _fetch_pexels_bg(self, query: str, W: int, H: int) -> Image.Image | None:
        if not self.pexels_key:
            return None
        orient = "landscape" if W > H else "portrait"
        try:
            r = requests.get(
                "https://api.pexels.com/v1/search",
                headers={"Authorization": self.pexels_key},
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
            logger.warning(f"Pexels SNS 실패: {e}")
            return None

    # ── Pixabay 배경 이미지 ───────────────────────────────────────────────────
    def _fetch_pixabay_bg(self, query: str, W: int, H: int) -> Image.Image | None:
        if not self.pixabay_key:
            return None
        orient = "horizontal" if W > H else "vertical"
        try:
            r = requests.get(
                "https://pixabay.com/api/",
                params={
                    "key": self.pixabay_key,
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
            logger.warning(f"Pixabay SNS 실패: {e}")
            return None

    # ── 배경 이미지 취득 (우선순위 적용) ─────────────────────────────────────
    def _acquire_base_image(
        self,
        title: str,
        mode: str,
        thumbnail_url: str,
        content: dict,
        W: int,
        H: int,
        image_prompt: str = "",
    ) -> Image.Image | None:
        # 0순위: FLUX.1-schnell (image_prompt 또는 title 기반)
        flux_prompt = image_prompt or title
        img = self._generate_flux_bg(flux_prompt, mode, W, H)
        if img:
            return img

        logger.info("FLUX 실패 → Pexels/Pixabay로 대체...")

        # 1순위: Pexels
        query = self._extract_query(title, mode, content)
        img = self._fetch_pexels_bg(query, W, H)
        if img:
            return img

        # 2순위: Pixabay
        img = self._fetch_pixabay_bg(query, W, H)
        if img:
            return img

        logger.warning("모든 이미지 소스 실패 → gradient 사용")
        return None

    def _extract_query(self, title: str, mode: str, content: dict) -> str:
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

    # ── 전체 생성 진입점 ──────────────────────────────────────────────────────
    def generate_all(
        self,
        title: str,
        mode: str,
        thumbnail_url: str = "",
        blog_url: str = "seedsup.tistory.com",
        timestamp: str = "",
        content: dict | None = None,
        image_prompt: str = "",
    ) -> dict[str, str]:
        if not timestamp:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M")
        content  = content or {}
        date_str = _extract_date(title)

        platform_list = ["facebook", "threads", "instagram", "instagram_portrait", "kakao"]
        paths: dict[str, str] = {}

        # FLUX는 1회 생성해 모든 플랫폼이 공유 (API 호출 최소화)
        shared_bg: Image.Image | None = None
        flux_attempted = False

        for platform in platform_list:
            try:
                logger.info(f"[{platform}] 썸네일 생성 중...")
                W, H = _get_platform_size(platform)

                # FLUX 배경은 첫 플랫폼에서 1회만 생성하고 이후 재사용
                if not flux_attempted:
                    flux_prompt = image_prompt or title
                    shared_bg = self._generate_flux_bg(flux_prompt, mode, W=1024, H=1024)
                    flux_attempted = True
                    if shared_bg:
                        logger.info("FLUX 배경 생성 성공 — 전 플랫폼 공유")
                    else:
                        logger.info("FLUX 실패 → Pexels/Pixabay로 대체")

                # 배경 확정
                if shared_bg:
                    bg_img = shared_bg
                else:
                    query  = self._extract_query(title, mode, content)
                    bg_img = self._fetch_pexels_bg(query, W, H)
                    if not bg_img:
                        bg_img = self._fetch_pixabay_bg(query, W, H)
                    # None이면 _build_thumbnail 내부에서 gradient 사용

                img = _build_thumbnail(bg_img, title, mode, platform, date_str, blog_url)

                filename  = f"thumb_{platform}_{mode}_{timestamp}.jpg"
                path      = os.path.join(self.output_dir, filename)
                img.save(path, "JPEG", quality=95, optimize=True)
                paths[platform] = path
                logger.info(f"  → 저장: {path} ({img.size})")

            except Exception as e:
                logger.error(f"[{platform}] 썸네일 생성 실패: {e}", exc_info=True)

        return paths
