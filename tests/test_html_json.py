"""HTML 문서에 심겨 온 JSON을 읽는지 확인 (네트워크 불필요).

Threads는 첫 화면(저장됨 목록 상단·글 상세) 데이터를 XHR이 아니라
<script type="application/json"> 안에 넣어 보낸다. 이걸 놓치면 그 글들은
본문이 비어 상세 페이지를 열게 되고, 거기서도 못 건지면 DOM 텍스트
("작성자명 23시간 본문…")로 때우게 된다 — 실제로 그렇게 오염된 적이 있다.
"""
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("DATA_DIR", "./data-test")

from app.collector import scraper  # noqa: E402


def _post(code: str, author: str, text: str, taken_at: int = 1786939176) -> dict:
    return {
        "code": code, "pk": "1", "taken_at": taken_at,
        "user": {"username": author, "full_name": author},
        "caption": {"text": text},
        "image_versions2": {"candidates": [{"url": f"https://x/{code}.jpg", "width": 640}]},
        "text_post_app_info": {"direct_reply_count": 0},
    }


def _html(payloads: list[dict]) -> str:
    blocks = "".join(
        f'<script type="application/json" data-sjs>{json.dumps(p, ensure_ascii=False)}</script>'
        for p in payloads)
    return f"<html><head>{blocks}</head><body><div>본문</div></body></html>"


def test_reads_posts_from_html():
    sink = scraper.JsonSink()
    html = _html([{"require": [["ScheduledServerJS", "handle", [
        {"__bbox": {"result": {"data": {"items": [_post("AAA111", "alice", "첫 글")]}}}}]]]}])
    found = sink.ingest_html(html)
    assert found == 1, found
    assert sink.by_code["AAA111"]["body"] == "첫 글"
    assert sink.by_code["AAA111"]["posted_at"] == 1786939176, "작성시각을 못 얻었다"
    assert sink.by_code["AAA111"]["media"], "이미지를 못 얻었다"
    assert sink.html_blocks == 1
    print("✓ HTML 안의 JSON에서 본문·작성시각·이미지를 얻는다")


def test_reads_thread_groups_from_html():
    """이어서 쓴 글이 thread_items 로 묶여 오면 그대로 보존해야 한다."""
    sink = scraper.JsonSink()
    payload = {"data": {"feed": [{"thread_items": [
        {"post": _post("BBB222", "bob", "본문")},
        {"post": _post("BBB333", "bob", "이어서 쓴 글", taken_at=1786939200)},
    ]}]}}
    sink.ingest_html(_html([payload]))
    assert sink.grouped is True
    assert [i["id"] for i in sink.threads["BBB222"]] == ["BBB222", "BBB333"]
    assert sink.by_code["BBB333"]["body"] == "이어서 쓴 글"
    print("✓ 이어쓴 글 묶음(thread_items)이 보존된다")


def test_ignores_garbage_safely():
    sink = scraper.JsonSink()
    html = ('<script type="application/json">{망가진 json</script>'
            '<script type="application/json">[]</script>'
            '<script>window.x = {"code":"NOTJSONTAG"}</script>'
            '<script type="text/javascript">{"code":"WRONGTYPE"}</script>')
    assert sink.ingest_html(html) == 0
    assert sink.ingest_html("") == 0
    assert sink.ingest_html(None) == 0        # content() 실패 시 대비
    assert sink.by_code == {}
    print("✓ 깨진 JSON·다른 script 태그는 조용히 무시한다")


def test_merges_with_responses_and_resets():
    """HTML과 응답에서 같은 글이 들어오면 정보가 더 많은 쪽을 남긴다."""
    sink = scraper.JsonSink()
    sink.ingest_html(_html([{"items": [_post("CCC444", "kim", "짧은 본문")]}]))
    sink._ingest({"items": [_post("CCC444", "kim", "훨씬 더 긴 본문입니다 " * 5)]})
    assert len(sink.by_code["CCC444"]["body"]) > 20

    sink._ingest({"items": [_post("CCC444", "kim", "짧")]})
    assert len(sink.by_code["CCC444"]["body"]) > 20, "빈약한 정보로 덮어썼다"

    sink.reset()
    assert sink.by_code == {} and sink.html_blocks == 0 and sink.threads == {}
    print("✓ 응답과 합쳐지고, reset 으로 초기화된다")


if __name__ == "__main__":
    test_reads_posts_from_html()
    test_reads_thread_groups_from_html()
    test_ignores_garbage_safely()
    test_merges_with_responses_and_resets()
    print("HTML JSON 수집 테스트 통과")
