# Setup Guide

## Requirements

- Python 3.8+
- Nothing else. No third-party packages are required for the post-processor or for
  either sample script.

## Install

```bash
git clone https://github.com/privateai/limina-postprocessor.git
cd limina-postprocessor
pip install -e .
```

Editable installs need pip 21.3 or newer. On older pip this fails with
`A "pyproject.toml" file was found, but editable mode currently requires a
setuptools-based build`; run `pip install --upgrade pip setuptools wheel` first.

On an externally managed Python, add `--break-system-packages`.

Verify:

```bash
python3 -c "import limina_postprocessor; print(limina_postprocessor.__version__)"
```

There is nothing to download and nothing to install beyond the package itself. The
name data ships in the repo: 8.8 MB of census JSON plus an 18.6 MB blocklist index.

## Run the samples

**Batch a folder.** Put DEID JSON files in `sample_input/`, then:

```bash
python3 sample_batch_process_files.py
```

Results land in `sample_output/`.

**Full pipeline.** Needs a running DEID container:

```bash
python3 sample_inline_deid_postprocess.py
```

Set `DEID_API_URL` inside the script if your container is not at the default address.

## Command line

```bash
python3 -m limina_postprocessor.processor --input deid_output.json
python3 -m limina_postprocessor.processor --input in.json --output out.json --seed 42
```

| flag | purpose |
|---|---|
| `--input` | DEID JSON file: one document or a list |
| `--output` | defaults to `<input>_processed.json` |
| `--seed` | reproduce a previous run |
| `--census-data` | override the packaged census data directory |

## Use it from code

```python
import json
import limina_postprocessor

with open('deid_output.json') as f:
    payload = json.load(f)

result = limina_postprocessor.run_postprocessor(payload)
```

For many documents, build the processor once:

```python
processor = limina_postprocessor.DEIDPostProcessor()
for doc in documents:
    result = processor.process_document(doc)
```

## Input format

The processor needs an `entities` list and the document text. It reads
`processed_text` if present, else `text`.

```json
{
  "text": "Patient [NAME_1] saw Dr. [NAME_2].",
  "entities": [
    {
      "text": "John Smith",
      "best_label": "NAME",
      "location": {"stt_idx_processed": 8, "end_idx_processed": 16}
    }
  ]
}
```

Positions may instead be flat `start_idx`/`end_idx`. If neither is present, the
entity's text is located by searching the document.

Only `NAME`, `NAME_GIVEN`, `NAME_FAMILY` and `NAME_MEDICAL_PROFESSIONAL` are replaced;
other entity types pass through untouched.

## Reproducing a run

Every run prints the seed it used:

```
Entities: 5 seen, 5 replaced (100.0%)
Seed:     6931694429430834376
```

Pass it back to get identical output:

```bash
python3 -m limina_postprocessor.processor --input in.json --seed 6931694429430834376
```

## Famous-name blocklist

Replacements are checked against a blocklist of famous names, so a synthetic name
never lands on a real public figure. **4,631,292 pairs are blocked**, and the check is
on by default — there is nothing to configure.

The index ships as `limina_postprocessor/data/blocked_pairs.bin` and needs no setup.

It stores name positions rather than names, so it only works with the census lists it
was built from. If those are edited it refuses to load rather than silently blocking
the wrong names — reinstall the package if that happens.

## Before scaling up

- **Names are not unique across documents.** The processor keeps no state between
  documents, so a name can recur. The full-name pool is ~1.95 billion, but
  `NAME_GIVEN` draws from 12,018 and `NAME_FAMILY` from 162,251, and those repeat
  quickly. Tell us your expected volume per entity type if this matters.
- **Memory is flat**, about 91 MB, regardless of how many names are issued.
- **Entity types other than names are not replaced.** Dates, addresses and IDs pass
  through as the DEID container left them.

## Troubleshooting

**`ModuleNotFoundError: limina_postprocessor`** — run `pip install -e .` from the repo
root, or run scripts from that directory.

**Editable install fails on `pyproject.toml`** — pip is older than 21.3. Upgrade it.

**Output changes every run** — expected; the seed comes from the clock. Use `--seed`.

