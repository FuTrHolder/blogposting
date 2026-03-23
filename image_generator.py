"""
이미지 생성 모듈 (개선 버전)
1차: Stable Diffusion XL (HuggingFace) - 날짜 시드로 매일 다른 이미지
2차: Unsplash API (무료) - SD 실패 시 자동 대체
"""

import requests
import logging
import time
import os
import hashlib
from datetime import datetime

logger = logging.getLogger(__name__)

# HuggingFace Inference API (신규 URL)
HF_API_URL = (
    "https://router.huggingface.co/hf-inference/models/"
    "stabilityai/stable-diffusion-xl-base-1.0"
)

# 품질 향상 접미사
PROMPT_SUFFIX = (
    ", professional financial photography, stock market, "
    "high quality, 8k resolution, cinematic lighting, "
    "sharp focus, award winning"
)

NEGATIVE_PROMPT = (
    "blurry, low quality, text, watermark, logo, cartoon, "
    "anime, faces, nsfw, dark, ugly, duplicate"
)

# Unsplash 무료 API (키 없이 사용 가능한 source URL)
# 키워드 기반으로 매번 다른 이미지 반환
UNSPLASH_URL = "https://source.unsplash.com/1024x576/?{keywords}&sig={sig}"

# 주제별 Unsplash 키워드 매핑
TOPIC_KEYWORDS = {
    "상승": "stock-market,bull,finance,growth,success",
    "하락": "stock-market,bear,finance,crisis,red",
    "혼조": "stock-market,finance,wall-street,trading,economy",
    "금리": "federal-reserve,interest-rate,banking,economy,finance",
    "기술주": "technology,nasdaq,silicon-valley,innovation,digital",
    "에너지": "energy,oil,renewable,petroleum,economy",
    "인플레이션": "inflation,economy,money,prices,consumer",
    "고용": "employment,jobs,economy,workforce,business",
    "default": "stock-market,wall-street,finance,economy,trading",
}

OUTPUT_DIR = "images"


def _extract_keywords_from_prompt(image_prompt: str, content: str) -> str:
    """글 내용에서 Unsplash 키워드를 추출합니다."""
    for topic, keywords in TOPIC_KEYWORDS.items():
        if topic in content or topic in image_prompt:
            return keywords
    return TOPIC_KEYWORDS["default"]


class ImageGenerator:
    def __init__(self, hf_token: str):
        self.headers = {"Authorization": f"Bearer {hf_token}"}
        os.makedirs(OUTPUT_DIR, exist_ok=True)

    def generate(self, prompt: str, filename: str, content: str = "") -> str:
        """
        이미지를 생성합니다.
        1순위: Stable Diffusion (HuggingFace)
        2순위: Unsplash 무료 이미지
        """
        # 1순위: Stable Diffusion
        result = self._generate_sd(prompt, filename)
        if result:
            return result

        # 2순위: Unsplash
        logger.info("Unsplash 이미지로 대체 시도...")
        result = self._fetch_unsplash(filename, prompt, content)
        if result:
            return result

        # 최후 fallback
        logger.warning("모든 이미지 생성 실패. 기본 이미지 사용")
        return self._default_fallback(filename)

    # ── Stable Diffusion ──────────────────────────────────────────────────
    def _generate_sd(self, prompt: str, filename: str, max_retries: int = 2) -> str | None:
        """Stable Diffusion XL로 이미지를 생성합니다."""

        # 날짜 + 프롬프트 해시로 매일 다른 시드 생성
        today = datetime.now().strftime("%Y%m%d")
        seed = int(hashlib.md5(f"{today}{prompt}".encode()).hexdigest()[:8], 16)

        full_prompt = f"{prompt}{PROMPT_SUFFIX}"
        payload = {
            "inputs": full_prompt,
            "parameters": {
                "negative_prompt": NEGATIVE_PROMPT,
                "num_inference_steps": 30,
                "guidance_scale": 7.5,
                "width": 1024,
                "height": 576,
                "seed": seed,  # ← 날짜 기반 시드로 매일 다른 이미지
            },
        }

        logger.info(f"SD 이미지 생성 중 (시드: {seed})...")

        for attempt in range(1, max_retries + 1):
            try:
                resp = requests.post(
                    HF_API_URL,
                    headers=self.headers,
                    json=payload,
                    timeout=90,
                )

                if resp.status_code == 200:
                    # 이미지 유효성 확인 (최소 10KB)
                    if len(resp.content) < 10000:
                        logger.warning("SD 응답이 너무 작음 (캐시된 이미지 가능성)")
                        return None

                    file_path = os.path.join(OUTPUT_DIR, filename)
                    with open(file_path, "wb") as f:
                        f.write(resp.content)
                    logger.info(f"SD 이미지 저장 완료: {file_path} ({len(resp.content)//1024}KB)")
                    return file_path

                elif resp.status_code == 503:
                    wait = min(resp.json().get("estimated_time", 20), 40)
                    logger.warning(f"모델 로딩 중... {wait}초 대기")
                    time.sleep(wait)

                elif resp.status_code == 429:
                    logger.warning("요청 한도 초과. SD 건너뜀")
                    return None

                else:
                    logger.warning(f"SD 실패 ({resp.status_code}): {resp.text[:100]}")
                    return None

            except requests.exceptions.Timeout:
                logger.warning(f"SD 타임아웃 (시도 {attempt})")
                time.sleep(5)
            except Exception as e:
                logger.warning(f"SD 오류: {e}")
                return None

        return None

    # ── Unsplash ──────────────────────────────────────────────────────────
    def _fetch_unsplash(
        self, filename: str, prompt: str, content: str
    ) -> str | None:
        """
        Unsplash source API로 무료 이미지를 가져옵니다.
        sig 파라미터로 날짜마다 다른 이미지가 반환됩니다.
        """
        keywords = _extract_keywords_from_prompt(prompt, content)

        # 날짜 기반 sig로 매일 다른 이미지
        today_sig = datetime.now().strftime("%Y%m%d")
        url = f"https://source.unsplash.com/1024x576/?{keywords}&sig={today_sig}"

        logger.info(f"Unsplash 이미지 요청: {url}")

        try:
            resp = requests.get(
                url,
                timeout=20,
                headers={"User-Agent": "Mozilla/5.0"},
                allow_redirects=True,
            )

            if resp.status_code == 200 and len(resp.content) > 10000:
                # jpg로 저장 (Unsplash는 JPEG 반환)
                jpg_filename = filename.replace(".png", ".jpg")
                file_path = os.path.join(OUTPUT_DIR, jpg_filename)
                with open(file_path, "wb") as f:
                    f.write(resp.content)
                logger.info(
                    f"Unsplash 이미지 저장 완료: {file_path} "
                    f"({len(resp.content)//1024}KB)"
                )
                return file_path
            else:
                logger.warning(f"Unsplash 응답 불량: {resp.status_code}")
                return None

        except Exception as e:
            logger.warning(f"Unsplash 오류: {e}")
            return None

    # ── 기본 fallback ─────────────────────────────────────────────────────
    def _default_fallback(self, filename: str) -> str:
        """모든 방법 실패 시 고정 Unsplash 이미지를 사용합니다."""
        fallback_urls = [
            "https://images.unsplash.com/photo-1611974789855-9c2a0a7236a3?w=1024&q=80",
            "https://images.unsplash.com/photo-1590283603385-17ffb3a7f29f?w=1024&q=80",
            "https://images.unsplash.com/photo-1559526324-4b87b5e36e44?w=1024&q=80",
        ]
        # 날짜로 돌아가며 선택
        idx = datetime.now().day % len(fallback_urls)
        fallback_path = os.path.join(OUTPUT_DIR, filename.replace(".png", ".jpg"))

        try:
            resp = requests.get(fallback_urls[idx], timeout=15)
            if resp.status_code == 200:
                with open(fallback_path, "wb") as f:
                    f.write(resp.content)
                return fallback_path
        except Exception:
            pass

        return fallback_path
