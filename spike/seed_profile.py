#!/usr/bin/env python3
"""Seed ~/.gpt-pro-profile from an exported cookie JSON — the headless
alternative to an interactive `gpt-pro-relay login` on a GUI-less box.

The profile is nothing but a Chrome user-data-dir whose one load-bearing piece
is the NextAuth session cookie (see gpt_pro.cli.SESSION_COOKIE_PREFIX). This
script injects a cookie export into that profile via a persistent context and
lets the context's close persist them to disk, so a later headless run is
"logged in" without any interactive sign-in.

SECURITY: the session cookie is a live bearer credential. This reads it from a
FILE (never argv/stdin echoed into a shell history), and prints only cookie
NAMES, never values.

Usage:
    # export chatgpt.com cookies (Cookie-Editor -> Export JSON) to a file first
    GPT_PRO_SPIKE_EXECUTABLE=/path/to/chrome \\
    GPT_PRO_COOKIE_FILE=~/.gpt-pro-cookies.json \\
    uv run python spike/seed_profile.py

Accepts either shape in the JSON:
  * an array of cookie objects (Cookie-Editor / EditThisCookie export), or
  * a flat map {"cookie-name": "value", ...}.

Exit 0 = at least one session-token cookie present and is_logged_in() true.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _common import HeadlessSession, goto_chatgpt, log  # noqa: E402
from gpt_pro.cli import PROFILE, SESSION_COOKIE_PREFIX, is_logged_in  # noqa: E402

DEFAULT_ORIGIN = "chatgpt.com"

# Cookie-Editor / EditThisCookie use these sameSite spellings; Playwright wants
# exactly Strict | Lax | None.
_SAMESITE = {
    "no_restriction": "None",
    "unspecified": "Lax",
    "none": "None",
    "lax": "Lax",
    "strict": "Strict",
}


def _normalize(entry: dict) -> dict | None:
    """Map one exported cookie object to Playwright's add_cookies shape.
    Returns None if it lacks a usable name/value."""
    name = entry.get("name")
    value = entry.get("value")
    if not name or value is None:
        return None
    domain = entry.get("domain") or DEFAULT_ORIGIN
    path = entry.get("path") or "/"
    ck: dict = {
        "name": name,
        "value": value,
        "domain": domain,
        "path": path,
        "secure": bool(entry.get("secure", name.startswith("__Secure") or name.startswith("__Host"))),
        "httpOnly": bool(entry.get("httpOnly", False)),
        "sameSite": _SAMESITE.get(str(entry.get("sameSite", "")).lower(), "Lax"),
    }
    # session cookies have no expiry; only forward a real one.
    exp = entry.get("expirationDate", entry.get("expires"))
    if isinstance(exp, (int, float)) and exp > 0:
        ck["expires"] = float(exp)
    return ck


def _load(path: Path) -> list[dict]:
    raw = json.loads(path.read_text())
    if isinstance(raw, dict):
        # flat {name: value} map
        entries = [{"name": k, "value": v} for k, v in raw.items()]
    elif isinstance(raw, list):
        entries = raw
    else:
        raise SystemExit(f"cookie file must be a JSON array or object, got {type(raw).__name__}")
    out = []
    for e in entries:
        n = _normalize(e)
        if n:
            out.append(n)
    return out


async def _inject(ctx, cookies: list[dict]) -> tuple[list[str], list[dict]]:
    """Add cookies one at a time so a single finicky entry (e.g. a __Host-
    cookie, which rejects a Domain attribute) can't block the whole batch.
    On failure, retry with the url form (host-only, path-derived), which is
    exactly what __Host-/hostOnly cookies want."""
    added: list[str] = []
    failed: list[dict] = []
    for c in cookies:
        try:
            await ctx.add_cookies([c])
            added.append(c["name"])
            continue
        except Exception:
            pass
        try:
            alt = {k: v for k, v in c.items() if k not in ("domain", "path")}
            host = c.get("domain", DEFAULT_ORIGIN).lstrip(".")
            alt["url"] = f"https://{host}{c.get('path', '/')}"
            await ctx.add_cookies([alt])
            added.append(c["name"])
        except Exception as e:
            failed.append({"name": c["name"], "error": f"{type(e).__name__}: {e}"})
    return added, failed


async def main() -> int:
    cookie_file = Path(os.path.expanduser(
        os.environ.get("GPT_PRO_COOKIE_FILE", "~/.gpt-pro-cookies.json")
    ))
    if not cookie_file.exists():
        log({"stage": "error", "msg": f"cookie file not found: {cookie_file}. "
             "Export chatgpt.com cookies to it (see script header)."})
        return 2

    cookies = _load(cookie_file)
    names = sorted({c["name"] for c in cookies})
    has_session = any(n.startswith(SESSION_COOKIE_PREFIX) for n in names)
    log({"stage": "loaded", "count": len(cookies), "names": names,
         "has_session_token": has_session})
    if not has_session:
        log({"stage": "error", "msg": f"no cookie starting with {SESSION_COOKIE_PREFIX!r} "
             "in the export — that is the one the login check requires."})
        return 2

    async with HeadlessSession(headless=True) as (ctx, page):
        added, failed = await _inject(ctx, cookies)
        log({"stage": "injected", "added": sorted(added),
             "failed": [f["name"] for f in failed]})
        for f in failed:
            log({"stage": "inject_failed", **f})
        nav = await goto_chatgpt(page)
        log({"stage": "navigated", **nav})
        logged_in = await is_logged_in(ctx)
        log({"stage": "login_check", "logged_in": logged_in,
             "profile": str(PROFILE)})
        # ctx close (on __aexit__) persists the cookies into the profile.

    ok = logged_in and not nav["auth_redirect"]
    log({"stage": "verdict", "seeded": ok, "profile": str(PROFILE),
         "hint": "run spike/01_auth_probe.py next to confirm from a fresh process"
         if ok else "cookies did not yield a logged-in session; re-export a fresh set"})
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
