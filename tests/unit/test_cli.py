"""Unit tests for the CLI."""

import tempfile
from pathlib import Path

import pytest
from typer.testing import CliRunner

from ontolith import Ontology
from ontolith.interfaces.cli import app

runner = CliRunner()


@pytest.fixture
def temp_db() -> Path:
    """Create a temporary database file."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        return Path(f.name)


@pytest.fixture
def seeded_db(temp_db: Path) -> tuple[Path, str, str]:
    """DB with a principal and entity pre-created. Returns (path, principal_id, entity_id)."""
    kb = Ontology.connect(temp_db)
    alice = kb.create_principal("alice@example.com", kind="human", default_capability="write")
    entity = kb.create_entity("Person", author=alice.id)
    kb.close()
    return temp_db, alice.id, entity.id


class TestPrincipalCreate:
    def test_creates_human_principal(self, temp_db: Path) -> None:
        result = runner.invoke(
            app,
            ["--db", str(temp_db), "principal", "create", "alice@example.com", "--kind", "human"],
        )
        assert result.exit_code == 0
        assert "alice@example.com" in result.output

    def test_creates_ai_principal_with_owner(self, temp_db: Path) -> None:
        result = runner.invoke(
            app,
            [
                "--db",
                str(temp_db),
                "principal",
                "create",
                "bot-1",
                "--kind",
                "ai",
                "--owner",
                "alice@example.com",
            ],
        )
        assert result.exit_code == 0
        assert "bot-1" in result.output

    def test_output_shows_kind_and_capability(self, temp_db: Path) -> None:
        result = runner.invoke(
            app,
            [
                "--db",
                str(temp_db),
                "principal",
                "create",
                "alice@example.com",
                "--kind",
                "human",
                "--capability",
                "write",
            ],
        )
        assert result.exit_code == 0
        assert "kind=human" in result.output
        assert "capability=write" in result.output


class TestPrincipalTokens:
    def test_issue_token_prints_token_and_credential_id(self, temp_db: Path) -> None:
        kb = Ontology.connect(temp_db)
        kb.create_principal("alice@example.com", kind="human", default_capability="write")
        kb.close()

        result = runner.invoke(
            app, ["--db", str(temp_db), "principal", "issue-token", "alice@example.com"]
        )
        assert result.exit_code == 0
        assert "Token for alice@example.com:" in result.output
        assert "Credential ID" in result.output
        assert "will not be shown again" in result.output

    def test_issue_token_unknown_principal_exits_nonzero(self, temp_db: Path) -> None:
        result = runner.invoke(
            app, ["--db", str(temp_db), "principal", "issue-token", "nobody@example.com"]
        )
        assert result.exit_code == 1

    def test_revoke_token_then_it_no_longer_resolves(self, temp_db: Path) -> None:
        kb = Ontology.connect(temp_db)
        kb.create_principal("alice@example.com", kind="human", default_capability="write")
        raw_token = kb.issue_token("alice@example.com")
        credential_id = kb.list_tokens("alice@example.com")[0].id
        kb.close()

        result = runner.invoke(
            app, ["--db", str(temp_db), "principal", "revoke-token", credential_id]
        )
        assert result.exit_code == 0
        assert credential_id in result.output

        from ontolith.core.errors import AuthError
        from ontolith.identity.token_auth import TokenAuthProvider

        kb2 = Ontology.connect(temp_db)
        with pytest.raises(AuthError):
            TokenAuthProvider(kb2.backend).resolve(raw_token)
        kb2.close()

    def test_revoke_token_unknown_credential_exits_nonzero(self, temp_db: Path) -> None:
        result = runner.invoke(
            app, ["--db", str(temp_db), "principal", "revoke-token", "nonexistent"]
        )
        assert result.exit_code == 1

    def test_list_tokens_shows_issued_credentials_not_raw_token(self, temp_db: Path) -> None:
        kb = Ontology.connect(temp_db)
        kb.create_principal("alice@example.com", kind="human", default_capability="write")
        raw_token = kb.issue_token("alice@example.com")
        credential_id = kb.list_tokens("alice@example.com")[0].id
        kb.close()

        result = runner.invoke(
            app, ["--db", str(temp_db), "principal", "list-tokens", "alice@example.com"]
        )
        assert result.exit_code == 0
        assert credential_id in result.output
        assert "active" in result.output
        assert raw_token not in result.output

    def test_list_tokens_no_credentials_message(self, temp_db: Path) -> None:
        kb = Ontology.connect(temp_db)
        kb.create_principal("alice@example.com", kind="human", default_capability="write")
        kb.close()

        result = runner.invoke(
            app, ["--db", str(temp_db), "principal", "list-tokens", "alice@example.com"]
        )
        assert result.exit_code == 0
        assert "No credentials issued" in result.output


class TestEntityCreate:
    def test_creates_entity(self, temp_db: Path) -> None:
        runner.invoke(
            app,
            ["--db", str(temp_db), "principal", "create", "alice@example.com", "--kind", "human"],
        )
        result = runner.invoke(
            app,
            [
                "--db",
                str(temp_db),
                "entity",
                "create",
                "--concept",
                "Person",
                "--author",
                "alice@example.com",
            ],
        )
        assert result.exit_code == 0
        assert "concept=Person" in result.output

    def test_creates_entity_with_natural_key(self, temp_db: Path) -> None:
        runner.invoke(
            app,
            ["--db", str(temp_db), "principal", "create", "alice@example.com", "--kind", "human"],
        )
        result = runner.invoke(
            app,
            [
                "--db",
                str(temp_db),
                "entity",
                "create",
                "--concept",
                "Person",
                "--author",
                "alice@example.com",
                "--key",
                "ada-lovelace",
            ],
        )
        assert result.exit_code == 0
        assert "concept=Person" in result.output


class TestAssertLiteral:
    def test_asserts_literal_value(self, seeded_db: tuple[Path, str, str]) -> None:
        db, author, entity_id = seeded_db
        result = runner.invoke(
            app,
            [
                "--db",
                str(db),
                "assert",
                entity_id,
                "Person.name",
                "Ada Lovelace",
                "--type",
                "Text",
                "--author",
                author,
            ],
        )
        assert result.exit_code == 0
        assert "Ada Lovelace" in result.output
        assert "Person.name" in result.output

    def test_asserts_with_optional_fields(self, seeded_db: tuple[Path, str, str]) -> None:
        db, author, entity_id = seeded_db
        result = runner.invoke(
            app,
            [
                "--db",
                str(db),
                "assert",
                entity_id,
                "Person.name",
                "Ada",
                "--type",
                "Text",
                "--author",
                author,
                "--confidence",
                "0.9",
                "--source",
                "Wikipedia",
            ],
        )
        assert result.exit_code == 0
        assert "Ada" in result.output


class TestListAssertions:
    def test_lists_active_assertions(self, seeded_db: tuple[Path, str, str]) -> None:
        db, author, entity_id = seeded_db
        runner.invoke(
            app,
            [
                "--db",
                str(db),
                "assert",
                entity_id,
                "Person.name",
                "Ada",
                "--type",
                "Text",
                "--author",
                author,
            ],
        )
        result = runner.invoke(app, ["--db", str(db), "assertions", "--subject", entity_id])
        assert result.exit_code == 0
        assert "Person.name" in result.output
        assert "Ada" in result.output

    def test_no_assertions_message(self, temp_db: Path) -> None:
        result = runner.invoke(app, ["--db", str(temp_db), "assertions"])
        assert result.exit_code == 0
        assert "No assertions found." in result.output

    def test_filter_by_predicate(self, seeded_db: tuple[Path, str, str]) -> None:
        db, author, entity_id = seeded_db
        runner.invoke(
            app,
            [
                "--db",
                str(db),
                "assert",
                entity_id,
                "Person.name",
                "Ada",
                "--type",
                "Text",
                "--author",
                author,
            ],
        )
        runner.invoke(
            app,
            [
                "--db",
                str(db),
                "assert",
                entity_id,
                "Person.born",
                "1815",
                "--type",
                "Text",
                "--author",
                author,
            ],
        )
        result = runner.invoke(app, ["--db", str(db), "assertions", "--predicate", "Person.name"])
        assert result.exit_code == 0
        assert "Person.name" in result.output
        assert "Person.born" not in result.output


class TestQuery:
    def test_returns_entities_of_concept(self, seeded_db: tuple[Path, str, str]) -> None:
        db, _, entity_id = seeded_db
        result = runner.invoke(app, ["--db", str(db), "query", "Person"])
        assert result.exit_code == 0
        assert entity_id in result.output
        assert "concept=Person" in result.output

    def test_no_results_message(self, temp_db: Path) -> None:
        result = runner.invoke(app, ["--db", str(temp_db), "query", "Person"])
        assert result.exit_code == 0
        assert "No entities found." in result.output

    def test_filters_with_where(self, seeded_db: tuple[Path, str, str]) -> None:
        db, author, entity_id = seeded_db
        runner.invoke(
            app,
            [
                "--db",
                str(db),
                "assert",
                entity_id,
                "Person.name",
                "Ada",
                "--type",
                "Text",
                "--author",
                author,
            ],
        )
        result = runner.invoke(app, ["--db", str(db), "query", "Person", "--where", "name=Ada"])
        assert result.exit_code == 0
        assert entity_id in result.output

    def test_where_no_match(self, seeded_db: tuple[Path, str, str]) -> None:
        db, _, _ = seeded_db
        result = runner.invoke(app, ["--db", str(db), "query", "Person", "--where", "name=Nobody"])
        assert result.exit_code == 0
        assert "No entities found." in result.output

    def test_invalid_filter_format_exits_nonzero(self, temp_db: Path) -> None:
        result = runner.invoke(app, ["--db", str(temp_db), "query", "Person", "--where", "invalid"])
        assert result.exit_code == 1


class TestErrorPaths:
    def test_ai_principal_without_owner_exits_nonzero(self, temp_db: Path) -> None:
        result = runner.invoke(
            app,
            ["--db", str(temp_db), "principal", "create", "bot-1", "--kind", "ai"],
        )
        assert result.exit_code == 1

    def test_assert_on_nonexistent_entity_exits_nonzero(self, temp_db: Path) -> None:
        runner.invoke(
            app,
            ["--db", str(temp_db), "principal", "create", "alice@example.com", "--kind", "human"],
        )
        result = runner.invoke(
            app,
            [
                "--db",
                str(temp_db),
                "assert",
                "nonexistent-id",
                "Person.name",
                "Ada",
                "--type",
                "Text",
                "--author",
                "alice@example.com",
            ],
        )
        assert result.exit_code == 1

    def test_assert_with_unknown_author_exits_nonzero(
        self, seeded_db: tuple[Path, str, str]
    ) -> None:
        db, _, entity_id = seeded_db
        result = runner.invoke(
            app,
            [
                "--db",
                str(db),
                "assert",
                entity_id,
                "Person.name",
                "Ada",
                "--type",
                "Text",
                "--author",
                "nobody@example.com",
            ],
        )
        assert result.exit_code == 1
