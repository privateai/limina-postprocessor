#!/usr/bin/env python3
"""
Post-processor for DEID synthetic output.

Replaces name entities in DEID output with synthetic names from a 1.2B name dictionary
while preserving:
- Global uniqueness (no cross-document name reuse)
- Within-document coreference (same name → same replacement)
- Gender matching (~70-75% accuracy)
- Honorifics (Dr., Mr., Mrs., Ms.)
- Index accuracy (character positions)

Usage:
    python3 post_process_deid.py --input deid_output.json --output processed.json
    python3 post_process_deid.py --input deid_output.json --dictionary custom_dict.parquet
"""

import argparse
import json
import sys
from pathlib import Path
from collections import Counter

# Import handlers
from .handlers import NameHandler

def print_section(title):
    """Print section header."""
    print(f"\n{'='*70}")
    print(title)
    print("="*70)

def check_file_exists(filepath, description):
    """Check if file exists, exit with error if not."""
    if not Path(filepath).exists():
        print(f"❌ Error: {description} not found: {filepath}")
        sys.exit(1)


class DEIDPostProcessor:
    """Post-processes DEID output with synthetic name replacement."""

    def __init__(self, name_dictionary_path=None, enable_names=True, enable_api_gender=False):
        """
        Initialize post-processor with name handler.

        Args:
            name_dictionary_path: Path to parquet name dictionary (required if enable_names=True)
            enable_names: Enable name replacement (default: True)
            enable_api_gender: Enable API fallback for gender detection (default: False, slower)
        """
        print_section("INITIALIZING DEID POST-PROCESSOR")

        # Initialize name handler
        self.handlers = []

        if enable_names:
            if not name_dictionary_path:
                raise ValueError("name_dictionary_path required when enable_names=True")
            self.handlers.append(NameHandler(name_dictionary_path, enable_api_gender=enable_api_gender))
        else:
            print("⚠️  Name replacement: DISABLED")

        if not self.handlers:
            raise ValueError("At least one handler must be enabled")

        # Statistics tracking
        self.stats = {
            'total_entities': 0,
            'entities_replaced': 0,
            'entity_types': Counter(),
            'replacements_by_handler': Counter(),
        }

    def _build_entity_positions(self, entities, full_text):
        """Build list of entities with their positions in text, sorted by position."""
        entities_with_positions = []
        for entity in entities:
            # Check for position in multiple formats (support different DEID outputs)
            pos = None

            # Format 1: Nested location object (Private AI DEID format)
            if 'location' in entity:
                location = entity['location']
                # Prefer stt_idx_processed (position in processed_text with placeholders)
                pos = location.get('stt_idx_processed') or location.get('stt_idx')

            # Format 2: Direct start_idx on entity
            if pos is None and 'start_idx' in entity:
                pos = entity['start_idx']

            # Format 3: Try to find by text
            if pos is None:
                entity_text = entity.get('processed_text') or entity.get('text', '')
                if entity_text:
                    pos = full_text.find(entity_text)

            if pos is not None and pos >= 0:
                entities_with_positions.append((pos, entity))

        return sorted(entities_with_positions, key=lambda x: x[0])

    def _update_entity_location(self, entity, original_pos, cumulative_offset, new_length):
        """Update entity location indices after replacement."""
        location = entity.get('location', {})
        if not location:
            location = {}
            entity['location'] = location

        new_stt_idx = original_pos + cumulative_offset
        new_end_idx = new_stt_idx + new_length

        location['stt_idx_processed'] = new_stt_idx
        location['end_idx_processed'] = new_end_idx

    def _apply_replacements(self, text, replacements_with_positions):
        """Apply replacements by position (safer than string.replace)."""
        # Sort by position (should already be sorted, but ensure it)
        replacements_with_positions = sorted(replacements_with_positions, key=lambda x: x[0])

        # Build new text by iterating through positions
        result = []
        last_pos = 0

        for pos, length, replacement in replacements_with_positions:
            # Add text before this entity
            result.append(text[last_pos:pos])
            # Add replacement
            result.append(replacement)
            # Update position
            last_pos = pos + length

        # Add remaining text after last entity
        result.append(text[last_pos:])

        return ''.join(result)

    def process_document(self, deid_output):
        """Process a single DEID document."""
        # Clear handler caches for new document
        for handler in self.handlers:
            handler.clear_cache()

        processed = deid_output.copy()

        # Check if entities exist
        if 'entities' not in processed or not processed['entities']:
            return processed

        # Get the full text (prefer 'text' for input, 'processed_text' for re-processing)
        full_text = processed.get('processed_text') or processed.get('text', '')
        entities_with_positions = self._build_entity_positions(processed['entities'], full_text)

        # Track cumulative offset from replacements
        cumulative_offset = 0
        replacements_with_positions = []

        for original_pos, entity in entities_with_positions:
            entity_type = entity.get('best_label', entity.get('entity_type', ''))
            old_processed_text = entity.get('processed_text') or entity.get('text', '')

            # Calculate the length of the original text to replace
            # Check multiple formats for positions
            original_length = None

            # Format 1: Nested location object
            if 'location' in entity:
                location = entity['location']
                start = location.get('stt_idx_processed') or location.get('stt_idx')
                end = location.get('end_idx_processed') or location.get('end_idx')
                if start is not None and end is not None:
                    original_length = end - start

            # Format 2: Direct indexes on entity
            if original_length is None and 'start_idx' in entity and 'end_idx' in entity:
                original_length = entity['end_idx'] - entity['start_idx']

            # Format 3: Use text length as fallback
            if original_length is None:
                original_length = len(old_processed_text)

            self.stats['total_entities'] += 1
            self.stats['entity_types'][entity_type] += 1

            # Find handler and get replacement
            handler = self._get_handler_for_entity(entity_type)

            if handler:
                replacement = handler.get_replacement(entity, context={})
                replacements_with_positions.append((original_pos, original_length, replacement))
                entity['processed_text'] = replacement

                self.stats['entities_replaced'] += 1
                self.stats['replacements_by_handler'][handler.__class__.__name__] += 1
            else:
                replacement = old_processed_text

            # Update entity location indices
            self._update_entity_location(entity, original_pos, cumulative_offset, len(replacement))

            # Update cumulative offset if text length changed
            if handler:
                cumulative_offset += (len(replacement) - original_length)

        # Update the full processed_text field using position-based replacement
        if replacements_with_positions:
            processed['processed_text'] = self._apply_replacements(full_text, replacements_with_positions)

        return processed

    def _get_handler_for_entity(self, entity_type):
        """Find the appropriate handler for an entity type."""
        for handler in self.handlers:
            if handler.can_handle(entity_type):
                return handler
        return None

    def process_file(self, input_path, output_path):
        """Process DEID output file."""
        print(f"\n📄 Processing: {input_path}")

        with open(input_path, 'r') as f:
            data = json.load(f)

        # Handle both single document and array of documents
        processed = [self.process_document(doc) for doc in data] if isinstance(data, list) else self.process_document(data)

        with open(output_path, 'w') as f:
            json.dump(processed, f, indent=2)

        print(f"✅ Saved to: {output_path}")

    def print_statistics(self):
        """Print processing statistics."""
        print_section("PROCESSING STATISTICS")

        total = self.stats['total_entities']
        replaced = self.stats['entities_replaced']

        print(f"\n📊 Overall:")
        print(f"   Total entities: {total:,} | Replaced: {replaced:,}", end='')
        if total > 0:
            print(f" | Rate: {replaced / total * 100:.1f}%")
        else:
            print()

        if self.stats['entity_types']:
            print(f"\n🏷️  Entity Types:")
            for entity_type, count in self.stats['entity_types'].most_common():
                print(f"   {entity_type}: {count:,}")

        if self.stats['replacements_by_handler']:
            print(f"\n🔧 Replacements by Handler:")
            for handler_name, count in self.stats['replacements_by_handler'].most_common():
                print(f"   {handler_name}: {count:,}")

        print(f"\n✅ Post-processing complete!")


def main():
    parser = argparse.ArgumentParser(description='Post-process DEID output with synthetic name replacement')
    parser.add_argument('--input', required=True, help='Path to DEID JSON output file')
    parser.add_argument('--output', help='Path to save processed output (default: input_processed.json)')
    parser.add_argument('--dictionary', default='limina_postprocessor/data/name_dictionary_1b_filtered.parquet',
                        help='Path to name dictionary (default: %(default)s)')
    parser.add_argument('--no-names', action='store_true', help='Disable name replacement')
    parser.add_argument('--enable-api-gender', action='store_true',
                        help='Enable API fallback for gender detection (slower)')
    args = parser.parse_args()

    # Default output path
    if not args.output:
        input_path = Path(args.input)
        args.output = str(input_path.parent / f"{input_path.stem}_processed.json")

    # Validate files
    check_file_exists(args.input, "Input file")
    if not args.no_names:
        if not Path(args.dictionary).exists():
            print(f"❌ Error: Dictionary not found: {args.dictionary}")
            print("   (Use --no-names to disable name replacement)")
            sys.exit(1)

    print_section("DEID POST-PROCESSOR")

    # Initialize processor
    processor = DEIDPostProcessor(
        name_dictionary_path=args.dictionary if not args.no_names else None,
        enable_names=not args.no_names,
        enable_api_gender=args.enable_api_gender
    )

    # Process file
    processor.process_file(args.input, args.output)

    # Print statistics
    processor.print_statistics()


if __name__ == '__main__':
    main()
