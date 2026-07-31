"""
이미지 생성 모듈 (v3 — FLUX.1-schnell 기반)
우선순위:
  1순위: HuggingFace FLUX.1-schnell (AI 생성 — 콘텐츠 맞춤형, 무료티어)
  2순위: Pexels API (스톡사진)
  3순위: Pixabay API (스톡사진)
  4순위: gradient fallback

v2 → v3 변경 이유:
  - Pexels/Pixabay 스톡사진은 증시 콘텐츠와 맥락이 맞지 않는 일반 이미지가
    섞이는 문제가 있었습니다.
  - Gemini가 생성한 image_prompt를 FLUX.1-schnell에 그대로 투입하면
    콘텐츠와 연계된 이미지를 매일 새롭게 생성할 수 있습니다.
  - FLUX.1-schnell은 SDXL 대비 속도가 빠르고 무료티어에서 사용 가능합니다.
    (cold start 포함 보통 15~40초, GitHub Actions 10분 timeout 이내)
  - Unsplash Source는 2024년 완전 종료(503)되어 제거했습니다.

mode:
  morning : 마감 후 조용한 월스트리트 분위기 (차분·분석적)
  evening : 개장 전 활기찬 트레이딩 분위기 (긴장·역동적)
"""

import requests
import logging
import os
import time
import hashlib
from datetime import datetime
from PIL import Image, ImageDraw, ImageFont
from io import BytesIO

logger = logging.getLogger(__name__)

# ── HuggingFace FLUX.1-schnell ────────────────────────────────────────────────
HF_FLUX_URL = (
    "https://router.huggingface.co/hf-inference/models/"
    "black-forest-labs/FLUX.1-schnell"
)

# 모드별 프롬프트 접미사 (FLUX는 자연어 지시에 강함)
FLUX_SUFFIX = {
    "morning": (
        ", Wall Street financial district at dawn, calm after market close, "
        "professional financial photography, stock market analysis, "
        "blue and gold color palette, cinematic lighting, 8K, sharp focus, "
        "no text, no watermark"
    ),
    "evening": (
        ", pre-market trading floor at night, dynamic stock exchange screens, "
        "professional financial photography, urgent market news atmosphere, "
        "purple and amber color palette, dramatic lighting, 8K, sharp focus, "
        "no text, no watermark"
    ),
}

FLUX_NEGATIVE = (
    "blurry, low quality, text, watermark, logo, cartoon, "
    "anime, faces, nsfw, ugly, duplicate, deformed"
)

# ── Pexels / Pixabay 키워드 (fallback) ───────────────────────────────────────
STOCK_KEYWORDS = {
    "morning": {
        "상승": "stock market bull finance growth morning",
        "하락": "stock market bear finance crisis red morning",
        "혼조": "wall street trading floor economy dawn",
        "금리": "federal reserve interest rate banking economy",
        "기술주": "technology nasdaq silicon valley innovation",
        "에너지": "energy oil renewable petroleum economy",
        "인플레이션": "inflation economy money prices consumer",
        "고용": "employment jobs economy workforce business",
        "default": "stock market wall street finance morning economy",
    },
    "evening": {
        "상승": "stock market bull trading night growth",
        "하락": "stock market bear crisis red night finance",
        "혼조": "premarket trading floor economy night",
        "금리": "federal reserve interest rate banking night",
        "기술주": "technology nasdaq innovation digital night",
        "에너지": "energy oil market economy night",
        "인플레이션": "inflation economy money prices night",
        "고용": "employment jobs economy business night",
        "default": "stock market premarket trading finance night",
    },
}

OUTPUT_DIR = "images"


def _daily_index(length: int) -> int:
    """날짜 기반으로 매일 다른 인덱스 선택."""
    if length <= 0:
        return 0
    seed = int(datetime.now().strftime("%Y%m%d"))
    return seed % length


def _extract_stock_query(prompt: str, content: str, mode: str) -> str:
    kmap = STOCK_KEYWORDS.get(mode, STOCK_KEYWORDS["morning"])
    for topic, keywords in kmap.items():
        if topic == "default":
            continue
        if topic in content or topic in prompt:
            return keywords
    return kmap["default"]


def _add_watermark(file_path: str, source: str) -> None:
    """이미지 우측 하단에 출처 표시 (실패해도 무시)."""
    try:
        img = Image.open(file_path).convert("RGBA")
        overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
        draw = ImageDraw.Draw(overlay)
        font_size = max(14, img.width // 55)
        try:
            font = ImageFont.truetype(
                "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", font_size
            )
        except Exception:
            font = ImageFont.load_default()

        text = f"Image: {source}"
        bbox = draw.textbbox((0, 0), text, font=font)
        tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
        pad, margin = 8, 14
        x = img.width - tw - pad * 2 - margin
        y = img.height - th - pad * 2 - margin
        draw.rectangle([x, y, x + tw + pad * 2, y + th + pad * 2], fill=(0, 0, 0, 140))
        draw.text((x + pad - bbox[0], y + pad - bbox[1]), text, font=font, fill=(255, 255, 255, 220))

        result = Image.alpha_composite(img, overlay).convert("RGB")
        result.save(file_path, quality=90)
    except Exception as e:
        logger.warning(f"워터마크 삽입 실패 (무시): {e}")


class ImageGenerator:
    def __init__(self, hf_token: str = ""):
        self.hf_token = hf_token
        self.pexels_key = os.environ.get("PEXELS_API_KEY", "")
        self.pixabay_key = os.environ.get("PIXABAY_API_KEY", "")
        self.last_image_source = ""
        os.makedirs(OUTPUT_DIR, exist_ok=True)

    def get_attribution_text(self) -> str:
        credit_map = {
            "FLUX.1-schnell": "Image generated by FLUX.1-schnell (HuggingFace)",
            "Pexels": "사진 제공: Pexels",
            "Pixabay": "사진 제공: Pixabay",
        }
        return credit_map.get(self.last_image_source, "")

    def generate(
        self,
        prompt: str,
        filename: str,
        content: str = "",
        mode: str = "morning",
    ) -> str:
        # 1순위: FLUX.1-schnell
        result = self._generate_flux(prompt, filename, mode)
        if result:
            self.last_image_source = "FLUX.1-schnell"
            return result

        # 2순위: Pexels
        logger.info("FLUX 실패 → Pexels로 대체 시도...")
        result = self._fetch_pexels(prompt, content, filename, mode)
        if result:
            self.last_image_source = "Pexels"
            return result

        # 3순위: Pixabay
        logger.info("Pexels 실패 → Pixabay로 대체 시도...")
        result = self._fetch_pixabay(prompt, content, filename, mode)
        if result:
            self.last_image_source = "Pixabay"
            return result

        # 4순위: gradient fallback
        logger.warning("모든 이미지 소스 실패 → gradient fallback 사용")
        self.last_image_source = ""
        return self._gradient_fallback(filename, mode)

    # ── FLUX.1-schnell ────────────────────────────────────────────────────────
    def _generate_flux(
        self, prompt: str, filename: str, mode: str, max_retries: int = 3
    ) -> str | None:
        if not self.hf_token:
            logger.info("HF_API_TOKEN 미설정 — FLUX 건너뜀")
            return None

        suffix = FLUX_SUFFIX.get(mode, FLUX_SUFFIX["morning"])
        full_prompt = f"{prompt}{suffix}"

        # 날짜+모드 기반 시드로 매일 다른 이미지
        seed = int(hashlib.md5(
            f"{datetime.now().strftime('%Y%m%d')}{mode}".encode()
        ).hexdigest()[:8], 16) % (2 ** 32)

        payload = {
            "inputs": full_prompt,
            "parameters": {
                "num_inference_steps": 4,   # schnell 권장값
                "guidance_scale": 0.0,       # schnell은 0이 최적
                "width": 1024,
                "height": 576,
                "seed": seed,
            },
        }
        headers = {"Authorization": f"Bearer {self.hf_token}"}

        logger.info(f"FLUX.1-schnell 이미지 생성 중 (모드: {mode}, 시드: {seed})...")

        for attempt in range(1, max_retries + 1):
            try:
                resp = requests.post(
                    HF_FLUX_URL,
                    headers=headers,
                    json=payload,
                    timeout=60,
                )

                if resp.status_code == 200:
                    if len(resp.content) < 10_000:
                        logger.warning("FLUX 응답 크기 불충분")
                        return None
                    file_path = os.path.join(
                        OUTPUT_DIR, filename.replace(".png", ".jpg")
                    )
                    # FLUX는 PNG 바이너리 반환 → JPEG로 저장
                    img = Image.open(BytesIO(resp.content)).convert("RGB")
                    img.save(file_path, "JPEG", quality=92, optimize=True)
                    _add_watermark(file_path, "FLUX.1-schnell / HuggingFace")
                    logger.info(
                        f"FLUX 이미지 저장: {file_path} "
                        f"({len(resp.content) // 1024}KB)"
                    )
                    return file_path

                elif resp.status_code == 503:
                    # 모델 cold start — estimated_time만큼 대기 후 재시도
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
                    logger.warning("FLUX 요청 한도 초과 (429) — Pexels로 전환")
                    return None

                else:
                    logger.warning(
                        f"FLUX 실패 ({resp.status_code}): {resp.text[:150]}"
                    )
                    return None

            except requests.exceptions.Timeout:
                logger.warning(f"FLUX 타임아웃 (시도 {attempt}/{max_retries})")
                if attempt < max_retries:
                    time.sleep(5)
            except Exception as e:
                logger.warning(f"FLUX 오류: {e}")
                return None

        return None

    # ── Pexels ────────────────────────────────────────────────────────────────
    def _fetch_pexels(
        self, prompt: str, content: str, filename: str, mode: str
    ) -> str | None:
        if not self.pexels_key:
            logger.info("PEXELS_API_KEY 미설정 — Pexels 건너뜀")
            return None
        query = _extract_stock_query(prompt, content, mode)
        try:
            resp = requests.get(
                "https://api.pexels.com/v1/search",
                headers={"Authorization": self.pexels_key},
                params={"query": query, "per_page": 10, "orientation": "landscape"},
                timeout=15,
            )
            resp.raise_for_status()
            photos = resp.json().get("photos", [])
            if not photos:
                logger.warning(f"Pexels 결과 없음: {query}")
                return None
            img_url = photos[_daily_index(len(photos))]["src"]["large2x"]
            ir = requests.get(img_url, timeout=20)
            ir.raise_for_status()
            if len(ir.content) < 10_000:
                return None
            file_path = os.path.join(OUTPUT_DIR, filename.replace(".png", ".jpg"))
            with open(file_path, "wb") as f:
                f.write(ir.content)
            _add_watermark(file_path, "Pexels")
            logger.info(f"Pexels 이미지 저장: {file_path}")
            return file_path
        except Exception as e:
            logger.warning(f"Pexels 실패: {e}")
            return None

    # ── Pixabay ───────────────────────────────────────────────────────────────
    def _fetch_pixabay(
        self, prompt: str, content: str, filename: str, mode: str
    ) -> str | None:
        if not self.pixabay_key:
            logger.info("PIXABAY_API_KEY 미설정 — Pixabay 건너뜀")
            return None
        query = _extract_stock_query(prompt, content, mode)
        try:
            resp = requests.get(
                "https://pixabay.com/api/",
                params={
                    "key": self.pixabay_key,
                    "q": query,
                    "image_type": "photo",
                    "orientation": "horizontal",
                    "per_page": 10,
                    "safesearch": "true",
                    "category": "business",
                },
                timeout=15,
            )
            resp.raise_for_status()
            hits = resp.json().get("hits", [])
            if not hits:
                logger.warning(f"Pixabay 결과 없음: {query}")
                return None
            img_url = hits[_daily_index(len(hits))]["largeImageURL"]
            ir = requests.get(img_url, timeout=20)
            ir.raise_for_status()
            if len(ir.content) < 10_000:
                return None
            file_path = os.path.join(OUTPUT_DIR, filename.replace(".png", ".jpg"))
            with open(file_path, "wb") as f:
                f.write(ir.content)
            _add_watermark(file_path, "Pixabay")
            logger.info(f"Pixabay 이미지 저장: {file_path}")
            return file_path
        except Exception as e:
            logger.warning(f"Pixabay 실패: {e}")
            return None

    # ── gradient fallback ─────────────────────────────────────────────────────
    def _gradient_fallback(self, filename: str, mode: str) -> str:
        W, H = 1024, 576
        img = Image.new("RGB", (W, H))
        draw = ImageDraw.Draw(img)
        if mode == "morning":
            colors = [(6, 20, 60), (15, 50, 110), (6, 20, 60)]
        else:
            colors = [(20, 5, 55), (50, 15, 100), (20, 5, 55)]
        n = len(colors) - 1
        step = H // n
        for i in range(n):
            c0, c1 = colors[i], colors[i + 1]
            for y in range(i * step, (i + 1) * step):
                t = (y - i * step) / step
                r = int(c0[0] * (1 - t) + c1[0] * t)
                g = int(c0[1] * (1 - t) + c1[1] * t)
                b = int(c0[2] * (1 - t) + c1[2] * t)
                draw.line([(0, y), (W, y)], fill=(r, g, b))
        file_path = os.path.join(OUTPUT_DIR, filename.replace(".png", ".jpg"))
        img.save(file_path, "JPEG", quality=85)
        logger.info(f"gradient fallback 저장: {file_path}")
        return file_path
