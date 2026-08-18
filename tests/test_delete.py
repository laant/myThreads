"""로컬 삭제 기능 테스트 (네트워크 불필요).

확인하는 것:
  1. 글 1건을 지우면 본문·이어쓴 글·분류·이미지 행이 모두 사라진다
  2. 내려받은 이미지 파일도 지워진다 (단, 다른 글이 쓰는 파일은 남는다)
  3. media 폴더 밖을 가리키는 경로는 절대 건드리지 않는다
  4. 지운 글은 다음 수집에서 다시 가져오지 않는다 (known_post_ids에 포함)
  5. 복원하면 그 기억이 사라진다
  6. DELETE /api/posts/{id} 가 위 동작을 그대로 수행한다
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("DATA_DIR", "./data-test")

from app import config  # noqa: E402
from app.db import (db, delete_post, deleted_posts, init_db, replace_media,  # noqa: E402
                    replace_segments, restore_deleted, skipped_index, upsert_post)


def _fresh_db() -> None:
    if config.DB_PATH.exists():
        config.DB_PATH.unlink()
    init_db()


def _make_post(pid: str, media: list[dict], segments: int = 2) -> None:
    with db() as conn:
        upsert_post(conn, {"id": pid, "author": "someone", "posted_at": 1700000000,
                           "body": f"{pid} 본문", "full_text": f"{pid} 본문", "detail_ok": 1})
        replace_segments(conn, pid, [{"kind": "root", "text": "본문"}]
                         + [{"kind": "reply", "text": f"이어쓴 글 {i}"}
                            for i in range(segments - 1)])
        replace_media(conn, pid, media)
        conn.execute(
            "INSERT INTO classification(post_id,category_id,summary,tags,method,updated_at) "
            "VALUES(?,1,'요약','[]','ai',1700000000)", (pid,))


def _touch_media(name: str) -> str:
    config.MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    (config.MEDIA_DIR / name).write_bytes(b"fake-image")
    return f"media/{name}"


def _counts(pid: str) -> dict:
    with db() as conn:
        return {t: conn.execute(
            f"SELECT COUNT(*) c FROM {t} WHERE {'id' if t == 'posts' else 'post_id'}=?",
            (pid,)).fetchone()["c"] for t in ("posts", "segments", "media", "classification")}


def test_delete_removes_rows_and_files():
    _fresh_db()
    mine = _touch_media("DEL1_aaa.jpg")
    shared = _touch_media("SHARED_bbb.jpg")
    outside = "../../etc/passwd"                      # 이런 값이 들어와도 무시해야 한다
    _make_post("DEL1", [{"kind": "image", "url": "u1", "local_path": mine},
                        {"kind": "image", "url": "u2", "local_path": shared},
                        {"kind": "image", "url": "u3", "local_path": outside}])
    _make_post("KEEP", [{"kind": "image", "url": "u2", "local_path": shared}])

    res = delete_post("DEL1")
    assert res["ok"] and res["forgotten"], res
    assert _counts("DEL1") == {"posts": 0, "segments": 0, "media": 0, "classification": 0}
    assert _counts("KEEP")["posts"] == 1, "다른 글이 함께 지워졌다"

    assert not (config.MEDIA_DIR / "DEL1_aaa.jpg").exists(), "이미지 파일이 남았다"
    assert (config.MEDIA_DIR / "SHARED_bbb.jpg").exists(), "다른 글이 쓰는 파일까지 지웠다"
    assert res["media_removed"] == 1, res
    print("✓ 행·이미지 파일 삭제 (공유 파일과 media 폴더 밖 경로는 보호)")


def test_delete_is_remembered_and_restorable():
    _fresh_db()
    _make_post("DEL2", [])
    delete_post("DEL2")

    ids, keys = skipped_index()
    assert "DEL2" in ids, "지운 글이 재수집 제외 목록에 없다"
    assert "someone|1700000000" in keys, "작성자+시각 열쇠가 없다"
    assert [r["id"] for r in deleted_posts()] == ["DEL2"]

    assert restore_deleted("DEL2") == 1
    assert "DEL2" not in skipped_index()[0], "복원 후에도 제외돼 있다"
    assert deleted_posts() == []
    print("✓ 재수집 차단 + 복원")


def test_delete_without_forget():
    _fresh_db()
    _make_post("DEL3", [])
    res = delete_post("DEL3", forget=False)
    assert res["ok"] and not res["forgotten"]
    assert "DEL3" not in skipped_index()[0], "forget=False 인데 기억했다"
    assert delete_post("없는글")["ok"] is False
    print("✓ forget=False · 없는 글 처리")


def test_api_delete():
    from fastapi.testclient import TestClient

    from app import main
    _fresh_db()
    path = _touch_media("DEL4_ccc.jpg")
    _make_post("DEL4", [{"kind": "image", "url": "u", "local_path": path}])

    client = TestClient(main.app)
    assert client.get("/api/posts/DEL4").status_code == 200

    r = client.delete("/api/posts/DEL4")
    assert r.status_code == 200 and r.json()["ok"], r.text
    assert r.json()["media_removed"] == 1
    assert client.get("/api/posts/DEL4").status_code == 404
    assert not (config.MEDIA_DIR / "DEL4_ccc.jpg").exists()
    assert client.delete("/api/posts/DEL4").status_code == 404, "두 번째 삭제가 404가 아니다"

    assert client.post("/api/posts/DEL4/restore").json()["restored"] == 1
    assert "DEL4" not in skipped_index()[0]
    print("✓ DELETE /api/posts/{id} · POST /api/posts/{id}/restore")


if __name__ == "__main__":
    test_delete_removes_rows_and_files()
    test_delete_is_remembered_and_restorable()
    test_delete_without_forget()
    test_api_delete()
    print("로컬 삭제 테스트 통과")
