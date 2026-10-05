# Limina Post-Processor

**Post-processing pipeline for Limina DEID output with synthetic name replacement**

Replaces de-identified name placeholders with realistic synthetic names while preserving:
- **Global uniqueness** - No cross-document reuse, tracked per entity type
- **Within-document coreference** - Same name gets same replacement
- **Gender matching** - Male/female names matched (~90% on the sample corpus)
- **Leading honorifics** - Dr., Mr., Mrs., Ms., Prof. preserved
- **Trailing suffixes** - Jr., Sr., II, III, IV preserved
- **Index accuracy** - Character positions tracked correctly

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

6. **Honorific and Suffix Handling:** leading titles and trailing generational suffixes are
   split off, preserved, and re-attached around the replacement — see
   [Honorific and Suffix Handling](#honorific-handling)

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
- **Note:** `Jr./Sr./III/IV` are listed as male titles but matched only as *prefixes*, so a
  trailing suffix never reaches Tier 1 — the first name decides. See
  [Honorific and Suffix Handling](#honorific-handling).

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

### Sample 3: Threaded Batching on Live DEID Output

Samples 1 and 2 post-process one document at a time, which is the correct shape when
documents *arrive* one at a time. Sample 3 covers the other shape — many documents in hand at
once — by handing the container's whole returned array to the thread pool instead of looping
over it. It is the one to copy for a batch job.

**Script:** `sample_threaded_deid_postprocess.py` (needs the DEID container on the first run
only; the raw output is cached and reused)

```bash
python3 sample_threaded_deid_postprocess.py --texts 100 --workers 8
```

It times both correct patterns against the loop-over-the-array mistake, which is the point:
`workers=8` is a ceiling, not a promise, so the loop measures ~1× while the batched call
measures ~5×. It then verifies the threaded output, reports how many documents passed, and
writes any failures to `failures.json` — exiting non-zero if any fail, so it can gate a
pipeline step.

Full walkthrough is in [SETUP_GUIDE.md](SETUP_GUIDE.md); the measured numbers, the GIL
explanation and the trade-offs are under [Parallel Processing](#parallel-processing).

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

# Decode only the column we substitute (~3ms, ~2MB), through a reader this thread owns
table = self._reader().read_row_group(row_group, columns=[column])

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
- **Multi-process parallelism breaks the guarantee.** Each worker *process* holds its own
  `used_names_global`, so two processes can independently emit the same name. There is
  no shared or partitioned uniqueness store across processes. `full_name` values *are*
  globally unique in the parquet, so assigning each
  process a disjoint subset of row groups would make cross-process uniqueness hold by
  construction — this is not implemented. Multi-*thread* parallelism is safe and is
  implemented; see [Parallel Processing](#parallel-processing).

<a name="parallel-processing"></a>
### Parallel Processing

`process_documents()` processes several documents at once on a thread pool, returning
results in input order:

```python
results = processor.process_documents(documents, workers=8)   # default: min(8, cpu_count)
```

For large runs use `iter_documents()`, which is the same thing but streaming — it holds at
most `workers * QUEUE_DEPTH_PER_WORKER` documents at a time, so the input may be a
generator over more data than fits in memory:

```python
for result in processor.iter_documents(load_documents(), workers=8):
    write(result)                       # input and output both stay bounded
```

`process_documents()` is just `list(iter_documents(...))`, so it holds every document and
every result at once — fine for a corpus, not for a backfill. Note this bounds the
*documents* in flight and does nothing for `used_names_global`, which accumulates every
name ever handed out; see [Scale Limits](#scale-limits).

**Why threading works here, despite the GIL.** Only one thread may run Python at a time,
so threading normally does nothing for CPU-bound work. This workload is the exception:
~99% of the per-name cost is `read_row_group`, which runs in PyArrow's C++ decoder and
releases the GIL while it decodes. Eight threads therefore decode eight row groups
genuinely simultaneously. Visible in `time`:

```
workers=1    2.36s user   0.20s sys    84% cpu   3.02s total
workers=8    3.30s user   0.22s sys   424% cpu   0.83s total
```

`424% cpu` — 3.3 seconds of compute inside 0.83 seconds of wall clock — is the evidence
the parallelism is real. Note `user` rises 40%: threading spreads the work, it does not
reduce it.

**Why threads and not processes.** Threads share one `used_names_global`, so
cross-document uniqueness still holds. Processes each get their own copy of that set and
can hand out the same name twice (see [Scale Limits](#scale-limits)).

Measured over 200 documents / 1,000 draws, against the pre-threading code:

| | Time | vs. before |
|---|---|---|
| Before threading, serial | 2.73s | 1.00× |
| After, `workers=1` | 2.78s | 0.98× — slightly slower |
| After, `workers=8` | 0.57s | **4.79×** |

Gains flatten past 8 workers (16 gave only 12% more): the decode is memory-bandwidth
bound, not core bound. Note the ~2% cost at `workers=1`, from routing per-document state
through a thread-local.

**Speedup is capped by document count, not worker count.** The unit of parallelism is the
document, so a batch holding fewer documents than workers cannot saturate the pool. The
same total work (1,000 draws) across three batch shapes:

| Batch shape | `workers=1` | `workers=8` | Speedup | Ceiling |
|---|---|---|---|---|
| 200 docs × 5 names | 1.50s | 0.19s | **7.80×** | `min(8, 200)` |
| 8 docs × 125 names | 0.89s | 0.18s | **4.92×** | `min(8, 8)` |
| 2 docs × 500 names | 0.90s | 0.58s | **1.54×** | `min(8, 2)` = 2× |

So batch *many* documents rather than a few large ones. A single large document cannot be
split across workers: coreference and gender state are per-document by design.

<a name="batching-worked-example"></a>
#### Batching: worked example

Hand the pool everything at once and let it pull. Build the processor **once** for the run:

```python
processor = DEIDPostProcessor(name_dictionary_path=DICT)   # once, not per batch

def load_documents(paths):
    for path in paths:                 # generator — nothing accumulates
        with open(path) as f:
            data = json.load(f)
        # DEID returns an array per file; yield the documents inside it, since one
        # document is one unit of work for the pool
        yield from (data if isinstance(data, list) else [data])

with open('out.jsonl', 'w') as out:
    for result in processor.iter_documents(load_documents(paths), workers=8):
        out.write(json.dumps(result) + '\n')
```

One pool for the whole run, input and output both bounded, all workers saturated. Measured
against the same 200 documents processed serially (best of 5; this host is noisy under the
memory-mapped dictionary, so only the large gaps are meaningful):

| Pattern | Best of 5 | vs. serial |
|---|---|---|
| Serial baseline (`workers=1`) | 1.16s | 1.00× |
| **`iter_documents`, one call, `workers=8`** | 0.18s | **6.6×** |
| `process_documents`, one call, `workers=8` | 0.21s | 5.5× |
| Chunks of 8, `workers=8` | 0.28s | 4.1× |
| Chunks of 2, `workers=8` | 0.52s | 2.2× |
| 200 calls of 1 document, `workers=8` | 0.91s | ~1× — within noise of serial |

**Four ways to lose the speedup:**

1. **Looping outside the call.** `workers=8` is a ceiling, not a promise — one document
   means one future in a pool of eight, so seven threads idle while you pay pool setup on
   every iteration:

   ```python
   for doc in documents:
       result = processor.process_documents([doc], workers=8)[0]   # ~1×
   ```

2. **Chunks smaller than `workers`.** Chunks of 2 cap at 2× no matter the core count. If
   you chunk for checkpointing, make chunks comfortably larger than `workers` and keep one
   processor across them.

3. **Rebuilding the processor per batch.** This is a *correctness* bug, not a slowdown:

   ```python
   for chunk in chunks(documents, 500):
       p = DEIDPostProcessor(name_dictionary_path=DICT)   # ← resets used_names_global
       p.process_documents(chunk, workers=8)
   ```

   `used_names_global` lives on the `NameHandler` instance, so a fresh processor starts with
   an empty set: names repeat across chunks and the cross-document uniqueness guarantee —
   the whole reason this uses threads rather than processes — silently breaks. It also
   re-opens the dictionary and re-indexes all 9,659 row groups each time.

4. **Merging documents to reduce overhead.** Backwards: the unit of work *is* the document,
   so merging removes the parallelism. It also changes output, since coreference state is
   per-document — merged documents share name mappings, so a name in one leaks into another
   that is supposed to be independent.

**The rule:** maximize the *number of documents* in flight, not the size of each. Speedup is
`min(workers, documents_in_the_call)`, so anything shrinking the second term is what costs
you, regardless of how much total text is going through.

#### When to use it

Offline batch work over **many** documents — corpus runs, backfills — on one host with RAM
headroom, at ~8 workers. That is the 5–8× regime and the case this is built for.

**When it does not help:**

- **One document per call** (e.g. a single REST request). Parallelism is *across*
  documents, never within one — a single document's names are still drawn sequentially.
  Expect the ~2% `workers=1` penalty and no gain.
- **A few large documents.** See the table above: two documents cap at 2× regardless of
  core count.
- **`process_file()` and the CLI**, which still loop serially. Only explicit
  `process_documents()` / `iter_documents()` calls are threaded.
- **When output must be reproducible.** Threads interleave draws from the shared random
  stream, so a document gets different names (still unique, still gender-matched)
  depending on the worker count. Use `workers=1` for byte-identical reruns.
- **Scaling past one process.** Threads are the limit of what is safe here; processes break
  the uniqueness guarantee (see [Scale Limits](#scale-limits)).

**Trade-offs to accept:**

- **It reaches the memory ceiling sooner.** Threading does not change what
  `used_names_global` costs (~139 bytes/name); it consumes names 5–8× faster in wall-clock,
  so whatever the RAM ceiling is, the run arrives there sooner. The streaming window in
  `iter_documents()` bounds documents in flight and does nothing for this.
- **A mid-run failure is not transactional.** When a document raises, `.result()` re-raises
  in yield order and the pool shuts down — but documents already submitted still finish,
  and every name they drew stays in `used_names_global`. The result is partial output plus
  permanently consumed names, with no rollback.
- **Each thread needs its own `ParquetFile`.** This used to be one shared reader, justified
  by concurrent reads on PyArrow 21.0.0 returning data byte-identical to serial reference
  reads — an observed property, not a contract, with a note to re-check on upgrade. It broke
  on exactly that upgrade: PyArrow 25 turned `pre_buffer` on by default (off through 21), and
  the `ReadRangeCache` behind it is mutated by every `read_row_group` call, so concurrent
  workers invalidate each other's entry and raise `ReadRangeCache did not find matching cache
  entry`. `_reader()` now hands each thread its own reader and `pre_buffer` is pinned off, so
  neither the sharing nor the default matters. **Do not reintroduce a shared reader**, and do
  not reach for a lock instead — it would have to span the decode, which is the only part that
  runs in parallel.

<a name="honorific-handling"></a>
### Honorific and Suffix Handling

A name is split into three parts — leading title, core name, trailing suffix — and only the
core is replaced. `_split_title` strips the front, `_split_suffix` strips the back, and both
are re-attached afterwards:

```
"Dr. Sarah Johnson"       →  ("Dr.", "Sarah Johnson", None)   →  "Dr. Cathleen Sciaraffa"
"Dr. John Smith Jr."      →  ("Dr.", "John Smith", " Jr.")    →  "Dr. Kamron Kroman Jr."
```

#### Leading titles (`_split_title`)

Two rules make this safe:

1. **A title must be followed by whitespace.** Without this, any name *beginning* with
   a title's letters gets mangled — `"Drew Lee"` became `"Dr"` + `"ew Lee"`, producing
   the fabricated output `"Dr Evelin Ramseur"`.
2. **Titles are tried longest-first** (`sorted_titles`, precomputed in `__init__`), so
   `Mrs.` wins over `Mr` and `Dr.` over `Dr`. The previous code iterated a `set`, making
   the result depend on arbitrary hash order.

Recognised: `Dr.` `Dr` `Mr.` `Mr` `Mrs.` `Mrs` `Ms.` `Ms` `Miss` `Mss.` `Mss` `Prof.`
`Prof` `Jr.` `Jr` `Sr.` `Sr` `III` `IV`

#### Trailing suffixes (`_split_suffix`)

DEID puts a generational suffix *inside* the name entity (`"James Wilson Sr"`), so a
replacement drawn from the dictionary has no suffix of its own and the text loses a token it
started with. `_split_suffix` is the mirror of `_split_title` and fixes that:

```python
NAME_SUFFIXES = frozenset({'JR', 'SR', 'II', 'III', 'IV'})
```

```
"John Smith Jr."      →  "Darvin Hoeper Jr."
"Robert Downey III"   →  "Jordy Kickbush III"
"Wilson, Jr."         →  "Christion Almonaci, Jr."    ← comma style preserved
"Dr. John Smith Jr."  →  "Dr. Kamron Kroman Jr."      ← both ends preserved
```

Four rules make this safe:

1. **Matched as a whole trailing token**, compared case-insensitively and ignoring a trailing
   period. Surnames that merely *end* in those letters are untouched — `"Sriram"`,
   `"Junior Alvarez"` keep their full text.
2. **Stripped before the cache key is built.** `"James Wilson Sr"` and `"James Wilson"` are
   therefore one person, the way `"Dr. James Wilson"` already was. This also fixes a
   coreference bug: `"Sr"` used to be cached as the surname component by `_cache_components`,
   so a later bare `"Wilson"` resolved against the wrong token.
3. **The separator travels with the suffix**, so `"Wilson, Jr."` does not come back as
   `"Wilson Jr."`.
4. **A bare suffix is left alone.** `_split_suffix("Jr")` returns `("Jr", None)` — stripping it
   would leave nothing to replace.

Bare `V` and `I` are deliberately **not** in `NAME_SUFFIXES`: as a trailing token they are far
more often a middle initial than a generational marker, and stripping an initial would change
the name rather than preserve it. So `"Mary Wilson V"` is replaced whole.

⚠️ `gender_detector` still classifies `Jr./Sr./III/IV` as male **prefix** titles, which is the
wrong shape — but it has no effect in practice, because `_detect_from_title` only matches at
the front of the string. A trailing suffix never produced a Tier 1 hit, so stripping it before
gender detection loses no signal that was being used. Verified: `detect_gender` returns `male`
for `"James Wilson Sr"` and `female` for `"Mary Wilson Sr"`, in both cases from the census
lookup on the first name, not from the suffix.

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
sample_batch_process_files.py        # Batch folder processing (sample_input/ → sample_output/)
sample_inline_deid_postprocess.py    # Full DEID pipeline + verification on 100 texts
sample_threaded_deid_postprocess.py  # Threaded batching on live DEID output

SETUP_GUIDE.md                       # Environment setup, all three samples
```

The two serial samples and the threaded one are different pipeline shapes, not old and new —
see [When to use it](#when-to-use-it). Walkthroughs for all three are in
[SETUP_GUIDE.md](SETUP_GUIDE.md).

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

### Mangled Titles or Missing Suffixes

**Problem:** A replacement comes out as `"Dr Evelin Ramseur"` from an input with no title  
**Cause:** A regression in `_split_title`'s boundary check — a name beginning with a
title's letters (`Drew`, `Sriram`, `Missy`, `Jrue`, `IVy`) being split apart. The guard
is that a title only matches when whitespace follows it. Verify with:

```python
handler._split_title("Drew Lee")   # must be (None, 'Drew Lee')
```

**Problem:** `Jr.` / `Sr.` / `II` / `III` / `IV` missing from output  
**Cause:** A regression in `_split_suffix`. These are preserved as of the suffix-handling
change; before it they were silently dropped. Verify with:

```python
handler._split_suffix("John Smith Jr.")   # must be ('John Smith', ' Jr.')
handler._split_suffix("Sriram")           # must be ('Sriram', None)
```

**Problem:** A trailing initial is being treated as a suffix, or `V` / `I` is dropped  
**Cause:** Something was added to `NAME_SUFFIXES`. Bare `V` and `I` are excluded on purpose —
as a trailing token they are usually a middle initial. See
[Honorific and Suffix Handling](#honorific-handling).

### Index Misalignment

**Problem:** `processed_text[start:end]` doesn't match entity  
**Solution:**
- Verify using correct DEID format (with `stt_idx_processed`/`end_idx_processed`)
- Check that entities are sorted by start index
- Ensure cumulative offset is tracked correctly

---

## License

Internal use only.
