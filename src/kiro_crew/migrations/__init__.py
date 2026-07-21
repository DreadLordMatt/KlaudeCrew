"""One-time data migrations that run before the gateway opens its stores.

Modules here follow the ``~/.kirocrew/.migrations/<marker>`` guarded
one-shot pattern: a migration checks for a per-migration marker file, runs
at most once, and writes the marker on success so subsequent boots are a
cheap no-op.
"""
