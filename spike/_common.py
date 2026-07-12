"""Shared helpers for the headless-Linux feasibility spike.

These scripts are THROWAWAY diagnostics, not part of the shipped tool. They
exist to settle the two questions a code read cannot (see spike/README.md):

  Gate 1 (auth):  does headless Chrome + the ~/.gpt-pro-profile get served
                  ChatGPT Pro at all, or does OpenAI's anti-abuse redirect to
                  an auth error?
  Gate 2 (io):    can we deliver a multi-hundred-KB prompt into ProseMirror and
                  extract clean markdown WITHOUT the macOS pasteboard?

Everything imports the *real* selectors/logic from gpt_pro.cli so the spike
can't drift from production. The one thing this module adds is the headless
launch + the in-browser clipboard I/O that replaces pbcopy/pbpaste/Meta+V.

The production tool uses `open -a` + connect-over-CDP shared Chrome; the spike
deliberately uses `launch_persistent_context` (one process owns the profile).
That is the minimal isolation of the two risks — the shared-Chrome/multi-tab
concurrency machinery is orthogonal to "does headless work at all", so it is
intentionally out of scope here.
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path

from playwright.async_api import async_playwright

# Reuse production truth so the spike tests exactly what the tool relies on.
from gpt_pro.cli import (  # noqa: E402
    PROFILE,
    PRO_MODEL_SLUGS,
    SOL_MODEL_TOKEN,
    DEFAULT_GOTO_TIMEOUT_MS,
    is_logged_in,
    is_pro_label,
    read_composer_chip_text,
    read_selected_model,
    classify_model_status,
    served_assistant_model_slug,
    _copy_button_present,
)

# The contenteditable ProseMirror root. Production clicks it via
# get_by_role("textbox").first; we also need a CSS handle for JS dispatch.
COMPOSER_CSS = "#prompt-textarea"
SEND_READY_SELECTOR = (
    '[data-testid="send-button"]:not([disabled]):not([aria-disabled="true"]), '
    'button[aria-label="Send prompt"]:not([disabled]):not([aria-disabled="true"]), '
    'button[aria-label="Send message"]:not([disabled]):not([aria-disabled="true"])'
)
SEND_MOUNTED_SELECTOR = (
    '[data-testid="send-button"], '
    'button[aria-label="Send prompt"], '
    'button[aria-label="Send message"]'
)
COPY_BUTTON_JS = """() => {
    const msgs = document.querySelectorAll('[data-message-author-role="assistant"]');
    const last = msgs[msgs.length - 1];
    if (!last) return false;
    const container = last.closest('[data-testid^="conversation-turn"]') || last.parentElement;
    if (!container) return false;
    const btn = container.querySelector('[data-testid="copy-turn-action-button"]');
    if (!btn) return false;
    btn.click();
    return true;
}"""

CHATGPT_URL = "https://chatgpt.com/"
CHATGPT_ORIGIN = "https://chatgpt.com"

# Mirror of gpt_pro.cli.CHROME_OPEN_ARGS minus the macOS window pin
# (--window-size, which fights a real GUI window; headless uses viewport).
# The anti-detection + cookie-persistence flags are load-bearing per CLAUDE.md.
SPIKE_CHROME_ARGS = [
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-blink-features=AutomationControlled",
    "--password-store=basic",
    "--use-mock-keychain",
    "--disable-features=DestroyProfileOnBrowserClose,DialMediaRouteProvider,MediaRouter,Translate,HttpsUpgrades,PaintHolding",
]

SPIKE_RUNS = Path.home() / ".gpt-pro" / "spike"


def _channel() -> str:
    # Real Chrome per the invariant. Override only for local experiments.
    return os.environ.get("GPT_PRO_SPIKE_CHANNEL", "chrome")


def _extra_args() -> list[str]:
    """Chrome on Linux refuses to launch as root / in most Docker containers
    without --no-sandbox. CLAUDE.md keeps that flag OFF by default (anti-detection),
    so prefer running as a NON-root user where the userns sandbox works. This
    escape hatch is opt-in only, for a box where you can't avoid root:
        GPT_PRO_SPIKE_NO_SANDBOX=1 uv run python spike/01_auth_probe.py
    """
    if os.environ.get("GPT_PRO_SPIKE_NO_SANDBOX") == "1":
        return ["--no-sandbox"]
    return []


def spike_run_dir(name: str) -> Path:
    d = SPIKE_RUNS / f"{time.strftime('%Y%m%d-%H%M%S')}-{name}"
    d.mkdir(parents=True, exist_ok=True)
    return d


def log(obj: dict) -> None:
    print(json.dumps(obj, separators=(",", ":"), default=str), flush=True)


class HeadlessSession:
    """Async context manager: a headless (or headed) persistent Chrome context
    bound to the production profile, with clipboard permissions granted.

    Usage:
        async with HeadlessSession(headless=True) as (ctx, page):
            ...
    """

    def __init__(self, *, headless: bool = True, viewport=(1280, 800)):
        self.headless = headless
        self.viewport = viewport
        self._pw = None
        self.ctx = None

    async def __aenter__(self):
        self._pw = await async_playwright().start()
        w, h = self.viewport
        self.ctx = await self._pw.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE),
            headless=self.headless,
            channel=_channel(),
            args=[*SPIKE_CHROME_ARGS, *_extra_args()],
            viewport={"width": w, "height": h},
        )
        # Chrome's IN-PROCESS clipboard (no OS pasteboard in headless) — this is
        # what makes navigator.clipboard.{writeText,readText} usable as the
        # pbcopy/pbpaste replacement.
        try:
            await self.ctx.grant_permissions(
                ["clipboard-read", "clipboard-write"], origin=CHATGPT_ORIGIN
            )
        except Exception as e:
            log({"stage": "grant_permissions_failed", "error": f"{type(e).__name__}: {e}"})
        page = self.ctx.pages[0] if self.ctx.pages else await self.ctx.new_page()
        return self.ctx, page

    async def __aexit__(self, *exc):
        try:
            if self.ctx is not None:
                await self.ctx.close()
        finally:
            if self._pw is not None:
                await self._pw.stop()


async def goto_chatgpt(page) -> dict:
    """Navigate to chatgpt.com. Returns a dict describing where we landed —
    the key Gate-1 signal is whether OpenAI bounced us to an auth-error page.
    """
    err = None
    try:
        await page.goto(CHATGPT_URL, wait_until="domcontentloaded", timeout=DEFAULT_GOTO_TIMEOUT_MS)
    except Exception as e:
        err = f"{type(e).__name__}: {e}"
    await asyncio.sleep(2.0)  # let client-side redirects settle
    url = page.url
    lowered = url.lower()
    auth_redirect = any(
        tok in lowered for tok in ("/auth/error", "/auth/login", "/login", "auth0.openai", "/api/auth/error")
    )
    return {"final_url": url, "goto_error": err, "auth_redirect": auth_redirect}


# ---- paste strategies (the pbcopy+Cmd+V replacement) ----

async def _send_button_mounted(page, *, timeout_ms: int = 12_000) -> bool:
    """Ingestion signal: the send button only mounts once ProseMirror has
    non-empty content (production uses this exact gate in _focus_and_paste).
    For large prompts ChatGPT converts the paste to a 'Pasted text' attachment,
    so the composer's own innerText is NOT a reliable length check — the send
    button is."""
    try:
        await page.wait_for_selector(SEND_MOUNTED_SELECTOR, timeout=timeout_ms, state="visible")
        return True
    except Exception:
        return False


async def _clear_composer(page) -> None:
    try:
        await page.locator(COMPOSER_CSS).first.click()
        await page.keyboard.press("ControlOrMeta+a")
        await page.keyboard.press("Delete")
        await asyncio.sleep(0.2)
    except Exception:
        pass


async def paste_strategy_clipboard_ctrl_v(page, text: str) -> bool:
    """PRIMARY — closest analog to macOS pbcopy+Cmd+V.

    Write into Chrome's internal clipboard, focus the composer, real Ctrl+V.
    This hits ProseMirror's OPTIMIZED paste handler (the whole reason production
    rejected keyboard.insert_text for big inputs). navigator.clipboard.writeText
    needs document focus + clipboard-write permission — both arranged here.
    """
    await page.bring_to_front()
    await page.evaluate("t => navigator.clipboard.writeText(t)", text)
    await page.locator(COMPOSER_CSS).first.click()
    await page.keyboard.press("Control+v")
    return await _send_button_mounted(page)


async def paste_strategy_synthetic_event(page, text: str) -> bool:
    """FALLBACK — dispatch a synthetic `paste` ClipboardEvent with a DataTransfer.

    Modern Chrome honors clipboardData passed to the ClipboardEvent constructor;
    older engines null it out. If this works it needs no clipboard permission at
    all, but it's a weaker guarantee than a real Ctrl+V.
    """
    await page.locator(COMPOSER_CSS).first.click()
    await page.evaluate(
        """({sel, t}) => {
            const el = document.querySelector(sel);
            el.focus();
            const dt = new DataTransfer();
            dt.setData('text/plain', t);
            el.dispatchEvent(new ClipboardEvent('paste', {clipboardData: dt, bubbles: true, cancelable: true}));
        }""",
        {"sel": COMPOSER_CSS, "t": text},
    )
    return await _send_button_mounted(page)


async def paste_strategy_insert_text(page, text: str) -> bool:
    """LAST RESORT — CDP insertText. Production found this chokes ProseMirror on
    multi-hundred-KB inputs; included only so the spike can confirm/deny that on
    the target box."""
    await page.locator(COMPOSER_CSS).first.click()
    await page.keyboard.insert_text(text)
    return await _send_button_mounted(page)


PASTE_STRATEGIES = [
    ("clipboard_ctrl_v", paste_strategy_clipboard_ctrl_v),
    ("synthetic_paste_event", paste_strategy_synthetic_event),
    ("insert_text", paste_strategy_insert_text),
]


async def try_paste(page, text: str) -> dict:
    """Try each paste strategy in order until one gets the send button to mount.
    Returns {"winner": name|None, "attempts": [{name, ok, secs, error}]}."""
    attempts = []
    winner = None
    for name, fn in PASTE_STRATEGIES:
        await _clear_composer(page)
        t0 = time.time()
        ok = False
        err = None
        try:
            ok = await fn(page, text)
        except Exception as e:
            err = f"{type(e).__name__}: {e}"
        attempts.append({"strategy": name, "ok": ok, "secs": round(time.time() - t0, 2), "error": err})
        if ok:
            winner = name
            break
    return {"winner": winner, "attempts": attempts}


# ---- extraction (the pbpaste replacement) ----

async def extract_via_clipboard(page) -> str | None:
    """Copy-button → navigator.clipboard.readText(). The headless replacement for
    the macOS Copy → pbpaste path; preserves markdown fidelity (math/code/tables)
    that innerText mangles. Returns None if the button was absent or the read
    failed (caller falls back to innerText)."""
    try:
        clicked = await page.evaluate(COPY_BUTTON_JS)
    except Exception:
        clicked = False
    if not clicked:
        return None
    await asyncio.sleep(0.6)
    try:
        txt = await page.evaluate("() => navigator.clipboard.readText()")
    except Exception:
        return None
    return txt if (txt and txt.strip()) else None


async def latest_assistant_innertext(page) -> str:
    try:
        return await page.evaluate(
            """() => {
                const e = document.querySelectorAll('[data-message-author-role="assistant"]');
                return e.length ? e[e.length - 1].innerText : '';
            }"""
        )
    except Exception:
        return ""


def make_probe_prompt(kb: int) -> str:
    """A ~kb-KB prompt that (a) exercises the large-paste path and (b) ends with a
    tiny, cheap, deterministic ask so a real send costs minimal Pro reasoning and
    the answer is easy to eyeball. The bulk is filler the model is told to ignore."""
    marker = "SPIKE-ECHO-7F3A"
    filler_line = "This is large-prompt filler the assistant must ignore. " * 10 + "\n"
    body = filler_line * max(1, (kb * 1024) // len(filler_line.encode()))
    ask = (
        f"\n\n---\nIgnore all of the filler above. Reply with EXACTLY this token and nothing else: {marker}\n"
    )
    return body + ask
