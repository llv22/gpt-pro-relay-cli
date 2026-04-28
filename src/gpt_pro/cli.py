import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

from playwright.async_api import async_playwright

PROFILE = Path.home() / ".gpt-pro-profile"
RUNS = Path.home() / ".gpt-pro" / "runs"
CHROME_ARGS = [
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-blink-features=AutomationControlled",
]


def launch_kwargs() -> dict:
    return dict(
        user_data_dir=str(PROFILE),
        channel="chrome",
        headless=False,
        no_viewport=True,
        args=CHROME_ARGS,
        ignore_default_args=["--enable-automation", "--no-sandbox"],
    )


SESSION_COOKIE_PREFIX = "__Secure-next-auth.session-token"


async def is_logged_in(ctx) -> bool:
    cookies = await ctx.cookies("https://chatgpt.com/")
    return any(c["name"].startswith(SESSION_COOKIE_PREFIX) for c in cookies)


async def wait_for_login(ctx, *, timeout: float = 600.0) -> bool:
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        try:
            if await is_logged_in(ctx):
                return True
        except Exception:
            return False
        await asyncio.sleep(1.0)
    return False


def new_run_dir(label: str) -> Path:
    d = RUNS / f"{time.strftime('%Y%m%d-%H%M%S')}-{label}"
    d.mkdir(parents=True, exist_ok=True)
    return d


async def cmd_doctor() -> int:
    run_dir = new_run_dir("doctor")
    async with async_playwright() as pw:
        ctx = await pw.chromium.launch_persistent_context(**launch_kwargs())
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        await page.goto("https://chatgpt.com/", wait_until="domcontentloaded")
        ok = await wait_for_login(ctx, timeout=30.0)
        await page.screenshot(path=str(run_dir / "page.png"), full_page=True)
        (run_dir / "page.html").write_text(await page.content())
        picker_status = "skipped"
        if ok:
            try:
                await page.locator('[data-testid="model-switcher-dropdown-button"]').click()
                await page.wait_for_selector('[role="menu"], [role="listbox"]', timeout=5000)
                await asyncio.sleep(0.5)
                await page.screenshot(path=str(run_dir / "picker.png"), full_page=True)
                (run_dir / "picker.html").write_text(await page.content())
                await page.keyboard.press("Escape")
                picker_status = "ok"
            except Exception as e:
                picker_status = f"failed: {type(e).__name__}: {e}"
        result = {
            "status": "ok" if ok else "needs_reauth",
            "url": page.url,
            "picker": picker_status,
            "run_dir": str(run_dir),
        }
        await ctx.close()
    print(json.dumps(result, indent=2))
    return 0 if ok else 1


async def cmd_ask() -> int:
    prompt_text = sys.stdin.read()
    if not prompt_text.strip():
        print(json.dumps({"status": "error", "reason": "empty_prompt"}), file=sys.stderr)
        return 2

    run_dir = new_run_dir("ask")
    (run_dir / "prompt.md").write_text(prompt_text)
    network_log: list = []

    async with async_playwright() as pw:
        ctx = await pw.chromium.launch_persistent_context(**launch_kwargs())
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()

        async def on_response(resp):
            try:
                if "/backend-api/" in resp.url or "/conversation" in resp.url:
                    network_log.append({"ts": time.time(), "url": resp.url, "status": resp.status})
            except Exception:
                pass

        page.on("response", lambda r: asyncio.create_task(on_response(r)))

        async def fail(reason: str, extra: dict | None = None) -> int:
            try:
                await page.screenshot(path=str(run_dir / f"error-{reason}.png"), full_page=True)
                (run_dir / "error.html").write_text(await page.content())
            except Exception:
                pass
            (run_dir / "network.json").write_text(json.dumps(network_log, indent=2))
            err = {"status": "error", "reason": reason, "run_dir": str(run_dir)}
            if extra:
                err.update(extra)
            print(json.dumps(err, indent=2), file=sys.stderr)
            await ctx.close()
            return 1

        await page.goto("https://chatgpt.com/", wait_until="domcontentloaded")
        if not await wait_for_login(ctx, timeout=30.0):
            return await fail("needs_reauth")

        async def open_picker():
            await page.locator('[data-testid="model-switcher-dropdown-button"]').click()
            await page.wait_for_selector('[role="menu"]', timeout=5000)

        async def pro_checked() -> str | None:
            return await page.locator(
                '[data-testid="model-switcher-gpt-5-5-pro"]'
            ).get_attribute("aria-checked")

        await open_picker()
        if await pro_checked() != "true":
            await page.locator('[data-testid="model-switcher-gpt-5-5-pro"]').click()
            await asyncio.sleep(0.5)
            await open_picker()
            if await pro_checked() != "true":
                return await fail("model_select_failed", {"aria_checked": await pro_checked()})
        await page.keyboard.press("Escape")
        await asyncio.sleep(0.5)

        composer = page.get_by_role("textbox").first
        await composer.click()
        await composer.fill(prompt_text)

        if await page.locator('[aria-label*="Extended Pro"]').count() == 0:
            return await fail("reasoning_mismatch")

        await page.screenshot(path=str(run_dir / "pre-send.png"), full_page=True)
        await page.locator('[data-testid="send-button"]').click()

        deadline = time.time() + 35 * 60
        last_text = ""
        last_change = time.time()
        snapshot_idx = 0
        next_snap = time.time() + 5.0
        completed = False
        while time.time() < deadline:
            now = time.time()
            if now >= next_snap:
                await page.screenshot(path=str(run_dir / f"streaming-{snapshot_idx:03d}.png"), full_page=True)
                snapshot_idx += 1
                next_snap = now + 30.0
            try:
                cur = await page.evaluate(
                    """() => {
                        const e = document.querySelectorAll('[data-message-author-role="assistant"]');
                        return e.length ? e[e.length - 1].innerText : '';
                    }"""
                )
            except Exception:
                cur = ""
            if cur != last_text:
                last_change = now
                last_text = cur
            if cur and (now - last_change) >= 5.0:
                stop = await page.locator('button[aria-label*="Stop"], [data-testid*="stop"]').count()
                if stop == 0:
                    completed = True
                    break
            await asyncio.sleep(1.5)

        await page.screenshot(path=str(run_dir / "final.png"), full_page=True)
        (run_dir / "final.html").write_text(await page.content())
        (run_dir / "network.json").write_text(json.dumps(network_log, indent=2))
        (run_dir / "response.md").write_text(last_text)

        result = {
            "status": "ok" if completed else "timeout",
            "url": page.url,
            "run_dir": str(run_dir),
            "response_chars": len(last_text),
        }
        await ctx.close()

    if completed:
        print(last_text)
    print(json.dumps(result, indent=2), file=sys.stderr)
    return 0 if completed else 3


async def cmd_login() -> int:
    async with async_playwright() as pw:
        ctx = await pw.chromium.launch_persistent_context(**launch_kwargs())
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        await page.goto("https://chatgpt.com/")
        print(f"Chrome launched against {PROFILE}", file=sys.stderr)
        print("Sign in to ChatGPT in the window. Login auto-detects.", file=sys.stderr)
        ok = await wait_for_login(ctx)
        print("Login detected." if ok else "Timed out without detecting login.", file=sys.stderr)
        await ctx.close()
    return 0 if ok else 1


def main() -> int:
    p = argparse.ArgumentParser(prog="gpt-pro")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("login", help="Open Chrome on chatgpt.com to sign in. Cookies persist for `ask`.")
    sub.add_parser("doctor", help="Verify the profile is logged in. Prints JSON; saves screenshot + HTML.")
    sub.add_parser("ask", help="Send a prompt from stdin to ChatGPT Pro Extended; print response on stdout.")
    args = p.parse_args()
    if args.cmd == "login":
        return asyncio.run(cmd_login())
    if args.cmd == "doctor":
        return asyncio.run(cmd_doctor())
    if args.cmd == "ask":
        return asyncio.run(cmd_ask())
    return 1


if __name__ == "__main__":
    sys.exit(main())
