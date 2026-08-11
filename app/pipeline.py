"""수집 → 저장 → 분류 파이프라인 및 CLI."""
from __future__ import annotations

import asyncio
import logging
import sys

from . import config
from .classifier import classify, taxonomy
from .collector import media as media_dl
from .collector import scraper
from .db import (db, finish_job, get_setting, init_db, is_cancel_requested, mark_skipped, now,
                 replace_media, replace_segments, set_setting, skipped_index, start_job,
                 touch_job, upsert_post)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("pipeline")


def known_post_ids() -> set[str]:
    """다시 처리할 필요가 없는 글 = 이미 수집 완료 + 저장하지 않기로 한 것."""
    with db() as conn:
        ids = {r["id"] for r in conn.execute("SELECT id FROM posts WHERE detail_ok=1")}
    return ids | skipped_index()[0]


def known_post_keys() -> set[str]:
    """'작성자 + 작성시각' 열쇠 — 코드가 안 잡혀도 내 글과 대조할 수 있게."""
    with db() as conn:
        rows = conn.execute(
            "SELECT author, posted_at FROM posts WHERE detail_ok=1 AND posted_at > 0")
        keys = {f"{(r['author'] or '').strip().lower()}|{int(r['posted_at'])}"
                for r in rows if r["author"]}
    return keys | skipped_index()[1]


async def save_post(item: dict) -> bool:
    """글 1건을 이미지까지 내려받아 저장. 신규면 True."""
    pid = item["id"]
    try:
        # 이미지 서버가 안 죽고 안 끊기는 경우 대비 — 글 하나에 매달리지 않는다
        item["media"] = await asyncio.wait_for(
            media_dl.download_all(pid, item.get("media") or []),
            timeout=config.MEDIA_TIMEOUT_SEC)
    except Exception as exc:
        log.warning("이미지 저장 건너뜀 %s: %s", pid, exc)
    with db() as conn:
        is_new = upsert_post(conn, item)
        replace_segments(conn, pid, item.get("segments") or [])
        replace_media(conn, pid, item.get("media") or [])
    return is_new


FULL_SWEEP_KEY = "last_full_sweep"
NEEDS_FULL_KEY = "needs_full_sweep"


def apply_saved_order(ids: list[str], partial: bool) -> int:
    """저장됨 목록에서의 순서를 DB에 반영 (최근 저장순 정렬용).

    증분 수집이라 위쪽 일부만 본 경우, 아래쪽 글들은 이번에 새로 추가된
    글 수만큼 순위가 뒤로 밀린다. 그만큼 먼저 더해준 뒤 본 구간을 덮어쓴다.
    반환값은 이번에 새로 발견한 글 수.
    """
    with db() as conn:
        existing = {r["id"] for r in conn.execute("SELECT id FROM posts")}
        added = sum(1 for i in ids if i not in existing)
        if partial and added:
            conn.execute("UPDATE posts SET saved_rank = COALESCE(saved_rank, 0) + ?", (added,))
        for rank, pid in enumerate(ids):
            conn.execute("UPDATE posts SET saved_rank=? WHERE id=?", (rank, pid))
    return added


def decide_sweep(force_full: bool = False) -> tuple[bool, str]:
    """이번 실행에서 저장됨 목록을 끝까지 훑을지(전체) 아니면
    새로 저장한 글만 확인하고 멈출지(증분) 결정한다."""
    if force_full:
        return True, "전체 훑기 (직접 지정)"
    if not config.INCREMENTAL:
        return True, "전체 훑기 (INCREMENTAL=0)"
    with db() as conn:
        have = conn.execute("SELECT COUNT(*) c FROM posts WHERE detail_ok=1").fetchone()["c"]
    if have < config.INCREMENTAL_MIN_POSTS:
        return True, f"전체 훑기 (수집된 글 {have}건 — 아직 기준이 없음)"
    if get_setting(NEEDS_FULL_KEY, False):
        return True, "전체 훑기 (지난번에 중간에 멈춰서 확인이 필요함)"
    last = int(get_setting(FULL_SWEEP_KEY, 0) or 0)
    days = (now() - last) / 86400 if last else 999
    if days >= config.FULL_SWEEP_DAYS:
        return True, f"전체 훑기 (마지막 전체 확인 {int(days)}일 전)"
    return False, "새로 저장한 글만 확인"


async def collect_only(job_id: int | None = None, force_full: bool = False) -> dict:
    init_db()
    known = known_post_ids()
    keys = known_post_keys()
    full, why = decide_sweep(force_full)
    log.info("%s — 이미 수집된 글 %d건", why, len(known))
    stats = {"발견": 0, "신규": 0, "갱신": 0, "기존유지": len(known)}

    async def on_post(item: dict) -> None:
        if not item.get("id"):
            return
        is_new = await save_post(item)
        stats["신규"] += int(is_new)
        stats["갱신"] += int(not is_new)

    async def on_skip(item: dict, reason: str) -> None:
        mark_skipped(item.get("id"), reason, item.get("author"), item.get("posted_at"))
        stats["건너뜀"] = stats.get("건너뜀", 0) + 1

    async def on_order(ids: list[str], partial: bool) -> None:
        apply_saved_order(ids, partial)

    async def progress(done: int, total: int) -> None:
        stats["발견"] = total
        msg = f"상세 수집 {done}/{total}건"
        if job_id:
            touch_job(job_id, msg, stats)
        if done % 10 == 0 or done == total:
            log.info("%s (신규 %d)", msg, stats["신규"])

    async def heartbeat(msg: str) -> None:
        if job_id:
            touch_job(job_id, msg, stats)

    def should_stop() -> bool:
        return bool(job_id) and is_cancel_requested(job_id)

    if job_id:
        touch_job(job_id, f"저장됨 목록 확인 중… — {why}", stats)

    items = await scraper.collect(
        max_posts=config.MAX_POSTS_PER_RUN, known_ids=known, known_keys=keys,
        on_post=on_post, on_skip=on_skip, on_order=on_order,
        progress=progress, should_stop=should_stop, heartbeat=heartbeat,
        stats=stats, stop_after_known=0 if full else config.STOP_AFTER_KNOWN,
    )
    stats["발견"] = max(stats["발견"], len(items))

    # 다음 실행에서 전체 훑기가 필요한지 기록
    swept_all = bool(stats.pop("전체훑기완료", False))
    interrupted = bool(stats.get("중단"))
    if swept_all and not interrupted:
        set_setting(FULL_SWEEP_KEY, now())
        set_setting(NEEDS_FULL_KEY, False)
    elif interrupted:
        # 중간에 멈췄으면 아래쪽에 못 가져온 글이 남아 있을 수 있다
        set_setting(NEEDS_FULL_KEY, True)

    log.info("수집 완료: %s", stats)
    return stats


async def sync(reclassify_all: bool = False, job_id: int | None = None,
               force_full: bool = False) -> dict:
    job = job_id or start_job("sync")
    try:
        stats = await collect_only(job_id=job, force_full=force_full)
        try:
            touch_job(job, "분류 중…", stats)
            cls = classify.run(only_unclassified=not reclassify_all,
                               rebuild_taxonomy=reclassify_all, job_id=job)
            stats["분류"] = cls.get("classified", 0)
        except Exception as exc:
            log.error("분류 단계 실패: %s", exc)
            stats["분류오류"] = str(exc)[:200]
        finish_job(job, "done", "동기화 완료", stats)
        return stats
    except Exception as exc:
        log.exception("동기화 실패")
        finish_job(job, "error", str(exc))
        raise


def classify_only(reclassify_all: bool = False, job_id: int | None = None) -> dict:
    init_db()
    job = job_id or start_job("reclassify" if reclassify_all else "classify")
    try:
        stats = classify.run(only_unclassified=not reclassify_all,
                             rebuild_taxonomy=reclassify_all, job_id=job)
        finish_job(job, "done", "분류 완료", stats)
        return stats
    except Exception as exc:
        finish_job(job, "error", str(exc))
        raise


def prune_comments(dry_run: bool = True) -> dict:
    """이미 저장된 것 중 '남의 글에 단 댓글'을 찾아 정리한다."""
    init_db()
    with db() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT id, author, reply_to, substr(COALESCE(body,''),1,40) AS peek FROM posts "
            "WHERE reply_to IS NOT NULL AND reply_to <> '' AND reply_to <> author")]
    print(f"댓글로 판단된 글: {len(rows)}건")
    for r in rows[:20]:
        print(f"  - {r['id']} @{r['author']} → @{r['reply_to']} | {r['peek']}")
    if len(rows) > 20:
        print(f"  … 외 {len(rows) - 20}건")

    unknown = 0
    with db() as conn:
        unknown = conn.execute(
            "SELECT COUNT(*) c FROM posts WHERE reply_to IS NULL").fetchone()["c"]
    if unknown:
        print(f"※ 판단 정보가 없는 예전 글 {unknown}건은 이번 정리 대상이 아닙니다 "
              f"(다음 전체 재수집 때 채워집니다).")

    if dry_run:
        print("\n실제로 지우려면: make prune-comments-apply")
        return {"found": len(rows), "deleted": 0}

    ids = [r["id"] for r in rows]
    with db() as conn:
        for pid in ids:
            conn.execute("DELETE FROM classification WHERE post_id=?", (pid,))
            conn.execute("DELETE FROM segments WHERE post_id=?", (pid,))
            conn.execute("DELETE FROM media WHERE post_id=?", (pid,))
            conn.execute("DELETE FROM posts WHERE id=?", (pid,))
    print(f"{len(ids)}건을 삭제했습니다.")
    return {"found": len(rows), "deleted": len(ids)}


def doctor() -> None:
    """지금 돌고 있는 컨테이너에 무엇이 적용돼 있고, 지난 실행이 어땠는지 그대로 보여준다."""
    import datetime as _dt
    import inspect
    import json as _json
    from pathlib import Path

    from .collector import parser, scraper

    print("── 1. 실행 중인 코드에 새 기능이 들어 있는가 " + "─" * 24)
    checks = [
        ("목록 묶음으로 전문 구성 (walk_threads)", hasattr(parser, "walk_threads")),
        ("작성자+시각 대조 (post_key)", hasattr(scraper, "post_key")),
        ("첫 known에서 중단", "known_keys" in inspect.signature(scraper.collect).parameters),
    ]
    for name, ok in checks:
        print(f"   {'✅' if ok else '❌'} {name}")
    if not all(ok for _, ok in checks):
        print("   → ❌ 가 있으면 이미지가 예전 것입니다: make build && make down && make up")

    src = Path(scraper.__file__)
    print(f"   코드 파일 시각: {_dt.datetime.fromtimestamp(src.stat().st_mtime):%Y-%m-%d %H:%M}")

    print("\n── 2. 지금 적용된 설정 " + "─" * 40)
    for k in ("INCREMENTAL", "STOP_AFTER_KNOWN", "INCREMENTAL_MIN_POSTS", "FULL_SWEEP_DAYS",
              "TRUST_LIST_THREADS", "FETCH_DETAIL", "SKIP_DETAIL_WHEN_NO_REPLIES",
              "SCROLL_PAUSE_MS", "DETAIL_PAUSE_MS"):
        print(f"   {k:28} = {getattr(config, k, '(없음)')}")

    print("\n── 3. 내 데이터 " + "─" * 47)
    with db() as conn:
        n = conn.execute("SELECT COUNT(*) c FROM posts").fetchone()["c"]
        ok = conn.execute("SELECT COUNT(*) c FROM posts WHERE detail_ok=1").fetchone()["c"]
    print(f"   저장된 글 {n}건 · 그중 '수집 완료' 표시 {ok}건")
    with db() as conn:
        by_reason = {r["reason"]: r["c"] for r in conn.execute(
            "SELECT reason, COUNT(*) c FROM skipped GROUP BY reason")}
        retrying = conn.execute(
            "SELECT COUNT(*) c FROM skipped WHERE reason='failed' AND tries < 3"
        ).fetchone()["c"]
    if by_reason:
        print("   저장하지 않기로 한 글: " +
              " · ".join(f"{k} {v}건" for k, v in by_reason.items()))
    if n and ok < n:
        print(f"   ⚠ 완료 표시가 없는 {n - ok}건이 있습니다 "
              f"(그중 {retrying}건은 아직 재시도 대상).")
    if by_reason.get("failed", 0) > 5:
        print("   ⚠ 상세 페이지에서 데이터를 못 건진 글이 많습니다. "
              "FETCH_DETAIL=0 으로 두면 상세를 아예 열지 않아 훨씬 빨라집니다 "
              "(이어쓴 글 일부 포기).")
    full, why = decide_sweep()
    print(f"   다음 동기화: {why}")
    if full and ok < config.INCREMENTAL_MIN_POSTS:
        print(f"   ⚠ 수집된 글이 {config.INCREMENTAL_MIN_POSTS}건 미만이라 계속 전체 훑기입니다.")

    print("\n── 4. 지난 실행 결과 " + "─" * 42)
    with db() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM jobs WHERE status IN ('done','error','canceled') "
            "ORDER BY id DESC LIMIT 3")]
    if not rows:
        print("   아직 완료된 작업이 없습니다.")
    for j in rows:
        when = _dt.datetime.fromtimestamp(j["started_at"] or 0)
        took = (j["finished_at"] or 0) - (j["started_at"] or 0)
        print(f"   [{j['id']}] {j['kind']} {when:%m-%d %H:%M} · {took}초 · {j['status']}")
        try:
            st = _json.loads(j["stats"] or "{}")
        except Exception:
            st = {}
        if st:
            print("       " + " · ".join(f"{k} {v}" for k, v in st.items()))
        opened = st.get("페이지열기")
        if opened:
            print(f"       ⚠ 상세 페이지를 {opened}번 열었습니다 → 목록 응답에서 스레드 묶음을 "
                  f"못 받았다는 뜻입니다. FETCH_DETAIL=0 으로 강제로 끌 수 있습니다.")


def reset_db() -> None:
    with db() as conn:
        for t in ("classification", "segments", "media", "posts", "categories", "jobs"):
            conn.execute(f"DELETE FROM {t}")
    log.info("데이터베이스를 초기화했습니다. (미디어 파일과 로그인 세션은 그대로)")


def main(argv: list[str]) -> int:
    init_db()
    cmd = argv[1] if len(argv) > 1 else "sync"
    full = "--full" in argv
    if cmd == "sync":
        asyncio.run(sync(force_full=full))
    elif cmd == "collect":
        asyncio.run(collect_only(force_full=full))
    elif cmd == "probe":
        # 저장 없이 '몇 개까지 긁히는지'만 빠르게 확인 (스크롤 진단용)
        items = asyncio.run(scraper.collect(fetch_details=False))
        print(f"\n저장됨 목록에서 {len(items)}개를 찾았습니다.")
        for it in items[:5]:
            print(f"  - @{it['author']:<20} {(it['body'] or '')[:40]}")
        if len(items) > 5:
            print(f"  … 외 {len(items) - 5}개")
    elif cmd == "classify":
        print(classify_only(False))
    elif cmd == "reclassify":
        print(classify_only(True))
    elif cmd == "taxonomy":
        for c in taxonomy.build(force="--force" in argv):
            print(f"{c['slug']:24} {c['name']}  ({c['view']})")
    elif cmd == "reset-db":
        reset_db()
    elif cmd == "prune-comments":
        prune_comments(dry_run="--apply" not in argv)
    elif cmd == "doctor":
        doctor()
    elif cmd == "status":
        full, why = decide_sweep()
        with db() as conn:
            n = conn.execute("SELECT COUNT(*) c FROM posts").fetchone()["c"]
            ok = conn.execute("SELECT COUNT(*) c FROM posts WHERE detail_ok=1").fetchone()["c"]
        last = int(get_setting(FULL_SWEEP_KEY, 0) or 0)
        import datetime as _dt
        print(f"저장된 글        : {n}건 (상세 완료 {ok}건)")
        print(f"마지막 전체 훑기 : "
              f"{_dt.datetime.fromtimestamp(last).strftime('%Y-%m-%d %H:%M') if last else '없음'}")
        print(f"전체 훑기 필요   : {'예' if get_setting(NEEDS_FULL_KEY, False) else '아니오'}")
        print(f"다음 동기화      : {why}")
    elif cmd == "unstick":
        from .db import reset_running_jobs
        n = reset_running_jobs()
        print(f"'실행 중'으로 멈춰 있던 작업 {n}건을 정리했습니다.")
    else:
        print("사용법: python -m app.pipeline "
              "[sync|collect|probe|classify|reclassify|taxonomy|reset-db]")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
