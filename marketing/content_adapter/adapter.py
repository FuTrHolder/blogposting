"""
content_adapter/adapter.py 패치 내용
=====================================
SYSTEM_PROMPT에 thumbnail_copy 필드 추가.
Gemini가 썸네일용 임팩트 카피를 직접 생성 → 정규식 기반 추출보다 품질 향상.
"""

# SYSTEM_PROMPT의 JSON 응답 형식에 아래 필드 추가:
THUMBNAIL_COPY_FIELD = '''  "thumbnail_copy": "썸네일 메인 카피 (8자 이내, 첫 줄)\\n서브 카피 (20자 이내, 둘째 줄)",'''

# 교체 대상 (기존):
OLD = '  "thumbnail_prompt": "SNS 썸네일용 Stable Diffusion 영문 프롬프트"'

# 교체 후:
NEW = '''  "thumbnail_copy": "썸네일 메인 카피 (8자 이내, 첫 줄)\\n서브 카피 (20자 이내, 둘째 줄)",
  "thumbnail_prompt": "SNS 썸네일용 Stable Diffusion 영문 프롬프트"'''

# adapter.py의 SYSTEM_PROMPT 문자열에서 OLD를 NEW로 replace() 하면 됩니다.
# thumbnail_copy 작성 규칙을 SYSTEM_PROMPT에 추가:
THUMB_COPY_RULE = """
thumbnail_copy 작성 규칙:
- 첫 줄: 숫자/수치 또는 핵심 훅 키워드 (8자 이내, 한글 기준)
  예: "-2.3% 급락", "나스닥 반등?", "연준 충격"
- 둘째 줄: 클릭 유도 서브 카피 (20자 이내)
  예: "지금 사야 할까?", "오늘 밤 대응 전략은"
"""
