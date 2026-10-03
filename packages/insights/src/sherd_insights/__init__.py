"""Deterministic insights over your sherd database."""

from importlib.metadata import version as _version

from sherd_insights.base import Dig, DigParams, DigResult, Headline

__all__ = ["Dig", "DigParams", "DigResult", "Headline"]

__version__ = _version("sherd-insights")
