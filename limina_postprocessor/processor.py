#!/usr/bin/env python3
"""Post-processor for DEID output: replaces name placeholders with synthetic names.

Preserves within-document coreference (one original name maps to one replacement),
gender, honorifics, and character offsets.

    python3 -m limina_postprocessor.processor --input deid_output.json
"""

import argparse
import json
import sys
from collections import Counter
from copy import deepcopy
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .handlers import NameHandler


class DEIDPostProcessor:
    """Replaces name entities across one or many DEID documents."""

    def __init__(self, census_data_dir: Optional[str] = None,
                 seed: Optional[int] = None):
        """Load the name data once; reuse the instance across documents.

        `seed` reproduces a previous run. When omitted the handler seeds itself from
        the clock, and `self.seed` reports what it chose.
        """
        self.name_handler = NameHandler(census_data_dir=census_data_dir, seed=seed)
        self.stats = {
            'total_entities': 0,
            'entities_replaced': 0,
            'entity_types': Counter(),
        }

    @property
    def seed(self) -> int:
        """The seed this run's names were drawn from. Pass it back to replay the run."""
        return self.name_handler.seed

    def process_document(self, deid_output: Dict) -> Dict:
        """Return a copy of one DEID document with name entities replaced.

        Raises TypeError if handed a list. Previously a list fell through the
        `'entities' not in deid_output` guard -- which tests list *elements*, not keys
        -- and was returned with no replacements made at all. Silently emitting
        un-replaced names is the worst possible failure for this tool, so it is now
        loud. Use `process_file` or `run_postprocessor` for a list of documents.
        """
        if not isinstance(deid_output, dict):
            raise TypeError(
                f"process_document expects a single document dict, got "
                f"{type(deid_output).__name__}. For a list of documents use "
                f"process_file() or run_postprocessor()."
            )

        self.name_handler.clear_cache()
        # Deep copy, not .copy(): the entity dicts are mutated below (processed_text
        # and location are rewritten). A shallow copy shares them with the caller, so
        # the input document was being modified in place and processing it twice gave
        # different results the second time.
        processed = deepcopy(deid_output)

        entities = processed.get('entities')
        if not entities:
            return processed

        # 'processed_text' when re-processing an already-substituted document,
        # 'text' on first pass.
        full_text = processed.get('processed_text') or processed.get('text', '')

        spans = sorted(
            (span for span in (self._locate(e, full_text) for e in entities) if span),
            key=lambda span: span[0],
        )

        replacements: List[Tuple[int, int, str]] = []
        offset = 0

        for start, length, entity in spans:
            entity_type = entity.get('best_label', entity.get('entity_type', ''))
            self.stats['total_entities'] += 1
            self.stats['entity_types'][entity_type] += 1

            if self.name_handler.can_handle(entity_type):
                replacement = self.name_handler.get_replacement(entity)
                replacements.append((start, length, replacement))
                entity['processed_text'] = replacement
                self.stats['entities_replaced'] += 1
                shift = len(replacement) - length
            else:
                replacement = entity.get('processed_text') or entity.get('text', '')
                shift = 0

            # Positioned using the offset accumulated by *earlier* entities, then the
            # offset grows for the ones that follow.
            self._set_location(entity, start + offset, len(replacement))
            offset += shift

        if replacements:
            processed['processed_text'] = self._apply(full_text, replacements)

        return processed

    def process_file(self, input_path: str, output_path: str):
        """Read a DEID JSON file (one document or a list) and write the result."""
        with open(input_path) as handle:
            data = json.load(handle)

        processed = ([self.process_document(doc) for doc in data]
                     if isinstance(data, list) else self.process_document(data))

        with open(output_path, 'w') as handle:
            json.dump(processed, handle, indent=2)

    def print_statistics(self):
        """Print what was seen and replaced."""
        total = self.stats['total_entities']
        replaced = self.stats['entities_replaced']
        rate = f" ({replaced / total * 100:.1f}%)" if total else ""

        print(f"\nEntities: {total:,} seen, {replaced:,} replaced{rate}")
        print(f"Seed:     {self.seed}")
        if self.stats['entity_types']:
            print("\nBy type:")
            for entity_type, count in self.stats['entity_types'].most_common():
                print(f"  {entity_type}: {count:,}")

    # ------------------------------------------------------------------ internals

    @staticmethod
    def _locate(entity: Dict, full_text: str) -> Optional[Tuple[int, int, Dict]]:
        """Return (start, length, entity), or None if the entity cannot be placed.

        Three position formats are accepted because DEID output varies: a nested
        `location` object, flat `start_idx`/`end_idx`, or neither -- in which case the
        entity's text is searched for in the document.
        """
        location = entity.get('location') or {}
        start = location.get('stt_idx_processed') or location.get('stt_idx')
        end = location.get('end_idx_processed') or location.get('end_idx')

        if start is None:
            start, end = entity.get('start_idx'), entity.get('end_idx')

        text = entity.get('processed_text') or entity.get('text', '')
        if start is None:
            if not text:
                return None
            start, end = full_text.find(text), None

        if start is None or start < 0:
            return None

        length = end - start if end is not None else len(text)
        return start, length, entity

    @staticmethod
    def _set_location(entity: Dict, start: int, length: int):
        """Write back the post-replacement character offsets."""
        location = entity.setdefault('location', {})
        location['stt_idx_processed'] = start
        location['end_idx_processed'] = start + length

    @staticmethod
    def _apply(text: str, replacements: List[Tuple[int, int, str]]) -> str:
        """Splice replacements into `text` by position, lowest first."""
        pieces = []
        cursor = 0
        for start, length, replacement in replacements:
            pieces.append(text[cursor:start])
            pieces.append(replacement)
            cursor = start + length
        pieces.append(text[cursor:])
        return ''.join(pieces)


def main():
    parser = argparse.ArgumentParser(
        description='Replace name entities in DEID output with synthetic names.')
    parser.add_argument('--input', required=True, help='DEID JSON output file')
    parser.add_argument('--output', help='Where to write the result '
                                         '(default: <input>_processed.json)')
    parser.add_argument('--census-data',
                        help='Override the packaged census data directory')
    parser.add_argument('--seed', type=int,
                        help='Reproduce a previous run (default: seed from the clock)')
    args = parser.parse_args()

    input_path = Path(args.input)
    if not input_path.exists():
        print(f"Input file not found: {input_path}", file=sys.stderr)
        return 1

    default_output = input_path.parent / f"{input_path.stem}_processed.json"
    output_path = args.output or str(default_output)

    processor = DEIDPostProcessor(census_data_dir=args.census_data, seed=args.seed)
    processor.process_file(str(input_path), output_path)
    processor.print_statistics()
    print(f"\nWrote {output_path}")
    return 0


if __name__ == '__main__':
    sys.exit(main())
