#!/usr/bin/env python3
"""Synthetic name source: US Census and Ontario name lists, drawn at random.

Names are composed at request time from ``data/census_data/``: one first name from the
pool matching the target gender, one surname. That reaches 12,018 x 162,251 =
1,949,932,518 distinct full names without storing any of them.

Each pool holds a first name under exactly one gender. Where a name appears in both
census files, the higher count wins and ties go to male. ``GenderDetector`` is built
from these same pools, so every name this pool can emit is one the detector resolves.
"""

import json
import os
import random
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# A Census aggregate row rather than a real surname, so it must never be emitted.
_CENSUS_AGGREGATE = 'All Other Names'

_FIRST_NAME_FILES = (('male', 'male_first_names.json'),
                     ('female', 'female_first_names.json'))


def new_seed() -> int:
    """Return a seed derived from the current time, mixed with OS entropy.

    The timestamp is what makes a run identifiable: the seed is printable, roughly
    sortable, and tells you when the run happened. The ``os.urandom`` term is what
    makes it safe. ``time.time_ns()`` alone is not sufficient -- 20,000 back-to-back
    calls yield only ~781 distinct values, and processes launched together can read
    the same nanosecond. Seeding 40 simultaneous processes from ``int(time.time())``
    produced one seed and 40 identical name streams; XOR-ing in 8 random bytes gave
    40 distinct seeds.
    """
    return time.time_ns() ^ int.from_bytes(os.urandom(8), 'big')


def _load_first_names(census_dir: Path) -> Dict[str, List[str]]:
    """Return {gender: [names]}, assigning each name the gender with the higher count."""
    counts: Dict[str, Dict[str, float]] = {}
    for gender, filename in _FIRST_NAME_FILES:
        path = census_dir / filename
        if not path.exists():
            raise FileNotFoundError(f"census file not found: {path}")
        for entry in json.loads(path.read_text()):
            name = ' '.join(entry['name'].split()).upper()
            if name:
                counts.setdefault(name, {'male': 0.0, 'female': 0.0})
                counts[name][gender] += float(entry.get('count', 0.0))

    pools: Dict[str, List[str]] = {'male': [], 'female': []}
    for name, score in sorted(counts.items()):
        gender = 'female' if score['female'] > score['male'] else 'male'
        pools[gender].append(name.title())
    return pools


def _load_surnames(census_dir: Path) -> List[str]:
    path = census_dir / 'surnames.json'
    if not path.exists():
        raise FileNotFoundError(f"census file not found: {path}")
    return [entry['name'] for entry in json.loads(path.read_text())
            if entry['name'] != _CENSUS_AGGREGATE]


class NamePool:
    """Draws synthetic names from the census lists using its own seeded generator.

    The generator is private to the instance rather than the ``random`` module's
    global one, so the handler neither disturbs nor depends on the caller's random
    state. Pass ``seed`` to reproduce a previous run; omit it to seed from the clock.
    """

    def __init__(self, census_data_dir: str, seed: Optional[int] = None):
        census_dir = Path(census_data_dir)
        self.first_names = _load_first_names(census_dir)
        self.surnames = _load_surnames(census_dir)
        self.seed = new_seed() if seed is None else seed
        self._random = random.Random(self.seed)

    def coin_flip(self) -> str:
        """Pick a gender, for entities whose gender could not be determined."""
        return self._random.choice(('male', 'female'))

    def full_name(self, gender: str) -> Tuple[str, str]:
        """Draw a (first name, surname) pair for `gender`.

        A pair is always drawn, even when the caller only needs one half, because
        within-document coreference may later need the other: a document that says
        "Alice" and then "Alice Chen" must get the same surname both times. Returning
        the halves separately means no caller has to re-split a joined name, which
        would truncate the six census first names that contain a space.
        """
        return (self._random.choice(self.first_names[gender]),
                self._random.choice(self.surnames))
