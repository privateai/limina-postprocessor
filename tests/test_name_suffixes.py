#!/usr/bin/env python3
"""Tests for trailing generational suffixes on name entities.

DEID puts the suffix inside the name entity rather than beside it — a live container
returns "James Wilson Sr" as one NAME_MEDICAL_PROFESSIONAL — so a replacement drawn
from the dictionary drops a token the text started with unless the suffix is split off
first and reattached. These cover _split_suffix directly plus the behaviour through
get_replacement, since the ordering against _split_title is easy to get wrong.

Requires the real 3.8GB dictionary, so these are skipped when it is absent.
"""
import pytest

from limina_postprocessor import DEFAULT_DICTIONARY
from limina_postprocessor.handlers.name_handler import NameHandler

pytestmark = pytest.mark.skipif(
    not __import__('pathlib').Path(DEFAULT_DICTIONARY).exists(),
    reason="name dictionary not downloaded (see README: Git LFS)",
)


@pytest.fixture(scope="module")
def handler():
    """One handler for the module; these tests only read, so sharing is safe."""
    return NameHandler(DEFAULT_DICTIONARY)


# --- splitting ---------------------------------------------------------------------

@pytest.mark.parametrize("name, expected", [
    ("James Wilson Sr", ("James Wilson", " Sr")),
    ("James Wilson Sr.", ("James Wilson", " Sr.")),
    ("James Wilson jr", ("James Wilson", " jr")),
    ("Henry Ford III", ("Henry Ford", " III")),
    ("Alan Grant IV", ("Alan Grant", " IV")),
    ("Bob Dole II", ("Bob Dole", " II")),
    # The comma belongs to the suffix, so reattaching is concatenation and the name does
    # not come back as "Evarts Jr" when it arrived as "Evarts, Jr".
    ("Michael Brown, Jr.", ("Michael Brown", ", Jr.")),
    ("Michael Brown , Jr.", ("Michael Brown", ", Jr.")),
])
def test_a_trailing_suffix_is_split_off(handler, name, expected):
    assert handler._split_suffix(name) == expected


@pytest.mark.parametrize("name", [
    # Surnames that merely end in a suffix's letters must survive intact.
    "Sriram Junior",
    "John Sridhar",
    "Mary Srinivasan",
    "Juan Iverson",
    # Bare "V" and "I" are excluded deliberately: as a trailing token they are far more
    # often a middle initial than a generational marker.
    "Robert Downey V",
    "Robert Downey I",
    # Nothing to split: one token is all there is to go on, so keep it as the name.
    "Jr",
    "Sr.",
    "",
    "   ",
])
def test_nothing_is_split_when_there_is_no_suffix(handler, name):
    assert handler._split_suffix(name) == (name, None)


def test_only_the_last_token_counts(handler):
    """A suffix in the middle is part of the name, not a trailing marker."""
    assert handler._split_suffix("Sr Wilson James") == ("Sr Wilson James", None)


# --- end to end through get_replacement --------------------------------------------

def suffix_of(text):
    """The trailing suffix token of a replacement, or None."""
    _, suffix = NameHandler._split_suffix(NameHandler, text)
    return suffix


@pytest.mark.parametrize("original, suffix", [
    ("James Wilson Sr", " Sr"),
    ("James Wilson Sr.", " Sr."),
    ("Henry Ford III", " III"),
    ("Michael Brown, Jr.", ", Jr."),
])
def test_the_replacement_keeps_the_suffix(handler, original, suffix):
    replacement = handler.get_replacement({"best_label": "NAME", "text": original})

    assert replacement.endswith(suffix), f"{original!r} -> {replacement!r}"
    assert replacement != original


def test_a_title_and_a_suffix_both_survive(handler):
    """The reattachment order matters: title in front, suffix behind."""
    replacement = handler.get_replacement(
        {"best_label": "NAME_MEDICAL_PROFESSIONAL", "text": "Dr. James Wilson Sr"})

    assert replacement.startswith("Dr. ")
    assert replacement.endswith(" Sr")
    # Title and suffix stripped, a real name was still drawn in between.
    assert len(replacement[len("Dr. "):-len(" Sr")].split()) == 2


def test_the_suffix_is_not_mistaken_for_the_surname(handler):
    """The suffix is stripped before components are cached for smart matching.

    Without that, "Wilson Sr" would cache "Sr" as the surname, and a later bare
    "Wilson" would miss the match and draw an unrelated name.
    """
    full = handler.get_replacement({"best_label": "NAME", "text": "Marcus Wilson Sr"})
    bare = handler.get_replacement({"best_label": "NAME_FAMILY", "text": "Wilson"})

    base, _ = handler._split_suffix(full)
    assert bare == base.split()[-1], f"{full!r} then {bare!r}"


def test_the_same_person_with_and_without_a_suffix_matches(handler):
    """"James Wilson Sr" and "James Wilson" are one person, as with a title."""
    with_suffix = handler.get_replacement({"best_label": "NAME", "text": "Clara Pembroke Sr"})
    without = handler.get_replacement({"best_label": "NAME", "text": "Clara Pembroke"})

    base, _ = handler._split_suffix(with_suffix)
    assert base == without
