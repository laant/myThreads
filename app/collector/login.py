"""최초 1회 Threads 로그인 → Playwright storage_state 저장.

기본: 컨테이너 안에서 실제 브라우저를 띄우고 noVNC로 접속해 직접 로그인.
    docker compose --profile login up login
    → http://localhost:6080/vnc.html?autoconnect=1&resize=scale

대안: 브라우저 확장(Cookie-Editor 등)으로 내보낸 쿠키 JSON 사용
    python -m app.collector.login --from-cookies /data/cookies.json
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from playwright.async_api import async_playwright

from .. import config

LOGIN_URL = "https://www.threads.com/login"
TIMEOUT_SEC = 15 * 60


async def _has_session(ctx) -> bool:
    cookies = await ctx.cookies()
    names = {c["name"] for c in cookies if "threads" in c.get("domain", "") or "instagram" in c.get("domain", "")}
    return "sessionid" in names


async def interactive() -> int:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=False,
            args=["--no-sandbox", "--disable-dev-shm-usage", "--start-maximized",
                  "--disable-blink-features=AutomationControlled"],
        )
        ctx = await browser.new_context(
            locale="ko-KR", timezone_id="Asia/Seoul",
            user_agent=config.USER_AGENT,
            viewport={"width": 1360, "height": 900},
        )
        page = await ctx.new_page()
        await page.goto(LOGIN_URL, wait_until="domcontentloaded")

        print("\n" + "=" * 68)
        print("  브라우저에서 Threads에 로그인해 주세요.")
        print("  접속 주소: http://localhost:6080/vnc.html?autoconnect=1&resize=scale")
        print("  로그인이 끝나면 자동으로 세션을 저장하고 종료합니다.")
        print("=" * 68 + "\n", flush=True)

        waited = 0
        while waited < TIMEOUT_SEC:
            await asyncio.sleep(3)
            waited += 3
            try:
                if await _has_session(ctx):
                    # 저장됨 페이지까지 열려야 진짜 로그인 완료
                    await page.goto(config.SAVED_URL, wait_until="domcontentloaded")
                    await page.wait_for_timeout(3000)
                    if "/login" not in page.url:
                        config.STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
                        await ctx.storage_state(path=str(config.STATE_PATH))
                        print(f"✅ 로그인 세션을 저장했습니다: {config.STATE_PATH}", flush=True)
                        await browser.close()
                        return 0
            except Exception:
                pass
            if waited % 30 == 0:
                print(f"…로그인 대기 중 ({waited}s)", flush=True)

        print("⛔ 시간이 초과되었습니다. 다시 시도해 주세요.", flush=True)
        await browser.close()
        return 1


async def from_cookies(path: Path) -> int:
    """Cookie-Editor / EditThisCookie 형식의 JSON 배열을 storage_state로 변환."""
    raw = json.loads(path.read_text(encoding="utf-8"))
    cookies = raw.get("cookies") if isinstance(raw, dict) else raw
    out = []
    for c in cookies:
        domain = c.get("domain") or ".threads.com"
        if not domain.startswith("."):
            domain = "." + domain.lstrip(".")
        out.append({
            "name": c["name"],
            "value": c["value"],
            "domain": domain,
            "path": c.get("path", "/"),
            "expires": float(c.get("expirationDate") or c.get("expires") or -1),
            "httpOnly": bool(c.get("httpOnly", False)),
            "secure": bool(c.get("secure", True)),
            "sameSite": {"no_restriction": "None", "lax": "Lax", "strict": "Strict"}.get(
                str(c.get("sameSite", "")).lower(), "Lax"
            ),
        })
    state = {"cookies": out, "origins": []}
    config.STATE_PATH.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")

    # 실제로 통하는지 확인
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True, args=["--no-sandbox"])
        ctx = await browser.new_context(storage_state=str(config.STATE_PATH),
                                        user_agent=config.USER_AGENT, locale="ko-KR")
        page = await ctx.new_page()
        await page.goto(config.SAVED_URL, wait_until="domcontentloaded", timeout=45000)
        await page.wait_for_timeout(3000)
        ok = "/login" not in page.url
        await browser.close()
    if ok:
        print(f"✅ 쿠키로 세션을 만들었습니다: {config.STATE_PATH}")
        return 0
    print("⛔ 쿠키로 로그인 확인에 실패했습니다. sessionid 쿠키가 포함됐는지 확인해 주세요.")
    return 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--from-cookies", type=Path, default=None)
    args = ap.parse_args()
    if args.from_cookies:
        return asyncio.run(from_cookies(args.from_cookies))
    return asyncio.run(interactive())


if __name__ == "__main__":
    sys.exit(main())
