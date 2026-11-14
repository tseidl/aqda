"""Locate a quoted passage in a document's text.

A model (or any program) sends back verbatim quotes; AQDA places them itself, so a coding
is always an exact span of the document. The search is deliberately narrow: a place matches
when it equals the quote, or equals it after ignoring only differences a copy-and-paste or
a model's typography introduces: whitespace runs (including line breaks and non-breaking
spaces), straight and curly quotation marks and apostrophes, hyphen and dash variants, soft
hyphens and other invisible characters, and Unicode normalisation (composed or decomposed
accents, common ligatures). An exact match is one such place and is not preferred over the
others; the method reported is ``exact`` when the placed slice equals the quote.

There is no case folding, no edit-distance matching, and no ellipsis expansion. When more
than one place matches, the optional text before and after the quote decides; if it does
not single out one place, the quote is refused. Every doubtful case is refused, never
placed approximately.

Offsets are Unicode code points (Python string indices), like every coding in AQDA.
"""

import unicodedata
from dataclasses import dataclass, replace

# Quotation marks and apostrophes that count as the same character in a tolerant match.
_SINGLE_QUOTES = "'‘’‚‛′`´‹›"
_DOUBLE_QUOTES = '"“”„‟″«»'
# Hyphens, dashes, and the minus sign.
_DASHES = "-‐‑‒–—―−⁃﹘﹣－"
# Characters that are invisible in reading and dropped: soft hyphen, zero-width space,
# zero-width no-break space (byte order mark), word joiner.
_INVISIBLE = "­​﻿⁠"
# Typographic ligatures written out as their letters.
_LIGATURES = {
    "ﬀ": "ff",
    "ﬁ": "fi",
    "ﬂ": "fl",
    "ﬃ": "ffi",
    "ﬄ": "ffl",
    "ﬅ": "st",
    "ﬆ": "st",
}
_ZERO_WIDTH_JOINER = "‍"
_ELLIPSES = ("...", "…", "[…]", "(…)")

_FOLD = {
    **{ch: "'" for ch in _SINGLE_QUOTES},
    **{ch: '"' for ch in _DOUBLE_QUOTES},
    **{ch: "-" for ch in _DASHES},
}

EXACT = "exact"
TOLERANT = "tolerant"

NOT_FOUND = "The quote was not found in the document."
NOT_FOUND_ELLIPSIS = (
    "The quote leaves words out (…) and was not found; only verbatim quotes can be placed."
)
EMPTY_QUOTE = "The quote is empty."
AMBIGUOUS = "The quote occurs {count} times in the document and the context does not say which."
CONTEXT_MISMATCH = (
    "The quote occurs {count} times in the document and the context given matches none of them."
)
CUTS_CHARACTER = "The quote would cut through a character of the document."


@dataclass(frozen=True)
class Anchor:
    """Where a quote was placed, or why it was refused (then ``start``/``end`` are None)."""

    start: int | None = None
    end: int | None = None
    method: str | None = None
    reason: str | None = None

    @property
    def found(self) -> bool:
        return self.reason is None


# Tell whether a character continues the one before it (accents, joiners, skin tones, and
# the vowel and final consonant jamo of a decomposed Hangul syllable).
def _is_continuation(ch: str) -> bool:
    if ch == _ZERO_WIDTH_JOINER or "\U0001f3fb" <= ch <= "\U0001f3ff":
        return True
    if "ᅠ" <= ch <= "ᇿ" or "ힰ" <= ch <= "퟿":
        return True
    return unicodedata.category(ch).startswith("M")


# Tell whether a span [start, end) of a text begins and ends between whole characters.
def cuts_character(text: str, start: int, end: int) -> bool:
    if start < len(text) and start > 0:
        if _is_continuation(text[start]) or text[start - 1] == _ZERO_WIDTH_JOINER:
            return True
    if 0 < end < len(text):
        if _is_continuation(text[end]) or text[end - 1] == _ZERO_WIDTH_JOINER:
            return True
    return False


# The tolerant form of one character (empty when it is dropped).
def _fold_char(ch: str) -> str:
    if ch in _INVISIBLE:
        return ""
    if ch in _LIGATURES:
        return _LIGATURES[ch]
    decomposed = unicodedata.normalize("NFD", ch)
    return "".join(_FOLD.get(part, part) for part in decomposed)


def _normalise(text: str) -> tuple[str, list[int], list[int], list[int]]:
    """Return the tolerant form of ``text`` with maps back to the original.

    For each character of the tolerant form, ``starts[i]`` and ``ends[i]`` give the span
    of original characters it came from (a collapsed whitespace run maps to the whole
    run). ``positions[j]`` is the tolerant-form index at which original character ``j``
    begins (``positions[len(text)]`` is the length of the tolerant form).
    """
    out: list[str] = []
    starts: list[int] = []
    ends: list[int] = []
    positions: list[int] = []
    in_space = False
    for index, ch in enumerate(text):
        positions.append(len(out))
        if ch.isspace():
            if in_space:
                ends[-1] = index + 1
            else:
                out.append(" ")
                starts.append(index)
                ends.append(index + 1)
                in_space = True
            continue
        folded = _fold_char(ch)
        if not folded:
            # An invisible character inside a whitespace run belongs to the run.
            if in_space:
                ends[-1] = index + 1
            continue
        in_space = False
        for part in folded:
            out.append(part)
            starts.append(index)
            ends.append(index + 1)
    positions.append(len(out))
    _reorder_marks(out, starts, ends)
    return "".join(out), starts, ends, positions


def _reorder_marks(out: list[str], starts: list[int], ends: list[int]) -> None:
    """Put runs of combining marks into canonical order (completes NFD), keeping the maps."""
    index = 0
    while index < len(out):
        if unicodedata.combining(out[index]) == 0:
            index += 1
            continue
        end = index
        while end < len(out) and unicodedata.combining(out[end]) != 0:
            end += 1
        if end - index > 1:
            run = sorted(
                zip(out[index:end], starts[index:end], ends[index:end]),
                key=lambda item: unicodedata.combining(item[0]),
            )
            out[index:end] = [item[0] for item in run]
            starts[index:end] = [item[1] for item in run]
            ends[index:end] = [item[2] for item in run]
        index = end


# The tolerant form of a short piece of text (a quote or its context), trimmed.
def tolerant_form(text: str) -> str:
    return _normalise(text)[0].strip(" ")


# Tell whether two pieces of text are the same under the tolerant comparison.
def same_text(first: str, second: str) -> bool:
    return tolerant_form(first) == tolerant_form(second)


# Every start index of ``needle`` in ``haystack``, overlapping occurrences included.
def _occurrences(haystack: str, needle: str) -> list[int]:
    found = []
    position = haystack.find(needle)
    while position >= 0:
        found.append(position)
        position = haystack.find(needle, position + 1)
    return found


class AnchorText:
    """A document text prepared for placing many quotes (the tolerant form is built once)."""

    def __init__(self, text: str):
        self.text = text
        self._normalised: tuple[str, list[int], list[int], list[int]] | None = None

    # Build the tolerant form on first use.
    def _tolerant(self) -> tuple[str, list[int], list[int], list[int]]:
        if self._normalised is None:
            self._normalised = _normalise(self.text)
        return self._normalised

    # Place one quote, using ``before``/``after`` context only to choose among matches.
    # Every place that matches tolerantly counts (an exact match is one of them), so an
    # exact match never wins over a near-identical place without context deciding.
    def locate(self, quote: str, before: str | None = None, after: str | None = None) -> Anchor:
        if not isinstance(quote, str) or not tolerant_form(quote):
            return Anchor(reason=EMPTY_QUOTE)
        spans = self._tolerant_matches(tolerant_form(quote))
        if spans:
            anchor = self._choose(spans, before, after)
            if anchor.found:
                exact = self.text[anchor.start:anchor.end] == quote.strip()
                return replace(anchor, method=EXACT if exact else TOLERANT)
            return anchor
        if any(mark in quote for mark in _ELLIPSES):
            return Anchor(reason=NOT_FOUND_ELLIPSIS)
        return Anchor(reason=NOT_FOUND)

    # Matches of an already folded quote, as trimmed spans of the original text. A span
    # runs from the first to the last original character any folded character of the match
    # came from (canonical reordering of marks can move them), and it is kept only if its
    # own tolerant form is the quote's, so it never misses or adds a visible character.
    def _tolerant_matches(self, folded_quote: str) -> list[tuple[int, int]]:
        folded, starts, ends, _ = self._tolerant()
        spans = []
        for begin in _occurrences(folded, folded_quote):
            finish = begin + len(folded_quote)
            # A match must not split what one original character became (a ligature or a
            # decomposed accent), nor stop before a combining mark.
            if begin > 0 and starts[begin - 1] == starts[begin]:
                continue
            if finish < len(folded) and starts[finish] == starts[finish - 1]:
                continue
            if finish < len(folded) and _is_continuation(folded[finish]):
                continue
            if _is_continuation(folded[begin]):
                continue
            start, end = min(starts[begin:finish]), max(ends[begin:finish])
            while start < end and self.text[start].isspace():
                start += 1
            while end > start and self.text[end - 1].isspace():
                end -= 1
            if start >= end or cuts_character(self.text, start, end):
                continue
            if tolerant_form(self.text[start:end]) != folded_quote:
                continue
            if (start, end) not in spans:
                spans.append((start, end))
        return spans

    # Pick the single match, or the single one whose surroundings fit the context.
    def _choose(
        self, spans: list[tuple[int, int]], before: str | None, after: str | None
    ) -> Anchor:
        if len(spans) == 1:
            start, end = spans[0]
            return Anchor(start=start, end=end)
        before_form = tolerant_form(before) if isinstance(before, str) else ""
        after_form = tolerant_form(after) if isinstance(after, str) else ""
        if not before_form and not after_form:
            return Anchor(reason=AMBIGUOUS.format(count=len(spans)))
        fitting = [
            span for span in spans
            if self._context_fits(span, before_form, after_form)
        ]
        if len(fitting) == 1:
            start, end = fitting[0]
            return Anchor(start=start, end=end)
        if not fitting:
            return Anchor(reason=CONTEXT_MISMATCH.format(count=len(spans)))
        return Anchor(reason=AMBIGUOUS.format(count=len(spans)))

    # Tell whether the text right before and after a span matches the given context.
    def _context_fits(self, span: tuple[int, int], before_form: str, after_form: str) -> bool:
        folded, _, _, positions = self._tolerant()
        start, end = span
        if before_form:
            stop = positions[start]
            while stop > 0 and folded[stop - 1] == " ":
                stop -= 1
            if stop < len(before_form) or folded[stop - len(before_form):stop] != before_form:
                return False
        if after_form:
            begin = positions[end]
            while begin < len(folded) and folded[begin] == " ":
                begin += 1
            if folded[begin:begin + len(after_form)] != after_form:
                return False
        return True


# Place one quote in a text; see ``AnchorText.locate``.
def locate_quote(
    text: str, quote: str, before: str | None = None, after: str | None = None
) -> Anchor:
    return AnchorText(text).locate(quote, before, after)
