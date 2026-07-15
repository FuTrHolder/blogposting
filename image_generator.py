"""
이미지 생성 모듈 (v2 — Pexels/Pixabay 기반)
1차: Pexels API (무료) - 키워드 기반 스톡 사진, 날짜 시드로 매일 다른 이미지
2차: Pixabay API (무료) - Pexels 실패 시 자동 대체
3차: 기본 이미지 - 위 두 곳 모두 실패 시 최종 폴백

변경 이유 (v1 → v2):
- Stable Diffusion XL(HuggingFace hf-inference)가 provider에서 모델 지원을 완전히
  중단함 (410 Gone: "The requested model is deprecated")
- source.unsplash.com(Unsplash Source)은 2024년에 서비스가 완전히 종료되어
  이후 모든 요청이 503으로 실패함
두 기존 경로가 이미 죽어있어 매번 기본 이미지로만 폴백되던 문제를 해결합니다.
마케팅 워크플로우의 SNS 썸네일 생성기(video_generator/thumbnail.py)와 동일한
Pexels → Pixabay 우선순위를 그대로 사용합니다.

mode:
  morning : 마감 후 조용한 월스트리트 분위기 (차분·분석적)
  evening : 개장 전 활기찬 트레이딩 분위기 (긴장·역동적)
"""

import requests
import logging
import os
from datetime import datetime

logger = logging.getLogger(__name__)

# 모드별 검색 키워드 (Pexels/Pixabay 공용 — 자연어 구문으로 검색)
TOPIC_KEYWORDS = {
    "morning": {
        "상승": "stock market bull finance morning growth",
        "하락": "stock market bear finance crisis red",
        "혼조": "wall street trading floor economy",
        "금리": "federal reserve interest rate banking",
        "기술주": "technology nasdaq silicon valley innovation",
        "에너지": "energy oil renewable petroleum",
        "인플레이션": "inflation economy money prices",
        "고용": "employment jobs economy workforce",
        "default": "stock market wall street finance morning",
    },
    "evening": {
        "상승": "stock market bull trading night growth",
        "하락": "stock market bear crisis red night",
        "혼조": "premarket trading floor economy night",
        "금리": "federal reserve interest rate banking night",
        "기술주": "technology nasdaq innovation night",
        "에너지": "energy oil market night",
        "인플레이션": "inflation economy money night",
        "고용": "employment jobs economy night",
        "default": "stock market premarket trading finance night",
    },
}

# 모드별 fallback 이미지 URL (Pexels/Pixabay 모두 실패했을 때 최종 폴백)
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


def _daily_index(length: int) -> int:
    """날짜 기반으로 매일 다른 인덱스를 골라 같은 검색 결과 안에서도 이미지가 바뀌게 함."""
    if length <= 0:
        return 0
    seed = int(datetime.now().strftime("%Y%m%d"))
    return seed % length


class ImageGenerator:
    def __init__(self, hf_token: str = ""):
        # hf_token은 더 이상 사용하지 않지만, 기존 호출부(main.py)와의
        # 하위 호환을 위해 인자는 그대로 받아둡니다.
        self.pexels_key = os.environ.get("PEXELS_API_KEY", "")
        self.pixabay_key = os.environ.get("PIXABAY_API_KEY", "")
        self.last_image_source = ""  # "Pexels" | "Pixabay" | "Unsplash" | ""
        os.makedirs(OUTPUT_DIR, exist_ok=True)

    def get_attribution_text(self) -> str:
        """가장 최근 generate() 호출에서 실제로 사용된 이미지 출처에 맞는
        저작자 표시 문구를 반환합니다. 각 서비스 라이선스는 표시를 의무로
        요구하지는 않지만, 안전하게 자동으로 남겨둡니다."""
        credit_map = {
            "Pexels": "사진 제공: Pexels",
            "Pixabay": "사진 제공: Pixabay",
            "Unsplash": "사진 제공: Unsplash",
        }
        return credit_map.get(self.last_image_source, "")

    def generate(
        self,
        prompt: str,
        filename: str,
        content: str = "",
        mode: str = "morning",
    ) -> str:
        keywords = _extract_keywords(prompt, content, mode)
        logger.info(f"이미지 검색 키워드 ({mode}): {keywords}")

        result = self._fetch_pexels(keywords, filename)
        if result:
            self.last_image_source = "Pexels"
            return result

        logger.info("Pixabay로 대체 시도...")
        result = self._fetch_pixabay(keywords, filename)
        if result:
            self.last_image_source = "Pixabay"
            return result

        logger.warning("Pexels/Pixabay 모두 실패. 기본 이미지 사용")
        self.last_image_source = "Unsplash"
        return self._default_fallback(filename, mode)

    # ── Pexels ────────────────────────────────────────────────────────────
    def _fetch_pexels(self, keywords: str, filename: str) -> str | None:
        if not self.pexels_key:
            logger.info("PEXELS_API_KEY 미설정 — Pexels 건너뜀")
            return None
        try:
            resp = requests.get(
                "https://api.pexels.com/v1/search",
                headers={"Authorization": self.pexels_key},
                params={"query": keywords, "per_page": 10, "orientation": "landscape"},
                timeout=15,
            )
            resp.raise_for_status()
            photos = resp.json().get("photos", [])
            if not photos:
                logger.warning(f"Pexels 결과 없음: {keywords}")
                return None

            idx = _daily_index(len(photos))
            img_url = photos[idx]["src"]["large2x"]

            ir = requests.get(img_url, timeout=20)
            ir.raise_for_status()
            if len(ir.content) < 10000:
                logger.warning("Pexels 이미지 응답이 너무 작음")
                return None

            file_path = os.path.join(OUTPUT_DIR, filename.replace(".png", ".jpg"))
            with open(file_path, "wb") as f:
                f.write(ir.content)
            logger.info(f"Pexels 이미지 저장: {file_path} ({len(ir.content)//1024}KB)")
            return file_path
        except Exception as e:
            logger.warning(f"Pexels 실패: {e}")
            return None

    # ── Pixabay ───────────────────────────────────────────────────────────
    def _fetch_pixabay(self, keywords: str, filename: str) -> str | None:
        if not self.pixabay_key:
            logger.info("PIXABAY_API_KEY 미설정 — Pixabay 건너뜀")
            return None
        try:
            resp = requests.get(
                "https://pixabay.com/api/",
                params={
                    "key": self.pixabay_key,
                    "q": keywords,
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
                logger.warning(f"Pixabay 결과 없음: {keywords}")
                return None

            idx = _daily_index(len(hits))
            img_url = hits[idx]["largeImageURL"]

            ir = requests.get(img_url, timeout=20)
            ir.raise_for_status()
            if len(ir.content) < 10000:
                logger.warning("Pixabay 이미지 응답이 너무 작음")
                return None

            file_path = os.path.join(OUTPUT_DIR, filename.replace(".png", ".jpg"))
            with open(file_path, "wb") as f:
                f.write(ir.content)
            logger.info(f"Pixabay 이미지 저장: {file_path} ({len(ir.content)//1024}KB)")
            return file_path
        except Exception as e:
            logger.warning(f"Pixabay 실패: {e}")
            return None

    # ── 최종 폴백 ─────────────────────────────────────────────────────────
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
