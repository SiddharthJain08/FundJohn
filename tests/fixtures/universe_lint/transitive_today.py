from src.strategies.universe_meta import TickerMetadata
from tests.fixtures.universe_lint.helper_with_today import is_recent
def universe_filter(meta: TickerMetadata, as_of) -> bool:
    return is_recent() and bool(meta.in_sp500)
