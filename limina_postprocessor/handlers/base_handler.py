#!/usr/bin/env python3
"""Base class for entity handlers."""

from abc import ABC, abstractmethod
from typing import Dict, Optional


class BaseEntityHandler(ABC):
    """Interface for entity replacement handlers, plus a per-document cache.

    The cache exists so that one original value maps to one replacement throughout a
    document. The processor calls `clear_cache` before each document.
    """

    def __init__(self):
        self.cache = {}

    @abstractmethod
    def can_handle(self, entity_type: str) -> bool:
        """Whether this handler replaces the given entity type."""

    @abstractmethod
    def get_replacement(self, entity: Dict, context: Optional[Dict] = None) -> str:
        """Return a replacement value for the entity."""

    def clear_cache(self):
        """Drop per-document state."""
        self.cache = {}
