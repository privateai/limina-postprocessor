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
git clone <repo-url> limina-postprocessor
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

## Production Considerations

The samples above run as-is. A few things to be aware of before scaling to a large job:

- **Create the processor once** and reuse it for every document. Uniqueness is
  tracked per instance, so building a new one per batch restarts tracking and will
  reuse earlier names.
- **Don't parallelize across processes yet.** Workers don't share uniqueness
  tracking, so `N` processes can emit up to `N` copies of a name. This needs a
  change inside the package — talk to us first.
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
2. The run is parallelized across processes, which don't share tracking
3. The available names for that entity type ran out — most likely `NAME_GIVEN` or
   `NAME_FAMILY`

See [Production Considerations](#production-considerations) for all three.

---

