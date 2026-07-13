# Setup

Full setup guide for `gpt-pro-relay` on both macOS and headless Linux. For what
the tool does and how to invoke it, see the [README](../README.md).

Requires Python 3.11+, [uv](https://docs.astral.sh/uv/), and a ChatGPT Pro (or
Enterprise) account.

```bash
uv venv && uv sync   # create the venv and install gpt-pro-relay
uv run pytest -q     # optional: 26 tests should pass
```

---

## macOS (original path)

Playwright drives **real Google Chrome** (not bundled Chromium — auth/anti-abuse
behave differently) and needs a logged-in GUI session.

- Keep the Mac signed into its GUI session; use `caffeinate` if it sleeps.
- Install real Google Chrome.

```bash
uv run gpt-pro-relay login    # opens Chrome at chatgpt.com; sign in manually
```

Login uses a dedicated profile at `~/.gpt-pro-profile/` (cookies persist there).
Once signed in, manually select your Pro **model** + **Pro** effort in the
composer so the account preference is set, then confirm:

```bash
uv run gpt-pro-relay doctor
```

---

## Headless Linux (this fork)

No display and no interactive login. Chrome and the login are supplied
out-of-band. Run as a **non-root** user where possible.

### 1. Chrome binary

Use real Google Chrome, or a rootless **Chrome for Testing** extraction (no root,
no `apt`). To fetch Chrome for Testing:

```bash
# find the current stable linux64 URL, then extract it anywhere:
curl -s "https://googlechromelabs.github.io/chrome-for-testing/last-known-good-versions-with-downloads.json" \
  | python3 -c "import json,sys;d=json.load(sys.stdin)['channels']['Stable'];print([x['url'] for x in d['downloads']['chrome'] if x['platform']=='linux64'][0])"
# → download that chrome-linux64.zip, unzip it, and point the tool at chrome-linux64/chrome
```

Point the tool at it:

```bash
export GPT_PRO_CHROME_BINARY=/path/to/chrome-linux64/chrome
```

If unset, the tool probes `/usr/bin/google-chrome`, `/usr/bin/google-chrome-stable`,
`/opt/google/chrome/chrome`, and `~/.cache/chrome-for-testing/chrome-linux64/chrome`.

Chrome needs its shared libraries present (`ldd chrome | grep 'not found'` should
be empty). Playwright's `uv run playwright install-deps chrome` installs them if
you have root; otherwise most desktop/server images already have them.

### 2. Login by cookie injection

Interactive login can't run headless, so seed the profile from an exported cookie
set:

1. In a browser already logged into ChatGPT, export the `chatgpt.com` cookies —
   e.g. the **Cookie-Editor** extension → *Export → JSON*. The export **must**
   include `__Secure-next-auth.session-token` (chunked `.0`/`.1` is fine);
   including `cf_clearance`/`__cf_bm` helps but isn't required.
2. Save the JSON to a file on the box (keep it private):
   ```bash
   umask 077 && cat > ~/.gpt-pro-cookies.json   # paste JSON, then Ctrl-D
   ```
3. Inject it into the profile:
   ```bash
   GPT_PRO_CHROME_BINARY=/path/to/chrome \
   GPT_PRO_COOKIE_FILE=~/.gpt-pro-cookies.json \
   uv run python spike/seed_profile.py
   ```
   It reports `logged_in: true` on success. The cookie file is a **live bearer
   credential** — shred it once seeded (`shred -u ~/.gpt-pro-cookies.json`); the
   profile keeps its own copy.

The cookie session is bound to its origin browser's IP/fingerprint, so a
cross-machine seed is genuinely what the auth gate tests — if `doctor` shows a
Cloudflare/auth bounce, re-export a fresh set.

### 3. Model + Pro effort

The tool is fail-closed on model (allowlist default `{gpt-5-6-pro}`) and requires
**Pro** effort.

- If your account has **GPT-5.6 Sol**: `spike/set_sol_model.py` selects it, and
  `spike/set_pro_effort.py` sets Pro effort. The send path also self-corrects
  effort each run.
- If your account **lacks Sol** (e.g. some Enterprise workspaces top out at
  GPT-5.5): allow its Pro slug instead of weakening the default —
  `export GPT_PRO_MODEL_SLUGS=gpt-5-5-pro`.

### 4. Verify

```bash
GPT_PRO_CHROME_BINARY=/path/to/chrome \
GPT_PRO_MODEL_SLUGS=gpt-5-5-pro \
uv run gpt-pro-relay doctor
```

`doctor` connects, reads the served model, and exits 0 when the profile is logged
in and the model matches.

### Why the headless flags

- **New headless (`--headless=new`) + a realistic UA** — old headless advertises
  `HeadlessChrome`, which Cloudflare's "Just a moment…" managed challenge walls
  before ChatGPT loads.
- **`--no-sandbox`** — many distros (Ubuntu 23.10+) disable unprivileged user
  namespaces via AppArmor, so Chrome's zygote sandbox aborts. Playwright passes
  `--no-sandbox` by default for the same reason. Set `GPT_PRO_NO_SANDBOX=0` where
  the sandbox works.
- **`--disable-dev-shm-usage`** — a small `/dev/shm` crashes the renderer on large
  pages.

---

## Running queries

Once `doctor` is green, relay prompts with `ask`. On a headless box, export the
env once (`GPT_PRO_CHROME_BINARY`, and `GPT_PRO_MODEL_SLUGS` if your account lacks
Sol) so you don't repeat it.

### Normal query (plain Pro)

A standard Pro-effort query. Reasons from the model's own knowledge (no web
browsing); typically returns in seconds to a few minutes.

```bash
echo "Explain the tradeoffs between DPO and GRPO for LLM alignment." \
  | uv run gpt-pro-relay ask --run-id my-q1
```

The extracted answer prints to stdout (clean markdown via the Copy button). Add
`--output answer.md` to write it to a file instead.

### Deep research

Enables ChatGPT's **Deep Research** tool: it browses the web and produces a long,
cited report. Takes **5–30 minutes**, so run it detached and poll, or give a
generous timeout.

```bash
echo "Survey trajectory-level credit assignment in multi-turn LLM agent RL (2024–2026), with citations." \
  | uv run gpt-pro-relay ask --run-id my-dr1 --tool deep-research --generation-timeout 1800
```

Tips:
- Add *"Begin immediately; do not ask clarifying questions"* to the prompt so it
  starts researching without a back-and-forth.
- The report renders inside a sandboxed iframe, so the tool extracts it via the
  report's *Export → Markdown* download (this path is inherently more fragile than
  a normal query). `result.json` reports `extraction: "deep_research_export_md"`.
- Deep Research runs its own model, so the served-slug audit is fail-open
  (`model_audit: "tool_mode_unaudited"`) — that's expected, not an error.

### Other composer tools

Same `--tool` flag, repeatable — e.g. `--tool web-search` (fast, cited),
`--tool create-image`, `--tool company-knowledge`, `--tool google-drive`. See the
[README](../README.md#composer-tools---tool). An unsupported name fails
immediately; a tool your account hasn't provisioned fails with `tool_unavailable`.

### Where results go / archiving

Run artifacts land in `./runs/<run_id>/` (current folder by default). To keep a
tidy record, file the prompt and answer under `query/` per the
[archive convention](../query/README.md):

```
query/Q<N>-<slug>.md                    # the prompt (tracked in git)
query/R<N>-<slug>-pro.md                # normal-query result   (git-ignored)
query/R<N>-<slug>-deep-research.md      # deep-research result  (git-ignored)
```

---

## Environment variables

See the [README configuration table](../README.md#configuration-environment) for
the full list. The ones you'll typically export on a headless box:

```bash
export GPT_PRO_CHROME_BINARY=/path/to/chrome
export GPT_PRO_MODEL_SLUGS=gpt-5-5-pro          # only if your account lacks Sol
# runs/ default to ./runs in the current folder; override with GPT_PRO_RUNS_DIR
```

---

## Spike scripts

The `spike/` directory holds the throwaway headless-setup helpers:

| Script | Purpose |
|---|---|
| `seed_profile.py` | Inject an exported cookie set into the profile (headless login) |
| `set_pro_effort.py` | Set the composer to **Pro** effort (persistent profile preference) |
| `set_sol_model.py` | Select **GPT-5.6 Sol** via the model submenu |
| `01_auth_probe.py` / `02_paste_probe.py` / `03_send_roundtrip.py` | Feasibility gates (auth, large-paste, full round-trip) |

Set `GPT_PRO_SPIKE_EXECUTABLE=/path/to/chrome` when running spike scripts.
