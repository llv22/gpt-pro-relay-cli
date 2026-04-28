# CLAUDE.md

Repo-specific notes for future Claude sessions. See README.md for what the tool does and how to use it.

## Architecture invariants — do not relax without a concrete reason

The shape was chosen after a multi-perspective design review converged on the smallest system that meets the goal. Don't add the components below without checking first:

- **No HTTP server.** SSH is the transport — it already provides auth, encryption, blocking-wait, cancellation. Don't add FastAPI.
- **No queue, no SQLite.** Single-tab single-user. A file lock is the right concurrency primitive when concurrency becomes real.
- **No daemon / launchd.** `tmux` is enough until something specifically demands persistence.
- **One `launch_kwargs()`** in `cli.py` — login, doctor, and `_run` (worker) all use the *exact* same Chrome flags. Diverging flags = subtle auth drift.
- **Real Chrome (`channel="chrome"`)**, not bundled Chromium. Auth/anti-abuse behaves differently.
- **Fail closed on model + reasoning.** The worker opens the picker, asserts `aria-checked="true"` on the Pro item, clicks if not, re-verifies. Same idempotent pattern for the Extended Pro chip after typing the prompt. Never send to a model we haven't verified.

## ask / fetch / _run — the submit-and-wait architecture

`ask` does NOT drive the browser directly. It's a supervisor:

1. Read prompt from stdin, validate `--run-id` (or generate one).
2. If run_dir exists with same `prompt_sha256` → attach (no respawn). With different sha → exit 2 `run_id_conflict`.
3. Otherwise: write `prompt.md` and `meta.json` atomically, emit a `submitted` JSONL line on stderr, spawn a detached `_run` worker via `subprocess.Popen(..., start_new_session=True, stdin=DEVNULL, stdout/stderr → run_dir/worker.{stdout,stderr})`.
4. Poll `result.json` until terminal or `--generation-timeout`.
5. On terminal `ok`: print `response.md` to stdout, terminal JSONL to stderr, exit 0.

`_run` (hidden subcommand) reads `run_dir/prompt.md`, runs the Playwright flow, writes `response.md` and `result.json` atomically. It survives `SIGHUP` because `start_new_session=True` puts it in its own session — that's how SSH-drop recovery works. **Do not catch `SIGHUP`** in the worker; the survival depends on it.

`fetch <run-id>` reads `result.json` (polling if not yet present, with `--timeout`). Same dispatch logic as the parent's post-wait. `--timeout 0` is a non-blocking check.

The split is the recovery story: if the parent (or the SSH session containing it) dies, the worker keeps running. The agent reconnects with the same `--run-id` and either re-runs `ask` (idempotent attach) or runs `fetch`. Never re-submit a fresh prompt — that costs another 5–20 min of Pro reasoning.

## Atomic writes

Use `atomic_write(path, content)` for `prompt.md`, `meta.json`, `response.md`, `result.json`. Pattern: write `path.tmp`, then `os.replace(tmp, path)` (POSIX atomic). `fetch` reads `result.json` concurrently with the worker writing it, so the rename guarantees a consistent read.

## Selectors — expect breakage here

ChatGPT changes these without notice. Current truth (verified via `gpt-pro doctor` artifacts):

- Picker button: `[data-testid="model-switcher-dropdown-button"]`
- Pro menuitem: `[data-testid="model-switcher-gpt-5-5-pro"]`, `aria-checked` is the selection signal
- Reasoning chip: `[aria-label="Extended Pro, click to remove"]` — only visible when composer has content; "click to remove" suffix means active
- Composer: `page.get_by_role("textbox").first` (works for textarea or contenteditable)
- Send button: `[data-testid="send-button"]`
- Assistant messages: `[data-message-author-role="assistant"]`

When `_run` starts failing with `model_select_failed` or `reasoning_mismatch`, the screenshots in `~/.gpt-pro/runs/<run_id>/error-*.png` are the diagnostic.

## Other fragile assumptions

- **Login detection** uses cookie prefix `__Secure-next-auth.session-token` (NextAuth chunked cookies — `.0`, `.1`). If OpenAI changes their auth scheme, update `SESSION_COOKIE_PREFIX`.
- **Completion detection is heuristic** (text-stable for 5s + no Stop button). The cleaner signal is `/backend-api/conversation/<id>/async-status` from the network log; not yet wired.
- **Anti-detection flags** (`--disable-blink-features=AutomationControlled`, dropping `--enable-automation` and `--no-sandbox`) are load-bearing. Removing them triggers ChatGPT's auth-error redirect.

## Conventions

- Python 3.11+, `uv` for env management. `uv venv` + `uv sync`. Never `pip` directly.
- Stdout = response. Stderr = newline-delimited JSON. Don't mix them — the SSH UX depends on this split.
- Artifacts go to `~/.gpt-pro/runs/<run_id>/`. The run_id is caller-supplied via `--run-id` (recommended) or auto-generated.
- Profile dir is `~/.gpt-pro-profile/` — outside the repo, never committed.
- Worker spawned via `[sys.executable, "-m", "gpt_pro.cli", "_run", run_id]` so it works regardless of how the parent was invoked.

## ToS reality

Browser automation against ChatGPT violates OpenAI's terms. The user accepts the account-ban risk for personal use. Don't suggest making this multi-tenant or productizing it.

## What is intentionally NOT built

So future-Claude doesn't reflexively add it:

- File lock for concurrent invocations — known gap. Two simultaneous `ask` calls will collide on Chrome's `SingletonLock`; one fails. Add when needed.
- Markdown-fidelity extraction (Copy button or SSE parse) — `innerText` is good enough for v1.
- `async-status` completion signal — heuristic works.
- Auto-retry on errors — fail closed, surface the `run_dir`, let the user decide.
- `launchd` keepalive — `tmux` is fine.
- Sleep / clamshell handling beyond `caffeinate` — out of scope.
- Worker liveness check / orphan detection — if the worker dies before writing `result.json`, the parent waits until `--generation-timeout`. Acceptable.
