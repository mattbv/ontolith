"""Unit tests for Principal model."""

from datetime import UTC, datetime

import pytest

from ontolith.identity import Principal


class TestPrincipal:
    """Tests for Principal model."""

    def test_create_human_principal(self) -> None:
        """Human principal can be created."""
        principal = Principal(
            id="alice@example.com",
            kind="human",
            auth_method="oidc",
            default_capability="write",
            trust_level=10,
            created_at=datetime(2025, 1, 1, tzinfo=UTC),
        )

        assert principal.id == "alice@example.com"
        assert principal.kind == "human"
        assert principal.owner is None  # Humans don't need owners
        assert principal.default_capability == "write"

    def test_create_ai_principal_with_owner(self) -> None:
        """AI principal can be created with required owner."""
        principal = Principal(
            id="research-bot",
            kind="ai",
            owner="alice@example.com",  # Required!
            auth_method="workload",
            default_capability="propose",
            trust_level=5,
            created_at=datetime(2025, 1, 1, tzinfo=UTC),
            metadata={"model": "claude-sonnet-4", "version": "20250101"},
        )

        assert principal.id == "research-bot"
        assert principal.kind == "ai"
        assert principal.owner == "alice@example.com"
        assert principal.metadata["model"] == "claude-sonnet-4"

    def test_ai_without_owner_raises(self) -> None:
        """AI principal without owner raises validation error."""
        with pytest.raises(ValueError, match="AI principals must have an owner"):
            Principal(
                id="rogue-bot",
                kind="ai",
                owner=None,  # Missing!
                auth_method="workload",
                created_at=datetime(2025, 1, 1, tzinfo=UTC),
            )

    def test_service_principal(self) -> None:
        """Service principal can be created."""
        principal = Principal(
            id="import-service",
            kind="service",
            auth_method="apikey",
            default_capability="write",
            trust_level=8,
            created_at=datetime(2025, 1, 1, tzinfo=UTC),
        )

        assert principal.id == "import-service"
        assert principal.kind == "service"
        assert principal.owner is None  # Services can omit owner

    def test_default_capability_is_propose(self) -> None:
        """Default capability is propose if not specified."""
        principal = Principal(
            id="bot",
            kind="ai",
            owner="alice@example.com",
            auth_method="workload",
            created_at=datetime(2025, 1, 1, tzinfo=UTC),
        )

        assert principal.default_capability == "propose"

    def test_principal_is_immutable(self) -> None:
        """Principal is frozen (immutable)."""
        from pydantic import ValidationError

        principal = Principal(
            id="alice@example.com",
            kind="human",
            auth_method="oidc",
            created_at=datetime(2025, 1, 1, tzinfo=UTC),
        )

        with pytest.raises(ValidationError):
            principal.kind = "ai"  # type: ignore

    def test_capability_levels(self) -> None:
        """All capability levels are valid."""
        capabilities = ["read", "propose", "write", "review", "admin"]

        for cap in capabilities:
            principal = Principal(
                id="test@example.com",
                kind="human",
                auth_method="oidc",
                default_capability=cap,  # type: ignore
                created_at=datetime(2025, 1, 1, tzinfo=UTC),
            )
            assert principal.default_capability == cap
