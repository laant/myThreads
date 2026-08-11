"""스케줄러 + 작업 큐 실행기.

- SYNC_TIMES(기본 09:10, 21:10 KST)에 자동 수집·분류
- 웹 UI의 '지금 동기화' 버튼이 넣은 queued 작업을 10초마다 확인해 실행
"""
from __future__ import annotations

import asyncio
import logging
import os
import threading
import time

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from . import config, pipeline
from .db import db, finish_job, init_db, now, reset_running_jobs

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("worker")

_lock = asyncio.Lock()


async def run_sync(reclassify_all: bool = False, job_id: int | None = None,
                   force_full: bool = False) -> bool:
    if _lock.locked():
        log.info("이미 실행 중 — 이번 요청은 건너뜁니다.")
        if job_id:
            finish_job(job_id, "done", "다른 작업이 실행 중이라 건너뛰었습니다.")
        return False
    async with _lock:
        try:
            await pipeline.sync(reclassify_all=reclassify_all, job_id=job_id,
                                force_full=force_full)
        except Exception as exc:
            log.error("동기화 실패: %s", exc)
        return True


def stalled_job():
    """진행이 WATCHDOG_MIN 분 이상 없는 '실행 중' 작업을 돌려준다 (없으면 None)."""
    if not config.WATCHDOG_MIN:
        return None
    cutoff = now() - config.WATCHDOG_MIN * 60
    with db() as conn:
        row = conn.execute(
            "SELECT id, message, heartbeat_at FROM jobs WHERE status='running' "
            "ORDER BY id DESC LIMIT 1").fetchone()
    if not row or (row["heartbeat_at"] or 0) > cutoff:
        return None
    return row


def watchdog_loop() -> None:
    """진행이 완전히 멈춘 작업을 감시한다 (별도 스레드).

    브라우저나 네트워크가 어중간하게 굳으면 어떤 상한도 안 걸릴 수 있고,
    그럴 땐 이벤트 루프까지 멈춰 있을 수 있다. 그래서 asyncio 밖에서 감시하고,
    프로세스를 끝내 컨테이너를 재시작시킨다 — 가장 확실한 복구다.
    (restart: unless-stopped → 자동 재시작 → 죽은 작업 정리 → 다음 실행에서 이어받기)
    """
    if not config.WATCHDOG_MIN:
        return
    while True:
        time.sleep(30)
        try:
            row = stalled_job()
            if not row:
                continue
            log.error("작업 %s(%s)가 %d분째 진행이 없습니다 — worker를 재시작합니다. "
                      "이미 저장된 글은 남아 있고 다음 실행에서 이어갑니다.",
                      row["id"], row["message"], config.WATCHDOG_MIN)
            finish_job(row["id"], "error",
                       f"{config.WATCHDOG_MIN}분간 진행 없음 — 자동 재시작 (이어받기 가능)")
        except Exception as exc:
            log.warning("watchdog 오류: %s", exc)
            continue
        os._exit(1)


async def poll_queue() -> None:
    with db() as conn:
        row = conn.execute(
            "SELECT id, kind FROM jobs WHERE status='queued' ORDER BY id LIMIT 1"
        ).fetchone()
        if row:
            conn.execute("UPDATE jobs SET status='running', started_at=? WHERE id=?",
                         (now(), row["id"]))
    if not row:
        return
    kind, jid = row["kind"], row["id"]
    log.info("큐 작업 실행: %s", kind)
    try:
        if kind in ("sync", "sync_full"):
            await run_sync(False, job_id=jid, force_full=(kind == "sync_full"))
        elif kind in ("reclassify", "classify"):
            async with _lock:
                await asyncio.to_thread(pipeline.classify_only, kind == "reclassify", jid)
        else:
            finish_job(jid, "error", f"알 수 없는 작업: {kind}")
    except Exception as exc:
        log.exception("작업 실패")
        finish_job(jid, "error", str(exc))


async def amain() -> None:
    init_db()
    killed = reset_running_jobs()
    if killed:
        log.warning("이전에 끝나지 않은 작업 %d건을 정리했습니다.", killed)
    sched = AsyncIOScheduler(timezone="Asia/Seoul")
    for t in config.SYNC_TIMES:
        try:
            hh, mm = t.split(":")
            sched.add_job(run_sync, CronTrigger(hour=int(hh), minute=int(mm)),
                          id=f"sync-{t}", replace_existing=True)
            log.info("자동 수집 예약: 매일 %s (KST)", t)
        except Exception:
            log.warning("SYNC_TIMES 형식 오류: %s", t)
    sched.add_job(poll_queue, "interval", seconds=10, id="queue", max_instances=1)
    threading.Thread(target=watchdog_loop, daemon=True, name="watchdog").start()
    sched.start()
    log.info("worker 시작됨 — 자동 수집 %s, 큐 대기 중", ", ".join(config.SYNC_TIMES))
    await asyncio.Event().wait()


def main() -> None:
    asyncio.run(amain())


if __name__ == "__main__":
    main()
