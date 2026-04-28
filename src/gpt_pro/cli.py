import argparse
import asyncio
import fcntl
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

from playwright.async_api import async_playwright

PROFILE = Path.home() / ".gpt-pro-profile"
STATE = Path.home() / ".gpt-pro"
RUNS = STATE / "runs"
BROWSER_LOCK = STATE / "browser.lock"
SESSION_COOKIE_PREFIX = "__Secure-next-auth.session-token"
RUN_ID_RE = re.compile(r"^[A-Za-z0-9._-]+$")
RUN_ID_MAX_LEN = 100
DEFAULT_GENERATION_TIMEOUT = 35 * 60

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


def stderr_jsonl(obj: dict) -> None:
    print(json.dumps(obj, separators=(",", ":")), file=sys.stderr, flush=True)


def log_stage(stage: str, **kwargs) -> None:
    """JSONL progress line to the worker's stderr (which is captured to worker.stderr)."""
    obj = {"ts": round(time.time(), 3), "stage": stage, **kwargs}
    print(json.dumps(obj, separators=(",", ":")), file=sys.stderr, flush=True)


def _kill_chrome_orphans() -> None:
    """Kill any stale Chrome procs still bound to our profile.

    Safe to call while holding the file lock — no other gpt-pro worker is
    allowed to be launching Chrome, so anything matching is an orphan from a
    SIGKILL'd or crashed previous worker. Without this, the next Chrome launch
    fails with SingletonLock.
    """
    try:
        out = subprocess.run(
            ["pgrep", "-f", f"user-data-dir={PROFILE}"],
            capture_output=True, text=True, timeout=5,
        ).stdout
    except Exception:
        return
    pids = [p for p in out.split() if p.strip()]
    if not pids:
        return
    log_stage("orphan_kill_term", pids=pids)
    try:
        subprocess.run(["kill", "-TERM", *pids], capture_output=True, timeout=5)
    except Exception:
        pass
    time.sleep(0.5)
    try:
        stubborn = [p for p in subprocess.run(
            ["pgrep", "-f", f"user-data-dir={PROFILE}"],
            capture_output=True, text=True, timeout=5,
        ).stdout.split() if p.strip()]
    except Exception:
        stubborn = []
    if stubborn:
        log_stage("orphan_kill_kill", pids=stubborn)
        try:
            subprocess.run(["kill", "-KILL", *stubborn], capture_output=True, timeout=5)
        except Exception:
            pass


def atomic_write(path: Path, content: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content)
    os.replace(tmp, path)


def gen_run_id() -> str:
    return time.strftime("%Y%m%d-%H%M%S") + "-ask"


def validate_run_id(s: str) -> None:
    if not s or len(s) > RUN_ID_MAX_LEN or not RUN_ID_RE.match(s):
        raise SystemExit(f"invalid run_id: {s!r}")


def new_run_dir(label: str) -> Path:
    d = RUNS / f"{time.strftime('%Y%m%d-%H%M%S')}-{label}"
    d.mkdir(parents=True, exist_ok=True)
    return d


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


# ---- doctor ----

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


# ---- login ----

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


# ---- ask: parent-side submit + wait ----

def _spawn_worker(run_id: str, run_dir: Path) -> None:
    worker_stdout = (run_dir / "worker.stdout").open("ab")
    worker_stderr = (run_dir / "worker.stderr").open("ab")
    subprocess.Popen(
        [sys.executable, "-m", "gpt_pro.cli", "_run", run_id],
        stdin=subprocess.DEVNULL,
        stdout=worker_stdout,
        stderr=worker_stderr,
        start_new_session=True,
        close_fds=True,
    )


async def _wait_for_result(run_dir: Path, *, poll_interval: float = 1.5, timeout: float | None = None) -> dict | None:
    """Polls run_dir/result.json until it appears or timeout. Returns parsed dict, or None on timeout."""
    result_path = run_dir / "result.json"
    deadline = (time.time() + timeout) if timeout is not None else None
    while True:
        if result_path.exists():
            try:
                return json.loads(result_path.read_text())
            except json.JSONDecodeError:
                await asyncio.sleep(0.1)
                continue
        if deadline is not None and time.time() >= deadline:
            return None
        await asyncio.sleep(poll_interval)


def _emit_terminal(result: dict, run_dir: Path) -> int:
    status = result.get("status", "error")
    if status == "ok":
        response_path = run_dir / "response.md"
        if response_path.exists():
            sys.stdout.write(response_path.read_text())
            sys.stdout.flush()
    stderr_jsonl(result)
    if status == "ok":
        return 0
    if status == "timeout":
        return 3
    return 1


async def cmd_ask(args) -> int:
    prompt_text = sys.stdin.read()
    if not prompt_text.strip():
        stderr_jsonl({"status": "error", "reason": "empty_prompt"})
        return 2

    run_id = args.run_id or gen_run_id()
    validate_run_id(run_id)
    run_dir = RUNS / run_id
    prompt_sha = hashlib.sha256(prompt_text.encode()).hexdigest()

    spawn_worker = True
    if run_dir.exists():
        meta_path = run_dir / "meta.json"
        if meta_path.exists():
            try:
                meta = json.loads(meta_path.read_text())
            except json.JSONDecodeError:
                meta = {}
            existing_sha = meta.get("prompt_sha256")
            if existing_sha == prompt_sha:
                stderr_jsonl({
                    "status": "submitted",
                    "run_id": run_id,
                    "run_dir": str(run_dir),
                    "prompt_sha256": prompt_sha,
                    "attached": True,
                })
                spawn_worker = False
            elif existing_sha is not None:
                stderr_jsonl({
                    "status": "error",
                    "reason": "run_id_conflict",
                    "run_id": run_id,
                    "run_dir": str(run_dir),
                })
                return 2

    if spawn_worker:
        run_dir.mkdir(parents=True, exist_ok=True)
        atomic_write(run_dir / "prompt.md", prompt_text)
        meta = {
            "run_id": run_id,
            "created_at": time.time(),
            "prompt_sha256": prompt_sha,
        }
        atomic_write(run_dir / "meta.json", json.dumps(meta))
        stderr_jsonl({
            "status": "submitted",
            "run_id": run_id,
            "run_dir": str(run_dir),
            "prompt_sha256": prompt_sha,
        })
        _spawn_worker(run_id, run_dir)

    result = await _wait_for_result(run_dir, timeout=args.generation_timeout)
    if result is None:
        stderr_jsonl({
            "status": "pending",
            "reason": "wait_timeout",
            "run_id": run_id,
            "run_dir": str(run_dir),
        })
        return 124
    return _emit_terminal(result, run_dir)


# ---- fetch ----

async def cmd_fetch(args) -> int:
    validate_run_id(args.run_id)
    run_dir = RUNS / args.run_id
    if not run_dir.exists():
        stderr_jsonl({"status": "error", "reason": "not_found", "run_id": args.run_id})
        return 4
    result = await _wait_for_result(run_dir, poll_interval=args.poll_interval, timeout=args.timeout)
    if result is None:
        stderr_jsonl({
            "status": "pending",
            "reason": "fetch_timeout",
            "run_id": args.run_id,
            "run_dir": str(run_dir),
        })
        return 124
    return _emit_terminal(result, run_dir)


# ---- _run: detached worker driving Chrome ----

async def _log_response(resp, log: list) -> None:
    try:
        if "/backend-api/" in resp.url or "/conversation" in resp.url:
            log.append({"ts": time.time(), "url": resp.url, "status": resp.status})
    except Exception:
        pass


async def _copy_button_extract(page) -> str | None:
    """Click the last assistant message's Copy button and read system clipboard.

    Preserves markdown fidelity (math, code fences, tables) where innerText mangles them.
    Returns None if the copy didn't change the clipboard (button missing, permission denied,
    not on macOS, etc.) — caller should fall back to innerText.
    """
    try:
        before = subprocess.run(["pbpaste"], capture_output=True, text=True, timeout=5).stdout
    except Exception:
        return None

    clicked = False
    try:
        clicked = await page.evaluate("""() => {
            const msgs = document.querySelectorAll('[data-message-author-role="assistant"]');
            const last = msgs[msgs.length - 1];
            if (!last) return false;
            const container = last.closest('[data-testid^="conversation-turn"]') || last.parentElement;
            if (!container) return false;
            const btn = container.querySelector('[data-testid="copy-turn-action-button"]');
            if (!btn) return false;
            btn.click();
            return true;
        }""")
    except Exception:
        return None
    if not clicked:
        return None

    await asyncio.sleep(0.6)
    try:
        after = subprocess.run(["pbpaste"], capture_output=True, text=True, timeout=5).stdout
    except Exception:
        return None
    if after and after != before and after.strip():
        return after
    return None


class BrowserLock:
    """Serializes access to the Chrome profile across worker processes via fcntl flock."""
    def __init__(self, path: Path = BROWSER_LOCK):
        self.path = path
        self._fd = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fd = open(self.path, "w")
        fcntl.flock(self._fd.fileno(), fcntl.LOCK_EX)
        _kill_chrome_orphans()
        return self

    def __exit__(self, *_):
        try:
            fcntl.flock(self._fd.fileno(), fcntl.LOCK_UN)
        finally:
            self._fd.close()
            self._fd = None


async def _browser_run(run_id: str, run_dir: Path, prompt_text: str) -> dict:
    network_log: list = []

    def err(reason: str, extra: dict | None = None) -> dict:
        d = {
            "status": "error",
            "reason": reason,
            "run_id": run_id,
            "run_dir": str(run_dir),
            "exit_code": 1,
        }
        if extra:
            d.update(extra)
        return d

    log_stage("start", run_id=run_id)
    lock_wait_start = time.time()
    with BrowserLock():
        lock_wait_secs = time.time() - lock_wait_start
        log_stage("lock_acquired", waited_secs=round(lock_wait_secs, 2))
        return await _run_with_browser(run_id, run_dir, prompt_text, network_log, err)


async def _run_with_browser(run_id, run_dir, prompt_text, network_log, err) -> dict:
    try:
        async with async_playwright() as pw:
            ctx = await pw.chromium.launch_persistent_context(**launch_kwargs())
            page = ctx.pages[0] if ctx.pages else await ctx.new_page()
            page.on("response", lambda r: asyncio.create_task(_log_response(r, network_log)))
            log_stage("chrome_launched")

            # NOTE: We tried `?temporary-chat=true` to skip history clutter, but
            # the picker in that mode doesn't expose `model-switcher-gpt-5-5-pro`
            # — Pro isn't selectable from a temp chat. Memory is disabled at the
            # account level instead.
            await page.goto("https://chatgpt.com/", wait_until="domcontentloaded")
            if not await wait_for_login(ctx, timeout=30.0):
                await page.screenshot(path=str(run_dir / "error-needs_reauth.png"), full_page=True)
                (run_dir / "error.html").write_text(await page.content())
                log_stage("error", reason="needs_reauth")
                return err("needs_reauth")
            log_stage("logged_in")

            picker = page.locator('[data-testid="model-switcher-dropdown-button"]')
            pro = page.locator('[data-testid="model-switcher-gpt-5-5-pro"]')

            await picker.click()
            await page.wait_for_selector('[role="menu"]', timeout=5000)
            already_pro = await pro.get_attribute("aria-checked") == "true"
            if not already_pro:
                await pro.click()
                await asyncio.sleep(0.5)
                await picker.click()
                await page.wait_for_selector('[role="menu"]', timeout=5000)
                if await pro.get_attribute("aria-checked") != "true":
                    await page.screenshot(path=str(run_dir / "error-model_select_failed.png"), full_page=True)
                    (run_dir / "error.html").write_text(await page.content())
                    log_stage("error", reason="model_select_failed")
                    return err("model_select_failed", {"aria_checked": await pro.get_attribute("aria-checked")})
            await page.keyboard.press("Escape")
            await asyncio.sleep(0.5)
            log_stage("model_selected", clicked=not already_pro)

            composer = page.get_by_role("textbox").first
            await composer.click()
            await composer.fill(prompt_text)
            log_stage("prompt_typed", chars=len(prompt_text))

            if await page.locator('[aria-label*="Extended Pro"]').count() == 0:
                await page.screenshot(path=str(run_dir / "error-reasoning_mismatch.png"), full_page=True)
                (run_dir / "error.html").write_text(await page.content())
                log_stage("error", reason="reasoning_mismatch")
                return err("reasoning_mismatch")

            await page.screenshot(path=str(run_dir / "pre-send.png"), full_page=True)
            await page.locator('[data-testid="send-button"]').click()
            send_ts = time.time()
            log_stage("sent")

            deadline = time.time() + DEFAULT_GENERATION_TIMEOUT
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

            log_stage(
                "completion_detected" if completed else "completion_timeout",
                chars=len(last_text),
                elapsed_secs=round(time.time() - send_ts, 1),
            )
            await page.screenshot(path=str(run_dir / "final.png"), full_page=True)
            (run_dir / "final.html").write_text(await page.content())

            extraction = "innertext"
            response = last_text
            if completed:
                copied = await _copy_button_extract(page)
                if copied is not None:
                    response = copied
                    extraction = "copy_button"
            log_stage("extracted", method=extraction, chars=len(response))
            atomic_write(run_dir / "response.md", response)

            result = {
                "status": "ok" if completed else "timeout",
                "run_id": run_id,
                "url": page.url,
                "run_dir": str(run_dir),
                "response_chars": len(response),
                "extraction": extraction,
                "exit_code": 0 if completed else 3,
            }
            log_stage("finished", status=result["status"])
            return result
    except Exception as e:
        log_stage("error", reason="worker_exception", exception=f"{type(e).__name__}: {e}")
        try:
            await page.screenshot(path=str(run_dir / "error-worker_exception.png"), full_page=True)
            (run_dir / "error.html").write_text(await page.content())
        except Exception:
            pass
        return {
            "status": "error",
            "reason": "worker_exception",
            "exception": f"{type(e).__name__}: {e}",
            "run_id": run_id,
            "run_dir": str(run_dir),
            "exit_code": 1,
        }
    finally:
        try:
            (run_dir / "network.json").write_text(json.dumps(network_log, indent=2))
        except Exception:
            pass


async def cmd_run(args) -> int:
    validate_run_id(args.run_id)
    run_dir = RUNS / args.run_id
    prompt_path = run_dir / "prompt.md"
    if not run_dir.exists() or not prompt_path.exists():
        result = {
            "status": "error",
            "reason": "missing_prompt",
            "run_id": args.run_id,
            "run_dir": str(run_dir),
            "exit_code": 1,
        }
        if run_dir.exists():
            atomic_write(run_dir / "result.json", json.dumps(result))
        return 1

    prompt_text = prompt_path.read_text()
    result = await _browser_run(args.run_id, run_dir, prompt_text)
    atomic_write(run_dir / "result.json", json.dumps(result))
    return result.get("exit_code", 1)


# ---- main ----

def main() -> int:
    p = argparse.ArgumentParser(prog="gpt-pro")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("login", help="Open Chrome on chatgpt.com to sign in. Cookies persist for `ask`.")
    sub.add_parser("doctor", help="Verify the profile is logged in. Prints JSON; saves screenshot + HTML.")

    ask_p = sub.add_parser("ask", help="Send a prompt from stdin to ChatGPT Pro Extended. Prints response on stdout when ready.")
    ask_p.add_argument("--run-id", default=None,
                      help="Caller-supplied run id. Same id + same prompt attaches to an in-progress run.")
    ask_p.add_argument("--generation-timeout", type=float, default=DEFAULT_GENERATION_TIMEOUT,
                      help="Max seconds the parent will wait for completion (default 2100).")

    fetch_p = sub.add_parser("fetch", help="Fetch the response of an existing run by id. Waits if still running.")
    fetch_p.add_argument("run_id")
    fetch_p.add_argument("--timeout", type=float, default=None,
                        help="Max seconds to wait. Default infinite. 0 = non-blocking check.")
    fetch_p.add_argument("--poll-interval", type=float, default=1.5)

    run_p = sub.add_parser("_run", help=argparse.SUPPRESS)
    run_p.add_argument("run_id")

    args = p.parse_args()
    if args.cmd == "login":
        return asyncio.run(cmd_login())
    if args.cmd == "doctor":
        return asyncio.run(cmd_doctor())
    if args.cmd == "ask":
        return asyncio.run(cmd_ask(args))
    if args.cmd == "fetch":
        return asyncio.run(cmd_fetch(args))
    if args.cmd == "_run":
        return asyncio.run(cmd_run(args))
    return 1


if __name__ == "__main__":
    sys.exit(main())
