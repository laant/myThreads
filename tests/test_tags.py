"""사이드바 태그가 '누르면 그 숫자대로 나오는지' 확인 (네트워크 불필요).

예전에는 태그 숫자를 전체 기준으로 세면서 목록은 현재 카테고리 안으로
한정돼 있었다. 그래서 카테고리를 보는 중에 '일상 117' 을 눌러도 0건이
나왔다. 게다가 상위 40개만 잘라 보여주는데 동점에서 임의로 잘렸다.
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("DATA_DIR", "./data-test")

from app import config, main  # noqa: E402
from app.db import db, init_db, now, upsert_post  # noqa: E402

CATS = [("marketing", "SNS 노하우·마케팅"), ("devtools", "AI 개발·에이전트 툴")]
#  글id,  주분류,      보조분류,     태그
POSTS = [
    ("M1", "marketing", None, ["마케팅", "스레드"]),
    ("M2", "marketing", None, ["마케팅"]),
    ("D1", "devtools", "marketing", ["오픈소스", "마케팅"]),  # 마케팅에 '관련 글'로 걸림
    ("D2", "devtools", None, ["오픈소스", "일상"]),
    ("D3", "devtools", None, ["일상"]),
]


def _seed() -> None:
    if config.DB_PATH.exists():
        config.DB_PATH.unlink()
    init_db()
    import json
    ids = {}
    with db() as conn:
        for i, (slug, name) in enumerate(CATS):
            cur = conn.execute(
                "INSERT INTO categories(slug,name,description,color,view,sort,created_at) "
                "VALUES(?,?,'','#000','card',?,?)", (slug, name, i, now()))
            ids[slug] = int(cur.lastrowid)
        for pid, cat, sec, tags in POSTS:
            upsert_post(conn, {"id": pid, "author": "a", "posted_at": 1000,
                               "body": pid, "full_text": pid, "detail_ok": 1})
            conn.execute(
                "INSERT INTO classification(post_id,category_id,secondary_id,summary,tags,"
                "method,updated_at) VALUES(?,?,?,'',?,'ai',?)",
                (pid, ids[cat], ids[sec] if sec else None,
                 json.dumps(tags, ensure_ascii=False), now()))


def test_counts_match_what_you_get_when_you_click():
    """모든 카테고리 × 모든 태그: 사이드바 숫자 == 눌렀을 때 나오는 글 수."""
    _seed()
    for cat in ("all", "marketing", "devtools"):
        for t in main._tag_counts(None if cat == "all" else cat):
            rows = main._posts(category=None if cat == "all" else cat, tag=t["name"])
            assert t["count"] == len(rows), \
                f"[{cat}] {t['name']}: 숫자 {t['count']} vs 실제 {len(rows)}건"
    print("✓ 태그 숫자 = 눌렀을 때 나오는 글 수 (모든 카테고리)")


def test_category_scoped_tags():
    _seed()
    everything = {t["name"]: t["count"] for t in main._tag_counts()}
    assert everything == {"마케팅": 3, "일상": 2, "오픈소스": 2, "스레드": 1}, everything

    marketing = {t["name"]: t["count"] for t in main._tag_counts("marketing")}
    # D1 은 보조분류로 걸려 있으니 그 태그도 함께 보인다. '일상'은 없어야 한다.
    assert marketing == {"마케팅": 3, "스레드": 1, "오픈소스": 1}, marketing
    assert "일상" not in marketing, "이 카테고리에 없는 태그가 보인다 (눌러도 0건)"
    print("✓ 카테고리 안의 태그만, 그 안의 개수로")


def test_sorted_by_count_then_name():
    """동점일 때 이름순 — 같은 횟수인데 어떤 건 보이고 어떤 건 안 보이는 일이 없도록."""
    _seed()
    rows = main._tag_counts()
    assert [r["count"] for r in rows] == sorted([r["count"] for r in rows], reverse=True)
    ties = [r["name"] for r in rows if r["count"] == 2]
    assert ties == sorted(ties), ties
    print("✓ 많이 쓰인 순 · 동점이면 이름순")


def test_edge_cases():
    _seed()
    assert main._tag_counts("unclassified") == []
    assert main._tag_counts("없는카테고리") == []
    # 글이 지워지면 그 태그도 함께 사라져야 한다
    from app.db import delete_post
    delete_post("D3")
    assert {t["name"]: t["count"] for t in main._tag_counts()}.get("일상") == 1
    print("✓ 미분류·없는 카테고리·글 삭제 반영")


if __name__ == "__main__":
    test_counts_match_what_you_get_when_you_click()
    test_category_scoped_tags()
    test_sorted_by_count_then_name()
    test_edge_cases()
    print("태그 테스트 통과")
