#!/usr/bin/env python3
"""Synthetic name source: US Census and Ontario name lists, paired at request time.

A name is composed per request from ``data/census_data/``: one first name from the
pool matching the target gender, one surname. That reaches 12,018 x 162,251 =
1,949,932,518 pairs, of which 4,631,292 are blocked as famous names, leaving
**1,945,301,226** reachable -- without storing any of them.

Pairing two names from lists already in memory takes ~0.9us. This approach
stays flat at ~126 MB RAM.

Pre-computing only pays off when looking the answer up is cheaper than working it
out. Here, working it out is the cheap part.
"""

import os
import random
import time
from pathlib import Path
from typing import Optional, Tuple

from .blocklist import DEFAULT_INDEX, Blocklist, fingerprint
from .census_data import GENDERS, load_first_names, load_surnames


def new_seed() -> int:
    """Return a seed derived from the current time, mixed with OS entropy.

    The timestamp makes a run identifiable; the entropy makes it safe. A clock alone
    is not enough -- 40 processes started together from ``int(time.time())`` all got
    the same seed and produced identical names.
    """
    return time.time_ns() ^ int.from_bytes(os.urandom(8), 'big')


class NamePool:
    """Draws synthetic names from the census lists, skipping blocked pairs.

    Each pool builds its own random number generator instead of using the one the
    ``random`` module shares with the whole program. That keeps the two sides from
    interfering: drawing names cannot change the numbers the caller's own ``random``
    calls return, and the caller calling ``random.seed()`` cannot change which names
    come out. Without that isolation, ``seed`` would not reliably reproduce a run.
    """

    def __init__(self, census_data_dir: str, seed: Optional[int] = None,
                 blocklist_index: Optional[str] = None):
        """Load the name lists and the blocklist index.

        Pass `seed` to get the same names a previous run produced. Leave it out and
        the pool picks its own from the clock, so every run differs; either way
        `self.seed` reports which one is in use.

        `blocklist_index` defaults to the index shipped in `data/`. Pass '' to run
        without blocking, which lets famous names through -- measured at 0.24% of
        draws, roughly 1 in 417.
        """
        self.first_names = load_first_names(census_data_dir)
        self.surnames = load_surnames(census_data_dir)
        self.seed = new_seed() if seed is None else seed
        self._random = random.Random(self.seed)
        self.blocklist = self._load_blocklist(blocklist_index)

    def _load_blocklist(self, index_path: Optional[str]) -> Blocklist:
        if index_path == '':
            return Blocklist.empty()
        path = Path(index_path or DEFAULT_INDEX)
        if not path.exists():
            raise FileNotFoundError(
                f"blocklist index not found: {path}. It ships with the package, so "
                f"this install is incomplete -- reinstall it, or pass "
                f"blocklist_index='' to run without the famous-name check."
            )
        return Blocklist.load(str(path),
                              fingerprint(self.first_names, self.surnames))

    @property
    def size(self) -> int:
        """Distinct full names reachable, after blocking."""
        combinations = (sum(len(n) for n in self.first_names.values())
                        * len(self.surnames))
        return combinations - self.blocklist.size

    def coin_flip(self) -> str:
        """Pick a gender, for entities whose gender could not be determined."""
        return self._random.choice(GENDERS)

    def full_name(self, gender: str) -> Tuple[str, str]:
        """Draw a (first name, surname) pair for `gender`, redrawing if blocked.

        Drawing by position rather than with `random.choice` is what makes the
        blocklist check possible -- it is keyed on positions, not strings. Only 0.24%
        of pairs are blocked, so the loop almost never runs twice.

        The halves are returned separately so callers never re-split a joined name,
        which would truncate the six census first names containing a space.
        """
        firsts = self.first_names[gender]
        while True:
            first_id = self._random.randrange(len(firsts))
            surname_id = self._random.randrange(len(self.surnames))
            if not self.blocklist.blocks(gender, first_id, surname_id):
                return firsts[first_id], self.surnames[surname_id]
