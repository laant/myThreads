"""LLM 래퍼 — Gemini(기본) · OpenAI · Anthropic.

이 모듈은 '분류' 단계에서만 쓰입니다. 글을 읽어와 저장하는 수집 단계는
LLM을 전혀 사용하지 않습니다.

바깥에서 보는 것은 ask_json() 하나뿐입니다. 어느 회사 모델을 쓰는지는
.env 의 LLM_PROVIDER 한 줄로 정해지고, 세 경로 모두 같은 재시도 규칙
(_ask_with_retry)을 공유합니다 — 일시적인 오류는 쉬었다 다시,
'이 항목은 못 알아듣는다'는 응답은 그 항목을 빼고 다시.
"""
from __future__ import annotations

import json
import logging
import re
import time
from functools import lru_cache
from typing import Any, Callable

import httpx

from .. import config

log = logging.getLogger("llm")

_JSON_RE = re.compile(r"(\{.*\}|\[.*\])", re.S)
_VER_RE = re.compile(r"(\d+(?:\.\d+)?)")

GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta"
OPENAI_BASE = "https://api.openai.com/v1"

# 분류에 쓸 수 없는 모델을 이름으로 걸러낸다 (임베딩·음성·이미지 전용 등)
BAD_MODEL_HINTS = ("embedding", "image", "tts", "vision", "aqa", "live", "native-audio",
                   "audio", "realtime", "transcribe", "moderation", "whisper", "dall-e")

# 429(요청 과다) · 5xx(서버 문제) · 529(Anthropic 과부하)는 기다렸다 다시
RETRY_STATUS = {408, 409, 425, 429, 500, 502, 503, 504, 529}
MAX_TRIES = 4
MAX_OUTPUT_CAP = 32000        # 출력 토큰 한도를 늘릴 때의 상한
TIMEOUT_SEC = 180


class MissingKey(RuntimeError):
    pass


class _Retry(Exception):
    """이 응답으로는 못 끝내지만 다시 해볼 만하다.

    요청 본문을 이미 고쳐 두었을 수도 있다(예: 못 알아듣는 항목을 뺐다).
    """

    def __init__(self, why: str, wait: float = 0) -> None:
        super().__init__(why)
        self.why, self.wait = why, wait


# ── 공통 ────────────────────────────────────────────────────────────────

def _parse_json(text: str):
    text = text.strip()
    if text.startswith("```"):                       # ```json … ``` 제거
        text = re.sub(r"^```[a-zA-Z]*\n?|\n?```$", "", text).strip()
    try:
        return json.loads(text)
    except Exception:
        pass
    m = _JSON_RE.search(text)
    if m:
        try:
            return json.loads(m.group(1))
        except Exception as exc:
            raise ValueError(f"JSON 파싱 실패: {exc}\n{text[:500]}") from exc
    raise ValueError(f"JSON 응답이 아닙니다: {text[:500]}")


def _need(env_name: str, value: str, hint: str = "") -> str:
    if not value:
        raise MissingKey(f"{env_name} 가 설정되지 않았습니다. .env 를 확인해 주세요.{hint}")
    return value


def _version_of(name: str) -> float:
    m = _VER_RE.search(name)
    try:
        return float(m.group(1)) if m else 0.0
    except ValueError:
        return 0.0


def _transient(exc: Exception) -> bool:
    """다시 시도해 볼 만한 오류인가 (일시적인 통신·서버 문제)."""
    status = getattr(exc, "status_code", None)
    if status is None:
        status = getattr(getattr(exc, "response", None), "status_code", None)
    if status in RETRY_STATUS:
        return True
    if isinstance(exc, (httpx.HTTPError, TimeoutError, ConnectionError)):
        return True
    # SDK 예외는 httpx 예외를 상속하지 않으므로 이름으로도 본다
    return type(exc).__name__ in {
        "APIConnectionError", "APITimeoutError", "RateLimitError",
        "InternalServerError", "ServiceUnavailableError", "OverloadedError",
    }


def _ask_with_retry(name: str, once: Callable[[], Any], tries: int = MAX_TRIES):
    """provider별 '한 번 호출'을 감싸 재시도를 준다 (세 경로가 같이 쓴다)."""
    last: Exception | None = None
    for attempt in range(tries):
        try:
            return once()
        except MissingKey:
            raise                                     # 키가 없으면 재시도해도 소용없다
        except _Retry as again:
            last = RuntimeError(again.why)
            if again.wait:
                wait = again.wait * (attempt + 1)     # 반복될수록 더 기다린다
                log.warning("%s: %s — %.0f초 후 재시도", name, again.why, wait)
                time.sleep(wait)
            else:
                log.info("%s: %s", name, again.why)
        except Exception as exc:
            if not _transient(exc):
                raise
            last = exc
            wait = 2 * (attempt + 1)
            log.warning("%s 통신 오류(%s) — %d초 후 재시도", name, str(exc)[:120], wait)
            time.sleep(wait)
    raise RuntimeError(f"{name} 호출 실패: {last}")


def _pick_newest(names: list[str], keep: Callable[[str], bool]) -> str | None:
    """조건에 맞는 것 중 버전이 가장 높은 모델."""
    cands = [m for m in names if keep(m) and not any(b in m for b in BAD_MODEL_HINTS)]
    return sorted(cands, key=lambda m: (_version_of(m), m))[-1] if cands else None


# ── Gemini ──────────────────────────────────────────────────────────────

def _key() -> str:
    return _need("GEMINI_API_KEY", config.GEMINI_API_KEY,
                 " (발급: https://aistudio.google.com/apikey)")


def _gemini_model() -> str:
    """설정한 모델이 없으면 사용 가능한 최신 flash 계열로 자동 대체."""
    want = config.GEMINI_MODEL
    try:
        r = httpx.get(f"{GEMINI_BASE}/models", params={"key": _key(), "pageSize": 200}, timeout=20)
        r.raise_for_status()
        models = r.json().get("models", [])
    except MissingKey:
        raise
    except Exception as exc:
        log.warning("모델 목록 조회 실패(%s) — 설정값 그대로 사용: %s", exc, want)
        return want

    usable = [
        m["name"].split("/")[-1] for m in models
        if "generateContent" in (m.get("supportedGenerationMethods") or [])
    ]
    if not usable or want in usable:
        return want

    chosen = (_pick_newest(usable, lambda m: "flash" in m and "lite" not in m and "preview" not in m)
              or _pick_newest(usable, lambda m: "flash" in m and "lite" not in m)
              or _pick_newest(usable, lambda m: "flash" in m)
              or _pick_newest(usable, lambda m: "gemini" in m)
              or usable[0])
    log.warning("모델 '%s' 를 찾을 수 없어 '%s' 로 대체합니다.", want, chosen)
    return chosen


def _gemini_ask(system: str, prompt: str, max_tokens: int, temperature: float):
    url = f"{GEMINI_BASE}/models/{resolve_model()}:generateContent"
    body = {
        "systemInstruction": {"parts": [{"text": system}]},
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": temperature,
            "maxOutputTokens": max_tokens,
            "responseMimeType": "application/json",
            # 분류는 긴 추론이 필요 없다 — 속도·비용을 위해 최소화
            "thinkingConfig": {"thinkingLevel": config.GEMINI_THINKING},
        },
    }

    def once():
        r = httpx.post(url, params={"key": _key()}, json=body, timeout=TIMEOUT_SEC)
        if r.status_code == 400 and "thinking" in r.text.lower():
            if body["generationConfig"].pop("thinkingConfig", None) is not None:
                raise _Retry("이 모델은 thinkingConfig 를 모릅니다 — 빼고 다시 시도")
        if r.status_code in RETRY_STATUS:
            raise _Retry(f"HTTP {r.status_code}", wait=5)
        if r.status_code >= 400:
            raise RuntimeError(f"Gemini 오류 {r.status_code}: {r.text[:400]}")

        data = r.json()
        cands = data.get("candidates") or []
        if not cands:
            fb = (data.get("promptFeedback") or {}).get("blockReason")
            raise RuntimeError(f"Gemini 응답 없음 (blockReason={fb})")
        cand = cands[0]
        text = "".join(p.get("text", "") for p in (cand.get("content") or {}).get("parts", []))
        if not text.strip():
            if cand.get("finishReason") == "MAX_TOKENS":
                gc = body["generationConfig"]
                gc["maxOutputTokens"] = min(gc["maxOutputTokens"] * 2, MAX_OUTPUT_CAP)
                raise _Retry("출력 토큰 초과 — 한도를 늘려 다시 시도")
            raise RuntimeError(f"Gemini 빈 응답 (finishReason={cand.get('finishReason')})")
        return _parse_json(text)

    return _ask_with_retry("Gemini", once)


# ── OpenAI ──────────────────────────────────────────────────────────────
# 모델마다 받는 항목이 다르다 (예: GPT-5 계열은 temperature 를 기본값 외에는 거부,
# 최신 모델은 max_tokens 대신 max_completion_tokens). 처음 한 번 400을 받으면
# 무엇을 빼야 하는지 기억해 두고, 다음 호출부터는 아예 보내지 않는다.
_openai_drop: set[str] = set()


def _openai_key() -> str:
    return _need("OPENAI_API_KEY", config.OPENAI_API_KEY,
                 " (발급: https://platform.openai.com/api-keys)")


def _openai_headers() -> dict:
    return {"Authorization": f"Bearer {_openai_key()}"}


def _openai_model() -> str:
    want = config.OPENAI_MODEL
    try:
        r = httpx.get(f"{OPENAI_BASE}/models", headers=_openai_headers(), timeout=20)
        r.raise_for_status()
        available = [m.get("id", "") for m in r.json().get("data", []) if m.get("id")]
    except MissingKey:
        raise
    except Exception as exc:
        log.warning("모델 목록 조회 실패(%s) — 설정값 그대로 사용: %s", exc, want)
        return want
    if not available or (want and want in available):
        return want or available[0]

    prefix = sorted(m for m in available if want and m.startswith(want))
    chosen = (prefix[-1] if prefix
              else _pick_newest(available, lambda m: m.startswith("gpt-"))
              or available[0])
    log.warning("모델 '%s' 를 찾을 수 없어 '%s' 로 대체합니다.", want, chosen)
    return chosen


def _openai_ask(system: str, prompt: str, max_tokens: int, temperature: float):
    body: dict = {
        "model": resolve_model(),
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": prompt}],
    }
    if "response_format" not in _openai_drop:
        body["response_format"] = {"type": "json_object"}     # JSON만 받도록
    if "temperature" not in _openai_drop:
        body["temperature"] = temperature
    body["max_tokens" if "max_completion_tokens" in _openai_drop
         else "max_completion_tokens"] = max_tokens

    def adapt(text: str) -> str | None:
        """이 모델이 못 받는 항목을 빼거나 바꾼다 (기억해 두고 다음부터 안 보낸다)."""
        low = text.lower()
        if "max_completion_tokens" in body and "max_completion_tokens" in low:
            body["max_tokens"] = body.pop("max_completion_tokens")
            _openai_drop.add("max_completion_tokens")
            return "이 모델은 max_tokens 를 씁니다 — 바꿔서 다시 시도"
        if "temperature" in body and "temperature" in low:
            body.pop("temperature")
            _openai_drop.add("temperature")
            return "이 모델은 temperature 를 받지 않습니다 — 빼고 다시 시도"
        if "response_format" in body and "response_format" in low:
            body.pop("response_format")
            _openai_drop.add("response_format")
            return "이 모델은 response_format 을 받지 않습니다 — 빼고 다시 시도"
        return None

    def once():
        r = httpx.post(f"{OPENAI_BASE}/chat/completions", headers=_openai_headers(),
                       json=body, timeout=TIMEOUT_SEC)
        if r.status_code == 400:
            fixed = adapt(r.text)
            if fixed:
                raise _Retry(fixed)
        if r.status_code in RETRY_STATUS:
            raise _Retry(f"HTTP {r.status_code}", wait=5)
        if r.status_code >= 400:
            raise RuntimeError(f"OpenAI 오류 {r.status_code}: {r.text[:400]}")

        choices = r.json().get("choices") or []
        if not choices:
            raise RuntimeError("OpenAI 응답에 결과가 없습니다.")
        choice = choices[0]
        text = (choice.get("message") or {}).get("content") or ""
        if not text.strip():
            if choice.get("finish_reason") == "length":
                field = "max_tokens" if "max_tokens" in body else "max_completion_tokens"
                body[field] = min(body[field] * 2, MAX_OUTPUT_CAP)
                raise _Retry("출력 토큰 초과 — 한도를 늘려 다시 시도")
            raise RuntimeError(f"OpenAI 빈 응답 (finish_reason={choice.get('finish_reason')})")
        return _parse_json(text)

    return _ask_with_retry("OpenAI", once)


# ── Anthropic ───────────────────────────────────────────────────────────

def _anthropic_client():
    _need("ANTHROPIC_API_KEY", config.ANTHROPIC_API_KEY)
    from anthropic import Anthropic  # 선택 의존성
    return Anthropic(api_key=config.ANTHROPIC_API_KEY)


def _anthropic_model() -> str:
    want = config.ANTHROPIC_MODEL
    try:
        available = [m.id for m in _anthropic_client().models.list(limit=100).data]
    except MissingKey:
        raise
    except Exception as exc:
        log.warning("모델 목록 조회 실패(%s) — 설정값 그대로 사용: %s", exc, want)
        return want
    if not available or want in available:
        return want
    prefix = sorted(m for m in available if m.startswith(want))
    if prefix:
        return prefix[-1]
    family = want.split("-")[1] if "-" in want else "sonnet"
    same = sorted(m for m in available if family in m)
    chosen = same[-1] if same else available[0]
    log.warning("모델 '%s' 를 찾을 수 없어 '%s' 로 대체합니다.", want, chosen)
    return chosen


def _anthropic_ask(system: str, prompt: str, max_tokens: int, temperature: float):
    body = {
        "model": resolve_model(),
        "max_tokens": max_tokens,
        "temperature": temperature,
        "system": system,
        # 여는 중괄호를 미리 넣어 JSON으로만 답하게 유도한다
        "messages": [{"role": "user", "content": prompt},
                     {"role": "assistant", "content": "{"}],
    }

    def once():
        msg = _anthropic_client().messages.create(**body)
        text = "{" + "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
        if getattr(msg, "stop_reason", "") == "max_tokens":
            body["max_tokens"] = min(body["max_tokens"] * 2, MAX_OUTPUT_CAP)
            raise _Retry("출력 토큰 초과 — 한도를 늘려 다시 시도")
        return _parse_json(text)

    return _ask_with_retry("Anthropic", once)


# ── provider 고르기 ─────────────────────────────────────────────────────

PROVIDERS: dict[str, dict] = {
    "gemini": {"ask": _gemini_ask, "model": _gemini_model,
               "env": "GEMINI_API_KEY", "key": lambda: config.GEMINI_API_KEY},
    "openai": {"ask": _openai_ask, "model": _openai_model,
               "env": "OPENAI_API_KEY", "key": lambda: config.OPENAI_API_KEY},
    "anthropic": {"ask": _anthropic_ask, "model": _anthropic_model,
                  "env": "ANTHROPIC_API_KEY", "key": lambda: config.ANTHROPIC_API_KEY},
}


def _provider() -> dict:
    p = PROVIDERS.get(config.LLM_PROVIDER)
    if p is None:
        raise RuntimeError(
            f"모르는 LLM_PROVIDER: '{config.LLM_PROVIDER}' — "
            f".env 에 {' | '.join(PROVIDERS)} 중 하나를 적어 주세요.")
    return p


def ask_json(system: str, prompt: str, max_tokens: int = 4000, temperature: float = 0.2):
    return _provider()["ask"](system, prompt, max_tokens, temperature)


@lru_cache(maxsize=1)
def resolve_model() -> str:
    return _provider()["model"]()


def key_env_name() -> str:
    """지금 provider가 필요로 하는 환경변수 이름 (화면 안내용)."""
    p = PROVIDERS.get(config.LLM_PROVIDER)
    return p["env"] if p else "LLM_PROVIDER"


def has_key() -> bool:
    p = PROVIDERS.get(config.LLM_PROVIDER)
    return bool(p and p["key"]())
