"""Regression tests for the model-selection chip read.

The contaminated run ask-20260531T065451Z read the composer chip as
"Extended Pro" ~400ms after login, then sent the prompt 2.6s later to a chip
that had re-resolved to "Thinking" (served gpt-5-5-thinking). The old
read_composer_chip_text returned the *first* non-placeholder value it saw,
catching the optimistic-hydration transient. These tests pin the stability
behavior that fixes it: the chip text must repeat for `stable_polls` reads
before it is trusted.
"""

import pytest

from gpt_pro import cli
from gpt_pro.cli import is_pro_extended_label, read_composer_chip_text


class _FakeChip:
    """Returns a scripted sequence of chip texts, cycling once exhausted.

    Cycling lets a short script model an indefinitely oscillating chip (for the
    timeout fail-closed test) while a script that ends on a repeated value still
    reaches a stable streak before it wraps.
    """

    def __init__(self, texts):
        self._texts = list(texts)
        self._i = 0

    async def wait_for(self, **_kwargs):
        return None

    async def inner_text(self):
        text = self._texts[self._i]
        self._i = (self._i + 1) % len(self._texts)
        return text


class _FakeLocator:
    def __init__(self, chip):
        self.first = chip


class _FakePage:
    def __init__(self, texts):
        self._chip = _FakeChip(texts)

    def locator(self, _selector):
        return _FakeLocator(self._chip)


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    """Make the 0.2s poll sleep instant so the tests run fast."""

    async def _instant(_delay):
        return None

    monkeypatch.setattr(cli.asyncio, "sleep", _instant)


async def test_optimistic_transient_is_rejected():
    # The exact failure shape: hydrates to "Extended Pro", then settles to
    # "Thinking". The stable read must return the SETTLED value, not the
    # transient — so the fast path fails and the slow path corrects it.
    page = _FakePage(["Model", "Extended Pro", "Thinking", "Thinking", "Thinking"])
    text = await read_composer_chip_text(page, timeout=30.0)
    assert text == "Thinking"
    assert not is_pro_extended_label(text)


async def test_steady_extended_pro_passes():
    page = _FakePage(["Model", "Extended Pro", "Extended Pro", "Extended Pro"])
    text = await read_composer_chip_text(page, timeout=30.0)
    assert text == "Extended Pro"
    assert is_pro_extended_label(text)


async def test_stable_polls_one_is_eager():
    # The slow-path post-click confirmation wants the old eager behavior:
    # return the first non-placeholder value (no hydration race after a
    # deliberate menu click).
    page = _FakePage(["Model", "Extended", "Extended"])
    text = await read_composer_chip_text(page, timeout=30.0, stable_polls=1)
    assert text == "Extended"


async def test_never_hydrates_returns_empty():
    # Chip stuck on the SSR placeholder -> times out, returns "" so the
    # caller's predicate fails closed.
    page = _FakePage(["Model"])
    text = await read_composer_chip_text(page, timeout=0.05)
    assert text == ""
    assert not is_pro_extended_label(text)


async def test_oscillating_chip_times_out_empty():
    # A chip that never holds one value for `stable_polls` consecutive reads
    # must NOT be accepted on the last lucky sample (the original timeout
    # fail-open). It oscillates Extended Pro <-> Thinking forever, so the read
    # times out and returns "" -> fails closed, even though a transient
    # "Extended Pro" was seen on every other poll.
    page = _FakePage(["Extended Pro", "Thinking"])
    text = await read_composer_chip_text(page, timeout=0.05, stable_polls=3)
    assert text == ""
    assert not is_pro_extended_label(text)


def test_pro_extended_label_passes_predicate():
    # The 2026-06 redesign flipped the selected-chip label from "Extended Pro"
    # to "Pro Extended" (the flat Intelligence list's tier name). Both contain
    # the "Pro" + "Extended" tokens, so the fail-closed predicate accepts it.
    assert is_pro_extended_label("Pro Extended")


def test_pro_extended_only_is_ambiguous_without_pro_token():
    # A bare "Extended" (e.g. a thinking-model effort tier) lacks "Pro" and must
    # still fail closed.
    assert not is_pro_extended_label("Extended")


async def test_steady_pro_extended_passes():
    page = _FakePage(["Model", "Pro Extended", "Pro Extended", "Pro Extended"])
    text = await read_composer_chip_text(page, timeout=1.0, stable_polls=3)
    assert text == "Pro Extended"
    assert is_pro_extended_label(text)
