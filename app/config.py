"""환경설정 로더."""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


def _bool(key: str, default: bool = False) -> bool:
    v = os.getenv(key)
    if v is None:
        return default
    return v.strip().lower() in {"1", "true", "yes", "y", "on"}


def _int(key: str, default: int) -> int:
    try:
        return int(os.getenv(key, "") or default)
    except ValueError:
        return default


DATA_DIR = Path(os.getenv("DATA_DIR", "/data"))
MEDIA_DIR = DATA_DIR / "media"
DB_PATH = DATA_DIR / "mythreads.db"
STATE_PATH = DATA_DIR / "state.json"          # Playwright storage_state (로그인 세션)

# 분류에 쓰는 LLM (gemini | openai | anthropic)
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "gemini").strip().lower()

GEMINI_API_KEY = (os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY") or "").strip()
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.6-flash").strip()
GEMINI_THINKING = os.getenv("GEMINI_THINKING", "low").strip()   # low | high

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-5-mini").strip()

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "").strip()
ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-5").strip()

SAVED_URL = os.getenv("SAVED_URL", "https://www.threads.com/saved").strip()
BASE_URL = "https://www.threads.com"

SYNC_TIMES = [t.strip() for t in os.getenv("SYNC_TIMES", "09:10,21:10").split(",") if t.strip()]
MAX_POSTS_PER_RUN = _int("MAX_POSTS_PER_RUN", 0)
MAX_SCROLLS = _int("MAX_SCROLLS", 400)
PAGE_SETTLE_MS = _int("PAGE_SETTLE_MS", 2000)       # 저장됨 페이지 최초 로딩 대기
SCROLL_PAUSE_MS = _int("SCROLL_PAUSE_MS", 1400)     # 스크롤 후 로딩 대기
SCROLL_IDLE_ROUNDS = _int("SCROLL_IDLE_ROUNDS", 6)  # 변화 없는 회차가 이만큼이면 종료

# 멈춤 방지용 상한선들 (초/분)
RESPONSE_READ_SEC = _int("RESPONSE_READ_SEC", 15)   # 한 응답 본문 읽기 제한
DRAIN_TIMEOUT_SEC = _int("DRAIN_TIMEOUT_SEC", 20)   # 응답 파싱 마무리 대기 제한
DETAIL_TIMEOUT_SEC = _int("DETAIL_TIMEOUT_SEC", 60)  # 글 1건 상세 수집 제한
DETAIL_PAUSE_MS = _int("DETAIL_PAUSE_MS", 700)      # 글 사이 간격
DETAIL_SETTLE_MS = _int("DETAIL_SETTLE_MS", 1800)   # 상세 페이지 로딩 대기
DETAIL_SCROLLS = _int("DETAIL_SCROLLS", 3)          # 상세 페이지에서 스크롤 횟수
RUN_BUDGET_MIN = _int("RUN_BUDGET_MIN", 120)        # 1회 실행 총 상한 (0 = 무제한)
RECYCLE_EVERY = _int("RECYCLE_EVERY", 40)           # N건마다 탭 재생성 (메모리 정리)
STALE_JOB_MIN = _int("STALE_JOB_MIN", 15)           # 이 시간 이상 소식 없으면 죽은 작업
MEDIA_TIMEOUT_SEC = _int("MEDIA_TIMEOUT_SEC", 90)   # 글 1건의 이미지 저장 상한
WATCHDOG_MIN = _int("WATCHDOG_MIN", 10)             # 진행이 이만큼 멈추면 worker 재시작
DOWNLOAD_MEDIA = _bool("DOWNLOAD_MEDIA", True)
SKIP_REPLIES = _bool("SKIP_REPLIES", True)          # 남의 글에 달린 '댓글'을 저장했으면 제외

# ── 증분 수집 ───────────────────────────────────────────────────────────
# 저장됨 목록은 '최근 저장이 위'이므로, 이미 가진 글이 연속으로 나오면 거기서 멈춘다.
INCREMENTAL = _bool("INCREMENTAL", True)
STOP_AFTER_KNOWN = _int("STOP_AFTER_KNOWN", 1)      # 이미 가진 글이 N개 연속이면 중단
# 목록 응답의 thread_items 묶음을 신뢰한다 = 상세 페이지를 열지 않는다
TRUST_LIST_THREADS = _bool("TRUST_LIST_THREADS", True)
# 0 으로 두면 어떤 경우에도 상세 페이지를 열지 않는다 (최대 속도, 이어쓴 글 일부 포기)
FETCH_DETAIL = _bool("FETCH_DETAIL", True)
# 목록 응답에 본문이 다 있고 댓글이 0개면 상세 페이지를 열지 않는다 (건당 5~10초 절약)
SKIP_DETAIL_WHEN_NO_REPLIES = _bool("SKIP_DETAIL_WHEN_NO_REPLIES", True)
INCREMENTAL_MIN_POSTS = _int("INCREMENTAL_MIN_POSTS", 30)  # 이만큼은 모아야 증분 시작
FULL_SWEEP_DAYS = _int("FULL_SWEEP_DAYS", 7)        # N일에 한 번은 끝까지 훑어 확인
HEADLESS = _bool("HEADLESS", True)

USER_AGENT = os.getenv(
    "USER_AGENT",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
)

MEDIA_DIR.mkdir(parents=True, exist_ok=True)
