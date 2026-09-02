"""Unit tests for the CLI."""

import tempfile
from pathlib import Path

import pytest
from typer.testing import CliRunner

from ontolith import Ontology
from ontolith.core import Assertion
from ontolith.interfaces.cli import app
from ontolith.schema import ConceptDef, PropertyDef, RelationDef, SchemaIR

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
        kb = Ontology.connect(temp_db)
        kb.create_principal("alice@example.com", kind="human")
        kb.create_principal("admin@example.com", kind="human", default_capability="admin")
        kb.close()

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
                "--author",
                "admin@example.com",
            ],
        )
        assert result.exit_code == 0
        assert "bot-1" in result.output

    def test_second_principal_requires_author(self, temp_db: Path) -> None:
        """KI-054: once a database has at least one principal, --author
        (naming an existing admin) becomes required - only the very first
        principal in a fresh database may omit it."""
        kb = Ontology.connect(temp_db)
        kb.create_principal("alice@example.com", kind="human")
        kb.close()

        result = runner.invoke(
            app,
            ["--db", str(temp_db), "principal", "create", "bob@example.com", "--kind", "human"],
        )
        assert result.exit_code == 1
        assert "--author is required" in result.output
        # The typer.Exit raised for this specific denial must not also be
        # caught by the generic `except Exception` below it and re-echoed
        # as a second, spurious "Error: 1" line.
        assert "Error: 1" not in result.output

    def test_second_principal_with_non_admin_author_is_rejected(self, temp_db: Path) -> None:
        kb = Ontology.connect(temp_db)
        kb.create_principal("alice@example.com", kind="human", default_capability="write")
        kb.close()

        result = runner.invoke(
            app,
            [
                "--db",
                str(temp_db),
                "principal",
                "create",
                "bob@example.com",
                "--kind",
                "human",
                "--author",
                "alice@example.com",
            ],
        )
        assert result.exit_code == 1

    def test_second_principal_with_admin_author_succeeds(self, temp_db: Path) -> None:
        kb = Ontology.connect(temp_db)
        kb.create_principal("admin@example.com", kind="human", default_capability="admin")
        kb.close()

        result = runner.invoke(
            app,
            [
                "--db",
                str(temp_db),
                "principal",
                "create",
                "bob@example.com",
                "--kind",
                "human",
                "--author",
                "admin@example.com",
            ],
        )
        assert result.exit_code == 0
        assert "bob@example.com" in result.output

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


class TestPrincipalList:
    """KI-022: `ontolith principal list`."""

    def test_lists_all_principals(self, temp_db: Path) -> None:
        kb = Ontology.connect(temp_db)
        kb.create_principal("alice@example.com", kind="human", default_capability="write")
        kb.create_principal("admin@example.com", kind="human", default_capability="admin")
        kb.close()

        result = runner.invoke(
            app,
            ["--db", str(temp_db), "principal", "list", "--author", "admin@example.com"],
        )
        assert result.exit_code == 0
        assert "alice@example.com" in result.output
        assert "admin@example.com" in result.output

    def test_non_admin_author_exits_nonzero(self, temp_db: Path) -> None:
        kb = Ontology.connect(temp_db)
        kb.create_principal("alice@example.com", kind="human", default_capability="write")
        kb.close()

        result = runner.invoke(
            app,
            ["--db", str(temp_db), "principal", "list", "--author", "alice@example.com"],
        )
        assert result.exit_code == 1


class TestPrincipalTokens:
    def test_issue_token_prints_token_and_credential_id(self, temp_db: Path) -> None:
        kb = Ontology.connect(temp_db)
        kb.create_principal("alice@example.com", kind="human", default_capability="write")
        kb.create_principal("admin@example.com", kind="human", default_capability="admin")
        kb.close()

        result = runner.invoke(
            app,
            [
                "--db",
                str(temp_db),
                "principal",
                "issue-token",
                "alice@example.com",
                "--author",
                "admin@example.com",
            ],
        )
        assert result.exit_code == 0
        assert "Token for alice@example.com:" in result.output
        assert "Credential ID" in result.output
        assert "will not be shown again" in result.output

    def test_issue_token_unknown_principal_exits_nonzero(self, temp_db: Path) -> None:
        kb = Ontology.connect(temp_db)
        kb.create_principal("admin@example.com", kind="human", default_capability="admin")
        kb.close()

        result = runner.invoke(
            app,
            [
                "--db",
                str(temp_db),
                "principal",
                "issue-token",
                "nobody@example.com",
                "--author",
                "admin@example.com",
            ],
        )
        assert result.exit_code == 1

    def test_issue_token_non_admin_author_exits_nonzero(self, temp_db: Path) -> None:
        kb = Ontology.connect(temp_db)
        kb.create_principal("alice@example.com", kind="human", default_capability="write")
        kb.close()

        result = runner.invoke(
            app,
            [
                "--db",
                str(temp_db),
                "principal",
                "issue-token",
                "alice@example.com",
                "--author",
                "alice@example.com",
            ],
        )
        assert result.exit_code == 1

    def test_revoke_token_then_it_no_longer_resolves(self, temp_db: Path) -> None:
        kb = Ontology.connect(temp_db)
        kb.create_principal("alice@example.com", kind="human", default_capability="write")
        kb.create_principal("admin@example.com", kind="human", default_capability="admin")
        raw_token, credential_id = kb.issue_token("alice@example.com", author="admin@example.com")
        kb.close()

        result = runner.invoke(
            app,
            [
                "--db",
                str(temp_db),
                "principal",
                "revoke-token",
                credential_id,
                "--author",
                "admin@example.com",
            ],
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
        kb = Ontology.connect(temp_db)
        kb.create_principal("admin@example.com", kind="human", default_capability="admin")
        kb.close()

        result = runner.invoke(
            app,
            [
                "--db",
                str(temp_db),
                "principal",
                "revoke-token",
                "nonexistent",
                "--author",
                "admin@example.com",
            ],
        )
        assert result.exit_code == 1

    def test_list_tokens_shows_issued_credentials_not_raw_token(self, temp_db: Path) -> None:
        kb = Ontology.connect(temp_db)
        kb.create_principal("alice@example.com", kind="human", default_capability="write")
        kb.create_principal("admin@example.com", kind="human", default_capability="admin")
        raw_token, credential_id = kb.issue_token("alice@example.com", author="admin@example.com")
        kb.close()

        result = runner.invoke(
            app,
            [
                "--db",
                str(temp_db),
                "principal",
                "list-tokens",
                "alice@example.com",
                "--author",
                "admin@example.com",
            ],
        )
        assert result.exit_code == 0
        assert credential_id in result.output
        assert "active" in result.output
        assert raw_token not in result.output

    def test_list_tokens_shows_issued_by_and_revoked_by(self, temp_db: Path) -> None:
        """KI-072: the audit trail's read half - who issued/revoked this
        credential must be visible from the CLI's own listing, not just
        the REST route."""
        kb = Ontology.connect(temp_db)
        kb.create_principal("alice@example.com", kind="human", default_capability="write")
        kb.create_principal("admin@example.com", kind="human", default_capability="admin")
        _, credential_id = kb.issue_token("alice@example.com", author="admin@example.com")
        kb.revoke_token(credential_id, author="admin@example.com")
        kb.close()

        result = runner.invoke(
            app,
            [
                "--db",
                str(temp_db),
                "principal",
                "list-tokens",
                "alice@example.com",
                "--author",
                "admin@example.com",
            ],
        )
        assert result.exit_code == 0
        assert "by admin@example.com" in result.output

    def test_list_tokens_pre_ki060_revoked_row_shows_no_literal_none(self, temp_db: Path) -> None:
        """A credential revoked before KI-060's revoked_by column existed
        has revoked_at set but revoked_by still None (PrincipalCredential's
        own docstring: "None if ... revoked before this field existed") -
        the output must not print the fields' attacker/operator-facing
        "by None", which on an audit surface reads as a principal literally
        named None."""
        from datetime import UTC, datetime

        from ontolith.identity.credential import PrincipalCredential

        kb = Ontology.connect(temp_db)
        kb.create_principal("alice@example.com", kind="human", default_capability="write")
        kb.create_principal("admin@example.com", kind="human", default_capability="admin")
        kb.backend.put_credential(
            PrincipalCredential(
                id="legacy-credential",
                principal_id="alice@example.com",
                token_hash="deadbeef",
                created_at=datetime(2025, 1, 1, tzinfo=UTC),
                revoked_at=datetime(2025, 1, 2, tzinfo=UTC),
                issued_by=None,
                revoked_by=None,
            )
        )
        kb.close()

        result = runner.invoke(
            app,
            [
                "--db",
                str(temp_db),
                "principal",
                "list-tokens",
                "alice@example.com",
                "--author",
                "admin@example.com",
            ],
        )
        assert result.exit_code == 0
        assert "by None" not in result.output
        assert "revoked at 2025-01-02T00:00:00+00:00" in result.output

    def test_list_tokens_no_credentials_message(self, temp_db: Path) -> None:
        kb = Ontology.connect(temp_db)
        kb.create_principal("alice@example.com", kind="human", default_capability="write")
        kb.create_principal("admin@example.com", kind="human", default_capability="admin")
        kb.close()

        result = runner.invoke(
            app,
            [
                "--db",
                str(temp_db),
                "principal",
                "list-tokens",
                "alice@example.com",
                "--author",
                "admin@example.com",
            ],
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


class TestRetract:
    def test_write_capability_auto_accepts(self, seeded_db: tuple[Path, str, str]) -> None:
        db, author, entity_id = seeded_db
        kb = Ontology.connect(db)
        assertion = kb.assert_literal(entity_id, "Person.name", "Ada", "Text", author)
        kb.close()

        result = runner.invoke(app, ["--db", str(db), "retract", assertion.id, "--author", author])
        assert result.exit_code == 0
        assert "decision=AutoAccept" in result.output
        assert "state=auto_accepted" in result.output

        kb = Ontology.connect(db)
        retracted = kb.assertions(subject=entity_id, status=None)[0]
        assert retracted.status == "retracted"
        kb.close()

    def test_ai_author_requires_review(self, temp_db: Path) -> None:
        kb = Ontology.connect(temp_db)
        alice = kb.create_principal("alice@example.com", kind="human", default_capability="write")
        bot = kb.create_principal(
            "bot@example.com",
            kind="ai",
            auth_method="apikey",
            owner=alice.id,
            default_capability="propose",
        )
        entity = kb.create_entity("Person", author=alice.id)
        assertion = kb.assert_literal(entity.id, "Person.name", "Ada", "Text", alice.id)
        kb.close()

        result = runner.invoke(
            app, ["--db", str(temp_db), "retract", assertion.id, "--author", bot.id]
        )
        assert result.exit_code == 0
        assert "decision=RequireReview" in result.output

    def test_acting_as_delegation(self, temp_db: Path) -> None:
        kb = Ontology.connect(temp_db)
        alice = kb.create_principal("alice@example.com", kind="human", default_capability="write")
        delegate = kb.create_principal(
            "delegate@example.com",
            kind="human",
            default_capability="write",
            owner=alice.id,
        )
        entity = kb.create_entity("Person", author=alice.id)
        assertion = kb.assert_literal(entity.id, "Person.name", "Ada", "Text", alice.id)
        kb.close()

        result = runner.invoke(
            app,
            [
                "--db",
                str(temp_db),
                "retract",
                assertion.id,
                "--author",
                delegate.id,
                "--acting-as",
                alice.id,
            ],
        )
        assert result.exit_code == 0
        assert "decision=AutoAccept" in result.output

    def test_retract_unknown_assertion_exits_nonzero(
        self, seeded_db: tuple[Path, str, str]
    ) -> None:
        db, author, _ = seeded_db
        result = runner.invoke(app, ["--db", str(db), "retract", "nonexistent", "--author", author])
        assert result.exit_code == 1
        assert "Error:" in result.output


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


class TestReindex:
    def test_reindexes_text_assertions(self, seeded_db: tuple[Path, str, str]) -> None:
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
        result = runner.invoke(app, ["--db", str(db), "reindex"])
        assert result.exit_code == 0
        assert "Reindexed 1 entity." in result.output

    def test_reindex_with_no_text_assertions_reports_zero(
        self, seeded_db: tuple[Path, str, str]
    ) -> None:
        db, _, _ = seeded_db
        result = runner.invoke(app, ["--db", str(db), "reindex"])
        assert result.exit_code == 0
        assert "Reindexed 0 entities." in result.output

    def test_reindex_concept_filter(self, seeded_db: tuple[Path, str, str]) -> None:
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
        result = runner.invoke(app, ["--db", str(db), "reindex", "--concept", "Organization"])
        assert result.exit_code == 0
        assert "Reindexed 0 entities." in result.output


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


class TestProposalList:
    def test_lists_pending_proposals_by_default(self, temp_db: Path) -> None:
        kb = Ontology.connect(temp_db)
        alice = kb.create_principal("alice@example.com", kind="human", default_capability="write")
        bot = kb.create_principal(
            "bot@example.com",
            kind="ai",
            auth_method="apikey",
            owner=alice.id,
            default_capability="propose",
        )
        entity = kb.create_entity("Person", author=alice.id)
        kb.propose(entity.id, "Person.name", "Ada", "Text", alice.id)  # auto_accepted
        pending, _ = kb.propose(
            entity.id, "Person.born", "1815", "Text", bot.id, model="test-model-v1"
        )
        kb.close()

        result = runner.invoke(app, ["--db", str(temp_db), "proposal", "list"])
        assert result.exit_code == 0
        assert pending.id in result.output
        assert "require_review" in result.output

    def test_no_proposals_message(self, temp_db: Path) -> None:
        result = runner.invoke(app, ["--db", str(temp_db), "proposal", "list"])
        assert result.exit_code == 0
        assert "No proposals found." in result.output

    def test_all_flag_includes_every_state(self, seeded_db: tuple[Path, str, str]) -> None:
        db, author, entity_id = seeded_db
        kb = Ontology.connect(db)
        kb.propose(
            entity_id, "Person.name", "Ada", "Text", author
        )  # write capability -> auto_accepted
        proposals = kb.proposals(state=None)
        kb.close()
        assert (
            len(proposals) == 1
        )  # auto_accepted, invisible to the default "require_review" filter

        default_result = runner.invoke(app, ["--db", str(db), "proposal", "list"])
        assert "No proposals found." in default_result.output

        all_result = runner.invoke(app, ["--db", str(db), "proposal", "list", "--all"])
        assert "auto_accepted" in all_result.output

    def test_state_pending_includes_changes_requested(self, temp_db: Path) -> None:
        """KI-027: request_changes() moves a proposal out of require_review
        with no way to see it alongside other still-open proposals.
        --state pending is an explicit, documented alias that merges both
        (deliberately not the default - see Ontology.proposals's
        docstring)."""
        kb = Ontology.connect(temp_db)
        alice = kb.create_principal("alice@example.com", kind="human", default_capability="write")
        reviewer = kb.create_principal(
            "carol@example.com", kind="human", default_capability="review"
        )
        bot = kb.create_principal(
            "bot@example.com",
            kind="ai",
            auth_method="apikey",
            owner=alice.id,
            default_capability="propose",
        )
        entity = kb.create_entity("Person", author=alice.id)
        proposal, _ = kb.propose(
            entity.id, "Person.born", "1815", "Text", bot.id, model="test-model-v1"
        )
        kb.request_changes(proposal.id, reviewer.id)
        kb.close()

        default_result = runner.invoke(app, ["--db", str(temp_db), "proposal", "list"])
        assert "No proposals found." in default_result.output

        result = runner.invoke(
            app, ["--db", str(temp_db), "proposal", "list", "--state", "pending"]
        )
        assert result.exit_code == 0
        assert proposal.id in result.output
        assert "changes_requested" in result.output


class TestProposalAccept:
    def test_accepts_pending_proposal_and_commits_operations(self, temp_db: Path) -> None:
        kb = Ontology.connect(temp_db)
        alice = kb.create_principal("alice@example.com", kind="human", default_capability="write")
        reviewer = kb.create_principal(
            "carol@example.com", kind="human", default_capability="review"
        )
        bot = kb.create_principal(
            "bot@example.com",
            kind="ai",
            auth_method="apikey",
            owner=alice.id,
            default_capability="propose",
        )
        entity = kb.create_entity("Person", author=alice.id)
        proposal, _ = kb.propose(entity.id, "Person.name", "Ada", "Text", bot.id, model="v1")
        kb.close()

        result = runner.invoke(
            app,
            ["--db", str(temp_db), "proposal", "accept", proposal.id, "--reviewer", reviewer.id],
        )
        assert result.exit_code == 0
        assert proposal.id in result.output
        assert "accepted" in result.output

        kb = Ontology.connect(temp_db)
        stored = kb.backend.get_proposal(proposal.id)
        assert stored is not None
        assert stored.state == "accepted"
        # accept, unlike reject/review, commits the payload through conflict
        # routing (ontology.py:1313) - a state-only check wouldn't catch a
        # regression that flipped the state without replaying operations.
        active = kb.assertions(subject=entity.id, predicate="Person.name")
        assert [a.value for a in active] == ["Ada"]
        kb.close()

    def test_accept_author_flag_is_accepted_as_reviewer_alias(self, temp_db: Path) -> None:
        """--author remains a working alias for --reviewer (pre-review-fix
        flag name), so existing scripts/muscle memory don't break."""
        kb = Ontology.connect(temp_db)
        alice = kb.create_principal("alice@example.com", kind="human", default_capability="write")
        reviewer = kb.create_principal(
            "carol@example.com", kind="human", default_capability="review"
        )
        bot = kb.create_principal(
            "bot@example.com",
            kind="ai",
            auth_method="apikey",
            owner=alice.id,
            default_capability="propose",
        )
        entity = kb.create_entity("Person", author=alice.id)
        proposal, _ = kb.propose(entity.id, "Person.name", "Ada", "Text", bot.id, model="v1")
        kb.close()

        result = runner.invoke(
            app,
            ["--db", str(temp_db), "proposal", "accept", proposal.id, "--author", reviewer.id],
        )
        assert result.exit_code == 0
        assert "accepted" in result.output

    def test_accept_nonexistent_proposal_reports_not_found(self, temp_db: Path) -> None:
        kb = Ontology.connect(temp_db)
        kb.create_principal("carol@example.com", kind="human", default_capability="review")
        kb.close()

        result = runner.invoke(
            app,
            [
                "--db",
                str(temp_db),
                "proposal",
                "accept",
                "nonexistent",
                "--reviewer",
                "carol@example.com",
            ],
        )
        assert result.exit_code == 1
        assert "not found" in result.output.lower()

    def test_accept_without_review_capability_exits_nonzero(
        self, seeded_db: tuple[Path, str, str]
    ) -> None:
        """seeded_db's principal only holds write capability, not review."""
        db, author, _ = seeded_db
        result = runner.invoke(
            app, ["--db", str(db), "proposal", "accept", "nonexistent", "--reviewer", author]
        )
        assert result.exit_code == 1
        assert "lacks review capability" in result.output


class TestProposalReject:
    def test_rejects_pending_proposal_and_records_reason(self, temp_db: Path) -> None:
        kb = Ontology.connect(temp_db)
        alice = kb.create_principal("alice@example.com", kind="human", default_capability="write")
        reviewer = kb.create_principal(
            "carol@example.com", kind="human", default_capability="review"
        )
        bot = kb.create_principal(
            "bot@example.com",
            kind="ai",
            auth_method="apikey",
            owner=alice.id,
            default_capability="propose",
        )
        entity = kb.create_entity("Person", author=alice.id)
        proposal, _ = kb.propose(entity.id, "Person.name", "Ada", "Text", bot.id, model="v1")
        kb.close()

        result = runner.invoke(
            app,
            [
                "--db",
                str(temp_db),
                "proposal",
                "reject",
                proposal.id,
                "--reviewer",
                reviewer.id,
                "--reason",
                "not credible",
            ],
        )
        assert result.exit_code == 0
        assert proposal.id in result.output
        assert "rejected" in result.output

        kb = Ontology.connect(temp_db)
        stored = kb.backend.get_proposal(proposal.id)
        assert stored is not None
        assert stored.state == "rejected"
        events = kb.backend.get_proposal_events(proposal.id)
        assert events[-1].type == "reject"
        assert events[-1].detail == "not credible"
        kb.close()

    def test_reject_nonexistent_proposal_reports_not_found(self, temp_db: Path) -> None:
        kb = Ontology.connect(temp_db)
        kb.create_principal("carol@example.com", kind="human", default_capability="review")
        kb.close()

        result = runner.invoke(
            app,
            [
                "--db",
                str(temp_db),
                "proposal",
                "reject",
                "nonexistent",
                "--reviewer",
                "carol@example.com",
            ],
        )
        assert result.exit_code == 1
        assert "not found" in result.output.lower()

    def test_reject_without_review_capability_exits_nonzero(
        self, seeded_db: tuple[Path, str, str]
    ) -> None:
        db, author, _ = seeded_db
        result = runner.invoke(
            app, ["--db", str(db), "proposal", "reject", "nonexistent", "--reviewer", author]
        )
        assert result.exit_code == 1
        assert "lacks review capability" in result.output


class TestProposalReview:
    def test_requests_changes_on_pending_proposal_and_records_reason(self, temp_db: Path) -> None:
        kb = Ontology.connect(temp_db)
        alice = kb.create_principal("alice@example.com", kind="human", default_capability="write")
        reviewer = kb.create_principal(
            "carol@example.com", kind="human", default_capability="review"
        )
        bot = kb.create_principal(
            "bot@example.com",
            kind="ai",
            auth_method="apikey",
            owner=alice.id,
            default_capability="propose",
        )
        entity = kb.create_entity("Person", author=alice.id)
        proposal, _ = kb.propose(entity.id, "Person.name", "Ada", "Text", bot.id, model="v1")
        kb.close()

        result = runner.invoke(
            app,
            [
                "--db",
                str(temp_db),
                "proposal",
                "review",
                proposal.id,
                "--reviewer",
                reviewer.id,
                "--reason",
                "needs a source",
            ],
        )
        assert result.exit_code == 0
        assert proposal.id in result.output
        assert "changes_requested" in result.output

        kb = Ontology.connect(temp_db)
        stored = kb.backend.get_proposal(proposal.id)
        assert stored is not None
        assert stored.state == "changes_requested"
        events = kb.backend.get_proposal_events(proposal.id)
        assert events[-1].type == "request_changes"
        assert events[-1].detail == "needs a source"
        kb.close()

    def test_review_nonexistent_proposal_reports_not_found(self, temp_db: Path) -> None:
        kb = Ontology.connect(temp_db)
        kb.create_principal("carol@example.com", kind="human", default_capability="review")
        kb.close()

        result = runner.invoke(
            app,
            [
                "--db",
                str(temp_db),
                "proposal",
                "review",
                "nonexistent",
                "--reviewer",
                "carol@example.com",
            ],
        )
        assert result.exit_code == 1
        assert "not found" in result.output.lower()

    def test_review_without_review_capability_exits_nonzero(
        self, seeded_db: tuple[Path, str, str]
    ) -> None:
        db, author, _ = seeded_db
        result = runner.invoke(
            app, ["--db", str(db), "proposal", "review", "nonexistent", "--reviewer", author]
        )
        assert result.exit_code == 1
        assert "lacks review capability" in result.output


class TestProposalResubmit:
    def test_resubmits_proposal_after_changes_requested(self, temp_db: Path) -> None:
        kb = Ontology.connect(temp_db)
        alice = kb.create_principal("alice@example.com", kind="human", default_capability="write")
        reviewer = kb.create_principal(
            "carol@example.com", kind="human", default_capability="review"
        )
        bot = kb.create_principal(
            "bot@example.com",
            kind="ai",
            auth_method="apikey",
            owner=alice.id,
            default_capability="propose",
        )
        entity = kb.create_entity("Person", author=alice.id)
        proposal, _ = kb.propose(entity.id, "Person.name", "Ada", "Text", bot.id, model="v1")
        kb.request_changes(proposal.id, reviewer.id)
        kb.close()

        result = runner.invoke(
            app,
            ["--db", str(temp_db), "proposal", "resubmit", proposal.id, "--author", bot.id],
        )
        assert result.exit_code == 0
        assert proposal.id in result.output
        # bot is ai-kind: ADR-0003's "AI proposals always require review" holds on
        # resubmit too, so the re-evaluated decision lands back on require_review,
        # not auto-accepted.
        assert "decision=RequireReview" in result.output
        assert "reason=AI proposals require review" in result.output

        kb = Ontology.connect(temp_db)
        stored = kb.backend.get_proposal(proposal.id)
        assert stored is not None
        assert stored.state == "require_review"
        kb.close()

    def test_resubmit_unknown_proposal_exits_nonzero(
        self, seeded_db: tuple[Path, str, str]
    ) -> None:
        db, author, _ = seeded_db
        result = runner.invoke(
            app, ["--db", str(db), "proposal", "resubmit", "nonexistent", "--author", author]
        )
        assert result.exit_code == 1
        assert "Error" in result.output


class TestContradictionList:
    def test_lists_open_contradictions_by_default(self, seeded_db: tuple[Path, str, str]) -> None:
        db, author, entity_id = seeded_db
        kb = Ontology.connect(db)
        kb.assert_literal(entity_id, "Person.name", "Ada", "Text", author)
        kb.assert_literal(entity_id, "Person.name", "Ava", "Text", author)  # -> contradiction
        kb.close()

        result = runner.invoke(app, ["--db", str(db), "contradiction", "list"])
        assert result.exit_code == 0
        assert "state=open" in result.output
        assert entity_id in result.output

    def test_no_contradictions_message(self, temp_db: Path) -> None:
        result = runner.invoke(app, ["--db", str(temp_db), "contradiction", "list"])
        assert result.exit_code == 0
        assert "No contradictions found." in result.output

    def test_resolved_excluded_by_default(self, seeded_db: tuple[Path, str, str]) -> None:
        db, author, entity_id = seeded_db
        kb = Ontology.connect(db)
        kb.create_principal("carol@example.com", kind="human", default_capability="review")
        kb.assert_literal(entity_id, "Person.name", "Ada", "Text", author)
        kb.assert_literal(entity_id, "Person.name", "Ava", "Text", author)
        flagged = kb.assertions(subject=entity_id, predicate="Person.name", status="flagged")
        [contradiction] = kb.contradictions()
        kb.resolve_contradiction(contradiction.id, flagged[0].id, "carol@example.com")
        kb.close()

        default_result = runner.invoke(app, ["--db", str(db), "contradiction", "list"])
        assert "No contradictions found." in default_result.output

        all_result = runner.invoke(app, ["--db", str(db), "contradiction", "list", "--all"])
        assert "state=resolved" in all_result.output


class TestContradictionFlag:
    def test_flag_creates_contradiction(self, seeded_db: tuple[Path, str, str]) -> None:
        db, author, entity_id = seeded_db
        kb = Ontology.connect(db)
        kb.assert_literal(entity_id, "Person.name", "Ada", "Text", author)
        first = kb.assertions(subject=entity_id, predicate="Person.name", status="active")[0]

        # Need a second, still-active assertion sharing (subject,
        # predicate) that conflict routing hasn't already flagged —
        # insert directly via backend (mirrors test_rest.py/
        # test_mcp_server.py's own TestFlagContradictionRoute/Tool setup).
        second = Assertion(
            id=kb.id_provider.next(),
            namespace="default",
            subject=entity_id,
            predicate="Person.name",
            value_kind="literal",
            value_type="Text",
            value="Ada Lovelace",
            author=author,
            asserted_at=kb.clock.now(),
            status="active",
        )
        kb.backend.put_assertion(second)
        kb.close()

        result = runner.invoke(
            app,
            [
                "--db",
                str(db),
                "contradiction",
                "flag",
                first.id,
                second.id,
                "--author",
                author,
            ],
        )
        assert result.exit_code == 0
        assert "Created:" in result.output
        assert "members=2" in result.output

        kb = Ontology.connect(db)
        [contradiction] = kb.contradictions()
        assert contradiction.state == "open"
        kb.close()

    def test_flag_unknown_assertion_exits_nonzero(self, seeded_db: tuple[Path, str, str]) -> None:
        db, author, _ = seeded_db
        result = runner.invoke(
            app,
            ["--db", str(db), "contradiction", "flag", "nope-a", "nope-b", "--author", author],
        )
        assert result.exit_code == 1
        assert "Assertion not found" in result.output


class TestContradictionResolve:
    def test_resolves_contradiction(self, seeded_db: tuple[Path, str, str]) -> None:
        db, author, entity_id = seeded_db
        kb = Ontology.connect(db)
        kb.create_principal("carol@example.com", kind="human", default_capability="review")
        kb.assert_literal(entity_id, "Person.name", "Ada", "Text", author)
        kb.assert_literal(entity_id, "Person.name", "Ava", "Text", author)
        flagged = kb.assertions(subject=entity_id, predicate="Person.name", status="flagged")
        [contradiction] = kb.contradictions()
        winner = flagged[0].id
        kb.close()

        result = runner.invoke(
            app,
            [
                "--db",
                str(db),
                "contradiction",
                "resolve",
                contradiction.id,
                "--winner",
                winner,
                "--reviewer",
                "carol@example.com",
            ],
        )
        assert result.exit_code == 0
        assert "Resolved:" in result.output
        assert "state=resolved" in result.output
        assert winner in result.output

        kb = Ontology.connect(db)
        assert kb.backend.get_assertion(winner).status == "active"  # type: ignore[union-attr]
        kb.close()

    def test_resolve_without_review_capability_exits_nonzero(
        self, seeded_db: tuple[Path, str, str]
    ) -> None:
        db, author, entity_id = seeded_db
        kb = Ontology.connect(db)
        kb.assert_literal(entity_id, "Person.name", "Ada", "Text", author)
        kb.assert_literal(entity_id, "Person.name", "Ava", "Text", author)
        flagged = kb.assertions(subject=entity_id, predicate="Person.name", status="flagged")
        [contradiction] = kb.contradictions()
        kb.close()

        result = runner.invoke(
            app,
            [
                "--db",
                str(db),
                "contradiction",
                "resolve",
                contradiction.id,
                "--winner",
                flagged[0].id,
                "--reviewer",
                author,
            ],
        )
        assert result.exit_code == 1
        assert "lacks review capability" in result.output


class TestNamespaceList:
    """KI-022: `ontolith namespace list`."""

    def test_lists_default_namespace(self, temp_db: Path) -> None:
        kb = Ontology.connect(temp_db)
        kb.close()

        result = runner.invoke(app, ["--db", str(temp_db), "namespace", "list"])
        assert result.exit_code == 0
        assert "default" in result.output


class TestSchemaShow:
    """KI-038: `ontolith schema show` - previously the only primary
    interface (SDK/REST/MCP all could) with no way to inspect a registered
    schema at all."""

    def test_no_schema_registered_prints_message(self, temp_db: Path) -> None:
        kb = Ontology.connect(temp_db)
        kb.close()

        result = runner.invoke(app, ["--db", str(temp_db), "schema", "show"])
        assert result.exit_code == 0
        assert "No schema registered" in result.output

    def test_shows_properties_and_relations(self, temp_db: Path) -> None:
        kb = Ontology.connect(temp_db)
        admin = kb.create_principal(
            "admin@example.com", kind="human", auth_method="oidc", default_capability="admin"
        )
        schema = SchemaIR(
            namespace="default",
            version=1,
            concepts={
                "Organization": ConceptDef(
                    name="Organization",
                    properties={
                        "name": PropertyDef(name="name", value_type="Text"),
                        "tags": PropertyDef(name="tags", value_type="Text", cardinality="many"),
                    },
                    relations={
                        "employees": RelationDef(
                            name="employees",
                            target_concept="Person",
                            cardinality="many",
                            inverse="employer",
                        ),
                    },
                ),
                "Person": ConceptDef(
                    name="Person",
                    properties={"name": PropertyDef(name="name", value_type="Text", required=True)},
                    relations={
                        "employer": RelationDef(
                            name="employer",
                            target_concept="Organization",
                            temporality="time_varying",
                            inverse="employees",
                        )
                    },
                ),
            },
        )
        kb.apply_schema(schema, author=admin.id)
        kb.close()

        # Bump to version 2 with the exact same concepts - pins that the
        # printed version tracks the latest applied version, not the first.
        kb = Ontology.connect(temp_db)
        kb.apply_schema(
            SchemaIR(namespace="default", version=2, concepts=schema.concepts), author=admin.id
        )
        kb.close()

        result = runner.invoke(app, ["--db", str(temp_db), "schema", "show"])
        assert result.exit_code == 0
        lines = result.output.splitlines()
        assert lines[0] == "namespace=default  version=2"
        # Concept order and each concept's properties-before-relations
        # ordering is pinned exactly, not just via substring containment.
        assert lines[1:6] == [
            "",
            "Organization",
            "  name: Text  cardinality=single  temporality=static  required=False",
            "  tags: Text  cardinality=many  temporality=static  required=False",
            "  employees -> Person  cardinality=many  temporality=static"
            "  required=False  inverse=employer",
        ]
        assert lines[6:] == [
            "",
            "Person",
            "  name: Text  cardinality=single  temporality=static  required=True",
            "  employer -> Organization  cardinality=single  temporality=time_varying"
            "  required=False  inverse=employees",
        ]

    def test_namespace_option_targets_a_different_namespace(self, temp_db: Path) -> None:
        """Schema registration is namespace-scoped independent of entity
        writes (this project's entity/assertion writes stay single
        -namespace per ADR-0015, but `apply_schema` keys on
        `SchemaIR.namespace` directly - a namespace can have a schema
        without ever having an entity). Proves `--namespace` is threaded
        through to a genuinely different, populated namespace, not merely
        that an empty one prints "not found"."""
        kb = Ontology.connect(temp_db)
        admin = kb.create_principal(
            "admin@example.com", kind="human", auth_method="oidc", default_capability="admin"
        )
        kb.apply_schema(
            SchemaIR(
                namespace="default", version=1, concepts={"Person": ConceptDef(name="Person")}
            ),
            author=admin.id,
        )
        kb.apply_schema(
            SchemaIR(namespace="other", version=1, concepts={"Widget": ConceptDef(name="Widget")}),
            author=admin.id,
        )
        kb.close()

        default_result = runner.invoke(app, ["--db", str(temp_db), "schema", "show"])
        assert "Person" in default_result.output
        assert "Widget" not in default_result.output

        other_result = runner.invoke(
            app, ["--db", str(temp_db), "schema", "show", "--namespace", "other"]
        )
        assert other_result.exit_code == 0
        assert "namespace=other  version=1" in other_result.output
        assert "Widget" in other_result.output
        assert "Person" not in other_result.output

    def test_namespace_with_no_schema_prints_message(self, temp_db: Path) -> None:
        kb = Ontology.connect(temp_db)
        admin = kb.create_principal(
            "admin@example.com", kind="human", auth_method="oidc", default_capability="admin"
        )
        kb.apply_schema(
            SchemaIR(
                namespace="default", version=1, concepts={"Person": ConceptDef(name="Person")}
            ),
            author=admin.id,
        )
        kb.close()

        result = runner.invoke(
            app, ["--db", str(temp_db), "schema", "show", "--namespace", "other"]
        )
        assert result.exit_code == 0
        assert "No schema registered for namespace 'other'" in result.output
        assert "Person" not in result.output


class TestSchemaMigrate:
    """KI-048: `ontolith schema migrate` - a thin wrapper around
    `Ontology.apply_schema` reading a LinkML-aligned YAML file from disk
    (ADR-0034, scope (a) only - no data migration/backfill)."""

    def _admin(self, temp_db: Path) -> str:
        kb = Ontology.connect(temp_db)
        admin = kb.create_principal(
            "admin@example.com", kind="human", auth_method="oidc", default_capability="admin"
        )
        kb.close()
        return admin.id

    def test_applies_first_version(self, temp_db: Path, tmp_path: Path) -> None:
        admin = self._admin(temp_db)
        schema_file = tmp_path / "schema.yaml"
        schema_file.write_text(
            """
            id: default
            version: 1
            classes:
              Person:
                attributes:
                  name:
                    range: string
                    required: true
            """
        )

        result = runner.invoke(
            app,
            ["--db", str(temp_db), "schema", "migrate", str(schema_file), "--author", admin],
        )
        assert result.exit_code == 0
        assert "Applied schema: namespace=default  version=1" in result.output

        kb = Ontology.connect(temp_db)
        ir = kb.backend.get_schema("default")
        kb.close()
        assert ir is not None
        assert ir.version == 1
        assert "Person" in ir.concepts
        assert ir.concepts["Person"].properties["name"].required is True

    def test_applies_next_version_on_top_of_existing(self, temp_db: Path, tmp_path: Path) -> None:
        admin = self._admin(temp_db)
        kb = Ontology.connect(temp_db)
        kb.apply_schema(
            SchemaIR(
                namespace="default", version=1, concepts={"Person": ConceptDef(name="Person")}
            ),
            author=admin,
        )
        kb.close()

        schema_file = tmp_path / "schema.yaml"
        schema_file.write_text(
            """
            id: default
            version: 2
            classes:
              Person:
                attributes:
                  name:
                    range: string
            """
        )

        result = runner.invoke(
            app,
            ["--db", str(temp_db), "schema", "migrate", str(schema_file), "--author", admin],
        )
        assert result.exit_code == 0
        assert "version=2" in result.output

    def test_wrong_version_rejected_same_as_apply_schema(
        self, temp_db: Path, tmp_path: Path
    ) -> None:
        """The file's own `version:` must be the exact next version -
        apply_schema's existing monotonic check, not auto-incremented or
        otherwise papered over by this command (ADR-0034)."""
        admin = self._admin(temp_db)
        schema_file = tmp_path / "schema.yaml"
        schema_file.write_text(
            """
            id: default
            version: 5
            classes: {}
            """
        )

        result = runner.invoke(
            app,
            ["--db", str(temp_db), "schema", "migrate", str(schema_file), "--author", admin],
        )
        assert result.exit_code == 1
        assert "not the next monotonic version" in result.output

    def test_missing_file_reports_error(self, temp_db: Path, tmp_path: Path) -> None:
        admin = self._admin(temp_db)
        missing = tmp_path / "does-not-exist.yaml"

        result = runner.invoke(
            app,
            ["--db", str(temp_db), "schema", "migrate", str(missing), "--author", admin],
        )
        assert result.exit_code == 1
        assert "No such file or directory" in result.output

    def test_malformed_yaml_reports_schema_error(self, temp_db: Path, tmp_path: Path) -> None:
        admin = self._admin(temp_db)
        schema_file = tmp_path / "schema.yaml"
        schema_file.write_text("not: a valid schema document, missing version")

        result = runner.invoke(
            app,
            ["--db", str(temp_db), "schema", "migrate", str(schema_file), "--author", admin],
        )
        assert result.exit_code == 1
        assert "must set 'id' or 'name'" in result.output

    def test_non_admin_author_rejected(self, temp_db: Path, tmp_path: Path) -> None:
        kb = Ontology.connect(temp_db)
        writer = kb.create_principal(
            "writer@example.com", kind="human", auth_method="oidc", default_capability="write"
        )
        kb.close()
        schema_file = tmp_path / "schema.yaml"
        schema_file.write_text(
            """
            id: default
            version: 1
            classes: {}
            """
        )

        result = runner.invoke(
            app,
            [
                "--db",
                str(temp_db),
                "schema",
                "migrate",
                str(schema_file),
                "--author",
                writer.id,
            ],
        )
        assert result.exit_code == 1
        assert "lacks admin capability" in result.output


class TestAdminEventsList:
    """KI-072: `ontolith admin-event list` - the read half of KI-060's
    audit trail for create_principal/apply_schema/register_plugin (token
    issuance/revocation are covered by `principal list-tokens` instead,
    see TestPrincipalTokens)."""

    def test_lists_recorded_events(self, temp_db: Path) -> None:
        kb = Ontology.connect(temp_db)
        kb.create_principal("admin@example.com", kind="human", default_capability="admin")
        kb.create_principal(
            "erin@example.com", kind="human", default_capability="write", author="admin@example.com"
        )
        kb.close()

        result = runner.invoke(
            app,
            ["--db", str(temp_db), "admin-event", "list", "--author", "admin@example.com"],
        )
        assert result.exit_code == 0
        assert "erin@example.com" in result.output
        assert "create_principal" in result.output

    def test_shows_detail_when_recorded(self, temp_db: Path) -> None:
        """No production call site currently passes `detail` - seed one
        directly to pin the CLI's own detail-display branch, not just
        REST's."""
        kb = Ontology.connect(temp_db)
        kb.create_principal("admin@example.com", kind="human", default_capability="admin")
        kb.record_admin_event(
            "admin@example.com", "apply_schema", "default:v1", detail="seeded for test"
        )
        kb.close()

        result = runner.invoke(
            app,
            ["--db", str(temp_db), "admin-event", "list", "--author", "admin@example.com"],
        )
        assert result.exit_code == 0
        assert "seeded for test" in result.output
        assert "admin@example.com" in result.output

    def test_filters_by_target(self, temp_db: Path) -> None:
        kb = Ontology.connect(temp_db)
        kb.create_principal("admin@example.com", kind="human", default_capability="admin")
        kb.create_principal(
            "erin@example.com", kind="human", default_capability="write", author="admin@example.com"
        )
        kb.create_principal(
            "frank@example.com",
            kind="human",
            default_capability="write",
            author="admin@example.com",
        )
        kb.close()

        result = runner.invoke(
            app,
            [
                "--db",
                str(temp_db),
                "admin-event",
                "list",
                "--author",
                "admin@example.com",
                "--target",
                "erin@example.com",
            ],
        )
        assert result.exit_code == 0
        assert "erin@example.com" in result.output
        assert "frank@example.com" not in result.output

    def test_no_events_message(self, temp_db: Path) -> None:
        kb = Ontology.connect(temp_db)
        kb.create_principal("admin@example.com", kind="human", default_capability="admin")
        kb.close()

        result = runner.invoke(
            app,
            ["--db", str(temp_db), "admin-event", "list", "--author", "admin@example.com"],
        )
        assert result.exit_code == 0
        assert "No admin events recorded" in result.output

    def test_non_admin_author_exits_nonzero(self, temp_db: Path) -> None:
        kb = Ontology.connect(temp_db)
        kb.create_principal("alice@example.com", kind="human", default_capability="write")
        kb.close()

        result = runner.invoke(
            app,
            ["--db", str(temp_db), "admin-event", "list", "--author", "alice@example.com"],
        )
        assert result.exit_code == 1
        assert "lacks admin capability" in result.output
