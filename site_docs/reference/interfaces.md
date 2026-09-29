# ontolith.interfaces

REST and GraphQL app factories — the two web interfaces with a genuine
Python call-signature surface (CLI flags and MCP tool schemas are excluded
from this pinned surface by ADR-0019 Decision #1).

::: ontolith.interfaces.rest
    options:
      members:
        - create_rest_app

## ontolith.interfaces.graphql

::: ontolith.interfaces.graphql
    options:
      members:
        - create_graphql_app
