"""
이미지 생성 모듈
Hugging Face Inference API (무료)를 사용해 Stable Diffusion으로 썸네일을 생성합니다.
모델: stabilityai/stable-diffusion-xl-base-1.0
"""

import requests
import logging
import time
import os

logger = logging.getLogger(__name__)

# Hugging Face Inference API 엔드포인트
HF_API_URL = "https://router.huggingface.co/hf-inference/models/stabilityai/stable-diffusion-xl-base-1.0"

# 이미지 프롬프트 기본 접미사 (품질 향상용)
PROMPT_SUFFIX = (
    ", professional photography, high quality, 8k, "
    "financial theme, stock market, cinematic lighting, sharp focus"
)

# 네거티브 프롬프트 (생성하지 않을 요소)
NEGATIVE_PROMPT = (
    "blurry, low quality, text, watermark, logo, cartoon, anime, "
    "people faces, nsfw, dark, ugly"
)

# 이미지 저장 경로
OUTPUT_DIR = "images"


class ImageGenerator:
    def __init__(self, hf_token: str):
        self.headers = {"Authorization": f"Bearer {hf_token}"}
        os.makedirs(OUTPUT_DIR, exist_ok=True)

    def generate(self, prompt: str, filename: str, max_retries: int = 3) -> str:
        """
        Stable Diffusion으로 이미지를 생성하고 파일 경로를 반환합니다.
        모델 로딩 중(503)이면 자동 재시도합니다.
        """
        full_prompt = prompt + PROMPT_SUFFIX
        payload = {
            "inputs": full_prompt,
            "parameters": {
                "negative_prompt": NEGATIVE_PROMPT,
                "num_inference_steps": 30,
                "guidance_scale": 7.5,
                "width": 1024,
                "height": 576,  # 16:9 비율 썸네일
            },
        }

        for attempt in range(1, max_retries + 1):
            logger.info(f"이미지 생성 시도 {attempt}/{max_retries}...")
            try:
                resp = requests.post(
                    HF_API_URL,
                    headers=self.headers,
                    json=payload,
                    timeout=120,
                )

                if resp.status_code == 200:
                    file_path = os.path.join(OUTPUT_DIR, filename)
                    with open(file_path, "wb") as f:
                        f.write(resp.content)
                    logger.info(f"이미지 저장 완료: {file_path}")
                    return file_path

                elif resp.status_code == 503:
                    # 모델 로딩 중 – 대기 후 재시도
                    wait_time = resp.json().get("estimated_time", 20)
                    logger.warning(f"모델 로딩 중... {wait_time}초 대기 후 재시도")
                    time.sleep(min(wait_time, 60))

                elif resp.status_code == 429:
                    logger.warning("요청 한도 초과. 30초 대기 후 재시도...")
                    time.sleep(30)

                else:
                    logger.error(f"이미지 생성 실패: {resp.status_code} {resp.text}")
                    break

            except requests.exceptions.Timeout:
                logger.warning(f"타임아웃 발생 (시도 {attempt})")
                time.sleep(10)
            except Exception as e:
                logger.error(f"이미지 생성 중 오류: {e}")
                break

        # 실패 시 기본 플레이스홀더 이미지 경로 반환
        logger.warning("이미지 생성 실패 – 플레이스홀더 사용")
        return self._get_fallback_image()

    def _get_fallback_image(self) -> str:
        """
        이미지 생성 실패 시 사용할 기본 이미지를 다운로드합니다.
        Unsplash 무료 이미지 (stock market 테마)
        """
        fallback_url = (
            "https://images.unsplash.com/photo-1611974789855-9c2a0a7236a3"
            "?w=1024&q=80"
        )
        fallback_path = os.path.join(OUTPUT_DIR, "fallback.jpg")
        try:
            resp = requests.get(fallback_url, timeout=15)
            if resp.status_code == 200:
                with open(fallback_path, "wb") as f:
                    f.write(resp.content)
                return fallback_path
        except Exception:
            pass
        return fallback_path
