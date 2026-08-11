"""무한 스크롤 수집 E2E 테스트.

Threads의 '저장됨' 페이지와 같은 조건(내부 컨테이너 스크롤 + 가상 스크롤로
화면 밖 카드 DOM 제거 + GraphQL 추가 로딩)을 흉내 낸 가짜 페이지를 띄우고,
수집기가 첫 화면(9개)에서 멈추지 않고 끝까지 긁어오는지 검증한다.
"""
import asyncio
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("DATA_DIR", "./data")

from app import config  # noqa: E402
from app.collector import scraper  # noqa: E402

PAGES = 6          # 총 페이지 수
PER_PAGE = 9       # 페이지당 글 수 (실제 사용자가 겪은 '9개'와 동일)
TOTAL = PAGES * PER_PAGE


def make_post(idx: int) -> dict:
    return {
        "pk": f"{1000 + idx}", "code": f"POST{idx:03d}", "taken_at": 1750000000 + idx,
        "user": {"username": f"user{idx % 7}", "full_name": "테스트", "profile_pic_url": ""},
        "caption": {"text": f"저장한 글 {idx} 본문"},
        "like_count": idx,
        "text_post_app_info": {"direct_reply_count": 0},
        "image_versions2": {"candidates": [{"url": f"https://cdn/{idx}.jpg", "width": 1080}]},
    }


def page_payload(page: int) -> dict:
    start = page * PER_PAGE
    return {"data": {"feedback": {"edges": [
        {"node": {"thread_items": [{"post": make_post(i)}]}}
        for i in range(start, min(start + PER_PAGE, TOTAL))
    ]}}}


HTML = """<!doctype html><html><head><meta charset="utf-8"><title>saved</title>
<style>
  *{box-sizing:border-box}
  body{margin:0} #side{position:fixed;left:0;top:0;width:200px;height:100vh;background:#eee}
  #feed{margin-left:200px;height:100vh;overflow-y:auto}
  .card{height:220px;border-bottom:1px solid #ccc;padding:8px}
</style></head><body>
<div id="side">사이드바(스크롤 안 됨)</div>
<div id="feed"><div id="spacer"></div><div id="list"></div></div>
<script>
let page = 0, loading = false, all = [];
const feed = document.getElementById('feed');
function render() {
  // 가상 스크롤 흉내: 최근 12개만 DOM에 남기고 나머지는 스페이서로 높이만 유지
  const visible = all.slice(-12);
  document.getElementById('spacer').style.height =
    ((all.length - visible.length) * 220) + 'px';
  document.getElementById('list').innerHTML = visible.map(p =>
    `<div class="card"><a href="/@${p.u}/post/${p.c}">${p.c}</a><p>${p.t}</p></div>`).join('');
}
async function loadMore() {
  if (loading || page >= PAGES_TOTAL) return;
  loading = true;
  const r = await fetch('/api/graphql?page=' + page);
  const d = await r.json();
  const edges = d.data.feedback.edges;
  for (const e of edges) {
    const p = e.node.thread_items[0].post;
    all.push({c: p.code, u: p.user.username, t: p.caption.text});
  }
  page++; render(); loading = false;
}
feed.addEventListener('scroll', () => {
  if (feed.scrollTop + feed.clientHeight > feed.scrollHeight - 400) loadMore();
});
loadMore();
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path.startswith("/api/graphql"):
            page = int(self.path.split("page=")[-1])
            body = json.dumps(page_payload(page)).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        body = HTML.replace("PAGES_TOTAL", str(PAGES)).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main() -> int:
    srv = ThreadingHTTPServer(("127.0.0.1", 8123), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    config.STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    config.STATE_PATH.write_text(json.dumps({"cookies": [], "origins": []}))
    config.SAVED_URL = "http://127.0.0.1:8123/saved"
    config.MAX_SCROLLS = 60
    config.SCROLL_PAUSE_MS = 400
    config.SCROLL_IDLE_ROUNDS = 4

    items = asyncio.run(scraper.collect(fetch_details=False))
    srv.shutdown()

    codes = {i["id"] for i in items}
    print(f"수집된 글: {len(codes)} / 기대값 {TOTAL}")
    missing = [f"POST{i:03d}" for i in range(TOTAL) if f"POST{i:03d}" not in codes]
    assert not missing, f"누락: {missing[:10]}"
    sample = next(i for i in items if i["id"] == "POST042")
    assert sample["body"] == "저장한 글 42 본문", sample
    assert sample["media"][0]["url"] == "https://cdn/42.jpg"
    assert sample["url"].endswith("/post/POST042")
    print("✓ 가상 스크롤 + 내부 컨테이너 + 추가 로딩 환경에서 전량 수집 확인")
    return 0


if __name__ == "__main__":
    sys.exit(main())
