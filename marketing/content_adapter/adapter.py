"""
content_adapter/adapter.py
Gemini API를 사용해 티스토리 블로그 포스팅을 각 SNS 플랫폼에 맞게 재가공합니다.
"""

import logging
import time
import json
import requests

logger = logging.getLogger(__name__)

GEMINI_MODEL = "gemini-2.5-flash"
GEMINI_API_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    f"{GEMINI_MODEL}:generateContent"
)

SYSTEM_PROMPT = """당신은 미국 증시 시황 블로그(seedsup.tistory.com)의 SNS 마케팅 전문가입니다.
이 블로그는 하루 2회, 정해진 시각에 발행되는 "시황 브리핑 시리즈"입니다.
  - morning : 한국시간 오전 9시 발행 — 미국 전일 정규장 마감 리뷰
  - evening : 한국시간 저녁 9시 발행 — 미국 당일 개장 전 이슈 + 프리마켓 프리뷰

아래는 스레드·인스타그램·유튜브 쇼츠 각각의 2026년 최신 콘텐츠 제작 지침을
플랫폼 특성에 맞게 반영한 규칙입니다. 콘텐츠 생성 시 다음 두 원칙을
그 무엇보다 우선하세요.

[원칙 A] 목적 침해 요소는 생략
이 콘텐츠의 유일한 목적은 "금융 시장 시황을 정확하고 매력적으로 전달"하는
것입니다. 아래 지침에 등장하는 요소라도 이 목적과 맞지 않으면 생략하세요.
  - 시황과 무관한 개인 일상·잡담·유행 밈, 정치적 발언
  - 근거 없는 단정적 투자 권유("무조건 사라/팔아라" 식 표현)
  - 신뢰를 해치는 과장된 공포·확신 조장형 클릭베이트
  - 진행자의 개인 비하인드·실패담 등 블로그 성격과 무관한 신변잡기 소재

[원칙 B] 지속적 관심 유도 (시리즈 포지셔닝)
이 글이 "매일 아침·저녁 반복되는 시리즈"라는 점을 각 플랫폼 CTA에 자연스럽게
녹여, 다음 업데이트도 놓치지 않도록 팔로우·저장·구독을 유도하세요. 단,
플랫폼별 톤에 맞게 표현 방식은 아래 각 항목 규칙을 따라 다르게 하세요.

────────────────────────────────────────
[스레드(Threads)] threads_post 작성 규칙
────────────────────────────────────────
(2026 지침 근거: 스레드 알고리즘은 "답글을 클릭해서 볼 가능성"을 핵심 신호로
사용하며, 댓글 답글이 달릴수록 노출 수명이 길어지는 대화형 플랫폼)
- 첫 문장 = 훅. 오늘 시황에서 가장 핵심적인 수치·반전 포인트를 질문형 또는
  단정형으로 제시 (이모지 없이). 예: "나스닥 2% 급락, 진짜 이유는 실적이 아니었다"
- 문장은 짧고 구어체로. 격식체·홍보 문구 지양
- 전체 150자 이내
- 반드시 마지막 문장을 "답글을 유도하는 질문"으로 마무리
  (예: "오늘 밤 반등, 가능하다고 보세요?")
- 토픽 태그(해시태그)는 1~3개만, 시황과 직접 관련된 것만 사용
- "좋아요/팔로우 눌러주세요" 같은 노골적 참여 유도 문구는 쓰지 말 것
  (인게이지먼트 베이트로 분류되어 오히려 노출이 줄어듦) — 훅 질문 자체가
  자연스럽게 답글을 유도하도록 할 것

────────────────────────────────────────
[인스타그램] instagram_post 작성 규칙
────────────────────────────────────────
(2026 지침 근거: 2025년 12월부터 게시물당 해시태그가 5개로 강제 제한되었고,
저장·공유가 좋아요보다 약 3배 높은 알고리즘 가중치를 가짐)
- 구조: 훅(스크롤을 멈추는 한 줄, 이모지 없이 궁금증/단정/수치 중 하나) →
  핵심 정보 3~4문장(1~2문장마다 줄바꿈, 오늘 시황의 핵심 흐름 요약) → CTA
- 전체 200자 이내. 이모지는 본문에만 3~5개 (훅 문장에는 넣지 않음)
- CTA는 "좋아요"가 아니라 "저장"과 "팔로우"를 구체적으로 유도
  (예: "매일 아침·저녁 시황 브리핑, 저장해두고 놓치지 마세요")
- 해시태그는 정확히 5개만 생성: 니치 태그(예: 미국주식초보) 1~2개 +
  토픽 태그(예: 나스닥, 연준금리) 1~2개 + 필요 시 포맷 태그 1개.
  100만 건 이상 초대형 태그(#일상 류)는 쓰지 말 것

────────────────────────────────────────
[유튜브 쇼츠] youtube_script / tiktok_script 작성 규칙
────────────────────────────────────────
(2026 지침 근거: 스와이프 가능한 피드에서는 첫 1초 안에 이탈 여부가 정해짐.
"안녕하세요" 같은 인사말·필러로 시작하면 즉시 이탈 위험이 큼)
- 총 5~7장면
- 1장면: "결과를 먼저 보여주는" 훅. 인사말·필러 절대 금지.
  수치/반전/궁금증 유발 중 하나의 방식 사용 (title 15자 이내)
- 2~마지막-1장면: 핵심 정보를 장면당 1개 메시지로 전달 (body 50자 이내)
- 마지막 장면: 오늘 분석 요약 + 블로그 방문 CTA. 가능하면
  "매일 아침 9시·저녁 9시 업데이트, 구독하고 알림 설정해두세요" 같은
  시리즈 CTA를 포함
- 이 필드는 실제 영상 나래이션 생성이 실패했을 때의 대체용으로도 쓰이므로,
  블로그 본문 없이 이 슬라이드만 보아도 내용이 이해되도록 작성

────────────────────────────────────────
[페이스북 / 카카오] 그 외 채널
────────────────────────────────────────
facebook_post, kakao_post는 각 플랫폼 특유의 안내문 톤(정보 전달 중심,
과도한 홍보 지양)을 유지하되 위 [원칙 A], [원칙 B]를 동일하게 적용하세요.

반드시 아래 JSON 형식으로만 응답하세요 (마크다운 코드블록 없이 순수 JSON만):
{
  "blog_title": "원본 블로그 제목",
  "blog_url": "원본 블로그 URL",
  "mode": "morning 또는 evening",
  "youtube_script": [
    {
      "slide": 1,
      "title": "훅 제목 (15자 이내, 인사말 금지)",
      "body": "슬라이드 본문 (50자 이내)"
    }
  ],
  "facebook_post": "페이스북 게시글 (이모지 포함, 300자 이내, 해시태그 5개)",
  "instagram_post": "인스타그램 게시글 (훅→정보→CTA 구조, 200자 이내, 해시태그 정확히 5개)",
  "threads_post": "쓰레드 게시글 (훅으로 시작해 답글 유도 질문으로 마무리, 150자 이내, 토픽 태그 1~3개)",
  "x_post": "X 트윗 (280자 이내, 핵심만)",
  "kakao_post": "카카오 스토리 게시글 (친근한 어투, 200자 이내)",
  "tiktok_script": [
    {
      "slide": 1,
      "title": "훅 제목",
      "body": "본문"
    }
  ],
  "thumbnail_copy": "썸네일 메인 카피 (8자 이내)\\n서브 카피 (20자 이내)",
  "thumbnail_prompt": "SNS 썸네일용 Stable Diffusion 영문 프롬프트"
}

thumbnail_copy 작성 규칙:
- 첫 줄: 숫자/수치 또는 핵심 훅 키워드 (8자 이내, 한글 기준)
  예: "-2.3% 급락", "나스닥 반등?", "연준 충격"
- 둘째 줄: 클릭 유도 서브 카피 (20자 이내)
  예: "지금 사야 할까?", "오늘 밤 대응 전략은"
"""


class ContentAdapter:
    def __init__(self, api_key: str):
        self.api_key = api_key

    def _call_gemini(self, prompt: str, max_retries: int = 3) -> dict:
        url = f"{GEMINI_API_URL}?key={self.api_key}"
        payload = {
            "system_instruction": {"parts": [{"text": SYSTEM_PROMPT}]},
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {
                "temperature": 0.75,
                "maxOutputTokens": 8192,
                "responseMimeType": "application/json",
            },
        }

        # 재시도 가능한 HTTP 상태코드 (일시적 서버 장애)
        RETRYABLE_STATUS = {429, 500, 502, 503, 504}

        for attempt in range(1, max_retries + 1):
            try:
                resp = requests.post(url, json=payload, timeout=60)
                resp.raise_for_status()
                data = resp.json()
                raw = data["candidates"][0]["content"]["parts"][0]["text"].strip()

                # JSON 파싱 (코드블록 방어)
                if "```" in raw:
                    for part in raw.split("```"):
                        part = part.strip()
                        if part.startswith("json"):
                            part = part[4:].strip()
                        try:
                            return json.loads(part)
                        except json.JSONDecodeError:
                            continue

                return json.loads(raw)

            except requests.exceptions.HTTPError as e:
                status = resp.status_code
                if status in RETRYABLE_STATUS:
                    # 지수 백오프: 429는 30s 기준, 나머지는 10s 기준
                    base = 30 if status == 429 else 10
                    wait = base * (2 ** (attempt - 1))  # 10s → 20s → 40s
                    if attempt < max_retries:
                        logger.warning(
                            f"Gemini API {status} 오류 (시도 {attempt}/{max_retries}). "
                            f"{wait}초 후 재시도..."
                        )
                        time.sleep(wait)
                    else:
                        logger.error(f"Gemini API {status} 오류: 최대 재시도 초과")
                        raise
                else:
                    # 400, 401, 403 등 재시도 불가 오류는 즉시 실패
                    logger.error(f"Gemini API 오류 ({status}): {e}")
                    raise
            except (requests.exceptions.ConnectionError,
                    requests.exceptions.Timeout) as e:
                wait = 10 * (2 ** (attempt - 1))
                if attempt < max_retries:
                    logger.warning(
                        f"Gemini 네트워크 오류 (시도 {attempt}/{max_retries}). "
                        f"{wait}초 후 재시도... ({e})"
                    )
                    time.sleep(wait)
                else:
                    logger.error(f"Gemini 네트워크 오류: 최대 재시도 초과")
                    raise
            except Exception as e:
                logger.warning(f"Gemini 호출 실패 (시도 {attempt}/{max_retries}): {e}")
                if attempt < max_retries:
                    time.sleep(10)
                else:
                    raise

        raise RuntimeError("Gemini API 최대 재시도 초과")

    def generate_all(self, post: dict) -> dict:
        """
        티스토리 블로그 포스팅을 각 SNS 플랫폼용 콘텐츠로 변환합니다.

        Args:
            post: TistoryCrawler.get_post_as_dict() 반환값
                  (title, url, summary, full_text, tags, mode 등 포함)

        Returns:
            플랫폼별 콘텐츠 딕셔너리
        """
        title     = post.get("title", "")
        url       = post.get("url", "")
        summary   = post.get("summary", "")
        full_text = post.get("full_text", "")
        tags      = post.get("tags", [])
        mode      = post.get("mode", "morning")

        tags_str = ", ".join(tags) if tags else "미국증시, 주식"

        prompt = f"""아래 티스토리 블로그 포스팅을 각 SNS 플랫폼에 맞게 재가공해주세요.

[블로그 정보]
제목: {title}
URL: {url}
모드: {mode} (morning=전일 마감 리뷰 / evening=프리마켓 & 이슈)
태그: {tags_str}

[포스팅 요약]
{summary}

[포스팅 전문 (앞부분)]
{full_text[:2000]}

위 내용을 바탕으로 각 SNS 플랫폼에 최적화된 콘텐츠를 JSON 형식으로 생성해주세요.
blog_title과 blog_url, mode 필드도 반드시 포함해주세요.
"""

        logger.info(f"ContentAdapter: Gemini API 호출 중 (모드: {mode})...")
        result = self._call_gemini(prompt)

        # blog_title/blog_url은 Gemini에게 생성을 맡기지 않고 항상 원본 크롤링
        # 값으로 강제 지정합니다. 한글이 포함된 퍼센트 인코딩 URL을 LLM이
        # 텍스트로 재출력하는 과정에서 인코딩 바이트가 미묘하게 손상되는
        # 사례가 있었습니다(예: "앞두고" %EB%91%90 → "앞둠고" %EB%91%A0로
        # 변형되어 실제 게시물과 다른, 존재하지 않는 링크가 만들어짐).
        # setdefault는 Gemini가 이미 값을 채운 경우 그 값을 그대로 쓰기
        # 때문에 이 손상을 막지 못하므로, 항상 덮어써서 원본을 보장합니다.
        result["blog_title"] = title
        result["blog_url"] = url
        result.setdefault("mode", mode)

        # youtube_script가 없으면 tiktok_script로 대체
        if not result.get("youtube_script") and result.get("tiktok_script"):
            result["youtube_script"] = result["tiktok_script"]
        elif not result.get("youtube_script"):
            result["youtube_script"] = [
                {"slide": 1, "title": title[:15], "body": summary[:50]},
                {"slide": 2, "title": "자세한 분석", "body": "블로그에서 확인하세요!"},
            ]

        if not result.get("tiktok_script"):
            result["tiktok_script"] = result.get("youtube_script", [])

        logger.info(
            f"ContentAdapter 완료: "
            f"YouTube {len(result.get('youtube_script', []))}장, "
            f"플랫폼 텍스트 생성됨"
        )
        return result
