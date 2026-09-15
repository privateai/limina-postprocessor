#!/usr/bin/env python3
"""Base class for entity handlers."""

from abc import ABC, abstractmethod
from typing import Dict, Optional


class BaseEntityHandler(ABC):
    """Abstract base class for entity replacement handlers."""

    def __init__(self):
        """Initialize the handler."""
        self.cache = {}

    @abstractmethod
    def can_handle(self, entity_type: str) -> bool:
        """Check if this handler can process the given entity type."""
        pass

    @abstractmethod
    def get_replacement(self, entity: Dict, context: Optional[Dict] = None) -> str:
        """Generate a replacement value for the entity."""
        pass

    def clear_cache(self):
        """Clear the cache for a new document."""
        self.cache = {}

    def _get_cached_or_generate(self, cache_key: str, generator_func) -> str:
        """Get cached value or generate new one."""
        if cache_key not in self.cache:
            self.cache[cache_key] = generator_func()
        return self.cache[cache_key]
