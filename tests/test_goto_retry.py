"""Regression tests for the pre-send page.goto retry.

Runs ask-20260621T005030Z/005031Z/005207Z on 2026-06-21 all died with
`Page.goto: Timeout 30000ms exceeded` navigating to https://chatgpt.com/ during
a transient server/Cloudflare slow window — the implicit Playwright 30s default
clipped a slow-but-working load. Identical prompts navigated in ~7s minutes
later. `bf35b1f8` failed while running essentially alone, so the slow window was
server-side, not local contention (which rules out jitter / lower parallelism as
the fix). These tests pin `_goto_with_retry`: raise the per-attempt timeout, retry
once on TimeoutError only, fail closed when the budget is exhausted, and never
retry a non-timeout error (which would mask a genuinely wedged Chrome).
"""

import pytest
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from gpt_pro import cli
from gpt_pro.cli import _goto_with_retry


class _FakeGotoPage:
    """A fake page whose `goto` raises TimeoutError for the first `fail_count`
    calls, then succeeds. Records every call's kwargs for assertion."""

    def __init__(self, fail_count=0, raise_exc=None):
        self._fail_count = fail_count
        self._raise_exc = raise_exc
        self.calls = []

    async def goto(self, url, *, wait_until, timeout):
        self.calls.append({"url": url, "wait_until": wait_until, "timeout": timeout})
        if self._raise_exc is not None:
            raise self._raise_exc
        if len(self.calls) <= self._fail_count:
            raise PlaywrightTimeoutError(f"Timeout {timeout}ms exceeded.")


@pytest.fixture
def stages(monkeypatch):
    """Capture log_stage(stage, **kw) calls as (stage, kw) tuples."""
    captured = []
    monkeypatch.setattr(cli, "log_stage", lambda stage, **kw: captured.append((stage, kw)))
    return captured


async def test_first_attempt_success_no_retry(stages):
    page = _FakeGotoPage(fail_count=0)
    await _goto_with_retry(page, "https://chatgpt.com/", timeout_ms=90_000, retries=1)
    assert len(page.calls) == 1
    assert page.calls[0]["timeout"] == 90_000
    assert page.calls[0]["wait_until"] == "domcontentloaded"
    assert [s for s, _ in stages if s == "goto_retry"] == []


async def test_retries_once_then_succeeds(stages):
    page = _FakeGotoPage(fail_count=1)
    await _goto_with_retry(page, "https://chatgpt.com/", timeout_ms=90_000, retries=1)
    assert len(page.calls) == 2  # first times out, second succeeds
    assert all(c["timeout"] == 90_000 for c in page.calls)
    retries = [kw for s, kw in stages if s == "goto_retry"]
    assert len(retries) == 1
    assert retries[0]["attempt"] == 1
    assert retries[0]["timeout_ms"] == 90_000


async def test_exhausts_retries_then_fails_closed(stages):
    page = _FakeGotoPage(fail_count=99)  # every attempt times out
    with pytest.raises(PlaywrightTimeoutError):
        await _goto_with_retry(page, "https://chatgpt.com/", timeout_ms=90_000, retries=1)
    assert len(page.calls) == 2  # initial + one retry, then re-raise
    assert len([s for s, _ in stages if s == "goto_retry"]) == 1


async def test_retries_zero_raises_on_first_timeout(stages):
    page = _FakeGotoPage(fail_count=99)
    with pytest.raises(PlaywrightTimeoutError):
        await _goto_with_retry(page, "https://chatgpt.com/", timeout_ms=90_000, retries=0)
    assert len(page.calls) == 1
    assert [s for s, _ in stages if s == "goto_retry"] == []


async def test_non_timeout_error_is_not_retried(stages):
    # A non-timeout navigation error (e.g. CDP disconnect) must surface
    # immediately — retrying it would mask a genuinely wedged Chrome.
    page = _FakeGotoPage(raise_exc=RuntimeError("Target closed"))
    with pytest.raises(RuntimeError):
        await _goto_with_retry(page, "https://chatgpt.com/", timeout_ms=90_000, retries=1)
    assert len(page.calls) == 1
    assert [s for s, _ in stages if s == "goto_retry"] == []
