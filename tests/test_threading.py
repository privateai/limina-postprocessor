#!/usr/bin/env python3
"""Tests for process_documents / iter_documents.

Covers the properties threading could plausibly break: per-document coreference,
cross-document uniqueness, statistics accuracy, input ordering, and the streaming
contract. Requires the real 3.8GB dictionary, so these are skipped when it is absent.

On what these tests can and cannot prove, established by removing each guard and
re-running:

  - Sharing the per-document cache across threads breaks coreference immediately and
    visibly (102 originals got two different replacements over 40 documents), so
    test_coreference_holds_within_each_document is a real detector.
  - Removing the statistics lock loses counts only under thread-switch pressure (2 of
    3,600 with sys.setswitchinterval(1e-6)), not at default settings.
  - Removing the uniqueness lock could not be made to produce a duplicate through this
    code path at all, because a collision needs two threads to draw the same candidate
    from a 1.096B pool inside the same ~100ns window while both are otherwise in
    pyarrow's C++ decode with the GIL released. The race is nonetheless real: the same
    check-then-act over a shared 50,000-name candidate list yields ~125 double claims
    across 8 threads unguarded, and 0 guarded.

So treat the uniqueness and statistics assertions here as regression guards on behaviour,
not as race detectors. They will not catch a lock someone deletes.
"""
import itertools
import random
import threading

import pytest

from limina_postprocessor import DEFAULT_DICTIONARY
from limina_postprocessor.processor import DEIDPostProcessor

pytestmark = pytest.mark.skipif(
    not __import__('pathlib').Path(DEFAULT_DICTIONARY).exists(),
    reason="name dictionary not downloaded (see README: Git LFS)",
)

FILLER = "Seen in clinic today for follow-up. "
WORKER_COUNTS = [1, 2, 4, 8]


def make_doc(doc_id, people=3, mentions=3):
    """A document naming `people` distinct people, each mentioned `mentions` times.

    Mentioning each person more than once is what exercises coreference: every mention
    of one original must come back as the same replacement.
    """
    names = [f"John Surname{doc_id}x{person}" for person in range(people)]
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
                "location": {
                    "stt_idx": pos, "end_idx": pos + len(name),
                    "stt_idx_processed": pos, "end_idx_processed": pos + len(name),
                },
            })
            pos += len(name)
    text = "".join(chunks)
    return {"doc_id": doc_id, "processed_text": text, "text": text, "entities": entities}


@pytest.fixture
def processor(capsys):
    """A fresh processor, so statistics start at zero and are exactly predictable."""
    instance = DEIDPostProcessor(name_dictionary_path=DEFAULT_DICTIONARY)
    capsys.readouterr()          # __init__ prints a banner
    return instance


def replacements_by_original(document):
    """Map each original name in a processed document to the set of its replacements."""
    mapping = {}
    for entity in document["entities"]:
        mapping.setdefault(entity["text"], set()).add(entity["processed_text"])
    return mapping


# --- core guarantees, at every worker count --------------------------------------

@pytest.mark.parametrize("workers", WORKER_COUNTS)
def test_coreference_holds_within_each_document(processor, workers):
    """Every mention of one original name must get the same replacement."""
    docs = [make_doc(i) for i in range(24)]

    for result in processor.process_documents(docs, workers=workers):
        for original, replacements in replacements_by_original(result).items():
            assert len(replacements) == 1, (
                f"doc {result['doc_id']}: {original!r} got {replacements}")


@pytest.mark.parametrize("workers", WORKER_COUNTS)
def test_no_name_is_reused_across_documents(processor, workers):
    """The uniqueness guarantee: a replacement is never handed out twice."""
    docs = [make_doc(i) for i in range(24)]

    results = processor.process_documents(docs, workers=workers)
    emitted = [
        next(iter(replacements))
        for result in results
        for replacements in replacements_by_original(result).values()
    ]

    assert len(emitted) == 24 * 3
    assert len(set(emitted)) == len(emitted), "a replacement was reused"


@pytest.mark.parametrize("workers", WORKER_COUNTS)
def test_statistics_lose_no_counts(processor, workers):
    """`+= 1` is not atomic; concurrent documents must not drop increments."""
    docs = [make_doc(i) for i in range(24)]
    expected = 24 * 3 * 3          # docs x people x mentions

    processor.process_documents(docs, workers=workers)

    assert processor.stats["total_entities"] == expected
    assert processor.stats["entities_replaced"] == expected
    assert processor.stats["entity_types"]["NAME"] == expected
    assert processor.stats["replacements_by_handler"]["NameHandler"] == expected


@pytest.mark.parametrize("workers", WORKER_COUNTS)
def test_results_come_back_in_input_order(processor, workers):
    """Threads finish out of order; the caller must not see that."""
    docs = [make_doc(i) for i in range(24)]

    results = processor.process_documents(docs, workers=workers)

    assert [r["doc_id"] for r in results] == list(range(24))


def test_workers_one_is_reproducible():
    """Documented behaviour: workers=1 reruns byte-identically, threads do not."""
    def run():
        random.seed(4242)
        processor = DEIDPostProcessor(name_dictionary_path=DEFAULT_DICTIONARY)
        docs = [make_doc(i) for i in range(8)]
        return [
            entity["processed_text"]
            for document in processor.process_documents(docs, workers=1)
            for entity in document["entities"]
        ]

    assert run() == run()


# --- streaming contract ------------------------------------------------------------

def test_iter_documents_does_not_consume_its_whole_input(processor):
    """The point of iter_documents: an input larger than memory must still work."""
    produced = itertools.count()
    endless = (make_doc(i) for i in produced)

    taken = list(itertools.islice(processor.iter_documents(endless, workers=4), 12))

    consumed = next(produced)
    window = 4 * processor.QUEUE_DEPTH_PER_WORKER
    assert len(taken) == 12
    assert consumed <= 12 + window + 2, f"consumed {consumed} inputs — not streaming"
    assert [d["doc_id"] for d in taken] == list(range(12))


def test_iter_documents_accepts_a_generator_and_preserves_order(processor):
    doc_ids = list(range(30))

    results = list(processor.iter_documents(
        (make_doc(i) for i in doc_ids), workers=4))

    assert [r["doc_id"] for r in results] == doc_ids


def test_abandoning_the_iterator_does_not_hang(processor):
    """Breaking out early must shut the pool down rather than block forever."""
    for result in processor.iter_documents((make_doc(i) for i in range(500)), workers=4):
        assert result["doc_id"] == 0
        break


def test_process_documents_returns_a_list(processor):
    """iter_documents yields; process_documents must still materialize."""
    results = processor.process_documents([make_doc(i) for i in range(4)], workers=4)

    assert isinstance(results, list)
    assert len(results) == 4


# --- argument handling and edge cases ---------------------------------------------

@pytest.mark.parametrize("workers", [0, -1])
def test_bad_worker_count_raises_at_the_call_site(processor, workers):
    """Not on the first next(), which would surface the error far from the mistake."""
    with pytest.raises(ValueError, match="workers must be >= 1"):
        processor.iter_documents([make_doc(0)], workers=workers)


def test_default_worker_count_is_capped(processor):
    assert processor._resolve_workers(None) <= processor.DEFAULT_MAX_WORKERS
    assert processor._resolve_workers(None) >= 1
    assert processor._resolve_workers(32) == 32


@pytest.mark.parametrize("workers", WORKER_COUNTS)
def test_empty_input(processor, workers):
    assert processor.process_documents([], workers=workers) == []


@pytest.mark.parametrize("workers", WORKER_COUNTS)
def test_single_document(processor, workers):
    results = processor.process_documents([make_doc(0)], workers=workers)

    assert len(results) == 1
    assert len(replacements_by_original(results[0])) == 3


def test_more_workers_than_documents(processor):
    results = processor.process_documents([make_doc(i) for i in range(2)], workers=8)

    assert [r["doc_id"] for r in results] == [0, 1]


def test_documents_without_entities_pass_through(processor):
    docs = [{"doc_id": 0, "text": "No names here.", "entities": []},
            make_doc(1),
            {"doc_id": 2, "text": "Nor here."}]

    results = processor.process_documents(docs, workers=4)

    assert [r["doc_id"] for r in results] == [0, 1, 2]
    assert results[0]["entities"] == []


def test_worker_exception_propagates_to_the_caller(processor):
    """A bad document must surface as an error, not be silently dropped."""
    docs = [make_doc(0), None, make_doc(2)]

    with pytest.raises(AttributeError):
        processor.process_documents(docs, workers=4)


def test_each_thread_reads_through_its_own_parquet_reader(processor):
    """No two threads may share a pq.ParquetFile.

    A shared reader carries mutable state across read_row_group, so concurrent readers
    can invalidate each other and raise "ReadRangeCache did not find matching cache
    entry". That shows up only when pyarrow has pre-buffering on — default since 25,
    off through 21 — and even then only when two threads collide, so it passed locally
    and failed on a newer install. Asserted structurally rather than by hammering the
    decode, so the guard does not depend on which pyarrow is installed or on winning a
    race.
    """
    handler = processor.handlers[0]
    readers = {}

    def record(name):
        readers[name] = id(handler._reader())

    threads = [threading.Thread(target=record, args=(i,)) for i in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(set(readers.values())) == len(readers), "threads shared a parquet reader"
    assert id(handler.parquet) not in set(readers.values()), \
        "a worker thread reused the reader owned by the constructing thread"


def test_constructing_thread_does_not_open_a_second_reader(processor):
    """The reader opened in __init__ is the one a single-threaded caller uses.

    workers=1 takes no pool and runs on the calling thread, so opening a second reader
    there would be pure cost on the path that gains nothing from threading.
    """
    handler = processor.handlers[0]

    assert handler._reader() is handler.parquet


def test_processor_can_be_reused_across_calls(processor):
    """Uniqueness must span calls, not just documents within one call."""
    first = processor.process_documents([make_doc(i) for i in range(6)], workers=4)
    second = processor.process_documents([make_doc(i) for i in range(6, 12)], workers=4)

    emitted = [
        next(iter(replacements))
        for result in first + second
        for replacements in replacements_by_original(result).values()
    ]
    assert len(set(emitted)) == len(emitted), "a name was reused between calls"
