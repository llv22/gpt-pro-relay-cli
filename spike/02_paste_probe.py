#!/usr/bin/env python3
"""Gate 2a — can a large prompt reach ProseMirror WITHOUT the macOS pasteboard?

Cheap half of Gate 2: pastes a generated N-KB prompt using each strategy in
turn and reports which one gets the send button to mount (the production
ingestion signal). Does NOT click Send, so it spends ZERO Pro reasoning — run
it freely at several sizes to find where (if anywhere) paste breaks.

    uv run python spike/02_paste_probe.py                 # 300 KB, headless
    uv run python spike/02_paste_probe.py --kb 1024       # 1 MB
    uv run python spike/02_paste_probe.py --kb 4096 --headed

Exit 0 if some strategy ingested the prompt; non-zero if all failed.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _common import (  # noqa: E402
    HeadlessSession,
    goto_chatgpt,
    spike_run_dir,
    log,
    is_logged_in,
    try_paste,
    make_probe_prompt,
    latest_assistant_innertext,  # noqa: F401  (kept for parity/manual poking)
    COMPOSER_CSS,
)


async def main(headless: bool, kb: int) -> int:
    run_dir = spike_run_dir(f"paste-{kb}kb")
    prompt = make_probe_prompt(kb)
    log({"stage": "start", "headless": headless, "kb": kb,
         "prompt_bytes": len(prompt.encode()), "run_dir": str(run_dir)})

    async with HeadlessSession(headless=headless) as (ctx, page):
        nav = await goto_chatgpt(page)
        log({"stage": "navigated", **nav})
        if not await is_logged_in(ctx) or nav["auth_redirect"]:
            log({"stage": "abort", "reason": "not_served_app_run_gate_1_first"})
            return 2

        # Make sure the composer exists before pasting.
        try:
            await page.locator(COMPOSER_CSS).first.wait_for(state="visible", timeout=30_000)
        except Exception as e:
            log({"stage": "abort", "reason": "composer_not_found", "error": f"{type(e).__name__}: {e}"})
            return 2

        result = await try_paste(page, prompt)
        log({"stage": "paste_result", **result})

        try:
            await page.screenshot(path=str(run_dir / "after-paste.png"), full_page=True)
        except Exception:
            pass

        verdict = {"kb": kb, "prompt_bytes": len(prompt.encode()),
                   "winning_strategy": result["winner"], "attempts": result["attempts"],
                   "GATE_2A_PASSED": result["winner"] is not None}
        (run_dir / "verdict.json").write_text(json.dumps(verdict, indent=2, default=str))
        log({"stage": "verdict", **verdict})
        return 0 if result["winner"] else 1


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--kb", type=int, default=300, help="Prompt size in KB (default 300).")
    ap.add_argument("--headed", action="store_true", help="Run with a visible window (needs a display/Xvfb).")
    args = ap.parse_args()
    raise SystemExit(asyncio.run(main(headless=not args.headed, kb=args.kb)))
