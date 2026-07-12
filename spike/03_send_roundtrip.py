#!/usr/bin/env python3
"""Gate 2b — full end-to-end round-trip through headless Chrome.

The EXPENSIVE gate: paste → Send → wait for completion → extract via the
in-browser clipboard → audit the served slug. This spends ONE real Pro send, so
it is opt-in (requires --send) and runs a single small prompt by default.

It proves the three things the cheap gates can't:
  1. A headless send actually completes and is served by an allowlisted Pro slug
     (gpt-5-6-pro) — i.e. headless doesn't silently get downgraded.
  2. navigator.clipboard.readText() extraction returns the same clean markdown
     the Copy button produced (vs the innerText fallback).
  3. The completion gate (text-stable + no-Stop + Copy-button) fires headless.

    uv run python spike/03_send_roundtrip.py --send                # ~small Pro send
    uv run python spike/03_send_roundtrip.py --send --kb 300       # + large paste
    uv run python spike/03_send_roundtrip.py --send --headed

Without --send it refuses to run (guards against accidental Pro spend).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _common import (  # noqa: E402
    HeadlessSession,
    goto_chatgpt,
    spike_run_dir,
    log,
    is_logged_in,
    is_pro_label,
    read_composer_chip_text,
    try_paste,
    make_probe_prompt,
    extract_via_clipboard,
    latest_assistant_innertext,
    served_assistant_model_slug,
    _copy_button_present,
    PRO_MODEL_SLUGS,
    SEND_READY_SELECTOR,
    SEND_MOUNTED_SELECTOR,
    COMPOSER_CSS,
)

COMPLETION_TIMEOUT = 45 * 60  # generous; a Pro reason can take 5-20 min.


async def wait_for_completion(page, run_dir: Path) -> tuple[bool, str]:
    """Production completion gate: assistant text stable 5s + no Stop button +
    Copy button mounted on the latest turn."""
    deadline = time.time() + COMPLETION_TIMEOUT
    last_text = ""
    last_change = time.time()
    while time.time() < deadline:
        now = time.time()
        cur = await latest_assistant_innertext(page)
        if cur != last_text:
            last_text, last_change = cur, now
        if cur and (now - last_change) >= 5.0:
            stop = await page.locator('button[aria-label*="Stop"], [data-testid*="stop"]').count()
            if stop == 0 and await _copy_button_present(page):
                return True, last_text
        await asyncio.sleep(1.5)
    return False, last_text


async def main(headless: bool, kb: int, do_send: bool) -> int:
    if not do_send:
        log({"stage": "refused", "msg": "This spends a real Pro send. Pass --send to proceed."})
        return 2

    run_dir = spike_run_dir(f"roundtrip-{kb}kb")
    prompt = make_probe_prompt(kb)
    log({"stage": "start", "headless": headless, "kb": kb,
         "prompt_bytes": len(prompt.encode()), "run_dir": str(run_dir)})

    async with HeadlessSession(headless=headless) as (ctx, page):
        nav = await goto_chatgpt(page)
        log({"stage": "navigated", **nav})
        if not await is_logged_in(ctx) or nav["auth_redirect"]:
            log({"stage": "abort", "reason": "not_served_app_run_gate_1_first"})
            return 2

        await page.locator(COMPOSER_CSS).first.wait_for(state="visible", timeout=30_000)

        # Effort chip must read Pro before we spend a send (mirror production's
        # fail-closed pre-send check; the spike does NOT self-correct the chip).
        chip = await read_composer_chip_text(page, timeout=30.0)
        log({"stage": "chip_read", "text": chip, "is_pro": is_pro_label(chip)})
        if not is_pro_label(chip):
            log({"stage": "abort", "reason": "chip_not_pro",
                 "hint": "Set the composer to Pro effort once in a headed/seed session, then retry."})
            return 1

        paste = await try_paste(page, prompt)
        log({"stage": "paste_result", **paste})
        if not paste["winner"]:
            log({"stage": "abort", "reason": "paste_failed_run_gate_2a"})
            return 1

        # Wait for any 'Pasted text' attachment upload to finish (send enabled).
        try:
            await page.wait_for_selector(SEND_READY_SELECTOR, timeout=300_000, state="visible")
        except Exception as e:
            log({"stage": "abort", "reason": "send_never_enabled", "error": f"{type(e).__name__}: {e}"})
            return 1

        await page.locator(SEND_MOUNTED_SELECTOR).first.click()
        send_ts = time.time()
        log({"stage": "sent"})

        completed, innertext = await wait_for_completion(page, run_dir)
        log({"stage": "completion", "completed": completed,
             "elapsed_secs": round(time.time() - send_ts, 1), "chars": len(innertext)})

        try:
            (run_dir / "final.html").write_text(await page.content())
            await page.screenshot(path=str(run_dir / "final.png"), full_page=True)
        except Exception:
            pass

        # Extraction comparison: clipboard-read (fidelity path) vs innertext.
        clip = await extract_via_clipboard(page) if completed else None
        (run_dir / "extract_clipboard.md").write_text(clip or "")
        (run_dir / "extract_innertext.md").write_text(innertext or "")
        log({"stage": "extraction",
             "clipboard_ok": clip is not None,
             "clipboard_chars": len(clip or ""),
             "innertext_chars": len(innertext or ""),
             "match": (clip or "").strip() == (innertext or "").strip()})

        slug = await served_assistant_model_slug(page)
        slug_ok = slug in PRO_MODEL_SLUGS
        log({"stage": "served_model", "slug": slug, "allowlisted": slug_ok})

        verdict = {
            "kb": kb,
            "completed": completed,
            "served_slug": slug,
            "served_slug_allowlisted": slug_ok,
            "clipboard_extract_ok": clip is not None,
            "winning_paste_strategy": paste["winner"],
            "GATE_2B_PASSED": bool(completed and slug_ok and clip is not None),
        }
        (run_dir / "verdict.json").write_text(json.dumps(verdict, indent=2, default=str))
        log({"stage": "verdict", **verdict})
        return 0 if verdict["GATE_2B_PASSED"] else 1


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--send", action="store_true", help="Required. Confirms you accept spending one real Pro send.")
    ap.add_argument("--kb", type=int, default=2, help="Prompt size in KB (default 2 = tiny/cheap).")
    ap.add_argument("--headed", action="store_true", help="Run with a visible window (needs a display/Xvfb).")
    args = ap.parse_args()
    raise SystemExit(asyncio.run(main(headless=not args.headed, kb=args.kb, do_send=args.send)))
