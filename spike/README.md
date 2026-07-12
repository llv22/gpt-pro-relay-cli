# Headless-Linux feasibility spike

Throwaway diagnostics to decide whether `gpt-pro-relay` can run on a **GUI-less
Linux server**. These are NOT part of the shipped tool and should be deleted (or
folded into a `GPT_PRO_HEADLESS` fork) once the questions below are answered.

The macOS design is deliberately GUI-bound (`README.md:37`, CLAUDE.md anti-detection
note). A code read can't settle the two things that decide the port, so we test
them empirically here:

- **Gate 1 — auth.** Does headless Chrome + the profile get served ChatGPT Pro,
  or does OpenAI's anti-abuse redirect to an auth error? *This is the kill gate.*
- **Gate 2 — I/O.** Can we deliver a multi-hundred-KB prompt into ProseMirror and
  extract clean markdown **without the macOS pasteboard** (`pbcopy`/`pbpaste`/`Meta+V`)?

The headless replacement for `pbcopy`+`Cmd+V` is **`navigator.clipboard.writeText`
+ `Ctrl+V`** — Chrome's *internal* clipboard, no OS pasteboard, and it hits the
same optimized ProseMirror paste handler production relies on. Extraction becomes
Copy-click + `navigator.clipboard.readText()`. The scripts try this first and
report which strategy actually worked.

## Prereqs (on the Linux box)

- Real Google Chrome installed (`google-chrome-stable`). Override the channel with
  `GPT_PRO_SPIKE_CHANNEL=chromium` only for throwaway experiments — the invariant
  is real Chrome.
- The repo synced and deps installed: `uv venv && uv sync`.
- Playwright's Chrome deps: `uv run playwright install-deps chrome` (or the distro
  equivalent). You do **not** need `playwright install chromium` — we use system Chrome.
- **Run as a non-root user.** Chrome refuses to launch as root / in most Docker
  containers without `--no-sandbox` (`"Running as root without --no-sandbox is not
  supported"`). As a normal user the userns sandbox works and the anti-detection
  invariant is preserved. If you truly can't avoid root, opt into the flag:
  `GPT_PRO_SPIKE_NO_SANDBOX=1 uv run python spike/01_auth_probe.py`.

## Run order

```bash
# 0. Seed a logged-in profile (once). See the header of 00_seed_login.py for the
#    Xvfb+VNC recipe, or just rsync a logged-in ~/.gpt-pro-profile from another box.
DISPLAY=:99 uv run python spike/00_seed_login.py

# 1. KILL GATE — cheap, no Pro spend. Run headless AND headed to compare.
uv run python spike/01_auth_probe.py
uv run python spike/01_auth_probe.py --headed      # baseline (needs a display)

# 2a. Large-paste ingestion — cheap, no Pro spend. Sweep sizes.
uv run python spike/02_paste_probe.py --kb 300
uv run python spike/02_paste_probe.py --kb 1024
uv run python spike/02_paste_probe.py --kb 4096

# 2b. Full round-trip — spends ONE real Pro send. Opt-in.
uv run python spike/03_send_roundtrip.py --send            # tiny prompt
uv run python spike/03_send_roundtrip.py --send --kb 300   # + large paste
```

Each script prints JSONL to stdout and drops artifacts (screenshot, `page.html`,
`verdict.json`, and for 2b the two extraction outputs) under
`~/.gpt-pro/spike/<ts>-<name>/`. Look for the `GATE_*_PASSED` field in each verdict.

## Decision tree

```
01_auth_probe headless ─ fail ─┐
        │ pass               ├─ headed also fails → profile not logged in → reseed (Gate 0)
        │                    └─ headed passes, headless fails → OpenAI blocks headless → PORT IS DEAD
        ▼
02_paste_probe ─ all strategies fail at your prompt sizes → paste path unsolved → port blocked on I/O
        │ some strategy wins (note which, and up to what KB)
        ▼
03_send_roundtrip --send
        ├─ served_slug not in {gpt-5-6-pro} → headless gets downgraded → PORT IS DEAD
        ├─ clipboard_extract_ok = false / match=false → extraction path needs work (innertext fallback only)
        └─ GATE_2B_PASSED = true → port is viable → implement the GPT_PRO_HEADLESS sys.platform fork
```

## What the spike deliberately does NOT cover

- **Shared-Chrome / multi-tab / the three locks.** Orthogonal to "does headless
  work"; each script owns the profile in a single process. If Gate 1/2 pass, the
  concurrency machinery ports separately (the in-browser clipboard is still shared
  across tabs in one Chrome, so `UiClipboardLock` still stands).
- **The `open -a` LaunchServices launch and CoreAnimation surface dance** — all
  macOS-only and irrelevant headless.
- **Ban risk over time.** A single passing send does not prove OpenAI won't flag
  the headless pattern under sustained/parallel load. That's an operational risk
  the account owner accepts, not something a spike can certify.
