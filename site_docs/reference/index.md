# API Reference

Auto-generated from the library's own docstrings, scoped to the public
surface ADR-0019 pins and freezes as of v1.0.0 — every symbol below is
covered by the project's SemVer commitment; anything not listed here isn't.

| Package | Covers |
|---|---|
| [`ontolith`](ontolith.md) | Top-level SDK surface: `Ontology`, core value types, the schema DSL |
| [`ontolith.core`](core.md) | `Assertion`, `Entity`, `Clock`/`IdProvider` ports and their test doubles |
| [`ontolith.identity`](identity.md) | `Principal`, `AuthProvider`, token-based auth |
| [`ontolith.store`](store.md) | `StorageBackend` port, and the SQLite/DuckDB adapters |
| [`ontolith.govern`](govern.md) | Proposals, policy strategies, conflict routing, contradictions |
| [`ontolith.query`](query.md) | `QueryBuilder` |
| [`ontolith.schema`](schema.md) | The class DSL, compiled `SchemaIR` |
| [`ontolith.plugins`](plugins.md) | `PluginRegistry`, plugin protocols and manifests |
| [`ontolith.interfaces`](interfaces.md) | `create_rest_app`, `create_graphql_app` |

CLI flags and MCP tool schemas are deliberately **not** part of this pinned
surface (ADR-0019 Decision #1) — see the CLI's own `--help` output and
[`src/ontolith/interfaces/mcp.py`](https://github.com/ontolith/ontolith/blob/main/src/ontolith/interfaces/mcp.py)'s
docstrings for those.
