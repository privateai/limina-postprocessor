#!/usr/bin/env python3
"""Name entity handler for replacing name entities by sampling parquet row groups."""

import random
import threading
import pyarrow as pa
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

    # Trailing generational suffixes, compared without a trailing period and upper-cased
    # (see _split_suffix). Bare "V" and "I" are deliberately absent: as a trailing token
    # they are far more often a middle initial than a generational marker, and stripping
    # an initial would change the name rather than preserve it.
    NAME_SUFFIXES = frozenset({'JR', 'SR', 'II', 'III', 'IV'})

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

        # Memory-map the parquet file; row groups are decoded only when sampled.
        #
        # pre_buffer is pinned off rather than left to the default. It coalesces reads
        # through a background I/O pool to hide latency on remote filesystems, which buys
        # a memory-mapped local file nothing, and the ReadRangeCache backing it is the
        # per-reader state that makes a shared ParquetFile unsafe to read from several
        # threads (see _reader). pyarrow turned the default on in 25 — it was off through
        # 21 — so leaving it implicit means the same code is thread-hostile or not
        # depending on which pyarrow the host resolved.
        self.dictionary_path = dictionary_path
        self.parquet = pq.ParquetFile(dictionary_path, memory_map=True, pre_buffer=False)
        self.dictionary_rows = self.parquet.metadata.num_rows

        # One reader per thread; the constructing thread reuses the one just opened, so
        # single-threaded callers open nothing extra. The footer is kept so that a new
        # thread's reader does not re-parse it. See _reader.
        self._metadata = self.parquet.metadata
        self._local = threading.local()
        self._local.reader = self.parquet

        # Index row groups by gender from column statistics (metadata only, no scan).
        # _sample_row_group corrects this index as it learns what each row group really
        # holds, so it changes at run time and needs guarding once threads are in play.
        self._index_lock = threading.Lock()
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

        # Global tracking to ensure NO repeats across documents. Threads share this one
        # set, which is what keeps the uniqueness guarantee intact under
        # process_documents; separate processes each get their own copy of it and can
        # therefore hand out the same name twice.
        self.used_names_global = set()  # Track all names used across ALL documents
        self._names_lock = threading.Lock()

    def __del__(self):
        """Close the parquet file handle on cleanup."""
        if hasattr(self, 'parquet'):
            self.parquet.close()

    @property
    def last_name_to_full(self):
        """Maps last name -> full name ("Smith" -> "John Smith"), for this document."""
        return self._doc_dict('last_name_to_full')

    @property
    def first_name_to_full(self):
        """Maps first name -> full name ("John" -> "John Smith"), for this document."""
        return self._doc_dict('first_name_to_full')

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

            # A single-valued row group needs no filtering; anything else (mixed,
            # unrecognized, or missing statistics) can hold either gender and is
            # filtered after reading
            single = None
            if stats is not None and stats.min == stats.max:
                single = self._stat_str(stats.min)

            if single in self.row_groups:
                self.row_groups[single].append(row_group)
            else:
                self.mixed_row_groups.add(row_group)
                self.row_groups['male'].append(row_group)
                self.row_groups['female'].append(row_group)

        # An empty pool means no row group was recognized, so every replacement
        # would fail later inside random.choice(). Fail here instead, with the
        # values that were actually seen.
        empty = [gender for gender, groups in self.row_groups.items() if not groups]
        if empty:
            sample = metadata.row_group(0).column(gender_col).statistics
            raise RuntimeError(
                f"No row groups available for {empty} in {self.dictionary_path}. "
                f"Expected the 'gender' column to contain 'male'/'female'; row group 0 "
                f"reported min={getattr(sample, 'min', None)!r} "
                f"max={getattr(sample, 'max', None)!r} "
                f"across {metadata.num_row_groups} row groups (pyarrow {pa.__version__})."
            )

    @staticmethod
    def _stat_str(value):
        """Normalize a parquet statistic to str.

        pyarrow returns BYTE_ARRAY statistics as bytes on some versions and str on
        others, so comparisons against str keys must not depend on which is in use.
        """
        return value.decode('utf-8', 'replace') if isinstance(value, bytes) else value

    def can_handle(self, entity_type: str) -> bool:
        """Check if this is a name entity."""
        return entity_type in ['NAME', 'NAME_GIVEN', 'NAME_FAMILY', 'NAME_MEDICAL_PROFESSIONAL']

    def get_replacement(self, entity: Dict, context: Optional[Dict] = None) -> str:
        """Get replacement name from parquet via DuckDB with smart caching."""
        entity_type = entity.get('best_label', entity.get('entity_type', ''))
        original_text = entity.get('text', '').strip()

        # Extract title if present
        title, cleaned_text = self._split_title(original_text)

        # And any trailing generational suffix. Stripped before the cache key is built, so
        # "James Wilson Sr" and "James Wilson" resolve to one person the way "Dr. James
        # Wilson" already does — and so the suffix is not mistaken for the surname when
        # components are cached for smart matching.
        cleaned_text, suffix = self._split_suffix(cleaned_text)

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

        # Suffix last, so a name carrying both comes back as "Dr. New Name Sr"
        if suffix:
            replacement = f"{replacement}{suffix}"

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

            # Probe within the decoded row group before paying for another read.
            #
            # The check-and-add has to be atomic, or two threads that both find
            # `check_name` unused will both emit it. The lock covers only this probe
            # loop, never the row group read above: that read is ~99% of the time and
            # the only part that runs in parallel, so holding the lock across it would
            # serialize the whole benefit away.
            with self._names_lock:
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
        """Decode one random row group, returning its values of `column` for `gender`.

        Row groups that statistics could not resolve to a single gender are filtered
        after reading. Each such read also teaches us what the row group really holds,
        and the index is corrected accordingly, so the filtering cost fades as row
        groups are visited. Never returns an empty result.

        Safe to call from several threads: the index is read and corrected under
        `_index_lock`, the decode runs unlocked against a reader this thread owns
        (see `_reader`), and unlocked decoding is where pyarrow releases the GIL and
        where the parallelism actually comes from. Reading
        `needs_filtering` under the lock and then acting on it after releasing is sound
        because `mixed_row_groups` only ever shrinks — nothing adds to it after __init__.
        A concurrent update can therefore only make us filter a row group that no longer
        needs it, which wastes a little work and cannot produce a wrong gender.
        """
        other = 'female' if gender == 'male' else 'male'
        reader = self._reader()

        while True:
            with self._index_lock:
                if not self.row_groups[gender]:
                    raise RuntimeError(
                        f"No {gender} names available in {self.dictionary_path}"
                    )
                row_group = random.choice(self.row_groups[gender])
                needs_filtering = row_group in self.mixed_row_groups

            if not needs_filtering:
                table = reader.read_row_group(row_group, columns=[column])
                return table.column(column)

            table = reader.read_row_group(row_group, columns=[column, 'gender'])
            total_rows = table.num_rows
            table = table.filter(pc.equal(table.column('gender'), gender))

            if table.num_rows == 0:
                # Holds none of this gender, so stop offering it for this gender
                with self._index_lock:
                    self._drop_row_group(row_group, gender)
                continue

            if table.num_rows == total_rows:
                # Holds only this gender, so it never needs filtering again
                with self._index_lock:
                    self.mixed_row_groups.discard(row_group)
                    self._drop_row_group(row_group, other)

            return table.column(column)

    def _reader(self):
        """The parquet reader belonging to the calling thread.

        pq.ParquetFile is not safe to read from concurrently: one reader carries mutable
        state across a read_row_group call, so two threads in that call at once can
        invalidate each other's and fail with "ReadRangeCache did not find matching cache
        entry". Giving each thread its own reader removes the sharing instead of locking
        around it — a lock would have to span the decode, which is where the GIL is
        released and where all of the parallelism comes from.

        Cheap to do per thread: the file is memory-mapped, so the readers share the same
        pages, and the footer is handed over already parsed. That last part is not an
        optimization detail — parsing it costs ~13 ms for the 9,659 row groups in the
        3.8 GB dictionary, and process_documents builds a fresh pool per call, so new
        threads open readers on every call rather than once per process. Paying the footer
        each time measured a third of the parallel run time; reusing it is ~0.1 ms.

        Readers are dropped when their thread exits, which for a pool is at shutdown.
        """
        reader = getattr(self._local, 'reader', None)
        if reader is None:
            reader = pq.ParquetFile(
                self.dictionary_path,
                metadata=self._metadata,
                memory_map=True,
                pre_buffer=False,
            )
            self._local.reader = reader
        return reader

    def _drop_row_group(self, row_group: int, gender: str):
        """Remove a row group from a gender's pool once it is known not to apply.

        Caller must hold `_index_lock`.
        """
        try:
            self.row_groups[gender].remove(row_group)
        except ValueError:
            pass

    def _split_suffix(self, name: str) -> tuple:
        """Split a trailing generational suffix off a name, returning (name, suffix).

        The mirror of _split_title, and needed for the same reason: DEID puts the suffix
        inside the name entity ("James Wilson Sr"), so a replacement drawn from the
        dictionary drops it and the text loses a token it started with.

        Matched as a whole trailing token, so surnames that merely end in those letters
        ("Sriram", "Junior") are left intact. `suffix` carries the separator that preceded
        it, so reattaching is concatenation and "Wilson, Jr." does not come back as
        "Wilson Jr.". Returns (name, None) when there is nothing to split.
        """
        parts = name.rstrip().rsplit(None, 1)
        if len(parts) != 2:
            # One token or empty: a bare "Jr" is all there is to go on, so keep it as the
            # name rather than strip it and have nothing left to replace.
            return name, None

        head, last = parts
        if last.rstrip(".").upper() not in self.NAME_SUFFIXES:
            return name, None

        base = head.rstrip()
        separator = ", " if base.endswith(",") else " "
        # Commas and any space before them, so "Brown , Jr." does not leave the base name
        # with a trailing space and miss the cache entry for a plain "Brown".
        return base.rstrip(', '), f"{separator}{last}"

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
