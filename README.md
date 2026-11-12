# Limina Post-Processor

Replaces de-identified name placeholders in DEID output with realistic synthetic names.

```
Input:  "Patient [NAME_1] visited Dr. [NAME_2] on [DATE_1]..."
Output: "Patient Andrew Davis visited Dr. Jessica Thompson on [DATE_1]..."
```

Python 3.8+ and nothing else — no third-party packages, no data to download.

---

## Install

```bash
git clone https://github.com/privateai/limina-postprocessor.git
cd limina-postprocessor
pip install -e .
```

## Usage

```python
import limina_postprocessor

# one document or a list of them
result = limina_postprocessor.run_postprocessor(deid_output)
```

For many documents, build the processor once so the name data loads once:

```python
processor = limina_postprocessor.DEIDPostProcessor()
for doc in documents:
    result = processor.process_document(doc)
```

From the command line, or via the sample scripts:

```bash
python3 -m limina_postprocessor.processor --input in.json [--output out.json] [--seed N]

python3 sample_batch_process_files.py        # sample_input/ -> sample_output/
python3 sample_inline_deid_postprocess.py    # full DEID pipeline + verification
```

---

## What gets replaced

| Entity type | Replacement |
|---|---|
| `NAME` | first + surname |
| `NAME_MEDICAL_PROFESSIONAL` | first + surname |
| `NAME_GIVEN` | first name only |
| `NAME_FAMILY` | surname only |

Every other entity type — dates, addresses, IDs — passes through untouched.

## Where the names come from

| | |
|---|---|
| First names | 3,148 (1,098 male / 2,050 female) |
| Surnames | 6,456 |
| **Distinct full names reachable** | **18,730,547** |
| On disk | 6.3 MB |

First names are the US Census lists plus Ontario birth registrations (1917–2024,
OGL-ON 1.0); surnames are US Census. Both have been cut down to the names cleared by
triage, and carry the triage spelling where it restores punctuation the Census
flattened — `O'Brien` for `Obrien`, `Alvarez-Cruz` for `Alvarezcruz`. A name is
composed from the two lists on each request rather than stored, which is why 18.8
million of them fit in 6.3 MB.

Each first name belongs to exactly one gender pool, so a replacement always matches
the gender the detector found.

## Behaviour

**Coreference.** One original name maps to one replacement throughout a document,
including across entity types:

```
Alice Chen  (NAME)         -> Metta Ruys
Chen        (NAME_FAMILY)  -> Ruys
Alice       (NAME_GIVEN)   -> Metta
```

This resets at each document.

**Gender.** Two tiers: a leading honorific (`Mr.`, `Mrs.`, `Ms.`, `Miss`), then a
lookup of the first name against the census table. `Dr.` and `Prof.` are recognised
but imply nothing. Unresolved names get a coin flip. A title only matches with
whitespace after it, so "Drew" is not read as `Dr`.

**Honorifics** are split off, never re-generated, and restored unchanged:
`"Dr. John Smith"` → `"Dr. Rajvir Schulman"`.

**Capitalization** of an all-caps or all-lowercase original is mirrored:

```
mary johnson  -> cristal pfender
MARY JOHNSON  -> SAIGE REHANA
Mary Johnson  -> Zainab Exelby
```

**Character offsets.** Entities are replaced in document order and each one's
`stt_idx_processed` / `end_idx_processed` is rewritten to account for the length
changes before it. Three input position formats are accepted, because DEID output
varies: a nested `location` object, flat `start_idx`/`end_idx`, or neither — in which
case the entity's text is located by searching the document.

**Reproducibility.** Every run reports the seed it used. Pass it back for identical
output:

```python
limina_postprocessor.DEIDPostProcessor(seed=6931694429430834376)
```

Omit it and the seed comes from the clock, so each run differs.

## Famous-name blocklist

Replacements are checked so a synthetic name never lands on a real public figure.
The index holds **5,194,418 pairs** — every famous pairing the census lists have ever
been able to produce — of which **1,592,941 apply** to the names currently installed.
The check is on by default and needs no setup; the index ships as
`data/blocked_pairs.bin` (22.2 MB).

It is keyed on names, not census positions, so **editing the census data does not
invalidate it**. Names that stay keep their protection, names that come back get
theirs back, and nothing needs rebuilding unless the famous-name list itself
changes.

To run without it, only sensible if you filter elsewhere:

```python
limina_postprocessor.DEIDPostProcessor(blocklist_index='')
```

That lets famous names through on **8.7% of replacements**, roughly 1 in 11. The
rate is this high because the triaged name lists keep the common names, which are
the ones public figures have.

---

## Limits

**There is no cross-document uniqueness guarantee.** The processor holds no state
between documents, so every name is an independent draw. Rate at which a full name
repeats one issued earlier:

| names issued | duplicate rate |
|---|---|
| 10 k | 0.03% |
| 100 k | 0.27% |
| 1 M | 2.61% |
| 10 M | 22.44% |

`NAME_GIVEN` and `NAME_FAMILY` repeat far sooner, because their pools are the 3,148
first names and 6,456 surnames rather than the full 18.8 million combinations — and
a `NAME_GIVEN` draw only sees the matching gender's half (2,050 female, 1,098 male).
Replacing 1,000 distinct first names yields **20.8%** collisions; 1,000 surnames,
**7.4%**.

No pool size fixes that: it is a property of the entity type. If cross-document
uniqueness matters at your volume, tell us before scaling.

## Performance

Single process, measured on the sample corpus:

| | |
|---|---|
| Import + init | 24 ms |
| Per name entity | 6.9 µs |
| Per document (~5 entities) | 0.057 ms |
| Throughput | **17,961 documents/sec** |
| Memory | **103 MB, flat** |

Memory does not grow with the number of names issued — verified flat from startup
through 500,000 replacements.

---

## API

### `DEIDPostProcessor(census_data_dir=None, seed=None, blocklist_index=None)`

```python
processor.process_document(deid_output)          # -> dict (raises on a list)
processor.process_file(input_path, output_path)   # one document or a list
processor.print_statistics()
processor.seed
```

### `run_postprocessor(deid_output, census_data_dir=None, seed=None)`

Convenience wrapper that builds a processor per call, so the name data is re-read each
time. Prefer `DEIDPostProcessor` for more than one document.

## Project layout

```
limina_postprocessor/
├── __init__.py       re-exports run_postprocessor, DEIDPostProcessor
├── processor.py      document walk, offset tracking, run_postprocessor, CLI
├── name_pool.py      the seeded draw, with the blocklist check
├── blocklist.py      reads the blocklist index
├── census_data.py    loads the census name lists
├── data/             census JSON + the blocklist index
└── handlers/
    ├── base_handler.py      per-document cache + interface
    ├── name_handler.py      entity -> replacement
    └── gender_detector.py   honorific and first-name lookup
```

---

## Troubleshooting

**The same replacement appears in two documents.** Expected — see
[Limits](#limits). Most likely on `NAME_GIVEN` or `NAME_FAMILY`.

**A name's gender looks wrong.** The lists are US- and Ontario-centric, so
international first names often miss both tiers and fall back to a coin flip. A bare
surname has no first name to look up, so it is ungenderable by construction.

**Results differ between runs.** By design. Pass `seed=` to fix the output.

**Offsets look wrong.** Only `stt_idx_processed` and `end_idx_processed` are
rewritten; any other index fields are left as the DEID container set them.

**`blocklist index not found`** — `data/blocked_pairs.bin` ships with the package, so
the install is incomplete. Reinstall, or pass `blocklist_index=''`.


---

## License

MIT
