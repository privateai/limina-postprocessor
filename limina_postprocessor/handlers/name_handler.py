#!/usr/bin/env python3
"""Name entity handler for replacing name entities by sampling parquet row groups."""

import random
import pyarrow.compute as pc
import pyarrow.parquet as pq
from pathlib import Path
from typing import Dict, Optional
from .base_handler import BaseEntityHandler
from .gender_detector import GenderDetector


class NameHandler(BaseEntityHandler):
    """Handler for name entity replacement, sampling parquet row groups directly."""

    # Column holding the value substituted for each entity type. full_name is stored
    # as "first_name last_name", so a full name needs only that one column decoded.
    COLUMN_BY_TYPE = {
        'NAME': 'full_name',
        'NAME_MEDICAL_PROFESSIONAL': 'full_name',
        'NAME_GIVEN': 'first_name',
        'NAME_FAMILY': 'last_name',
    }

    # Sampling budget: 100 probes total (as before), but at most 4 row group reads
    MAX_ROW_GROUP_READS = 4
    PROBES_PER_ROW_GROUP = 25

    def __init__(self, dictionary_path: str, enable_api_gender: bool = False,
                 census_data_dir: Optional[str] = None):
        """Initialize name handler with a memory-mapped parquet reader."""
        super().__init__()

        # Set default census data path relative to this file
        if census_data_dir is None:
            package_dir = Path(__file__).parent.parent  # limina_postprocessor/
            census_data_dir = str(package_dir / "data" / "census_data")

        if not Path(dictionary_path).exists():
            raise FileNotFoundError(f"Dictionary not found: {dictionary_path}")

        # Memory-map the parquet file; row groups are decoded only when sampled
        self.dictionary_path = dictionary_path
        self.parquet = pq.ParquetFile(dictionary_path, memory_map=True)
        self.dictionary_rows = self.parquet.metadata.num_rows

        # Index row groups by gender from column statistics (metadata only, no scan)
        self.row_groups = {'male': [], 'female': []}
        self.mixed_row_groups = set()
        self._index_row_groups()

        # Initialize gender detector
        self.gender_detector = GenderDetector(
            census_data_dir=census_data_dir,
            enable_api=enable_api_gender
        )

        # Honorifics, longest first so the longest match wins (see _split_title)
        self.sorted_titles = sorted(
            self.gender_detector.male_titles
            | self.gender_detector.female_titles
            | self.gender_detector.neutral_titles,
            key=len,
            reverse=True
        )

        # Within-document coreference: smart component matching
        self.last_name_to_full = {}   # Maps last name → full name (e.g., "Smith" → "John Smith")
        self.first_name_to_full = {}  # Maps first name → full name (e.g., "John" → "John Smith")

        # Global tracking to ensure NO repeats across documents
        self.used_names_global = set()  # Track all names used across ALL documents

    def __del__(self):
        """Close the parquet file handle on cleanup."""
        if hasattr(self, 'parquet'):
            self.parquet.close()

    def _index_row_groups(self):
        """Record which row groups can contain each gender, using parquet statistics."""
        metadata = self.parquet.metadata

        # Parquet column order need not match ours, so find gender by name
        gender_col = next(
            i for i in range(metadata.num_columns)
            if metadata.row_group(0).column(i).path_in_schema == 'gender'
        )

        for row_group in range(metadata.num_row_groups):
            stats = metadata.row_group(row_group).column(gender_col).statistics

            # A single-valued row group needs no filtering; anything else (mixed, or
            # missing statistics) can hold either gender and is filtered after reading
            if stats is not None and stats.min == stats.max:
                if stats.min in self.row_groups:
                    self.row_groups[stats.min].append(row_group)
            else:
                self.mixed_row_groups.add(row_group)
                self.row_groups['male'].append(row_group)
                self.row_groups['female'].append(row_group)

    def can_handle(self, entity_type: str) -> bool:
        """Check if this is a name entity."""
        return entity_type in ['NAME', 'NAME_GIVEN', 'NAME_FAMILY', 'NAME_MEDICAL_PROFESSIONAL']

    def get_replacement(self, entity: Dict, context: Optional[Dict] = None) -> str:
        """Get replacement name from parquet via DuckDB with smart caching."""
        entity_type = entity.get('best_label', entity.get('entity_type', ''))
        original_text = entity.get('text', '').strip()

        # Extract title if present
        title, cleaned_text = self._split_title(original_text)

        # Check exact cache first
        cache_key = (entity_type, cleaned_text)
        if cache_key in self.cache:
            replacement = self.cache[cache_key]
        else:
            # Smart matching: check if we've seen this name component before
            replacement = self._smart_match(entity_type, cleaned_text)
            if replacement:
                self.cache[cache_key] = replacement
            else:
                # Generate new replacement from parquet
                replacement = self._generate_name(entity_type, cleaned_text)
                self.cache[cache_key] = replacement

                # Cache components for future smart matching
                self._cache_components(entity_type, cleaned_text, replacement)

        # Add title back if present
        if title:
            replacement = f"{title} {replacement}"

        return replacement

    def _smart_match(self, entity_type: str, cleaned_text: str) -> Optional[str]:
        """Try to match against previously seen name components."""
        is_full_name = entity_type in ['NAME', 'NAME_MEDICAL_PROFESSIONAL']

        # Check last name match
        if cleaned_text in self.last_name_to_full:
            full_name = self.last_name_to_full[cleaned_text]
            return full_name if is_full_name else full_name.split()[-1]

        # Check first name match
        if cleaned_text in self.first_name_to_full:
            full_name = self.first_name_to_full[cleaned_text]
            return full_name if is_full_name else full_name.split()[0]

        return None

    def _cache_components(self, entity_type: str, original_text: str, replacement: str):
        """Cache name components for future smart matching."""
        if entity_type not in ['NAME', 'NAME_MEDICAL_PROFESSIONAL']:
            return

        parts = replacement.split()
        original_parts = original_text.split()

        if len(parts) >= 2 and len(original_parts) >= 2:
            self.last_name_to_full[original_parts[-1]] = replacement
            self.first_name_to_full[original_parts[0]] = replacement

    def _generate_name(self, entity_type: str, original_text: str) -> str:
        """Generate replacement name with NO reuse guarantee across documents."""
        gender = self.gender_detector.detect_gender(original_text)

        # Select gender pool, random choice if unknown
        if gender in ('male', 'female'):
            target_gender = gender
        else:
            target_gender = 'male' if random.random() < 0.5 else 'female'

        # Only the component we actually use needs to be decoded
        column = self.COLUMN_BY_TYPE.get(entity_type, 'full_name')

        # Keep trying until we find an unused name
        fallback = None
        for _ in range(self.MAX_ROW_GROUP_READS):
            candidates = self._sample_row_group(target_gender, column)
            if len(candidates) == 0:
                continue

            # Probe within the decoded row group before paying for another read
            for _ in range(min(self.PROBES_PER_ROW_GROUP, len(candidates))):
                check_name = candidates[random.randrange(len(candidates))].as_py()

                # If not used globally, mark it and use it
                if check_name not in self.used_names_global:
                    self.used_names_global.add(check_name)
                    return self._match_capitalization(original_text, check_name)

                fallback = check_name

        # If we couldn't find unused name in 100 probes (very unlikely), use it anyway
        # This should never happen with 1.2B names
        if fallback is None:
            raise RuntimeError(
                f"No {target_gender} names available in {self.dictionary_path}"
            )

        return self._match_capitalization(original_text, fallback)

    def _sample_row_group(self, gender: str, column: str):
        """Decode one random row group, returning its values of `column` for `gender`."""
        row_group = random.choice(self.row_groups[gender])

        if row_group not in self.mixed_row_groups:
            table = self.parquet.read_row_group(row_group, columns=[column])
        else:
            table = self.parquet.read_row_group(row_group, columns=[column, 'gender'])
            table = table.filter(pc.equal(table.column('gender'), gender))

        return table.column(column)

    def _split_title(self, name: str) -> tuple:
        """Split a leading honorific off a name, returning (title, remaining_name).

        A title only matches when whitespace follows it, so names that merely start
        with a title's letters ("Drew", "Sriram", "Missy") are left intact. Titles
        are tried longest-first so "Mrs." wins over "Mr" and "Dr." over "Dr".
        """
        name = name.strip()
        for title in self.sorted_titles:
            if name.startswith(title) and name[len(title):len(title) + 1].isspace():
                return title, name[len(title):].strip()
        return None, name

    def _match_capitalization(self, original: str, replacement: str) -> str:
        """Match capitalization pattern of original text."""
        if original.isupper():
            return replacement.upper()
        elif original.islower():
            return replacement.lower()
        return replacement

    def clear_cache(self):
        """Clear cache for new document, including smart matching caches."""
        super().clear_cache()
        self.last_name_to_full = {}
        self.first_name_to_full = {}
