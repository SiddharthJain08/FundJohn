"""SP-3 instrument-class resolver. Reads the declared instrument_class for a
strategy from the manifest; defaults to 'equity' for unknown strategies.
Re-exports the taxonomy constants from lifecycle so callers have one import."""
from __future__ import annotations
from pathlib import Path

try:
    from .lifecycle import (VALID_INSTRUMENT_CLASSES, ROUTED_INSTRUMENT_CLASSES,
                            LifecycleStateMachine)
except ImportError:  # pragma: no cover - alt import root
    from strategies.lifecycle import (VALID_INSTRUMENT_CLASSES,  # type: ignore
                                      ROUTED_INSTRUMENT_CLASSES, LifecycleStateMachine)

_DEFAULT_MANIFEST = Path(__file__).resolve().parent / "manifest.json"


def instrument_class_for(strategy_id: str, manifest_path: "str | Path | None" = None) -> str:
    """Return the declared instrument_class for strategy_id, or 'equity'."""
    path = Path(manifest_path) if manifest_path else _DEFAULT_MANIFEST
    sm = LifecycleStateMachine.from_manifest(path)
    rec = sm._records.get(strategy_id)
    return rec.instrument_class if rec is not None else "equity"
