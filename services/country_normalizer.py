"""Compatibility import; implementation lives in playwright_service."""
import sys
from importlib import import_module

sys.modules[__name__] = import_module("playwright_service.country_normalizer")
