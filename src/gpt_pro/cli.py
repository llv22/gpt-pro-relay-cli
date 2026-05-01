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
DEFAULT_GENERATION_TIMEOUT = 60 * 60
MAX_PROMPT_BYTES = 1_000_000

CHROME_ARGS = [
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-blink-features=AutomationControlled",
    # Force a real composited window. Without this the OS can hand Chrome a
    # zero-area / occluded window, which means no layout frame is ever produced
    # — the symptom is a white window plus every click failing with
    # "element is outside of the viewport" and screenshots timing out.
    "--window-size=1280,800",
]


def launch_kwargs() -> dict:
    return dict(
        user_data_dir=str(PROFILE),
        channel="chrome",
        headless=False,
        # Don't let Playwright drive viewport at launch time. Setting `viewport=`
        # makes Playwright issue Browser.getWindowForTarget + setWindowBounds
        # during context init, which races under --remote-debugging-pipe on
        # Chrome 147+ ("Browser window not found") and is not retryable on the
        # second-to-tens-of-seconds scale. Instead, OS window size is pinned via
        # --window-size, and the renderer viewport is pinned post-launch via a
        # direct CDP Emulation.setDeviceMetricsOverride (see pin_viewport_cdp).
        no_viewport=True,
        args=CHROME_ARGS,
        ignore_default_args=["--enable-automation", "--no-sandbox"],
    )


async def pin_viewport_cdp(context, page, *, width: int = 1280, height: int = 800) -> None:
    """Pin renderer viewport via direct CDP, bypassing the racy launch-time path.

    setDeviceMetricsOverride only affects renderer-level emulation — no
    Browser.getWindowForTarget call, no window-bounds dance, no race. Result:
    getBoundingClientRect / window.innerWidth track our pinned viewport, so
    Playwright's "outside of viewport" clickability check stays accurate even
    if the OS window state ever drifts.

    Call this only AFTER a real navigation. The initial about:blank target in a
    persistent context rejects setDeviceMetricsOverride with "Target does not
    support metrics override" (observed on Chrome 147 + persistent profile).
    Best-effort: log and continue on failure rather than killing the run —
    the OS-level --window-size still gives the renderer a sane default, and
    the override is belt-and-suspenders for unstable window states.
    """
    try:
        cdp = await context.new_cdp_session(page)
        await cdp.send("Emulation.setDeviceMetricsOverride", {
            "width": width, "height": height, "deviceScaleFactor": 1, "mobile": False,
        })
    except Exception as e:
        log_stage("pin_viewport_skipped", exception=f"{type(e).__name__}: {e}")


def _find_chrome_browser_pid() -> int | None:
    """Return the PID of the gpt-pro Chrome BROWSER process — the parent that
    owns the Cocoa window. Helper/renderer processes carry --type= in argv and
    don't own windows; activating them is a no-op.
    """
    try:
        out = subprocess.run(
            ["pgrep", "-fl", f"user-data-dir={PROFILE}"],
            capture_output=True, text=True, timeout=5,
        ).stdout
    except Exception:
        return None
    for line in out.splitlines():
        try:
            pid_str, cmd = line.split(maxsplit=1)
        except ValueError:
            continue
        if "--type=" not in cmd:
            return int(pid_str)
    return None


async def activate_chrome_for_paint(page) -> None:
    """Force Chrome's window onto a real CoreAnimation compositor surface.

    Chrome on macOS displays web content via a BrowserCompositorCALayerTree
    attached to the Cocoa view. When the worker is launched from a detached
    Popen (sshd → start_new_session=True → no AppKit activation), Chrome can
    skip the LaunchServices/AppKit foreground path and never bind a visible
    CA surface. DOM, CDP, and clicks keep working — but Page.captureScreenshot
    waits forever for a frame, and a human watcher sees a white window.

    PID-targeted activation: a bundle-wide `open -b com.google.Chrome` is
    ambiguous when the user has another Chrome running (interactive +
    gpt-pro share the bundle but are separate processes). Resolve the gpt-pro
    Chrome browser PID via pgrep, then activate that specific NSRunningApplication
    via JXA — `NSRunningApplication.activateWithOptions:` doesn't need
    Accessibility permission and won't get redirected to the wrong instance.
    Then `page.bring_to_front()` focuses the automation target so the first
    navigation creates a visible surface. Best-effort.
    """
    if sys.platform == "darwin":
        pid = _find_chrome_browser_pid()
        if pid is None:
            log_stage("chrome_activation_skipped", reason="browser_pid_not_found")
        else:
            jxa = (
                'ObjC.import("AppKit");'
                f'$.NSRunningApplication.runningApplicationWithProcessIdentifier({pid})'
                '.activateWithOptions(2);'
            )
            try:
                subprocess.run(
                    ["/usr/bin/osascript", "-l", "JavaScript", "-e", jxa],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    timeout=5, check=True,
                )
                log_stage("chrome_activated", pid=pid)
            except Exception as e:
                log_stage("chrome_activation_skipped", exception=f"{type(e).__name__}: {e}")
    try:
        await page.bring_to_front()
    except Exception as e:
        log_stage("page_bring_to_front_skipped", exception=f"{type(e).__name__}: {e}")


def stderr_jsonl(obj: dict) -> None:
    print(json.dumps(obj, separators=(",", ":")), file=sys.stderr, flush=True)


def log_stage(stage: str, **kwargs) -> None:
    """JSONL progress line to the worker's stderr (which is captured to worker.stderr)."""
    obj = {"ts": round(time.time(), 3), "stage": stage, **kwargs}
    print(json.dumps(obj, separators=(",", ":")), file=sys.stderr, flush=True)


async def safe_screenshot(page, path: Path, *, timeout_ms: int = 10_000) -> None:
    """Best-effort diagnostic screenshot. Never propagate failure.

    Screenshots are artifacts, not part of the critical path. A renderer that's
    busy (e.g. mid-paste reflow on a large prompt) can stall page.screenshot
    long enough to blow Playwright's default 30s timeout — this once killed an
    otherwise-healthy run before Send was even clicked. Bail fast (10s) and let
    the run continue; record the skip in worker.stderr for diagnostics.
    """
    try:
        await page.screenshot(path=str(path), full_page=True, timeout=timeout_ms)
    except Exception as e:
        log_stage("screenshot_skipped", path=path.name, exception=f"{type(e).__name__}: {e}")


# Backoff schedule (seconds) between launch retries. Chosen empirically from
# run ask-20260501T070205Z-call4-chunk1: two consecutive launches 1s apart
# both lost the Browser.getWindowForTarget race, but a fresh worker 31s later
# launched cleanly. The race window can outlast a couple of seconds, so back
# off generously instead of hammering. Worst case ~17s of waiting.
LAUNCH_RETRY_BACKOFF_SECS = (2.0, 5.0, 10.0)


async def launch_chrome_with_retry(pw):
    """Launch the persistent Chrome context, retrying on a known launch race.

    With viewport={...} set, Playwright issues CDP `Browser.getWindowForTarget`
    against the initial about:blank target during persistent-context init.
    Under `--remote-debugging-pipe` (Playwright's default), Chrome 147+ has a
    race where the host window isn't yet registered against the target when
    Playwright queries it, returning "Browser window not found" and aborting
    the launch. Retry with backoff; between attempts force-kill any Chrome
    children still bound to the profile, since Playwright's "graceful close"
    of the failed parent doesn't always wait for renderer/helper teardown
    and a partially-alive process tree can poison the next attempt's window
    registration. Other exceptions propagate immediately.
    """
    attempts = len(LAUNCH_RETRY_BACKOFF_SECS) + 1
    for attempt in range(attempts):
        try:
            return await pw.chromium.launch_persistent_context(**launch_kwargs())
        except Exception as e:
            if "Browser.getWindowForTarget" not in str(e) or attempt >= attempts - 1:
                raise
            log_stage("launch_retry", attempt=attempt, exception=f"{type(e).__name__}: {e}")
            _kill_chrome_orphans()
            await asyncio.sleep(LAUNCH_RETRY_BACKOFF_SECS[attempt])


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
    # 4 hex chars from os.urandom prevent collision when two `ask` calls fire
    # in the same wall-clock second without --run-id.
    return time.strftime("%Y%m%d-%H%M%S") + "-" + os.urandom(2).hex() + "-ask"


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


# Composer chip combines model + reasoning. Extended reasoning is gated to Pro
# models, so any label containing "Extended" verifies both axes — same fail-closed
# guarantee as the old (model picker + reasoning chip) pair. We match by predicate
# rather than exact string because ChatGPT renders this label inconsistently:
# observed values include "Extended" and "Extended Pro" (varies with A/B tests
# and/or the chip's responsive truncation classes — `max-w-40 truncate` and
# `[[data-collapse-labels]_&]:sr-only`). Either is correct.
COMPOSER_CHIP = 'button.__composer-pill[aria-haspopup="menu"]'
EXTENDED_TOKEN = "Extended"


def is_pro_extended_label(text: str | None) -> bool:
    """Predicate: chip text unambiguously indicates Pro + Extended reasoning.

    Requires *both* "Extended" and "Pro" tokens. The chip exposes no model-axis
    signal beyond the visible label (no aria-label, no dataset, no hidden mirror
    that differs from innerText), so a label of just "Extended" is ambiguous —
    it could be Pro+Extended (truncated) or Thinking+Extended. Demanding "Pro"
    too closes that fail-closed gap. The post-click confirmation in
    ensure_extended_chip can be looser since we know which submenu we picked.
    """
    return bool(text) and EXTENDED_TOKEN in text and "Pro" in text


SSR_CHIP_PLACEHOLDER = "Model"  # Server-rendered text before React hydrates the user's actual selection.


async def read_composer_chip_text(page, *, timeout: float = 30.0) -> str:
    """Read the composer chip's text after React hydration.

    The chip's SSR text is 'Model'; hydration replaces it with the user's
    selected mode ('Extended', 'Extended Pro', 'Auto', etc.). We poll until the
    placeholder is gone — reading too early would cause a self-correction click
    on an unhydrated chip, which doesn't open the menu.
    """
    chip = page.locator(COMPOSER_CHIP).first
    await chip.wait_for(state="visible", timeout=timeout * 1000)
    deadline = time.time() + timeout
    last = ""
    while time.time() < deadline:
        last = (await chip.inner_text()).strip()
        if last and last != SSR_CHIP_PLACEHOLDER:
            return last
        await asyncio.sleep(0.2)
    return last


# The Effort submenu trigger inside the chip menu. Two "Effort" buttons share
# the same accessible name (one per model row) — disambiguation is by testid.
# The button is rendered as a "trailing button" with Tailwind `invisible` and
# its container is `pointer-events-none` until the parent row is hovered, so
# Playwright's hover/click won't reach it. We bypass with a synthetic
# HTMLElement.click() via evaluate() — Radix's submenu state is wired to the
# click handler, not pointer events, so the synthetic click opens it cleanly.
PRO_EFFORT_TRIGGER_TESTID = "model-switcher-gpt-5-5-pro-thinking-effort"


async def ensure_extended_chip(page, *, run_dir: Path) -> tuple[bool, str | None]:
    """Make the composer chip read an Extended-reasoning label. Returns (ok, observed_text).

    Idempotent: if the chip already reads an Extended label we no-op. Otherwise
    we open the chip menu, synthetically click the Pro Effort submenu trigger
    (it's hidden behind a group-hover affordance — see PRO_EFFORT_TRIGGER_TESTID
    docstring), then click the "Extended" leaf in the resulting submenu. The
    submenu's leaves use role='menuitemradio' (not menuitem).
    """
    chip = page.locator(COMPOSER_CHIP).first
    text = await read_composer_chip_text(page, timeout=30.0)
    if is_pro_extended_label(text):
        return True, text

    await chip.click()
    try:
        await page.wait_for_selector('[role="menu"]', timeout=5000)
    except Exception as e:
        await safe_screenshot(page, run_dir / "error-chip_menu_open.png")
        (run_dir / "error.html").write_text(await page.content())
        log_stage("error", reason="chip_menu_open_failed", exception=f"{type(e).__name__}: {e}")
        return False, text

    # Capture the menu-count baseline so we can detect the *new* menu the
    # trigger click mounts (avoids racing with unrelated portal menus that may
    # already be mounted on the page).
    baseline_menu_count = await page.evaluate(
        "() => document.querySelectorAll('[role=\"menu\"]').length"
    )

    trigger = page.locator(f'[data-testid="{PRO_EFFORT_TRIGGER_TESTID}"]').first
    try:
        await trigger.wait_for(state="attached", timeout=3000)
        await trigger.evaluate("el => el.click()")
    except Exception as e:
        await safe_screenshot(page, run_dir / "error-chip_pro_trigger.png")
        (run_dir / "error.html").write_text(await page.content())
        log_stage("error", reason="pro_effort_trigger_click_failed", exception=f"{type(e).__name__}: {e}")
        await page.keyboard.press("Escape")
        return False, text

    try:
        await page.wait_for_function(
            f"document.querySelectorAll('[role=\"menu\"]').length > {baseline_menu_count}",
            timeout=3000,
        )
    except Exception as e:
        await safe_screenshot(page, run_dir / "error-chip_submenu_open.png")
        (run_dir / "error.html").write_text(await page.content())
        log_stage("error", reason="pro_effort_submenu_open_failed", exception=f"{type(e).__name__}: {e}")
        await page.keyboard.press("Escape")
        return False, text

    # Scope the Extended lookup to the newest-mounted menu (the submenu the
    # trigger just opened). Leaves are role='menuitemradio'. Anchor the regex
    # so future variants like "Extended+" or "Extended (beta)" don't silently
    # match — those would be intentional product changes worth reviewing.
    submenu = page.locator('[role="menu"]').last
    item = submenu.get_by_role("menuitemradio", name=re.compile(rf"^{EXTENDED_TOKEN}$"))
    try:
        await item.first.click(timeout=5000)
    except Exception as e:
        await safe_screenshot(page, run_dir / "error-chip_menuitem.png")
        (run_dir / "error.html").write_text(await page.content())
        log_stage("error", reason="chip_menuitem_missing", exception=f"{type(e).__name__}: {e}")
        await page.keyboard.press("Escape")
        return False, text

    # Poll up to 5s for the chip text to update. The post-click confirmation is
    # looser than the fast-path predicate: we already navigated through Pro's
    # submenu, so any "Extended"-containing label proves the click took effect.
    deadline = time.time() + 5.0
    final_text = text
    while time.time() < deadline:
        final_text = (await chip.inner_text()).strip()
        if EXTENDED_TOKEN in final_text:
            return True, final_text
        await asyncio.sleep(0.2)
    return False, final_text


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
        ctx = await launch_chrome_with_retry(pw)
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        await activate_chrome_for_paint(page)
        await page.goto("https://chatgpt.com/", wait_until="domcontentloaded")
        await pin_viewport_cdp(ctx, page)
        ok = await wait_for_login(ctx, timeout=30.0)
        await page.screenshot(path=str(run_dir / "page.png"), full_page=True)
        (run_dir / "page.html").write_text(await page.content())
        chip_status = "skipped"
        chip_text = None
        if ok:
            try:
                chip_text = await read_composer_chip_text(page, timeout=10.0)
                chip_status = "ok" if is_pro_extended_label(chip_text) else f"unexpected: {chip_text!r}"
            except Exception as e:
                chip_status = f"failed: {type(e).__name__}: {e}"
        result = {
            "status": "ok" if ok else "needs_reauth",
            "url": page.url,
            "chip": chip_status,
            "chip_text": chip_text,
            "run_dir": str(run_dir),
        }
        await ctx.close()
    print(json.dumps(result, indent=2))
    return 0 if ok else 1


# ---- login ----

async def cmd_login() -> int:
    async with async_playwright() as pw:
        ctx = await launch_chrome_with_retry(pw)
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        await activate_chrome_for_paint(page)
        await page.goto("https://chatgpt.com/")
        await pin_viewport_cdp(ctx, page)
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


async def _wait_for_result(run_dir: Path, *, poll_interval: float = 0.5, timeout: float | None = None) -> dict | None:
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


def _emit_terminal(result: dict, run_dir: Path, output_path: Path | None = None) -> int:
    status = result.get("status", "error")
    if status == "ok":
        response_path = run_dir / "response.md"
        if response_path.exists():
            content = response_path.read_text()
            if output_path is not None:
                resolved = output_path.expanduser()
                resolved.parent.mkdir(parents=True, exist_ok=True)
                atomic_write(resolved, content)
                result = {**result, "output": str(resolved)}
            else:
                sys.stdout.write(content)
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

    prompt_bytes = len(prompt_text.encode())
    if prompt_bytes > MAX_PROMPT_BYTES:
        stderr_jsonl({
            "status": "error",
            "reason": "prompt_too_large",
            "bytes": prompt_bytes,
            "limit": MAX_PROMPT_BYTES,
        })
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
        _spawn_worker(run_id, run_dir)
        stderr_jsonl({
            "status": "submitted",
            "run_id": run_id,
            "run_dir": str(run_dir),
            "prompt_sha256": prompt_sha,
        })

    if args.no_wait:
        return 0

    result = await _wait_for_result(run_dir, timeout=args.generation_timeout)
    if result is None:
        stderr_jsonl({
            "status": "pending",
            "reason": "wait_timeout",
            "run_id": run_id,
            "run_dir": str(run_dir),
        })
        return 124
    return _emit_terminal(result, run_dir, output_path=args.output)


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
    return _emit_terminal(result, run_dir, output_path=args.output)


# ---- _run: detached worker driving Chrome ----

async def _log_response(resp, log: list) -> None:
    try:
        if "/backend-api/" in resp.url or "/conversation" in resp.url:
            log.append({"ts": time.time(), "url": resp.url, "status": resp.status})
    except Exception:
        pass


async def _paste_prompt(page, prompt_text: str) -> None:
    """Paste prompt_text into the focused composer via the system clipboard.

    Playwright's keyboard.insert_text dispatches a single synthetic input event;
    ProseMirror reacts by re-rendering the entire document, which chokes on
    multi-hundred-KB inputs. Cmd+V hits the contenteditable's paste handler
    instead, which ChatGPT's UI is optimized for. Saves and restores the user's
    clipboard since the Mac mini may be in interactive use.
    """
    try:
        before = subprocess.run(["pbpaste"], capture_output=True, text=True, timeout=5).stdout
    except Exception:
        before = None
    try:
        subprocess.run(["pbcopy"], input=prompt_text, text=True, check=True, timeout=10)
        await page.keyboard.press("Meta+V")
    finally:
        if before is not None:
            try:
                subprocess.run(["pbcopy"], input=before, text=True, timeout=5)
            except Exception:
                pass


async def _copy_button_present(page) -> bool:
    """True if the latest assistant turn's post-completion Copy button is mounted.

    The turn-action toolbar (copy/regenerate/share) only renders after the turn
    is finalized — Pro Extended's mid-run "thinking summary" panel does not have
    it. Used as the affirmative completion gate alongside text-stable + no Stop
    button: the text-only heuristic false-positives because Pro can sit on a
    summary string for tens of seconds while reasoning continues silently with
    no Stop button visible.
    """
    try:
        return await page.evaluate("""() => {
            const msgs = document.querySelectorAll('[data-message-author-role="assistant"]');
            const last = msgs[msgs.length - 1];
            if (!last) return false;
            const container = last.closest('[data-testid^="conversation-turn"]') || last.parentElement;
            if (!container) return false;
            return !!container.querySelector('[data-testid="copy-turn-action-button"]');
        }""")
    except Exception:
        return False


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

    # Restore the user's clipboard if we changed it — the Mac mini may be in
    # interactive use and we shouldn't clobber whatever they had copied.
    if after != before:
        try:
            subprocess.run(["pbcopy"], input=before, text=True, timeout=5)
        except Exception:
            pass

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
    def worker_exception_result(e: Exception) -> dict:
        return {
            "status": "error",
            "reason": "worker_exception",
            "exception": f"{type(e).__name__}: {e}",
            "run_id": run_id,
            "run_dir": str(run_dir),
            "exit_code": 1,
        }

    try:
        async with async_playwright() as pw:
            ctx = await launch_chrome_with_retry(pw)
            # Inner try/except/finally guarantees ctx.close() runs while Chrome
            # is still alive — clean shutdown flushes the cookie/session SQLite.
            # Worker-exception screenshots also live inside the inner try so
            # they're captured before ctx is torn down.
            try:
                page = ctx.pages[0] if ctx.pages else await ctx.new_page()
                await activate_chrome_for_paint(page)
                page.on("response", lambda r: asyncio.create_task(_log_response(r, network_log)))
                log_stage("chrome_launched")

                await page.goto("https://chatgpt.com/", wait_until="domcontentloaded")
                await pin_viewport_cdp(ctx, page)
                if not await wait_for_login(ctx, timeout=30.0):
                    await safe_screenshot(page, run_dir / "error-needs_reauth.png")
                    (run_dir / "error.html").write_text(await page.content())
                    log_stage("error", reason="needs_reauth")
                    return err("needs_reauth")
                log_stage("logged_in")

                ok, chip_text = await ensure_extended_chip(page, run_dir=run_dir)
                if not ok:
                    await safe_screenshot(page, run_dir / "error-model_select_failed.png")
                    (run_dir / "error.html").write_text(await page.content())
                    log_stage("error", reason="model_select_failed", chip_text=chip_text)
                    return err("model_select_failed", {"chip_text": chip_text})
                log_stage("model_verified", chip_text=chip_text)

                composer = page.get_by_role("textbox").first
                await composer.click()
                await _paste_prompt(page, prompt_text)
                log_stage("prompt_typed", chars=len(prompt_text))

                await safe_screenshot(page, run_dir / "pre-send.png")
                send_btn = page.locator(
                    '[data-testid="send-button"], button[aria-label="Send prompt"], button[aria-label="Send message"]'
                ).first
                await send_btn.click()
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
                        await safe_screenshot(page, run_dir / f"streaming-{snapshot_idx:03d}.png")
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
                        if stop == 0 and await _copy_button_present(page):
                            completed = True
                            break
                    await asyncio.sleep(1.5)

                log_stage(
                    "completion_detected" if completed else "completion_timeout",
                    chars=len(last_text),
                    elapsed_secs=round(time.time() - send_ts, 1),
                )
                await safe_screenshot(page, run_dir / "final.png")
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
                return worker_exception_result(e)
            finally:
                try:
                    await ctx.close()
                except Exception:
                    pass
    except Exception as e:
        # Pre-page setup error (Chrome won't launch, etc). No page to screenshot.
        log_stage("error", reason="worker_exception", exception=f"{type(e).__name__}: {e}")
        return worker_exception_result(e)
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
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("login", help="Open Chrome on chatgpt.com to sign in. Cookies persist for `ask`.")
    sub.add_parser("doctor", help="Verify the profile is logged in. Prints JSON; saves screenshot + HTML.")

    ask_p = sub.add_parser("ask", help="Send a prompt from stdin to ChatGPT Pro Extended. Prints response on stdout when ready.")
    ask_p.add_argument("--run-id", default=None,
                      help="Caller-supplied run id. Same id + same prompt attaches to an in-progress run.")
    ask_p.add_argument("--generation-timeout", type=float, default=DEFAULT_GENERATION_TIMEOUT,
                      help="Max seconds the parent will wait for completion (default 3600).")
    ask_p.add_argument("--output", type=Path, default=None,
                      help="Write response to this file (on macmini) instead of stdout. Stderr JSONL is unchanged. Ignored with --no-wait.")
    ask_p.add_argument("--no-wait", action="store_true",
                      help="Submit (or attach to) the run and exit 0 immediately after `submitted`. Use `fetch` to retrieve the response. Designed for short-session SSH polling — see SKILL.md.")

    fetch_p = sub.add_parser("fetch", help="Fetch the response of an existing run by id. Waits if still running.")
    fetch_p.add_argument("run_id")
    fetch_p.add_argument("--timeout", type=float, default=None,
                        help="Max seconds to wait. Default infinite. 0 = non-blocking check.")
    fetch_p.add_argument("--poll-interval", type=float, default=0.5)
    fetch_p.add_argument("--output", type=Path, default=None,
                        help="Write response to this file (on macmini) instead of stdout. Stderr JSONL is unchanged.")

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
