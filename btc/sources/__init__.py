"""Public-source adapters for BTCUSD. Each adapter returns a normalised SourceResult.

Adopted per design/sources.json and PARENT-DECISIONS P8: no Coinbase, Twelve
Data, CoinGlass or Yahoo. Raw bodies are never returned; only normalised
values and ``raw_sha256``.
"""
