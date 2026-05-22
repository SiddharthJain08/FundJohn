from datetime import date as _d
def universe_filter(meta, as_of):
    return bool(meta.in_sp500) and _d.today().year >= 2021
