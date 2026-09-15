"""
Entity handlers for DEID post-processing.

Each handler is responsible for replacing one or more entity types
with realistic synthetic values.
"""

from .base_handler import BaseEntityHandler
from .name_handler import NameHandler
from .gender_detector import GenderDetector

__all__ = [
    'BaseEntityHandler',
    'NameHandler',
    'GenderDetector',
]
