#!/usr/bin/env python3
"""Gender detection from an honorific, falling back to census first-name data."""

from typing import Dict, Iterable, List, Optional

# Honorifics that imply a gender.
MALE_TITLES = frozenset({'Mr.', 'Mr', 'Jr.', 'Jr', 'III', 'IV', 'Sr.', 'Sr'})
FEMALE_TITLES = frozenset({'Ms.', 'Ms', 'Mrs.', 'Mrs', 'Miss', 'Mss.', 'Mss'})
# Recognised so they can be stripped before a first-name lookup, but they imply nothing.
NEUTRAL_TITLES = frozenset({'Dr.', 'Dr', 'Prof.', 'Prof'})

ALL_TITLES = MALE_TITLES | FEMALE_TITLES | NEUTRAL_TITLES

# Honorifics that trail a name rather than lead it. They are listed in MALE_TITLES
# too, but the prefix scan can never match them there, so they only matter when
# identifying which word of a name is the surname.
SUFFIX_TITLES = frozenset({'Jr.', 'Jr', 'Sr.', 'Sr', 'III', 'IV'})


def _longest_first(titles: Iterable[str]) -> List[str]:
    """Order titles so the longest match wins: "Mrs." before "Mr", "Dr." before "Dr"."""
    return sorted(titles, key=len, reverse=True)


# Derived once at import rather than per instance: these depend only on the constants
# above. TITLES_LONGEST_FIRST is also what NameHandler scans when splitting a title
# off a name, so both modules share one ordering instead of each sorting its own.
TITLES_LONGEST_FIRST = _longest_first(ALL_TITLES)
GENDERED_TITLES_LONGEST_FIRST = _longest_first(MALE_TITLES | FEMALE_TITLES)
GENDER_OF_TITLE = {
    **{title: 'male' for title in MALE_TITLES},
    **{title: 'female' for title in FEMALE_TITLES},
}


def starts_with_title(text: str, title: str) -> bool:
    """True if `text` opens with `title` as a whole word.

    The trailing-whitespace requirement is what stops "Sriram" being read as the title
    "Sr" and resolved to male, or "Drew" having "Dr" stripped so that "ew" is looked up
    as a first name.
    """
    return text.startswith(title) and text[len(title):len(title) + 1].isspace()


class GenderDetector:
    """Resolves a name to 'male', 'female', or None.

    None means undetermined and the caller decides what to do with it. There is no
    network fallback: a name absent from the census lists is simply unresolved.
    """

    def __init__(self, first_names: Dict[str, List[str]]):
        """Build the lookup from gender-resolved name pools.

        Takes the pools `NamePool` has already built rather than re-reading the census
        files. Besides halving the parse cost, this makes it impossible for the
        detector and the name source to disagree about a name's gender.
        """
        self._gender_of_first_name = {
            name.upper(): gender
            for gender, names in first_names.items()
            for name in names
        }

    def detect_gender(self, name_text: str) -> Optional[str]:
        """Return the gender implied by an honorific, else by the first name."""
        return self._from_title(name_text) or self._from_first_name(name_text)

    @staticmethod
    def _from_title(name_text: str) -> Optional[str]:
        for title in GENDERED_TITLES_LONGEST_FIRST:
            if starts_with_title(name_text, title):
                return GENDER_OF_TITLE[title]
        return None

    def _from_first_name(self, name_text: str) -> Optional[str]:
        first_name = self._first_name_of(name_text)
        return self._gender_of_first_name.get(first_name.upper()) if first_name else None

    @staticmethod
    def _first_name_of(name_text: str) -> Optional[str]:
        """Strip one leading honorific and return the first remaining word."""
        text = name_text.strip()
        for title in TITLES_LONGEST_FIRST:
            if starts_with_title(text, title):
                text = text[len(title):].strip()
                break
        parts = text.split()
        return parts[0] if parts else None
