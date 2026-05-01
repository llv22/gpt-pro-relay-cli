## Observations

- Request: diagnose intermittent macOS headed-Chrome white-screen runs where DOM/CDP/click/copy still work, but the OS window is blank and `page.screenshot()` times out waiting for a paint frame.
- Relevant repo invariants: real Chrome, headed mode, persistent profile, one shared `launch_kwargs()`, no relay daemon, worker survives SSH drops via detached `_run`.
- Recent changes already ruled out viewport/window-size fixes. Current code uses `--window-size=1280,800`, `no_viewport=True`, and post-navigation CDP viewport pinning.
- The failure crosses the boundary after renderer/DOM correctness: successful DOM and copy extraction prove the target is alive, while screenshot timeout points at the browser compositor / WindowServer surface path.

## Hypothesis

Chrome is launched by Playwright from an sshd-originated detached worker by direct executable spawn. That can produce a valid Chrome process and NSWindow without a reliable foreground LaunchServices/AppKit activation transaction in the logged-in Aqua session, leaving the macOS CoreAnimation-backed web-content compositor without a visible display cycle.

## Root Cause

The missing piece is not renderer viewport geometry; it is OS-visible app/window activation. The renderer can keep processing ChatGPT and CDP can keep clicking, but the macOS browser compositor surface can remain unpainted when the app/window has not been ordered active/visible in WindowServer before first navigation.

## Fix

After `launch_persistent_context()` returns and after selecting or creating the Playwright `page`, activate the existing Google Chrome app through LaunchServices and then call `page.bring_to_front()` before the first `page.goto()`. For `_run`, this must execute inside the detached worker, because that is the process launching and driving Chrome.

## Verification

Skipped local reproduction: this workspace cannot exercise the macOS WindowServer paint path. Monitor future `worker.stderr` for `chrome_activated` followed by absence of `screenshot_skipped`.
