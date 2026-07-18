"""Isolated, bounded Accoya targeted-email workflow."""

from .models import (
    GenerationResult,
    GenerationStatus,
    ProductSelection,
)
from .workflow import AccoyaEmailAgent

__all__ = [
    "AccoyaEmailAgent",
    "GenerationResult",
    "GenerationStatus",
    "ProductSelection",
]
