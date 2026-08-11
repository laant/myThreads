"""본문 이미지 로컬 저장."""
from __future__ import annotations

import hashlib
import logging
import mimetypes
from pathlib import Path

import httpx

from .. import config

log = logging.getLogger("media")

HEADERS = {
    "User-Agent": config.USER_AGENT,
    "Referer": "https://www.threads.com/",
    "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
}


def _filename(post_id: str, url: str, ctype: str | None) -> str:
    h = hashlib.sha1(url.encode()).hexdigest()[:12]
    ext = mimetypes.guess_extension((ctype or "").split(";")[0].strip()) or ".jpg"
    if ext in (".jpe", ".jpeg"):
        ext = ".jpg"
    return f"{post_id}_{h}{ext}"


async def download_all(post_id: str, media: list[dict]) -> list[dict]:
    if not config.DOWNLOAD_MEDIA or not media:
        return media
    out = []
    async with httpx.AsyncClient(headers=HEADERS, timeout=30, follow_redirects=True) as client:
        for m in media:
            item = dict(m)
            url = m.get("url")
            if m.get("kind") == "video":
                url = m.get("poster") or url  # 영상은 썸네일만 저장
            if not url:
                out.append(item)
                continue
            try:
                r = await client.get(url)
                r.raise_for_status()
                name = _filename(post_id, url, r.headers.get("content-type"))
                path: Path = config.MEDIA_DIR / name
                path.write_bytes(r.content)
                item["local_path"] = f"media/{name}"
            except Exception as exc:
                log.debug("이미지 저장 실패 %s: %s", url, exc)
            out.append(item)
    return out
