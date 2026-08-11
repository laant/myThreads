"""중간 저장 · 이어받기 E2E 테스트.

수백 건을 한 번에 돌리다 중간에 끊기면 예전 코드는 '전부' 날아갔다(마지막에
한꺼번에 저장했기 때문). 이제는 한 건씩 즉시 저장하고, 다음 실행에서 남은 것만
이어받아야 한다. 그 동작을 실제 브라우저 + 가짜 Threads 서버로 검증한다.
"""
import asyncio
import json
import os
import sys
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
os.environ.setdefault("DATA_DIR", "./data-test")

import test_scroll_e2e as base  # noqa: E402

from app import config  # noqa: E402
from app import pipeline  # noqa: E402
from app.collector import scraper  # noqa: E402
from app.db import db, init_db  # noqa: E402

STOP_AFTER = 20

# 이 시나리오의 글들은 전부 '작성자가 이어서 쓴 댓글'을 가지고 있다 →
# 목록 응답의 댓글 수도 그에 맞춰야 상세 페이지를 연다 (실제 Threads와 동일한 조건)
_orig_make_post = base.make_post


def make_post_with_replies(idx: int) -> dict:
    p = _orig_make_post(idx)
    p["text_post_app_info"]["direct_reply_count"] = 3
    return p


def thread_payload(code: str) -> dict:
    """루트 + 작성자가 이어 쓴 댓글 2개 + 남의 댓글 1개."""
    idx = int(code.replace("POST", ""))
    root = base.make_post(idx)
    author = root["user"]["username"]

    def reply(n, user, text):
        p = base.make_post(idx * 100 + n)
        p["code"] = f"{code}R{n}"
        p["user"]["username"] = user
        p["caption"] = {"text": text}
        p["taken_at"] = root["taken_at"] + n
        p["text_post_app_info"]["reply_to_author"] = {"username": author}
        p.pop("image_versions2", None)
        return p

    return {"data": {"thread": {"items": [
        {"post": root},
        {"post": reply(1, author, f"이어서 쓴 글 {idx}-1")},
        {"post": reply(2, author, f"이어서 쓴 글 {idx}-2")},
        {"post": reply(3, "stranger", "남이 단 댓글")},
    ]}}}


POST_HTML = """<!doctype html><meta charset="utf-8"><body><div id="t">loading</div>
<script>
fetch('/api/graphql/post?code=CODE').then(r => r.json()).then(d => {
  document.getElementById('t').textContent = 'ok';
});
</script></body>"""


class Handler(base.Handler):
    def do_GET(self):
        if self.path.startswith("/api/graphql/post"):
            code = self.path.split("code=")[-1]
            body = json.dumps(thread_payload(code)).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if "/post/" in self.path:
            code = self.path.split("/post/")[-1]
            body = POST_HTML.replace("CODE", code).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        super().do_GET()


def db_state() -> tuple[int, int]:
    with db() as conn:
        total = conn.execute("SELECT COUNT(*) c FROM posts").fetchone()["c"]
        done = conn.execute("SELECT COUNT(*) c FROM posts WHERE detail_ok=1").fetchone()["c"]
    return total, done


def main() -> int:
    base.make_post = make_post_with_replies
    srv = ThreadingHTTPServer(("127.0.0.1", 8126), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    # 깨끗한 테스트 DB
    if config.DB_PATH.exists():
        config.DB_PATH.unlink()
    init_db()

    config.STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    config.STATE_PATH.write_text(json.dumps({"cookies": [], "origins": []}))
    config.SAVED_URL = "http://127.0.0.1:8126/saved"
    config.BASE_URL = "http://127.0.0.1:8126"
    config.MAX_SCROLLS, config.SCROLL_PAUSE_MS, config.SCROLL_IDLE_ROUNDS = 40, 350, 3
    config.DETAIL_SETTLE_MS, config.DETAIL_SCROLLS, config.DETAIL_PAUSE_MS = 250, 1, 50
    config.RECYCLE_EVERY = 10          # 탭 재생성 경로도 태운다
    config.DOWNLOAD_MEDIA = False
    # 목록이 스레드 묶음을 안 줄 때의 대체 경로를 검증한다
    config.TRUST_LIST_THREADS = False

    # ── 1차: 20건에서 강제 중단 ──────────────────────────────
    seen = {"n": 0}

    async def on_post(item):
        await pipeline.save_post(item)
        seen["n"] += 1

    asyncio.run(scraper.collect(
        known_ids=set(), on_post=on_post,
        should_stop=lambda: seen["n"] >= STOP_AFTER,
    ))
    total1, done1 = db_state()
    print(f"1차: 저장 {total1}건 (상세 완료 {done1}건)")
    assert done1 >= STOP_AFTER, f"중간 저장이 안 됨: {done1}"
    assert done1 <= STOP_AFTER + 2, f"중단이 안 먹음: {done1}"

    # ── 2차: 이어받기 ────────────────────────────────────────
    known = pipeline.known_post_ids()
    assert len(known) == done1

    async def on_post2(item):
        await pipeline.save_post(item)

    asyncio.run(scraper.collect(known_ids=known, on_post=on_post2))
    total2, done2 = db_state()
    print(f"2차: 저장 {total2}건 (상세 완료 {done2}건)")
    srv.shutdown()

    assert done2 == base.TOTAL, f"이어받기 후 {done2}건 (기대 {base.TOTAL})"

    # 본문 + 이어쓴 댓글이 제대로 붙었는지
    with db() as conn:
        row = conn.execute("SELECT * FROM posts WHERE id='POST042'").fetchone()
        segs = [dict(r) for r in conn.execute(
            "SELECT * FROM segments WHERE post_id='POST042' ORDER BY ord")]
    assert row["body"] == "저장한 글 42 본문", row["body"]
    assert "이어서 쓴 글 42-1" in row["thread_text"]
    assert "이어서 쓴 글 42-2" in row["thread_text"]
    assert "남이 단 댓글" not in row["full_text"]
    assert [s["kind"] for s in segs] == ["root", "reply", "reply"]
    print("✓ 중간 저장 · 이어받기 · 본문+이어쓴 글 구성 모두 정상")
    return 0


if __name__ == "__main__":
    sys.exit(main())
