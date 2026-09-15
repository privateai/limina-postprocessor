#!/usr/bin/env python3
"""
Batch process DEID JSON files from an input folder.

Usage:
    python3 sample_batch_process_files.py

Input: All .json files in 'sample_input/'
Output: Processed files in 'sample_output/'
"""

import json
import sys
from pathlib import Path
sys.path.insert(0, '.')
import limina_postprocessor

# Configure input/output folders
INPUT_FOLDER = Path("sample_input")
OUTPUT_FOLDER = Path("sample_output")

def main():
    """Process all DEID JSON files in the input folder."""

    # Create output folder if it doesn't exist
    OUTPUT_FOLDER.mkdir(exist_ok=True)

    # Get all JSON files from input folder
    json_files = sorted(INPUT_FOLDER.glob("*.json"))

    if not json_files:
        print(f"No JSON files found in '{INPUT_FOLDER}/'")
        print(f"Please add your DEID output files to process.")
        return

    print(f"Found {len(json_files)} file(s) to process")

    # Initialize processor once (efficient - dictionary loads once)
    processor = limina_postprocessor.DEIDPostProcessor(
        name_dictionary_path=limina_postprocessor.DEFAULT_DICTIONARY,
        enable_names=True
    )

    # Process each file
    processed_count = 0
    for input_file in json_files:
        try:
            # Load DEID JSON
            with open(input_file, 'r') as f:
                data = json.load(f)

            # Process document(s)
            if isinstance(data, list):
                # Array of documents - process each one
                processed_data = [processor.process_document(doc) for doc in data]
            else:
                # Single document
                processed_data = processor.process_document(data)

            # Save to output folder
            output_file = OUTPUT_FOLDER / input_file.name
            with open(output_file, 'w') as f:
                json.dump(processed_data, f, indent=2)

            print(f"✅ {input_file.name}")
            processed_count += 1

        except Exception as e:
            print(f"❌ {input_file.name} - Error: {e}")
            continue

    print(f"\n✅ Processed {processed_count}/{len(json_files)} files")
    print(f"📁 Results saved to '{OUTPUT_FOLDER}/'")

if __name__ == "__main__":
    main()
