---
name: pro-relay
description: |
  Send a prompt to ChatGPT Pro Extended via gpt-pro-relay running on a remote Mac, over SSH.
  Use this whenever the user wants a deep ChatGPT Pro response from a machine that's not their
  primary Chrome session — triggers on "ask gpt-pro", "send to gpt-pro", "use gpt-pro", "get a
  Pro Extended take", "ask the deep model", "second opinion from chatgpt pro". Returns response
  on stdout. Resilient to flaky networks via a short-session polling pattern (`ask --no-wait`
  followed by `fetch --timeout 60` in a retry loop) — no single SSH session ever sits idle for
  the full 5–20 min reasoning duration. Replace the `mac` SSH alias below with your own;
  `gpt-pro-relay` is assumed to be on the remote shell's PATH (see the repo's "Optional: bare
  command on PATH" setup note).
allowed-tools: Bash(ssh:*), Bash(uuidgen:*), Bash(date:*), Read, Write
user-invocable: true
---

# pro-relay

One prompt in, one response out. The browser automation runs on mac behind SSH against a dedicated logged-in profile. The work is done by a detached worker so SSH drops don't kill it — you can reconnect and `fetch` the result.

## The command

> **About the bare `gpt-pro-relay` command:** it's not a system tool. The remote shell finds it because the project ships a console script (in `.venv/bin/gpt-pro-relay`) that's symlinked into a directory on the SSH non-interactive `PATH` (e.g. `~/.local/bin/gpt-pro-relay`). If you get `gpt-pro-relay: command not found`, the symlink isn't set up — fall back to the absolute venv path (`<repo>/.venv/bin/gpt-pro-relay`) or follow the repo's "Optional: bare command on PATH" setup.

Two phases: a 1-second `--no-wait` submit, then a polling loop where each
SSH session lasts ≤60s. A NAT/firewall idle-drop on any single session
just costs one retry instead of the whole run.

```bash
SSH_OPTS=(-S none -o BatchMode=yes -o ConnectTimeout=15 -o ServerAliveInterval=15 -o ServerAliveCountMax=4)
RUN_ID="ask-$(date -u +%Y%m%dT%H%M%SZ)-$(uuidgen | tr '[:upper:]' '[:lower:]')"

# Phase 1: submit (≤1s SSH session; idempotent on same run_id + same prompt)
ssh "${SSH_OPTS[@]}" mac gpt-pro-relay ask --run-id "$RUN_ID" --no-wait <<'PROMPT'
... the prompt ...
PROMPT

# Phase 2: poll (each SSH session ≤60s, exponential backoff on transport drop)
deadline=$((SECONDS + 3600)); delay=5
while (( SECONDS < deadline )); do
  out=$(ssh "${SSH_OPTS[@]}" mac gpt-pro-relay fetch "$RUN_ID" --timeout 60 2>/tmp/gpt-pro-$RUN_ID.err); rc=$?
  case $rc in
    0)   printf '%s' "$out"; exit 0 ;;
    124) delay=5; continue ;;                              # still pending
    255) sleep "$delay"; (( delay < 30 )) && delay=$((delay * 2)) ;;  # ssh died
    *)   cat /tmp/gpt-pro-$RUN_ID.err >&2; exit "$rc" ;;   # terminal error
  esac
done
echo "gpt-pro-relay overall timeout for $RUN_ID" >&2; exit 124
```

- **stdout** of the whole block = the ChatGPT response (markdown, captured via Copy button when possible)
- **stderr** = newline-delimited JSON: a `submitted` line from Phase 1, then a terminal `ok` / `error` / `timeout` from the final fetch. The `ok` line includes `extraction: "copy_button" | "innertext"` so you can audit which capture path won.
- **exit 0** = success. Other codes mean inspect stderr.

Always pass `--run-id`. Use a UUID or timestamp+UUID — anything matching `[A-Za-z0-9._-]+`. Same id + same prompt bytes attaches to an existing run instead of submitting a new one (`submitted` JSONL gains `"attached": true`), so the Phase 1 submit is safe to retry on transport flakiness.

Use a heredoc, never `echo "$prompt"` — bare echo mangles `$`, backticks, and quotes.

### SSH options (load-bearing)
- `ConnectTimeout=15` — bail in 15s on a dead connect.
- `ServerAliveInterval=15` + `ServerAliveCountMax=4` — bound a dead established session to ~60s.
- `BatchMode=yes` — never prompt for a password (would hang an agent forever).
- `-S none` — no ControlMaster reuse; reuse can resurrect a stale network path.

### Fallback: blocking single-call

For stable links (or local invocation, where you should prefer the `gpt-pro-local` skill anyway), the older blocking form still works:

```bash
ssh "${SSH_OPTS[@]}" mac gpt-pro-relay ask --run-id "$RUN_ID" <<'PROMPT'
... the prompt ...
PROMPT
```

This holds the SSH session open for the full 5–20 min reasoning duration. A single NAT/firewall idle-drop kills the run from the caller's view (the worker on mac survives — recover with `gpt-pro-relay fetch $RUN_ID`, ideally inside the polling loop). Default to the polling pattern.

## Output: stdout or file

By default the response is on stdout (from the polling block's `printf '%s' "$out"` on success). If you'd rather end up with a file:

**Caller-side redirect** — file on the *caller's* machine:

```bash
# In the polling block, replace the success branch with:
0)   printf '%s' "$out" > /tmp/response-$RUN_ID.md; exit 0 ;;
```

**`--output PATH` on `fetch`** — file on mac; the polling block's stdout stays empty; terminal stderr JSON gains `"output": "<resolved-path>"`:

```bash
# Replace the fetch line in the polling block with:
out=$(ssh "${SSH_OPTS[@]}" mac gpt-pro-relay fetch "$RUN_ID" --output ~/responses/$RUN_ID.md --timeout 60 2>/tmp/gpt-pro-$RUN_ID.err); rc=$?
# Read it back from mac when the run is done:
ssh "${SSH_OPTS[@]}" mac cat ~/responses/$RUN_ID.md
```

Caller-side redirect is one fewer SSH hop. `--output` on `fetch` is mainly useful when driving gpt-pro-relay from the same host (use the `gpt-pro-local` skill instead). `--output` on `ask --no-wait` is silently ignored — the response only exists at fetch time.

## Cost gate

Pro Extended runs cost real Pro quota and take 5–20 minutes per prompt. Confirm with the user before invoking *unless* they explicitly named gpt-pro:

> "Send this to gpt-pro? It'll take ~5–20 min and use your Pro quota."

If they invoked the skill directly or named gpt-pro in their request, they've consented — just go.

## Background and timeout

The polling block above runs up to 60 min wall-clock. Always wrap the whole bash invocation in:

- `run_in_background: true`
- `timeout: 3600000` (60 min)

Wait for the completion notification. Do NOT poll the output file from the agent side — the bash loop is already polling.

## Manual recovery

If you launched a *blocking* `ask` (not the polling pattern) and SSH dropped, do NOT re-run `ask` without `--no-wait` — that holds another long SSH session open. Recover by entering the polling loop with the same `RUN_ID`, or do a one-shot manual fetch:

```bash
ssh "${SSH_OPTS[@]}" mac gpt-pro-relay fetch "$RUN_ID"   # blocks until ready
ssh "${SSH_OPTS[@]}" mac gpt-pro-relay fetch "$RUN_ID" --timeout 0  # non-blocking check
```

Exit 124 = still running. Exit 4 = `not_found` (the run never reached mac — SSH died before the parent read stdin; submit again with the same run_id).

## Idempotent re-attach

`ask` with the **same `--run-id` and same prompt bytes** attaches to the existing run instead of submitting a new one. The `submitted` JSONL gains `"attached": true`. This is what makes Phase 1 of the polling pattern safe to retry — if the submit SSH session drops mid-flight, just run it again with the same `RUN_ID`.

Same `run_id` with a **different** prompt exits 2 with `run_id_conflict` — gpt-pro-relay refuses to overwrite, by design.

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
| `timeout` | no completion within 60 min | Inspect `run_dir/streaming-*.png` |
| `empty_prompt` | nothing on stdin | You forgot the heredoc |
| `prompt_too_large` | prompt > 1 MB | Trim or split the prompt; the cap is at submission, no Pro quota burned |
| `run_id_conflict` | same run_id, different prompt | Pick a fresh run_id |
| `not_found` | fetch couldn't find run_dir | The `ask` parent died before submission; re-submit fresh |

## Exit codes

| code | meaning |
|---|---|
| 0 | response on stdout, status ok (or `ask --no-wait` submitted; nothing on stdout) |
| 1 | error — read stderr `reason` |
| 2 | usage error (empty prompt, prompt_too_large, conflict, invalid run_id) |
| 3 | worker `timeout` (didn't finish within 60 min) |
| 4 | run_dir not found (fetch only) |
| 124 | wait timed out, run still pending |
| 255 | SSH transport failure (the polling loop catches this and retries) |

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
| Tolerating a flaky network on a 5–20 min reasoning run | Yes — the polling pattern handles drops without intervention |
| Same machine as ChatGPT — no SSH needed | Use the `gpt-pro-local` skill instead |
| Multi-turn follow-ups in the same chat | Doesn't fit — pro-relay is one-shot per invocation |

## Multi-turn

pro-relay is one-shot per invocation — every call is a fresh ChatGPT conversation. To continue a thread, paste the prior response into the next prompt yourself. The dedicated profile retains login but does not persist conversation context across calls.
