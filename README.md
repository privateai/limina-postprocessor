# Limina Post-Processor

**Post-processing pipeline for Limina DEID output with synthetic name replacement**

Replaces de-identified name placeholders with realistic synthetic names while preserving:
- **Global uniqueness** - No cross-document reuse, tracked per entity type
- **Within-document coreference** - Same name gets same replacement
- **Gender matching** - Male/female names matched (~90% on the sample corpus)
- **Leading honorifics** - Dr., Mr., Mrs., Ms., Prof. preserved
- **Index accuracy** - Character positions tracked correctly

> **Known limitation:** trailing suffixes (`Jr.`, `Sr.`, `III`, `IV`) are **dropped**, not
> preserved — `_split_title` only strips titles from the *front* of a name. See
> [Honorific Handling](#honorific-handling).

---

## Overview

This package processes de-identified text from Limina DEID by replacing name entity placeholders with synthetic names from a **1.2 billion name dictionary**.

**What it does:**
```
Input:  "Patient John Smith visited Dr. Sarah Johnson..."
Output: "Patient Andrew Davis visited Dr. Jessica Thompson..."
```

**Key Features:**
- **1.2B name dictionary** - US Census data + filtered famous names
- **Global uniqueness tracking** - Zero cross-document reuse per entity type
- **Pure PyArrow** - Memory-mapped row group reads (~50MB RAM, no DuckDB)
- **3-tier gender detection** - Titles → census → API fallback (off by default)
- **Entity-level tracking** - NAME, NAME_GIVEN, NAME_FAMILY tracked independently
- **Package-based** - Easy ETL integration with `pip install`

---

## Architecture

The post-processor uses a handler-based architecture focused on name entity replacement:

### 1. **Name Handler** (`limina_postprocessor/handlers/name_handler.py`)

Replaces name entities with synthetic names from a 1.2B name dictionary.

**Core Features:**

1. **Global Uniqueness Tracking:**
   - `used_names_global` set tracks every value handed out, across all documents
   - Random row group sampling from the 1.2B dictionary
   - Up to 100 probes per name (`PROBES_PER_ROW_GROUP` 25 × `MAX_ROW_GROUP_READS` 4)
   - **Result:** No cross-document reuse, until the relevant pool is exhausted
     (see [Pool Ceilings](#pool-ceilings))

2. **Entity-Level Tracking:**
   - Each entity type is tracked against the value it actually substitutes:
     - `NAME` / `NAME_MEDICAL_PROFESSIONAL`: full name, e.g. `"Andrew Davis"`
     - `NAME_GIVEN`: first name only, e.g. `"Andrew"`
     - `NAME_FAMILY`: last name only, e.g. `"Davis"`
   - Components are *not* blocked, so they recombine freely
   - **Example:** `"Andrew Davis"` used in doc 1 → `"Stella Davis"` still allowed in doc 2

3. **Within-Document Coreference:**
   - Per-document cache keyed on `(entity_type, cleaned_name)`: same original → same replacement
   - `last_name_to_full` / `first_name_to_full` let a partial mention reuse an
     already-assigned full name (`_smart_match`)
   - **Example:** `"John Smith"` → `"Champ Dannenmueller"`, then `"Smith"` → `"Dannenmueller"`
   - Both caches reset per document via `clear_cache()`; `used_names_global` does not

4. **Gender Detection (3-tier system):**
   - **Tier 1 - Title matching:** Mr., Mrs., Ms. (instant)
   - **Tier 2 - Census lookup:** 6,782 US first names
   - **Tier 3 - API fallback:** Optional Genderize.io (disabled by default)
   - **Measured:** 90.7% agreement on the 100-doc sample corpus (175/193 entities
     where gender was resolvable on both sides)
   - Unknown gender (20% of entities, mostly bare surnames) falls back to a 50/50
     coin flip, so it is never a hard failure — see
     [If Gender Cannot Be Determined](#if-gender-cannot-be-determined)

5. **Entity Type Handling:**
   - `NAME` → Full name ("Andrew Davis")
   - `NAME_GIVEN` → First name only ("Andrew")
   - `NAME_FAMILY` → Last name only ("Davis")
   - `NAME_MEDICAL_PROFESSIONAL` → Full name (treated identically to `NAME`)

6. **Honorific Handling:** see [Honorific Handling](#honorific-handling)

**Performance** (measured on the 1.2B / 3.8GB dictionary, single process):
- **Initialization:** ~0.05s (reads parquet metadata only, no data scan)
- **Per generated name:** `NAME` 2.78ms · `NAME_FAMILY` 1.50ms · `NAME_GIVEN` 0.92ms
- **Per document:** ~7.9ms on the sample corpus (~4 name entities each)
- **Memory:** ~50MB steady state for sampling — but `used_names_global` grows
  ~139 bytes per name retained (see [Scale Limits](#scale-limits))

### 2. **Gender Detector** (`limina_postprocessor/handlers/gender_detector.py`)

Detects gender from names using three-tier fallback strategy.

**Tier 1 - Title Matching (instant):**
- Male: Mr., Jr., Sr., III, IV
- Female: Ms., Mrs., Miss, Mss.
- Neutral: Dr., Prof. (no gender signal, passed to Tier 2)
- **Note:** with the recommended `entity_detection` payload, DEID leaves honorifics
  *outside* the entity span, so Tier 1 fired 0/402 times on the sample corpus. It
  only matters for upstream configs that include the title in the entity text.

**Tier 2 - Census Lookup (<0.01ms):**
- 6,782 first names from US Census data, loaded into a dict at init
- One winning gender per name, not a probability: the male/female counts are
  compared once in `_load_census_data` and only the winner is stored (ties → male)
- Measured coverage on the sample corpus: 57/58 distinct original first names
- US-centric; international first names are the main miss (see Tier 3)

**Tier 3 - API Fallback (100-500ms, optional):**
- Genderize.io API for uncommon/international names, accepted at ≥0.7 confidence
- Results (including negatives) cached in `.gender_cache.json`
- Disabled by default (enable with `enable_api_gender=True`); adds a network
  round-trip per uncached name, so it slows post-processing substantially

#### If Gender Cannot Be Determined

When every enabled tier misses, `detect_gender()` returns `None`. This is not an
error and never blocks replacement — `_generate_name` falls back to a 50/50 coin
flip and samples from that gender's pool:

```python
if gender in ('male', 'female'):
    target_gender = gender
else:
    target_gender = 'male' if random.random() < 0.5 else 'female'
```

Consequences:
- A name is **always** produced; uniqueness and coreference are unaffected
- Gender match becomes chance, so roughly half of these land on the wrong gender
- The choice is **not** recorded per-name, so the same original text can flip
  gender across documents (within a document the per-doc cache keeps it stable)

Measured unknown rate on the sample corpus (Tiers 1+2, API off) — **82/402 = 20%**:

| Entity type | Unknown | Why |
|-------------|---------|-----|
| `NAME` | 9/200 (4%) | genuinely unisex first names ("Drew", "Ash") |
| `NAME_FAMILY` | 73/202 (36%) | a bare surname has no first name to look up — inherently ungenderable |

`NAME_FAMILY` dominates the unknowns and is largely unfixable: the entity is just
"Flores", so there is nothing to gender. It also matters least, since the
replacement is a surname and carries no gender signal of its own.

**Accuracy:** 90.7% agreement measured on the 100-doc sample corpus with Tiers 1+2
only (175/193 entities where gender was resolvable on *both* the original and the
replacement). Corpus-specific — this sample uses common US names, so expect lower
on international data unless Tier 3 is enabled.

---

## Installation

### Prerequisites

```bash
# Python 3.8+
python3 --version

# Install package dependencies
pip install pyarrow requests
```

### Download the 1.2B Name Dictionary

The dictionary is pre-generated and stored in the repository using Git LFS (Large File Storage) due to its size (3.8GB).

**Option 1: Clone repo with Git LFS (Recommended)**
```bash
# Install Git LFS if not already installed
# macOS:
brew install git-lfs

# Ubuntu/Debian:
sudo apt install git-lfs

# Initialize Git LFS
git lfs install

# Clone the repository (automatically downloads LFS files)
git clone <repo-url> limina-postprocessor
cd limina-postprocessor

# Verify dictionary was downloaded
ls -lh limina_postprocessor/data/name_dictionary_1b_filtered.parquet
# Should show ~3.8GB
```

**Option 2: Pull LFS files in existing repo**
```bash
# If you already cloned the repo without Git LFS
cd limina-postprocessor

# Install Git LFS
git lfs install

# Pull the dictionary file
git lfs pull

# Verify
ls -lh limina_postprocessor/data/name_dictionary_1b_filtered.parquet
```

---

## Usage Examples

### Sample 1: Batch Folder Processing

Process multiple DEID JSON files from a folder.

**Script:** `sample_batch_process_files.py`

```python
#!/usr/bin/env python3
import json
import sys
from pathlib import Path
sys.path.insert(0, '.')
import limina_postprocessor

# Initialize processor once (reuse for all files)
processor = limina_postprocessor.DEIDPostProcessor(
    name_dictionary_path=limina_postprocessor.DEFAULT_DICTIONARY,
    enable_names=True
)

# Process all JSON files in input folder
input_folder = Path("sample_input")
output_folder = Path("sample_output")
output_folder.mkdir(exist_ok=True)

for input_file in input_folder.glob("*.json"):
    # Load DEID output
    with open(input_file, 'r') as f:
        deid_data = json.load(f)
    
    # Post-process
    processed_data = processor.process_document(deid_data[0])
    
    # Save result
    output_file = output_folder / input_file.name
    with open(output_file, 'w') as f:
        json.dump([processed_data], f, indent=2)
    
    print(f"✅ {input_file.name}")

print(f"\n✅ Processed {len(list(input_folder.glob('*.json')))} files")
```

**Run:**
```bash
# Place DEID JSON files in sample_input/
python3 sample_batch_process_files.py

# Results saved to sample_output/
```

---

### Sample 2: Inline DEID → Post-process → Verify

Full pipeline: Call local DEID container → post-process → verify quality.

**Script:** `sample_inline_deid_postprocess.py`

**Prerequisites:**
- Local Limina DEID container running at `http://localhost:8080`
- No API key needed

**What it does:**
```python
#!/usr/bin/env python3
import limina_postprocessor

# Step 1: Initialize processor once
processor = limina_postprocessor.DEIDPostProcessor(
    name_dictionary_path=limina_postprocessor.DEFAULT_DICTIONARY,
    enable_names=True
)

result = processor.process_document(deid_output)

# Step 3: Print results
print("ORIGINAL:")
print(f"  {text}\n")

print("AFTER DEID:")
print(f"  {deid_output['processed_text']}\n")

print("AFTER POST-PROCESSING:")
print(f"  {result['processed_text']}\n")

print("ENTITY REPLACEMENTS:")
for entity in result['entities']:
    print(f"  • {entity['text']} → {entity['processed_text']}")
```

**Run:**
```bash
python3 sample_inline_deid_postprocess.py
```

**Output** (actual run, doc 1 of the sample corpus):
```
ORIGINAL:
  Patient John Smith visited the clinic on March 15th. Dr. Sarah Johnson examined
  Mr. Smith and prescribed medication. Smith will return for a follow-up with
  Dr. Johnson next month.

AFTER POST-PROCESSING:
  Patient Champ Dannenmueller visited the clinic on March 15th. Dr. Cathleen
  Sciaraffa examined Mr. Dannenmueller and prescribed medication. Dannenmueller
  will return for a follow-up with Dr. Sciaraffa next month.

ENTITY REPLACEMENTS:
  NAME         'John Smith'    → 'Champ Dannenmueller'
  NAME         'Sarah Johnson' → 'Cathleen Sciaraffa'
  NAME_FAMILY  'Smith'         → 'Dannenmueller'     ← coreference
  NAME_FAMILY  'Smith'         → 'Dannenmueller'     ← coreference
  NAME_FAMILY  'Johnson'       → 'Sciaraffa'         ← coreference
```

Note `Dr.` and `Mr.` survive, `March 15th` is untouched (entity detection is
restricted to name types), and every later mention of `Smith` resolves to the
same surname assigned to the full name.

**Restricting entity detection.** The sample script asks DEID for name entities
only. Without this, DEID classifies `Dr.`/`Prof.` as separate `OCCUPATION`
entities, which this package has no handler for — they would be left as
`[OCCUPATION_1]` placeholders and the honorific would be lost:

```python
payload = {
    "text": texts,
    "entity_detection": {
        "entity_types": [{"type": "ENABLE", "value": ["NAME", "NAME_FAMILY", "NAME_GIVEN"]}],
        "return_entity": True
    },
    "processed_text": {"type": "SYNTHETIC", "coreference_resolution": "heuristics"}
}
```

---

## Technical Details

### PyArrow Integration

**Why pure PyArrow?**
- Memory-maps the 3.8GB parquet file; row groups are decoded only when sampled
- Memory usage: ~50MB (vs 192GB if loaded in-memory)
- One dependency instead of two — no SQL engine needed for a single-table lookup
- **~214× faster than previous design.** `LIMIT 1 OFFSET n` combined
  with `WHERE gender = ?` forced a scan of up to 554M rows per query, measured at
  **686ms median / 1.26s p95**. Row group sampling avoids the scan entirely: **2.78ms**.

**Sampling Strategy (Random Row Group):**

The dictionary has 9,659 row groups of ~124K rows each. A row group is the smallest
unit parquet can decode, so sampling works at that granularity:

```python
# Pick a random row group known to contain the target gender
row_group = random.choice(self.row_groups[gender])

# Decode only the column we substitute (~3ms, ~2MB)
table = self.parquet.read_row_group(row_group, columns=[column])

# Then probe random rows within it
check_name = table.column(column)[random.randrange(table.num_rows)].as_py()
```

**Gender routing via parquet statistics:**

At init, per-row-group `gender` statistics are read from metadata (no data scan) to
index which row groups hold which gender:
- 9,021 row groups are single-gender → read the name column alone, no filtering
- 638 row groups are mixed → also read `gender` and filter after the read

This replaces the old `GROUP BY gender` full scan, cutting init from ~0.56s to ~0.05s.

**Why random row groups?**
- Each draw lands in a different row group, so first names stay varied
  (200 consecutive draws yielded 199 distinct first names and 200 distinct full names)
- Only the substituted column is decoded, since `full_name` is stored as
  `"first_name last_name"` — a full name needs just that one column
- Retries are nearly free: 25 probes reuse the decoded row group before another read
- Combined with global uniqueness tracking for zero cross-document reuse

### Global Uniqueness Tracking

One `used_names_global` set holds every value ever handed out. It is **never cleared** —
`clear_cache()` resets only the per-document coreference caches.

What gets checked depends on the entity type, because that determines which string is
actually substituted into the text:

| Entity Type | Value tracked | Example |
|-------------|---------------|---------|
| `NAME`, `NAME_MEDICAL_PROFESSIONAL` | Full name | `"Andrew Davis"` |
| `NAME_GIVEN` | First name only | `"Andrew"` |
| `NAME_FAMILY` | Last name only | `"Davis"` |

```python
# _generate_name, simplified — see name_handler.py for the real probe loop
column = self.COLUMN_BY_TYPE.get(entity_type, 'full_name')

for _ in range(self.MAX_ROW_GROUP_READS):          # 4 row group decodes
    candidates = self._sample_row_group(target_gender, column)
    for _ in range(self.PROBES_PER_ROW_GROUP):     # 25 probes per decode
        check_name = candidates[random.randrange(len(candidates))].as_py()
        if check_name not in self.used_names_global:
            self.used_names_global.add(check_name)
            return self._match_capitalization(original_text, check_name)
        fallback = check_name

return self._match_capitalization(original_text, fallback)   # ⚠ see below
```

**Cross-document behavior:**

```
Doc 1:  "John Smith" (NAME) → "Elmore Whicker"      tracks "Elmore Whicker"

Doc 2:  "Elmore Whicker" (NAME)        ❌ blocked  — that exact full name is used
        "Sarah Whicker"  (NAME)        ✅ allowed  — different full name
        "Whicker"        (NAME_FAMILY) ✅ allowed  — the bare surname was never tracked
```

Components are deliberately *not* blocked: tracking `"Whicker"` when `"Elmore Whicker"`
is used would burn the 162K-surname pool at full-name rates.

**Verified** on 100 documents / 402 name entities: 400 distinct replacements,
**0 reused across documents**, 0 broken coreferences, 402/402 indices correct.

<a name="pool-ceilings"></a>
#### Pool Ceilings

Uniqueness holds only while the relevant pool has unused values left. The pools are
very different sizes:

| Entity Type | Distinct values available | Safe at 700M? |
|---|---|---|
| `NAME` / `NAME_MEDICAL_PROFESSIONAL` | ~1.2B | Yes |
| `NAME_FAMILY` | ~162K | **No** |
| `NAME_GIVEN` | ~7,500 (3,437 male / 4,018 female) | **No** |

⚠️ **When a pool is exhausted, uniqueness fails silently.** All 100 probes miss, and
the function returns `fallback` — a value that is already in `used_names_global` — and
the check is skipped. Measured with a depleted surname pool (147K pre-marked):

```
NAME_FAMILY, fresh pool:     1.50ms,   0% duplicates
NAME_FAMILY, depleted pool:  6.50ms,  64% duplicates   (128 of 200)
```

Throughput drops 4.3× *and* correctness breaks, with no warning logged. Only `NAME`
has enough headroom for a 700M-entity run.

<a name="scale-limits"></a>
#### Scale Limits

Two further constraints apply beyond the sample-corpus scale this package is verified at:

- **Memory.** `used_names_global` costs ~139 bytes per retained name (measured):
  10M names ≈ 1.4GB, 100M ≈ 14GB, **700M ≈ 97GB**. A single process needs a host
  with headroom above that or it will swap.
- **Parallelism breaks the guarantee.** Each worker process holds its own
  `used_names_global`, so two workers can independently emit the same name. There is
  currently no shared or partitioned uniqueness store. `full_name` values *are*
  globally unique in the parquet (verified over 1.5M sampled rows), so assigning each
  worker a disjoint subset of row groups would make cross-worker uniqueness hold by
  construction — this is not implemented.

<a name="honorific-handling"></a>
### Honorific Handling

`_split_title` splits a leading honorific off the name, replaces the remainder, then
re-attaches the honorific:

```
"Dr. Sarah Johnson"  →  ("Dr.", "Sarah Johnson")  →  "Dr. Cathleen Sciaraffa"
```

Two rules make this safe:

1. **A title must be followed by whitespace.** Without this, any name *beginning* with
   a title's letters gets mangled — `"Drew Lee"` became `"Dr"` + `"ew Lee"`, producing
   the fabricated output `"Dr Evelin Ramseur"`.
2. **Titles are tried longest-first** (`sorted_titles`, precomputed in `__init__`), so
   `Mrs.` wins over `Mr` and `Dr.` over `Dr`. The previous code iterated a `set`, making
   the result depend on arbitrary hash order.

Recognised: `Dr.` `Dr` `Mr.` `Mr` `Mrs.` `Mrs` `Ms.` `Ms` `Miss` `Mss.` `Mss` `Prof.`
`Prof` `Jr.` `Jr` `Sr.` `Sr` `III` `IV`

⚠️ **Trailing suffixes are dropped.** `_split_title` only inspects the front of the
string, so `Jr.`, `Sr.`, `III` and `IV` — which in real text appear *after* the name —
are silently lost:

```
"John Smith Jr."      →  "Hunter Molthan"        (Jr. dropped)
"Robert Downey III"    →  (III dropped)
"Dr. John Smith Jr."  →  "Dr. Hunter Molthan"    (Dr. kept, Jr. dropped)
```

`gender_detector` also classifies `Jr./Sr./III/IV` as male *prefix* titles, which is the
same wrong shape. Accepted for now; fixing it means matching suffixes at the end of the
string in both modules.

### Index Tracking

The processor maintains accurate character positions through cumulative offset tracking:

```python
cumulative_offset = 0

for entity in entities:
    # Get original position
    start = entity['location']['stt_idx_processed']
    end = entity['location']['end_idx_processed']
    original_length = end - start
    
    # Apply cumulative offset
    adjusted_start = start + cumulative_offset
    adjusted_end = adjusted_start + len(replacement)
    
    # Update cumulative offset
    cumulative_offset += (len(replacement) - original_length)
    
    # Update entity positions
    entity['location']['stt_idx_processed'] = adjusted_start
    entity['location']['end_idx_processed'] = adjusted_end
```

**Why this works:**
- Entities are processed in order (sorted by start index)
- Each replacement changes the text length
- Cumulative offset tracks the total shift
- All subsequent positions are adjusted accordingly

---

## Package Structure

```
limina_postprocessor/
├── __init__.py                    # Package exports: run_postprocessor,
│                                  #   DEIDPostProcessor, DEFAULT_DICTIONARY
├── processor.py                   # DEIDPostProcessor: entity positions, index
│                                  #   offsets, handler dispatch
├── data/
│   ├── name_dictionary_1b_filtered.parquet  # 1.2B names, 3.8GB, 9,659 row groups
│   └── census_data/
│       ├── male_first_names.json            # 3,437 names
│       ├── female_first_names.json          # 4,018 names
│       └── surnames.json                    # 162,254 surnames
└── handlers/
    ├── __init__.py                # Exports NameHandler
    ├── base_handler.py            # BaseEntityHandler: per-document cache + interface
    ├── name_handler.py            # Name entity replacement (row group sampling)
    └── gender_detector.py         # 3-tier gender detection

# Sample scripts
sample_batch_process_files.py      # Batch folder processing (sample_input/ → sample_output/)
sample_inline_deid_postprocess.py  # Full DEID pipeline + verification on 100 texts

SETUP_GUIDE.md                     # Environment setup
```

`NameHandler` is the only handler. Facility and address handlers were removed, as
were the dictionary-generation scripts — the parquet ships with the repo via Git LFS.

---

## API Reference

### DEIDPostProcessor

```python
processor = limina_postprocessor.DEIDPostProcessor(
    name_dictionary_path: str = None,   # Path to parquet; required when enable_names=True
    enable_names: bool = True,          # Enable name replacement
    enable_api_gender: bool = False     # Enable Genderize.io fallback (slow, rate-limited)
)
```

**Methods:**

```python
# Process a single DEID document. Clears per-document coreference caches on entry,
# so no manual cache management is needed between documents.
result = processor.process_document(deid_output: dict) -> dict

# Read a JSON file (single document or array) and write the processed result
processor.process_file(input_path: str, output_path: str) -> None

# Print counts of entities seen and replaced
processor.print_statistics() -> None
```

Reuse one processor across documents — it holds the memory-mapped dictionary and the
`used_names_global` set. Constructing a new one resets uniqueness tracking.

### run_postprocessor

Convenience wrapper that builds a processor per call. Fine for one-off use; use
`DEIDPostProcessor` directly for batches.

```python
limina_postprocessor.run_postprocessor(
    deid_output,                        # dict or list of dicts
    dictionary_path=None,               # defaults to DEFAULT_DICTIONARY
    enable_names=True,
    enable_api_gender=False
)
```

---

## Troubleshooting

### Git LFS Issues

**Problem:** Dictionary file is only a few KB (pointer file instead of actual data)  
**Solution:**
```bash
# Install Git LFS
git lfs install

# Pull the actual file
git lfs pull

# Verify size (should be ~3.8GB)
ls -lh limina_postprocessor/data/name_dictionary_1b_filtered.parquet
```

**Problem:** "This repository is over its data quota" error  
**Solution:**
- Contact repository admin to upgrade Git LFS quota

**Problem:** Slow download of dictionary file  
**Solution:**
- Git LFS can be slow for large files (3.8GB)
- Expected time: 5-15 minutes depending on connection

### Memory Issues

**Problem:** Out of memory during initialization  
**Solution:** 
- Initialization reads parquet metadata only and needs well under 1GB
- Steady-state usage for *sampling* is ~50MB: the file is memory-mapped and only one
  row group (~124K rows, ~2MB) is decoded per generated name
- If memory is still an issue, confirm `memory_map=True` is reaching
  `pq.ParquetFile` in `name_handler.py` — a non-mapped read pulls in more pages

**Problem:** Memory grows steadily over a long run  
**Cause:** Expected, not a leak. `used_names_global` retains every name handed out at
~139 bytes each and is never cleared — that is what makes cross-document uniqueness
work. Budget ~14GB per 100M entities. See [Scale Limits](#scale-limits).

### Duplicate Names Appearing Across Documents

**Problem:** The same replacement shows up in two documents  
**Causes, in order of likelihood:**
- **Pool exhausted.** `NAME_GIVEN` runs out at ~7,500 values and `NAME_FAMILY` at
  ~162K, after which `_generate_name` returns an already-used name with no warning.
  Check `len(handler.used_names_global)` against the ceilings in
  [Pool Ceilings](#pool-ceilings).
- **Multiple processes.** Each holds a separate `used_names_global`; uniqueness is
  per-process only. See [Scale Limits](#scale-limits).
- **Processor recreated.** Constructing a new `DEIDPostProcessor` (or calling
  `run_postprocessor` repeatedly) resets tracking. Reuse one instance.

### Slow Processing

**Problem:** Processing slower than ~3ms per generated name  
**Solution:**
- Use faster storage (gp3 → io2 → instance store NVMe); each name decodes a
  ~2MB row group off disk, so this path is I/O bound on a cold page cache
- Check that most row groups are single-gender (`len(handler.mixed_row_groups)`
  should be small); mixed row groups cost an extra column read plus a filter

### Low Gender Accuracy

**Problem:** Incorrect gender matching (<70%)  
**Solution:**
- Enable API fallback: `enable_api_gender=True`
- Check census data exists: `limina_postprocessor/data/census_data/`
- Note: API is slow (100-500ms per call) and rate-limited (1000/day free)
- The census list is US-centric (6,782 names); international first names often miss
  all three tiers, and unresolved gender falls back to a coin flip
- **Expected, not a bug:** ~36% of `NAME_FAMILY` entities are ungenderable because a
  bare surname ("Flores") has no first name to look up. Enabling the API will not
  help these. They also matter least — the replacement is a surname either way.
  See [If Gender Cannot Be Determined](#if-gender-cannot-be-determined)

### Mangled or Fabricated Titles

**Problem:** A replacement comes out as `"Dr Evelin Ramseur"` from an input with no title  
**Cause:** A regression in `_split_title`'s boundary check — a name beginning with a
title's letters (`Drew`, `Sriram`, `Missy`, `Jrue`, `IVy`) being split apart. The guard
is that a title only matches when whitespace follows it. Verify with:

```python
handler._split_title("Drew Lee")   # must be (None, 'Drew Lee')
```

**Problem:** `Jr.` / `Sr.` / `III` / `IV` missing from output  
**Cause:** Known limitation, not a bug — trailing suffixes are dropped. See
[Honorific Handling](#honorific-handling).

### Index Misalignment

**Problem:** `processed_text[start:end]` doesn't match entity  
**Solution:**
- Verify using correct DEID format (with `stt_idx_processed`/`end_idx_processed`)
- Check that entities are sorted by start index
- Ensure cumulative offset is tracked correctly

---

## License

Internal use only.
