"""OpenAI·Anthropic 경로와 공통 재시도 검증 (실제 API 호출 없음).

세 provider가 같은 재시도 규칙을 공유하는지, 그리고 모델마다 다른
'못 받는 항목'을 스스로 빼고 다시 시도하는지를 본다.
"""
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("DATA_DIR", "./data-test")

import httpx  # noqa: E402

from app import config  # noqa: E402
from app.classifier import llm  # noqa: E402


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


def _chat(text, finish="stop"):
    return FakeResp(200, {"choices": [{"message": {"content": text}, "finish_reason": finish}]})


def _use_openai(model="gpt-5-mini"):
    config.LLM_PROVIDER = "openai"
    config.OPENAI_API_KEY = "test-key"
    config.OPENAI_MODEL = model
    llm._openai_drop.clear()
    llm.resolve_model.cache_clear()


def _restore():
    config.LLM_PROVIDER = "gemini"
    llm.resolve_model.cache_clear()
    llm._openai_drop.clear()


def test_openai_request_shape():
    _use_openai()
    seen = {}

    def fake_post(url, headers=None, json=None, timeout=None):
        seen.update(url=url, headers=headers, body=json)
        return _chat('{"assignments": [{"id": "A1"}]}')

    httpx.post = fake_post
    httpx.get = lambda *a, **k: FakeResp(200, {"data": [{"id": "gpt-5-mini"}]})
    try:
        out = llm.ask_json("시스템", "프롬프트")
        assert out["assignments"][0]["id"] == "A1"
        assert seen["url"] == "https://api.openai.com/v1/chat/completions"
        assert seen["headers"]["Authorization"] == "Bearer test-key"
        b = seen["body"]
        assert b["model"] == "gpt-5-mini"
        assert b["response_format"] == {"type": "json_object"}
        assert b["max_completion_tokens"] == 4000      # 요즘 모델이 쓰는 이름
        assert [m["role"] for m in b["messages"]] == ["system", "user"]
    finally:
        _restore()


def test_openai_drops_params_the_model_rejects():
    """GPT-5 계열은 temperature 를 거부하고, 옛 모델은 max_tokens 를 쓴다."""
    _use_openai()
    bodies = []

    def fake_post(url, headers=None, json=None, timeout=None):
        bodies.append(dict(json))
        if "max_completion_tokens" in json:
            return FakeResp(400, text="Unsupported parameter: 'max_completion_tokens'")
        if "temperature" in json:
            return FakeResp(400, text="Unsupported value: 'temperature' does not support 0.2")
        return _chat('{"ok": true}')

    httpx.post = fake_post
    httpx.get = lambda *a, **k: FakeResp(200, {"data": [{"id": "gpt-5-mini"}]})
    try:
        assert llm.ask_json("s", "p") == {"ok": True}
        assert len(bodies) == 3, bodies
        assert "max_tokens" in bodies[1] and "max_completion_tokens" not in bodies[1]
        assert "temperature" not in bodies[2]
        # 한 번 알아낸 것은 기억해 다음 호출부터 처음부터 빼고 보낸다
        bodies.clear()
        assert llm.ask_json("s", "p") == {"ok": True}
        assert len(bodies) == 1, "같은 400을 또 맞았다"
        assert "temperature" not in bodies[0] and "max_tokens" in bodies[0]
    finally:
        _restore()


def test_openai_retries_server_errors_and_raises_client_errors():
    _use_openai()
    calls = []

    def flaky(url, headers=None, json=None, timeout=None):
        calls.append(1)
        return _chat('{"ok": 1}') if len(calls) > 2 else FakeResp(503, text="overloaded")

    httpx.post = flaky
    httpx.get = lambda *a, **k: FakeResp(200, {"data": [{"id": "gpt-5-mini"}]})
    llm.time.sleep = lambda *_: None            # 테스트에서 실제로 기다리지 않는다
    try:
        assert llm.ask_json("s", "p") == {"ok": 1}
        assert len(calls) == 3

        httpx.post = lambda *a, **k: FakeResp(401, text="invalid api key")
        try:
            llm.ask_json("s", "p")
            raise AssertionError("401 은 재시도 없이 바로 실패해야 한다")
        except RuntimeError as exc:
            assert "401" in str(exc)
    finally:
        _restore()


def test_openai_grows_token_budget_when_cut_off():
    _use_openai()
    bodies = []

    def fake_post(url, headers=None, json=None, timeout=None):
        bodies.append(dict(json))
        if len(bodies) == 1:
            return _chat("", finish="length")
        return _chat('{"ok": true}')

    httpx.post = fake_post
    httpx.get = lambda *a, **k: FakeResp(200, {"data": [{"id": "gpt-5-mini"}]})
    try:
        assert llm.ask_json("s", "p", max_tokens=1000) == {"ok": True}
        assert bodies[1]["max_completion_tokens"] == 2000
    finally:
        _restore()


def test_openai_model_fallback():
    _use_openai(model="gpt-없는모델")
    httpx.get = lambda *a, **k: FakeResp(200, {"data": [
        {"id": "gpt-4.1-mini"}, {"id": "gpt-5-mini"}, {"id": "gpt-4o-audio-preview"},
        {"id": "text-embedding-3-small"}]})
    try:
        assert llm._openai_model() == "gpt-5-mini", llm._openai_model()
    finally:
        _restore()


def test_unknown_provider_says_what_to_do():
    config.LLM_PROVIDER = "그록"
    llm.resolve_model.cache_clear()
    try:
        llm.ask_json("s", "p")
        raise AssertionError("예외가 나야 함")
    except RuntimeError as exc:
        assert "gemini" in str(exc) and "openai" in str(exc), str(exc)
    finally:
        _restore()


def test_key_helpers_follow_provider():
    config.OPENAI_API_KEY, config.GEMINI_API_KEY = "", "g"
    config.LLM_PROVIDER = "openai"
    assert llm.key_env_name() == "OPENAI_API_KEY" and llm.has_key() is False
    config.LLM_PROVIDER = "gemini"
    assert llm.key_env_name() == "GEMINI_API_KEY" and llm.has_key() is True
    _restore()


def test_anthropic_retries_transient_errors():
    """예전에는 Anthropic 경로에 재시도가 없어 배치 15건이 통째로 날아갔다."""
    config.LLM_PROVIDER = "anthropic"
    config.ANTHROPIC_API_KEY = "test-key"
    llm.resolve_model.cache_clear()
    llm.time.sleep = lambda *_: None

    class APIStatusError(Exception):     # SDK 가 529(과부하)를 이렇게 올려준다
        status_code = 529

    class Msg:
        stop_reason = "end_turn"
        content = [type("B", (), {"type": "text", "text": '"ok": true}'})()]

    calls = []

    class FakeMessages:
        def create(self, **body):
            calls.append(body)
            if len(calls) == 1:
                raise APIStatusError("overloaded_error")
            return Msg()

    class FakeClient:
        messages = FakeMessages()

    llm._anthropic_client = lambda: FakeClient()
    try:
        assert llm.ask_json("s", "p") == {"ok": True}
        assert len(calls) == 2, "일시적 오류인데 재시도하지 않았다"
        assert calls[0]["messages"][-1] == {"role": "assistant", "content": "{"}
    finally:
        _restore()


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"✓ {name}")
    print("provider 테스트 통과")
