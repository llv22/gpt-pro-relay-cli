#!/usr/bin/env python3
"""One-shot: set the composer effort to Pro in the seeded profile.

The composer effort is a persistent profile preference. A freshly-seeded
profile defaults to a lower tier (observed: "Instant"), and the spike's Gate 2b
fail-closes on a non-Pro chip (unlike production, which self-corrects mid-send).
So run this once after seeding to flip the persistent default to Pro, reusing
production's own ensure_pro_chip. The model (Sol) is already the account default.

    GPT_PRO_SPIKE_EXECUTABLE=/path/to/chrome uv run python spike/set_pro_effort.py
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _common import HeadlessSession, goto_chatgpt, spike_run_dir, log, COMPOSER_CSS  # noqa: E402
from gpt_pro.cli import (  # noqa: E402
    ensure_pro_chip,
    read_composer_chip_text,
    is_pro_label,
)


async def main() -> int:
    run_dir = spike_run_dir("set-pro")
    async with HeadlessSession(headless=True) as (ctx, page):
        nav = await goto_chatgpt(page)
        log({"stage": "navigated", **nav})
        await page.locator(COMPOSER_CSS).first.wait_for(state="visible", timeout=30_000)

        before = await read_composer_chip_text(page, timeout=30.0)
        ok, observed = await ensure_pro_chip(page, run_dir=run_dir)
        log({"stage": "ensure_pro_chip", "before": before, "ok": ok, "observed": observed})
        # Give the persistent context a moment to flush the preference before close.
        await asyncio.sleep(1.0)

    return 0 if ok and is_pro_label(observed or "") else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
