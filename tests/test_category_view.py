"""카테고리를 클릭했을 때 남의 글이 섞이지 않는지 확인 (네트워크 불필요).

분류기는 글마다 주분류(category)와 보조분류(secondary)를 준다. 예전에는
카테고리를 누르면 둘 중 하나만 맞아도 같이 나와서, 주분류가 다른 글이
'다른 색 뱃지'를 달고 섞여 보였다. 이제는 주분류 글이 먼저 나오고
보조로 걸린 글은 related=1 로 표시돼 화면에서 '관련 글'로 갈린다.
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("DATA_DIR", "./data-test")

from app import config, main  # noqa: E402
from app.db import db, init_db, now, upsert_post  # noqa: E402

CATS = [("marketing", "SNS 노하우·마케팅"), ("devtools", "AI 개발·에이전트 툴")]
#  글id,  주분류,       보조분류,     작성시각
POSTS = [
    ("M1", "marketing", None, 3000),
    ("M2", "marketing", "devtools", 2000),
    ("D1", "devtools", "marketing", 1000),   # 마케팅에는 '관련 글'로만 나와야 한다
    ("D2", "devtools", None, 500),
]


def _seed() -> dict[str, int]:
    if config.DB_PATH.exists():
        config.DB_PATH.unlink()
    init_db()
    ids = {}
    with db() as conn:
        for i, (slug, name) in enumerate(CATS):
            cur = conn.execute(
                "INSERT INTO categories(slug,name,description,color,view,sort,created_at) "
                "VALUES(?,?,'','#000','card',?,?)", (slug, name, i, now()))
            ids[slug] = int(cur.lastrowid)
        for pid, cat, sec, posted in POSTS:
            upsert_post(conn, {"id": pid, "author": "someone", "posted_at": posted,
                               "body": pid, "full_text": pid, "detail_ok": 1})
            conn.execute(
                "INSERT INTO classification(post_id,category_id,secondary_id,summary,tags,"
                "method,updated_at) VALUES(?,?,?,'','[]','ai',?)",
                (pid, ids[cat], ids[sec] if sec else None, now()))
    return ids


def test_primary_first_then_related():
    _seed()
    rows = main._posts(category="marketing")
    assert [r["id"] for r in rows] == ["M1", "M2", "D1"], [r["id"] for r in rows]
    assert [r["related"] for r in rows] == [0, 0, 1], "주/관련 구분이 틀렸다"

    own = [r for r in rows if not r["related"]]
    assert [r["id"] for r in own] == ["M1", "M2"]
    assert all(r["cat_slug"] == "marketing" for r in own), "주분류가 다른 글이 섞였다"
    print("✓ 주분류 글이 먼저, 보조로 걸린 글은 related=1")


def test_sidebar_count_matches_own_posts():
    """사이드바 숫자(주분류 기준)와 화면의 '내 글' 개수가 같아야 한다."""
    _seed()
    counts = {c["slug"]: c["count"] for c in main._categories()}
    for slug in ("marketing", "devtools"):
        own = [r for r in main._posts(category=slug) if not r["related"]]
        assert counts[slug] == len(own), f"{slug}: 사이드바 {counts[slug]} vs 목록 {len(own)}"
    print("✓ 사이드바 숫자 = 목록의 주분류 글 수")


def test_sort_applies_within_each_group():
    _seed()
    newest = [r["id"] for r in main._posts(category="marketing", sort="newest")]
    oldest = [r["id"] for r in main._posts(category="marketing", sort="oldest")]
    assert newest == ["M1", "M2", "D1"], newest      # 주분류 안에서 최신순
    assert oldest == ["M2", "M1", "D1"], oldest      # 정렬이 바뀌어도 관련 글은 뒤에
    print("✓ 정렬은 각 구역 안에서만 적용되고 관련 글은 항상 뒤")


def test_other_views_unaffected():
    _seed()
    for kind in ("all", None):
        rows = main._posts(category=kind)
        assert len(rows) == len(POSTS)
        assert all(r["related"] == 0 for r in rows), "전체 보기엔 관련 글 구분이 없어야 한다"
    assert main._posts(category="unclassified") == []
    assert [r["id"] for r in main._posts(q="D1")] == ["D1"]
    print("✓ 전체·미분류·검색 보기는 영향 없음")


if __name__ == "__main__":
    test_primary_first_then_related()
    test_sidebar_count_matches_own_posts()
    test_sort_applies_within_each_group()
    test_other_views_unaffected()
    print("카테고리 보기 테스트 통과")
