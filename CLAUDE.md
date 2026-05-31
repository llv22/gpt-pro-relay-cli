# CLAUDE.md

Repo-specific notes for future Claude sessions. See README.md for what the tool does and how to use it.

## Architecture invariants — do not relax without a concrete reason

The shape was chosen after a multi-perspective design review converged on the smallest system that meets the goal. Don't add the components below without checking first:

- **No HTTP server.** SSH is the transport — it already provides auth, encryption, blocking-wait, cancellation. Don't add FastAPI.
- **No queue, no SQLite.** Concurrency is admitted via *file-lock primitives*, not a queue service. The three locks (`LaunchLock`, `UiClipboardLock`, `ParallelSlot`) each guard a *brief* critical section; the long-running per-tab work runs lock-free.
- **No daemon / launchd.** `tmux` is enough until something specifically demands persistence. Chrome itself is the persistence — once a worker (or `login`/`doctor`) launches it, subsequent workers connect over CDP to the same process. Workers never `browser.close()` Chrome; `gpt-pro-relay close-chrome` is the only authorized teardown.
- **One `launch_kwargs()`** in `cli.py` — `login`, `doctor`, and `_run` all use the *exact* same `_chrome_open_argv()`. Diverging flags = subtle auth drift. Only the *launching* worker exercises the flag set; followers `connect_over_cdp` and never re-launch, so flag drift is impossible by construction.
- **Real Chrome (`channel="chrome"`)**, not bundled Chromium. Auth/anti-abuse behaves differently.
- **Fail closed on model + reasoning.** The fast-path requires the chip text to contain *both* `"Extended"` and `"Pro"` tokens (`is_pro_extended_label`). The chip exposes no model-axis signal beyond its visible label — no aria-label, no dataset key, no hidden mirror text — so a label of just `"Extended"` is ambiguous (could be Pro+Extended truncated, or Thinking+Extended). Demanding `"Pro"` too closes the gap. Use predicate matching (substring tests, not exact-string equality): ChatGPT renders the full label inconsistently (`"Extended Pro"` confirmed; earlier today `"Extended"` alone was also observed, with no way to know which model). Post-click confirmation in `ensure_extended_chip` can be looser (just `"Extended"` in text) since we already navigated through Pro's effort submenu — but the fast-path must be strict. Never send to a model we haven't verified.
- **The model chip is verified at three points, not one — it's a time-of-check/time-of-use problem.** Reading the chip once after page load is insufficient: the pill hydrates optimistically to the persisted "Extended Pro" and then an async resolution re-resolves a new conversation's default to "Thinking" within ~2.6s. Run `ask-20260531T065451Z` logged `model_verified="Extended Pro"` yet was served `gpt-5-5-thinking` (≈12% of runs hit this). The three layers, all fail-closed: (1) **`read_composer_chip_text` requires a stable read** — the same non-placeholder label for `stable_polls` (default 3) consecutive ~0.2s polls; on timeout it returns `""` (never the last transient), so an oscillating chip fails the predicate. (2) **Pre-send re-verify** — immediately before the Send click, re-read the chip (a passive `inner_text()`, *no* `UiClipboardLock`, *no* menu) and `model_drift_before_send` if it isn't `is_pro_extended_label`. Do NOT re-run the chip menu here (needs the lock, fragile Radix nav on a loaded composer) — fail closed and surface the run_dir. (3) **Served-slug audit** — after completion, read the served turn's `data-message-model-slug`; a present slug not in `PRO_MODEL_SLUGS` (an explicit allowlist, *not* a `startswith` test) is fatal *regardless of* `completed`. A *missing* slug degrades fail-OPEN (returns `ok` with `model_audit: "unverified_missing_slug"` + a `served_model_unverified` stage) — making it fatal would brick the tool on a single attribute rename, the same blast radius the network-body-gate was rejected for. The audit verifies the *model* only; the pre-send chip ("Extended" + "Pro") is the sole *effort* signal, so a server-side effort downgrade with the Pro model intact is a known residual risk. Regression tests in `tests/test_chip_read.py`.

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
- **Completion detection requires three signals**: text stable 5s + no Stop button + Copy button mounted on the latest assistant turn (`[data-testid="copy-turn-action-button"]` inside the `[data-testid^="conversation-turn"]` container). The Copy button is the affirmative gate — the turn-action toolbar only renders after the turn is finalized. The text-stable + no-Stop heuristic alone false-positives on Pro Extended runs: the "thinking summary" panel renders text that sits stable for tens of seconds while the model continues reasoning silently, with no Stop button visible (this caused run `reframe-review-040` on 2026-04-30 to return a 228-char summary fragment after only 234s on a 236KB-prompt task). If the Copy button selector ever changes, runs will hit `--generation-timeout` (default 60min) — fail closed; patch the selector when this happens, don't loosen the gate.
- **Anti-detection flags** (`--disable-blink-features=AutomationControlled`, dropping `--enable-automation` and `--no-sandbox`) are load-bearing. Removing them triggers ChatGPT's auth-error redirect.
- **Extraction prefers the Copy button** via `[data-testid="copy-turn-action-button"]` (clean markdown), then `pbpaste` reads the system clipboard; falls back to `innerText` if either step fails. Result captures `extraction: "copy_button"|"innertext"` so you can audit which path won. Math, code fences, and tables are mangled under `innerText` — the fallback is only for degraded environments.
- **Concurrency model: shared Chrome + multi-tab + three brief locks.** `~/.gpt-pro/browser.lock` no longer exists. The flow is:
  - `ParallelSlot(N)` (`~/.gpt-pro/slots/slot-*.lock`) — file-lock semaphore admitting at most `GPT_PRO_MAX_PARALLEL` (default 3) concurrent `_run` workers. Held for the entire run; the cap exists because parallel bursts on one ChatGPT Pro session are an account-side anti-abuse signal — raising it is a knob, not a default. Polls every 2s when full and emits one `slot_queued` JSONL on entry to a wait, then `slot_acquired` with `waited_secs`.
  - `LaunchLock` (`~/.gpt-pro/launch.lock`) — held only across the CDP probe + conditional `open -n -a` launch in `ensure_shared_chrome_running`. Re-probes inside the lock to absorb double-launch races. `_kill_chrome_orphans()` runs *only* on the launch path (when the probe failed) — never on the connect path — so it cannot terminate the shared Chrome out from under live tabs.
  - `UiClipboardLock` (`~/.gpt-pro/clipboard.lock`) — held across the foreground+focus+`pbcopy`+`Meta+V`+restore transaction in `_focus_and_paste`, and across the baseline-`pbpaste`+click-Copy+post-`pbpaste`+restore transaction in `_copy_button_extract`. Wider than just the pbcopy/pbpaste calls because `Meta+V` follows OS focus and ChatGPT's Copy-button onClick uses `navigator.clipboard.writeText` which needs document focus. Two parallel workers must NOT interleave these phases — they will silently swap each other's prompts/responses through the global macOS pasteboard. **Narrowing this lock to just `pbpaste` is wrong.**
- **Each worker creates its own tab via `ctx.new_page()`** in `_run_with_browser`. On exit it `await page.close()` — only its own tab. `await browser.close()` is explicitly NOT called: it's been observed to terminate the shared Chrome via CDP under some Playwright versions, which would kill every other worker's tab. The Playwright `async with async_playwright()` exit handles disconnect cleanly without killing the connect-over-cdp browser.
- **Chrome stays alive across worker lifecycles.** The mac mini's GUI session hosts Chrome indefinitely; workers connect, work, disconnect. Use `gpt-pro-relay close-chrome` to tear down (held under `LaunchLock` so a launching worker can't race the shutdown). No refcount, no auto-teardown — refcount-on-flock was rejected during design (cross-process state corrupts on SIGKILL).
- **Worker tabs always come from `ctx.new_page()`, never `ctx.pages[i]`.** A persistent `--user-data-dir` surfaces session-restored phantom tabs in `ctx.pages` (no compositor surface — `Page.captureScreenshot` hangs forever waiting for a paint frame, although DOM/CDP/input keep working). A freshly-created tab in an already-foreground Chrome is windowed by construction, so workers never need to probe via `Browser.getWindowForTarget`. **Don't try to close the phantoms** — `page.close()` on `chrome://omnibox-popup` hangs; leave them, they're harmless background orphans. Symptom of regression: every `screenshot_skipped` line in `worker.stderr` with `"waiting for fonts to load... fonts loaded"` followed by timeout, plus the user can't see the chatgpt tab. If the symptom returns, check whether anyone reintroduced a path that picks from `ctx.pages` instead of calling `ctx.new_page()`.

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

- Network-side completion signals (`async-status`, `implicit_message_feedback`, etc.) — the DOM-side Copy-button gate covers the same moment with a simpler implementation and no race against Playwright response-event timing. Wire one only if the Copy button selector itself becomes unstable.
- Auto-retry on errors — fail closed, surface the `run_dir`, let the user decide. Especially do NOT auto-retry on CDP disconnect (shared-Chrome crash) — re-submitting burns another 5–20 min of Pro reasoning, and a crash that recurs would multiply usage.
- `launchd` keepalive — `tmux` is fine.
- Sleep / clamshell handling beyond `caffeinate` — out of scope.
- Worker liveness check / orphan detection — if the worker dies before writing `result.json`, the parent waits until `--generation-timeout`. Acceptable.
- **Refcount-based Chrome teardown** — was considered (option C from the multi-tab design review) and rejected. Cross-process refcount on `flock` is fragile to SIGKILL'd workers and adds exactly the kind of "small database" the no-SQLite invariant warns against. Manual `gpt-pro-relay close-chrome` is the supported teardown path; Chrome lives indefinitely otherwise.
- **Per-tab `_kill_chrome_orphans()` on every run** — the old `BrowserLock` ran it on entry. The new flow only runs it on the launch path (when CDP probe fails inside `LaunchLock`), so it cannot terminate the shared Chrome out from under live tabs. Putting it back on the connect path defeats multi-tab.
- **Auto-rate-limit detection / backoff** — if `network.json` starts showing 429s, captcha redirects, or unexplained `needs_reauth` after parallel bursts, that's account-side anti-abuse and the response is to drop `GPT_PRO_MAX_PARALLEL` to 1 (effectively the old serialized behavior), not to add code.
