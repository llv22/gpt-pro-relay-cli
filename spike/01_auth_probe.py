#!/usr/bin/env python3
"""Gate 1 — does headless Chrome get served ChatGPT Pro?

This is the KILL GATE. If OpenAI's anti-abuse bounces a headless session to an
auth error, no amount of clipboard cleverness rescues the port — stop here.

Cheap and safe: navigates, checks cookies, checks for an auth-error redirect,
reads the composer effort chip and the selected model (read-only, NO send). No
Pro reasoning is spent.

Run (on the Linux box, after seeding ~/.gpt-pro-profile — see spike/README.md):
    uv run python spike/01_auth_probe.py            # headless (the real test)
    uv run python spike/01_auth_probe.py --headed   # baseline for comparison

Exit 0 = logged in, no auth redirect, chip readable. Non-zero otherwise.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
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
    read_selected_model,
    classify_model_status,
)


async def main(headless: bool) -> int:
    run_dir = spike_run_dir("auth")
    log({"stage": "start", "headless": headless, "run_dir": str(run_dir)})

    async with HeadlessSession(headless=headless) as (ctx, page):
        nav = await goto_chatgpt(page)
        log({"stage": "navigated", **nav})

        logged_in = await is_logged_in(ctx)
        log({"stage": "login_check", "logged_in": logged_in})

        # Diagnostics regardless of outcome.
        try:
            await page.screenshot(path=str(run_dir / "page.png"), full_page=True)
        except Exception as e:
            log({"stage": "screenshot_skipped", "error": f"{type(e).__name__}: {e}"})
        try:
            (run_dir / "page.html").write_text(await page.content())
        except Exception:
            pass

        chip_text = None
        chip_ok = None
        model_text = None
        model_status = None
        if logged_in and not nav["auth_redirect"]:
            chip_text = await read_composer_chip_text(page, timeout=30.0)
            chip_ok = is_pro_label(chip_text)
            log({"stage": "chip_read", "text": chip_text, "is_pro": chip_ok})

            model_text = await read_selected_model(page)
            model_status = classify_model_status(model_text)
            log({"stage": "model_read", "text": model_text, "status": model_status})

        verdict = {
            "logged_in": logged_in,
            "auth_redirect": nav["auth_redirect"],
            "final_url": nav["final_url"],
            "chip_text": chip_text,
            "chip_is_pro": chip_ok,
            "model_text": model_text,
            "model_status": model_status,
        }
        # Gate 1 passes on: served the app (logged in, no auth bounce) AND the
        # composer chip is readable. Sol+Pro being the *default* is a bonus the
        # production slow-path would self-correct anyway — we report it but don't
        # fail Gate 1 on a merely-wrong default effort.
        passed = logged_in and not nav["auth_redirect"] and bool(chip_text)
        verdict["GATE_1_PASSED"] = passed
        (run_dir / "verdict.json").write_text(__import__("json").dumps(verdict, indent=2, default=str))
        log({"stage": "verdict", **verdict})

        if not passed:
            if not logged_in or nav["auth_redirect"]:
                log({"stage": "hint", "msg": "Not served the app. If this is headless-only "
                     "(headed works), OpenAI is blocking headless — the port is dead. If both "
                     "fail, the profile isn't logged in: seed ~/.gpt-pro-profile (see README)."})
        return 0 if passed else 1


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--headed", action="store_true", help="Run with a visible window (needs a display/Xvfb) as a baseline.")
    args = ap.parse_args()
    raise SystemExit(asyncio.run(main(headless=not args.headed)))
