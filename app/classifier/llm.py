"""LLM 래퍼 — 기본값 Google Gemini (필요하면 Anthropic으로 전환 가능).

이 모듈은 '분류' 단계에서만 쓰입니다. 글을 읽어와 저장하는 수집 단계는
LLM을 전혀 사용하지 않습니다.
"""
from __future__ import annotations

import json
import logging
import re
import time
from functools import lru_cache

import httpx

from .. import config

log = logging.getLogger("llm")

_JSON_RE = re.compile(r"(\{.*\}|\[.*\])", re.S)
_VER_RE = re.compile(r"(\d+(?:\.\d+)?)")

GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta"
BAD_MODEL_HINTS = ("embedding", "image", "tts", "vision", "aqa", "live", "native-audio")


class MissingKey(RuntimeError):
    pass


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


def ask_json(system: str, prompt: str, max_tokens: int = 4000, temperature: float = 0.2):
    if config.LLM_PROVIDER == "anthropic":
        return _anthropic_ask(system, prompt, max_tokens, temperature)
    return _gemini_ask(system, prompt, max_tokens, temperature)


@lru_cache(maxsize=1)
def resolve_model() -> str:
    if config.LLM_PROVIDER == "anthropic":
        return _anthropic_model()
    return _gemini_model()


# ── Gemini ──────────────────────────────────────────────────────────────

def _key() -> str:
    if not config.GEMINI_API_KEY:
        raise MissingKey(
            "GEMINI_API_KEY 가 설정되지 않았습니다. .env 를 확인해 주세요. "
            "(발급: https://aistudio.google.com/apikey)"
        )
    return config.GEMINI_API_KEY


def _version_of(name: str) -> float:
    m = _VER_RE.search(name)
    try:
        return float(m.group(1)) if m else 0.0
    except ValueError:
        return 0.0


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
    if not usable:
        return want
    if want in usable:
        return want

    def pick(pred):
        cands = [m for m in usable if pred(m) and not any(b in m for b in BAD_MODEL_HINTS)]
        return sorted(cands, key=lambda m: (_version_of(m), m))[-1] if cands else None

    chosen = (pick(lambda m: "flash" in m and "lite" not in m and "preview" not in m)
              or pick(lambda m: "flash" in m and "lite" not in m)
              or pick(lambda m: "flash" in m)
              or pick(lambda m: "gemini" in m)
              or usable[0])
    log.warning("모델 '%s' 를 찾을 수 없어 '%s' 로 대체합니다.", want, chosen)
    return chosen


def _gemini_ask(system: str, prompt: str, max_tokens: int, temperature: float):
    model = resolve_model()
    url = f"{GEMINI_BASE}/models/{model}:generateContent"
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

    last_err = None
    for attempt in range(4):
        try:
            r = httpx.post(url, params={"key": _key()}, json=body, timeout=180)
        except Exception as exc:
            last_err = exc
            time.sleep(2 * (attempt + 1))
            continue

        if r.status_code == 400 and "thinking" in r.text.lower():
            # 이 모델이 thinkingConfig 를 모르면 빼고 재시도
            body["generationConfig"].pop("thinkingConfig", None)
            continue
        if r.status_code in (429, 500, 502, 503, 504):
            wait = 5 * (attempt + 1)
            log.warning("Gemini %s — %d초 후 재시도", r.status_code, wait)
            time.sleep(wait)
            last_err = RuntimeError(f"HTTP {r.status_code}: {r.text[:200]}")
            continue
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
                body["generationConfig"]["maxOutputTokens"] = min(max_tokens * 2, 32000)
                last_err = RuntimeError("출력 토큰 초과 — 한도를 늘려 재시도")
                continue
            raise RuntimeError(f"Gemini 빈 응답 (finishReason={cand.get('finishReason')})")
        return _parse_json(text)

    raise RuntimeError(f"Gemini 호출 실패: {last_err}")


# ── Anthropic (선택) ────────────────────────────────────────────────────

def _anthropic_client():
    if not config.ANTHROPIC_API_KEY:
        raise MissingKey("ANTHROPIC_API_KEY 가 설정되지 않았습니다.")
    from anthropic import Anthropic  # 선택 의존성
    return Anthropic(api_key=config.ANTHROPIC_API_KEY)


def _anthropic_model() -> str:
    want = config.ANTHROPIC_MODEL
    try:
        available = [m.id for m in _anthropic_client().models.list(limit=100).data]
    except Exception as exc:
        log.warning("모델 목록 조회 실패(%s) — 설정값 그대로 사용: %s", exc, want)
        return want
    if not available or want in available:
        return want
    prefix = [m for m in available if m.startswith(want)]
    if prefix:
        return sorted(prefix)[-1]
    family = want.split("-")[1] if "-" in want else "sonnet"
    same = [m for m in available if family in m]
    chosen = sorted(same)[-1] if same else available[0]
    log.warning("모델 '%s' 를 찾을 수 없어 '%s' 로 대체합니다.", want, chosen)
    return chosen


def _anthropic_ask(system: str, prompt: str, max_tokens: int, temperature: float):
    msg = _anthropic_client().messages.create(
        model=resolve_model(), max_tokens=max_tokens, temperature=temperature,
        system=system,
        messages=[{"role": "user", "content": prompt},
                  {"role": "assistant", "content": "{"}],
    )
    text = "{" + "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
    return _parse_json(text)
