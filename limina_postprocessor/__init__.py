"""Limina Postprocessor - replaces DEID name placeholders with synthetic names.

Names are composed at request time from US Census and Ontario name lists in
``data/census_data/``, with famous names excluded by a prebuilt blocklist index.
There is no name dictionary to download.

Two usage patterns:

1. One-shot::

       import limina_postprocessor
       result = limina_postprocessor.run_postprocessor(payload)

2. Reusable, for many documents -- the name lists load once::

       processor = limina_postprocessor.DEIDPostProcessor()
       for doc in documents:
           result = processor.process_document(doc)
"""

from .processor import DEIDPostProcessor, run_postprocessor

__version__ = '0.3.0'
__all__ = ['run_postprocessor', 'DEIDPostProcessor']
