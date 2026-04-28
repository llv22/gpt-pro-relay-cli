# gpt-pro-relay

Relay prompts to your logged-in ChatGPT Pro session from anywhere with SSH. Your always-on Mac drives a real Chrome via Playwright; remote agents and other machines invoke it as a CLI:

```bash
RUN_ID=$(uuidgen)
echo "your prompt" | ssh mac gpt-pro-relay ask --run-id "$RUN_ID"
```

`uv sync` installs both `gpt-pro-relay` (canonical name, matches the repo) and `gpt-pro` (kept for backward compatibility) into the project's venv. Personal lazy tool: single user, single Mac, single account.

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

No daemon. No HTTP server. No queue. SSH is the transport. The worker is detached from the SSH session, so a mid-run drop doesn't kill it — `gpt-pro-relay fetch <run_id>` recovers the response.

## Setup

Requires:

- A Mac that stays logged into its GUI session. Playwright drives real Chrome and needs WindowServer access, so a headless box won't work — leave the Mac signed in (and use `caffeinate` if it sleeps).
- Python 3.11+, [uv](https://docs.astral.sh/uv/), and Google Chrome (real Chrome, not bundled Chromium — auth and anti-abuse behave differently).
- A ChatGPT Pro account.

```bash
uv sync
uv run gpt-pro-relay login    # opens Chrome; sign in to ChatGPT manually
```

Login uses a dedicated profile at `~/.gpt-pro-profile/`. Cookies persist there. Manually select **Pro** + **Extended Pro** once so the account preference is set.

### Optional: bare command on PATH

For SSH callers to use `gpt-pro-relay` without the full venv path, symlink it into a directory that's on your non-interactive shell `PATH`. On zsh, `~/.local/bin/` works if you export it in `~/.zshenv` (which zsh sources for SSH sessions, unlike `~/.zshrc`):

```bash
mkdir -p ~/.local/bin
ln -sf "$PWD/.venv/bin/gpt-pro-relay" ~/.local/bin/gpt-pro-relay
```

After that, `ssh mac gpt-pro-relay ask ...` resolves without the absolute path. Skip if you'd rather hardcode the full venv path in your callers.

## Commands

| Command | What it does |
|---|---|
| `gpt-pro-relay login` | Open Chrome at chatgpt.com using the dedicated profile. Auto-detects login (session cookie) and exits. |
| `gpt-pro-relay doctor` | Verify the profile is logged in. Probes the model picker. Saves screenshot + HTML to `~/.gpt-pro/runs/`. Prints JSON status. |
| `gpt-pro-relay ask [--run-id ID] [--output PATH]` | Read prompt from stdin. Spawns a detached worker, waits for completion, prints response on stdout. Same `--run-id` + same prompt re-attaches to an in-progress run (idempotent). `--output` writes to a file instead of stdout. |
| `gpt-pro-relay fetch <run-id> [--output PATH]` | Read the result of an existing run. Waits if still running. `--timeout 0` for non-blocking check. `--output` writes to a file instead of stdout. |

## SSH usage

**Happy path** — single command, response on stdout:

```bash
RUN_ID=$(uuidgen)
ssh -o ServerAliveInterval=30 -o ServerAliveCountMax=10 mac \
    gpt-pro-relay ask --run-id "$RUN_ID" <<'PROMPT'
your prompt here
PROMPT
```

**Recovery after SSH drop:**

```bash
ssh -o ServerAliveInterval=30 mac \
    gpt-pro-relay fetch "$RUN_ID"
```

The worker survives `SIGHUP` from SSH session teardown and continues to completion. `fetch` polls the run directory and prints the response when ready. **Never re-run `ask` to recover** — that would submit a fresh prompt to ChatGPT and burn another 5–20 min of Pro reasoning.

`stdout` is the response. `stderr` is newline-delimited JSON: a `submitted` line when the run starts, then a terminal `ok`/`error`/`timeout` line.

Pass `--output PATH` to write the response to a file on the gpt-pro host instead. stdout stays empty; the terminal stderr line gains an `"output": "<resolved-path>"` field. Useful when the caller would rather `Read` a file than capture potentially-large stdout.

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
- If the SSH-side parent dies before reading stdin and spawning the worker, no run is created — `fetch` returns `not_found`. That's by design.

## Claude Code skill

[`skills/pro-relay/SKILL.md`](skills/pro-relay/SKILL.md) is a [Claude Code skill](https://docs.claude.com/en/docs/claude-code/skills) that wraps the SSH command. Copy it into `~/.claude/skills/pro-relay/` and edit the `mac` SSH alias to match your own. After that, Claude triggers it on prompts like "ask gpt-pro about X" or "get a Pro Extended take on Y".
