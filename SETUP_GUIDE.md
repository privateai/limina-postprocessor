# Setup Guide - Limina Post-Processor

This guide shows you how to set up and test the Limina Post-Processor with three sample scripts.

---

## Overview

The post-processor provides three sample scripts for testing:

| | Script | DEID container | Post-processing |
|---|---|---|---|
| 1 | **`sample_batch_process_files.py`** | not needed | serial, one document at a time |
| 2 | **`sample_inline_deid_postprocess.py`** | required | serial, one document at a time |
| 3 | **`sample_threaded_deid_postprocess.py`** | required (first run only) | threaded batch |

The serial scripts and the threaded one are different pipeline shapes, not old and new — see
[Choosing a Processing Mode](#choosing-a-processing-mode). Samples 2 and 3 are the same
pipeline, differing only in how they post-process what the container returns.

---

## Prerequisites

### Required

- **Python 3.8+**
- **Git with Git LFS** (for downloading the 3.8GB name dictionary)
- **Python packages:** `pyarrow`, `requests`

### Optional (for Samples 2 and 3 only)

- **Limina DEID container** running locally (default: `http://localhost:8080`).
  Sample 3 needs it on the first run only — it caches the container's output and reuses it.

---

## Installation

### Step 1: Clone Repository

```bash
# Install Git LFS if not already installed
# macOS:
brew install git-lfs

# Ubuntu/Debian:
sudo apt install git-lfs

# Windows:
# Download from https://git-lfs.github.com

# Initialize Git LFS
git lfs install

# Clone repository
git clone https://github.com/privateai/limina-postprocessor.git limina-postprocessor
cd limina-postprocessor

# Verify dictionary was downloaded (should be ~3.8GB, not a few KB)
ls -lh limina_postprocessor/data/name_dictionary_1b_filtered.parquet

# If only showing a few KB, pull the actual file:
git lfs pull
```

### Step 2: Install Python Dependencies

```bash
pip install pyarrow requests

# Or on systems with externally-managed Python:
pip install --break-system-packages pyarrow requests
```

### Step 3: Verify the Install

No DEID container needed for this — it exercises the dictionary and the thread pool only.

```bash
pip install pytest
python3 -m pytest tests/ -q
```

Expect **64 passed**.

`64 skipped` means the dictionary is a Git LFS pointer rather than the real 3.8GB file —
the tests skip rather than fail so the suite is usable without it. Go back to
[Step 1](#step-1-clone-repository) and run `git lfs pull`.

Worth running more than once on a new host. The properties under test are concurrent, and
the one real bug found this way — all threads sharing a single parquet reader — surfaced
on some runs and not others on the same machine.

---

## Sample 1: Batch Processing (No DEID Required)

This sample processes pre-generated DEID outputs that are already in the `sample_input/` folder.

**What it does:**
- Reads DEID JSON files from `sample_input/`
- Post-processes all name entities with synthetic replacements
- Saves results to `sample_output/`

**Run:**
```bash
python3 sample_batch_process_files.py
```

**Verify outputs:**
```bash
# Check output files
ls -lh sample_output/

# View a processed file
cat sample_output/test1.json | head -50
```

---

## Sample 2: Full Pipeline with Live DEID (Recommended)

This sample demonstrates the complete ETL pipeline with your local Limina DEID container.

**What it does:**
1. Generates 100 sample medical texts with various names
2. Calls your local Limina DEID container at `http://localhost:8080`
3. Post-processes all DEID outputs with synthetic name replacements
4. Saves results to `sample_output_batch/` (100 JSON files)
5. Verifies quality (indices, honorifics, coreference, uniqueness)
6. Displays performance metrics and sample outputs

### Prerequisites

You must have your **Limina DEID container running locally** at `http://localhost:8080`.

**Test the container is accessible:**
```bash
curl --request POST \
  --url http://localhost:8080/process/text \
  --header 'Content-Type: application/json' \
  --data '{"text": ["Hello John"]}'

# Should return JSON with entities detected
```

If your DEID container runs on a different host/port, edit the `DEID_API_URL` in `sample_inline_deid_postprocess.py`:
```python
# Line 28
DEID_API_URL = "http://your-host:your-port/process/text"
```

### Run the Sample

```bash
python3 sample_inline_deid_postprocess.py
```

**Verify outputs:**
```bash
# Check output folder (should have 100 files)
ls sample_output_batch/

# View a processed file
cat sample_output_batch/processed_001.json | head -50
```

**Expected uniqueness results:**
- **100% cross-document uniqueness** (no name reused across different documents)
- Any duplicates are within the same document (correct coreference behavior)
- This holds because all 100 documents run through a single processor instance —
  see [Production Considerations](#production-considerations) before scaling up

---

## Sample 3: Threaded Batching on Live DEID Output

Same pipeline as Sample 2, but it hands the container's whole array to the thread pool
instead of looping over it. The input is real DEID output — nothing synthesized.

This is the sample to copy for a batch job, and it demonstrates the thing that decides whether
threading pays off at all: **how you batch matters more than the worker count.** DEID
naturally returns an array, so a DEID pipeline already has many documents in hand at once —
which is exactly the shape threading rewards, and exactly the shape a `for` loop throws away.

**Prerequisites:** the DEID container, as for Sample 2. Only the first run needs it — the raw
DEID output is cached to `sample_output_threaded_deid/deid_raw.json` and reused, so you can
re-run the comparison without re-calling the container (`--refresh` to re-fetch).

**What it does:**
1. Sends the sample texts to the container in batches and flattens the returned arrays into one list of documents
2. Times serial, `process_documents()`, `iter_documents()` streaming, and the loop-over-the-array mistake
3. Verifies indexes, honorifics, coreference and cross-document uniqueness against the original texts
4. Reports **how many documents passed**, and names the failing ones by line number
5. Writes results to `sample_output_threaded_deid/processed.jsonl`, and any failures to `failures.json`

**Run:**
```bash
python3 sample_threaded_deid_postprocess.py

python3 sample_threaded_deid_postprocess.py --texts 100 --workers 8
python3 sample_threaded_deid_postprocess.py --refresh         # re-call the container
python3 sample_threaded_deid_postprocess.py --skip-mistake    # only the correct patterns
```

**Expected result** — the default corpus of 100 documents / 402 entities on a 10-core host,
against a ceiling of `min(workers, documents)` = 8×:

```
Serial, one document at a time (workers=1)              1.29s      1.00x
process_documents, whole batch, workers=8               0.25s      5.23x
iter_documents streaming, workers=8                     0.28s      4.56x
MISTAKE: looping over the DEID array                    1.22s      1.06x
```

Post-processing only; the container round trip is not in these figures. Expect lower ratios
on fewer cores — the default worker count is `min(8, os.cpu_count())`, so a 2-vCPU box
ceilings near 2× however many workers you ask for. Check `nproc` before reading a low ratio
as a regression.

The last row is the one that matters for a DEID pipeline, and it is why `workers=8` is a
**ceiling, not a promise**. The container already returned a batch; looping over it puts one
document in a pool of eight and wastes the other seven — `workers=8` bought nothing, because a
pool can only parallelize across the documents handed to a *single* call.

The two correct rows differ only in memory: `process_documents()` returns a list and holds
everything at once, while `iter_documents()` streams and holds only a bounded window, so its
input may be a generator over more data than fits in RAM. Timed here over the same in-memory
list to keep the comparison fair, which means this run measures their speed and not that
difference.

> **Ratios are suppressed on small runs.** Reporting them needs at least 50 documents *and*
> 250 entities between them. Documents are the unit of parallelism but entities are the unit
> of work, and the two come apart: a sparse run of 100 documents holding 4 entities measured
> 31.9× against an 8× ceiling, because there was almost nothing to parallelize.

### Reading the failure report

Verification is reported per document, so a bad batch tells you *which* files to look at:

```
100 documents, 402 entities

documents passed           100 / 100
documents failed             0 / 100   (0.0%)
cross-document issues        0

✅ Index accuracy       - every span slices back to its entity
✅ Honorifics           - Dr./Mr./Mrs./Ms./Prof./Jr./Sr. counts unchanged from the original
✅ Coreference          - one original name, one replacement per document
✅ Cross-doc uniqueness - no replacement name reused between documents
```

When something fails, the summary is followed by a breakdown and the offending documents,
identified by **line number in `processed.jsonl`**:

```
documents passed            71 / 100
documents failed            29 / 100   (29.0%)
cross-document issues        0

❌ Failures by check:
     Index accuracy             18
     Honorifics                 11

❌ Failing documents (by line number in processed.jsonl):
     line 1     1 issue(s)  Honorific 'Dr.' count mismatch: 2 → 1
     line 2     1 issue(s)  Honorific 'Dr.' count mismatch: 1 → 0
     line 3     1 issue(s)  Index mismatch: [4:18] 'Domingo Yuriar' != 'Bogus Name12'
     line 4     1 issue(s)  Honorific 'Dr.' count mismatch: 2 → 1
     line 13    1 issue(s)  Index mismatch: [4:20] 'Algernon Panjabi' != 'Bogus Name15'
     ... and 14 more failing documents

📄 Full failure report: 'sample_output_threaded_deid/failures.json'
```

That block is real output, produced by deliberately damaging a copy of a passing run — a
replacement overwritten so it no longer matches its recorded span, and a title removed from
the document text. It is what a regression looks like, not a mock-up.

Three things to know about it:

- **The console list is truncated at 15; `failures.json` holds every failure.** Read the file,
  not the terminal, when triaging a large run. It carries `documents_total`,
  `documents_failed`, `failures_by_check`, the per-document issues keyed `line_1`, `line_2`…,
  and `cross_document`.
- **Inspect a failing document directly** — the line number is its line in the output:

  ```bash
  sed -n '12p' sample_output_threaded_deid/processed.jsonl | python3 -m json.tool
  ```

- **The script exits non-zero when anything fails**, so it can gate a pipeline step rather
  than only being read by eye. `Other` counts issues the script could not categorise, which
  means a check's wording changed — they are counted, never dropped.

Cross-document issues are listed separately because they are not attributable to one
document: a reused name implicates a *pair* of lines, and neither is wrong on its own.

> **What the uniqueness check does and does not assert.** It checks names the dictionary
> *drew*, not names derived from one. If `"John Smith"` becomes `"Elmore Whicker"` in one
> document, a bare `"Whicker"` elsewhere is permitted by design — the package reserves full
> names and components separately, so it never claimed the bare surname. The check skips
> those deliberately; counting them would report a failure the package does not promise to
> avoid, and only ever at random, whenever a later surname draw collided inside the ~162K
> pool. See [Global Uniqueness Tracking](README.md#global-uniqueness-tracking).

---

## Key Features Demonstrated

All three samples demonstrate:

✅ **Global uniqueness** - No replacement name is reused across documents, tracked per entity type  
✅ **Within-document coreference** - Same original name gets same replacement within a document  
✅ **Gender matching** - Male names replaced with male, female with female (~90% on the sample corpus; unknown gender falls back to a 50/50 coin flip)  
✅ **Leading honorific preservation** - Titles like Dr., Mr., Mrs., Ms., Prof. are kept  
✅ **Trailing suffix preservation** - Jr., Sr., II, III, IV are kept, including the comma in "Wilson, Jr."  
✅ **Index accuracy** - Character positions tracked correctly through replacements  

Sample 3 additionally demonstrates that threading preserves every one of the above — the
cross-document uniqueness check is the one most at risk, and is why it uses threads rather
than processes.

---

## Integration with Your ETL Pipeline

To integrate the post-processor into your production ETL pipeline, first install it as a system-wide package.

### Step 1: Install as a System-Wide Package

**Editable Install:**

```bash
# From the limina-postprocessor directory
pip install -e .

# Or on systems with externally-managed Python:
pip install --break-system-packages -e .
```

**Verify installation:**

```bash
# Test from any directory
python3 -c "import limina_postprocessor; print(limina_postprocessor.__version__)"
# Should print: 0.1.0
```

**Uninstall:**

```bash
pip uninstall limina-postprocessor
```

---

### Step 2: Use in Your ETL Pipeline

Once installed, you can import and use `limina_postprocessor` from anywhere in your codebase.

#### Choosing a Processing Mode

There are three modes. They are not old-vs-new — each fits a different pipeline shape, and
the serial modes remain fully supported:

| Your pipeline shape | Use | Threading |
|---|---|---|
| One document per request (REST endpoint, live ETL) | `process_document()` | No benefit — parallelism is *across* documents |
| A folder or corpus that fits in memory | `process_documents(docs, workers=8)` | ~5–6× |
| A large backfill that does not fit in memory | `iter_documents(generator, workers=8)` | ~5–6×, bounded memory |
| Output must be byte-identical between runs | any mode with `workers=1` | Off — threads reorder name draws |

Threading only pays off when you hand many documents to a **single call**. Speedup is
`min(workers, documents_in_that_call)`, so processing one document at a time in a loop gets
you nothing no matter what `workers` is set to. See
[Parallel Batch Mode](#parallel-batch-mode-threaded) below.

#### Single-Document Mode (Serial)

Use when you have one DEID output at a time — a REST request, or a file-at-a-time job. This
is unchanged by threading and is still the right choice for this shape:

```python
import json
import limina_postprocessor

# Initialize once, then reuse — see "Production Considerations" below
processor = limina_postprocessor.DEIDPostProcessor(
    name_dictionary_path=limina_postprocessor.DEFAULT_DICTIONARY,
    enable_names=True
)

# The DEID container returns a JSON *array*, even for a single input text
with open('deid_output.json') as f:
    deid_data = json.load(f)

# process_document() takes ONE document. Handing it the array returns the array
# unchanged, with no replacement and no error — so unwrap it first.
processed = [processor.process_document(doc) for doc in deid_data]

# Save or return processed result
with open('processed_output.json', 'w') as f:
    json.dump(processed, f, indent=2)
```

> **Watch the array.** `process_document()` expects a single document object. If you pass the
> container's top-level array it silently returns it untouched — no names replaced, no error
> raised. `processor.process_file(input_path, output_path)` handles both shapes for you and is
> the safer choice when reading straight from a file.

#### Inline Processing Mode

Integrate directly into your ETL pipeline where you call DEID:

```python
import requests
import limina_postprocessor

# Initialize processor once (reuse for all documents)
processor = limina_postprocessor.DEIDPostProcessor(
    name_dictionary_path=limina_postprocessor.DEFAULT_DICTIONARY,
    enable_names=True
)

# Your ETL loop
for text in your_text_batches:
    # Step 1: Call your DEID container
    response = requests.post(
        'http://localhost:8080/process/text',
        headers={'Content-Type': 'application/json'},
        json={
            'text': [text],
            'entity_detection': {
                'entity_types': [
                    {'type': 'ENABLE', 'value': ['NAME', 'NAME_FAMILY', 'NAME_GIVEN']}
                ],
                'return_entity': True
            },
            'processed_text': {
                'type': 'SYNTHETIC',
                'coreference_resolution': 'heuristics'
            }
        }
    )
    deid_output = response.json()[0]

    # Step 2: Post-process with synthetic names
    processed = processor.process_document(deid_output)

    # Step 3: Use processed result in your pipeline
    your_downstream_processing(processed)
```

This loop is serial by nature — it calls DEID once per text, so there is only ever one
document to post-process at a time. If your DEID calls can be batched, collect the DEID
outputs first and then post-process them together using the mode below.

<a name="parallel-batch-mode-threaded"></a>
#### Parallel Batch Mode (Threaded)

Use when you have **many** DEID outputs available at once. Hand them all to a single call and
let the thread pool pull from it:

```python
import json
import limina_postprocessor

# Create ONCE for the whole run — see Production Considerations
processor = limina_postprocessor.DEIDPostProcessor(
    name_dictionary_path=limina_postprocessor.DEFAULT_DICTIONARY,
    enable_names=True
)

def load_documents(paths):
    for path in paths:                 # generator — nothing accumulates in memory
        with open(path) as f:
            data = json.load(f)
        # The container returns an array per file; yield the documents inside it
        yield from (data if isinstance(data, list) else [data])

# Streaming: holds only a bounded window of documents, so `paths` may be larger than RAM
with open('processed.jsonl', 'w') as out:
    for processed in processor.iter_documents(load_documents(paths), workers=8):
        out.write(json.dumps(processed) + '\n')
```

Feeding a generator of *documents* (not of file-shaped arrays) is what keeps the pool busy —
each document is one unit of work.

If the whole corpus comfortably fits in memory, `process_documents()` is the simpler call and
returns a list in input order:

```python
results = processor.process_documents(documents, workers=8)   # default: min(8, cpu_count)
```

**Four mistakes that cost you the speedup:**

1. **Looping outside the call.** `workers=8` is a ceiling, not a promise — one document per
   call means seven idle threads plus pool setup on every iteration:

   ```python
   for doc in documents:
       processor.process_documents([doc], workers=8)      # ~1x, no faster than serial
   ```

2. **Chunks smaller than `workers`.** Chunks of 2 cap at 2× regardless of core count. If you
   chunk for checkpointing, make chunks comfortably larger than `workers`.

3. **Rebuilding the processor per batch.** This breaks *correctness*, not just speed —
   uniqueness tracking lives on the instance, so a fresh processor forgets every name it has
   issued and will reuse them:

   ```python
   for chunk in chunks(documents, 500):
       p = DEIDPostProcessor(...)      # ← resets uniqueness tracking; names repeat
   ```

4. **Merging documents into fewer, larger ones.** The unit of parallelism is the document, so
   merging removes the parallelism. It also makes separate documents share coreference state,
   so a name from one leaks into another.

**Rule of thumb:** maximize the *number* of documents in a single call, not the size of each.

`sample_threaded_deid_postprocess.py` times both correct patterns against mistake 1 on your
own hardware, so you can see the gap rather than take it on faith. See
[Sample 3](#sample-3-threaded-batching-on-live-deid-output).

Measured numbers, the GIL explanation, and the full list of trade-offs are in the
[Parallel Processing](README.md#parallel-processing) section of the README.

---

## Production Considerations

The samples above run as-is. A few things to be aware of before scaling to a large job:

- **Create the processor once** and reuse it for every document, including across every
  worker thread. Uniqueness is tracked per instance, so building a new one per batch
  restarts tracking and will reuse earlier names.
- **Threads are safe; processes are not.** `process_documents()` and `iter_documents()`
  use a thread pool, and threads share one uniqueness set, so the cross-document
  guarantee still holds — use them freely. Splitting a run across *processes* is still
  unsupported: each process gets its own copy of the tracking set, so `N` processes can
  emit up to `N` copies of a name. That needs a change inside the package — talk to us
  first.
- **Threaded output is not reproducible.** Threads interleave their draws from the shared
  random stream, so the same input yields different (still unique, still gender-matched)
  names depending on the worker count. Use `workers=1` when you need byte-identical
  reruns, such as for a golden-file test or an audit.
- **Threading reaches the memory ceiling sooner.** It does not change how much memory a
  run needs, but it consumes names 5–6× faster in wall-clock time, so a long job arrives
  at whatever RAM limit you have that much earlier.
- **Memory grows with the run**, since every name issued is retained to guarantee it
  is never reused: roughly 1.4GB per 10M replacements (~100GB at 700M).
- **`NAME_GIVEN` and `NAME_FAMILY` have much smaller pools** than the headline 1.2
  billion (~7,500 first names and ~162,000 surnames). If a pool runs out, duplicates
  appear with no error, so let us know your expected volume per entity type.

---

## Troubleshooting

### Git LFS Issues

**Problem:** Dictionary file is only a few KB  
**Solution:**
```bash
git lfs install
git lfs pull
ls -lh limina_postprocessor/data/name_dictionary_1b_filtered.parquet
# Should show ~3.8GB
```

### DEID Container Connection Error

**Problem:** `Connection refused` or `API Error` when running Sample 2 or Sample 3  
**Solution:**
```bash
# Verify container is running
curl http://localhost:8080/process/text \
  -H "Content-Type: application/json" \
  -d '{"text": ["Hello"]}'

# If different host/port, edit sample_inline_deid_postprocess.py line 28:
# DEID_API_URL = "http://your-host:port/process/text"
```

Sample 3 imports `DEID_API_URL` from `sample_inline_deid_postprocess.py`, so editing it there
fixes both. Sample 3 also caches the container's output, so once it has succeeded once it will
keep running without the container until you pass `--refresh`.

**Problem:** Sample 3 says `Cached DEID output does not match the requested texts`  
**Cause:** Not an error — `--texts` changed since the cache was written, so it is re-calling the
container. The cache stores the texts alongside the documents on purpose: verification pairs
each document with the text that produced it, and a drifted cache would silently verify the
wrong pairs instead of failing.

### Python Package Errors

**Problem:** `ModuleNotFoundError: No module named 'pyarrow'`  
**Solution:**
```bash
pip install pyarrow requests

# Or on Ubuntu 22.04+:
pip install --break-system-packages pyarrow requests
```

### Memory Issues

**Problem:** Out of memory during initialization  
**Solution:**
- Steady-state usage is ~50MB; the dictionary is memory-mapped, not loaded
- Ensure the parquet file is on local disk (not a network mount) for best performance
- The dictionary itself is 3.8GB on disk but sampling uses only ~50MB RAM

**Problem:** Memory grows steadily over a long run  
**Cause:** Expected, not a leak. Every name handed out is retained so it is never
reused — that is what enforces uniqueness. See
[Production Considerations](#production-considerations) for how much to budget.

### Duplicate Names Across Documents

**Problem:** The same synthetic name appears in two different documents  
**Causes, in the order worth checking:**
1. A new processor was created per batch instead of once for the run
2. The run is parallelized across *processes*, which don't share tracking. Note that
   threads — `process_documents()` / `iter_documents()` — are safe and are *not* a cause
3. The available names for that entity type ran out — most likely `NAME_GIVEN` or
   `NAME_FAMILY`

See [Production Considerations](#production-considerations) for all three.

### Verification Failures in Sample 3

**Problem:** `Honorific 'Sr.' count mismatch: 1 → 0` (or `Jr.`, `II`, `III`, `IV`)  
**Cause:** A trailing generational suffix was dropped from the replacement. These are
preserved — `"James Wilson Sr"` comes back as `"<new name> Sr"` — so a failure here means
`_split_suffix` regressed. See
[Honorific and Suffix Handling](README.md#honorific-handling) in the README.

**Problem:** `Index mismatch: [start:end] 'x' != 'y'`  
**Cause:** The entity's recorded span no longer slices back to its own replacement text, so
downstream consumers reading by offset will get the wrong characters. Check that the input
really is DEID output with `stt_idx_processed`/`end_idx_processed`, and that entities arrive
sorted by start index.

**Problem:** `'<name>' reused on lines N and M`  
**Cause:** Cross-document uniqueness broke. In a threaded run the usual cause is a processor
built per batch rather than once for the run — see
[Production Considerations](#production-considerations).

### Threading Is Not Making It Faster

**Problem:** `workers=8` runs at about the same speed as `workers=1`  
**Cause:** Almost always one document per call. Speedup is
`min(workers, documents_in_that_call)`, so a loop that calls `process_documents([doc])` once
per document has a ceiling of 1× — `workers` is a cap, not a promise.

**Check:** how many documents reach a single call?

```python
# ~1x, regardless of workers
for doc in documents:
    processor.process_documents([doc], workers=8)

# ~5-6x
processor.process_documents(documents, workers=8)
```

Also worth checking: chunks smaller than `workers` (chunks of 2 cap at 2×), and whether your
pipeline is genuinely one-document-per-request — in which case threading cannot help and the
serial mode is correct. See [Parallel Batch Mode](#parallel-batch-mode-threaded).

---

