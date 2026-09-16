"""Benchmark catalog: popular broad-market indices, fetched as EOD prices.

Each benchmark is a dict {code, name, stooq, currency}. ``code`` is the stable
key used in the API and the stored Security's ticker; ``stooq`` is the Stooq
symbol. Prices come from Stooq (free, no key). The list is deliberately
curated, not exhaustive — a benchmark is a comparison anchor, and a short,
recognisable list serves that better than every index on earth.
"""

BENCHMARKS = [
    {"code": "SP500", "name": "S&P 500 (US)", "stooq": "^spx", "currency": "USD"},
    {"code": "NASDAQ100", "name": "Nasdaq 100 (US)", "stooq": "^ndx", "currency": "USD"},
    {"code": "NIFTY50", "name": "Nifty 50 (India)", "stooq": "^nsei", "currency": "INR"},
    {"code": "NIKKEI225", "name": "Nikkei 225 (Japan)", "stooq": "^nkx", "currency": "JPY"},
    {"code": "EUROSTOXX50", "name": "EURO STOXX 50 (Eurozone)", "stooq": "^stoxx50e", "currency": "EUR"},
    {"code": "KOSPI", "name": "KOSPI (Korea)", "stooq": "^kospi", "currency": "KRW"},
    {"code": "SSE", "name": "Shanghai Composite (China)", "stooq": "^shc", "currency": "CNY"},
]

BY_CODE = {b["code"]: b for b in BENCHMARKS}


def codes():
    return [b["code"] for b in BENCHMARKS]


def get(code):
    return BY_CODE.get(code)
