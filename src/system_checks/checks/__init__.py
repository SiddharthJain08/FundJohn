"""Importing this package side-effect-imports every check module, which
registers their @check-decorated functions in the registry. New domains:
add a new module here and import it from this file."""
from . import pipeline    # noqa: F401
from . import broker      # noqa: F401
from . import regime      # noqa: F401
from . import strategies  # noqa: F401
from . import agents      # noqa: F401
from . import storage     # noqa: F401
from . import universe    # noqa: F401
from . import maintenance # noqa: F401
from . import universe_resolution          # noqa: F401
from . import metadata_snapshot_freshness  # noqa: F401
from . import backfill_progress            # noqa: F401
from . import ticker_metadata_history_depth  # noqa: F401
from . import universe_recs_health           # noqa: F401
from . import papermint_predicate_coverage   # noqa: F401
from . import option_routing                 # noqa: F401
