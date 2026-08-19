"""Playwright로 Threads '저장됨' 목록과 각 글의 전문을 수집한다."""
from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any

from playwright.async_api import Page, Response, async_playwright

from .. import config
from . import parser

log = logging.getLogger("collector")

GRAPHQL_HINTS = ("/graphql", "/api/graphql", "/api/v1/")
DEFAULT_BASE = "https://www.threads.com"
POST_HREF_RE = re.compile(r"/@([\w.\-]+)/post/([A-Za-z0-9_\-]+)")
# 첫 화면 데이터는 XHR이 아니라 HTML 문서 안에 이 형태로 실려 온다
JSON_SCRIPT_RE = re.compile(r'<script[^>]*type="application/json"[^>]*>(.*?)</script>', re.S)


class NotLoggedIn(RuntimeError):
    pass


def is_comment(item: dict) -> bool:
    """이 항목이 '남의 글에 달린 댓글'인가.

    Threads는 댓글도 저장할 수 있다. 원글이 아니라 댓글을 저장해 둔 경우는
    본문 맥락이 없어 분류에도 도움이 안 되므로 걸러낸다.
    (자기 스레드의 이어쓴 글은 reply_to가 본인이므로 원글로 취급한다.)
    """
    reply_to = (item.get("reply_to") or "").strip()
    return bool(reply_to) and reply_to != (item.get("author") or "").strip()


class JsonSink:
    """페이지가 주는 JSON에서 글 항목을 실시간으로 뽑아 모으는 수집통.

    두 곳에서 들어온다:
      1. 네트워크 응답(XHR) — 스크롤하며 더 불러오는 부분
      2. 최초 HTML 문서에 심긴 <script type="application/json"> — **첫 화면 부분**
    2번을 빼먹으면 저장됨 목록의 맨 위쪽 글들과 글 상세를 통째로 놓쳐,
    DOM 폴백(작성자명·상대시각이 섞인 텍스트)에 의존하게 된다.
    """

    def __init__(self) -> None:
        self.by_code: dict[str, dict] = {}
        self.threads: dict[str, list[dict]] = {}   # 루트 코드 → [루트, 이어쓴 글…]
        self.grouped = False                       # thread_items 묶음을 실제로 본 적 있는가
        self.responses = 0
        self.html_blocks = 0                       # HTML에서 읽어낸 JSON 블록 수
        self._tasks: set[asyncio.Task] = set()

    def attach(self, page: Page) -> None:
        page.on("response", self._on_response)

    def _on_response(self, response: Response) -> None:
        url = response.url
        if not any(h in url for h in GRAPHQL_HINTS):
            return
        task = asyncio.ensure_future(self._read(response))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _read(self, response: Response) -> None:
        try:
            ctype = (response.headers or {}).get("content-type", "")
            if "json" not in ctype:
                return
            # 스트리밍/롱폴링 응답은 본문이 영원히 끝나지 않는다 → 반드시 타임아웃
            body = await asyncio.wait_for(response.text(), timeout=config.RESPONSE_READ_SEC)
        except Exception:
            return
        self.responses += 1
        for chunk in body.split("\n"):
            chunk = chunk.strip()
            if not chunk or not chunk.startswith("{"):
                continue
            try:
                payload = json.loads(chunk)
            except Exception:
                continue
            self._ingest(payload)

    def _ingest(self, payload: Any) -> None:
        """파싱된 JSON 한 덩어리에서 글을 뽑아 담는다 (응답이든 HTML이든 동일)."""
        # 1) 스레드 묶음을 그대로 보존 — 여기에 '이어서 쓴 글'이 들어 있다
        for group in parser.walk_threads(payload):
            items = [parser.normalize(n) for n in group]
            for it in items:
                self._merge(it)
            root = items[0]
            code = root.get("id")
            if not code:
                continue
            self.grouped = True
            if len(items) > len(self.threads.get(code, [])):
                self.threads[code] = items
        # 2) 묶음 밖에 있는 글도 놓치지 않는다
        for node in parser.walk_posts(payload):
            self._merge(parser.normalize(node))

    def ingest_html(self, html: str) -> int:
        """HTML 문서에 심겨 온 JSON을 읽는다. 반환값은 새로 알게 된 글 수.

        Threads는 첫 화면 데이터를 응답이 아니라 문서 안에 넣어 보낸다.
        여기를 읽어야 저장됨 목록 상단과 글 상세의 본문·작성시각·이어쓴 글을
        제대로 얻는다 (읽지 못하면 DOM 텍스트로 때우게 된다).
        """
        before = len(self.by_code)
        for block in JSON_SCRIPT_RE.findall(html or ""):
            block = block.strip()
            if not block or not block.startswith("{"):
                continue
            try:
                payload = json.loads(block)
            except Exception:
                continue
            self.html_blocks += 1
            self._ingest(payload)
        return len(self.by_code) - before

    def _merge(self, item: dict) -> None:
        code = item.get("id") or item.get("pk")
        if not code:
            return
        cur = self.by_code.get(code)
        if cur is None:
            self.by_code[code] = item
            return
        score = lambda x: len(x.get("body") or "") + 10 * len(x.get("media") or [])  # noqa: E731
        if score(item) > score(cur):
            self.by_code[code] = item

    async def drain(self) -> None:
        """대기 중인 응답 파싱을 마무리한다. 끝나지 않는 응답에 발목 잡히지 않도록
        전체 대기 시간에도 상한을 둔다."""
        tasks = [t for t in self._tasks if not t.done()]
        if not tasks:
            return
        done, pending = await asyncio.wait(tasks, timeout=config.DRAIN_TIMEOUT_SEC)
        for t in pending:
            t.cancel()
            self._tasks.discard(t)
        if pending:
            log.debug("응답 %d건이 제한 시간 내에 끝나지 않아 건너뜁니다.", len(pending))

    def count(self) -> int:
        return len(self.by_code)

    def items(self) -> list[dict]:
        return list(self.by_code.values())

    def reset(self) -> None:
        self.by_code.clear()
        self.threads.clear()
        self.responses = 0
        self.html_blocks = 0


async def _absorb_html(page: Page, sink: JsonSink, what: str) -> int:
    """지금 열린 문서에 심긴 JSON을 수집통에 넣는다. 실패해도 수집을 멈추지 않는다."""
    try:
        found = sink.ingest_html(await page.content())
    except Exception as exc:
        log.debug("%s HTML 읽기 실패: %s", what, exc)
        return 0
    if found:
        log.info("%s HTML에서 글 %d건 확보", what, found)
    return found


async def _dismiss_popups(page: Page) -> None:
    for label in ["쿠키 허용", "선택적 쿠키 허용", "Allow all cookies", "모두 허용", "나중에 하기", "Not now"]:
        try:
            btn = page.get_by_role("button", name=label)
            if await btn.count() > 0:
                await btn.first.click(timeout=1500)
                await page.wait_for_timeout(400)
        except Exception:
            pass


async def _is_logged_out(page: Page) -> bool:
    if "/login" in page.url:
        return True
    try:
        content = await page.content()
    except Exception:
        return False
    markers = ("Instagram으로 로그인", "Log in with Instagram", "계정을 만드세요")
    return any(m in content for m in markers) and "저장됨" not in content


# 실제로 스크롤되는 요소를 찾아 끝까지 내리는 스크립트.
# Threads는 화면 밖 카드를 DOM에서 제거(가상화)하고, 페이지가 아니라
# 내부 컨테이너가 스크롤되는 경우가 있어 매번 스크롤 대상을 다시 고른다.
SCROLL_JS = """
() => {
  const doc = document.scrollingElement || document.documentElement;
  // '어느 요소가 진짜 스크롤러인지' 고르려 하지 않는다. 스크롤 가능한 것을
  // 전부 바닥까지 내려버리면 어떤 레이아웃이든 추가 로딩이 걸린다.
  const targets = [doc];
  for (const e of document.querySelectorAll('div,main,section,ul')) {
    const oy = getComputedStyle(e).overflowY;
    if ((oy === 'auto' || oy === 'scroll') && e.scrollHeight - e.clientHeight > 50) {
      targets.push(e);
    }
  }
  let moved = 0, height = 0;
  for (const el of targets) {
    const before = el.scrollTop;
    el.scrollTop = el.scrollHeight;
    moved += Math.abs(el.scrollTop - before);
    height = Math.max(height, el.scrollHeight);
  }
  window.scrollTo(0, document.body.scrollHeight);
  return {moved, height, targets: targets.length,
          docHeight: doc.scrollHeight, docTop: doc.scrollTop};
}
"""


def post_key(item: dict | None) -> str | None:
    """글 하나를 가리키는 보조 열쇠 — '작성자 + 작성시각'.

    코드(shortcode)가 안 잡히는 상황에서도 내 DB의 글과 대조할 수 있다.
    """
    if not item:
        return None
    author = (item.get("author") or "").strip().lower()
    ts = int(item.get("posted_at") or 0)
    return f"{author}|{ts}" if author and ts else None


async def _autoscroll(page: Page, max_scrolls: int, sink: JsonSink,
                      known_ids: set[str] | None = None,
                      known_keys: set[str] | None = None,
                      stop_after_known: int = 0,
                      heartbeat=None) -> tuple[dict[str, str], bool]:
    """스크롤하면서 화면에 나타난 글 링크를 누적한다.

    가상 스크롤 때문에 '현재 DOM의 링크 수'는 늘지 않으므로,
    매 회차마다 링크를 집합에 모아두는 것이 핵심이다.

    저장됨 목록은 '최근 저장이 위'이므로, 이미 가지고 있는 글이
    stop_after_known개 연속으로 나오면 그 아래는 전부 예전 것이라고 보고 멈춘다.
    (하나둘 섞여 나오는 것에 속지 않도록 '연속' 조건을 쓴다.)

    반환: (코드→작성자 순서 보존 dict, 조기 종료 여부)
    """
    seen: dict[str, str] = {}          # code -> author (등장 순서 유지)
    known_ids = known_ids or set()
    known_keys = known_keys or set()
    consec_known = 0
    early_stop = False
    stagnant = 0
    last_sig = None
    pause = config.SCROLL_PAUSE_MS

    def already_have(code: str) -> bool:
        """내 DB에 이미 있는 글인가 — 코드로, 없으면 작성자+작성시각으로 대조."""
        if code in known_ids:
            return True
        return post_key(sink.by_code.get(code)) in known_keys

    def note(links) -> bool:
        """새로 발견한 링크를 순서대로 기록. 종료 조건에 닿으면 True."""
        nonlocal consec_known
        for author, code in links:
            if code in seen:
                continue
            seen[code] = author
            if not stop_after_known:
                continue
            consec_known = consec_known + 1 if already_have(code) else 0
            if consec_known >= stop_after_known:
                log.info("이미 가진 글에 도달 — 여기서 멈춥니다 (%s)", code)
                return True
        return False

    for i in range(max_scrolls):
        if note(await _dom_links(page)):
            early_stop = True
            break

        try:
            info = await page.evaluate(SCROLL_JS)
        except Exception as exc:
            log.debug("스크롤 스크립트 실패(%s) — 휠 입력으로 대체", exc)
            info = {}
            await page.mouse.move(700, 500)
            await page.mouse.wheel(0, 5000)

        await page.wait_for_timeout(pause)
        await sink.drain()

        if note(await _dom_links(page)):
            early_stop = True
            break

        sig = (len(seen), sink.count(),
               int(info.get("height") or 0), int(info.get("docHeight") or 0))
        if sig == last_sig:
            stagnant += 1
            # 마지막 카드로 강제 이동 + End 키 — 관성 로딩을 한 번 더 자극
            try:
                await page.eval_on_selector_all(
                    'a[href*="/post/"]',
                    "els => els.length && els[els.length-1].scrollIntoView({block:'end'})")
            except Exception:
                pass
            await page.keyboard.press("End")
            await page.wait_for_timeout(pause)
            await sink.drain()
            if note(await _dom_links(page)):
                early_stop = True
                break
        else:
            stagnant = 0
        last_sig = sig

        if heartbeat:                     # 살아 있음을 알린다 (감시견이 오해하지 않도록)
            await heartbeat(f"목록 확인 중… {len(seen)}개 (스크롤 {i + 1}회)")
        if i % 5 == 0 or stagnant:
            log.info("스크롤 %d회 · 링크 %d개 · JSON %d개 · 높이 %s (스크롤러 %s개)",
                     i + 1, len(seen), sink.count(), info.get("height", "?"),
                     info.get("targets", "?"))

        if stagnant >= config.SCROLL_IDLE_ROUNDS:
            log.info("스크롤 종료 — 목록 끝까지 확인했습니다 (링크 %d개 / JSON %d개)",
                     len(seen), sink.count())
            break
    else:
        log.warning("MAX_SCROLLS(%d)에 도달했습니다. 저장한 글이 더 있으면 값을 늘려주세요.",
                    max_scrolls)

    if early_stop:
        log.info("이미 가진 글이 %d개 연속 나와 훑기를 멈춥니다 — 링크 %d개만 확인 "
                 "(그 아래는 전부 예전에 저장한 글)", stop_after_known, len(seen))

    await sink.drain()
    return seen, early_stop


async def _dom_links(page: Page) -> list[tuple[str, str]]:
    hrefs = await page.eval_on_selector_all(
        'a[href*="/post/"]', "els => els.map(e => e.getAttribute('href'))"
    )
    out, seen = [], set()
    for h in hrefs or []:
        m = POST_HREF_RE.search(h or "")
        if not m:
            continue
        key = m.group(2)
        if key in seen:
            continue
        seen.add(key)
        out.append((m.group(1), key))
    return out


async def _dom_thread(page: Page) -> list[dict]:
    """JSON 수집이 실패했을 때를 위한 최소한의 DOM 폴백."""
    try:
        blocks = await page.eval_on_selector_all(
            "div[data-pressable-container='true']",
            """els => els.slice(0, 40).map(e => ({
                 text: (e.innerText || '').trim(),
                 imgs: Array.from(e.querySelectorAll('img'))
                          .map(i => ({src: i.src, alt: i.alt}))
                          .filter(i => i.src && !i.src.includes('profile'))
               }))""",
        )
    except Exception:
        return []
    return [b for b in blocks if b.get("text")]


async def collect(
    max_posts: int = 0,
    known_ids: set[str] | None = None,
    known_keys: set[str] | None = None,
    fetch_details: bool = True,
    progress=None,
    on_post=None,
    on_skip=None,
    on_order=None,
    should_stop=None,
    heartbeat=None,
    stats: dict | None = None,
    stop_after_known: int = 0,
) -> list[dict]:
    """저장됨 목록을 수집해 표준 형태의 글 목록을 반환.

    on_post(item)      — 글 하나를 다 읽을 때마다 즉시 호출 (중간 저장용, async)
    should_stop()      — True를 돌려주면 남은 글을 남겨두고 정상 종료
    progress(i, total) — 진행 상황 보고 (async 가능)
    """
    known_ids = known_ids or set()
    known_keys = known_keys or set()
    if not config.STATE_PATH.exists():
        raise NotLoggedIn(
            "로그인 세션이 없습니다. `make login` 으로 최초 1회 로그인해 주세요."
        )

    results: list[dict] = []
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=config.HEADLESS,
            args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-blink-features=AutomationControlled"],
        )
        ctx = await browser.new_context(
            storage_state=str(config.STATE_PATH),
            locale="ko-KR",
            timezone_id="Asia/Seoul",
            user_agent=config.USER_AGENT,
            viewport={"width": 1280, "height": 1600},
        )
        # 모든 Playwright 호출에 자체 상한을 건다 (밖에서 취소하지 않기 위해)
        ctx.set_default_timeout(config.DETAIL_TIMEOUT_SEC * 1000)
        ctx.set_default_navigation_timeout(config.DETAIL_TIMEOUT_SEC * 1000)
        page = await ctx.new_page()
        sink = JsonSink()
        sink.attach(page)

        await page.goto(config.SAVED_URL, wait_until="domcontentloaded", timeout=60000)
        await page.wait_for_timeout(config.PAGE_SETTLE_MS)
        await _dismiss_popups(page)
        if await _is_logged_out(page):
            await browser.close()
            raise NotLoggedIn("세션이 만료되었습니다. `make login` 으로 다시 로그인해 주세요.")

        # 첫 화면 글들은 응답이 아니라 이 문서 안에 들어 있다 — 먼저 읽어둔다
        await _absorb_html(page, sink, "저장됨 목록 첫 화면")

        t_scroll = asyncio.get_event_loop().time()
        seen_links, early_stop = await _autoscroll(
            page, config.MAX_SCROLLS, sink, known_ids=known_ids, known_keys=known_keys,
            stop_after_known=stop_after_known, heartbeat=heartbeat)
        t_scroll = asyncio.get_event_loop().time() - t_scroll
        log.info("목록 훑기에 %.1f초 걸렸습니다.", t_scroll)
        if stats is not None:
            stats["목록훑기(초)"] = round(t_scroll, 1)

        thread_groups = dict(sink.threads)
        sink_grouped = sink.grouped and config.TRUST_LIST_THREADS
        # 스레드 묶음의 2번째 이후 항목 = 작성자가 이어서 쓴 글.
        # 이건 '저장한 글'이 아니라 본문의 일부이므로 목록에서 빼야 한다.
        continuation = {i["id"] for g in thread_groups.values() for i in g[1:] if i.get("id")}

        listed = [i for i in sink.items() if i.get("id") not in continuation]
        log.info("저장됨 목록: 글 %d개 / 링크 %d개%s (이어쓴 글 %d개는 본문으로 흡수)",
                 len(listed), len(seen_links),
                 " (새 글만 확인)" if early_stop else "", len(continuation))

        by_id = {i["id"]: i for i in listed if i.get("id")}
        for code, author in seen_links.items():
            if code not in by_id:
                by_id[code] = {
                    "id": code, "pk": "", "author": author, "author_name": "", "author_pic": "",
                    "url": f"https://www.threads.com/@{author}/post/{code}",
                    "posted_at": 0, "body": "", "like_count": 0, "reply_count": 0,
                    "media": [], "reply_to": "", "link": "",
                }

        # 저장됨 목록은 최근 저장 순 → 스크롤하며 본 순서를 유지
        order = list(seen_links.keys())
        if not early_stop:
            # 끝까지 훑은 경우에만, 링크로는 못 봤지만 JSON에는 있던 글을 뒤에 붙인다.
            # (중간에 멈춘 경우엔 위치를 알 수 없으므로 넣지 않는다 — 다음 전체 훑기에서 잡힌다)
            for code in by_id:
                if code not in order:
                    order.append(code)

        targets = []
        for rank, code in enumerate(c for c in order if c in by_id):
            item = by_id[code]
            item["saved_rank"] = rank          # 0 = 가장 최근에 저장한 글
            targets.append(item)
        if max_posts:
            targets = targets[:max_posts]

        if on_order:
            await on_order([t["id"] for t in targets], early_stop)

        # 저장한 것이 '남의 글에 달린 댓글'이면 목록 단계에서 이미 걸러낸다
        excluded = 0
        if config.SKIP_REPLIES:
            keep, drop = [], []
            for t in targets:
                (drop if is_comment(t) else keep).append(t)
            excluded = len(drop)
            if excluded:
                log.info("댓글로 저장된 항목 %d건 제외", excluded)
            for t in drop:                       # 다음 실행에서 또 만나지 않도록 기억
                if on_skip:
                    await on_skip(t, "comment")
            targets = keep

        if stats is not None:
            stats["제외(댓글)"] = excluded
            stats["훑기"] = "새 글만" if early_stop else "전체"
            stats["전체훑기완료"] = not early_stop

        if not fetch_details:
            await browser.close()
            return [_finalize_from_list(t) for t in targets]

        multi = sum(1 for g in thread_groups.values() if len(g) > 1)
        log.info("목록에서 받은 스레드 묶음 %d개 (이어쓴 글이 있는 것 %d개)%s",
                 len(thread_groups), multi,
                 " — 상세 페이지 없이 전문 구성" if sink_grouped else "")

        todo = [t for t in targets
                if t["id"] not in known_ids and post_key(t) not in known_keys]
        log.info("처리 대상 %d건 (이미 가진 %d건은 건너뜁니다)",
                 len(todo), len(targets) - len(todo))

        deadline = (asyncio.get_event_loop().time() + config.RUN_BUDGET_MIN * 60
                    if config.RUN_BUDGET_MIN else None)
        stopped = ""
        skipped_comments = 0
        quick = 0        # 목록 정보만으로 끝낸 건수
        opened = 0       # 실제로 상세 페이지를 연 건수
        t_detail = asyncio.get_event_loop().time()

        for idx, item in enumerate(todo):
            if should_stop and should_stop():
                stopped = "사용자 요청으로 중단"
                break
            if deadline and asyncio.get_event_loop().time() > deadline:
                stopped = f"이번 실행 제한시간({config.RUN_BUDGET_MIN}분) 도달"
                break
            visited = False
            group = thread_groups.get(item["id"])
            if not detail_needed(item, group, sink_grouped):
                # 목록 응답만으로 전문이 완성된다 → 페이지를 열지 않는다
                if group and len(group) > 1:
                    detail = parser.build_thread(group, item["id"])
                    detail["detail_ok"] = 1
                    for k in ("url", "author", "author_name", "author_pic", "saved_rank"):
                        detail.setdefault(k, item.get(k))
                    detail["id"] = item["id"]
                else:
                    detail = _finalize_from_list(item, complete=True)
                quick += 1
            else:
                visited = True
                try:
                    # 밖에서 asyncio.wait_for 로 잘라내면 브라우저 연결이 어중간한 상태로
                    # 남아 이후 모든 호출이 영원히 멈출 수 있다. Playwright 자체 타임아웃에 맡긴다.
                    detail = await _fetch_detail(page, sink, item)
                    opened += 1
                except Exception as exc:  # 개별 글 실패가 전체를 막지 않도록
                    log.warning("상세 수집 실패 %s: %s", item["id"], exc)
                    detail = _finalize_from_list(item)
                    if on_skip:              # 몇 번 실패하면 그만 시도하도록 기록
                        await on_skip(item, "failed")

            # 열어봤는데 아무것도 못 건진 경우도 '실패'로 세어 둔다.
            # 안 그러면 detail_ok=0 으로 저장돼 매 실행마다 똑같이 다시 열게 된다.
            if visited and not detail.get("detail_ok") and on_skip:
                await on_skip(item, "failed")

            detail.setdefault("saved_rank", item.get("saved_rank"))
            # 목록에서는 몰랐다가 상세에서 드러나는 댓글도 여기서 제외
            if config.SKIP_REPLIES and is_comment(detail):
                skipped_comments += 1
                log.debug("댓글이라 제외: %s (→ @%s)", detail["id"], detail.get("reply_to"))
                if on_skip:
                    await on_skip(detail, "comment")
                if progress:
                    await progress(idx + 1, len(todo))
                continue

            results.append(detail)
            if on_post:                      # 한 건씩 즉시 저장 → 중간에 끊겨도 남는다
                try:
                    await on_post(detail)
                except Exception as exc:
                    log.error("저장 실패 %s: %s", item["id"], exc)
            if progress:
                await progress(idx + 1, len(todo))
            if not visited:
                continue                     # 페이지를 안 열었으면 쉴 이유도 없다
            await page.wait_for_timeout(config.DETAIL_PAUSE_MS)

            # 수백 건을 한 페이지로 계속 열면 메모리가 쌓여 브라우저가 죽는다.
            # 주기적으로 탭을 새로 만들어 정리한다.
            if config.RECYCLE_EVERY and opened % config.RECYCLE_EVERY == 0:
                try:
                    await sink.drain()
                    await page.close()
                    page = await ctx.new_page()
                    sink.attach(page)
                    log.info("탭을 새로 열어 메모리를 정리했습니다 (%d건 처리)", idx + 1)
                except Exception as exc:
                    log.warning("탭 재생성 실패: %s", exc)

        if skipped_comments:
            log.info("상세 확인 후 댓글이라 제외한 항목: %d건", skipped_comments)
        t_detail = asyncio.get_event_loop().time() - t_detail
        log.info("상세 단계: %.1f초 — 페이지를 연 글 %d건 / 목록 정보로 끝낸 글 %d건",
                 t_detail, opened, quick)
        if stats is not None:
            stats["상세수집(초)"] = round(t_detail, 1)
            stats["제외(댓글)"] = excluded + skipped_comments
            stats["페이지열기"] = opened
            stats["빠른수집"] = quick
        if stats is not None and stopped:
            stats["중단"] = stopped
        if stopped:
            log.warning("%s — %d/%d건까지 처리했습니다. 남은 글은 다음 실행에서 이어갑니다.",
                        stopped, len(results), len(todo))

        await browser.close()
    return results


def _finalize_from_list(item: dict, complete: bool = False) -> dict:
    out = dict(item)
    out["segments"] = [{"kind": "root", "author": item.get("author"),
                        "text": item.get("body", ""), "posted_at": item.get("posted_at")}]
    out["thread_text"] = ""
    out["full_text"] = item.get("body", "")
    out["detail_ok"] = 1 if complete else 0
    return out


def detail_needed(item: dict, group: list[dict] | None = None,
                  grouping_works: bool = False) -> bool:
    """이 글의 상세 페이지를 굳이 열어야 하는가.

    목록 응답이 스레드 묶음(thread_items)을 주고 있다면 '이어서 쓴 글'까지
    이미 손에 있다는 뜻이다. 그때는 어떤 글도 페이지를 열 필요가 없다.
    묶음을 못 받은 경우에만 예전 방식(본문 유무 · 댓글 수)으로 판단한다.
    """
    if not config.FETCH_DETAIL:
        return False                      # 절대 열지 않기 (사용자가 명시적으로 끔)
    if not config.SKIP_DETAIL_WHEN_NO_REPLIES:
        return True
    if not (item.get("body") or "").strip():
        return True                       # 본문을 아직 모르면 열어봐야 한다
    if grouping_works:
        return False                      # 묶음이 곧 전문 — 더 볼 것이 없다
    if group and len(group) > 1:
        return False
    rc = item.get("reply_count")
    if rc is None:
        return True                       # 댓글 수를 모르면 안전하게 열어본다
    return int(rc) > 0


async def _fetch_detail(page: Page, sink: JsonSink, item: dict) -> dict:
    url = item.get("url") or f"{config.BASE_URL}/t/{item['id']}"
    if config.BASE_URL != DEFAULT_BASE:      # 미러/테스트 서버를 가리킬 때
        url = url.replace(DEFAULT_BASE, config.BASE_URL)
    sink.reset()
    await page.goto(url, wait_until="domcontentloaded",
                    timeout=config.DETAIL_TIMEOUT_SEC * 1000)
    await page.wait_for_timeout(config.DETAIL_SETTLE_MS)
    # 상세 페이지도 본문이 문서 안에 실려 온다 (응답만 봐서는 한 건도 못 건진다)
    await _absorb_html(page, sink, f"상세 {item['id']}")
    # 이어쓴 댓글이 접혀 있을 수 있으니 조금 스크롤
    for _ in range(config.DETAIL_SCROLLS):
        await page.mouse.wheel(0, 2500)
        await page.wait_for_timeout(600)
    await sink.drain()

    items = sink.items()
    if items:
        thread = parser.build_thread(items, item["id"])
        thread["detail_ok"] = 1
        # 목록에서 얻은 값 보강
        for k in ("url", "author", "author_name", "author_pic"):
            if not thread.get(k) and item.get(k):
                thread[k] = item[k]
        thread["id"] = item["id"]
        return thread

    blocks = await _dom_thread(page)
    if blocks:
        texts = [b["text"] for b in blocks]
        media = [{"kind": "image", "url": i["src"], "alt": i.get("alt")}
                 for b in blocks for i in b.get("imgs", [])]
        out = _finalize_from_list(item)
        out["body"] = out["body"] or texts[0]
        out["thread_text"] = "\n\n".join(texts[1:3])
        out["full_text"] = "\n\n".join(texts[:3])
        out["segments"] = [{"kind": "root" if n == 0 else "reply", "author": item.get("author"),
                            "text": t, "posted_at": item.get("posted_at")}
                           for n, t in enumerate(texts[:3])]
        out["media"] = media[:8]
        out["detail_ok"] = 1
        return out

    return _finalize_from_list(item)
