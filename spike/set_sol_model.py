#!/usr/bin/env python3
"""One-shot: set the composer model to GPT-5.6 Sol in the seeded profile.

The tool is fail-closed on model (served-slug audit rejects anything outside
PRO_MODEL_SLUGS), and production's ensure_pro_chip deliberately does NOT
self-correct the model submenu. A freshly-seeded profile can inherit a non-Sol
account default (observed: GPT-5.5). This selects GPT-5.6 Sol once via the chip
menu's model submenu so subsequent sends serve gpt-5-6-pro.

    GPT_PRO_SPIKE_EXECUTABLE=/path/to/chrome uv run python spike/set_sol_model.py
"""
from __future__ import annotations

import asyncio
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _common import HeadlessSession, goto_chatgpt, log, COMPOSER_CSS  # noqa: E402
from gpt_pro.cli import (  # noqa: E402
    COMPOSER_CHIP,
    SOL_MODEL_TOKEN,
    read_selected_model,
    classify_model_status,
)


async def select_sol(page, *, timeout: float = 10.0) -> bool:
    """Open the chip menu, hover the model submenu, click the GPT-5.6 Sol radio."""
    chip = page.locator(COMPOSER_CHIP).first
    try:
        await chip.click()
        await page.wait_for_selector('[role="menu"]', timeout=timeout * 1000)
        trigger = page.locator(
            '[role="menu"] [role="menuitem"][aria-haspopup="menu"]'
        ).filter(has_text=re.compile(r"\S")).first
        await trigger.hover()
        await page.locator('[role="menu"]').nth(1).wait_for(state="visible", timeout=timeout * 1000)
        submenu = page.locator('[role="menu"]').last
        sol = submenu.get_by_role("menuitemradio", name=re.compile(re.escape(SOL_MODEL_TOKEN)))
        await sol.first.click(timeout=timeout * 1000)
        await asyncio.sleep(0.5)
        return True
    except Exception as e:
        log({"stage": "select_sol_failed", "error": f"{type(e).__name__}: {e}"})
        return False
    finally:
        try:
            await page.keyboard.press("Escape")
            await asyncio.sleep(0.1)
            await page.keyboard.press("Escape")
        except Exception:
            pass


async def main() -> int:
    async with HeadlessSession(headless=True) as (ctx, page):
        nav = await goto_chatgpt(page)
        log({"stage": "navigated", **nav})
        await page.locator(COMPOSER_CSS).first.wait_for(state="visible", timeout=30_000)

        before = await read_selected_model(page)
        log({"stage": "before", "model": before})
        clicked = await select_sol(page)
        after = await read_selected_model(page)
        log({"stage": "after", "clicked": clicked, "model": after,
             "status": classify_model_status(after)})
        await asyncio.sleep(1.0)  # let the persistent context flush the preference

    return 0 if classify_model_status(after) == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
