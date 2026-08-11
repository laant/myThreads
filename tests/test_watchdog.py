"""감시견 테스트 — 멈춘 작업을 알아보고, 진행 중인 작업은 건드리지 않는다."""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("DATA_DIR", "./data-test")

from app import config, worker  # noqa: E402
from app.db import db, init_db, now, start_job, touch_job  # noqa: E402


def fresh_db():
    if config.DB_PATH.exists():
        config.DB_PATH.unlink()
    init_db()


def test_running_job_with_recent_heartbeat_is_fine():
    fresh_db()
    config.WATCHDOG_MIN = 10
    jid = start_job("sync")
    touch_job(jid, "상세 수집 3/25건")
    assert worker.stalled_job() is None, "진행 중인 작업을 멈춘 것으로 봤습니다"
    print("✓ 진행 중인 작업은 그대로 둔다")


def test_stalled_job_is_detected():
    fresh_db()
    config.WATCHDOG_MIN = 10
    jid = start_job("sync")
    touch_job(jid, "상세 수집 3/25건")
    with db() as conn:            # 11분간 소식 없음
        conn.execute("UPDATE jobs SET heartbeat_at=? WHERE id=?", (now() - 11 * 60, jid))
    row = worker.stalled_job()
    assert row is not None and row["id"] == jid, "멈춘 작업을 못 찾았습니다"
    assert "3/25" in (row["message"] or "")
    print("✓ 진행이 멈춘 작업을 찾아낸다 (→ worker 재시작 → 이어받기)")


def test_finished_job_is_ignored():
    fresh_db()
    config.WATCHDOG_MIN = 10
    jid = start_job("sync")
    with db() as conn:
        conn.execute("UPDATE jobs SET status='done', heartbeat_at=? WHERE id=?",
                     (now() - 60 * 60, jid))
    assert worker.stalled_job() is None
    print("✓ 끝난 작업은 무시한다")


def test_disabled_watchdog():
    fresh_db()
    config.WATCHDOG_MIN = 0
    jid = start_job("sync")
    with db() as conn:
        conn.execute("UPDATE jobs SET heartbeat_at=? WHERE id=?", (now() - 60 * 60, jid))
    assert worker.stalled_job() is None
    config.WATCHDOG_MIN = 10
    print("✓ WATCHDOG_MIN=0 이면 끄인다")


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
    print("감시견 테스트 통과")
