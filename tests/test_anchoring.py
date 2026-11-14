import unicodedata

import pytest

from aqda.services.anchoring import (
    EXACT,
    TOLERANT,
    AnchorText,
    cuts_character,
    locate_quote,
    same_text,
)


# Place a quote and return the exact original slice (or the refusal reason).
def place(text, quote, before=None, after=None):
    anchor = locate_quote(text, quote, before, after)
    if not anchor.found:
        return None, anchor.reason
    return text[anchor.start:anchor.end], anchor.method


def test_exact_match_returns_code_point_offsets():
    text = "Alice says hello. Bob says no."
    anchor = locate_quote(text, "Bob says no.")
    assert (anchor.start, anchor.end, anchor.method) == (18, 30, EXACT)


def test_offsets_are_code_points_after_astral_characters():
    text = "😀😀 We agree 🇩🇪 on 𝔘𝔫𝔦𝔠𝔬𝔡𝔢 here."
    anchor = locate_quote(text, "on 𝔘𝔫𝔦𝔠𝔬𝔡𝔢")
    assert anchor.method == EXACT
    assert text[anchor.start:anchor.end] == "on 𝔘𝔫𝔦𝔠𝔬𝔡𝔢"
    assert anchor.start == text.index("on")


def test_surrounding_whitespace_of_the_quote_is_trimmed():
    assert place("a b  hello world  c", "  hello world \n") == ("hello world", EXACT)


@pytest.mark.parametrize(
    ("text", "quote", "expected"),
    [
        # Curly quotation marks and apostrophes in the text, straight ones in the quote.
        ("He said “we don’t know” today.", '"we don\'t know"', "“we don’t know”"),
        # And the other way round.
        ('He said "we don\'t know" today.', "“we don’t know”", '"we don\'t know"'),
        # German and French quotation marks.
        ("Sie sagte „nein“ dazu.", '"nein"', "„nein“"),
        ("Il a dit « non » alors.", '" non "', "« non »"),
        # Dashes and the minus sign.
        ("prices rose – sharply — in 2020−2021", "rose - sharply - in 2020-2021",
         "rose – sharply — in 2020−2021"),
        # Line-wrapped text and runs of spaces.
        ("the industrial\n   policy of\r\nthe state", "industrial policy of the state",
         "industrial\n   policy of\r\nthe state"),
        # Non-breaking and narrow spaces.
        ("12 000 jobs were lost", "12 000 jobs were lost", "12 000 jobs were lost"),
        # A soft hyphen inside a word.
        ("an inter­national deal", "international deal", "inter­national deal"),
        # A zero-width space.
        ("state​aid rules", "stateaid rules", "state​aid rules"),
        # Ligatures written as letters.
        ("the ﬁnal oﬃce", "the final office", "the ﬁnal oﬃce"),
    ],
)
def test_tolerant_match_returns_the_original_slice(text, quote, expected):
    assert place(text, quote) == (expected, TOLERANT)


def test_decomposed_text_matches_a_composed_quote_and_back():
    composed = "Die Bürger und die Regierung"
    decomposed = unicodedata.normalize("NFD", composed)
    assert len(decomposed) > len(composed)

    slice_, method = place(decomposed, "Bürger und")
    assert method == TOLERANT
    assert slice_ == unicodedata.normalize("NFD", "Bürger und")

    slice_, method = place(composed, unicodedata.normalize("NFD", "Bürger und"))
    assert (slice_, method) == ("Bürger und", TOLERANT)


def test_combining_marks_in_another_order_still_match():
    # q + dot below + dot above, written in two orders that are canonically equal.
    text = "x q̣̇ y"
    assert place(text, "q̣̇")[0] == "q̣̇"


def test_no_case_folding():
    assert place("The State decides.", "the state decides.") == (
        None, "The quote was not found in the document."
    )


def test_no_fuzzy_matching():
    assert place("The state decides.", "The state decided.")[0] is None


def test_quote_absent_is_refused():
    slice_, reason = place("Alice says hello.", "Carol says goodbye.")
    assert slice_ is None
    assert "not found" in reason


@pytest.mark.parametrize("quote", ["Alice ... no.", "Alice … no.", "Alice […] no."])
def test_quote_with_ellipsis_is_refused(quote):
    slice_, reason = place("Alice says hello. Bob says no.", quote)
    assert slice_ is None
    assert "leaves words out" in reason


def test_ellipsis_that_is_really_in_the_text_matches():
    assert place("Well... we will see.", "Well... we will") == ("Well... we will", EXACT)


def test_repeated_phrase_without_context_is_ambiguous():
    slice_, reason = place("We agree. They disagree. We agree.", "We agree.")
    assert slice_ is None
    assert "2 times" in reason


def test_repeated_phrase_is_resolved_by_context_before():
    text = "Alice: we agree. Bob: we agree. Carol: we agree."
    anchor = locate_quote(text, "we agree.", before="Bob:")
    assert anchor.found
    assert anchor.start == text.index("Bob:") + len("Bob: ")


def test_repeated_phrase_is_resolved_by_context_after():
    text = "Yes, and more. Yes, and less."
    anchor = locate_quote(text, "Yes, and", after="less")
    assert (anchor.start, anchor.end) == (15, 23)


def test_context_is_matched_with_the_same_tolerance():
    text = "Alice said\n “yes”. Bob said “yes”."
    anchor = locate_quote(text, '"yes"', before="Alice  said")
    assert anchor.found and anchor.method == TOLERANT
    assert text[anchor.start:anchor.end] == "“yes”"
    assert anchor.start == text.index("“yes”")


def test_context_must_be_adjacent():
    # "Bob:" occurs before the third match, but not directly before it.
    text = "Bob: we agree. Carol: we agree. Dan: we agree."
    anchor = locate_quote(text, "we agree.", before="Bob: we agree. Carol:")
    assert anchor.found and anchor.start == text.index("Carol:") + len("Carol: ")
    refused = locate_quote(text, "we agree.", before="Eve:")
    assert not refused.found and "matches none" in refused.reason


def test_context_that_fits_several_places_is_ambiguous():
    text = "A: yes. A: yes. B: yes."
    anchor = locate_quote(text, "yes.", before="A:")
    assert not anchor.found
    assert "3 times" in anchor.reason


def test_unique_match_ignores_context():
    # Context only chooses among several places; a single place is accepted whatever
    # context was supplied, also when that place matches only tolerantly.
    assert locate_quote("one two three", "two", before="nothing like it").found
    anchor = locate_quote("one “two” three", '"two"', after="nothing like it")
    assert anchor.found and anchor.method == TOLERANT


def test_overlapping_occurrences_count_as_ambiguous():
    assert not locate_quote("aaa", "aa").found


def test_empty_quote_is_refused():
    assert locate_quote("text", "   ").reason == "The quote is empty."
    assert locate_quote("text", "­").reason == "The quote is empty."
    assert not locate_quote("text", None).found


def test_a_match_never_cuts_an_accent_off():
    decomposed = unicodedata.normalize("NFD", "the café is open")
    # "cafe" is in the decomposed text exactly, but the accent belongs to the "e".
    assert not locate_quote(decomposed, "the cafe").found
    assert not locate_quote("the café is open", "the cafe").found


def test_a_match_never_cuts_a_ligature():
    assert not locate_quote("the ﬁnal word", "inal word").found


def test_a_match_never_cuts_an_emoji_sequence():
    text = "thumbs 👍🏽 and 👍 here"
    anchor = locate_quote(text, "👍")
    assert anchor.found and anchor.start == text.index("👍 here")
    family = "a 👩‍👩‍👧 b"
    assert not locate_quote(family, "👩").found


def test_span_is_trimmed_in_the_original_text():
    text = "start   hello \n end"
    anchor = locate_quote(text, " hello ")
    assert text[anchor.start:anchor.end] == "hello"


def test_exact_and_tolerant_places_count_alike():
    # An exact match does not win over a place that matches only tolerantly: both are
    # places, and the context has to decide between them.
    text = "Say \"yes\" here and “yes” there."
    refused = locate_quote(text, '"yes"')
    assert not refused.found and "2 times" in refused.reason
    anchor = locate_quote(text, '"yes"', before="Say")
    assert (anchor.method, text[anchor.start:anchor.end]) == (EXACT, '"yes"')
    anchor = locate_quote(text, '"yes"', after="there.")
    assert (anchor.method, text[anchor.start:anchor.end]) == (TOLERANT, "“yes”")


def test_context_selects_a_tolerant_place_over_an_exact_one():
    text = "First: re-elect now. Later: re–elect now."
    anchor = locate_quote(text, "re-elect now", before="Later:")
    assert anchor.found and anchor.method == TOLERANT
    assert anchor.start == text.index("re–elect")
    assert not locate_quote(text, "re-elect now").found


def test_tolerant_span_covers_every_reordered_mark():
    # e, combining acute, soft hyphen, combining dot below: canonical order puts the dot
    # below first, so the last folded character is not the last original one.
    text = "x é­̣ y"
    anchor = locate_quote(text, "ẹ́")
    assert anchor.found and anchor.method == TOLERANT
    assert text[anchor.start:anchor.end] == "é­̣"


def test_a_match_never_begins_inside_a_decomposed_hangul_syllable():
    decomposed = unicodedata.normalize("NFD", "간 가 각")
    assert len(decomposed) > len("간 가 각")
    # "가" is in the decomposed text three times, but only once as a whole syllable.
    anchor = locate_quote(decomposed, unicodedata.normalize("NFD", "가"))
    assert anchor.found
    assert decomposed[anchor.start:anchor.end] == unicodedata.normalize("NFD", "가")
    assert anchor.start == decomposed.index(unicodedata.normalize("NFD", " 가 ")) + 1
    # A quote that starts at the vowel of a syllable is refused.
    assert not locate_quote(decomposed, decomposed[1:3]).found
    assert cuts_character(decomposed, 1, len(decomposed))
    assert cuts_character(decomposed, 0, 2)


def test_prepared_text_places_many_quotes():
    prepared = AnchorText("First – one. Second – two.")
    assert prepared.locate("First - one.").method == TOLERANT
    assert prepared.locate("Second - two.").method == TOLERANT
    assert prepared.locate("Second – two.").method == EXACT


def test_same_text_and_cuts_character_helpers():
    assert same_text("“we don’t”\n", '"we  don\'t"')
    assert not same_text("We", "we")
    assert cuts_character("cafe\u0301", 0, 4)
    assert not cuts_character("cafe\u0301", 0, 5)
    assert not cuts_character("abc", 0, 3)


# Every accepted span must read as the quote under the tolerant comparison and begin and
# end between whole characters, whatever the text looks like. Texts are built from clusters
# (a base character followed by combining marks, invisible characters, or spaces) so that
# marks in non-canonical order across invisible characters occur often.
def test_accepted_spans_always_read_as_the_quote():
    import random

    bases = [
        "a", "e", "\u00e9", "\u1eb9", " ", "\n", "-", "\u2013", "'", "\u2019", '"',
        "\u201c", "\ufb01", "\U0001f600", "\U0001d518", "\u200d", "\u1100",
    ]
    marks = [
        "\u0301", "\u0323", "\u0308",  # combining acute, dot below, diaeresis
        "\u00ad", "\u200b", "\ufeff",  # soft hyphen, zero-width space, byte order mark
        "\U0001f3fd", "\u1161", "\u11a8", " ",  # skin tone, Hangul vowel and final
    ]
    invisible = "\u00ad\u200b\ufeff"
    rng = random.Random(7)

    def cluster():
        return rng.choice(bases) + "".join(
            rng.choice(marks) for _ in range(rng.choice([0, 1, 2, 3]))
        )

    accepted = 0
    for _ in range(4000):
        text = "".join(cluster() for _ in range(rng.randint(1, 6)))
        if rng.random() < 0.7:
            start = rng.randint(0, len(text) - 1)
            quote = text[start:rng.randint(start + 1, len(text))]
            if rng.random() < 0.5:
                quote = "".join(ch for ch in quote if ch not in invisible)
            if rng.random() < 0.7:
                quote = unicodedata.normalize(rng.choice(["NFC", "NFD"]), quote)
        else:
            quote = cluster() + cluster()
        before = rng.choice([None, text[:rng.randint(0, len(text))]])
        anchor = locate_quote(text, quote, before=before)
        if not anchor.found:
            continue
        accepted += 1
        slice_ = text[anchor.start:anchor.end]
        assert same_text(slice_, quote), (text, quote, slice_)
        assert not cuts_character(text, anchor.start, anchor.end), (text, quote, slice_)
        assert anchor.method == (EXACT if slice_ == quote.strip() else TOLERANT)
    assert accepted > 500
