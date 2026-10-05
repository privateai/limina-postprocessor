#!/usr/bin/env python3
"""
Threaded batching on live DEID output: DEID container → threaded post-process → verify.

Same pipeline as `sample_inline_deid_postprocess.py`, but where that script post-processes
the container's output one document at a time in a loop, this one hands the whole batch to
the thread pool. The input is real DEID output — nothing synthesized.

That difference is the point. DEID naturally returns a batch (you post `{"text": [...]}` and
get an array back), so a DEID pipeline already has many documents in hand at once, which is
exactly the shape threading pays off on. The common mistake is to receive that array and
then loop over it, which throws the speedup away; this script times that too so you can see
the cost.

The corpus, the container payload and the quality checks are imported from
`sample_inline_deid_postprocess.py` so both samples stay in step.

Usage:
    python3 sample_threaded_deid_postprocess.py
    python3 sample_threaded_deid_postprocess.py --texts 100 --workers 8
    python3 sample_threaded_deid_postprocess.py --refresh        # re-call the container

Requirements:
    pip install requests
    A Limina DEID container at http://localhost:8080 (first run only — see --refresh)

Input:  live DEID container output, cached to sample_output_threaded_deid/deid_raw.json
Output: sample_output_threaded_deid/processed.jsonl
"""

import argparse
import json
import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, ".")
import limina_postprocessor
from sample_inline_deid_postprocess import DEID_API_URL, SAMPLE_TEXTS, call_deid_api_batch, verify_output

OUTPUT_FOLDER = Path("sample_output_threaded_deid")
CACHE_PATH = OUTPUT_FOLDER / "deid_raw.json"
OUTPUT_PATH = OUTPUT_FOLDER / "processed.jsonl"
FAILURE_REPORT_PATH = OUTPUT_FOLDER / "failures.json"

# Texts per container call. Matches sample_inline_deid_postprocess.py so the two scripts
# put the same load on the container.
DEID_BATCH_SIZE = 10

# Thresholds for reporting ratios at all. Below either one the run is too short to measure:
# the dictionary buffers a cursor per name type and gender, and one cold row-group decode
# outweighs all the real work, which pushes ratios above the min(workers, documents) ceiling.
# Suppressed rather than reported, so the numbers never contradict the point being made.
#
# Both are needed. Documents are the unit of parallelism, but entities are the unit of work,
# and the two come apart: a sparse DEID run of 100 documents holding 4 entities between them
# measured 31.9x against an 8x ceiling, because there was almost nothing to parallelize.
MEANINGFUL_CORPUS = 50
MEANINGFUL_ENTITIES = 250

MAX_ISSUES_SHOWN = 15


# ------------------------------------------------------------------ DEID container

def fetch_from_container(texts, batch_size):
    """Call the DEID container in batches, returning its documents as one flat list.

    The container returns an array per call. Flattening those arrays into a single list of
    documents is what lets the thread pool treat one document as one unit of work.

    Returns None if any batch fails, since a partial corpus would make the timings and the
    uniqueness check meaningless.
    """
    documents = []
    batches = range(0, len(texts), batch_size)

    for batch_num, start in enumerate(batches, 1):
        batch = texts[start : start + batch_size]
        print(f"   Batch {batch_num}/{len(batches)}...", end="", flush=True)

        result = call_deid_api_batch(batch, batch_size=batch_size)
        if not result:
            print(" ❌ failed")
            return None

        documents.extend(result)
        print(f" ✅ ({len(result)} texts)")

    return documents


def load_deid_output(texts, batch_size, refresh):
    """Real DEID output for `texts`, from the cache when it already matches.

    Cached with the texts it came from, not on its own: the honorific check needs each
    document paired with the text that produced it, so a cache that had drifted out of
    alignment with the corpus would verify the wrong pairs rather than fail outright.
    """
    if not refresh and CACHE_PATH.exists():
        cached = json.loads(CACHE_PATH.read_text())
        if cached.get("texts") == texts:
            print(f"♻️  Reusing cached DEID output from '{CACHE_PATH}' (--refresh to re-call)")
            return cached["documents"]
        print("♻️  Cached DEID output does not match the requested texts; re-calling")

    print(f"📤 Calling DEID at {DEID_API_URL} — {len(texts)} texts in batches of {batch_size}")
    documents = fetch_from_container(texts, batch_size)
    if documents is None:
        return None

    CACHE_PATH.write_text(json.dumps({"texts": texts, "documents": documents}))
    print(f"💾 Saved raw DEID output to '{CACHE_PATH}'")
    return documents


# ------------------------------------------------------------------ measurement

def fresh(documents):
    """A deep copy, so each timed pattern starts from identical untouched DEID output.

    process_document mutates the document it is given, so without this the second pattern
    would be re-processing the first one's results.
    """
    return json.loads(json.dumps(documents))


def time_it(fn, documents):
    start = time.perf_counter()
    result = fn(fresh(documents))
    return result, time.perf_counter() - start


def stream_to_disk(processor, documents, workers):
    """Post-process streaming, writing one JSON document per line as results arrive.

    The pattern to copy for a real backfill: `documents` may be a generator over more data
    than fits in memory, and only workers * QUEUE_DEPTH_PER_WORKER are ever held at once.
    """
    written = 0
    with open(OUTPUT_PATH, "w") as out:
        for processed in processor.iter_documents(iter(documents), workers=workers):
            out.write(json.dumps(processed) + "\n")
            written += 1
    return written


def load_processed(path):
    """Read the streamed output back, so verification checks the file on disk.

    Each timed pattern draws its own names, so verifying one run's in-memory result while
    reporting line numbers into another run's file would name replacements that are not on
    that line. Reading the artifact back makes `line N` exact by construction — and it is
    the output the client actually consumes.
    """
    with open(path) as f:
        return [json.loads(line) for line in f]


# ------------------------------------------------------------------ verification

# Entity types whose replacement is a whole drawn name rather than one component of one.
FULL_NAME_LABELS = frozenset({"NAME", "NAME_MEDICAL_PROFESSIONAL"})


def bare_name(name):
    """Drop a leading honorific, so "Dr. Ann Lee" and "Ann Lee" compare as the same name."""
    first, _, rest = name.partition(" ")
    return rest if rest and first.endswith(".") else name


def check_cross_document_uniqueness(processed):
    """No *drawn* replacement name may appear in two different documents.

    The guarantee threading is most likely to break, and the reason this uses threads rather
    than processes: one shared used_names_global across threads, where separate processes
    would each get their own copy and could hand out the same name twice.

    Reported by line number in processed.jsonl, the same way the per-document failures are,
    so both halves of the report point at the output the same way.
    """
    issues = []
    seen = {}

    for doc_i, document in enumerate(processed):
        entities = document.get("entities") or []

        # Components of the full names assigned in this document. A bare surname matching
        # one of them came from _smart_match, so it was never added to used_names_global:
        # the package deliberately does not reserve components, and documents that
        # "John Smith" -> "Elmore Whicker" in one document leaves a bare "Whicker"
        # available to another. Checking those would make this stricter than the guarantee
        # and fire at random, whenever a later NAME_FAMILY draw happens to collide inside
        # the ~162K surname pool.
        components = set()
        for entity in entities:
            if entity.get("best_label") in FULL_NAME_LABELS:
                components.update(bare_name(entity.get("processed_text") or "").split())

        drawn = set()
        for entity in entities:
            name = bare_name(entity.get("processed_text") or "")
            if not name:
                continue
            if entity.get("best_label") not in FULL_NAME_LABELS and name in components:
                continue
            drawn.add(name)

        # A set per document, so coreference repeating a name within one document reports
        # the pair once instead of once per mention.
        for name in sorted(drawn):
            if name in seen:
                issues.append(f"'{name}' reused on lines {seen[name] + 1} and {doc_i + 1}")
            else:
                seen[name] = doc_i

    return issues


# Each per-document check, paired with the prefix verify_output gives its messages.
PER_DOCUMENT_CHECKS = (
    ("Index accuracy", "Index mismatch"),
    ("Honorifics", "Honorific"),
    ("Coreference", "Coreference"),
)


def classify(issue):
    """Bucket a per-document issue by the check that produced it.

    Matched on the message prefix, which couples this to verify_output's wording over in
    sample_inline_deid_postprocess.py. Anything unrecognised is counted as "Other" rather
    than dropped, so a reworded message shows up as uncategorised instead of vanishing from
    the totals.
    """
    for label, prefix in PER_DOCUMENT_CHECKS:
        if issue.startswith(prefix):
            return label
    return "Other"


def verify_all(texts, deid_documents, processed):
    """Run the per-document checks plus cross-document uniqueness.

    Returns (per_document, cross_document), where per_document maps a document's index to
    its issues and holds only the documents that failed. Keyed by index rather than
    flattened into one list so the report can say *which* documents are bad: the index is
    the document's line number in processed.jsonl, which is how the client finds it.

    Pairs each document with the text that produced it, which relies on the pool returning
    results in input order — a property worth leaning on here, since silently reordered
    output would show up as spurious honorific failures.
    """
    per_document = {}

    for doc_i, (text, deid_document, result) in enumerate(zip(texts, deid_documents, processed)):
        issues = verify_output(text, deid_document, result)
        if issues:
            per_document[doc_i] = issues

    return per_document, check_cross_document_uniqueness(processed)


# ------------------------------------------------------------------ reporting

def report_timings(timings, document_count, measurable):
    print(f"\n{'=' * 78}")
    print(f"POST-PROCESSING TIMINGS ({document_count} documents from the DEID container)")
    print("=" * 78)

    baseline = timings[0][1]
    print(f"{'pattern':<52}{'time':>9}{'vs serial':>12}")
    print("-" * 78)
    for label, seconds in timings:
        ratio = f"{baseline / seconds:10.2f}x" if measurable else f"{'n/a':>11}"
        print(f"{label:<52}{seconds:8.2f}s{ratio}")

    print("\nThe DEID container hands you an array. Post-processing it in one call is the")
    print("whole difference between the second row and the last one.")


def report_verification(per_document, cross_document, processed):
    """Print the pass/fail summary, and on failure say which documents and write a report."""
    print(f"\n{'=' * 78}")
    print("VERIFICATION (threaded output, against the original texts)")
    print("=" * 78)

    total = len(processed)
    failing = len(per_document)
    entity_count = sum(len(d.get("entities") or []) for d in processed)
    print(f"{total} documents, {entity_count} entities")

    print(f"\n{'documents passed':<24}{total - failing:>6} / {total}")
    print(f"{'documents failed':<24}{failing:>6} / {total}" + (f"   ({failing / total:.1%})" if total else ""))
    print(f"{'cross-document issues':<24}{len(cross_document):>6}")

    if not per_document and not cross_document:
        print("\n✅ Index accuracy       - every span slices back to its entity")
        print("✅ Honorifics           - Dr./Mr./Mrs./Ms./Prof./Jr./Sr. counts unchanged from the original")
        print("✅ Coreference          - one original name, one replacement per document")
        print("✅ Cross-doc uniqueness - no replacement name reused between documents")
        return

    counts = Counter(classify(issue) for issues in per_document.values() for issue in issues)
    if cross_document:
        counts["Cross-doc uniqueness"] = len(cross_document)

    print("\n❌ Failures by check:")
    for label, count in counts.most_common():
        print(f"     {label:<24}{count:>5}")

    if per_document:
        # The index is the document's line in processed.jsonl, so this is enough to go and
        # look at the offending output directly.
        print(f"\n❌ Failing documents (by line number in {OUTPUT_PATH.name}):")
        for doc_i, issues in sorted(per_document.items())[:MAX_ISSUES_SHOWN]:
            print(f"     line {doc_i + 1:<5} {len(issues)} issue(s)  {issues[0]}")
        if failing > MAX_ISSUES_SHOWN:
            print(f"     ... and {failing - MAX_ISSUES_SHOWN} more failing documents")

    if cross_document:
        # Listed separately because these are not attributable to one document: a reused name
        # implicates the pair, so neither line is wrong on its own.
        print("\n❌ Reused replacement names (names appearing in more than one document):")
        for issue in cross_document[:MAX_ISSUES_SHOWN]:
            print(f"     {issue}")
        if len(cross_document) > MAX_ISSUES_SHOWN:
            print(f"     ... and {len(cross_document) - MAX_ISSUES_SHOWN} more")

    # Written out because the console list is truncated, and a client triaging a large run
    # needs every failure rather than the first few.
    report = {
        "documents_total": total,
        "documents_failed": failing,
        "failures_by_check": dict(counts),
        "per_document": {f"line_{doc_i + 1}": issues for doc_i, issues in sorted(per_document.items())},
        "cross_document": cross_document,
    }
    FAILURE_REPORT_PATH.write_text(json.dumps(report, indent=2))
    print(f"\n📄 Full failure report: '{FAILURE_REPORT_PATH}'")


# ------------------------------------------------------------------ main

def parse_args():
    parser = argparse.ArgumentParser(description="Threaded batching over live DEID container output")
    parser.add_argument("--texts", type=int, default=len(SAMPLE_TEXTS), help="how many sample texts to send (default: all %(default)s)")
    parser.add_argument("--workers", type=int, default=8, help="post-processing thread count (default: %(default)s)")
    parser.add_argument("--batch-size", type=int, default=DEID_BATCH_SIZE, help="texts per DEID call (default: %(default)s)")
    parser.add_argument("--refresh", action="store_true", help="ignore the cache and call the container again")
    parser.add_argument("--skip-mistake", action="store_true", help="skip timing the one-call-per-document mistake")
    return parser.parse_args()


def main():
    args = parse_args()
    OUTPUT_FOLDER.mkdir(exist_ok=True)

    texts = SAMPLE_TEXTS[: args.texts]

    print("=" * 78)
    print("STEP 1: DEID CONTAINER")
    print("=" * 78)

    deid_documents = load_deid_output(texts, args.batch_size, args.refresh)
    if deid_documents is None:
        print(f"\n❌ Could not get DEID output. Is the container up at {DEID_API_URL}?")
        print("   Check with:")
        print("     curl -X POST --url http://localhost:8080/process/text \\")
        print("       -H 'Content-Type: application/json' -d '{\"text\": [\"Hello John\"]}'")
        return 1

    # One entity-bearing document per input text, in order. Everything downstream pairs the
    # two by index, so a mismatch here would misattribute every verification failure.
    if len(deid_documents) != len(texts):
        print(f"\n❌ DEID returned {len(deid_documents)} documents for {len(texts)} texts; cannot pair them for verification.")
        return 1

    entities_found = sum(len(d.get("entities") or []) for d in deid_documents)
    print(f"\n✅ {len(deid_documents)} DEID documents, {entities_found} entities detected")

    print(f"\n{'=' * 78}")
    print("STEP 2: POST-PROCESSING")
    print("=" * 78)

    # Created ONCE for the whole run, and shared by every worker thread. Uniqueness tracking
    # lives on this instance, so a processor per batch would forget every name issued so far.
    processor = limina_postprocessor.DEIDPostProcessor(name_dictionary_path=limina_postprocessor.DEFAULT_DICTIONARY, enable_names=True)

    # Absorbs the first row-group decode and the gender detector's setup, which would
    # otherwise all land on whichever pattern runs first and inflate every ratio after it.
    processor.process_documents(fresh(deid_documents[: min(8, len(deid_documents))]), workers=2)

    measurable = len(deid_documents) >= MEANINGFUL_CORPUS and entities_found >= MEANINGFUL_ENTITIES
    if not measurable:
        print(f"\n⚠️  {len(deid_documents)} documents holding {entities_found} entities — too little work to")
        print(f"    measure, so ratios are suppressed. Timings worth reading need at least")
        print(f"    {MEANINGFUL_CORPUS} documents and {MEANINGFUL_ENTITIES} entities between them.")

    timings = []

    # Baseline: what sample_inline_deid_postprocess.py does — loop over the DEID array.
    _, seconds = time_it(lambda docs: processor.process_documents(docs, workers=1), deid_documents)
    timings.append(("Serial, one document at a time (workers=1)", seconds))

    # The fix: hand the container's whole array to one call.
    _, seconds = time_it(lambda docs: processor.process_documents(docs, workers=args.workers), deid_documents)
    timings.append((f"process_documents, whole batch, workers={args.workers}", seconds))

    # Same thing streaming, for a backfill too large to hold in memory.
    written, seconds = time_it(lambda docs: stream_to_disk(processor, docs, args.workers), deid_documents)
    timings.append((f"iter_documents streaming, workers={args.workers}", seconds))

    if not args.skip_mistake:
        # The trap specific to a DEID pipeline: the container already returned a batch, and
        # looping over it puts one document in a pool of `workers`, so the rest idle.
        _, seconds = time_it(lambda docs: [processor.process_documents([d], workers=args.workers) for d in docs], deid_documents)
        timings.append(("MISTAKE: looping over the DEID array", seconds))

    report_timings(timings, len(deid_documents), measurable)

    # Verified from the file, not from a timing run's return value — see load_processed.
    processed = load_processed(OUTPUT_PATH)
    per_document, cross_document = verify_all(texts, deid_documents, processed)
    report_verification(per_document, cross_document, processed)

    print("\n📝 EXAMPLE (first document)")
    print(f"   DEID said:      {deid_documents[0].get('processed_text', '')[:110]}")
    print(f"   Post-processed: {processed[0].get('processed_text', '')[:110]}")
    for entity in (processed[0].get("entities") or [])[:4]:
        print(f"     {entity.get('text')} → {entity.get('processed_text')}")

    print(f"\n📁 Streamed {written} documents to '{OUTPUT_PATH}'")
    processor.print_statistics()

    # Non-zero when anything failed, so this can gate a pipeline step rather than only
    # being read by eye.
    return 1 if (per_document or cross_document) else 0


if __name__ == "__main__":
    sys.exit(main())
