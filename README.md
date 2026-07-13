# gpt-pro-relay

Relay prompts to your logged-in **ChatGPT Pro** session from anywhere with SSH. A host machine drives a real Chrome via Playwright; remote agents and other machines invoke it as a CLI:

```bash
RUN_ID=$(uuidgen)
echo "your prompt" | ssh host gpt-pro-relay ask --run-id "$RUN_ID"
```

> **Fork.** This is a fork of Chris Liu's original **[chrisliu298/gpt-pro-relay](https://github.com/chrisliu298/gpt-pro-relay)** — full credit for the design (the submit-and-wait architecture, the file-lock concurrency model, the fail-closed model/effort audit) goes to the original. This fork adds a **headless-Linux port**, **ChatGPT composer tools** (`--tool`: web search, deep research, …), and configurable model/paths. See [Credits](#credits).

> Browser automation against ChatGPT violates OpenAI's ToS. Account-ban risk is yours. Don't build a product on it.

## How it works

```
remote ──ssh──▶ host ──[parent: ask]
                        │
                        │ writes prompt.md, meta.json
                        │ spawns detached worker (start_new_session)
                        ▼
                       [worker: _run] ──Playwright──▶ Chrome (persistent profile)
                        │                                          │
                        │ <── poll result.json ──┐                 ▼
                        │                        │      chatgpt.com / Pro model / Pro effort
                        ▼                        │                 │
                   response on stdout            └─── result.json ◀┘
                   JSON status on stderr
```

No daemon. No HTTP server. No queue. SSH is the transport. The worker is detached from the SSH session, so a mid-run drop doesn't kill it — `gpt-pro-relay fetch <run_id>` recovers the response.

## Platforms

| | macOS (original) | Headless Linux (this fork) |
|---|---|---|
| Chrome launch | `open -n -a "Google Chrome"` (LaunchServices) | direct-exec the Chrome binary, **new headless** + spoofed UA |
| Clipboard I/O | `pbcopy` / `pbpaste` / `Cmd+V` | Chrome's in-browser clipboard (`writeText` + `Ctrl+V`, `readText`) |
| Display | needs a logged-in GUI session | none — runs fully headless |
| Login | interactive (`login`) | cookie injection (see below) |

A single `IS_MAC` gate forks exactly those three seams; the shared-Chrome-over-CDP architecture, the three file locks, the worker/supervisor split, the completion gate, and the model audit are identical on both.

## Setup

Requires Python 3.11+, [uv](https://docs.astral.sh/uv/), and a ChatGPT Pro (or Enterprise) account.

```bash
uv sync   # installs gpt-pro-relay into the project's venv
```

- **macOS** — `uv run gpt-pro-relay login`, then sign in and pick your Pro model + **Pro** effort once.
- **Headless Linux** — supply a Chrome binary (`GPT_PRO_CHROME_BINARY`) and seed the login by cookie injection (`spike/seed_profile.py`); no display needed.

Full instructions for both platforms — Chrome for Testing, cookie seeding, model/effort selection, and running normal vs. deep-research queries — are in **[docs/SETUP.md](docs/SETUP.md)**.

Optional: put the command on `PATH` — `ln -sf "$PWD/.venv/bin/gpt-pro-relay" ~/.local/bin/gpt-pro-relay`.

## Commands

| Command | What it does |
|---|---|
| `login` | Open Chrome at chatgpt.com using the dedicated profile; auto-detects login (session cookie) and exits. (macOS / interactive.) |
| `doctor` | Verify the profile is logged in and the composer is set to the expected model + **Pro** effort (read-only; no prompt sent). Exits non-zero on a confirmed wrong model. Saves screenshot + HTML. |
| `ask [--run-id ID] [--tool TOOL]… [--no-wait] [--output PATH]` | Read prompt from stdin, spawn a detached worker. Default: wait for completion, print response on stdout. `--no-wait`: exit 0 right after submission (use `fetch`). Same `--run-id` + same prompt re-attaches (idempotent). |
| `fetch <run-id> [--output PATH]` | Read the result of an existing run. Waits if still running. `--timeout 0` = non-blocking; `--timeout 60` bounds a single poll. |
| `close-chrome [--force]` | Tear down the shared Chrome. Refuses by default if any worker holds a slot; `--force` kills anyway. |

## Composer tools (`--tool`)

`ask --tool <name>` enables a ChatGPT composer tool before sending (repeatable). Names mirror the ChatGPT UI; an unsupported name fails immediately (argparse error):

| `--tool` | ChatGPT option |
|---|---|
| `web-search` | Web search (real-time info + citations) |
| `create-image` | Create image |
| `company-knowledge` | Company knowledge (workspace connectors) |
| `deep-research` | Deep research (multi-minute cited report) |
| `google-drive` | Google Drive |

```bash
echo "latest stable Python version?" | gpt-pro-relay ask --tool web-search
echo "survey trajectory-level credit assignment in agent RL" | gpt-pro-relay ask --tool deep-research
```

Tool availability depends on your account (connectors like `company-knowledge` / `google-drive` need workspace provisioning; a missing tool fails with `tool_unavailable`). When a tool is active, the served-slug model audit is fail-open (`tool_mode_unaudited`) — tools legitimately run their own model.

**Deep research** renders its report inside an OpenAI cross-origin **sandboxed iframe** with no DOM/clipboard access, so extraction goes through the report's *Export → Markdown* download (coordinate-driven, inherently more fragile than other tools). Completion is detected by the export succeeding.

## Configuration (environment)

| Env var | Default | Purpose |
|---|---|---|
| `GPT_PRO_CHROME_BINARY` | probe common paths | Chrome binary for the headless Linux launch |
| `GPT_PRO_NO_SANDBOX` | `1` (Linux) | `0` disables `--no-sandbox` where the OS sandbox works |
| `GPT_PRO_MODEL_SLUGS` | `gpt-5-6-pro` | Comma-separated allowlist of served model slugs, **unioned** with the Sol default. An account without GPT-5.6 Sol can opt in its Pro slug (e.g. `gpt-5-5-pro`) without weakening the shipped fail-closed default. |
| `GPT_PRO_RUNS_DIR` | `./runs` | Where run artifacts go — **defaults to the current folder**, not `~/.gpt-pro`. |
| `GPT_PRO_HOME` | `~/.gpt-pro` | Coordination-lock dir (and runs, if `GPT_PRO_RUNS_DIR` unset). Keep consistent across concurrent workers. |
| `GPT_PRO_MAX_PARALLEL` | `6` | Max concurrent runs sharing one Chrome (ceiling 10). Drop to `1` if the account flags parallel bursts. |
| `GPT_PRO_COOKIE_FILE` | `~/.gpt-pro-cookies.json` | Cookie export read by `spike/seed_profile.py`. |

## Usage

The CLI is identical locally or over SSH.

### Local

```bash
RUN_ID=$(uuidgen)
echo "your prompt" | gpt-pro-relay ask --run-id "$RUN_ID"
```

### Remote (SSH) — short-session polling

Holding one SSH connection idle for the full 5–20 min reasoning window is brittle. Submit with `--no-wait`, then poll `fetch` with bounded timeouts:

```bash
SSH_OPTS=(-S none -o BatchMode=yes -o ConnectTimeout=15 -o ServerAliveInterval=15 -o ServerAliveCountMax=4)
RUN_ID="ask-$(date -u +%Y%m%dT%H%M%SZ)-$(uuidgen | tr '[:upper:]' '[:lower:]')"

ssh "${SSH_OPTS[@]}" host gpt-pro-relay ask --run-id "$RUN_ID" --no-wait <<'PROMPT'
your prompt here
PROMPT

deadline=$((SECONDS + 3600)); delay=5
while (( SECONDS < deadline )); do
  out=$(ssh "${SSH_OPTS[@]}" host gpt-pro-relay fetch "$RUN_ID" --timeout 60 2>/tmp/gpt-pro-$RUN_ID.err); rc=$?
  case $rc in
    0)   printf '%s' "$out"; exit 0 ;;
    124) delay=5; continue ;;
    255) sleep "$delay"; (( delay < 30 )) && delay=$((delay * 2)) ;;
    *)   cat /tmp/gpt-pro-$RUN_ID.err >&2; exit "$rc" ;;
  esac
done
echo "overall timeout for $RUN_ID" >&2; exit 124
```

If a session drops mid-run, **never re-run `ask` fresh** — recover with `fetch "$RUN_ID"` (the same `--run-id` + same prompt bytes re-attaches idempotently).

### Stdio contract

`stdout` = the response. `stderr` = newline-delimited JSON (`submitted`, then a terminal `ok`/`error`/`timeout`). `--output PATH` writes the response to a file instead; stdout stays empty and the terminal line gains `"output": "<path>"`.

| exit | meaning |
|---|---|
| 0 | `ok` (or `--no-wait` submitted) |
| 1 | `error`, see `reason` |
| 2 | usage error (empty/oversized prompt, run_id conflict, bad tool) |
| 3 | `timeout` (worker didn't finish within the generation timeout) |
| 4 | run_dir not found (fetch) |
| 124 | wait timed out, run still pending |

## Suggested: research as a private sub-repo

If you use this CLI for research, we suggest keeping your queries and results **out of any public repo** so topics stay internal. A clean pattern: put them in a **separate private repo** cloned into a git-ignored `research/` folder. Keep it a plain nested repo, **not** a git submodule, so the public repo holds no reference to it (a `.gitmodules` entry would leak the private repo's URL).

Suggested structure — one folder per query, grouping a prompt with all of its answers:

```
research/                          # private repo, git-ignored in this checkout
├── README.md                      # archive index + convention
└── Q<N>-<slug>/                    # N sequential, kebab-case slug
    ├── query.md                   # the exact prompt sent
    ├── result-pro.md              # plain Pro answer (no tool)
    └── result-<option>.md         # one file per composer option used (deep-research, web-search, …)
```

`runs/` (raw per-run artifacts — screenshots, `result.json`, worker logs) is likewise git-ignored and local. To set this up, create a private repo and clone it into `research/`; its own README carries the index and "how to add a query".

## Artifacts

Each run writes to `<RUNS>/<run_id>/` (default `./runs/<run_id>/`):

- `prompt.md`, `meta.json` (`{run_id, created_at, prompt_sha256, tools}`)
- `response.md` — extracted assistant message; `result.json` reports `extraction: "copy_button" | "innertext" | "deep_research_export_md"`
- `result.json` — terminal status (atomic)
- `pre-send.png`, `streaming-NNN.png`, `final.png`, `error-*.png`, `final.html`, `network.json`
- `worker.stdout`, `worker.stderr`

## Concurrency

Up to `GPT_PRO_MAX_PARALLEL` (default 6) runs share one Chrome, each in its own tab; beyond that they queue on a file-lock semaphore (`~/.gpt-pro/slots/`). Chrome stays alive between runs; `gpt-pro-relay close-chrome` tears it down when no workers are in flight.

## Credits

Original project and design by **Chris Liu** ([@chrisliu298](https://github.com/chrisliu298)) — **[github.com/chrisliu298/gpt-pro-relay](https://github.com/chrisliu298/gpt-pro-relay)**. The submit-and-wait architecture, the file-lock concurrency model, and the fail-closed model/effort audit are all his; his commit history is preserved here.

This repo ([github.com/llv22/gpt-pro-relay-cli](https://github.com/llv22/gpt-pro-relay-cli)) continues that work with a headless-Linux port, ChatGPT composer tools, and configurable model/paths.
