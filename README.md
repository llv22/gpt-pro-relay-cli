# gpt-pro

CLI that drives a logged-in ChatGPT Pro browser session via Playwright. Designed to be invoked over SSH:

```bash
RUN_ID=$(uuidgen)
echo "your prompt" | ssh mac /path/to/gpt-pro/.venv/bin/gpt-pro ask --run-id "$RUN_ID"
```

Personal lazy tool. Single user, single Mac, single account.

> Browser automation against ChatGPT violates OpenAI's ToS. Account-ban risk is yours. Don't build a product on it.

## How it works

```
remote ──ssh──▶ Mac ──[parent: ask]
                       │
                       │ writes prompt.md, meta.json
                       │ spawns detached worker (start_new_session)
                       ▼
                      [worker: _run] ──Playwright──▶ Chrome (persistent profile)
                       │                                          │
                       │ <── poll result.json ──┐                 ▼
                       │                        │      chatgpt.com / Pro / Extended Pro
                       ▼                        │                 │
                  response on stdout            └─── result.json ◀┘
                  JSON status on stderr
```

No daemon. No HTTP server. No queue. SSH is the transport. The worker is detached from the SSH session, so a mid-run drop doesn't kill it — `gpt-pro fetch <run_id>` recovers the response.

## Setup

Requires Python 3.11+, [uv](https://docs.astral.sh/uv/), and Google Chrome (real Chrome, not bundled Chromium).

```bash
uv sync
uv run gpt-pro login    # opens Chrome; sign in to ChatGPT manually
```

Login uses a dedicated profile at `~/.gpt-pro-profile/`. Cookies persist there. Manually select **Pro** + **Extended Pro** once so the account preference is set.

## Commands

| Command | What it does |
|---|---|
| `gpt-pro login` | Open Chrome at chatgpt.com using the dedicated profile. Auto-detects login (session cookie) and exits. |
| `gpt-pro doctor` | Verify the profile is logged in. Probes the model picker. Saves screenshot + HTML to `~/.gpt-pro/runs/`. Prints JSON status. |
| `gpt-pro ask [--run-id ID]` | Read prompt from stdin. Spawns a detached worker, waits for completion, prints response on stdout. Same `--run-id` + same prompt re-attaches to an in-progress run (idempotent). |
| `gpt-pro fetch <run-id>` | Read the result of an existing run. Waits if still running. `--timeout 0` for non-blocking check. |

## SSH usage

**Happy path** — single command, response on stdout:

```bash
RUN_ID=$(uuidgen)
ssh -o ServerAliveInterval=30 -o ServerAliveCountMax=10 mac \
    /Users/you/Developer/GitHub/gpt-pro/.venv/bin/gpt-pro ask --run-id "$RUN_ID" <<'PROMPT'
your prompt here
PROMPT
```

**Recovery after SSH drop:**

```bash
ssh -o ServerAliveInterval=30 mac \
    /Users/you/Developer/GitHub/gpt-pro/.venv/bin/gpt-pro fetch "$RUN_ID"
```

The worker survives `SIGHUP` from SSH session teardown and continues to completion. `fetch` polls the run directory and prints the response when ready. **Never re-run `ask` to recover** — that would submit a fresh prompt to ChatGPT and burn another 5–20 min of Pro reasoning.

`stdout` is the response. `stderr` is newline-delimited JSON: a `submitted` line when the run starts, then a terminal `ok`/`error`/`timeout` line.

Exit codes:

| code | meaning |
|---|---|
| 0 | `status: ok`, response on stdout |
| 1 | `status: error`, see `reason` field |
| 2 | usage error (empty prompt, run_id_conflict, invalid run_id) |
| 3 | `status: timeout` (browser worker didn't finish within 35 min) |
| 4 | run_dir not found (fetch only) |
| 124 | wait timed out, run still pending |

## Artifacts

Each run writes to `~/.gpt-pro/runs/<run_id>/`:

- `prompt.md` — input
- `meta.json` — `{run_id, created_at, prompt_sha256}`
- `response.md` — extracted assistant message (atomic). `result.json` reports `extraction: "copy_button"` or `"innertext"`.
- `result.json` — terminal status (atomic)
- `pre-send.png`, `streaming-NNN.png`, `final.png`, `error-*.png`
- `final.html` — last DOM snapshot
- `network.json` — captured `/backend-api/*` calls
- `worker.stdout`, `worker.stderr` — detached worker's output

## Known limitations

- Concurrent `ask` invocations serialize via a `flock` on `~/.gpt-pro/browser.lock` — second worker waits for first to finish before launching Chrome.
- Markdown extraction uses the page's Copy button (clean LaTeX, code fences, tables); falls back to `innerText` if the Copy button isn't reachable or `pbpaste` isn't available (non-macOS).
- Completion detection is heuristic (text-stable + no Stop button), not the `/backend-api/conversation/<id>/async-status` endpoint. The async-status endpoint only fires once at the end and our heuristic catches the same moment — not worth wiring.
- The Mac must be in a logged-in GUI session — Playwright needs WindowServer access.
- If the SSH-side parent dies before reading stdin and spawning the worker, no run is created — `fetch` returns `not_found`. That's by design.
