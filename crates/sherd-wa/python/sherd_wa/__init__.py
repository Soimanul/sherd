"""Bounded Rust parsing of WhatsApp exports; canonical rows stay in Python."""

from ._native import RecordIterator, detect_date_order

__all__ = ["RecordIterator", "detect_date_order"]
