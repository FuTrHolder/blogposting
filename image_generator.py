"""
이미지 생성 모듈
1차: Stable Diffusion XL (HuggingFace) - 날짜 시드로 매일 다른 이미지
2차: Unsplash API (무료) - SD 실패 시 자동 대체

mode:
  morning : 마감 후 조용한 월스트리트 분위기 (차분·분석적)
  evening : 개장 전 활기찬 트레이딩 분위기 (긴장·역동적)
"""

import requests
import logging
import time
import os
import hashlib
from datetime import datetime

logger = logging.getLogger(__name__)

HF_API_URL = (
    "https://router.huggingface.co/hf-inference/models/"
    "stabilityai/stable-diffusion-xl-base-1.0"
)

# 모드별 SD 프롬프트 접미사
PROMPT_SUFFIX = {
    "morning": (
        ", after-hours wall street, calm financial district at dawn, "
        "professional financial photography, stock market, "
        "high quality, 8k resolution, cinematic lighting, sharp focus"
    ),
    "evening": (
        ", pre-market trading floor, dynamic stock exchange, "
        "professional financial photography, urgent market news, "
        "high quality, 8k resolution, dramatic lighting, sharp focus"
    ),
}

NEGATIVE_PROMPT = (
    "blurry, low quality, text, watermark, logo, cartoon, "
    "anime, faces, nsfw, dark, ugly, duplicate"
)

# 모드별 Unsplash 키워드 세트
TOPIC_KEYWORDS = {
    # 오전(마감 리뷰) 키워드
    "morning": {
        "상승": "stock-market,bull,finance,growth,success,morning",
        "하락": "stock-market,bear,finance,crisis,red,morning",
        "혼조": "stock-market,finance,wall-street,trading,economy,dawn",
        "금리": "federal-reserve,interest-rate,banking,economy,finance",
        "기술주": "technology,nasdaq,silicon-valley,innovation,digital",
        "에너지": "energy,oil,renewable,petroleum,economy",
        "인플레이션": "inflation,economy,money,prices,consumer",
        "고용": "employment,jobs,economy,workforce,business",
        "default": "stock-market,wall-street,finance,morning,economy",
    },
    # 저녁(프리마켓 & 이슈) 키워드
    "evening": {
        "상승": "stock-market,bull,premarket,trading,growth,night",
        "하락": "stock-market,bear,premarket,crisis,red,night",
        "혼조": "stock-market,premarket,trading-floor,economy,night",
        "금리": "federal-reserve,interest-rate,banking,economy,night",
        "기술주": "technology,nasdaq,innovation,digital,night",
        "에너지": "energy,oil,economy,market,night",
        "인플레이션": "inflation,economy,money,prices,night",
        "고용": "employment,jobs,economy,business,night",
        "default": "stock-market,premarket,trading,finance,night",
    },
}

# 모드별 fallback Unsplash 이미지 URL
FALLBACK_URLS = {
    "morning": [
        "https://images.unsplash.com/photo-1611974789855-9c2a0a7236a3?w=1024&q=80",
        "https://images.unsplash.com/photo-1590283603385-17ffb3a7f29f?w=1024&q=80",
        "https://images.unsplash.com/photo-1559526324-4b87b5e36e44?w=1024&q=80",
    ],
    "evening": [
        "https://images.unsplash.com/photo-1611974789855-9c2a0a7236a3?w=1024&q=80",
        "https://images.unsplash.com/photo-1526628953301-3e589a6a8b74?w=1024&q=80",
        "https://images.unsplash.com/photo-1642790551116-18a150d1f65d?w=1024&q=80",
    ],
}

OUTPUT_DIR = "images"


def _extract_keywords(image_prompt: str, content: str, mode: str) -> str:
    topic_map = TOPIC_KEYWORDS.get(mode, TOPIC_KEYWORDS["morning"])
    for topic, keywords in topic_map.items():
        if topic == "default":
            continue
        if topic in content or topic in image_prompt:
            return keywords
    return topic_map["default"]


class ImageGenerator:
    def __init__(self, hf_token: str):
        self.headers = {"Authorization": f"Bearer {hf_token}"}
        os.makedirs(OUTPUT_DIR, exist_ok=True)

    def generate(
        self,
        prompt: str,
        filename: str,
        content: str = "",
        mode: str = "morning",
    ) -> str:
        result = self._generate_sd(prompt, filename, mode)
        if result:
            return result

        logger.info("Unsplash 이미지로 대체 시도...")
        result = self._fetch_unsplash(filename, prompt, content, mode)
        if result:
            return result

        logger.warning("모든 이미지 생성 실패. 기본 이미지 사용")
        return self._default_fallback(filename, mode)

    # ── Stable Diffusion ──────────────────────────────────────────────────
    def _generate_sd(
        self, prompt: str, filename: str, mode: str, max_retries: int = 2
    ) -> str | None:
        today = datetime.now().strftime("%Y%m%d")
        seed = int(hashlib.md5(f"{today}{mode}{prompt}".encode()).hexdigest()[:8], 16)

        suffix = PROMPT_SUFFIX.get(mode, PROMPT_SUFFIX["morning"])
        full_prompt = f"{prompt}{suffix}"
        payload = {
            "inputs": full_prompt,
            "parameters": {
                "negative_prompt": NEGATIVE_PROMPT,
                "num_inference_steps": 30,
                "guidance_scale": 7.5,
                "width": 1024,
                "height": 576,
                "seed": seed,
            },
        }

        logger.info(f"SD 이미지 생성 중 (모드: {mode}, 시드: {seed})...")

        for attempt in range(1, max_retries + 1):
            try:
                resp = requests.post(
                    HF_API_URL,
                    headers=self.headers,
                    json=payload,
                    timeout=90,
                )

                if resp.status_code == 200:
                    if len(resp.content) < 10000:
                        logger.warning("SD 응답이 너무 작음")
                        return None
                    file_path = os.path.join(OUTPUT_DIR, filename)
                    with open(file_path, "wb") as f:
                        f.write(resp.content)
                    logger.info(f"SD 이미지 저장: {file_path} ({len(resp.content)//1024}KB)")
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
        self, filename: str, prompt: str, content: str, mode: str
    ) -> str | None:
        keywords = _extract_keywords(prompt, content, mode)
        today_sig = datetime.now().strftime(f"%Y%m%d{mode}")
        url = f"https://source.unsplash.com/1024x576/?{keywords}&sig={today_sig}"

        logger.info(f"Unsplash 요청 ({mode}): {url}")

        try:
            resp = requests.get(
                url,
                timeout=20,
                headers={"User-Agent": "Mozilla/5.0"},
                allow_redirects=True,
            )
            if resp.status_code == 200 and len(resp.content) > 10000:
                jpg_filename = filename.replace(".png", ".jpg")
                file_path = os.path.join(OUTPUT_DIR, jpg_filename)
                with open(file_path, "wb") as f:
                    f.write(resp.content)
                logger.info(f"Unsplash 저장: {file_path} ({len(resp.content)//1024}KB)")
                return file_path
            else:
                logger.warning(f"Unsplash 응답 불량: {resp.status_code}")
                return None
        except Exception as e:
            logger.warning(f"Unsplash 오류: {e}")
            return None

    # ── fallback ──────────────────────────────────────────────────────────
    def _default_fallback(self, filename: str, mode: str) -> str:
        urls = FALLBACK_URLS.get(mode, FALLBACK_URLS["morning"])
        idx = datetime.now().day % len(urls)
        fallback_path = os.path.join(OUTPUT_DIR, filename.replace(".png", ".jpg"))
        try:
            resp = requests.get(urls[idx], timeout=15)
            if resp.status_code == 200:
                with open(fallback_path, "wb") as f:
                    f.write(resp.content)
                return fallback_path
        except Exception:
            pass
        return fallback_path
