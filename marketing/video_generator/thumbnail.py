"""
SNS 썸네일 생성기 v7 — Cloudflare Workers AI 기반
=====================================
우선순위 (v6 → v7 변경):
  1순위: Cloudflare Workers AI · FLUX.1-schnell (AI 생성 — 완전 무료, 신용카드 불필요)
  2순위: HuggingFace Inference Providers · FLUX.1-schnell (유료 크레딧 남은 계정 대비 보존)
  3순위: Pexels API (스톡사진 + 텍스트 오버레이)
  4순위: Pixabay API (스톡사진 + 텍스트 오버레이)
  5순위: gradient fallback + 텍스트 오버레이

v6 → v7 변경 이유 (본문용 image_generator.py와 동일한 원인/해결):
  - Hugging Face Inference Providers(fal-ai/together/replicate)는 무료 계정
    기준 월 $0.10 상당의 크레딧만 제공합니다. 이 모듈은 발행 1회당 채널
    스타일 그룹 3개(facebook/kakao, instagram/instagram_portrait, threads)를
    생성하므로, main.py의 본문 썸네일 1회분까지 합치면 하루 여러 번 FLUX를
    호출하게 되어 월초 며칠 안에 그 $0.10 크레딧이 소진됩니다. 이후 요청은
    전부 결제 필요(402)로 실패하고, hf-inference provider는 2026-07경
    FLUX.1-schnell 무료 서빙 자체를 중단(410)했습니다 — 즉 무료 HF 토큰만
    으로는 크레딧 소진 이후 계속 Pexels/Pixabay로만 폴백되는 것이 정상적인
    현상이었습니다.
  - Cloudflare Workers AI는 하루 10,000 뉴런이 매일 자정(UTC) 초기화되어
    제공되고, FLUX.1-schnell 1장에 약 40~60 뉴런 수준이라 신용카드 등록
    없이 하루 수백 장을 생성할 수 있습니다. 이 프로젝트는 이미 Cloudflare
    Pages/D1을 대시보드로 쓰고 있어 추가 가입도 필요 없습니다.
  - CLOUDFLARE_ACCOUNT_ID / CLOUDFLARE_API_TOKEN이 설정되어 있지 않으면
    조용히 건너뛰고 기존 HF → Pexels → Pixabay → gradient 경로 그대로
    동작하므로, 시크릿을 아직 추가하지 않아도 기존 동작을 깨뜨리지 않습니다.
  - image_prompt(Gemini 생성)를 그대로 사용 → 콘텐츠와 연계된 이미지
    모든 플랫폼이 동일한 AI 생성 배경을 공유하되,
    텍스트 오버레이(제목/날짜/URL/배지)는 Pillow로 각 플랫폼 규격에 맞게 적용
"""

import base64
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

# ── Cloudflare Workers AI (신규 1순위 — 완전 무료) ──────────────────────────
CF_FLUX_MODEL = "@cf/black-forest-labs/flux-1-schnell"
CF_API_BASE = "https://api.cloudflare.com/client/v4/accounts"

# ── HuggingFace FLUX.1-schnell (Inference Providers 경유, 2순위) ────────────
# raw REST(router.huggingface.co/hf-inference/...)는 hf-inference provider에서
# FLUX.1-schnell 서빙이 중단(410)된 이후 다른 provider에서도 모델-provider
# 매핑을 못 찾아 400을 반환합니다. huggingface_hub 라이브러리를 쓰면 이
# 매핑을 자동으로 처리해주므로 이 방식으로 교체합니다 (image_generator.py와 동일).
# 다만 무료 계정은 월 $0.10 크레딧만 제공되어 금방 소진되므로, Cloudflare
# Workers AI가 성공하면 이 경로는 시도하지 않습니다 (크레딧 절약).
HF_FLUX_PROVIDERS = ["fal-ai", "together", "replicate", "hf-inference"]
HF_FLUX_MODEL = "black-forest-labs/FLUX.1-schnell"

# 모드별(시간대) 기본 프롬프트 접미사
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

# ── 채널별 비주얼 톤 접미사 ───────────────────────────────────────────────────
# 같은 콘텐츠라도 플랫폼 성격에 맞게 배경 분위기를 다르게 생성합니다.
# FLUX_SUFFIX(시간대)와 조합되어 최종 프롬프트를 구성합니다.
PLATFORM_STYLE_SUFFIX = {
    # Facebook: 범용 정보 전달 — 신뢰감 있는 정통 금융 사진 톤 (기본값과 동일)
    "facebook": "",
    # Kakao: Facebook과 동일한 범용 톤 재사용
    "kakao": "",
    # Instagram: 비주얼 중심 트렌디 플랫폼 — 채도 높고 세련된 편집샷 느낌
    "instagram": (
        ", vibrant saturated colors, sleek modern editorial photography style, "
        "high contrast, trendy aesthetic, glossy finish"
    ),
    "instagram_portrait": (
        ", vibrant saturated colors, sleek modern editorial photography style, "
        "high contrast, trendy aesthetic, glossy finish"
    ),
    # Threads: 텍스트 중심 캐주얼 플랫폼 — 미니멀하고 차분한 스냅샷 톤
    "threads": (
        ", minimalist composition, soft natural lighting, candid snapshot feel, "
        "understated and calm mood, muted tones"
    ),
}

# ── 시스템 폰트 ───────────────────────────────────────────────────────────────
_FONT_BLACK   = "/usr/share/fonts/opentype/noto/NotoSansCJK-Black.ttc"
_FONT_BOLD    = "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc"
_FONT_REGULAR = "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"

# ── 텍스트 정제 (HTML 엔티티 + 이모지) ──────────────────────────────────────
import html as _html_module

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

def _clean_text(text: str) -> str:
    """
    Pillow 렌더링 전 텍스트 정제.
    1) HTML 엔티티 변환: &middot; → · / &amp; → & / &ndash; → – 등
       Gemini가 JSON 안에 HTML 엔티티를 그대로 출력하는 경우를 방지합니다.
    2) 잔여 numeric 엔티티 처리: &#183; / &#xB7; 형태
    3) 이모지 제거: Pillow CJK 폰트가 렌더링하지 못해 □ 또는 오류 발생
    """
    text = _html_module.unescape(text)
    text = re.sub(r"&#(\d+);", lambda m: chr(int(m.group(1))), text)
    text = re.sub(r"&#x([0-9a-fA-F]+);", lambda m: chr(int(m.group(1), 16)), text)
    text = _EMOJI_RE.sub("", text)
    return text.strip()

# 하위 호환 별칭 (generator.py 등에서 _strip_emoji 직접 호출 시 대비)
_strip_emoji = _clean_text


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
        self.cf_account_id = os.environ.get("CLOUDFLARE_ACCOUNT_ID", "").strip()
        self.cf_api_token  = os.environ.get("CLOUDFLARE_API_TOKEN", "").strip()
        self.pexels_key  = os.environ.get("PEXELS_API_KEY", "")
        self.pixabay_key = os.environ.get("PIXABAY_API_KEY", "")
        os.makedirs(output_dir, exist_ok=True)

    # ── Cloudflare Workers AI 배경 이미지 생성 (신규 1순위, 완전 무료) ────────
    def _generate_cf_bg(
        self, prompt: str, mode: str, W: int, H: int, platform: str = "",
    ) -> Image.Image | None:
        if not self.cf_account_id or not self.cf_api_token:
            logger.info(
                "CLOUDFLARE_ACCOUNT_ID/CLOUDFLARE_API_TOKEN 미설정 — "
                "Cloudflare Workers AI 건너뜀"
            )
            return None

        mode_suffix     = FLUX_SUFFIX.get(mode, FLUX_SUFFIX["morning"])
        platform_suffix = PLATFORM_STYLE_SUFFIX.get(platform, "")
        full_prompt = f"{prompt}{mode_suffix}{platform_suffix}"

        seed = int(hashlib.md5(
            f"{datetime.now().strftime('%Y%m%d')}{mode}{platform}".encode()
        ).hexdigest()[:8], 16) % (2 ** 32)

        # 주의: flux-1-schnell의 공식 입력 스키마는 prompt(필수)와
        # steps(기본 4, 최대 8)만 받습니다 — width/height는 지원하지 않고
        # 파라미터 이름도 "num_steps"가 아니라 "steps"입니다. 문서:
        # https://developers.cloudflare.com/workers-ai/models/flux-1-schnell/schema-input.json
        # W/H 파라미터는 받은 이미지를 이후 _build_thumbnail()의 _crop_fit()
        # 단계에서 각 플랫폼 규격으로 다시 크롭할 때 쓰이므로 여기서
        # API에 그대로 전달하지 않아도 문제 없습니다.
        url = f"{CF_API_BASE}/{self.cf_account_id}/ai/run/{CF_FLUX_MODEL}"
        payload = {
            "prompt": full_prompt,
            "steps": 8,
            "seed": seed,
        }

        logger.info(
            f"Cloudflare Workers AI SNS 배경 생성 중 (모드: {mode}, "
            f"채널: {platform or '공용'}, 시드: {seed})..."
        )
        try:
            resp = requests.post(
                url,
                headers={
                    "Authorization": f"Bearer {self.cf_api_token}",
                    "Accept": "application/json",
                },
                json=payload,
                timeout=60,
            )
            if resp.status_code != 200:
                logger.warning(
                    f"[{platform or '공용'}] Cloudflare Workers AI 실패 "
                    f"({resp.status_code}): {resp.text[:200]}"
                )
                return None

            # 응답이 JSON(+base64)이 아니라 raw 이미지 바이너리로 올 가능성도
            # 방어적으로 함께 처리합니다.
            content_type = resp.headers.get("Content-Type", "")
            if content_type.startswith("image/"):
                image = Image.open(BytesIO(resp.content)).convert("RGB")
            else:
                data = resp.json()
                if not data.get("success", True):
                    logger.warning(
                        f"[{platform or '공용'}] Cloudflare Workers AI 실패: {data.get('errors')}"
                    )
                    return None
                b64_img = (data.get("result") or {}).get("image", "")
                if not b64_img:
                    logger.warning(f"[{platform or '공용'}] Cloudflare 응답에 image 데이터 없음")
                    return None
                image = Image.open(BytesIO(base64.b64decode(b64_img))).convert("RGB")

            # 이 함수가 받은 W, H는 최종 크롭 목표로만 사용 — flux-1-schnell은
            # 요청 해상도를 받지 않으므로, 반환된 이미지를 바로 여기서
            # 표준 정사각형(1024x1024)에 맞게 크롭해 이후 그룹 캐시/재사용
            # 로직(group_bg)이 기존과 동일하게 동작하도록 합니다.
            image = _crop_fit(image, W, H)

            logger.info(f"[{platform or '공용'}] Cloudflare Workers AI 배경 생성 성공: {image.size}")
            return image

        except Exception as e:
            logger.warning(f"[{platform or '공용'}] Cloudflare Workers AI 오류: {e}")
            return None

    # ── FLUX.1-schnell 배경 이미지 생성 (huggingface_hub 경유, 2순위) ──────────
    def _generate_flux_bg(
        self, prompt: str, mode: str, W: int, H: int,
        platform: str = "", max_retries: int = 2,
    ) -> Image.Image | None:
        if not self.hf_token:
            logger.info("HF_API_TOKEN 미설정 — FLUX 건너뜀")
            return None

        try:
            from huggingface_hub import InferenceClient
            from huggingface_hub.errors import HfHubHTTPError
        except ImportError:
            logger.error("huggingface_hub 미설치 — requirements.txt 확인 필요")
            return None

        mode_suffix     = FLUX_SUFFIX.get(mode, FLUX_SUFFIX["morning"])
        platform_suffix = PLATFORM_STYLE_SUFFIX.get(platform, "")
        full_prompt = f"{prompt}{mode_suffix}{platform_suffix}"

        # 날짜+모드+플랫폼 기반 시드 — 플랫폼마다 살짝 다른 구도의 이미지가
        # 나오도록 시드를 분리 (동일 시드면 프롬프트만 달라도 구도가 유사해짐)
        seed = int(hashlib.md5(
            f"{datetime.now().strftime('%Y%m%d')}{mode}{platform}".encode()
        ).hexdigest()[:8], 16) % (2 ** 32)

        logger.info(
            f"FLUX SNS 배경 생성 중 (모드: {mode}, 채널: {platform or '공용'}, "
            f"시드: {seed})..."
        )

        for provider in HF_FLUX_PROVIDERS:
            client = InferenceClient(provider=provider, api_key=self.hf_token, timeout=60)
            for attempt in range(1, max_retries + 1):
                try:
                    image = client.text_to_image(
                        full_prompt,
                        model=HF_FLUX_MODEL,
                        guidance_scale=0.0,
                        num_inference_steps=4,
                        width=1024,
                        height=1024,  # 정사각형으로 생성 후 각 플랫폼에 맞게 크롭
                        seed=seed,
                    )
                    logger.info(f"[{provider}] FLUX SNS 배경 생성 성공: {image.size}")
                    return image.convert("RGB")

                except HfHubHTTPError as e:
                    status = getattr(e.response, "status_code", None) if hasattr(e, "response") else None
                    msg = str(e)[:150]

                    if status == 402:
                        logger.warning(
                            f"[{provider}] 결제 필요(402) — 무료 크레딧 소진 가능성. "
                            f"Cloudflare Workers AI 설정을 권장합니다."
                        )
                        break

                    if status == 503 or "loading" in msg.lower():
                        wait = 20
                        logger.warning(
                            f"[{provider}] FLUX 모델 로딩 중 (503) — {wait}초 대기 "
                            f"(시도 {attempt}/{max_retries})..."
                        )
                        if attempt < max_retries:
                            time.sleep(wait)
                            continue
                        break

                    if status == 429:
                        logger.warning(f"[{provider}] FLUX 요청 한도 초과 (429)")
                        break

                    if status in (400, 404, 410):
                        logger.warning(f"[{provider}] FLUX 모델 미지원 ({status})")
                        break

                    logger.warning(f"[{provider}] FLUX 실패 ({status}): {msg}")
                    break

                except Exception as e:
                    logger.warning(f"[{provider}] FLUX 오류 (시도 {attempt}/{max_retries}): {e}")
                    if attempt < max_retries:
                        time.sleep(5)
                    else:
                        break

        logger.warning("모든 FLUX provider 실패 — Pexels/Pixabay로 대체")
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
        platform: str = "",
    ) -> Image.Image | None:
        # 0순위: Cloudflare Workers AI (완전 무료)
        flux_prompt = image_prompt or title
        img = self._generate_cf_bg(flux_prompt, mode, W, H, platform=platform)
        if img:
            return img

        # 1순위: FLUX.1-schnell via HuggingFace (유료 크레딧 남은 계정 대비)
        img = self._generate_flux_bg(flux_prompt, mode, W, H, platform=platform)
        if img:
            return img

        logger.info("AI 이미지 생성 실패 → Pexels/Pixabay로 대체...")

        # 2순위: Pexels
        query = self._extract_query(title, mode, content)
        img = self._fetch_pexels_bg(query, W, H)
        if img:
            return img

        # 3순위: Pixabay
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
        # 진입점에서 HTML 엔티티 + 이모지 일괄 정제
        # (Gemini가 &middot; 같은 HTML 엔티티를 JSON 안에 넣는 경우 방지)
        title      = _clean_text(title)
        image_prompt = _clean_text(image_prompt) if image_prompt else ""
        date_str = _extract_date(title)

        platform_list = ["facebook", "threads", "instagram", "instagram_portrait", "kakao"]
        paths: dict[str, str] = {}

        # ── 채널별 배경을 "스타일 그룹" 단위로 생성 ──────────────────────────
        # 플랫폼마다 매번 새로 생성하면 API 호출이 5배로 늘어나므로, 비주얼
        # 톤이 같은 채널끼리는 배경을 공유하되 톤이 다른 채널은 별도 생성합니다.
        #   그룹 A "범용/정보 전달": facebook, kakao (기본 톤)
        #   그룹 B "비주얼/트렌디": instagram, instagram_portrait (채도 높은 편집샷 톤)
        #   그룹 C "미니멀/캐주얼": threads (차분한 스냅샷 톤)
        style_groups: dict[str, list[str]] = {
            "facebook": ["facebook", "kakao"],
            "instagram": ["instagram", "instagram_portrait"],
            "threads": ["threads"],
        }
        group_bg: dict[str, Image.Image | None] = {}

        for group_key in style_groups:
            flux_prompt = image_prompt or title
            # 1순위: Cloudflare Workers AI (완전 무료)
            bg = self._generate_cf_bg(flux_prompt, mode, W=1024, H=1024, platform=group_key)
            # 2순위: HuggingFace FLUX (유료 크레딧 남은 계정 대비)
            if not bg:
                bg = self._generate_flux_bg(flux_prompt, mode, W=1024, H=1024, platform=group_key)
            if not bg:
                query = self._extract_query(title, mode, content)
                bg = self._fetch_pexels_bg(query, 1024, 1024)
                if not bg:
                    bg = self._fetch_pixabay_bg(query, 1024, 1024)
            group_bg[group_key] = bg
            logger.info(
                f"[{group_key} 그룹] 배경 확보: "
                f"{'AI/스톡 성공' if bg else '실패 → gradient 예정'}"
            )

        def _group_for(platform: str) -> str:
            for gkey, members in style_groups.items():
                if platform in members:
                    return gkey
            return "facebook"

        for platform in platform_list:
            try:
                logger.info(f"[{platform}] 썸네일 생성 중...")
                W, H = _get_platform_size(platform)

                bg_img = group_bg.get(_group_for(platform))
                img = _build_thumbnail(bg_img, title, mode, platform, date_str, blog_url)

                filename  = f"thumb_{platform}_{mode}_{timestamp}.jpg"
                path      = os.path.join(self.output_dir, filename)
                img.save(path, "JPEG", quality=95, optimize=True)
                paths[platform] = path
                logger.info(f"  → 저장: {path} ({img.size})")

            except Exception as e:
                logger.error(f"[{platform}] 썸네일 생성 실패: {e}", exc_info=True)

        return paths