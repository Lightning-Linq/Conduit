"""Importing each skill module registers it.

Modules whose optional dependency is not installed are skipped (a warning is
logged) rather than crashing the app — install the matching extra from pyproject
(e.g. ``pip install -e '.[qr,bolt11,markdown]'``) to enable them.
"""

import importlib
import logging

_SKILL_MODULES = (
    "entity_extract",
    "hash_digest",
    "json_csv",
    "text_stats",
    "qr_generate",
    "bolt11_decode",
    "markdown_html",
    "mempool_fees",
    "btc_price",
    "weather",
    "geocode",
)

_log = logging.getLogger(__name__)

for _name in _SKILL_MODULES:
    try:
        importlib.import_module(f"{__name__}.{_name}")
    except ModuleNotFoundError as _exc:  # optional dependency not installed
        _log.warning("seed skill %r not loaded (%s)", _name, _exc)
