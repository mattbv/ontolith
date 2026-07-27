# Ontolith

[![CI](https://github.com/mattbv/ontolith/workflows/CI/badge.svg)](https://github.com/mattbv/ontolith/actions)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![License](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](https://opensource.org/licenses/Apache-2.0)

A Python framework/SDK for building collaborative knowledge bases where humans and AI agents are co-equal authors of a shared, governed ontology.

## Features

- **Dual-principal model**: Humans and AI agents as co-equal authors
- **Provenance & confidence**: Every assertion tracked with source and confidence
- **Governed collaboration**: Configurable proposal/review workflow
- **Bitemporal time-travel**: Query knowledge as of any point in time
- **Conflict handling**: Temporal supersession and explicit contradictions
- **Plugin extensibility**: Importers, exporters, reasoners, validators

## Quick Start

```bash
# Clone repository
git clone https://github.com/mattbv/ontolith.git
cd ontolith

# Install dependencies (requires Python 3.11+)
uv sync

# Run tests
uv run pytest

# Run quality checks
uv run ruff check
uv run mypy --strict src conformance/conftest.py
```

## Development Status

🚧 **Pre-Alpha** - M3 (Extensible) in progress — M1 substrate and M2 collaboration complete

See [docs/Ontolith_Implementation_Plan.md](docs/Ontolith_Implementation_Plan.md) for roadmap.

## Documentation

- [Product Requirements](docs/Ontolith_PRD.md)
- [Technical Specification](docs/Ontolith_SPEC.md)
- [Implementation Plan](docs/Ontolith_Implementation_Plan.md)
- [Architecture Decision Records](docs/adr/)

## Contributing

We welcome contributions! Please see [CONTRIBUTING.md](CONTRIBUTING.md) for guidelines.

## License

Apache-2.0 - See [LICENSE](LICENSE) for details.