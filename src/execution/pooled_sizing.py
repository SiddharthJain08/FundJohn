"""Crypto pooled sizing (crypto engine Phase 3).

``OPENCLAW_CRYPTO_POOLED_SIZING`` unset ⇒ SHADOW: equity semantics are exactly
as before crypto joined the pooled sizer; ``=1`` ⇒ apply. Task 7 of the Phase 3
plan extends this module with the pooled-sizing helpers.
"""
from __future__ import annotations

import os


def pooled_sizing_enabled(env=os.environ) -> bool:
    """True only when ``OPENCLAW_CRYPTO_POOLED_SIZING=1`` (read at call time)."""
    return env.get('OPENCLAW_CRYPTO_POOLED_SIZING', '') == '1'
