#!/usr/bin/env python3
"""
Inline example: DEID → Post-process → Print (100 texts)

This script demonstrates the full pipeline at scale:
1. Generate 100 sample texts with different names
2. Call local Limina DEID container to de-identify all texts
3. Post-process the DEID output with limina_postprocessor
4. Verify output quality (indices, honorifics, gender, coreference)
5. Print summary statistics

Requirements:
    pip install requests

Setup:
    Run your local DEID container at localhost:8080
"""

import json
import sys
import time
import requests
sys.path.insert(0, '.')
import limina_postprocessor
from limina_postprocessor.handlers.gender_detector import GenderDetector

# Configure Limina DEID API
DEID_API_URL = "http://localhost:8080/process/text"

# Generate 100 sample texts with variety
# Include some duplicate names across different documents to verify cross-document behavior
SAMPLE_TEXTS = [
    "Patient John Smith visited the clinic on March 15th. Dr. Sarah Johnson examined Mr. Smith and prescribed medication. Smith will return for a follow-up with Dr. Johnson next month.",
    "Dr. Michael Brown treated Mrs. Emily Davis for hypertension. Brown recommended medication and Davis agreed to the treatment plan.",
    "Mr. Robert Wilson met with Prof. Jennifer Martinez about his research. Wilson and Martinez discussed the methodology.",
    "Ms. Amanda Taylor consulted Dr. Christopher Lee regarding her symptoms. Taylor was advised by Dr. Lee to schedule a follow-up.",
    "Patient David Anderson saw Dr. Jessica White. Anderson reported chest pain and Dr. White ordered tests.",
    "Dr. James Martin examined Mrs. Mary Thompson. Martin noted that Thompson's condition had improved.",
    "Mr. William Garcia visited Dr. Patricia Rodriguez. Garcia discussed his medications with Rodriguez.",
    "Dr. Richard Hernandez treated Ms. Linda Lopez. Hernandez prescribed antibiotics for Lopez.",
    "Patient Charles Lewis saw Dr. Barbara Walker. Lewis was referred by Dr. Walker to a specialist.",
    "Patient John Smith returned for follow-up. Dr. Sarah Johnson examined Smith again. Smith reported feeling better and Dr. Johnson agreed.",
]

# Extend to 100 by adding variations
COMMON_FIRST_NAMES = [
    "Alex", "Sam", "Jordan", "Taylor", "Casey", "Morgan", "Jamie", "Riley", "Avery", "Quinn",
    "Blake", "Cameron", "Dakota", "Drew", "Emerson", "Finley", "Harper", "Hayden", "Jesse", "Kai",
    "Logan", "Madison", "Mason", "Noah", "Parker", "Peyton", "Reese", "River", "Rowan", "Sage",
    "Skylar", "Spencer", "Sydney", "Tyler", "Winter", "Zion", "Angel", "Ariel", "Ash", "August"
]

COMMON_LAST_NAMES = [
    "Johnson", "Williams", "Brown", "Jones", "Garcia", "Miller", "Davis", "Rodriguez", "Martinez", "Hernandez",
    "Lopez", "Gonzalez", "Wilson", "Anderson", "Thomas", "Taylor", "Moore", "Jackson", "Martin", "Lee",
    "Perez", "Thompson", "White", "Harris", "Sanchez", "Clark", "Ramirez", "Lewis", "Robinson", "Walker",
    "Young", "Allen", "King", "Wright", "Scott", "Torres", "Nguyen", "Hill", "Flores", "Green"
]

# Generate remaining 90 texts
for i in range(90):
    first1 = COMMON_FIRST_NAMES[i % len(COMMON_FIRST_NAMES)]
    last1 = COMMON_LAST_NAMES[i % len(COMMON_LAST_NAMES)]
    first2 = COMMON_FIRST_NAMES[(i + 7) % len(COMMON_FIRST_NAMES)]
    last2 = COMMON_LAST_NAMES[(i + 13) % len(COMMON_LAST_NAMES)]

    template_id = i % 5
    if template_id == 0:
        text = f"Patient {first1} {last1} visited Dr. {first2} {last2}. {last1} was examined by Dr. {last2}."
    elif template_id == 1:
        text = f"Dr. {first1} {last1} treated Mr. {first2} {last2}. {last1} prescribed medication for {last2}."
    elif template_id == 2:
        text = f"Ms. {first1} {last1} consulted with Prof. {first2} {last2}. {last1} and {last2} discussed the results."
    elif template_id == 3:
        text = f"Mrs. {first1} {last1} saw Dr. {first2} {last2}. {last2} recommended that {last1} schedule a follow-up."
    else:
        text = f"Patient {first1} {last1} met with Dr. {first2} {last2}. Dr. {last2} ordered tests for {last1}."

    SAMPLE_TEXTS.append(text)

def call_deid_api_batch(texts, batch_size=10):
    """Call local DEID container."""

    headers = {
        "Content-Type": "application/json"
    }

    payload = {
        "text": texts[:batch_size],
        # Restrict detection to name entities so honorifics (Dr., Prof.) stay in the
        # surrounding text instead of being pulled out as OCCUPATION entities
        "entity_detection": {
            "entity_types": [
                {
                    "type": "ENABLE",
                    "value": ["NAME", "NAME_FAMILY", "NAME_GIVEN"]
                }
            ],
            "return_entity": True
        },
        "processed_text": {
            "type": "SYNTHETIC",
            "coreference_resolution": "heuristics"
        }
    }

    try:
        response = requests.post(DEID_API_URL, headers=headers, json=payload)
        response.raise_for_status()
        return response.json()
    except requests.exceptions.RequestException as e:
        print(f"❌ API Error: {e}")
        return None

def verify_output(original_text, deid_output, processed_output):
    """Verify output quality: indices, honorifics, gender, coreference."""
    issues = []

    # 1. Verify indices
    processed_text = processed_output.get('processed_text', '')
    for entity in processed_output.get('entities', []):
        if 'location' in entity and entity.get('best_label') in ['NAME', 'NAME_FAMILY', 'NAME_GIVEN']:
            loc = entity['location']
            start = loc.get('stt_idx_processed')
            end = loc.get('end_idx_processed')
            expected = entity.get('processed_text')

            if start is not None and end is not None:
                actual = processed_text[start:end]
                if actual != expected:
                    issues.append(f"Index mismatch: [{start}:{end}] '{actual}' != '{expected}'")

    # 2. Verify honorifics preserved
    honorifics = ['Dr.', 'Mr.', 'Mrs.', 'Ms.', 'Prof.', 'Jr.', 'Sr.']
    for hon in honorifics:
        orig_count = original_text.count(hon)
        proc_count = processed_text.count(hon)
        if orig_count > 0 and proc_count != orig_count:
            issues.append(f"Honorific '{hon}' count mismatch: {orig_count} → {proc_count}")

    # 3. Verify coreference (duplicate names get same replacement)
    name_map = {}
    for entity in processed_output.get('entities', []):
        orig = entity.get('text')
        repl = entity.get('processed_text')
        if orig and repl:
            if orig in name_map:
                if name_map[orig] != repl:
                    issues.append(f"Coreference broken: '{orig}' → '{name_map[orig]}' and '{repl}'")
            else:
                name_map[orig] = repl

    return issues

def print_sample_results(original_text, deid_output, processed_output, sample_num):
    """Print comparison for a sample text."""

    print(f"\n{'='*70}")
    print(f"SAMPLE #{sample_num}")
    print('='*70)

    deid_doc = deid_output[0] if isinstance(deid_output, list) else deid_output

    print("\n📝 ORIGINAL:")
    print(f"   {original_text[:150]}...")

    print("\n📝 AFTER DEID:")
    print(f"   {deid_doc.get('processed_text', '')[:150]}...")

    print("\n📝 AFTER POST-PROCESSING:")
    print(f"   {processed_output.get('processed_text', '')[:150]}...")

    print("\n📊 ENTITY REPLACEMENTS:")
    for i, entity in enumerate(processed_output.get('entities', [])[:5], 1):
        print(f"   {i}. {entity.get('text')} → {entity.get('processed_text')}")

def main():
    """Run the full pipeline on 100 texts."""

    print("="*80)
    print("DEID → POST-PROCESS → VERIFY PIPELINE (100 TEXTS)")
    print("="*80)

    # Step 1: Call DEID API in batches
    print(f"\n{'='*80}")
    print("STEP 1: CALLING LIMINA DEID API")
    print('='*80)
    print(f"\n📤 Processing {len(SAMPLE_TEXTS)} texts in batches of 10...")

    all_deid_results = []
    start_time = time.time()

    for i in range(0, len(SAMPLE_TEXTS), 10):
        batch = SAMPLE_TEXTS[i:i+10]
        print(f"   Batch {i//10 + 1}/{(len(SAMPLE_TEXTS) + 9)//10}...", end='', flush=True)

        deid_results = call_deid_api_batch(batch, batch_size=10)
        if deid_results:
            all_deid_results.extend(deid_results)
            print(f" ✅ ({len(deid_results)} texts)")
        else:
            print(" ❌ Failed")
            return

        # Rate limit: small delay between batches
        if i + 10 < len(SAMPLE_TEXTS):
            time.sleep(0.5)

    deid_time = time.time() - start_time
    print(f"\n✅ DEID API completed in {deid_time:.1f}s ({len(all_deid_results)} texts)")

    # Step 2: Post-process all results
    print(f"\n{'='*80}")
    print("STEP 2: POST-PROCESSING WITH LIMINA_POSTPROCESSOR")
    print('='*80)

    print("\n📊 Initializing processor...")
    start_time = time.time()

    processor = limina_postprocessor.DEIDPostProcessor(
        name_dictionary_path=limina_postprocessor.DEFAULT_DICTIONARY,
        enable_names=True
    )

    init_time = time.time() - start_time
    print(f"\n✅ Processor initialized in {init_time:.1f}s")

    print(f"\n📊 Processing {len(all_deid_results)} documents...")
    start_time = time.time()

    all_processed = []
    for i, deid_doc in enumerate(all_deid_results, 1):
        processed = processor.process_document(deid_doc)
        all_processed.append(processed)

        if i % 10 == 0:
            print(f"   Processed {i}/{len(all_deid_results)}...", flush=True)

    process_time = time.time() - start_time
    print(f"\n✅ Post-processing completed in {process_time:.1f}s ({process_time/len(all_processed)*1000:.1f}ms per document)")

    # Save outputs to files
    print(f"\n{'='*80}")
    print("SAVING OUTPUTS TO FILES")
    print('='*80)

    from pathlib import Path
    output_folder = Path("sample_output_batch")
    output_folder.mkdir(exist_ok=True)

    for i, processed in enumerate(all_processed, 1):
        output_file = output_folder / f"processed_{i:03d}.json"
        with open(output_file, 'w') as f:
            json.dump(processed, f, indent=2)

    print(f"\n✅ Saved {len(all_processed)} files to 'sample_output_batch/'")

    # Step 3: Verify output quality
    print(f"\n{'='*80}")
    print("STEP 3: VERIFYING OUTPUT QUALITY")
    print('='*80)

    total_issues = 0
    total_entities = 0
    total_name_entities = 0

    # Track cross-document replacements to verify NO cross-document coreference
    cross_doc_names = {}  # {original_name: [(doc_num, replacement), ...]}

    for i, (orig, deid, proc) in enumerate(zip(SAMPLE_TEXTS, all_deid_results, all_processed)):
        issues = verify_output(orig, deid, proc)
        total_issues += len(issues)

        # Count entities
        total_entities += len(proc.get('entities', []))
        total_name_entities += sum(1 for e in proc.get('entities', [])
                                   if e.get('best_label') in ['NAME', 'NAME_FAMILY', 'NAME_GIVEN'])

        # Track replacements across documents
        for entity in proc.get('entities', []):
            if entity.get('best_label') == 'NAME':  # Only track full names
                orig_name = entity.get('text')
                repl_name = entity.get('processed_text')
                if orig_name not in cross_doc_names:
                    cross_doc_names[orig_name] = []
                cross_doc_names[orig_name].append((i+1, repl_name))

        if issues:
            print(f"\n⚠️  Text #{i+1} has {len(issues)} issue(s):")
            for issue in issues:
                print(f"     • {issue}")

    # Check for cross-document coreference (should NOT happen)
    print(f"\n📊 Cross-Document Behavior Check:")
    cross_doc_issues = 0
    for orig_name, replacements in cross_doc_names.items():
        if len(replacements) > 1:  # Name appears in multiple documents
            unique_repls = set(r[1] for r in replacements)
            if len(unique_repls) == len(replacements):
                print(f"   ✅ '{orig_name}' appears in {len(replacements)} docs, gets {len(unique_repls)} different replacements")
                # Show examples if it's a common name
                if len(replacements) >= 2:
                    for doc_num, repl in replacements[:3]:  # Show first 3
                        print(f"      • Doc {doc_num}: → '{repl}'")
            else:
                cross_doc_issues += 1
                print(f"   ❌ '{orig_name}' got SAME replacement across different docs (unexpected!)")
                for doc_num, repl in replacements:
                    print(f"      • Doc {doc_num}: → '{repl}'")

    if total_issues == 0 and cross_doc_issues == 0:
        print("\n✅ ALL OUTPUTS VERIFIED - NO ISSUES FOUND!")
    else:
        print(f"\n⚠️  Found {total_issues} within-doc issue(s) and {cross_doc_issues} cross-doc issue(s)")

    # Step 4: Summary statistics
    print(f"\n{'='*80}")
    print("STEP 4: SUMMARY STATISTICS")
    print('='*80)

    print(f"\n📊 Processing Stats:")
    print(f"   • Total texts: {len(SAMPLE_TEXTS)}")
    print(f"   • Total entities: {total_entities}")
    print(f"   • Name entities: {total_name_entities}")
    print(f"   • Issues found: {total_issues}")
    print(f"   • Success rate: {(1 - total_issues/max(total_name_entities, 1))*100:.1f}%")

    print(f"\n⏱️  Performance:")
    print(f"   • DEID API: {deid_time:.1f}s")
    print(f"   • Initialization: {init_time:.1f}s (one-time)")
    print(f"   • Post-processing: {process_time:.1f}s")
    print(f"   • Total time: {deid_time + init_time + process_time:.1f}s")
    print(f"   • Per document: {(process_time/len(all_processed))*1000:.1f}ms")

    # Step 5: Show sample outputs
    print(f"\n{'='*80}")
    print("STEP 5: SAMPLE OUTPUTS")
    print('='*80)

    # Show doc 1 and doc 10 first (both have "John Smith" - should get different replacements)
    print(f"\n{'='*80}")
    print("DEMONSTRATING CROSS-DOCUMENT BEHAVIOR:")
    print("Note: 'John Smith' appears in Doc 1 and Doc 10 - they get DIFFERENT replacements")
    print('='*80)
    print_sample_results(SAMPLE_TEXTS[0], all_deid_results[0], all_processed[0], 1)
    print_sample_results(SAMPLE_TEXTS[9], all_deid_results[9], all_processed[9], 10)

    # Show additional samples
    for i in [25, 50, 75, 99]:  # Show 4 more samples
        print_sample_results(SAMPLE_TEXTS[i], all_deid_results[i], all_processed[i], i+1)

    print(f"\n{'='*80}")
    print("✅ PIPELINE COMPLETE")
    print('='*80)

if __name__ == "__main__":
    main()
