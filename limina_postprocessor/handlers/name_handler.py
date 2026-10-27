#!/usr/bin/env python3
"""Name entity handler: replaces name placeholders with synthetic census names."""

from pathlib import Path
from typing import Dict, Optional, Tuple

from ..name_pool import NamePool
from .base_handler import BaseEntityHandler
from .gender_detector import (TITLES_LONGEST_FIRST, GenderDetector,
                              starts_with_title)

# Entity types this handler claims, and which of them want a full name rather than a
# single component.
FULL_NAME_TYPES = frozenset({'NAME', 'NAME_MEDICAL_PROFESSIONAL'})
GIVEN_NAME_TYPES = frozenset({'NAME_GIVEN'})
FAMILY_NAME_TYPES = frozenset({'NAME_FAMILY'})
HANDLED_TYPES = FULL_NAME_TYPES | GIVEN_NAME_TYPES | FAMILY_NAME_TYPES


class NameHandler(BaseEntityHandler):
    """Replaces name entities with synthetic names composed from census data.

    Holds no state between documents. Within a document it keeps a cache so the same
    original name always yields the same replacement, including when one document
    refers to a person by full name in one place and by surname alone in another.
    """

    def __init__(self, census_data_dir: Optional[str] = None,
                 seed: Optional[int] = None):
        """Load the name pools.

        `census_data_dir` defaults to the packaged census data. `seed` reproduces a
        previous run; omitted, the pool seeds itself from the clock.
        """
        super().__init__()

        if census_data_dir is None:
            census_data_dir = str(Path(__file__).parent.parent / 'data' / 'census_data')

        self.pool = NamePool(census_data_dir, seed=seed)
        # The detector reads its lookup from the pool, so the two cannot disagree
        # about a name's gender and the census files are parsed only once.
        self.gender_detector = GenderDetector(self.pool.first_names)

        # Within-document coreference, keyed on the original name's components and
        # holding the replacement as (first, surname) rather than a joined string. A
        # joined string would have to be re-split to recover either half, and
        # splitting breaks on the six multi-word first names in the census lists
        # ("Jo Ann", "Maria De Fatima", "Le Roy").
        self._replacement_for_first: Dict[str, Tuple[str, str]] = {}
        self._replacement_for_last: Dict[str, Tuple[str, str]] = {}

    @property
    def seed(self) -> int:
        """The seed this handler's names were drawn from. Pass it back to replay."""
        return self.pool.seed

    def can_handle(self, entity_type: str) -> bool:
        return entity_type in HANDLED_TYPES

    def get_replacement(self, entity: Dict, context: Optional[Dict] = None) -> str:
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

    def clear_cache(self):
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
        word by word. Without that, the match only worked in one direction: a full
        name followed by a bare surname was caught, but a bare surname followed by
        the full name was not, and the same person got two synthetic identities.
        """
        candidates = [name_text]
        parts = name_text.split()
        if len(parts) > 1:
            candidates += [parts[-1], parts[0]]

        for candidate in candidates:
            known = (self._replacement_for_last.get(candidate)
                     or self._replacement_for_first.get(candidate))
            if known:
                return known
        return None

    def _remember(self, name_text: str, components: Tuple[str, str]):
        """Index a replacement under the parts of the original name it replaced."""
        parts = name_text.split()
        if len(parts) >= 2:
            self._replacement_for_first[parts[0]] = components
            self._replacement_for_last[parts[-1]] = components
        elif parts:
            self._replacement_for_first[parts[0]] = components
            self._replacement_for_last[parts[0]] = components

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
