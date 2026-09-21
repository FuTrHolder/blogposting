"""
content_adapter/adapter.py
Gemini API를 사용해 티스토리 블로그 포스팅을 각 SNS 플랫폼에 맞게 재가공합니다.
"""

import logging
import time
import json
import requests

# repo 루트의 fact_reference.py를 가져옵니다. marketing/main_marketing.py가
# 이 모듈(content_adapter.adapter)을 임포트하기 전에 sys.path에 repo
# 루트를 이미 추가해두므로(main_marketing.py 상단 참고) 정상 동작합니다.
import fact_reference

logger = logging.getLogger(__name__)

GEMINI_MODELS = [
    "gemini-3.6-flash",
    "gemini-3.5-flash",
    "gemini-2.5-flash",
]
GEMINI_API_URL_TMPL = (
    "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
)

SYSTEM_PROMPT_INTRO = """당신은 미국 증시 시황 블로그(seedsup.tistory.com)의 SNS 마케팅 전문가입니다.
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
"""

# [신규] 인물 정보는 날짜 정보와 마찬가지로 "사실"이며, Gemini의 사전지식이
# 낡아 있을 수 있는 영역입니다(예: 연준 의장 교체). fact_reference.py의
# CURRENT_OFFICIALS를 그대로 프롬프트에 주입해, 블로그 본문 생성
# (content_generator.py)과 동일한 인물 정보를 SNS 콘텐츠에도 강제합니다.
SYSTEM_PROMPT_OFFICIALS_NOTE = (
    "\n"
    + fact_reference.officials_reference_text()
    + "\n위 인물 정보는 날짜와 마찬가지로 반드시 지켜야 하는 사실입니다. "
    "인물의 이름이나 직책을 언급할 때는 반드시 이 표를 따르고, 학습 시점의 "
    "기억에 의존해 이전 인물 이름을 쓰지 마세요.\n"
)

SYSTEM_PROMPT = (
    SYSTEM_PROMPT_INTRO
    + SYSTEM_PROMPT_OFFICIALS_NOTE
    + """
────────────────────────────────────────
[스레드(Threads)] threads_post 작성 규칙
채널 포지션: "실시간 시장 반응 & 의견(hot-take) 데스크"
────────────────────────────────────────
(2026 지침 근거: 스레드 알고리즘은 "답글을 클릭해서 볼 가능성"을 핵심 신호로
사용하며, 댓글 답글이 달릴수록 노출 수명이 길어지는 대화형 플랫폼)
- 이 채널은 "정보 요약"이 아니라 "그 정보에 대한 반응/의견"을 다룹니다.
  인스타그램이 사실을 친절하게 설명하는 채널이라면, 스레드는 같은 사실을 두고
  "저는 이렇게 봅니다"라고 짧게 관점을 던지는 채널입니다. 인스타그램 문구를
  그대로 줄여쓰지 마세요.
- 첫 문장 = 훅. 오늘 시황에서 가장 핵심적인 수치·반전 포인트를 질문형 또는
  단정형으로 제시 (이모지 없이). 예: "나스닥 2% 급락, 진짜 이유는 실적이 아니었다"
- 훅 다음 문장에는 반드시 "왜 그렇게 보는지"에 대한 짧은 관점/근거를 1개
  포함하세요 (예: "옵션 만기 수급 영향이 더 컸다고 봅니다"). 단순 사실
  재진술로 끝내지 마세요.
- 문장은 짧고 구어체로. 격식체·홍보 문구 지양
- 전체 150자 이내 (Threads 게시물 자체 한도는 500자이지만, 짧고 강한 훅
  유지를 위해 150자를 넘기지 않습니다)
- 반드시 마지막 문장을 "답글을 유도하는 질문"으로 마무리
  (예: "오늘 밤 반등, 가능하다고 보세요?")
- 토픽 태그(해시태그)는 1~3개만, 시황과 직접 관련된 것만 사용
- "좋아요/팔로우 눌러주세요" 같은 노골적 참여 유도 문구는 쓰지 말 것
  (인게이지먼트 베이트로 분류되어 오히려 노출이 줄어듦) — 훅 질문 자체가
  자연스럽게 답글을 유도하도록 할 것

[신규] threads_thread — 답글 체인(스레드) 배열
- threads_post와 별개로, 오늘 내용을 3~4개의 게시물로 나눠 서로 답글로
  이어지는 "진짜 스레드" 형태도 함께 만드세요. 각 항목은 반드시 2~3문장,
  최소 180자 이상 (Threads 게시물 한도는 500자이므로 여유가 충분합니다.
  100자 안팎의 짧은 한 문장으로 끝내지 마세요 — 문장을 늘리기 위해서라도
  구체적인 수치·비교·근거를 최소 1개 이상 포함해 내용을 채우세요.
  순서: ① 훅+관점(threads_post와 유사한 역할, 구체적 수치 1개 포함) → ② 그
  관점을 뒷받침하는 데이터·근거를 1~2개 상세히 설명 → ③ 반대 시나리오나
  리스크 요인을 근거와 함께 설명(선택) → ④ 오늘 흐름을 정리하는 2~3문장
  + 답글 유도 질문. 해시태그는 마지막 항목에만 1~3개 붙이세요.
- 각 항목은 그 자체로 완결된 문단이어야 합니다 ("그리고", "또한"으로 앞
  항목에 의존해 문장이 끊기지 않게 함) — 실제로는 순서대로 답글로 붙지만,
  하나씩 읽어도 어색하지 않아야 합니다.
- threads_post는 이 배열이 어떤 이유로 발행되지 못했을 때를 위한 단일
  게시물 폴백이므로 반드시 함께 채우세요.

────────────────────────────────────────
[인스타그램] instagram_post 작성 규칙
채널 포지션: "시각적 교육 & 발견(Discovery) 채널" — 초보 투자자의 진입 관문
────────────────────────────────────────
(2026 지침 근거: 2025년 12월부터 게시물당 해시태그가 5개로 강제 제한되었고,
저장·공유가 좋아요보다 약 3배 높은 알고리즘 가중치를 가짐)
- 스레드(의견/반응)와 역할을 분리하세요. 이 채널의 핵심은 "쉬운 설명"입니다.
  오늘 등장한 용어·개념(예: CPI, PER, 옵션 만기, Fear & Greed 지수 등) 중
  하나를 골라 "이게 뭔지" 초보자도 이해할 수 있게 풀어주는 문장을 반드시
  1개 포함하세요.
- 구조: 훅(스크롤을 멈추는 한 줄, 이모지 없이 궁금증/단정/수치 중 하나) →
  쉬운 설명 1문장 → 핵심 정보 2~3문장(1~2문장마다 줄바꿈, 오늘 시황의 핵심
  흐름 요약) → CTA
- 전체 200자 이내. 이모지는 본문에만 3~5개 (훅 문장에는 넣지 않음)
- CTA는 "좋아요"가 아니라 "저장"과 "팔로우"를 구체적으로 유도
  (예: "매일 아침·저녁 시황 브리핑, 저장해두고 놓치지 마세요")
- 해시태그는 정확히 5개만 생성: 니치 태그(예: 미국주식초보) 1~2개 +
  토픽 태그(예: 나스닥, 연준금리) 1~2개 + 필요 시 포맷 태그 1개.
  100만 건 이상 초대형 태그(#일상 류)는 쓰지 말 것

[신규] instagram_carousel — 캐러셀 슬라이드 배열
- instagram_post(단일 이미지용 캡션)와 별개로, 오늘 내용을 4~6장의 캐러셀
  슬라이드로도 만드세요. 캐러셀은 스와이프할수록 체류시간이 늘어나 단일
  이미지보다 도달·저장률이 높습니다.
- 슬라이드 구조 공식(반드시 지킬 것 — 슬라이드당 메시지 1개 원칙):
  1번(표지): 훅 문구만 (headline 12자 이내, body는 비워둠)
  2번: 오늘 상황이 왜 중요한지 문제 제기 (headline 8자 이내 키워드,
       body 20자 이내 설명)
  3~N-1번: 핵심 내용 1개씩 (headline 8자 이내 키워드, body 20~30자 설명)
  N번(마지막): 오늘 핵심 한 줄 요약 (headline 8자 이내, body는 정리 문장)
- headline은 화면에 크게 들어갈 짧은 키워드/문구, body는 그 아래 부연설명
  1문장입니다. body를 2문장 이상으로 늘리지 마세요.
- 캡션(instagram_post)의 훅과 1번 슬라이드의 headline은 같은 사건을
  다루되 문장을 똑같이 복붙하지 말고 자연스럽게 어울리게 작성하세요.

────────────────────────────────────────
[유튜브 쇼츠] youtube_script / tiktok_script 작성 규칙
채널 포지션: "교육적 권위 & 장기 유입(SEO) 채널" — 검색으로 찾아오는
시청자에게도 통해야 함
────────────────────────────────────────
(2026 지침 근거: 스와이프 가능한 피드에서는 첫 1초 안에 이탈 여부가 정해짐.
"안녕하세요" 같은 인사말·필러로 시작하면 즉시 이탈 위험이 큼)
- 총 5~7장면
- 1장면: "결과를 먼저 보여주는" 훅. 인사말·필러 절대 금지.
  수치/반전/궁금증 유발 중 하나의 방식 사용 (title 15자 이내). 가능하면
  "CPI", "FOMC", "나스닥"처럼 검색으로도 찾아질 핵심 키워드를 title에
  자연스럽게 포함하세요 (틱톡용 훅보다 조금 더 정보 지향적으로).
- 2~마지막-1장면: 핵심 정보를 장면당 1개 메시지로 전달 (body 50자 이내).
  결과만이 아니라 "왜 그런지" 짧은 근거를 곁들이세요 — 이 채널은 틱톡보다
  신뢰감·전문성이 더 중요합니다.
- 마지막 장면: 오늘 분석 요약 + 블로그 방문 CTA. 가능하면
  "매일 아침 9시·저녁 9시 업데이트, 구독하고 알림 설정해두세요" 같은
  시리즈 CTA를 포함
- 이 필드는 실제 영상 나래이션 생성이 실패했을 때의 대체용으로도 쓰이므로,
  블로그 본문 없이 이 슬라이드만 보아도 내용이 이해되도록 작성

────────────────────────────────────────
[페이스북] facebook_post 작성 규칙
채널 포지션: "커뮤니티 & 심화 분석 채널" — 중장년층·진지한 투자자 대상
────────────────────────────────────────
- 스레드·인스타그램보다 훨씬 차분하고 깊이 있는 톤을 사용하세요. 짧은 훅보다
  "무슨 일이 있었고 왜 중요한지"를 근거와 함께 차근차근 짚어주는 미니 칼럼
  형태로 작성합니다.
- 분량: 600~900자 (스레드·인스타그램보다 훨씬 길게 — Facebook은 글자수
  제한이 매우 넉넉하므로 억지로 줄이지 마세요)
- 구조: 오늘 상황 요약 1~2문장 → 배경·근거 설명 2~3문장 → 앞으로 주목할 점
  1~2문장 → 커뮤니티 토론을 유도하는 질문 1개로 마무리
  (예: "이번 조정을 매수 기회로 보시나요, 추가 하락 신호로 보시나요?")
- 이모지는 최소화(0~2개), 신뢰감 있는 문어체 사용
- 해시태그 3~5개. 스레드/인스타그램과 겹치지 않는 신뢰형 태그 위주
  (예: 미국주식분석, 나스닥전망)

────────────────────────────────────────
[카카오스토리채널] kakao_post 작성 규칙
────────────────────────────────────────
kakao_post는 친근한 안내문 톤(정보 전달 중심, 과도한 홍보 지양)을 유지하되
위 [원칙 A], [원칙 B]를 동일하게 적용하세요. 200자 이내.

────────────────────────────────────────
[틱톡] tiktok_post 작성 규칙 (영상 캡션 — 영상 나래이션과는 별개 필드)
채널 포지션: "바이럴 교육 & 빠른 발견 채널" — 팔로워 없이도 알고리즘이
밀어주는 채널. 캐주얼하고 에너지 있는 톤
────────────────────────────────────────
- 이 필드는 별도 파이프라인이 만드는 틱톡 영상에 붙는 캡션입니다. 영상
  나래이션과 같은 소재를 다루되, 캡션 자체는 짧고 경쾌하게 씁니다.
- 100자 이내. 이모지 1~2개 허용 (다른 채널보다 캐주얼한 톤)
- "궁금하면 영상 끝까지 보세요" 류의 완주 유도 문구를 1개 포함
- 마지막 줄에 해시태그 4~6개 (예: #미국주식 #나스닥 #재테크 #주식초보)

반드시 아래 JSON 형식으로만 응답하세요 (마크다운 코드블록 없이 순수 JSON만):
{
  "blog_title": "원본 블로그 제목",
  "blog_url": "원본 블로그 URL",
  "mode": "morning 또는 evening",
  "youtube_script": [
    {
      "slide": 1,
      "title": "훅 제목 (15자 이내, 인사말 금지, 가능하면 검색 키워드 포함)",
      "body": "슬라이드 본문 (50자 이내)"
    }
  ],
  "facebook_post": "페이스북 게시글 (커뮤니티·심화 분석 톤, 600~900자, 토론 유도 질문 포함, 해시태그 3~5개)",
  "instagram_post": "인스타그램 게시글 (훅→쉬운 설명→정보→CTA 구조, 200자 이내, 해시태그 정확히 5개)",
  "instagram_carousel": [
    {"slide": 1, "headline": "표지 훅 문구 (12자 이내)", "body": ""},
    {"slide": 2, "headline": "키워드 (8자 이내)", "body": "부연설명 (20자 이내)"}
  ],
  "threads_post": "쓰레드 게시글 (훅+짧은 관점/근거로 시작해 답글 유도 질문으로 마무리, 150자 이내, 토픽 태그 1~3개)",
  "threads_thread": [
    "① 훅+관점, 구체적 수치 1개 포함 (2~3문장, 180자 이상)",
    "② 근거/데이터 1~2개를 상세히 (2~3문장, 180자 이상)",
    "③ 리스크·반대 시나리오 (선택, 2~3문장, 180자 이상)",
    "④ 정리 + 답글 유도 질문 + 해시태그 1~3개 (2~3문장, 180자 이상)"
  ],
  "x_post": "X 트윗 (280자 이내, 핵심만) — 현재 자동 발행에는 쓰이지 않지만 하위 호환을 위해 유지",
  "kakao_post": "카카오 스토리 게시글 (친근한 어투, 200자 이내)",
  "tiktok_post": "틱톡 영상 캡션 (100자 이내, 완주 유도 문구 + 해시태그 4~6개)",
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
)


class ContentAdapter:
    def __init__(self, api_key: str):
        self.api_key = api_key

    def _call_gemini(self, prompt: str, max_retries: int = 3) -> dict:
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

        last_error = None

        for model in GEMINI_MODELS:
            url = f"{GEMINI_API_URL_TMPL.format(model=model)}?key={self.api_key}"

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
                    last_error = e

                    if status in RETRYABLE_STATUS:
                        # 지수 백오프: 429는 30s 기준, 나머지는 10s 기준
                        base = 30 if status == 429 else 10
                        wait = base * (2 ** (attempt - 1))  # 10s → 20s → 40s
                        if attempt < max_retries:
                            logger.warning(
                                f"Gemini API {status} 오류 (모델: {model}, "
                                f"시도 {attempt}/{max_retries}). {wait}초 후 재시도..."
                            )
                            time.sleep(wait)
                        else:
                            logger.warning(
                                f"모델 {model} 재시도 {max_retries}회 모두 실패({status}) "
                                "→ 다음 모델로 폴백"
                            )
                    elif status == 404:
                        # 해당 모델 자체를 사용할 수 없는 경우 — 같은 모델 반복 호출 없이
                        # 즉시 다음 모델로 이동
                        logger.error(f"Gemini 모델을 찾을 수 없음 (모델: {model}): {e}")
                        break
                    else:
                        # 400, 401, 403 등 재시도 불가 오류는 즉시 실패
                        logger.error(f"Gemini API 오류 ({status}, 모델: {model}): {e}")
                        raise

                except (requests.exceptions.ConnectionError,
                        requests.exceptions.Timeout) as e:
                    last_error = e
                    wait = 10 * (2 ** (attempt - 1))
                    if attempt < max_retries:
                        logger.warning(
                            f"Gemini 네트워크 오류 (모델: {model}, "
                            f"시도 {attempt}/{max_retries}). {wait}초 후 재시도... ({e})"
                        )
                        time.sleep(wait)
                    else:
                        logger.warning(f"모델 {model} 네트워크 오류로 전체 실패 → 다음 모델로 폴백")

                except Exception as e:
                    last_error = e
                    logger.warning(
                        f"Gemini 호출 실패 (모델: {model}, 시도 {attempt}/{max_retries}): {e}"
                    )
                    if attempt < max_retries:
                        time.sleep(10)

            logger.warning(f"모델 {model} 모든 시도 실패 → 다음 모델로 폴백")

        raise RuntimeError(
            "모든 Gemini 모델 호출 실패. "
            f"시도 모델: {', '.join(GEMINI_MODELS)}. 마지막 오류: {last_error}"
        )

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

        # tiktok_post 누락 시 안전한 폴백 (x_post → 제목 순, main_marketing.py의
        # 기존 fallback 체인과 별개로 여기서도 한 번 더 방어)
        if not result.get("tiktok_post"):
            result["tiktok_post"] = (
                result.get("x_post", "")[:100] or title[:80]
            )

        # threads_thread 방어: Gemini가 배열이 아닌 값을 주거나 문자열이 아닌
        # 항목을 섞어 반환해도 파이프라인이 죽지 않게 정리합니다. 여기서
        # 정상적인 배열(문자열 2개 이상)이 아니면 비워서, ThreadsPublisher가
        # 자동으로 threads_post 단일 게시물 방식으로 폴백하도록 둡니다.
        raw_thread = result.get("threads_thread")
        if isinstance(raw_thread, list):
            cleaned_thread = [str(t).strip() for t in raw_thread if str(t).strip()]
        else:
            cleaned_thread = []
        result["threads_thread"] = cleaned_thread if len(cleaned_thread) >= 2 else []

        # instagram_carousel 방어: 항목 형식이 어긋나거나 개수가 부족하면
        # 비워서 SNSThumbnailGenerator/InstagramPublisher가 자동으로 기존
        # 단일 이미지 방식으로 폴백하도록 둡니다.
        raw_carousel = result.get("instagram_carousel")
        cleaned_carousel = []
        if isinstance(raw_carousel, list):
            for i, item in enumerate(raw_carousel, start=1):
                if isinstance(item, dict) and (item.get("headline") or item.get("body")):
                    cleaned_carousel.append({
                        "slide": item.get("slide", i),
                        "headline": str(item.get("headline", ""))[:40],
                        "body": str(item.get("body", ""))[:120],
                    })
        result["instagram_carousel"] = cleaned_carousel if len(cleaned_carousel) >= 2 else []

        # [신규] 최종 안전망: SNS용으로 새로 생성된 모든 텍스트 필드에서
        # 이미 교체된 인물의 이름이 남아있으면 자동으로 현재 인물 이름으로
        # 교정합니다. blog_title/blog_url/mode는 원본 크롤링 값을 그대로
        # 쓰는 필드라(이미 블로그 생성 단계에서 검증됨) 대상에서 제외합니다.
        official_fixes: list[dict] = []

        def _scrub(value):
            if isinstance(value, str):
                fixed, applied = fact_reference.scrub_outdated_officials(value)
                official_fixes.extend(applied)
                return fixed
            if isinstance(value, list):
                return [_scrub(v) for v in value]
            if isinstance(value, dict):
                return {k: _scrub(v) for k, v in value.items()}
            return value

        for key in result:
            if key in ("blog_title", "blog_url", "mode"):
                continue
            result[key] = _scrub(result[key])

        if official_fixes:
            for fix in official_fixes:
                logger.warning(fact_reference.format_official_fix_log(fix))

        logger.info(
            f"ContentAdapter 완료: "
            f"YouTube {len(result.get('youtube_script', []))}장, "
            f"플랫폼 텍스트 생성됨"
        )
        return result