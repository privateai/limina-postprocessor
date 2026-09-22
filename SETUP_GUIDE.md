# Setup Guide - Limina Post-Processor

This guide shows you how to set up and test the Limina Post-Processor with two sample scripts.

---

## Overview

The post-processor provides two sample scripts for testing:

1. **`sample_batch_process_files.py`** - Processes pre-generated DEID outputs (no live DEID needed)
2. **`sample_inline_deid_postprocess.py`** - Full pipeline with live DEID container (requires local Limina container)

---

## Prerequisites

### Required

- **Python 3.8+**
- **Git with Git LFS** (for downloading the 3.8GB name dictionary)
- **Python packages:** `pyarrow`, `requests`

### Optional (for `sample_inline_deid_postprocess.py` only)

- **Limina DEID container** running locally (default: `http://localhost:8080`)

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

## Key Features Demonstrated

Both samples demonstrate:

✅ **Global uniqueness** - No replacement name is reused across documents, tracked per entity type  
✅ **Within-document coreference** - Same original name gets same replacement within a document  
✅ **Gender matching** - Male names replaced with male, female with female (~90% on the sample corpus; unknown gender falls back to a 50/50 coin flip)  
✅ **Leading honorific preservation** - Titles like Dr., Mr., Mrs., Ms., Prof. are kept  
✅ **Index accuracy** - Character positions tracked correctly through replacements  

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

#### Batch Processing Mode

Use when you already have DEID outputs saved as files:

```python
import json
import limina_postprocessor

# Initialize once, then reuse — see "Production Considerations" below
processor = limina_postprocessor.DEIDPostProcessor(
    name_dictionary_path=limina_postprocessor.DEFAULT_DICTIONARY,
    enable_names=True
)

# Process each DEID output
with open('deid_output.json') as f:
    deid_data = json.load(f)

processed = processor.process_document(deid_data)

# Save or return processed result
with open('processed_output.json', 'w') as f:
    json.dump(processed, f, indent=2)
```

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

---

<a name="performance-benchmark-results"></a>
## Performance Benchmark Results

| Metric | Measured |
|---|---|
| Per distinct name | **2.6 ms** → ~385 names/sec on one core |
| Startup | 28 ms (the 3.8GB dictionary is memory-mapped, not loaded) |
| Uniqueness | 50,000 draws, 0 duplicates |
| Document length, 259 → 400,293 chars | 13.22 → 13.38 ms — no change |
| Mentions per person, 1× → 10× | 13.22 → 13.25 ms — no change |
| Document with no names, up to 10MB | 0.0005 ms |

**Estimate comes from distinct people, not document count.** A 400KB note costs the same as a
250-byte one, naming someone ten times costs the same as once, and documents with no
names return immediately.
Calculation for speed is `distinct people × 2.6 ms`; corpus size in GB isn't a useful input.

Cost varies by entity type - full names (`NAME`) ~2.6ms, surnames ~1.4ms, first names
~0.9ms - so a workload weighted toward first names runs faster than the headline.

Measured on macOS, Python 3.9.6, seed 20260917, single process. Absolute times are
hardware-dependent; re-run before committing to a schedule.

### Memory

Uniqueness works by retaining every synthetic name already issued, so memory grows for
the whole run and is never released. That is the guarantee working as designed, not a
leak.

| Distinct names | Memory for uniqueness | Runtime, 1 core |
|---|---|---|
| 10M | 0.9 GB | 7.2 h |
| 100M | 10.6 GB | 3 days |
| 700M | 78.3 GB | 21 days |

Treat these as a **floor** for RAM sizing — every name draw checks the whole set, so it
has to fit in real memory. If the machine is short on RAM the operating system will start
swapping the set to disk, and throughput collapses. Increasing throughput by increasing
number of cores is possible, but there is a trade-off - see the parallelism note below.

### Cores and uniqueness

The per-name cost is CPU-bound parquet decode, and each draw is independent work, so
runtime divides cleanly across processes:

| Workers | 700M names |
|---|---|
| 1 | 505 h (21 days) |
| 8 | 63 h |
| 32 | 15.8 h |

**Uniqueness is not guaranteed if labour is parallelized.** Uniqueness is enforced by the name set kept in memory and that
set lives inside a single process (core). If you run 32 workers and you have 32 independent sets:
worker 3 cannot see what worker 17 has issued, both sample the same 1.2B row
dictionary, and both will eventually hand out the same name. At 700M names,
over half the dictionary is consumed so collisions are the expected outcome from parallelization.

Current ways to keep parallelization speedy along with the uniqueness guarantee of names are being explored.

---

## Production Considerations

The samples above run as-is. A few things to be aware of before scaling to a large job:

- **Create the processor once** and reuse it for every document. Uniqueness is
  tracked per instance, so building a new one per batch restarts tracking and will
  reuse earlier names.
- **Don't parallelize across processes yet.** Workers don't share uniqueness
  tracking, so `N` processes can emit up to `N` copies of a name. This is the central
  trade-off at scale: one process keeps the guarantee but needs the full memory budget
  and serial runtime, while `N` processes cut runtime by `N` and give the guarantee up.
  Partitioning the dictionary's row groups across workers would restore it by
  construction, but that is not implemented.
- **Memory grows with the run**, since every name issued is retained to guarantee it
  is never reused: ~0.9GB per 10M distinct names, ~78GB at 700M. See
  [Performance Benchmark Results](#performance-benchmark-results).
- **Memory, not CPU, is the binding constraint at scale.** Runtime parallelizes;
  the uniqueness set does not.
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

**Problem:** `Connection refused` or `API Error` when running `sample_inline_deid_postprocess.py`  
**Solution:**
```bash
# Verify container is running
curl http://localhost:8080/process/text \
  -H "Content-Type: application/json" \
  -d '{"text": ["Hello"]}'

# If different host/port, edit sample_inline_deid_postprocess.py line 28:
# DEID_API_URL = "http://your-host:port/process/text"
```

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
- Startup reads parquet metadata only; the 3.8GB dictionary is memory-mapped, not loaded
- Ensure the parquet file is on local disk (not a network mount) for best performance
- Each generated name decodes one ~2MB row group, so sampling itself is cheap

**Problem:** Memory grows steadily over a long run  
**Cause:** Expected, not a leak. Every name handed out is retained so it is never
reused — that is what enforces uniqueness. See
[Performance Benchmark Results](#performance-benchmark-results) for a table of what to budget.

### Duplicate Names Across Documents

**Problem:** The same synthetic name appears in two different documents  
**Causes, in the order worth checking:**
1. A new processor was created per batch instead of once for the run
2. The run is parallelized across processes, which don't share tracking
3. The available names for that entity type ran out — most likely `NAME_GIVEN` or
   `NAME_FAMILY`

See [Production Considerations](#production-considerations) for all three.

---

