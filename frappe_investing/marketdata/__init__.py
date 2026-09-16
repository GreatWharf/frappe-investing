"""Market-data providers: normalized EOD quotes and security search.

Pure Python — no ``import frappe``. Quote contract:
``{security_key, day, close, currency}`` where ``close`` is a
Decimal-parseable string and ``currency`` may be None when the feed does
not report one (the caller then applies the Security's currency).
"""
