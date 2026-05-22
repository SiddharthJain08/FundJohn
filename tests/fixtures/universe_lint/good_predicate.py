from src.strategies.universe_meta import TickerMetadata
def universe_filter(meta: TickerMetadata, as_of) -> bool:
    return bool(meta.in_sp500)
