"""파서 단위 테스트 — 실제 Threads 응답과 같은 모양의 가짜 페이로드로 검증."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.collector import parser  # noqa: E402


def _post(code, pk, text, taken_at, images=(), reply_to=None, username="mira_dev"):
    node = {
        "pk": pk, "id": f"{pk}_1", "code": code, "taken_at": taken_at,
        "user": {"username": username, "full_name": "미라", "profile_pic_url": "https://x/p.jpg"},
        "caption": {"text": text},
        "like_count": 12,
        "text_post_app_info": {"direct_reply_count": 2},
    }
    if reply_to:
        node["text_post_app_info"]["reply_to_author"] = {"username": reply_to}
    if images:
        node["image_versions2"] = {"candidates": [
            {"url": images[0], "width": 1080, "height": 1080},
            {"url": images[0] + "?small", "width": 320, "height": 320},
        ]}
    return node


PAYLOAD = {
    "data": {"feedback": {"edges": [
        {"node": {"thread_items": [{"post": _post("CabcD1", "111", "본문입니다. 저장한 글.", 1750000000,
                                                  ["https://cdn/img1.jpg"])}]}},
        {"node": {"thread_items": [
            {"post": _post("CabcD2", "222", "이어서 쓰는 두 번째 글", 1750000100, reply_to="mira_dev")},
            {"post": _post("CabcD3", "333", "세 번째 마무리", 1750000200, reply_to="mira_dev")},
            {"post": _post("CzzzZ9", "999", "남이 단 댓글", 1750000300,
                           reply_to="mira_dev", username="someone_else")},
        ]}},
    ]}}
}


def test_walk_and_normalize():
    nodes = parser.walk_posts(PAYLOAD)
    assert len(nodes) == 4
    items = parser.dedupe([parser.normalize(n) for n in nodes])
    assert len(items) == 4
    root = next(i for i in items if i["id"] == "CabcD1")
    assert root["author"] == "mira_dev"
    assert root["url"] == "https://www.threads.com/@mira_dev/post/CabcD1"
    assert root["media"][0]["url"] == "https://cdn/img1.jpg"   # 가장 큰 후보 선택
    assert root["posted_at"] == 1750000000


def test_build_thread_keeps_only_self_replies():
    items = parser.dedupe([parser.normalize(n) for n in parser.walk_posts(PAYLOAD)])
    t = parser.build_thread(items, "CabcD1")
    assert t["body"] == "본문입니다. 저장한 글."
    assert [s["kind"] for s in t["segments"]] == ["root", "reply", "reply"]
    assert "남이 단 댓글" not in t["full_text"]
    assert t["thread_text"].startswith("이어서 쓰는")
    assert "세 번째 마무리" in t["full_text"]


def test_carousel_media():
    node = _post("CarX", "444", "캐러셀", 1750000400)
    node["carousel_media"] = [
        {"image_versions2": {"candidates": [{"url": "https://cdn/a.jpg", "width": 1080}]}},
        {"image_versions2": {"candidates": [{"url": "https://cdn/b.jpg", "width": 1080}]}},
    ]
    m = parser.extract_media(node)
    assert [x["url"] for x in m] == ["https://cdn/a.jpg", "https://cdn/b.jpg"]


def test_dedupe_prefers_richer():
    thin = parser.normalize(_post("Same1", "555", "", 1750000500))
    rich = parser.normalize(_post("Same1", "555", "긴 본문", 1750000500, ["https://cdn/c.jpg"]))
    out = parser.dedupe([thin, rich])
    assert len(out) == 1 and out[0]["body"] == "긴 본문"


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"✓ {name}")
    print("모든 파서 테스트 통과")
