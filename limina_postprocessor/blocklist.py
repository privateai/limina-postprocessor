#!/usr/bin/env python3
"""Famous-name blocklist: which (first name, surname) pairs must never be emitted.

4,631,292 pairs are blocked, so a synthetic name never lands on a real public figure.
The list ships prebuilt as ``data/blocked_pairs.bin`` (18.6 MB) and this module only
reads it.

**How it is stored.** Two flat arrays per gender. ``ids`` holds every blocked surname
position, with all the entries for one first name sitting together in a sorted run;
``offsets`` records where each first name's run begins. Looking up a pair is then a
binary search inside one run -- roughly nine comparisons.

That shape is what makes it small. Holding the same 4,631,292 pairs as a dict of sets
measured ~190 MB, and as a set of joined strings ~420 MB, against 18.6 MB here. Do
not "simplify" it to either.

**Positions, not names.** The index records *where* a name sits in the census lists,
never the name itself. So it is only valid for the exact lists it was built from:
editing the census data shifts every position and would silently block the wrong
names. The index carries a fingerprint of the lists and refuses to load against
anything else.
"""

import array
import bisect
import hashlib
import struct
from pathlib import Path
from typing import Dict, List, Tuple

from .census_data import GENDERS

MAGIC = b'LIMBLK1'
FINGERPRINT_CHARS = 64

#: Prebuilt index shipped with the package.
DEFAULT_INDEX = str(Path(__file__).parent / 'data' / 'blocked_pairs.bin')


def fingerprint(first_names: Dict[str, List[str]], surnames: List[str]) -> str:
    """Hash the resolved name lists, to detect an index built from different data."""
    digest = hashlib.sha256()
    for gender in GENDERS:
        digest.update(gender.encode())
        for name in first_names[gender]:
            digest.update(name.encode())
            digest.update(b'\0')
    for surname in surnames:
        digest.update(surname.encode())
        digest.update(b'\0')
    return digest.hexdigest()


class Blocklist:
    """Blocked surname positions per first name, as one sorted flat array."""

    def __init__(self, offsets: Dict[str, array.array],
                 ids: Dict[str, array.array], census_fingerprint: str = ''):
        """Take the raw arrays directly. Use `load` or `empty` instead."""
        self._offsets = offsets
        self._ids = ids
        self.fingerprint = census_fingerprint

    @property
    def size(self) -> int:
        """Blocked pairs held."""
        return sum(len(ids) for ids in self._ids.values())

    def arrays(self, gender: str) -> Tuple[array.array, array.array]:
        """Return this gender's (offsets, ids). Used by the index builder."""
        return self._offsets[gender], self._ids[gender]

    def blocks(self, gender: str, first_id: int, surname_id: int) -> bool:
        """Whether pairing these two positions is blocked.

        A first name with no run recorded blocks nothing. That covers the empty
        blocklist, whose offsets array is too short to index by position at all.
        """
        offsets = self._offsets[gender]
        if first_id + 1 >= len(offsets):
            return False
        lo, hi = offsets[first_id], offsets[first_id + 1]
        if lo == hi:
            return False
        ids = self._ids[gender]
        at = bisect.bisect_left(ids, surname_id, lo, hi)
        return at < hi and ids[at] == surname_id

    @classmethod
    def empty(cls) -> 'Blocklist':
        """A blocklist that blocks nothing."""
        return cls({g: array.array('I', [0]) for g in GENDERS},
                   {g: array.array('I') for g in GENDERS})

    @classmethod
    def load(cls, path: str, expect: str = '') -> 'Blocklist':
        """Read an index, refusing one built from different census data.

        Parses the layout `tools/build_blocklist.py` writes: magic, fingerprint, then
        per gender a pair of counts followed by the two arrays. Change one and the
        other has to change with it.
        """
        data = Path(path).read_bytes()
        if not data.startswith(MAGIC):
            raise ValueError(f"{path} is not a blocklist index")

        cursor = len(MAGIC)
        stored = data[cursor:cursor + FINGERPRINT_CHARS].decode()
        cursor += FINGERPRINT_CHARS
        if expect and stored != expect:
            raise ValueError(
                f"{path} does not match the census data in this package. The index "
                f"stores name positions, so it only works with the lists it was "
                f"built from -- reinstall the package, or pass blocklist_index='' to "
                f"run without the famous-name check."
            )

        offsets, ids = {}, {}
        for gender in GENDERS:
            n_offsets, n_ids = struct.unpack_from('<II', data, cursor)
            cursor += 8
            offsets[gender] = array.array('I')
            offsets[gender].frombytes(data[cursor:cursor + n_offsets * 4])
            cursor += n_offsets * 4
            ids[gender] = array.array('I')
            ids[gender].frombytes(data[cursor:cursor + n_ids * 4])
            cursor += n_ids * 4
        return cls(offsets, ids, stored)
