"""증분 수집 테스트.

저장됨 목록은 '최근 저장이 위'이므로, 이미 가진 글이 연속으로 나오면 그 아래는
전부 예전 것이다. 끝까지 훑지 않고 멈추는지, 그러면서도 새 글은 하나도
빠뜨리지 않는지, 저장 순서(랭크)가 어긋나지 않는지 확인한다.
"""
import asyncio
import json
import os
import sys
import threading
import time
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
os.environ.setdefault("DATA_DIR", "./data-test")

import test_scroll_e2e as base  # noqa: E402

from app import config  # noqa: E402
from app.collector import scraper  # noqa: E402
from app.db import db, init_db, now, upsert_post  # noqa: E402
from app.pipeline import apply_saved_order  # noqa: E402

NEW_COUNT = 6      # 목록 맨 위 6건만 새 글, 나머지 48건은 이미 가지고 있음


def setup_server(port: int):
    srv = ThreadingHTTPServer(("127.0.0.1", port), base.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    config.STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    config.STATE_PATH.write_text(json.dumps({"cookies": [], "origins": []}))
    config.SAVED_URL = f"http://127.0.0.1:{port}/saved"
    config.MAX_SCROLLS, config.SCROLL_PAUSE_MS, config.SCROLL_IDLE_ROUNDS = 60, 350, 3
    config.SKIP_REPLIES = True
    return srv


def test_incremental_stops_early_and_keeps_new():
    srv = setup_server(8128)
    known = {f"POST{i:03d}" for i in range(NEW_COUNT, base.TOTAL)}

    t0 = time.time()
    partial = asyncio.run(scraper.collect(
        known_ids=known, fetch_details=False, stop_after_known=1))
    t_partial = time.time() - t0

    t0 = time.time()
    full = asyncio.run(scraper.collect(known_ids=known, fetch_details=False))
    t_full = time.time() - t0
    srv.shutdown()

    codes = [p["id"] for p in partial]
    new_ids = {f"POST{i:03d}" for i in range(NEW_COUNT)}
    missing = new_ids - set(codes)
    assert not missing, f"새 글을 놓침: {missing}"
    assert len(codes) < base.TOTAL, f"조기 종료 안 됨 ({len(codes)}건)"
    assert len(codes) <= NEW_COUNT + base.PER_PAGE, \
        f"이미 가진 글에서 바로 안 멈춤 ({len(codes)}건)"
    assert len(full) == base.TOTAL, f"전체 훑기가 {len(full)}건"
    assert t_partial < t_full, f"증분({t_partial:.1f}s)이 전체({t_full:.1f}s)보다 안 빠름"
    print(f"✓ 증분 {len(codes)}건 / {t_partial:.1f}초  vs  "
          f"전체 {len(full)}건 / {t_full:.1f}초 — 새 글 {NEW_COUNT}건 모두 확보")


def test_no_early_stop_when_all_new():
    """전부 새 글이면 끝까지 훑어야 한다 (조기 종료 오작동 방지)."""
    srv = setup_server(8129)
    items = asyncio.run(scraper.collect(
        known_ids=set(), fetch_details=False, stop_after_known=1))
    srv.shutdown()
    assert len(items) == base.TOTAL, f"{len(items)}건 — 전량 수집돼야 함"
    print("✓ 아는 글이 없으면 끝까지 훑음")


def test_scattered_known_does_not_trigger_stop():
    """이미 가진 글이 드문드문 섞여 있을 때는 멈추면 안 된다."""
    srv = setup_server(8130)
    known = {f"POST{i:03d}" for i in range(base.TOTAL) if i % 3 == 0}   # 3개 중 1개
    items = asyncio.run(scraper.collect(
        known_ids=known, fetch_details=False, stop_after_known=3))
    srv.shutdown()
    assert len(items) == base.TOTAL, f"{len(items)}건 — 섞여 있을 땐 끝까지 가야 함"
    print("✓ 아는 글이 흩어져 있으면 조기 종료하지 않음")


def test_match_by_author_and_time():
    """코드가 아니라 '작성자 + 작성시각'으로도 내 글을 알아본다."""
    srv = setup_server(8134)
    # 코드는 하나도 모르고, 7번째 글부터의 작성자·시각만 알고 있는 상태
    keys = {f"user{i % 7}|{1750000000 + i}" for i in range(NEW_COUNT, base.TOTAL)}
    items = asyncio.run(scraper.collect(
        known_ids=set(), known_keys=keys, fetch_details=False, stop_after_known=1))
    srv.shutdown()
    codes = [p["id"] for p in items]
    assert {f"POST{i:03d}" for i in range(NEW_COUNT)} <= set(codes), "새 글을 놓침"
    assert len(codes) < base.TOTAL, f"작성자+시각 대조로 못 멈춤 ({len(codes)}건)"
    print(f"✓ 작성자+작성시각 대조만으로 이미 가진 글을 알아보고 중단 ({len(codes)}건)")


def test_saved_rank_shift_on_partial():
    if config.DB_PATH.exists():
        config.DB_PATH.unlink()
    init_db()
    with db() as conn:
        for rank, pid in enumerate(["A", "B", "C"]):
            upsert_post(conn, {"id": pid, "saved_rank": rank, "author": "u",
                               "body": pid, "full_text": pid, "detail_ok": 1,
                               "posted_at": now()})

    # 새 글 N1, N2 가 맨 위에 생겼고 그 아래로 A, B 까지만 확인하고 멈춘 상황
    added = apply_saved_order(["N1", "N2", "A", "B"], partial=True)
    assert added == 2, added
    with db() as conn:
        ranks = {r["id"]: r["saved_rank"] for r in conn.execute("SELECT id, saved_rank FROM posts")}
    # A,B 는 본 구간이라 새 순위로 덮어써지고, 못 본 C 는 새 글 2건만큼 밀린다
    assert ranks == {"A": 2, "B": 3, "C": 4}, ranks
    print("✓ 증분 수집 후에도 '최근 저장순'이 어긋나지 않음")


if __name__ == "__main__":
    test_saved_rank_shift_on_partial()
    test_incremental_stops_early_and_keeps_new()
    test_no_early_stop_when_all_new()
    test_scattered_known_does_not_trigger_stop()
    test_match_by_author_and_time()
    print("증분 수집 테스트 통과")
