---
name: gpt-pro-local
description: |
  Send a prompt to ChatGPT Pro Extended via gpt-pro-relay running on the SAME machine as
  Claude Code — no SSH wrapper. Use this when the gpt-pro-relay binary is on the local PATH
  (e.g. you cloned the repo, ran `uv sync`, and either symlinked into `~/.local/bin/` or
  activated the venv). Triggers on "ask gpt-pro", "send to gpt-pro", "use gpt-pro", "get a
  Pro Extended take", "ask the deep model", "second opinion from chatgpt pro" — same triggers
  as the SSH-wrapped variant. For the SSH-wrapped variant, install the `pro-relay` skill
  instead. Returns response on stdout. Resilient to parent-process death via caller-supplied
  `--run-id` plus a `fetch` recovery command.
allowed-tools: Bash(gpt-pro-relay:*), Bash(uuidgen:*), Bash(date:*), Read, Write
user-invocable: true
---

# gpt-pro-local

Same as `pro-relay`, minus SSH. The browser automation runs on the same machine as Claude Code, against a dedicated logged-in profile. The work is done by a detached worker so a parent-process crash doesn't kill it — the run survives and `fetch` recovers it.

## The command

> **About the bare `gpt-pro-relay` command:** it's not a system tool. It's a console script in the project's venv (`<repo>/.venv/bin/gpt-pro-relay`). To call it bare, either symlink it into `~/.local/bin/` (see the repo's "Optional: bare command on PATH" setup) or run via `uv run --project <repo> gpt-pro-relay ...`. If you get `gpt-pro-relay: command not found`, fall back to the absolute venv path.

```bash
RUN_ID="ask-$(date -u +%Y%m%dT%H%M%SZ)-$(uuidgen | tr '[:upper:]' '[:lower:]')"

gpt-pro-relay ask --run-id "$RUN_ID" <<'PROMPT'
... the prompt ...
PROMPT
```

- **stdout** = the ChatGPT response (markdown, captured via the page's Copy button when possible)
- **stderr** = newline-delimited JSON: a `submitted` line first, then a terminal `ok` / `error` / `timeout` line. The `ok` line includes `extraction: "copy_button" | "innertext"` so you can audit which capture path won.
- **exit 0** = success. Other codes mean inspect stderr.

**Always pass `--run-id`.** Even without SSH, a caller-supplied id is the recovery handle if the parent process dies (or you Ctrl-C by mistake). Use a UUID or timestamp+UUID — anything matching `[A-Za-z0-9._-]+`.

Use a heredoc, never `echo "$prompt"` — bare echo mangles `$`, backticks, and quotes.

### Fire-and-forget variant

If the calling process is itself fragile (e.g. you're about to swap shells), submit without blocking and fetch later:

```bash
gpt-pro-relay ask --run-id "$RUN_ID" --no-wait <<<prompt   # exits 0 in ~1s after spawning the worker
gpt-pro-relay fetch "$RUN_ID"                              # blocks until response ready
```

## Output: stdout or file

Default: response on stdout. Pass `--output PATH` to write to a file instead — handy for `Read`-ing large responses without piping through the model's context:

```bash
gpt-pro-relay ask --run-id "$RUN_ID" --output ~/responses/$RUN_ID.md <<'PROMPT'
... the prompt ...
PROMPT
```

stdout stays empty; the terminal stderr JSON gains `"output": "<resolved-path>"`. The file is local — `Read` it directly. `fetch` accepts `--output` too.

## Cost gate

Pro Extended runs cost real Pro quota and take 5–20 minutes per prompt. Confirm with the user before invoking *unless* they explicitly named gpt-pro:

> "Send this to gpt-pro? It'll take ~5–20 min and use your Pro quota."

If they invoked the skill directly or named gpt-pro in their request, they've consented — just go.

## Background and timeout

`gpt-pro-relay ask` blocks for the full reasoning duration. Always:

- `run_in_background: true`
- `timeout: 1800000` (30 min, well above typical max)

Wait for the completion notification. Do NOT poll the output file.

## Recovery after parent-process death

If the background task completes with a non-zero exit AND you have the `run_id`, do NOT re-run `ask` — that submits a fresh prompt and burns another 5–20 min of Pro reasoning. The detached worker survives parent death. Recover with:

```bash
gpt-pro-relay fetch "$RUN_ID"
```

`fetch` polls `result.json` and prints the response on stdout when ready. Same exit codes as `ask`.

Quick "is it done yet" check (non-blocking):

```bash
gpt-pro-relay fetch "$RUN_ID" --timeout 0
```

Exit 124 means still running. Exit 4 means `not_found` — the run never started (parent died before the worker forked).

## Idempotent re-attach

Re-running `ask` with the **same `--run-id` and the same prompt bytes** attaches to the existing run instead of submitting a new one. The `submitted` JSONL line will include `"attached": true`. Useful when you want a single command that handles both fresh and recovery cases without branching.

Re-running with the same `run_id` but a **different** prompt exits 2 with `run_id_conflict` — gpt-pro-relay refuses to overwrite, by design.

## Concurrency

Two simultaneous `ask` calls serialize on a `flock` at `~/.gpt-pro/browser.lock` — the second worker waits for the first to release Chrome before launching its own. From the caller's perspective, this just looks like the second run took longer than usual. The wait is recorded in `worker.stderr` as `{"stage":"lock_acquired","waited_secs":N}`. Pro Extended runs are 5–20 min, so an extra wait is in the same magnitude — don't add caller-side timeouts shorter than that.

## Errors

The terminal stderr JSON's `reason` field tells you what failed:

| reason | meaning | what to do |
|---|---|---|
| `needs_reauth` | session cookie missing or expired | Tell the user to run `gpt-pro-relay login` |
| `model_select_failed` | couldn't get Pro selected in the picker | Selectors drifted; surface `run_dir` to the user |
| `reasoning_mismatch` | Extended Pro chip absent after model select | Same — selectors drifted |
| `worker_exception` | Python exception in the worker | Inspect `run_dir/worker.stderr` (structured stage trace) — the last `stage` before the error tells you where it died |
| `timeout` | no completion within 60 min | Inspect `run_dir/streaming-*.png` |
| `empty_prompt` | nothing on stdin | You forgot the heredoc |
| `run_id_conflict` | same run_id, different prompt | Pick a fresh run_id |
| `not_found` | fetch couldn't find run_dir | The `ask` parent died before submission; re-submit fresh |

## Exit codes

| code | meaning |
|---|---|
| 0 | response on stdout, status ok |
| 1 | error — read stderr `reason` |
| 2 | usage error (empty prompt, conflict, invalid run_id) |
| 3 | worker `timeout` (didn't finish within 60 min) |
| 4 | run_dir not found (fetch only) |
| 124 | wait timed out, run still pending |

## Run artifacts

`run_dir` is at `~/.gpt-pro/runs/<run_id>/`:

- `prompt.md`, `response.md`, `meta.json`, `result.json`
- `pre-send.png`, `streaming-NNN.png`, `final.png`, `error-*.png`
- `final.html`, `network.json`
- `worker.stdout` — detached worker's stdout (usually empty)
- `worker.stderr` — **structured JSONL stage trace**: one line per stage (`start`, `lock_acquired`, `chrome_launched`, `logged_in`, `model_selected`, `prompt_typed`, `sent`, `completion_detected`, `extracted`, `finished`, plus `error` / `orphan_kill_*`). When something fails mid-run, this is the fastest path to the failure point.

Since you're local, `Read` them directly when diagnosing.

## When gpt-pro-local fits

| Situation | Verdict |
|---|---|
| Pro Extended reasoning on the same Mac as Claude Code | Yes |
| Tolerating parent-process death on a 5–20 min run | Yes (drop-recovery via `fetch`) |
| Driving a remote Mac over SSH | Use `pro-relay` instead |
| Multi-turn follow-ups in the same chat | Doesn't fit — one-shot per invocation |

## Multi-turn

gpt-pro-local is one-shot per invocation — every call is a fresh ChatGPT conversation. To continue a thread, paste the prior response into the next prompt yourself. The dedicated profile retains login but does not persist conversation context across calls.
