"""
이미지 생성 모듈 (v4 — Cloudflare Workers AI 기반)
우선순위:
  1순위: Cloudflare Workers AI · FLUX.1-schnell (AI 생성 — 완전 무료, 신용카드 불필요)
  2순위: HuggingFace Inference Providers · FLUX.1-schnell (유료 크레딧이 남은 계정 대비 보존)
  3순위: Pexels API (스톡사진)
  4순위: Pixabay API (스톡사진)
  5순위: gradient fallback

v3 → v4 변경 이유 (중요 — 최근 "AI 생성 대신 Pexels만 나오는" 문제의 원인):
  - Hugging Face Inference Providers(fal-ai/together/replicate)는 **무료 계정
    기준 월 $0.10 상당의 크레딧만** 제공합니다. 이 채널은 main.py(1일 2회)와
    marketing 파이프라인(SNS 썸네일까지 합치면 1일 최대 8회 이상)이 같은
    HF_API_TOKEN을 공유해서 FLUX를 호출하므로, 월초 며칠 안에 그 $0.10
    크레딧이 바닥나고 이후 요청은 전부 결제 필요(402 Payment Required)로
    실패합니다. 게다가 hf-inference provider는 2026-07경 FLUX.1-schnell 무료
    서빙 자체를 중단(410)했습니다. 즉 무료 HF 토큰만으로는 크레딧 소진 이후
    "며칠 반짝 성공 → 이후 계속 Pexels로 폴백"이 구조적으로 반복될 수밖에
    없었습니다. (HF PRO 결제 없이는 해결 불가)
  - 반면 Cloudflare Workers AI는 **하루 10,000 뉴런이 매일 자정(UTC) 초기화**
    되어 제공되고, FLUX.1-schnell 1장에 약 40~60 뉴런 수준이라 신용카드 등록
    없이도 하루 수백 장을 무료로 생성할 수 있습니다. 이 프로젝트는 이미
    Cloudflare Pages/D1을 대시보드로 쓰고 있어 추가 계정 가입도 필요
    없습니다. 그래서 1순위를 Cloudflare Workers AI로 교체했습니다.
  - 기존 HF 경로는 삭제하지 않고 2순위로 남겨둡니다 (나중에 유료 크레딧을
    충전한 HF 계정을 쓰고 싶어질 경우를 대비 — 최소 변경 원칙).
  - CLOUDFLARE_ACCOUNT_ID / CLOUDFLARE_API_TOKEN이 설정되어 있지 않으면
    이 단계는 조용히 건너뛰고 기존 경로(HF → Pexels → Pixabay → gradient)로
    그대로 동작하므로, 시크릿을 아직 추가하지 않은 상태에서도 기존 동작을
    깨뜨리지 않습니다.

v4 추가 기능 — 썸네일 중앙 제목 오버레이:
  - AI 생성이든 Pexels/Pixabay 스톡사진이든, 어떤 배경을 쓰든 상관없이
    블로그 글 제목을 이미지 중앙에 큼직하게 얹어서, 썸네일만 보고도 어떤
    내용인지 기대감이 들도록 합니다 (generate()의 title 인자).
  - 한글 렌더링을 위해 Noto Sans CJK 폰트가 필요합니다. GitHub Actions
    워크플로우(.github/workflows/daily_post.yml)에 폰트 설치 단계가
    추가되어 있어야 하며, 폰트가 없으면 이 기능은 예외 없이 조용히
    건너뛰고 원본 이미지를 그대로 사용합니다 (파이프라인 중단 방지).

mode:
  morning : 마감 후 조용한 월스트리트 분위기 (차분·분석적)
  evening : 개장 전 활기찬 트레이딩 분위기 (긴장·역동적)
"""

import base64
import html as _html_module
import re
import requests
import logging
import os
import time
import hashlib
from datetime import datetime
from PIL import Image, ImageDraw, ImageFont
from io import BytesIO


def _clean_text(text: str) -> str:
    """
    이미지 생성 전 텍스트 정제.
    HTML 엔티티(&middot; &amp; &ndash; 등)를 실제 문자로 변환합니다.
    Gemini가 JSON 내부에 HTML 엔티티를 출력하는 경우를 방지합니다.
    """
    text = _html_module.unescape(text)
    text = re.sub(r"&#(\d+);", lambda m: chr(int(m.group(1))), text)
    text = re.sub(r"&#x([0-9a-fA-F]+);", lambda m: chr(int(m.group(1), 16)), text)
    return text.strip()

logger = logging.getLogger(__name__)

# ── Cloudflare Workers AI (신규 1순위 — 완전 무료) ──────────────────────────
CF_FLUX_MODEL = "@cf/black-forest-labs/flux-1-schnell"
CF_API_BASE = "https://api.cloudflare.com/client/v4/accounts"

# ── HuggingFace FLUX.1-schnell (Inference Providers 경유, 2순위 — 유료 크레딧
# 대비 보존) ─────────────────────────────────────────────────────────────────
# 2026-07-15경 hf-inference provider에서 FLUX.1-schnell 서빙이 중단되어
# (HTTP 410 "deprecated and no longer supported by provider hf-inference")
# fal-ai/together/replicate로 순서대로 폴백을 시도했으나, 이 provider들도
# raw REST 요청(router.huggingface.co/{provider}/models/{model} 직접 호출)에는
# 즉시 400 "Model not supported by provider"를 반환했습니다.
#
# 원인: HuggingFace 라우터는 provider별로 실제 내부 모델 ID가 다를 수 있어
# (예: google/gemma-3-27b-it → scaleway에서는 google/gemma-3-27b-it-fast)
# 이 매핑을 huggingface_hub 파이썬 라이브러리(InferenceClient)가 내부적으로
# 해석해서 요청을 보냅니다. raw HTTP로 모델 원본 ID를 그대로 넣으면 이
# 변환이 적용되지 않아 라우터가 매핑을 못 찾고 400을 반환합니다.
# → huggingface_hub 라이브러리를 사용하도록 변경해 이 문제를 해결합니다.
#
# 다만 fal-ai/together/replicate는 무료 계정 기준 월 $0.10 크레딧만 제공되어
# 금방 소진되므로(위 모듈 docstring 참고), 이제는 Cloudflare Workers AI가
# 성공하면 이 경로는 아예 시도하지 않습니다 — 크레딧을 아끼기 위함입니다.
HF_FLUX_PROVIDERS = ["fal-ai", "together", "replicate", "hf-inference"]
HF_FLUX_MODEL = "black-forest-labs/FLUX.1-schnell"


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

# ── 썸네일 제목 오버레이용 시스템 폰트 (한글 렌더링 필수) ────────────────────
_FONT_BLACK = "/usr/share/fonts/opentype/noto/NotoSansCJK-Black.ttc"
_FONT_BOLD = "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc"


def _crop_fit(img: Image.Image, W: int, H: int) -> Image.Image:
    """중앙 기준으로 크롭한 뒤 목표 크기로 리사이즈합니다. Cloudflare Workers AI
    처럼 width/height를 요청할 수 없는 소스의 출력물을 표준 썸네일 비율로
    맞추는 데 사용합니다 (marketing/video_generator/thumbnail.py의 동명 함수와
    동일한 로직)."""
    sw, sh = img.size
    if sw / sh > W / H:
        nh = sh
        nw = int(nh * W / H)
        ox = (sw - nw) // 2
        img = img.crop((ox, 0, ox + nw, nh))
    else:
        nw = sw
        nh = int(nw * H / W)
        oy = (sh - nh) // 3
        img = img.crop((0, oy, nw, oy + nh))
    return img.resize((W, H), Image.LANCZOS)


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


# ── 썸네일 제목 오버레이 헬퍼 ─────────────────────────────────────────────────

def _load_title_font(size: int) -> ImageFont.FreeTypeFont | None:
    """Noto Sans CJK 폰트를 로드합니다. 시스템에 폰트가 없으면 None을 반환해
    호출부가 오버레이 자체를 건너뛰도록 합니다 (한글이 깨진 채로 그려지는
    것을 방지 — PIL 기본 폰트는 한글을 지원하지 않습니다)."""
    for path in (_FONT_BLACK, _FONT_BOLD):
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size, index=0)
            except Exception:
                continue
    return None


def _wrap_title(draw, text: str, font, max_px: int, max_lines: int = 3) -> list:
    words = text.split()
    lines, cur = [], ""
    for word in words:
        if len(lines) >= max_lines:
            break
        candidate = f"{cur} {word}".strip()
        if draw.textbbox((0, 0), candidate, font=font)[2] <= max_px:
            cur = candidate
        else:
            if cur:
                lines.append(cur)
            cur = word
    if cur and len(lines) < max_lines:
        lines.append(cur)
    return lines or [text[:18]]


def _draw_title_outlined(draw, xy, text, font, fill=(255, 255, 255), outline=(0, 0, 0), ow=4):
    x, y = xy
    for dx, dy in [(-ow, 0), (ow, 0), (0, -ow), (0, ow),
                   (-ow, -ow), (ow, -ow), (-ow, ow), (ow, ow)]:
        draw.text((x + dx, y + dy), text, font=font, fill=(*outline, 235))
    draw.text((x, y), text, font=font, fill=fill)


class ImageGenerator:
    def __init__(self, hf_token: str = ""):
        self.hf_token = hf_token
        self.cf_account_id = os.environ.get("CLOUDFLARE_ACCOUNT_ID", "")
        self.cf_api_token = os.environ.get("CLOUDFLARE_API_TOKEN", "")
        self.pexels_key = os.environ.get("PEXELS_API_KEY", "")
        self.pixabay_key = os.environ.get("PIXABAY_API_KEY", "")
        self.last_image_source = ""
        os.makedirs(OUTPUT_DIR, exist_ok=True)

    def get_attribution_text(self) -> str:
        """
        스톡 이미지(Pexels/Pixabay) 폴백 시에만 출처 문구를 반환합니다.
        Cloudflare Workers AI / FLUX.1-schnell로 생성한 이미지는 Apache 2.0
        라이선스상 결과물에 저작자 표시 의무가 없으므로(모델 자체를
        재배포/수정할 때만 고지 필요) 출처 표시를 생략합니다.
        """
        credit_map = {
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
        title: str = "",
    ) -> str:
        # 진입점에서 HTML 엔티티 정제 (Gemini 생성 프롬프트에 &middot; 등 포함 가능)
        prompt = _clean_text(prompt)

        # 1순위: Cloudflare Workers AI (완전 무료, 신용카드 불필요)
        result = self._generate_cloudflare(prompt, filename, mode)
        if result:
            self.last_image_source = "Cloudflare Workers AI"
            return self._finalize(result, title, mode)

        # 2순위: HuggingFace FLUX.1-schnell (유료 크레딧이 남은 계정 대비 보존)
        logger.info("Cloudflare Workers AI 실패/미설정 → HuggingFace FLUX로 대체 시도...")
        result = self._generate_flux(prompt, filename, mode)
        if result:
            self.last_image_source = "FLUX.1-schnell"
            return self._finalize(result, title, mode)

        # 3순위: Pexels
        logger.info("AI 이미지 생성 실패 → Pexels로 대체 시도...")
        result = self._fetch_pexels(prompt, content, filename, mode)
        if result:
            self.last_image_source = "Pexels"
            return self._finalize(result, title, mode)

        # 4순위: Pixabay
        logger.info("Pexels 실패 → Pixabay로 대체 시도...")
        result = self._fetch_pixabay(prompt, content, filename, mode)
        if result:
            self.last_image_source = "Pixabay"
            return self._finalize(result, title, mode)

        # 5순위: gradient fallback
        logger.warning("모든 이미지 소스 실패 → gradient fallback 사용")
        self.last_image_source = ""
        result = self._gradient_fallback(filename, mode)
        return self._finalize(result, title, mode)

    # ── 제목 오버레이 최종 적용 (실패해도 파이프라인을 절대 중단시키지 않음) ──
    def _finalize(self, file_path: str, title: str, mode: str) -> str:
        if not title:
            return file_path
        try:
            self._apply_title_overlay(file_path, title, mode)
        except Exception as e:
            logger.warning(f"썸네일 제목 오버레이 실패 (원본 이미지 유지): {e}")
        return file_path

    # ── Cloudflare Workers AI (신규 1순위) ───────────────────────────────────
    def _generate_cloudflare(self, prompt: str, filename: str, mode: str) -> str | None:
        if not self.cf_account_id or not self.cf_api_token:
            logger.info(
                "CLOUDFLARE_ACCOUNT_ID/CLOUDFLARE_API_TOKEN 미설정 — "
                "Cloudflare Workers AI 건너뜀"
            )
            return None

        suffix = FLUX_SUFFIX.get(mode, FLUX_SUFFIX["morning"])
        full_prompt = f"{prompt}{suffix}"

        # 주의: Cloudflare Workers AI의 flux-1-schnell 공식 입력 스키마는
        # prompt(필수)와 steps(기본 4, 최대 8)만 받습니다 — width/height는
        # 아예 지원하지 않고, 파라미터 이름도 "num_steps"가 아니라 "steps"
        # 입니다. 문서 참고:
        # https://developers.cloudflare.com/workers-ai/models/flux-1-schnell/schema-input.json
        # [실전 확인] 문서의 curl 예제에는 seed가 등장하지만, 실제 API는
        # seed를 보내면 매번 400 Bad Request로 거부합니다
        # ("Additional or unevaluated properties '/seed' at '/' not allowed") —
        # 스키마가 반드시 필요한 필드 없어야 함(additionalProperties: false)을
        # 엄격히 검증합니다. 그래서 seed는 절대 페이로드에 넣지 않습니다
        # (매일 같은 이미지를 재현하는 기능은 이 모델에서는 포기).
        url = f"{CF_API_BASE}/{self.cf_account_id}/ai/run/{CF_FLUX_MODEL}"
        payload = {
            "prompt": full_prompt,
            "steps": 8,
        }

        logger.info(f"Cloudflare Workers AI 이미지 생성 중 (모드: {mode})...")
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
                    f"Cloudflare Workers AI 실패 ({resp.status_code}): {resp.text[:200]}"
                )
                return None

            # 응답이 JSON(+base64)이 아니라 raw 이미지 바이너리로 올 가능성도
            # 방어적으로 함께 처리합니다 (Workers AI 모델별로 응답 형식이
            # 다를 수 있음 — 공식 예제는 base64 JSON을 반환).
            content_type = resp.headers.get("Content-Type", "")
            if content_type.startswith("image/"):
                image = Image.open(BytesIO(resp.content)).convert("RGB")
            else:
                data = resp.json()
                if not data.get("success", True):
                    logger.warning(f"Cloudflare Workers AI 실패: {data.get('errors')}")
                    return None
                b64_img = (data.get("result") or {}).get("image", "")
                if not b64_img:
                    logger.warning("Cloudflare Workers AI 응답에 image 데이터가 없음")
                    return None
                image = Image.open(BytesIO(base64.b64decode(b64_img))).convert("RGB")

            # width/height를 요청할 수 없으므로, 받은 이미지를 우리가 쓰는
            # 표준 썸네일 비율(1024x576, 16:9)로 크롭 — Pexels/Pixabay/HF와
            # 동일한 비율을 유지하기 위함.
            image = _crop_fit(image, 1024, 576)

            file_path = os.path.join(OUTPUT_DIR, filename.replace(".png", ".jpg"))
            image.save(file_path, "JPEG", quality=92, optimize=True)
            logger.info(f"Cloudflare Workers AI 이미지 저장: {file_path}")
            return file_path

        except Exception as e:
            logger.warning(f"Cloudflare Workers AI 오류: {e}")
            return None

    # ── FLUX.1-schnell (huggingface_hub 라이브러리 경유, provider 순차 폴백) ──
    def _generate_flux(
        self, prompt: str, filename: str, mode: str, max_retries: int = 2
    ) -> str | None:
        if not self.hf_token:
            logger.info("HF_API_TOKEN 미설정 — FLUX 건너뜀")
            return None

        try:
            from huggingface_hub import InferenceClient
            from huggingface_hub.errors import HfHubHTTPError
        except ImportError:
            logger.error(
                "huggingface_hub 미설치 — requirements.txt에 "
                "huggingface_hub 추가 필요 (pip install huggingface_hub)"
            )
            return None

        suffix = FLUX_SUFFIX.get(mode, FLUX_SUFFIX["morning"])
        full_prompt = f"{prompt}{suffix}"

        # 날짜+모드 기반 시드로 매일 다른 이미지
        seed = int(hashlib.md5(
            f"{datetime.now().strftime('%Y%m%d')}{mode}".encode()
        ).hexdigest()[:8], 16) % (2 ** 32)

        for provider in HF_FLUX_PROVIDERS:
            logger.info(
                f"FLUX.1-schnell 이미지 생성 중 (provider: {provider}, "
                f"모드: {mode}, 시드: {seed})..."
            )
            result = self._try_flux_provider(
                InferenceClient, HfHubHTTPError,
                provider, full_prompt, seed, filename, max_retries,
            )
            if result:
                return result
            logger.warning(f"provider={provider} 실패 — 다음 provider로 폴백")

        logger.warning(
            f"모든 FLUX provider 실패({', '.join(HF_FLUX_PROVIDERS)}) — Pexels로 전환 "
            "(무료 계정은 월 $0.10 크레딧 소진 후 전부 402로 실패하는 것이 정상적인 "
            "현상입니다. Cloudflare Workers AI 설정을 권장합니다.)"
        )
        return None

    def _try_flux_provider(
        self,
        InferenceClient,
        HfHubHTTPError,
        provider: str,
        full_prompt: str,
        seed: int,
        filename: str,
        max_retries: int,
    ) -> str | None:
        """
        단일 provider에 대해 huggingface_hub.InferenceClient로 이미지 생성을
        시도합니다. 이 클라이언트는 provider별 실제 내부 모델 ID 매핑을
        자동으로 처리하므로, raw REST 호출에서 발생했던
        "Model not supported by provider" 400 오류를 피할 수 있습니다.

        확정적으로 사용 불가한 오류(404/400 모델 미지원)는 즉시 None을
        반환해 다음 provider로 넘어가고, 일시적 오류(503/429)는 재시도합니다.
        """
        client = InferenceClient(provider=provider, api_key=self.hf_token, timeout=60)

        for attempt in range(1, max_retries + 1):
            try:
                image = client.text_to_image(
                    full_prompt,
                    model=HF_FLUX_MODEL,
                    guidance_scale=0.0,
                    num_inference_steps=4,
                    width=1024,
                    height=576,
                    seed=seed,
                )
                file_path = os.path.join(
                    OUTPUT_DIR, filename.replace(".png", ".jpg")
                )
                image.convert("RGB").save(file_path, "JPEG", quality=92, optimize=True)
                logger.info(f"[{provider}] FLUX 이미지 저장: {file_path}")
                return file_path

            except HfHubHTTPError as e:
                status = getattr(e.response, "status_code", None) if hasattr(e, "response") else None
                msg = str(e)[:200]

                if status == 402:
                    logger.warning(
                        f"[{provider}] 결제 필요(402) — 무료 크레딧 소진 가능성. "
                        f"HF PRO 결제 또는 Cloudflare Workers AI 사용을 권장합니다."
                    )
                    return None

                if status == 503 or "loading" in msg.lower():
                    wait = 20
                    logger.warning(
                        f"[{provider}] FLUX 모델 로딩 중 (503) — {wait}초 대기 "
                        f"(시도 {attempt}/{max_retries})..."
                    )
                    if attempt < max_retries:
                        time.sleep(wait)
                        continue
                    return None

                if status == 429:
                    logger.warning(f"[{provider}] FLUX 요청 한도 초과 (429)")
                    return None

                if status in (400, 404, 410):
                    # 이 provider에서 모델이 지원되지 않음 — 재시도 무의미,
                    # 즉시 다음 provider로 넘어감
                    logger.warning(
                        f"[{provider}] FLUX 모델 미지원 ({status}): {msg}"
                    )
                    return None

                logger.warning(f"[{provider}] FLUX 실패 ({status}): {msg}")
                return None

            except Exception as e:
                logger.warning(f"[{provider}] FLUX 오류 (시도 {attempt}/{max_retries}): {e}")
                if attempt < max_retries:
                    time.sleep(5)
                else:
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

    # ── 썸네일 중앙 제목 오버레이 ─────────────────────────────────────────────
    def _apply_title_overlay(self, file_path: str, title: str, mode: str) -> None:
        """
        생성/수집된 썸네일 이미지 중앙에 블로그 제목을 오버레이합니다.
        AI 생성이든 Pexels/Pixabay 스톡사진이든 동일하게 적용되어, 썸네일만
        보고도 어떤 글인지 기대감이 들도록 합니다.

        한글 폰트가 없으면 아무것도 하지 않고 조용히 반환합니다 (원본 이미지
        유지 — 절대 파이프라인을 중단시키지 않음).
        """
        if not any(os.path.exists(p) for p in (_FONT_BLACK, _FONT_BOLD)):
            logger.warning(
                "한글 폰트(Noto Sans CJK)를 찾을 수 없어 썸네일 제목 오버레이를 "
                "건너뜁니다. GitHub Actions 워크플로우에 'fonts-noto-cjk' 설치 "
                "단계가 있는지 확인하세요."
            )
            return

        clean_title = _clean_text(title)
        # SEO용 날짜/접두어는 화면에 다시 보여줄 필요가 없으므로 제거해
        # 짧고 강렬하게 만듭니다.
        stripped = re.sub(r"^\d{4}년\s*\d{1,2}월\s*\d{1,2}일\s*", "", clean_title)
        stripped = re.sub(r"^미국\s*증시\s*[:：]?\s*", "", stripped).strip()
        display_title = stripped or clean_title
        if not display_title:
            return

        img = Image.open(file_path).convert("RGBA")
        W, H = img.size
        accent = (56, 189, 248) if mode == "morning" else (167, 139, 250)

        # 중앙 영역 가독성 확보용 반투명 밴드 (배경이 밝든 어둡든 텍스트가
        # 항상 잘 보이도록 함)
        band = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        bd = ImageDraw.Draw(band)
        band_top, band_bottom = int(H * 0.30), int(H * 0.74)
        fade = max(1, int(H * 0.10))
        for y in range(band_top, band_bottom):
            t = min((y - band_top) / fade, (band_bottom - y) / fade, 1.0)
            alpha = int(175 * max(0.0, t))
            bd.line([(0, y), (W, y)], fill=(4, 8, 20, alpha))
        img = Image.alpha_composite(img, band)
        draw = ImageDraw.Draw(img)

        font_size = int(H * 0.11)
        max_px = int(W * 0.86)
        font = _load_title_font(font_size)
        lines = _wrap_title(draw, display_title, font, max_px, max_lines=3)

        # 3줄로도 안 들어가면 폰트를 줄여가며 재시도 (최소 크기까지)
        min_size = int(H * 0.06)
        while len(lines) >= 3 and font_size > min_size:
            font_size -= 6
            font = _load_title_font(font_size)
            lines = _wrap_title(draw, display_title, font, max_px, max_lines=3)

        line_h = int(font_size * 1.28)
        total_h = line_h * len(lines)
        start_y = H // 2 - total_h // 2

        # 제목 위 짧은 포인트 라인
        draw.rectangle(
            [(W // 2 - 46, start_y - 22), (W // 2 + 46, start_y - 16)],
            fill=(*accent, 255),
        )

        y = start_y
        for line in lines:
            bbox = draw.textbbox((0, 0), line, font=font)
            lw = bbox[2] - bbox[0]
            _draw_title_outlined(draw, (W // 2 - lw // 2, y), line, font)
            y += line_h

        img.convert("RGB").save(file_path, "JPEG", quality=92, optimize=True)
        logger.info(f"썸네일 제목 오버레이 적용 완료: {file_path}")
