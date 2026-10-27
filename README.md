# Limina Post-Processor

Replaces de-identified name placeholders in DEID output with realistic synthetic names.

```
Input:  "Patient [NAME_1] visited Dr. [NAME_2] on [DATE_1]..."
Output: "Patient Andrew Davis visited Dr. Jessica Thompson on [DATE_1]..."
```

**No dependencies, no data download.** Names are composed at request time from the
census lists in `limina_postprocessor/data/census_data/` using only the standard
library.

---

## Install

```bash
git clone https://github.com/privateai/limina-postprocessor.git
cd limina-postprocessor
pip install -e .
```

Python 3.8+ and nothing else. No third-party packages, at all — the post-processor and
both sample scripts use only the standard library.

## Usage

```python
import limina_postprocessor

# One document or a list of them
result = limina_postprocessor.run_postprocessor(deid_output)
```

For many documents, build the processor once so the census lists load once:

```python
processor = limina_postprocessor.DEIDPostProcessor()
for doc in documents:
    result = processor.process_document(doc)
```

Command line:

```bash
python3 -m limina_postprocessor.processor --input deid_output.json
python3 -m limina_postprocessor.processor --input in.json --output out.json --seed 42
```

Sample scripts:

```bash
python3 sample_batch_process_files.py        # sample_input/ -> sample_output/
python3 sample_inline_deid_postprocess.py    # full DEID pipeline + verification
```

---

## What it does

### Entity types

| Entity type | Replacement |
|---|---|
| `NAME` | first + surname |
| `NAME_MEDICAL_PROFESSIONAL` | first + surname |
| `NAME_GIVEN` | first name only |
| `NAME_FAMILY` | surname only |

Anything else is left untouched.

### Name source

| | |
|---|---|
| First names | 12,018 (5,591 male / 6,427 female) |
| Surnames | 162,251 |
| Distinct full names reachable | **1,949,932,518** |
| On disk | 8.8 MB of JSON |

First names come from `data/census_data/{male,female}_first_names.json` — the US
Census lists plus Ontario birth registrations (1917–2024, OGL-ON 1.0). Surnames come
from `surnames.json` (US Census), minus the `All Other Names` aggregate row, which is
a total rather than a name.

Each first name belongs to exactly one gender pool. Where a name appears in both files
the higher count wins, ties go to male. `GenderDetector` builds its lookup from these
same pools, so every name the pool can emit is one the detector can resolve.

### Randomness and the seed

Each `NameHandler` creates its own `random.Random`, seeded once at construction, and
draws every name from it. Using a private generator means the handler neither disturbs
nor depends on the caller's `random` state.

The default seed is `time.time_ns() ^ os.urandom(8)` — timestamped so a run is
identifiable and roughly sortable, with OS entropy mixed in so that processes starting
together cannot collide. The entropy term is load-bearing: seeding 40 simultaneous
processes from `int(time.time())` produced **one** seed and 40 identical name streams.

`processor.seed` reports the seed used. Pass it back to reproduce a run exactly:

```python
processor = limina_postprocessor.DEIDPostProcessor(seed=6931694429430834376)
```

### Within-document coreference

One original name maps to one replacement throughout a document, including across
entity types. If a document says "Dr. Alice Chen" and later just "Chen":

```
Alice Chen  (NAME)         -> Metta Ruys
Chen        (NAME_FAMILY)  -> Ruys
Alice       (NAME_GIVEN)   -> Metta
```

Replacements are cached as `(first, surname)` pairs indexed by both halves of the
original name, so recovering either component never requires re-splitting a joined
string. That matters because six census first names contain spaces — "Jo Ann",
"Maria De Fatima", "Le Roy" — and splitting on whitespace truncates them.

`process_document` clears this cache for each document.

### Gender

Two tiers, no network:

1. **Honorific** — `Mr.`, `Mrs.`, `Ms.`, `Miss`, `Jr.`, `Sr.`, `III`, `IV`.
2. **Census first name** — one lookup against the 12,018-name table.

Unresolved names get a coin flip from the same seeded generator.

A title only matches when whitespace follows it, so "Drew" is not read as `Dr` and
"Sriram" is not read as `Sr`.

`Dr.` and `Prof.` are recognised so they can be stripped before the first-name lookup,
but they imply no gender.

### Honorifics

A leading honorific is split off, the remainder is replaced, and the original title is
restored: `"Dr. John Smith"` → `"Dr. Rajvir Schulman"`. The title is never
re-generated, so it cannot be changed or invented.

### Capitalization

An all-caps or all-lowercase original is mirrored:

```
mary johnson  -> cristal pfender
MARY JOHNSON  -> SAIGE REHANA
Mary Johnson  -> Zainab Exelby
```

### Character offsets

Entities are replaced in document order and each entity's `location` is rewritten with
the offset accumulated by the entities before it:

```python
location['stt_idx_processed'] = original_start + cumulative_offset
location['end_idx_processed'] = stt_idx_processed + len(replacement)
```

Three input position formats are accepted, because DEID output varies: a nested
`location` object, flat `start_idx`/`end_idx`, or neither — in which case the entity's
text is located by searching the document.

---

## Performance

Single process, measured on the sample corpus:

| | |
|---|---|
| Import | 12.6 ms |
| Init (census parse) | 43.6 ms |
| Per name | **5.3 µs** |
| Cold call: import + init + 4 names | **57.1 ms** |
| Memory | **91 MB, flat** |

Memory does not grow with the number of names issued — there is nothing to accumulate.

---

## Uniqueness

**There is no cross-document uniqueness guarantee.** The processor holds no state
between documents, so each name is an independent draw from the pool. Expected rate at
which a full name repeats one issued earlier:

| names issued | duplicate rate |
|---|---|
| 1 M | 0.03% |
| 10 M | 0.26% |
| 100 M | 2.52% |

`NAME_GIVEN` and `NAME_FAMILY` draw from far smaller pools — 12,018 first names and
162,251 surnames — so they repeat much sooner. Replacing 6,000 distinct first names
statelessly yields roughly 2,000 collisions.

If cross-document uniqueness matters for your volume, say so before scaling; it
requires either shared state or a different construction.

---

## Project layout

```
limina_postprocessor/
├── __init__.py              run_postprocessor, DEIDPostProcessor
├── processor.py             document walk, offset tracking, CLI
├── name_pool.py             census lists + the seeded generator
├── data/census_data/
│   ├── male_first_names.json      6,077 entries
│   ├── female_first_names.json    6,787 entries
│   └── surnames.json              162,252 entries
└── handlers/
    ├── base_handler.py      BaseEntityHandler: per-document cache + interface
    ├── name_handler.py      NameHandler: entity -> replacement
    └── gender_detector.py   honorific and census first-name lookup

sample_batch_process_files.py       batch a folder
sample_inline_deid_postprocess.py   DEID pipeline + verification
```

---

## API

### `DEIDPostProcessor(census_data_dir=None, seed=None)`

```python
processor.process_document(deid_output)        # -> processed dict
processor.process_file(input_path, output_path)
processor.print_statistics()
processor.seed                                 # the seed in use
```

### `run_postprocessor(deid_output, census_data_dir=None, seed=None)`

Convenience wrapper. Builds a processor per call, so the census data is re-read each
time; prefer `DEIDPostProcessor` for more than one document.

---

## Troubleshooting

**The same replacement appears in two documents.** Expected — see
[Uniqueness](#uniqueness). Most likely on `NAME_GIVEN` or `NAME_FAMILY`, whose pools
are small.

**A name's gender looks wrong.** The lists are US- and Ontario-centric; international
first names often miss both tiers and fall back to a coin flip. A bare surname
(`NAME_FAMILY`) has no first name to look up, so it is ungenderable by construction.

**Results differ between runs.** By design — the seed comes from the clock. Pass
`seed=` to fix it.

**Offsets look wrong.** The processor rewrites `stt_idx_processed` and
`end_idx_processed` only. If you rely on other index fields, they are unchanged.

---

## License

MIT
