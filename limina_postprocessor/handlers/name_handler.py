#!/usr/bin/env python3
"""Name entity handler: replaces name placeholders with synthetic census names."""

from typing import Dict, List, Optional, Tuple

from ..census_data import DEFAULT_CENSUS_DATA
from ..name_pool import NamePool
from .base_handler import BaseEntityHandler
from .gender_detector import (SUFFIX_TITLES, TITLES_LONGEST_FIRST,
                              GenderDetector, starts_with_title)

# Entity types this handler claims, and which of them want a full name rather than a
# single component.
FULL_NAME_TYPES = frozenset({'NAME', 'NAME_MEDICAL_PROFESSIONAL'})
GIVEN_NAME_TYPES = frozenset({'NAME_GIVEN'})
FAMILY_NAME_TYPES = frozenset({'NAME_FAMILY'})
HANDLED_TYPES = FULL_NAME_TYPES | GIVEN_NAME_TYPES | FAMILY_NAME_TYPES


def _name_words(name: str) -> List[str]:
    """Split a name into words, dropping trailing honorifics.

    The coreference index treats the last word as the surname, so a trailing "Sr"
    or "III" has to come off first -- otherwise "James Wilson Sr" indexes under "Sr",
    a later bare "Wilson" misses, and one person gets two synthetic identities.
    """
    words = name.split()
    while len(words) > 1 and words[-1] in SUFFIX_TITLES:
        words.pop()
    return words


class NameHandler(BaseEntityHandler):
    """Replaces name entities with synthetic names composed from census data.

    Holds no state between documents. Within a document it keeps a cache so the same
    original name always yields the same replacement, including when one document
    refers to a person by full name in one place and by surname alone in another.
    """

    def __init__(self, census_data_dir: Optional[str] = None,
                 seed: Optional[int] = None,
                 blocklist_index: Optional[str] = None):
        """Load the name pool and the gender detector.

        `census_data_dir` defaults to the packaged census data and `blocklist_index`
        to the packaged index.

        Pass `seed` to get the same names a previous run produced. Leave it out and
        the pool picks its own seed from the clock, so every run differs. Either way
        `self.seed` reports the seed in use, so a run can be replayed afterwards.
        """
        super().__init__()

        self.pool = NamePool(census_data_dir or DEFAULT_CENSUS_DATA, seed=seed,
                             blocklist_index=blocklist_index)
        # The detector reads its lookup from the pool, so the two cannot disagree
        # about a name's gender and the census files are parsed only once.
        self.gender_detector = GenderDetector(self.pool.first_names)

        # Within-document coreference. Replacements are held as (first, surname)
        # rather than joined, because recovering a half from a joined name needs a
        # split, and splitting truncates the six census first names that contain a
        # space ("Jo Ann", "Maria De Fatima", "Le Roy").
        self._replacement_for_first: Dict[str, Tuple[str, str]] = {}
        self._replacement_for_last: Dict[str, Tuple[str, str]] = {}

    @property
    def seed(self) -> int:
        """The seed this handler's names were drawn from. Pass it back to replay."""
        return self.pool.seed

    def can_handle(self, entity_type: str) -> bool:
        return entity_type in HANDLED_TYPES

    def get_replacement(self, entity: Dict) -> str:
        """Return a synthetic replacement for one name entity."""
        entity_type = entity.get('best_label', entity.get('entity_type', ''))
        original_text = entity.get('text', '').strip()

        title, name_text = self._split_title(original_text)

        cache_key = (entity_type, name_text)
        if cache_key in self.cache:
            replacement = self.cache[cache_key]
        else:
            # Coreference matches on the title-stripped name, but gender detection
            # gets the original: the honorific is the strongest gender signal there
            # is, and stripping it first made that tier unreachable.
            components = (self._match_known_component(name_text)
                          or self._new_name(original_text))
            replacement = self._format(entity_type, components)
            replacement = self._match_capitalization(name_text, replacement)
            self.cache[cache_key] = replacement
            self._remember(name_text, components)

        return f"{title} {replacement}" if title else replacement

    def clear_cache(self) -> None:
        """Drop per-document state. Called by the processor for each new document."""
        super().clear_cache()
        self._replacement_for_first.clear()
        self._replacement_for_last.clear()

    # ------------------------------------------------------------------ internals

    def _new_name(self, name_text: str) -> Tuple[str, str]:
        """Draw a fresh (first, surname) pair matching the entity's apparent gender."""
        gender = self.gender_detector.detect_gender(name_text) or self.pool.coin_flip()
        return self.pool.full_name(gender)

    def _match_known_component(self, name_text: str) -> Optional[Tuple[str, str]]:
        """Reuse an earlier replacement if this document already named this person.

        "Dr. Alice Chen" followed by a bare "Chen" must map to the same synthetic
        person, so both halves of every replacement are indexed by the original name
        they stood in for.

        The index is keyed on single words, so a multi-word lookup is also tried
        word by word. Matching only the whole string would work in one direction
        only -- full name then bare surname -- and miss the reverse, giving the same
        person two synthetic identities.
        """
        parts = _name_words(name_text)
        candidates = [name_text] + ([parts[-1], parts[0]] if len(parts) > 1 else [])

        for candidate in candidates:
            known = (self._replacement_for_last.get(candidate)
                     or self._replacement_for_first.get(candidate))
            if known:
                return known
        return None

    def _remember(self, name_text: str, components: Tuple[str, str]) -> None:
        """Index a replacement under the parts of the original name it replaced."""
        parts = _name_words(name_text)
        if not parts:
            return
        self._replacement_for_first[parts[0]] = components
        self._replacement_for_last[parts[-1]] = components

    @staticmethod
    def _format(entity_type: str, components: Tuple[str, str]) -> str:
        """Render the part of the name the entity type asks for.

        Every type in HANDLED_TYPES is matched explicitly rather than falling through
        to a default, so adding a type to that set without deciding how it renders
        fails here instead of silently producing a full name.
        """
        first, surname = components
        if entity_type in GIVEN_NAME_TYPES:
            return first
        if entity_type in FAMILY_NAME_TYPES:
            return surname
        if entity_type in FULL_NAME_TYPES:
            return f"{first} {surname}"
        raise ValueError(f"no formatting defined for entity type {entity_type!r}")

    @staticmethod
    def _split_title(name: str) -> Tuple[Optional[str], str]:
        """Split a leading honorific off a name, returning (title, remainder)."""
        name = name.strip()
        for title in TITLES_LONGEST_FIRST:
            if starts_with_title(name, title):
                return title, name[len(title):].strip()
        return None, name

    @staticmethod
    def _match_capitalization(original: str, replacement: str) -> str:
        """Mirror an all-caps or all-lowercase original."""
        if original.isupper():
            return replacement.upper()
        if original.islower():
            return replacement.lower()
        return replacement
