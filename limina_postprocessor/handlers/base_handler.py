#!/usr/bin/env python3
"""Base class for entity handlers."""

import threading
from abc import ABC, abstractmethod
from typing import Dict, Optional


class BaseEntityHandler(ABC):
    """Abstract base class for entity replacement handlers."""

    def __init__(self):
        """Initialize the handler."""
        # Per-document state is thread-local. DEIDPostProcessor.process_documents can run
        # several documents at once, and each needs its own coreference view: a plain dict
        # here would let one document's clear_cache() wipe another's mid-flight, and would
        # leak replacements between documents that are supposed to be independent.
        self._doc_state = threading.local()

    @property
    def cache(self) -> Dict:
        """This thread's exact-match cache, for the document it is currently processing."""
        return self._doc_dict("cache")

    def _doc_dict(self, name: str) -> Dict:
        """Fetch one of this thread's per-document dicts, creating it on first touch."""
        value = getattr(self._doc_state, name, None)
        if value is None:
            value = {}
            setattr(self._doc_state, name, value)
        return value

    @abstractmethod
    def can_handle(self, entity_type: str) -> bool:
        """Check if this handler can process the given entity type."""
        pass

    @abstractmethod
    def get_replacement(self, entity: Dict, context: Optional[Dict] = None) -> str:
        """Generate a replacement value for the entity."""
        pass

    def clear_cache(self):
        """Drop this thread's per-document state, ready for a new document.

        Clears the thread-local wholesale, so a subclass that adds per-document dicts
        through _doc_dict is covered automatically. Only the calling thread is affected.
        """
        self._doc_state.__dict__.clear()

    def _get_cached_or_generate(self, cache_key: str, generator_func) -> str:
        """Get cached value or generate new one."""
        if cache_key not in self.cache:
            self.cache[cache_key] = generator_func()
        return self.cache[cache_key]
