"""UI 확인용 데모 데이터 주입 (AI 호출 없이 화면을 미리 볼 때 사용).

    DATA_DIR=./data python tests/seed_demo.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.db import db, init_db, now, replace_media, replace_segments, upsert_post  # noqa: E402

CATS = [
    ("marketing", "마케팅 인사이트", "브랜딩·카피·고객 심리에 대한 관찰", "#6366f1", "card"),
    ("dev-tips", "개발 팁", "코드·도구·자동화 관련 실전 팁", "#0ea5e9", "table"),
    ("food-biz", "외식업 운영", "매장 운영, 배달, 원가 관리 이야기", "#10b981", "card"),
    ("visual", "레퍼런스 이미지", "디자인·공간·메뉴판 등 시각 자료", "#f59e0b", "board"),
    ("etc", "기타", "다른 분류에 맞지 않는 글", "#94a3b8", "card"),
]

POSTS = [
    ("Ca1", "brand_note", "카피 한 줄이 매출을 바꾼다.\n'맛있는 김밥' 대신 '아침 7시에 싼 김밥'.",
     "구체적인 시간·장소·행동이 들어가면 사람은 장면을 상상한다.\n상상이 되면 지갑이 열린다.",
     "marketing", "구체적 카피가 상상을 만든다", ["카피", "브랜딩"], []),
    ("Ca2", "dev_kim", "도커 이미지 용량 90% 줄인 방법",
     "1. 멀티스테이지 빌드\n2. slim 베이스\n3. .dockerignore 정리\n실측 1.2GB → 130MB.",
     "dev-tips", "도커 이미지 다이어트 3단계", ["도커", "최적화"], []),
    ("Ca3", "sikdang_ceo", "배달앱 수수료 계산 다시 해봤습니다.",
     "객단가 18,000원 기준 실수령 62%.\n포장 유도 쿠폰이 결국 남는 장사.",
     "food-biz", "배달 수수료 실수령 62%", ["배달", "원가"], []),
    ("Ca4", "space_ref", "요즘 카페 메뉴판 트렌드",
     "", "visual", "메뉴판 레이아웃 레퍼런스", ["메뉴판", "디자인"],
     ["https://placehold.co/600x800/6366f1/fff?text=menu+1", "https://placehold.co/600x600/0ea5e9/fff?text=menu+2"]),
    ("Ca5", "writer_j", "글이 안 써질 때 쓰는 문장 시작 틀 5개",
     "1. '나는 늘 ~라고 생각했다. 그런데'\n2. '이건 아무도 말 안 해주는데'\n…",
     "etc", "글 막힐 때 쓰는 도입부 틀", ["글쓰기"], []),
]


def main():
    init_db()
    with db() as conn:
        for i, (slug, name, desc, color, view) in enumerate(CATS):
            conn.execute(
                """INSERT INTO categories(slug,name,description,color,view,sort,created_at)
                   VALUES(?,?,?,?,?,?,?) ON CONFLICT(slug) DO UPDATE SET name=excluded.name""",
                (slug, name, desc, color, view, i, now()))
        cats = {r["slug"]: r["id"] for r in conn.execute("SELECT id,slug FROM categories")}

        for n, (pid, author, body, cont, cat, summary, tags, imgs) in enumerate(POSTS):
            full = body + ("\n\n" + cont if cont else "")
            upsert_post(conn, {
                "id": pid, "pk": str(1000 + n), "author": author, "author_name": "",
                "url": f"https://www.threads.com/@{author}/post/{pid}",
                "posted_at": now() - 86400 * (n + 1), "body": body, "thread_text": cont,
                "full_text": full, "like_count": 30 + n, "reply_count": 2, "detail_ok": 1,
            })
            segs = [{"kind": "root", "author": author, "text": body, "posted_at": now()}]
            if cont:
                segs.append({"kind": "reply", "author": author, "text": cont, "posted_at": now()})
            replace_segments(conn, pid, segs)
            replace_media(conn, pid, [{"kind": "image", "url": u} for u in imgs])
            import json as _json
            conn.execute(
                """INSERT INTO classification(post_id,category_id,confidence,summary,tags,model,
                       method,locked,updated_at) VALUES(?,?,?,?,?,'demo','ai',0,?)
                   ON CONFLICT(post_id) DO UPDATE SET category_id=excluded.category_id""",
                (pid, cats[cat], 0.9, summary, _json.dumps(tags, ensure_ascii=False), now()))
    print("데모 데이터 주입 완료")


if __name__ == "__main__":
    main()
