from datetime import datetime
from src.strategies.universe_meta import TickerMetadata
def universe_filter(meta: TickerMetadata, as_of) -> bool:
    return datetime.now().year > 2020 and bool(meta.in_sp500)
