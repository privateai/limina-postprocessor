#!/usr/bin/env python3
"""Loading the census name lists.

Separate from the modules that use them so that `blocklist` and `name_pool` can both
read the lists without importing each other.
"""

import json
from pathlib import Path
from typing import Dict, List

GENDERS = ('male', 'female')

# A Census aggregate row rather than a real surname, so it must never be emitted.
CENSUS_AGGREGATE = 'All Other Names'

FIRST_NAME_FILES = (('male', 'male_first_names.json'),
                    ('female', 'female_first_names.json'))

#: Census data shipped with the package.
DEFAULT_CENSUS_DATA = str(Path(__file__).parent / 'data' / 'census_data')


def load_first_names(census_data_dir: str) -> Dict[str, List[str]]:
    """Return {gender: [first names]}, each name assigned exactly one gender.

    Where a name appears in both census files the higher count wins, and ties go to
    male.

    One gender per name does two things. It lets `GenderDetector` resolve every name
    the pool can emit, and it keeps full names unique -- a unisex name sitting in both
    pools could pair with the same surname twice.
    """
    census_dir = Path(census_data_dir)

    counts: Dict[str, Dict[str, float]] = {}
    for gender, filename in FIRST_NAME_FILES:
        for entry in _read_json(census_dir / filename):
            name = ' '.join(entry['name'].split()).upper()
            if name:
                counts.setdefault(name, {'male': 0.0, 'female': 0.0})
                counts[name][gender] += float(entry.get('count', 0.0))

    first_names: Dict[str, List[str]] = {gender: [] for gender in GENDERS}
    for name, score in sorted(counts.items()):
        gender = 'female' if score['female'] > score['male'] else 'male'
        first_names[gender].append(name.title())
    return first_names


def load_surnames(census_data_dir: str) -> List[str]:
    """Return the usable surnames, dropping the Census aggregate row."""
    return [entry['name']
            for entry in _read_json(Path(census_data_dir) / 'surnames.json')
            if entry['name'] != CENSUS_AGGREGATE]


def _read_json(path: Path) -> list:
    if not path.exists():
        raise FileNotFoundError(f"census file not found: {path}")
    return json.loads(path.read_text())
