# Contributing to Ontolith

Thank you for your interest in contributing to Ontolith! This document provides guidelines for contributing to the project.

## Table of Contents

- [Code of Conduct](#code-of-conduct)
- [Getting Started](#getting-started)
- [Development Workflow](#development-workflow)
- [Architecture Principles](#architecture-principles)
- [Testing Requirements](#testing-requirements)
- [Code Quality Standards](#code-quality-standards)
- [Pull Request Process](#pull-request-process)
- [Commit Message Guidelines](#commit-message-guidelines)

## Code of Conduct

This project adheres to a Code of Conduct that all contributors are expected to follow. Please read [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md) before contributing.

## Getting Started

### Prerequisites

- Python ≥ 3.11
- [uv](https://github.com/astral-sh/uv) for dependency management

### Initial Setup

```bash
# Clone the repository
git clone https://github.com/yourusername/ontolith.git
cd ontolith

# Install dependencies
uv sync

# Install pre-commit hooks
uv run pre-commit install

# Run tests to verify setup
uv run pytest
```

### Running Quality Gates

```bash
# Format code
uv run ruff format

# Lint
uv run ruff check

# Type check
uv run mypy --strict src

# Run all tests
uv run pytest

# Run conformance suite (for core/govern/store/query changes)
uv run pytest conformance/

# Full quality gate (run before committing)
uv run pytest && ruff check && mypy --strict src
```

## Development Workflow

### 1. Create a Branch

Always create a branch for your work. Never commit directly to `main`.

```bash
git checkout -b <type>/<short-description>
```

Branch naming conventions:
- `feat/<description>` — New features
- `fix/<description>` — Bug fixes
- `docs/<description>` — Documentation changes
- `test/<description>` — Test additions/fixes
- `refactor/<description>` — Code refactoring

### 2. Make Your Changes

- Write tests first for new functionality (TDD)
- Keep commits atomic and focused
- Follow the architecture principles (see below)
- Run quality gates frequently during development

### 3. Commit Your Changes

We use [Conventional Commits](https://www.conventionalcommits.org/):

```bash
git commit -m "type(scope): description"
```

See [Commit Message Guidelines](#commit-message-guidelines) below for details.

### 4. Push and Create a Pull Request

```bash
# Ensure your branch is up to date
git checkout main
git pull
git checkout your-branch
git rebase main

# Push your branch
git push -u origin your-branch

# Create a PR via GitHub web UI or gh CLI
gh pr create
```

## Architecture Principles

Ontolith follows a **ports & adapters (hexagonal)** architecture. Please familiarize yourself with these core principles before contributing:

### Dependency Rule (CI-Enforced)

**Domain modules** (`core/`, `schema/`, `govern/`, `query/`) MUST NOT import:
- Concrete adapters (`store/sqlite`, `interfaces/*`)
- Any concrete implementation

**They may only import:**
- Abstract ports: `StorageBackend`, `Embedder`, `AuthProvider`, `PolicyStrategy`, `Clock`, `IdProvider`
- Standard library
- Approved dependencies (pydantic, structlog, etc.)

This rule is enforced by `import-linter` in CI.

### Immutability Invariants

- **Assertions are append-only**: Only `status`, `valid_to`, and successor links may be modified
- Never edit an assertion's `value` field in-place
- Create new assertion records for updates

### Determinism by Construction

- Never use `datetime.now()` or `uuid4()` in domain logic
- Use injected `Clock` and `IdProvider` ports
- All bitemporal behavior must be reproducible in tests

### Policy Purity

The policy engine (`govern/policy`) must be **pure**:
- No I/O operations
- Deterministic given the same inputs
- Testable without mocks

### Other Core Invariants

- AI principals MUST declare an accountable owner
- Confidence is a single scalar (0.0-1.0), NEVER auto-combined in v1
- MCP server exposes NO direct write tool
- Static facts are NEVER silently overwritten (create contradictions instead)

## Testing Requirements

### Test-Driven Development

For correctness-critical modules (`govern/conflict`, bitemporal logic), write tests **before** implementation:

1. Write a failing conformance vector or property test
2. Implement the feature
3. Verify all tests pass
4. Ensure coverage requirements are met

### Test Categories

- **Unit tests** — Pure functions and value objects
- **Property tests** (Hypothesis) — Invariants that must always hold
- **Conformance tests** — SPEC compliance vectors (reusable by storage backends)
- **Integration tests** — Component interactions
- **Benchmarks** — Performance validation

### Coverage Requirements

- **≥90%** coverage on domain modules (`core`, `govern`, `query`)
- **100%** coverage on `govern/conflict` and bitemporal query logic
- **≥95%** docstring coverage on public APIs

## Code Quality Standards

All code must pass these gates before merge:

| Check | Tool | Requirement |
|-------|------|-------------|
| Format | `ruff format --check` | Clean |
| Lint | `ruff check` | Zero errors |
| Types | `mypy --strict` | Zero errors on `src/` |
| Tests | `pytest` | All pass |
| Coverage | `pytest-cov` | Meets thresholds |
| Conformance | `pytest conformance/` | All vectors pass |
| Dependency rule | `import-linter` | Contract holds |
| Security | `bandit`, `pip-audit` | No high/critical issues |
| Docstrings | `interrogate` | ≥95% on public API |

## Pull Request Process

### Before Submitting

- [ ] All quality gates pass locally
- [ ] Conformance tests pass (if touching core/govern/store/query)
- [ ] New tests added for new functionality
- [ ] Documentation updated (docstrings, examples if needed)
- [ ] Commits follow conventional format
- [ ] Branch is up to date with `main`

### PR Description Template

```markdown
## Summary
Brief description of what this PR does and why.

## Changes
- Bulleted list of changes
- Reference SPEC sections if implementing requirements

## Testing
- Which tests were added/modified
- How to verify the changes work

## Checklist
- [ ] Tests pass
- [ ] Documentation updated
- [ ] No breaking changes (or documented if necessary)

Closes #<issue-number> (if applicable)
```

### Review Process

1. CI must pass (all quality gates green)
2. At least one maintainer approval required
3. All review comments must be resolved
4. Conventional commits format verified

### After Merge

- Delete your feature branch
- Update your local `main`: `git pull origin main`

## Commit Message Guidelines

We use [Conventional Commits](https://www.conventionalcommits.org/) to automate changelog generation and version bumps.

### Format

```
<type>(<scope>): <description>

[optional body]

[optional footer]
```

### Types

- `feat` — New feature
- `fix` — Bug fix
- `docs` — Documentation only
- `test` — Adding/updating tests
- `refactor` — Code refactoring (no behavior change)
- `perf` — Performance improvement
- `chore` — Maintenance tasks
- `ci` — CI/CD changes

### Scopes

Use the module name: `core`, `schema`, `govern`, `store`, `query`, `identity`, `interfaces`, `plugins`, `observe`

### Examples

```bash
feat(core): add Clock and IdProvider ports for determinism
fix(govern): close validity window on time_varying supersession
docs(adr): record ADR-0009 assertion immutability
test(conformance): add SPEC §19 contradiction vector
refactor(query): extract temporal filter logic
chore(ci): add import-linter to quality gates
```

## Questions?

- Open a [Discussion](https://github.com/yourusername/ontolith/discussions) for questions
- Check existing [Issues](https://github.com/yourusername/ontolith/issues) before creating new ones
- Review the [Technical Specification](docs/Ontolith_SPEC.md) for implementation details

## License

By contributing to Ontolith, you agree that your contributions will be licensed under the Apache-2.0 License.

Thank you for contributing to Ontolith! 🎉
