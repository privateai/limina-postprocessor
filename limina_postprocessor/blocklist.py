#!/usr/bin/env python3
"""Famous-name blocklist: which (first name, surname) pairs must never be emitted.

5,194,418 pairs are blocked, so a synthetic name never lands on a real public figure.
The index ships prebuilt as ``data/blocked_pairs.bin`` (22.2 MB) and this module only
reads it; ``scripts/build_blocklist.py`` writes it.

**Keyed on names, not census positions.** The file holds the names themselves, so it
is valid against any census lists -- re-triage the data and the surviving names keep
their protection with no rebuild. `bind` attaches a pool by recording, for each pool
position, the blocklist id at the same name; after that a lookup is two array reads
and a binary search.

**No gender.** Whether 'Frankie Smith' names a public figure does not depend on which
gender pool 'Frankie' lands in, and the census tiebreak moves names between those
pools whenever the lists change.

**Two flat arrays.** ``ids`` holds every blocked surname id, the entries for one first
name sitting together in a sorted run; ``offsets`` marks where each run begins. The
same pairs as a dict of sets measured ~448 MB, and as joined strings ~622 MB, against
22.2 MB here -- do not "simplify" it to either.
"""

import array
import bisect
import re
import struct
import unicodedata
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .census_data import GENDERS

MAGIC = b'LIMBLK2'

#: Prebuilt index shipped with the package.
DEFAULT_INDEX = str(Path(__file__).parent / 'data' / 'blocked_pairs.bin')

#: Marks a pool position whose name the blocklist has never heard of.
UNKNOWN = -1

_ID_SIZE = 4            # bytes per entry in the offsets and ids arrays
_ID_TYPE = 'I'          # array typecode matching _ID_SIZE
_MAP_TYPE = 'i'         # signed, so a position can hold UNKNOWN


def fold(name: str) -> str:
    """Reduce a name to the letters the census and the famous list agree on.

    The sources punctuate differently -- 'Strauß' against 'Strauss', "O'Brien"
    against 'Obrien', 'Élise' against 'Elise' -- so comparison runs on letters only.

    Upper-casing must come before the stripping: ``'ß'.upper()`` is ``'SS'``, so
    'Strauß' folds onto 'Strauss'. Stripping first deletes the character and yields
    'STRAU' instead.
    """
    upper = unicodedata.normalize('NFKD', name.upper())
    return re.sub(r'[^A-Z]', '',
                  ''.join(c for c in upper if not unicodedata.combining(c)))


def _translate(pool_names: List[str], table: List[str]) -> array.array:
    """Map each pool name to its id in `table`, or `UNKNOWN`.

    Exact spelling is tried first and the fold index built only if something misses,
    which nothing does today: the table is the union of every census list the package
    has shipped. Folding all 162,920 surnames up front cost 119ms of a 168ms startup
    to answer zero queries.
    """
    exact = {name: index for index, name in enumerate(table)}
    mapped = [exact.get(name, UNKNOWN) for name in pool_names]
    if UNKNOWN not in mapped:
        return array.array(_MAP_TYPE, mapped)

    folded: Dict[str, int] = {}
    for index, name in enumerate(table):
        folded.setdefault(fold(name), index)
    return array.array(_MAP_TYPE, [
        found if found != UNKNOWN else folded.get(fold(name), UNKNOWN)
        for name, found in zip(pool_names, mapped)])


def _read_table(data: bytes, cursor: int) -> Tuple[List[str], int]:
    """Read one length-prefixed, newline-joined name table."""
    size, = struct.unpack_from('<I', data, cursor)
    cursor += 4
    blob = data[cursor:cursor + size].decode('utf-8')
    return (blob.split('\n') if blob else []), cursor + size


def _read_array(data: bytes, cursor: int, count: int) -> Tuple[array.array, int]:
    """Read `count` little-endian uint32s."""
    values = array.array(_ID_TYPE)
    values.frombytes(data[cursor:cursor + count * _ID_SIZE])
    return values, cursor + count * _ID_SIZE


class Blocklist:
    """Blocked surname ids per first name, as one sorted flat array."""

    def __init__(self, first_names: List[str], surnames: List[str],
                 offsets: array.array, ids: array.array):
        """Take the name tables and raw arrays. Use `load` or `empty` instead."""
        self._first_names = first_names
        self._surnames = surnames
        self._offsets = offsets
        self._ids = ids
        self._first_map: Dict[str, array.array] = {}
        self._surname_map = array.array(_MAP_TYPE)
        self._applicable: Optional[int] = 0

    @classmethod
    def load(cls, path: str, first_names: Optional[Dict[str, List[str]]] = None,
             surnames: Optional[List[str]] = None) -> 'Blocklist':
        """Read an index, binding it to a pool if one is given.

        Parses what `scripts/build_blocklist.py` writes: magic, the two name tables,
        then the offsets and ids. Change one and the other has to change with it.
        """
        data = Path(path).read_bytes()
        if not data.startswith(MAGIC):
            raise ValueError(
                f"{path} is not a blocklist index, or is an older position-keyed one "
                f"-- rebuild it with scripts/build_blocklist.py"
            )

        cursor = len(MAGIC)
        first_table, cursor = _read_table(data, cursor)
        surname_table, cursor = _read_table(data, cursor)
        n_offsets, n_ids = struct.unpack_from('<II', data, cursor)
        cursor += 8
        offsets, cursor = _read_array(data, cursor, n_offsets)
        ids, _ = _read_array(data, cursor, n_ids)

        blocklist = cls(first_table, surname_table, offsets, ids)
        if first_names is not None and surnames is not None:
            blocklist.bind(first_names, surnames)
        return blocklist

    @classmethod
    def empty(cls) -> 'Blocklist':
        """A blocklist that blocks nothing."""
        return cls([], [], array.array(_ID_TYPE, [0]), array.array(_ID_TYPE))

    @property
    def size(self) -> int:
        """Blocked pairs held, whether or not a pool can reach them."""
        return len(self._ids)

    @property
    def applicable(self) -> int:
        """Held pairs this pool can reach, counted on first use.

        Counting walks every blocked surname of every pool first name, and the names
        triage keeps are the ones with the longest runs -- 'Michael' alone has 29,970.
        That measured 248ms against 9ms for the rest of construction. Nothing on the
        per-document path reads it, so it waits until asked for.
        """
        if self._applicable is None:
            reachable = {sid for sid in self._surname_map if sid != UNKNOWN}
            self._applicable = sum(
                1
                for gender in GENDERS
                for bid in self._first_map[gender] if bid != UNKNOWN
                for sid in self._ids[self._offsets[bid]:self._offsets[bid + 1]]
                if sid in reachable)
        return self._applicable

    @property
    def names(self) -> Tuple[List[str], List[str]]:
        """The (first name, surname) tables."""
        return self._first_names, self._surnames

    @property
    def arrays(self) -> Tuple[array.array, array.array]:
        """The raw (offsets, ids)."""
        return self._offsets, self._ids

    @property
    def maps(self) -> Tuple[Dict[str, array.array], array.array]:
        """The bound pool-position-to-id lookups, as ({gender: firsts}, surnames)."""
        return self._first_map, self._surname_map

    def bind(self, first_names: Dict[str, List[str]],
             surnames: List[str]) -> 'Blocklist':
        """Point this blocklist at a pool, so positions translate to ids.

        A pool name the tables have never seen maps to `UNKNOWN` and blocks nothing;
        there is no famous pairing to block it against. `applicable` is invalidated
        rather than recomputed -- see that property.
        """
        self._surname_map = _translate(surnames, self._surnames)
        self._first_map = {gender: _translate(first_names[gender], self._first_names)
                           for gender in GENDERS}
        self._applicable = None
        return self

    def blocks(self, gender: str, first_id: int, surname_id: int) -> bool:
        """Whether pairing these two pool positions is blocked.

        Both are translated into the blocklist's own ids first. An unknown position,
        or a first name with no run recorded, blocks nothing -- which is also what
        makes the empty blocklist answer False everywhere.
        """
        first_map = self._first_map.get(gender)
        if first_map is None or first_id >= len(first_map):
            return False
        bid = first_map[first_id]
        if bid == UNKNOWN or surname_id >= len(self._surname_map):
            return False
        sid = self._surname_map[surname_id]
        if sid == UNKNOWN:
            return False

        lo, hi = self._offsets[bid], self._offsets[bid + 1]
        at = bisect.bisect_left(self._ids, sid, lo, hi)
        return at < hi and self._ids[at] == sid
