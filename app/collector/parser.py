"""Threads 웹이 주고받는 GraphQL/JSON 응답에서 글 항목을 뽑아내는 순수 파서.

DOM 구조는 자주 바뀌지만 내부 JSON(인스타그램 계열 media 오브젝트)의 모양은
비교적 안정적이라 이쪽을 1순위로 사용한다. 실패 시 scraper 쪽 DOM 폴백이 동작한다.
"""
from __future__ import annotations

from typing import Any, Iterable

POST_KEYS = ("caption", "text_post_app_info", "image_versions2", "carousel_media")


def looks_like_post(node: dict) -> bool:
    if not isinstance(node, dict):
        return False
    if not node.get("code"):
        return False
    user = node.get("user")
    if not isinstance(user, dict) or not user.get("username"):
        return False
    return any(k in node for k in POST_KEYS)


def walk_posts(node: Any, out: list[dict] | None = None) -> list[dict]:
    """중첩 JSON을 훑으며 글처럼 생긴 오브젝트를 모두 수집."""
    out = [] if out is None else out
    if isinstance(node, dict):
        if looks_like_post(node):
            out.append(node)
        for v in node.values():
            walk_posts(v, out)
    elif isinstance(node, list):
        for v in node:
            walk_posts(v, out)
    return out


def walk_threads(node: Any, out: list[list[dict]] | None = None) -> list[list[dict]]:
    """`thread_items` 묶음을 '한 스레드'로 보존한 채 수집한다.

    Threads 목록 응답은 글 하나를 thread_items 배열로 준다. 작성자가 이어서 쓴 글이
    있으면 그 배열에 같이 들어 있다 — 즉 목록만으로 전문을 만들 수 있고,
    글마다 상세 페이지를 여는 것은 이미 받은 데이터를 버리고 다시 받는 셈이다.
    """
    out = [] if out is None else out
    if isinstance(node, dict):
        items = node.get("thread_items")
        if isinstance(items, list) and items:
            group = []
            for it in items:
                if not isinstance(it, dict):
                    continue
                post = it.get("post") if isinstance(it.get("post"), dict) else it
                if looks_like_post(post):
                    group.append(post)
            if group:
                out.append(group)
        for v in node.values():
            walk_threads(v, out)
    elif isinstance(node, list):
        for v in node:
            walk_threads(v, out)
    return out


def _fragment_text(node: dict) -> str:
    """caption 이 비어 있는 글의 본문. 주로 긴 글이 이 형태로 온다.

    Threads는 어느 정도 길이를 넘는 글의 본문을 caption 이 아니라
    text_post_app_info.snippet_attachment_info.text_fragments 에 조각으로 넣어 보낸다.
    여기를 안 읽으면 그런 글은 '본문 없는 빈 글'로 저장된다.
    """
    tpai = node.get("text_post_app_info")
    if not isinstance(tpai, dict):
        return ""
    snippet = tpai.get("snippet_attachment_info") or {}
    if not isinstance(snippet, dict):
        return ""
    fragments = (snippet.get("text_fragments") or {}).get("fragments") or []
    parts = [(f.get("plaintext") or "").strip()
             for f in fragments if isinstance(f, dict)]
    return "\n".join(p for p in parts if p).strip()


def _caption_text(node: dict) -> str:
    cap = node.get("caption")
    if isinstance(cap, dict) and (cap.get("text") or "").strip():
        return cap["text"].strip()
    if isinstance(cap, str) and cap.strip():
        return cap.strip()
    return _fragment_text(node)


def _best_image(candidates: Iterable[dict]) -> str | None:
    best, best_w = None, -1
    for c in candidates or []:
        if not isinstance(c, dict) or not c.get("url"):
            continue
        w = int(c.get("width") or 0)
        if w > best_w:
            best, best_w = c["url"], w
    return best


def extract_media(node: dict) -> list[dict]:
    """본문 이미지/영상 목록 추출 (캐러셀 포함)."""
    out: list[dict] = []

    def one(n: dict) -> None:
        vids = n.get("video_versions")
        if isinstance(vids, list) and vids:
            url = vids[0].get("url") if isinstance(vids[0], dict) else None
            if url:
                thumb = _best_image((n.get("image_versions2") or {}).get("candidates") or [])
                out.append({"kind": "video", "url": url, "alt": n.get("accessibility_caption"),
                            "poster": thumb})
                return
        iv = n.get("image_versions2") or {}
        url = _best_image(iv.get("candidates") or [])
        if url:
            out.append({"kind": "image", "url": url, "alt": n.get("accessibility_caption")})

    carousel = node.get("carousel_media")
    if isinstance(carousel, list) and carousel:
        for c in carousel:
            if isinstance(c, dict):
                one(c)
    else:
        one(node)

    # 링크 미리보기 썸네일도 이미지로 취급
    tpai = node.get("text_post_app_info") or {}
    lpa = tpai.get("link_preview_attachment") or {}
    if isinstance(lpa, dict) and lpa.get("image_url") and not out:
        out.append({"kind": "image", "url": lpa["image_url"], "alt": lpa.get("title")})

    seen, uniq = set(), []
    for m in out:
        if m["url"] in seen:
            continue
        seen.add(m["url"])
        uniq.append(m)
    return uniq


def normalize(node: dict) -> dict:
    """원시 노드 → 내부 표준 형태."""
    user = node.get("user") or {}
    username = user.get("username") or ""
    code = node.get("code") or ""
    tpai = node.get("text_post_app_info") or {}
    reply_to = (tpai.get("reply_to_author") or {}) if isinstance(tpai, dict) else {}
    lpa = tpai.get("link_preview_attachment") or {} if isinstance(tpai, dict) else {}

    return {
        "id": code,
        "pk": str(node.get("pk") or node.get("id") or ""),
        "url": f"https://www.threads.com/@{username}/post/{code}" if username and code else "",
        "author": username,
        "author_name": user.get("full_name") or "",
        "author_pic": user.get("profile_pic_url") or "",
        "posted_at": int(node.get("taken_at") or 0),
        "body": _caption_text(node),
        "like_count": int(node.get("like_count") or 0),
        # 값이 아예 없으면 '모름'(None) — 상세 페이지를 열지 말지 판단할 때 구분해야 한다
        "reply_count": (int(tpai.get("direct_reply_count") or 0)
                        if isinstance(tpai, dict) and "direct_reply_count" in tpai else None),
        "media": extract_media(node),
        "reply_to": reply_to.get("username") or "",
        "link": lpa.get("url") or lpa.get("display_url") or "",
    }


def dedupe(items: Iterable[dict]) -> list[dict]:
    """같은 글이 여러 응답에 반복 등장 → 정보가 가장 풍부한 것만 남긴다."""
    best: dict[str, dict] = {}
    for it in items:
        key = it.get("id") or it.get("pk")
        if not key:
            continue
        cur = best.get(key)
        if cur is None:
            best[key] = it
            continue
        score_new = len(it.get("body") or "") + 10 * len(it.get("media") or [])
        score_cur = len(cur.get("body") or "") + 10 * len(cur.get("media") or [])
        if score_new > score_cur:
            best[key] = it
    return list(best.values())


def build_thread(items: list[dict], root_code: str) -> dict:
    """상세 페이지에서 모은 항목들로 '본문 + 작성자가 이어서 쓴 댓글'을 구성."""
    by_code = {i["id"]: i for i in items if i.get("id")}
    root = by_code.get(root_code)
    if root is None:
        # 코드 매칭 실패 시 가장 오래된 글을 루트로 간주
        ordered = sorted([i for i in items if i.get("posted_at")], key=lambda x: x["posted_at"])
        root = ordered[0] if ordered else (items[0] if items else {})
    author = root.get("author")

    continuation = [
        i for i in items
        if i.get("id") != root.get("id")
        and i.get("author") == author
        and (not i.get("reply_to") or i.get("reply_to") == author)
        and (i.get("posted_at") or 0) >= (root.get("posted_at") or 0)
    ]
    continuation.sort(key=lambda x: (x.get("posted_at") or 0, x.get("id") or ""))

    segments = [{"kind": "root", "author": author, "text": root.get("body", ""),
                 "posted_at": root.get("posted_at")}]
    for c in continuation:
        if not (c.get("body") or c.get("media")):
            continue
        segments.append({"kind": "reply", "author": c.get("author"), "text": c.get("body", ""),
                         "posted_at": c.get("posted_at")})

    media = list(root.get("media") or [])
    for c in continuation:
        media.extend(c.get("media") or [])
    seen, uniq_media = set(), []
    for m in media:
        if m["url"] in seen:
            continue
        seen.add(m["url"])
        uniq_media.append(m)

    thread_text = "\n\n".join(s["text"] for s in segments[1:] if s.get("text"))
    full_text = "\n\n".join(s["text"] for s in segments if s.get("text"))

    out = dict(root)
    out["segments"] = segments
    out["media"] = uniq_media
    out["thread_text"] = thread_text
    out["full_text"] = full_text
    return out
