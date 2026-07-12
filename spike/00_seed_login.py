#!/usr/bin/env python3
"""Gate 0 — seed ~/.gpt-pro-profile with a logged-in ChatGPT session on Linux.

A headless box can't do the interactive OAuth dance, so you log in ONCE through
a visible window, then the persistent profile carries the session cookies into
the headless runs. Two ways to get a visible window on a server:

  A) Xvfb + a VNC viewer:
       Xvfb :99 -screen 0 1280x800x24 &
       x11vnc -display :99 -localhost -nopw -forever &   # tunnel :5900 over ssh
       DISPLAY=:99 uv run python spike/00_seed_login.py
  B) X11-forward from your laptop:  ssh -X user@box, then run it.

Alternatively, skip this entirely and COPY a logged-in profile from another
machine — because the tool launches Chrome with --password-store=basic +
--use-mock-keychain, cookies are NOT sealed to the OS keychain, so the profile
is far more portable than a normal Chrome profile. Try:
    rsync -a ~/.gpt-pro-profile/  user@box:~/.gpt-pro-profile/
then go straight to 01_auth_probe.py. If the copied profile shows logged-out,
fall back to this interactive seed.

This launches HEADED and waits (up to --timeout s) for the session cookie.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _common import HeadlessSession, log, CHATGPT_URL  # noqa: E402
from gpt_pro.cli import wait_for_login  # noqa: E402


async def main(timeout: float) -> int:
    # headless=False: you need to see and complete the login.
    async with HeadlessSession(headless=False) as (ctx, page):
        await page.goto(CHATGPT_URL, wait_until="domcontentloaded")
        log({"stage": "waiting_for_login", "msg": "Complete login in the window.", "timeout_s": timeout})
        ok = await wait_for_login(ctx, timeout=timeout)
        log({"stage": "login_result", "logged_in": ok})
        return 0 if ok else 1


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--timeout", type=float, default=600.0, help="Seconds to wait for login (default 600).")
    args = ap.parse_args()
    raise SystemExit(asyncio.run(main(args.timeout)))
