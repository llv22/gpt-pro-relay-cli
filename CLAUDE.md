# CLAUDE.md

Repo-specific notes for future Claude sessions. See README.md for what the tool does and how to use it.

## Architecture invariants — do not relax without a concrete reason

The shape was chosen after a multi-perspective design review converged on the smallest system that meets the goal. Don't add the components below without checking first:

- **No HTTP server.** SSH is the transport — it already provides auth, encryption, blocking-wait, cancellation. Don't add FastAPI.
- **No queue, no SQLite.** Single-tab single-user. A file lock is the right concurrency primitive when concurrency becomes real.
- **No daemon / launchd.** `tmux` is enough until something specifically demands persistence.
- **One `launch_kwargs()`** in `cli.py` — login, doctor, and `_run` (worker) all use the *exact* same Chrome flags. Diverging flags = subtle auth drift.
- **Real Chrome (`channel="chrome"`)**, not bundled Chromium. Auth/anti-abuse behaves differently.
- **Fail closed on model + reasoning.** The worker reads the composer chip's text and asserts it *contains* `"Extended"` — Extended reasoning is gated to Pro models, so any "Extended" label verifies both axes. Match by predicate (`"Extended" in text`), never exact string: ChatGPT renders this label inconsistently (`"Extended"` and `"Extended Pro"` both observed in production within hours of each other; varies with A/B tests and the chip's responsive truncation classes). Tightening this back to exact match caused a same-day regression — don't. Never send to a model we haven't verified.

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

ChatGPT changes these without notice. Current truth (verified via `gpt-pro-relay doctor` artifacts):

- Model+reasoning chip (composer-embedded): `button.__composer-pill[aria-haspopup="menu"]`. Visible text is the entire signal — any label *containing* `"Extended"` (e.g. `"Extended"` or `"Extended Pro"`) means GPT-5.5 Pro + Extended reasoning. The chip's SSR text is `"Model"` until React hydrates, so always wait for visible state before reading. There is no separate top-bar model picker anymore (removed in the 2026-04 redesign).
- **Chip menu is two levels.** When chip text doesn't match the Extended predicate, the worker opens the chip menu (which contains rows like "Latest / Instant / Thinking / Pro / Configure...") and navigates a per-row "Effort" submenu. Pro's effort submenu is opened by triggering `[data-testid="model-switcher-gpt-5-5-pro-thinking-effort"]` (don't confuse with `gpt-5-5-thinking-thinking-effort` — that's a different model). The trigger is a "trailing button" rendered with Tailwind `invisible` and `pointer-events-none` until the parent row is hovered (`group-hover/model-picker-thinking-effort-row:visible`). Playwright `.hover()` does not reliably activate the CSS `:hover` state for this affordance — instead, dispatch a synthetic native click via `trigger.evaluate("el => el.click()")`. Radix wires the submenu state to the click handler, not pointer events, so this works regardless of visibility. The submenu's leaves use `role="menuitemradio"` (not `menuitem`) and have plain text labels ("Standard", "Extended").
- Composer: `page.get_by_role("textbox").first` (works for textarea or contenteditable; the underlying `id="prompt-textarea"` is the contenteditable)
- Send button: `[data-testid="send-button"]` is still primary; resilient fallback is `button[aria-label="Send prompt"]` / `button[aria-label="Send message"]`. Send button is only mounted when composer has content — never assert it before pasting.
- Assistant messages: `[data-message-author-role="assistant"]`
- Copy button (extraction): `[data-testid="copy-turn-action-button"]` inside the assistant message's `[data-testid^="conversation-turn"]` container.

When `_run` starts failing with `model_select_failed`, the screenshots + `error.html` in `~/.gpt-pro/runs/<run_id>/` are the diagnostic. The `error.html` is `page.content()` at the moment of failure — grep it for testids and aria-labels to find the new selectors before patching blind.

## Other fragile assumptions

- **Login detection** uses cookie prefix `__Secure-next-auth.session-token` (NextAuth chunked cookies — `.0`, `.1`). If OpenAI changes their auth scheme, update `SESSION_COOKIE_PREFIX`.
- **Completion detection is heuristic** (text-stable for 5s + no Stop button). The async-status endpoint fires exactly once at the end of a run — our heuristic catches the same moment, so the endpoint is redundant for completion. If the heuristic ever false-positives mid-run, async-status is the obvious supplementary check to add.
- **Anti-detection flags** (`--disable-blink-features=AutomationControlled`, dropping `--enable-automation` and `--no-sandbox`) are load-bearing. Removing them triggers ChatGPT's auth-error redirect.
- **Extraction prefers the Copy button** via `[data-testid="copy-turn-action-button"]` (clean markdown), then `pbpaste` reads the system clipboard; falls back to `innerText` if either step fails. Result captures `extraction: "copy_button"|"innertext"` so you can audit which path won. Math, code fences, and tables are mangled under `innerText` — the fallback is only for degraded environments.
- **Concurrent worker serialization** is a `fcntl.flock` exclusive lock on `~/.gpt-pro/browser.lock` held during the entire browser section. Required because Chrome's `SingletonLock` prevents two processes sharing a `--user-data-dir`. Kept blocking (no timeout) — agents wait their turn rather than fail-fast, which matches the queue-up-and-respond UX of the rest of the system.

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

- `async-status` completion signal — heuristic catches the same moment empirically. Wire only if the heuristic ever false-positives.
- Auto-retry on errors — fail closed, surface the `run_dir`, let the user decide.
- `launchd` keepalive — `tmux` is fine.
- Sleep / clamshell handling beyond `caffeinate` — out of scope.
- Worker liveness check / orphan detection — if the worker dies before writing `result.json`, the parent waits until `--generation-timeout`. Acceptable.
- Lock timeout / fail-fast on busy profile — current behavior is to wait. Pro Extended runs are 5–20 min anyway; an extra wait is in the same magnitude.
