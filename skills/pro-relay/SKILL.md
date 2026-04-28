---
name: pro-relay
description: |
  Send a prompt to ChatGPT Pro Extended via gpt-pro-relay running on a remote Mac, over SSH.
  Use this whenever the user wants a deep ChatGPT Pro response from a machine that's not their
  primary Chrome session — triggers on "ask gpt-pro", "send to gpt-pro", "use gpt-pro", "get a
  Pro Extended take", "ask the deep model", "second opinion from chatgpt pro". Returns response
  on stdout. Resilient to SSH drops via caller-supplied `--run-id` plus a `fetch` recovery
  command. Replace the `mac` SSH alias below with your own; `gpt-pro-relay` is assumed to be on
  the remote shell's PATH (see the repo's "Optional: bare command on PATH" setup note).
allowed-tools: Bash(ssh:*), Bash(uuidgen:*), Bash(date:*), Read, Write
user-invocable: true
---

# pro-relay

One prompt in, one response out. The browser automation runs on mac behind SSH against a dedicated logged-in profile. The work is done by a detached worker so SSH drops don't kill it — you can reconnect and `fetch` the result.

## The command

> **About the bare `gpt-pro-relay` command:** it's not a system tool. The remote shell finds it because the project ships a console script (in `.venv/bin/gpt-pro-relay`) that's symlinked into a directory on the SSH non-interactive `PATH` (e.g. `~/.local/bin/gpt-pro-relay`). If you get `gpt-pro-relay: command not found`, the symlink isn't set up — fall back to the absolute venv path (`<repo>/.venv/bin/gpt-pro-relay`) or follow the repo's "Optional: bare command on PATH" setup.

```bash
RUN_ID="ask-$(date -u +%Y%m%dT%H%M%SZ)-$(uuidgen | tr '[:upper:]' '[:lower:]')"

ssh -o ServerAliveInterval=30 -o ServerAliveCountMax=10 mac \
    gpt-pro-relay ask --run-id "$RUN_ID" <<'PROMPT'
... the prompt ...
PROMPT
```

- **stdout** = the ChatGPT response (markdown, captured via the page's Copy button when possible)
- **stderr** = newline-delimited JSON: a `submitted` line first, then a terminal `ok` / `error` / `timeout` line. The `ok` line includes `extraction: "copy_button" | "innertext"` so you can audit which capture path won.
- **exit 0** = success. Other codes mean inspect stderr.

**Always pass `--run-id`.** A caller-supplied id is the recovery handle if SSH drops. Use a UUID or timestamp+UUID — anything matching `[A-Za-z0-9._-]+`.

Use a heredoc, never `echo "$prompt"` — bare echo mangles `$`, backticks, and quotes.

## Output: stdout or file

By default the response is on stdout. If you'd rather end up with a markdown file (for `Read`, large responses, or to keep your own stdout pipeline clean), two patterns work:

**Shell redirect** — simpler, file lives on the *caller's* machine:

```bash
ssh -o ServerAliveInterval=30 mac \
    gpt-pro-relay ask --run-id "$RUN_ID" <<'PROMPT' > /tmp/response-$RUN_ID.md
... the prompt ...
PROMPT
```

**`--output PATH`** — file lives on mac (the gpt-pro-relay host); stdout is empty; terminal stderr JSON gains `"output": "<resolved-path>"`:

```bash
ssh mac gpt-pro-relay \
    ask --run-id "$RUN_ID" --output ~/responses/$RUN_ID.md <<'PROMPT'
... the prompt ...
PROMPT
# Read it back from mac if the caller needs the contents locally:
ssh mac cat ~/responses/$RUN_ID.md
```

Use shell redirect when you want the file on the caller's machine — one fewer hop. Use `--output` when driving gpt-pro-relay from the same host (e.g. an agent running directly on mac), where the file can be `Read` directly. `fetch` accepts `--output` too.

## Cost gate

Pro Extended runs cost real Pro quota and take 5–20 minutes per prompt. Confirm with the user before invoking *unless* they explicitly named gpt-pro:

> "Send this to gpt-pro? It'll take ~5–20 min and use your Pro quota."

If they invoked the skill directly or named gpt-pro in their request, they've consented — just go.

## Background and timeout

`gpt-pro-relay ask` blocks for the full reasoning duration. Always:

- `run_in_background: true`
- `timeout: 1800000` (30 min, well above typical max)

Wait for the completion notification. Do NOT poll the output file.

## SSH-drop recovery

If the background task completes with a non-zero exit AND you have the `run_id`, do NOT re-run `ask` — that submits a fresh prompt and burns another 5–20 min of Pro reasoning. Instead:

```bash
ssh -o ServerAliveInterval=30 mac \
    gpt-pro-relay fetch "$RUN_ID"
```

The detached worker on mac survived the SSH drop. `fetch` polls `result.json` and prints the response on stdout when ready. Same exit codes as `ask`.

Quick "is it done yet" check (non-blocking):

```bash
ssh mac gpt-pro-relay fetch "$RUN_ID" --timeout 0
```

Exit 124 means still running. Exit 4 means `not_found` — the run never reached mac (SSH died before the parent read stdin).

## Idempotent re-attach

Re-running `ask` with the **same `--run-id` and the same prompt bytes** attaches to the existing run instead of submitting a new one. The `submitted` JSONL line will include `"attached": true`. Useful when you want a single command that handles both fresh and recovery cases without branching.

Re-running with the same `run_id` but a **different** prompt exits 2 with `run_id_conflict` — gpt-pro-relay refuses to overwrite, by design.

## Concurrency

Two simultaneous `ask` calls to mac serialize on a file lock — the second worker waits for the first to release Chrome before launching its own. From the caller's perspective, this just looks like the second run took longer than usual. The wait is recorded in `worker.stderr` as `{"stage":"lock_acquired","waited_secs":N}`. Pro Extended runs are 5–20 min, so an extra wait is in the same magnitude — don't add caller-side timeouts shorter than that.

## Errors

The terminal stderr JSON's `reason` field tells you what failed:

| reason | meaning | what to do |
|---|---|---|
| `needs_reauth` | session cookie missing or expired | Tell the user to run `gpt-pro-relay login` on mac |
| `model_select_failed` | couldn't get Pro selected in the picker | Selectors drifted; surface `run_dir` to the user |
| `reasoning_mismatch` | Extended Pro chip absent after model select | Same — selectors drifted |
| `worker_exception` | Python exception in the worker | Inspect `run_dir/worker.stderr` (structured stage trace) — the last `stage` before the error tells you where it died |
| `timeout` | no completion within 35 min | Inspect `run_dir/streaming-*.png` |
| `empty_prompt` | nothing on stdin | You forgot the heredoc |
| `run_id_conflict` | same run_id, different prompt | Pick a fresh run_id |
| `not_found` | fetch couldn't find run_dir | The `ask` parent died before submission; re-submit fresh |

## Exit codes

| code | meaning |
|---|---|
| 0 | response on stdout, status ok |
| 1 | error — read stderr `reason` |
| 2 | usage error (empty prompt, conflict, invalid run_id) |
| 3 | worker `timeout` (didn't finish within 35 min) |
| 4 | run_dir not found (fetch only) |
| 124 | wait timed out, run still pending |

## Run artifacts

`run_dir` lives on mac at `~/.gpt-pro/runs/<run_id>/`:

- `prompt.md`, `response.md`, `meta.json`, `result.json`
- `pre-send.png`, `streaming-NNN.png`, `final.png`, `error-*.png`
- `final.html`, `network.json`
- `worker.stdout` — detached worker's stdout (usually empty)
- `worker.stderr` — **structured JSONL stage trace**: one line per stage (`start`, `lock_acquired`, `chrome_launched`, `logged_in`, `model_selected`, `prompt_typed`, `sent`, `completion_detected`, `extracted`, `finished`, plus `error` / `orphan_kill_*`). When something fails mid-run, this is the fastest path to the failure point.

Reach for them via `ssh mac cat <run_dir>/<file>` or `ssh mac ls <run_dir>` when diagnosing.

## When pro-relay fits

| Situation | Verdict |
|---|---|
| Pro Extended reasoning, from any machine with SSH to your Mac | Yes |
| Tolerating a flaky network on a 5–20 min reasoning run | Yes (drop-recovery via `fetch`) |
| Same machine as ChatGPT — no SSH needed | Use the `gpt-pro-local` skill instead |
| Multi-turn follow-ups in the same chat | Doesn't fit — pro-relay is one-shot per invocation |

## Multi-turn

pro-relay is one-shot per invocation — every call is a fresh ChatGPT conversation. To continue a thread, paste the prior response into the next prompt yourself. The dedicated profile retains login but does not persist conversation context across calls.
