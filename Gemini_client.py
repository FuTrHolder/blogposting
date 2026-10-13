"""
gemini_client.py — Gemini 호출 공용 모듈 (무료 티어 기준)

- API 키는 URL이 아니라 x-goog-api-key 헤더로 전송 (로그/예외 메시지에 키 노출 방지)
- 429: 다음 모델로 전환, 전 모델이 429면 30초 대기 후 1회 재순회
- 5xx/네트워크: 짧은 백오프 재시도 후 다음 모델
- MAX_TOKENS / 빈 응답 / JSON 파싱 실패: 다음 시도·다음 모델
- 모델 목록은 환경변수로 교체 가능 (예: GEMINI_MODELS="gemini-3.6-flash,gemini-3.5-flash")
"""

import json
import logging
import os
import re
import time

import requests

logger = logging.getLogger(__name__)

_API = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
_RETRY_SERVER = {500, 502, 503, 504}


class GeminiError(RuntimeError):
    pass


def models_from_env(env_name: str, default: list[str]) -> list[str]:
    raw = os.environ.get(env_name, "").strip()
    if raw:
        models = [m.strip() for m in raw.split(",") if m.strip()]
        if models:
            return models
    return list(default)


def _redact(text: str, key: str) -> str:
    text = str(text)
    return text.replace(key, "***") if key else text


def parse_json_object(raw: str) -> dict:
    text = (raw or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s*```$", "", text).strip()
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        s, e = text.find("{"), text.rfind("}")
        if s < 0 or e <= s:
            raise
        obj = json.loads(text[s:e + 1])
    if not isinstance(obj, dict):
        raise ValueError("Gemini 응답 JSON이 객체가 아닙니다.")
    return obj


def call_gemini_text(
    api_key: str,
    system: str,
    prompt: str,
    models: list[str],
    *,
    temperature: float = 0.7,
    max_output_tokens: int = 8192,
    timeout: int = 90,
    max_retries: int = 2,
    rounds: int = 2,
    validate=None,
) -> str:
    """응답 원문(JSON 문자열)을 반환합니다. validate(raw)가 예외를 던지면 실패로 간주."""
    if not api_key:
        raise GeminiError("GEMINI_API_KEY가 설정되지 않았습니다.")

    payload = {
        "system_instruction": {"parts": [{"text": system}]},
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": temperature,
            "maxOutputTokens": max_output_tokens,
            "responseMimeType": "application/json",
        },
    }
    headers = {"Content-Type": "application/json", "x-goog-api-key": api_key}
    last_error = "시도 없음"

    for round_no in range(1, rounds + 1):
        rate_limited = False

        for model in models:
            url = _API.format(model=model)

            for attempt in range(1, max_retries + 1):

                def _retry_or_next(reason: str) -> bool:
                    """True면 같은 모델 재시도, False면 다음 모델."""
                    nonlocal last_error
                    last_error = f"{model}: {reason}"
                    logger.warning(f"[Gemini] {last_error} (시도 {attempt}/{max_retries})")
                    if attempt < max_retries:
                        time.sleep(min(2 ** attempt, 8))
                        return True
                    return False

                try:
                    resp = requests.post(url, headers=headers, json=payload, timeout=timeout)
                except requests.RequestException as e:
                    if _retry_or_next(f"네트워크 오류 {type(e).__name__}"):
                        continue
                    break

                code = resp.status_code

                if code == 429:
                    rate_limited = True
                    last_error = f"{model}: HTTP 429(한도 초과)"
                    logger.warning(f"[Gemini] {last_error} → 다음 모델로 전환")
                    break
                if code == 401:
                    raise GeminiError("HTTP 401: GEMINI_API_KEY가 유효하지 않습니다.")
                if code in (400, 403, 404):
                    last_error = f"{model}: HTTP {code} {_redact(resp.text[:200], api_key)}"
                    logger.warning(f"[Gemini] {last_error} → 다음 모델로 전환")
                    break
                if code in _RETRY_SERVER:
                    if _retry_or_next(f"HTTP {code}"):
                        continue
                    break
                if code != 200:
                    last_error = f"{model}: HTTP {code}"
                    logger.warning(f"[Gemini] {last_error} → 다음 모델로 전환")
                    break

                try:
                    data = resp.json()
                except ValueError:
                    if _retry_or_next("응답이 JSON이 아님"):
                        continue
                    break

                cands = data.get("candidates") or []
                if not cands:
                    last_error = f"{model}: candidates 없음 ({data.get('promptFeedback')})"
                    logger.warning(f"[Gemini] {last_error} → 다음 모델로 전환")
                    break

                cand = cands[0]
                finish = cand.get("finishReason", "")
                parts = (cand.get("content") or {}).get("parts") or []
                raw = "".join(
                    p.get("text", "")
                    for p in parts
                    if isinstance(p, dict) and not p.get("thought")
                ).strip()

                if finish == "MAX_TOKENS":
                    last_error = f"{model}: MAX_TOKENS로 응답이 잘림"
                    logger.warning(f"[Gemini] {last_error} → 다음 모델로 전환")
                    break
                if not raw:
                    if _retry_or_next(f"빈 응답(finishReason={finish})"):
                        continue
                    break

                if validate is not None:
                    try:
                        validate(raw)
                    except Exception as e:
                        if _retry_or_next(f"응답 검증 실패: {str(e)[:120]}"):
                            continue
                        break

                logger.info(f"[Gemini] 성공: model={model}, {len(raw)}자")
                return raw

        if rate_limited and round_no < rounds:
            logger.warning("[Gemini] 전 모델 한도 초과 가능성 → 30초 대기 후 재순회")
            time.sleep(30)
            continue
        if not rate_limited:
            break

    raise GeminiError(
        f"모든 Gemini 모델 호출 실패 (시도 모델: {', '.join(models)}). "
        f"마지막 오류: {_redact(last_error, api_key)}"
    )


def call_gemini_json(
    api_key: str,
    system: str,
    prompt: str,
    models: list[str],
    *,
    validate=None,
    **kwargs,
) -> dict:
    """JSON 객체로 파싱된 dict를 반환합니다. validate(obj)는 dict를 받아 검증."""

    def _v(raw: str):
        obj = parse_json_object(raw)
        if validate is not None:
            validate(obj)

    raw = call_gemini_text(api_key, system, prompt, models, validate=_v, **kwargs)
    return parse_json_object(raw)