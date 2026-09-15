"""
Limina Postprocessor - Post-processing for DEID synthetic output.

Two usage patterns:

1. Convenience function (for single/batch processing):
    import limina_postprocessor
    result = limina_postprocessor.run_postprocessor(payload)

2. Reusable processor (for repeated calls - more efficient):
    processor = limina_postprocessor.DEIDPostProcessor(
        name_dictionary_path='path/to/dict.parquet',
        enable_names=True
    )
    # Dictionary loads once, reuse for many documents
    for doc in documents:
        result = processor.process_document(doc)
"""

from pathlib import Path
from .processor import DEIDPostProcessor

__version__ = "0.1.0"
__all__ = ["run_postprocessor", "DEIDPostProcessor"]

# Default paths relative to package
_PACKAGE_DIR = Path(__file__).parent
DEFAULT_DICTIONARY = str(_PACKAGE_DIR / "data" / "name_dictionary_1b_filtered.parquet")
DEFAULT_CENSUS_DATA = str(_PACKAGE_DIR / "data" / "census_data")


def run_postprocessor(
    deid_output,
    dictionary_path=None,
    enable_names=True,
    enable_api_gender=False
):
    """
    Post-process DEID output with synthetic name replacement.

    Note: This function creates a new processor instance each time. For processing
    many documents efficiently, create a DEIDPostProcessor instance and reuse it:
        processor = limina_postprocessor.DEIDPostProcessor(...)
        for doc in documents:
            result = processor.process_document(doc)

    Args:
        deid_output (dict or list): DEID JSON output (single document or array)
        dictionary_path (str, optional): Path to name dictionary
                                        (defaults to packaged 1B dictionary)
        enable_names (bool): Enable name replacement (default: True)
        enable_api_gender (bool): Enable API fallback for gender detection (default: False, slower)

    Returns:
        dict or list: Processed output with replaced entities

    Example (single document):
        >>> import limina_postprocessor
        >>> import json
        >>>
        >>> # Load DEID output
        >>> with open('deid_output.json') as f:
        ...     payload = json.load(f)
        >>>
        >>> # Process it
        >>> result = limina_postprocessor.run_postprocessor(payload)
        >>>
        >>> # Save result
        >>> with open('processed_output.json', 'w') as f:
        ...     json.dump(result, f, indent=2)

    Example (reusable processor for many documents - RECOMMENDED):
        >>> import limina_postprocessor
        >>>
        >>> # Create processor once (dictionary loads once)
        >>> processor = limina_postprocessor.DEIDPostProcessor(
        ...     name_dictionary_path=limina_postprocessor.DEFAULT_DICTIONARY,
        ...     enable_names=True
        ... )
        >>>
        >>> # Process many documents (dictionary stays in memory)
        >>> for doc_file in document_files:
        ...     with open(doc_file) as f:
        ...         doc = json.load(f)
        ...     result = processor.process_document(doc)
        ...     # Save result...
    """
    # Use default dictionary if not specified
    if dictionary_path is None and enable_names:
        dictionary_path = DEFAULT_DICTIONARY

    # Initialize processor
    processor = DEIDPostProcessor(
        name_dictionary_path=dictionary_path,
        enable_names=enable_names,
        enable_api_gender=enable_api_gender
    )

    # Process document(s)
    if isinstance(deid_output, list):
        result = [processor.process_document(doc) for doc in deid_output]
    else:
        result = processor.process_document(deid_output)

    return result
