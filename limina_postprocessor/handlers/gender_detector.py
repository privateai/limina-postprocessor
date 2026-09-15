#!/usr/bin/env python3
"""Gender detection service with three-tier fallback strategy."""

import json
from pathlib import Path
from typing import Optional, Dict
import requests


class GenderDetector:
    """Three-tier gender detection with caching."""

    def __init__(
        self,
        census_data_dir: str = "dataset/census_data",
        enable_api: bool = False,
        api_cache_file: str = ".gender_cache.json"
    ):
        """Initialize gender detector."""
        self.enable_api = enable_api
        self.api_cache_file = Path(api_cache_file)

        # Tier 1: Title patterns (gender-specific only, neutral titles handled by census data)
        self.male_titles = {'Mr.', 'Mr', 'Jr.', 'Jr', 'III', 'IV', 'Sr.', 'Sr'}
        self.female_titles = {'Ms.', 'Ms', 'Mrs.', 'Mrs', 'Miss', 'Mss.', 'Mss'}
        # Gender-neutral titles (for extraction but not gender detection)
        self.neutral_titles = {'Dr.', 'Dr', 'Prof.', 'Prof'}

        # Tier 2: Load census data into memory
        self.census_lookup = self._load_census_data(census_data_dir)

        # Tier 3: API cache
        self.api_cache = self._load_api_cache()

    def _load_name_counts(self, filepath: Path, gender: str) -> Dict[str, Dict[str, int]]:
        """Load name counts from JSON file."""
        if not filepath.exists():
            return {}

        with open(filepath, 'r') as f:
            names = json.load(f)

        name_counts = {}
        for entry in names:
            name = entry['name'].upper()
            if name not in name_counts:
                name_counts[name] = {}
            name_counts[name][gender] = entry.get('count', 0)

        return name_counts

    def _load_census_data(self, data_dir: str) -> Dict[str, str]:
        """Load census first name data into memory."""
        data_path = Path(data_dir)

        # Load male and female names with counts
        male_counts = self._load_name_counts(data_path / "male_first_names.json", 'male')
        female_counts = self._load_name_counts(data_path / "female_first_names.json", 'female')

        # Merge counts for names that appear in both genders
        all_names = set(male_counts.keys()) | set(female_counts.keys())
        name_counts = {}
        for name in all_names:
            name_counts[name] = {
                'male': male_counts.get(name, {}).get('male', 0),
                'female': female_counts.get(name, {}).get('female', 0),
            }

        # Build lookup by selecting gender with higher count
        return {
            name: 'male' if counts['male'] >= counts['female'] else 'female'
            for name, counts in name_counts.items()
        }

    def _load_api_cache(self) -> Dict[str, str]:
        """Load cached API results from disk."""
        if self.api_cache_file.exists():
            try:
                with open(self.api_cache_file, 'r') as f:
                    return json.load(f)
            except Exception:
                pass
        return {}

    def _save_api_cache(self):
        """Save API cache to disk."""
        try:
            with open(self.api_cache_file, 'w') as f:
                json.dump(self.api_cache, f, indent=2)
        except Exception:
            pass

    def detect_gender(self, name_text: str) -> Optional[str]:
        """Detect gender using three-tier strategy."""
        # Tier 1: Check for titles
        gender = self._detect_from_title(name_text)
        if gender:
            return gender

        # Extract first name for further lookups
        first_name = self._extract_first_name(name_text)
        if not first_name:
            return None

        # Tier 2: Census database lookup
        gender = self._lookup_census(first_name)
        if gender:
            return gender

        # Tier 3: API fallback (if enabled)
        if self.enable_api:
            gender = self._lookup_api(first_name)
            if gender:
                return gender

        return None

    def _detect_from_title(self, name_text: str) -> Optional[str]:
        """Tier 1: Detect gender from titles."""
        # Combine all titles and sort by length (longest first) to avoid "Mr" matching "Mrs"
        all_gendered_titles = [
            (title, 'male') for title in self.male_titles
        ] + [
            (title, 'female') for title in self.female_titles
        ]
        # Sort by length descending
        all_gendered_titles.sort(key=lambda x: len(x[0]), reverse=True)

        # Check if name starts with any title
        for title, gender in all_gendered_titles:
            if name_text.startswith(title + ' ') or name_text.startswith(title):
                return gender
        return None

    def _extract_first_name(self, name_text: str) -> Optional[str]:
        """Extract first name from full name text."""
        all_titles = self.male_titles | self.female_titles | self.neutral_titles
        # Sort by length (longest first) to avoid "Dr" matching before "Dr."
        sorted_titles = sorted(all_titles, key=len, reverse=True)
        cleaned = name_text.strip()

        for title in sorted_titles:
            if cleaned.startswith(title + ' ') or cleaned.startswith(title):
                cleaned = cleaned[len(title):].strip()
                break  # Only remove one title

        parts = cleaned.split()
        return parts[0].strip() if parts else None

    def _lookup_census(self, first_name: str) -> Optional[str]:
        """Tier 2: Look up gender in census database."""
        return self.census_lookup.get(first_name.upper())

    def _lookup_api(self, first_name: str) -> Optional[str]:
        """Tier 3: Look up gender via Genderize.io API with caching."""
        cache_key = first_name.lower()
        if cache_key in self.api_cache:
            return self.api_cache[cache_key]

        try:
            response = requests.get(
                "https://api.genderize.io",
                params={'name': first_name},
                timeout=5
            )
            response.raise_for_status()
            data = response.json()

            gender = data.get('gender')
            probability = data.get('probability', 0)

            # Only trust high-confidence results (>70%)
            if gender and probability >= 0.7:
                self.api_cache[cache_key] = gender
                self._save_api_cache()
                return gender

        except Exception:
            pass

        # Cache negative result
        self.api_cache[cache_key] = None
        self._save_api_cache()
        return None
