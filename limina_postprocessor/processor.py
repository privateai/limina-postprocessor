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
from typing import Dict, List, Optional, Tuple, Union

from .handlers import BaseEntityHandler, NameHandler


class DEIDPostProcessor:
    """Replaces name entities across one or many DEID documents."""

    def __init__(self, census_data_dir: Optional[str] = None,
                 seed: Optional[int] = None,
                 blocklist_index: Optional[str] = None):
        """Load the name data once; reuse the instance across documents.

        Pass `seed` to get the same names a previous run produced. Leave it out and
        the pool picks its own from the clock; either way `self.seed` reports which
        one is in use.

        `blocklist_index` defaults to the index shipped in `data/`. Pass '' to skip
        the famous-name check, which lets ~0.24% of replacements land on a real
        public figure.
        """
        self.name_handler = NameHandler(census_data_dir=census_data_dir, seed=seed,
                                        blocklist_index=blocklist_index)
        # Entity types are claimed in order, first match wins. To replace another
        # type, write a BaseEntityHandler and add it here -- nothing else changes.
        self.handlers = [self.name_handler]
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

        Raises TypeError on a list -- use `process_file` or `run_postprocessor` for
        those. The check is loud rather than lenient because the alternative is
        returning a document with no replacements made, and silently emitting real
        names is this tool's worst failure.
        """
        if not isinstance(deid_output, dict):
            raise TypeError(
                f"process_document expects a single document dict, got "
                f"{type(deid_output).__name__}. For a list of documents use "
                f"process_file() or run_postprocessor()."
            )

        for handler in self.handlers:
            handler.clear_cache()
        # Deep copy, not .copy(): the entity dicts are mutated below (processed_text
        # and location are rewritten). A shallow copy would share them with the
        # caller, modifying the input in place and making a second pass over the same
        # document return something different.
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

            handler = self._handler_for(entity_type)
            if handler is not None:
                replacement = handler.get_replacement(entity)
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

    def process_file(self, input_path: str, output_path: str) -> None:
        """Read a DEID JSON file (one document or a list) and write the result."""
        with open(input_path) as handle:
            data = json.load(handle)

        processed = ([self.process_document(doc) for doc in data]
                     if isinstance(data, list) else self.process_document(data))

        with open(output_path, 'w') as handle:
            json.dump(processed, handle, indent=2)

    def print_statistics(self) -> None:
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

    def _handler_for(self, entity_type: str) -> Optional[BaseEntityHandler]:
        """The first handler that claims this entity type, or None."""
        for handler in self.handlers:
            if handler.can_handle(entity_type):
                return handler
        return None

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
    def _set_location(entity: Dict, start: int, length: int) -> None:
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


def run_postprocessor(deid_output: Union[Dict, List[Dict]],
                      census_data_dir: Optional[str] = None,
                      seed: Optional[int] = None) -> Union[Dict, List[Dict]]:
    """Replace name entities in one DEID document or a list of them.

    Builds a processor per call, so the census data is re-read each time. For many
    documents, construct a `DEIDPostProcessor` once and call `process_document`.

    Args:
        deid_output: DEID JSON output -- a single document or a list of them.
        census_data_dir: Override the packaged census data directory.
        seed: Reproduce a previous run. Omitted, names are seeded from the clock.

    Returns:
        The processed document, or a list of them, matching the input shape.
    """
    processor = DEIDPostProcessor(census_data_dir=census_data_dir, seed=seed)

    if isinstance(deid_output, list):
        return [processor.process_document(doc) for doc in deid_output]
    return processor.process_document(deid_output)


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

    processor = DEIDPostProcessor(census_data_dir=args.census_data,
                                  seed=args.seed)
    processor.process_file(str(input_path), output_path)
    processor.print_statistics()
    print(f"\nWrote {output_path}")
    return 0


if __name__ == '__main__':
    sys.exit(main())
