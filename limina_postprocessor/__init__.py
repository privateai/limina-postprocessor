"""Limina Postprocessor - replaces DEID name placeholders with synthetic names.

Names are composed at request time from US Census and Ontario name lists in
``data/census_data/``; there is no name dictionary to download.

Two usage patterns:

1. One-shot::

       import limina_postprocessor
       result = limina_postprocessor.run_postprocessor(payload)

2. Reusable, for many documents -- the name lists load once::

       processor = limina_postprocessor.DEIDPostProcessor()
       for doc in documents:
           result = processor.process_document(doc)
"""

from .processor import DEIDPostProcessor

__version__ = '0.2.0'
__all__ = ['run_postprocessor', 'DEIDPostProcessor']


def run_postprocessor(deid_output, census_data_dir=None, seed=None):
    """Replace name entities in one DEID document or a list of them.

    Builds a processor per call, so the census data is re-read each time. For many
    documents, construct a `DEIDPostProcessor` once and call `process_document`.

    Args:
        deid_output: DEID JSON output -- a single document or a list of them.
        census_data_dir: Override the packaged census data directory.
        seed: Reproduce a previous run. Omitted, names are seeded from the clock.

    Returns:
        The processed document, or a list of them, matching the input shape.
    """
    processor = DEIDPostProcessor(census_data_dir=census_data_dir, seed=seed)

    if isinstance(deid_output, list):
        return [processor.process_document(doc) for doc in deid_output]
    return processor.process_document(deid_output)
