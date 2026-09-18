#!/usr/bin/env python3
"""Performance benchmarks for the DEID post-processor.

Run from the repo root:

    python3 tests/benchmark.py                # ~2 min, the default suite
    python3 tests/benchmark.py --quick        # ~10s, smoke test
    python3 tests/benchmark.py --names 50000  # longer memory-growth run
    python3 tests/benchmark.py --target 700e6 # project to a volume

Needs the real 3.8 GB dictionary; exits with a message if it is absent. Not a pytest
module — the filename does not match test_*.py, so pytest will not collect it.

Output is written to be pasted into a ticket or chat as-is.
"""

import argparse
import contextlib
import gc
import io
import json
import random
import resource
import statistics
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from limina_postprocessor.handlers.name_handler import NameHandler   # noqa: E402
from limina_postprocessor.processor import DEIDPostProcessor         # noqa: E402

DICTIONARY = REPO_ROOT / "limina_postprocessor" / "data" / "name_dictionary_1b_filtered.parquet"

# ru_maxrss is bytes on macOS and kilobytes on Linux.
RSS_SCALE = 1.0 if sys.platform == "darwin" else 1024.0

# Probe names spread across genders so pools are exercised the way real data does.
PROBE_NAMES = ["John Smith", "Mary Jones", "Robert Lee", "Linda Diaz", "James Chen"]

FILLER = "Seen in clinic today for follow-up. "

# Documents per variant in the length and repeat-mention sweeps. One is spent warming up.
DOCS_PER_VARIANT = 20


def peak_rss_mb():
    """Peak RSS since process start. Never decreases, and on this workload it
    includes memory-mapped dictionary pages, so it is not a clean measure of the
    uniqueness set. Reported for context only; set_bytes and set_table_bytes are
    what the projection uses."""
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * RSS_SCALE / 1e6


def set_bytes(name_set):
    """Footprint of the uniqueness set, split into its two parts.

    RSS cannot isolate this, because memory-mapped parquet pages grow in the same
    counter. Measuring the structure directly works at any sample size.

    Returned separately because only one of them can be extrapolated: the strings
    scale linearly, while the hash table is a step function of n (see
    set_table_bytes) and must be modelled at the target size instead.
    """
    table = sys.getsizeof(name_set)
    strings = sum(sys.getsizeof(s) for s in name_set)
    return table, strings


# CPython setentry is a hash plus a pointer. The base is the PySetObject struct,
# which includes an inline 8-slot table used until the set outgrows it.
SET_SLOT_BYTES = 16
SET_BASE_BYTES = sys.getsizeof(set())


def set_table_bytes(n):
    """Predicted size of the hash table backing a set of n strings.

    CPython grows a set when the table is 60% full, to the first power of two
    above used*2 (used*4 below ~50k entries). So bytes-per-element is not a
    constant: it swings between 2.1 and 6.6 slots depending on where n falls
    relative to the next boundary.

    That is why this is modelled rather than measured-and-multiplied. Sampling
    the table at n=20,000 lands on the ratio's worst point (6.55 slots/element)
    and, extrapolated, overstated a 700M-name run by roughly 40 GB. The strings
    are extrapolated; the table is computed.

    The final size cannot be derived from n alone: the last resize fires at a
    'used' count below n, and sizes itself from *that* count. So walk the resize
    sequence, which converges in about 30 steps for any realistic n. Validated
    against sys.getsizeof by bench_memory at runtime.
    """
    slots = 8
    while True:
        # CPython resizes after an insert leaves fill*5 >= mask*3.
        trigger = -(-3 * (slots - 1) // 5)
        if n < trigger:
            break
        minused = trigger * 4 if trigger <= 50_000 else trigger * 2
        grown = 8
        while grown <= minused:
            grown <<= 1
        if grown <= slots:                  # no growth possible; stop
            break
        slots = grown
    if slots <= 8:                          # still in the inline smalltable
        return SET_BASE_BYTES
    return SET_BASE_BYTES + slots * SET_SLOT_BYTES


def quiet():
    """Swallow the constructor's banner so the report stays readable."""
    return contextlib.redirect_stdout(io.StringIO())


def new_processor():
    """A processor with a uniqueness set of its own, banner suppressed."""
    with quiet():
        return DEIDPostProcessor(name_dictionary_path=str(DICTIONARY), enable_names=True)


def time_docs(processor, docs):
    """Mean ms/document over docs[1:], spending docs[0] as a warm-up."""
    processor.process_document(docs[0])
    start = time.perf_counter()
    for doc in docs[1:]:
        processor.process_document(doc)
    return (time.perf_counter() - start) / (len(docs) - 1) * 1000


def make_doc(names, mentions=1, pad_chars=0):
    """Build a DEID-shaped document with correct indices.

    names:    distinct people in the document
    mentions: how many times each name appears
    pad_chars: extra prose appended, to vary document length independently of names
    """
    chunks, entities, pos = [], [], 0
    for _ in range(mentions):
        for name in names:
            chunks.append(FILLER)
            pos += len(FILLER)
            chunks.append(name)
            entities.append({
                "text": name,
                "processed_text": name,
                "best_label": "NAME",
                "location": {"stt_idx": pos, "end_idx": pos + len(name),
                             "stt_idx_processed": pos, "end_idx_processed": pos + len(name)},
            })
            pos += len(name)
    text = "".join(chunks)
    if pad_chars:
        text += "x" * pad_chars
    return {"processed_text": text, "text": text, "entities": entities}


def unique_names(count, seed=0):
    """Distinct originals, so no cache absorbs a draw and every name costs a real one.

    The seed goes into the surname, so two calls never collide. Without it only the
    first name varied, leaving documents drawn from just 8 x count combinations —
    harmless today because process_document clears the per-document cache on entry,
    but it would silently overstate throughput if that ever stopped being true.
    """
    rng = random.Random(seed)
    firsts = ["John", "Mary", "Robert", "Linda", "James", "Susan", "David", "Karen"]
    return [f"{rng.choice(firsts)} Surname{seed}x{i}" for i in range(count)]


def header(title):
    print(f"\n{title}")
    print("-" * len(title))


# --------------------------------------------------------------------------- #
# benchmarks
# --------------------------------------------------------------------------- #

def bench_init():
    header("Initialization")
    times, handler = [], None
    for _ in range(3):
        handler = None              # release the previous mapping before timing the next
        gc.collect()
        start = time.perf_counter()
        handler = NameHandler(str(DICTIONARY))       # the last one is returned to the caller
        times.append(time.perf_counter() - start)
    print(f"  construct NameHandler : {min(times) * 1000:7.1f} ms  (best of 3)")
    print(f"  dictionary rows       : {handler.dictionary_rows:,}")
    print(f"  row groups            : {handler.parquet.num_row_groups:,}")
    print(f"  mixed-gender groups   : {len(handler.mixed_row_groups):,}")
    print("  (reads file metadata only; the 3.8 GB file is memory-mapped, not loaded)")
    return handler


def bench_per_name(handler, draws):
    header(f"Per generated name, by entity type ({draws:,} draws each)")
    print(f"  {'entity type':<28}{'median':>10}{'mean':>10}{'p95':>10}")
    results = {}
    for entity_type in ("NAME", "NAME_GIVEN", "NAME_FAMILY"):
        handler._generate_name(entity_type, PROBE_NAMES[0])          # warm the page cache
        times = []
        for i in range(draws):
            probe = PROBE_NAMES[i % len(PROBE_NAMES)]
            start = time.perf_counter()
            handler._generate_name(entity_type, probe)
            times.append((time.perf_counter() - start) * 1000)
        times.sort()
        median = statistics.median(times)
        results[entity_type] = median
        p95 = times[int(len(times) * 0.95)]
        print(f"  {entity_type:<28}{median:>9.3f}ms{statistics.mean(times):>9.3f}ms{p95:>9.3f}ms")
    return results


def bench_documents(docs_count, names_per_doc):
    header(f"Document throughput ({docs_count} docs x {names_per_doc} distinct names)")
    processor = new_processor()
    docs = [make_doc(unique_names(names_per_doc, seed=i)) for i in range(docs_count)]

    processor.process_document(make_doc(unique_names(names_per_doc, seed=9999)))   # warm-up

    start = time.perf_counter()
    for doc in docs:
        processor.process_document(doc)
    elapsed = time.perf_counter() - start

    total_names = docs_count * names_per_doc
    print(f"  total time            : {elapsed:7.2f} s")
    print(f"  per document          : {elapsed / docs_count * 1000:7.2f} ms")
    print(f"  per name              : {elapsed / total_names * 1000:7.3f} ms")
    print(f"  throughput            : {total_names / elapsed:7.0f} names/sec  (single core)")
    return elapsed / total_names * 1000


def bench_doc_size():
    header("Does document length matter? (5 names, text padded)")
    processor = new_processor()
    print(f"  {'text chars':>12}{'ms/doc':>12}")
    for pad in (0, 5_000, 50_000, 400_000):
        docs = [make_doc(unique_names(5, seed=pad * 100 + i), pad_chars=pad)
                for i in range(DOCS_PER_VARIANT)]
        per_doc = time_docs(processor, docs)
        print(f"  {len(docs[1]['processed_text']):>12,}{per_doc:>11.2f}ms")
    print("  (cost tracks distinct names, not text length)")


def bench_repeat_mentions():
    header("Do repeated mentions of the same person cost extra?")
    processor = new_processor()
    print(f"  {'mentions each':>14}{'entities':>10}{'ms/doc':>12}")
    for mentions in (1, 2, 5, 10):
        docs = [make_doc(unique_names(5, seed=mentions * 100 + i), mentions=mentions)
                for i in range(DOCS_PER_VARIANT)]
        per_doc = time_docs(processor, docs)
        print(f"  {mentions:>14}{len(docs[1]['entities']):>10}{per_doc:>11.2f}ms")
    print("  (the per-document cache absorbs repeats; only distinct names cost a draw)")


def bench_memory(names):
    header(f"Memory growth over {names:,} names")
    gc.collect()
    with quiet():
        handler = NameHandler(str(DICTIONARY))
    handler._generate_name("NAME", PROBE_NAMES[0])          # warm the page cache

    # Baseline AFTER the warm-up draw, so the delta counts only this run's names.
    set_before = len(handler.used_names_global)
    table_before, strings_before = set_bytes(handler.used_names_global)

    checkpoints = [n for n in (10_000, 50_000, 100_000, 250_000, 500_000, 1_000_000)
                   if n <= names]
    if names not in checkpoints:
        checkpoints.append(names)

    print(f"  {'names':>12}{'strings':>11}{'B/name':>9}{'+table':>10}"
          f"{'total':>10}{'peak RSS':>11}{'elapsed':>9}")
    start = time.perf_counter()
    emitted, distinct, per_name = 0, 0, 0.0
    for target in checkpoints:
        while emitted < target:
            handler._generate_name("NAME", PROBE_NAMES[emitted % len(PROBE_NAMES)])
            emitted += 1
        table, strings = set_bytes(handler.used_names_global)
        table -= table_before
        strings -= strings_before
        # Per *retained* name, not per draw: a duplicate draw costs no memory, so
        # dividing by emitted would understate the projection once a pool runs dry.
        # Only the strings are extrapolated; the table is modelled at the target.
        distinct = len(handler.used_names_global) - set_before
        per_name = strings / distinct
        print(f"  {emitted:>12,}{strings / 1e6:>10.1f}M{per_name:>8.1f}B"
              f"{table / 1e6:>9.1f}M{(strings + table) / 1e6:>9.1f}M"
              f"{peak_rss_mb():>10.1f}M{time.perf_counter() - start:>8.1f}s")

    # The last checkpoint is always `names`, so these carry the whole run.
    duplicates = emitted - distinct
    print(f"\n  distinct emitted      : {distinct:,} of {emitted:,} draws")
    print(f"  duplicates            : {duplicates:,}")
    if duplicates:
        print("  WARNING: a pool ran out - uniqueness no longer holds past this point")

    # Guard the projection's table model against this interpreter, rather than
    # trusting it. A mismatch means set growth changed and the model needs a look.
    measured = sys.getsizeof(handler.used_names_global)
    predicted = set_table_bytes(len(handler.used_names_global))
    verdict = "matches" if measured == predicted else f"MISMATCH (model {predicted:,})"
    print(f"  set table model check : {measured:,} B measured, {verdict}")

    print("\n  B/name is the retained strings only. It is the one figure that")
    print("  extrapolates: it holds flat across sample sizes. '+table' is the hash")
    print("  table, a step function of n, so the projection below computes it at the")
    print("  target size instead of scaling this sample. Peak RSS exceeds both because")
    print("  it counts memory-mapped dictionary pages the OS can reclaim.")
    return per_name


def project(strings_per_name, ms_per_name, target):
    header(f"Projection to {target:,.0f} names")
    seconds = target * ms_per_name / 1000
    print(f"  single core           : {seconds / 3600:9.1f} h  ({seconds / 86400:.1f} days)")
    for workers in (8, 16, 32, 64):
        print(f"  {workers:>2} workers            : {seconds / 3600 / workers:9.1f} h")

    strings = target * strings_per_name
    table = set_table_bytes(int(target))
    print("\n  uniqueness set, one process:")
    print(f"    retained name strings     : {strings / 1e9:7.1f} GB  (measured, linear)")
    print(f"    set hash table            : {table / 1e9:7.1f} GB  "
          f"({(table - SET_BASE_BYTES) // SET_SLOT_BYTES:,} slots, modelled)")
    print(f"    total                     : {(strings + table) / 1e9:7.1f} GB")
    print("  This is one process holding every name emitted so far. It is the hard")
    print("  constraint on a single-machine run, not CPU time.")
    print("  NOTE: workers do not share the uniqueness set, so parallel runs can")
    print("        emit the same name twice. Partitioning row groups across workers")
    print("        gets most of the way to a fix, but full_name is unique per gender")
    print("        rather than globally, so it is not a complete answer on its own.")
    return (strings + table) / target


# --------------------------------------------------------------------------- #

def check_dictionary():
    """Exit with instructions unless the real dictionary is present; return its size in GB.

    Binary GB, so the figure matches what `ls -lh` shows and what the docs quote (~3.8 GB).
    Decimal GB would report 4.04 and contradict the 3.8 GB mentioned elsewhere in the run.
    """
    if not DICTIONARY.exists():
        sys.exit(f"dictionary not found: {DICTIONARY}\n"
                 f"run 'git lfs pull' from the repo root first")
    size_gb = DICTIONARY.stat().st_size / 2**30
    if size_gb < 3:
        sys.exit(f"dictionary is {size_gb:.2f} GB, expected ~3.8 GB - looks like a Git LFS "
                 f"pointer.\nrun 'git lfs install && git lfs pull'")
    return size_gb


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--quick", action="store_true", help="fast smoke run (~10s)")
    parser.add_argument("--names", type=int, default=None,
                        help="names for the memory-growth run (default 20000, 2000 with --quick)")
    parser.add_argument("--target", type=float, default=700e6,
                        help="volume to project to (default 700e6)")
    parser.add_argument("--seed", type=int, default=20260917)
    parser.add_argument("--json", metavar="PATH", help="also write a machine-readable summary")
    args = parser.parse_args()

    size_gb = check_dictionary()

    random.seed(args.seed)
    draws = 100 if args.quick else 500
    docs_count = 20 if args.quick else 100
    mem_names = args.names or (2_000 if args.quick else 20_000)

    print("=" * 66)
    print("DEID POST-PROCESSOR BENCHMARK")
    print("=" * 66)
    print(f"  python     : {sys.version.split()[0]}")
    print(f"  platform   : {sys.platform}")
    print(f"  dictionary : {size_gb:.2f} GB")
    print(f"  seed       : {args.seed}")

    handler = bench_init()
    per_type = bench_per_name(handler, draws)
    ms_per_name = bench_documents(docs_count, names_per_doc=5)
    if not args.quick:
        bench_doc_size()
        bench_repeat_mentions()
    strings_per_name = bench_memory(mem_names)
    at_target = project(strings_per_name, ms_per_name, args.target)

    print("\n" + "=" * 66)
    print("Headline: "
          f"{ms_per_name:.2f} ms per distinct name, "
          f"{1000 / ms_per_name:.0f} names/sec/core, "
          f"{at_target:.0f} bytes per name at {args.target:,.0f}")
    print("=" * 66)

    if args.json:
        Path(args.json).write_text(json.dumps({
            "python": sys.version.split()[0],
            "platform": sys.platform,
            "ms_per_name_by_type": per_type,
            "ms_per_name_document_path": ms_per_name,
            "names_per_sec_per_core": 1000 / ms_per_name,
            "string_bytes_per_name": strings_per_name,
            "bytes_per_name_at_target": at_target,
            "target_names": args.target,
            "target_set_bytes": args.target * strings_per_name + set_table_bytes(int(args.target)),
            "memory_run_names": mem_names,
        }, indent=2))
        print(f"wrote {args.json}")


if __name__ == "__main__":
    main()
