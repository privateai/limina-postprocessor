#!/usr/bin/env python3
"""Base class for entity handlers."""

from abc import ABC, abstractmethod
from typing import Dict


class BaseEntityHandler(ABC):
    """Interface for entity replacement handlers, plus a per-document cache."""

    def __init__(self):
        self.cache: Dict = {}

    @abstractmethod
    def can_handle(self, entity_type: str) -> bool:
        """Whether this handler replaces the given entity type."""

    @abstractmethod
    def get_replacement(self, entity: Dict) -> str:
        """Return a replacement for one entity, whose original text is `entity['text']`.

        Write results to `self.cache` so a value that recurs in the document gets the
        same replacement each time.
        """

    def clear_cache(self) -> None:
        """Forget the current document. Override to also clear a subclass's own state."""
        self.cache = {}
