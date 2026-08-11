"""Gemini 래퍼 테스트 — 실제 API 호출 없이 요청 조립/재시도/파싱을 검증."""
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("GEMINI_API_KEY", "test-key")
os.environ.setdefault("DATA_DIR", "./data")

import httpx  # noqa: E402

from app import config  # noqa: E402
from app.classifier import llm  # noqa: E402

config.GEMINI_API_KEY = "test-key"


class FakeResp:
    def __init__(self, status, payload=None, text=""):
        self.status_code = status
        self._payload = payload or {}
        self.text = text or json.dumps(self._payload)

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(self.status_code)


MODEL_LIST = {"models": [
    {"name": "models/gemini-2.5-flash", "supportedGenerationMethods": ["generateContent"]},
    {"name": "models/gemini-3.6-flash", "supportedGenerationMethods": ["generateContent"]},
    {"name": "models/gemini-3.5-flash-lite", "supportedGenerationMethods": ["generateContent"]},
    {"name": "models/gemini-3.1-flash-image-preview", "supportedGenerationMethods": ["generateContent"]},
    {"name": "models/text-embedding-004", "supportedGenerationMethods": ["embedContent"]},
]}


def _ok(text):
    return FakeResp(200, {"candidates": [{"content": {"parts": [{"text": text}]},
                                          "finishReason": "STOP"}]})


def test_model_resolution_picks_newest_stable_flash(monkeypatch=None):
    llm.resolve_model.cache_clear()
    config.GEMINI_MODEL = "gemini-9-nonexistent"
    httpx.get = lambda *a, **k: FakeResp(200, MODEL_LIST)
    assert llm._gemini_model() == "gemini-3.6-flash"


def test_configured_model_is_kept():
    config.GEMINI_MODEL = "gemini-3.5-flash-lite"
    httpx.get = lambda *a, **k: FakeResp(200, MODEL_LIST)
    assert llm._gemini_model() == "gemini-3.5-flash-lite"


def test_ask_json_parses_and_sends_expected_body():
    llm.resolve_model.cache_clear()
    config.GEMINI_MODEL = "gemini-3.6-flash"
    httpx.get = lambda *a, **k: FakeResp(200, MODEL_LIST)
    seen = {}

    def fake_post(url, params=None, json=None, timeout=None):
        seen["url"] = url
        seen["body"] = json
        return _ok('{"assignments": [{"id": "A1", "category": "dev"}]}')

    httpx.post = fake_post
    out = llm.ask_json("시스템", "프롬프트")
    assert out["assignments"][0]["category"] == "dev"
    assert "gemini-3.6-flash:generateContent" in seen["url"]
    gc = seen["body"]["generationConfig"]
    assert gc["responseMimeType"] == "application/json"
    assert gc["thinkingConfig"]["thinkingLevel"] == "low"
    assert seen["body"]["systemInstruction"]["parts"][0]["text"] == "시스템"


def test_retries_without_thinking_config_when_rejected():
    llm.resolve_model.cache_clear()
    httpx.get = lambda *a, **k: FakeResp(200, MODEL_LIST)
    calls = []

    def fake_post(url, params=None, json=None, timeout=None):
        calls.append(dict(json["generationConfig"]))
        if "thinkingConfig" in json["generationConfig"]:
            return FakeResp(400, text="Unknown name 'thinkingConfig' for this model")
        return _ok('{"ok": true}')

    httpx.post = fake_post
    assert llm.ask_json("s", "p") == {"ok": True}
    assert len(calls) == 2 and "thinkingConfig" not in calls[1]


def test_markdown_fenced_json_is_parsed():
    assert llm._parse_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert llm._parse_json('설명 문장\n{"b": 2}') == {"b": 2}


def test_missing_key_message():
    old = config.GEMINI_API_KEY
    config.GEMINI_API_KEY = ""
    try:
        llm._key()
        raise AssertionError("예외가 나야 함")
    except llm.MissingKey as exc:
        assert "GEMINI_API_KEY" in str(exc)
    finally:
        config.GEMINI_API_KEY = old


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"✓ {name}")
    print("Gemini 래퍼 테스트 통과")
