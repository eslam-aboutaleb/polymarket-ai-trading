"""Platform domain: cross-cutting application infrastructure.

Owns the append-only event log, audit trail, experiment registry and the
background-service lifecycle registry. Every other domain may import `platform`.
"""
