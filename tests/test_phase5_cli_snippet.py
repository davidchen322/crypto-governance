"""The truncation marker must mean truncation happened."""

from __future__ import annotations

from ai_agent import cli


def test_multiline_text_that_fits_is_not_marked_truncated():
    """The bug: comparing `len(" ".join(words))` against summed line lengths loses one space
    per line break, so every multi-line text was marked truncated. Harmless in a search
    listing, actively misleading on an answer — it implies the analyst said more."""
    text = " ".join(["word"] * 30)
    lines = cli._snippet(text, width=40, lines=10)
    assert " ".join(lines).split() == text.split(), "content changed"
    assert not lines[-1].endswith("..."), f"spurious truncation marker: {lines[-1]!r}"


def test_text_that_overflows_is_marked_truncated():
    lines = cli._snippet(" ".join(["word"] * 500), width=40, lines=3)
    assert len(lines) == 3
    assert lines[-1].endswith("...")


def test_single_line_is_untouched():
    assert cli._snippet("a short chunk", width=80, lines=3) == ["a short chunk"]
