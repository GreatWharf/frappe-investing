"""Broker connectors: normalize external broker data into plain dicts.

Pure Python — no ``import frappe`` — so every connector is testable offline
and reusable from any entry point. Output contracts are documented in
``connectors.base``.
"""
