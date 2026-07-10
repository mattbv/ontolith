"""Unit tests for Ontology entry point."""

import tempfile
from datetime import UTC, datetime
from pathlib import Path

import pytest

from ontolith import Ontology
from ontolith.core import FixedClock, SequentialIdProvider
from ontolith.core.errors import (
    AuthError,
    CapabilityError,
    NotFoundError,
    SchemaError,
    ValidationError,
)
from ontolith.schema import ConceptDef, PropertyDef, RelationDef, SchemaIR


@pytest.fixture
def temp_db() -> Path:
    """Create a temporary database file."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        return Path(f.name)


@pytest.fixture
def kb(temp_db: Path) -> Ontology:
    """Create an Ontology instance with deterministic behavior and a seeded principal."""
    clock = FixedClock("2025-01-01T00:00:00Z")
    ids = SequentialIdProvider(prefix="test")
    ontology = Ontology.connect(temp_db, clock=clock, id_provider=ids)
    # Pre-create the principal used across tests. Uses its own string ID,
    # not the SequentialIdProvider, so entity/assertion IDs are unaffected.
    # write capability: this fixture's tests exercise assert_literal/assert_ref
    # (SPEC §9.3 direct writes), which require >= write.
    ontology.create_principal("alice@example.com", kind="human", default_capability="write")
    yield ontology
    ontology.close()
    temp_db.unlink()


class TestOntology:
    """Tests for Ontology class."""

    def test_connect_creates_database(self, temp_db: Path) -> None:
        """Connecting creates the database and schema."""
        kb = Ontology.connect(temp_db)
        assert temp_db.exists()
        kb.close()

    def test_create_entity(self, kb: Ontology) -> None:
        """Entities can be created."""
        entity = kb.create_entity(
            concept="Person",
            author="alice@example.com",
            natural_key="ada",
        )

        assert entity.id == "test-001"  # Sequential ID
        assert entity.concept == "Person"
        assert entity.natural_key == "ada"
        assert entity.namespace == "default"
        assert entity.created_by == "alice@example.com"
        assert entity.created_at == datetime(2025, 1, 1, tzinfo=UTC)

    def test_create_entity_persists(self, kb: Ontology) -> None:
        """Created entities are persisted."""
        entity = kb.create_entity(
            concept="Person",
            author="alice@example.com",
        )

        retrieved = kb.get_entity(entity.id)
        assert retrieved is not None
        assert retrieved.id == entity.id

    def test_create_entity_unknown_author_raises_auth_error(self, kb: Ontology) -> None:
        with pytest.raises(AuthError, match="Principal not found"):
            kb.create_entity("Person", author="nobody@example.com")

    def test_create_entity_read_only_author_raises_capability_error(self, kb: Ontology) -> None:
        kb.create_principal("readonly@example.com", kind="human", default_capability="read")
        with pytest.raises(CapabilityError, match="lacks propose capability"):
            kb.create_entity("Person", author="readonly@example.com")

    def test_create_entity_propose_capability_succeeds(self, kb: Ontology) -> None:
        kb.create_principal("bob@example.com", kind="human", default_capability="propose")
        entity = kb.create_entity("Person", author="bob@example.com")
        assert entity.created_by == "bob@example.com"

    def test_assert_literal(self, kb: Ontology) -> None:
        """Literal assertions can be created."""
        entity = kb.create_entity("Person", author="alice@example.com")

        assertion = kb.assert_literal(
            subject=entity.id,
            predicate="Person.name",
            value="Ada Lovelace",
            value_type="Text",
            author="alice@example.com",
            confidence=0.95,
            source="test",
        )

        assert assertion.id == "test-002"  # Entity was test-001
        assert assertion.subject == entity.id
        assert assertion.predicate == "Person.name"
        assert assertion.value_kind == "literal"
        assert assertion.value_type == "Text"
        assert assertion.value == "Ada Lovelace"
        assert assertion.confidence == 0.95
        assert assertion.source == "test"

    def test_assert_ref(self, kb: Ontology) -> None:
        """Reference assertions can be created."""
        person = kb.create_entity("Person", author="alice@example.com")
        org = kb.create_entity("Organization", author="alice@example.com")

        assertion = kb.assert_ref(
            subject=person.id,
            predicate="Person.employer",
            target=org.id,
            author="alice@example.com",
            confidence=1.0,
        )

        assert assertion.value_kind == "ref"
        assert assertion.value_type is None  # No type for refs
        assert assertion.value == org.id

    def test_assert_literal_requires_write_capability(self, kb: Ontology) -> None:
        """SPEC §9.3: direct writes require >= write capability."""
        kb.create_principal("bob@example.com", kind="human", default_capability="propose")
        entity = kb.create_entity("Person", author="alice@example.com")

        with pytest.raises(CapabilityError, match="lacks write capability"):
            kb.assert_literal(entity.id, "Person.name", "Ada", "Text", "bob@example.com")

    def test_assert_ref_requires_write_capability(self, kb: Ontology) -> None:
        """SPEC §9.3: direct writes require >= write capability."""
        kb.create_principal("bob@example.com", kind="human", default_capability="propose")
        person = kb.create_entity("Person", author="alice@example.com")
        org = kb.create_entity("Organization", author="alice@example.com")

        with pytest.raises(CapabilityError, match="lacks write capability"):
            kb.assert_ref(person.id, "Person.employer", org.id, "bob@example.com")

    def test_assert_literal_rejects_ai_principal_even_with_write_capability(
        self, kb: Ontology
    ) -> None:
        """AI principals never get the direct-write path, even if misconfigured
        with write capability (ADR-0003: AI proposals always require review)."""
        kb.create_principal(
            "bot@example.com",
            kind="ai",
            owner="alice@example.com",
            default_capability="write",
        )
        entity = kb.create_entity("Person", author="alice@example.com")

        with pytest.raises(CapabilityError, match="cannot make direct writes"):
            kb.assert_literal(entity.id, "Person.name", "Ada", "Text", "bot@example.com")

    def test_assert_literal_routes_through_conflict_pipeline(self, kb: Ontology) -> None:
        """Direct writes still go through SPEC §10 conflict routing, not a raw insert."""
        entity = kb.create_entity("Person", author="alice@example.com")
        kb.assert_literal(entity.id, "Person.name", "Ada", "Text", "alice@example.com")
        kb.assert_literal(entity.id, "Person.name", "Ava", "Text", "alice@example.com")

        contradiction = kb.backend.get_open_contradiction("default", entity.id, "Person.name")
        assert contradiction is not None
        flagged = kb.assertions(subject=entity.id, predicate="Person.name", status="flagged")
        assert len(flagged) == 2

    def test_propose_ref_auto_accept_creates_relation(self, kb: Ontology) -> None:
        """propose_ref() mirrors propose() for relations (SPEC §9)."""
        person = kb.create_entity("Person", author="alice@example.com")
        org = kb.create_entity("Organization", author="alice@example.com")

        proposal, decision = kb.propose_ref(
            person.id, "Person.employer", org.id, "alice@example.com"
        )

        assert proposal.state == "auto_accepted"
        active = kb.assertions(subject=person.id, predicate="Person.employer", status="active")
        assert len(active) == 1
        assert active[0].value_kind == "ref"
        assert active[0].value == org.id
        assert active[0].value_type is None

    def test_propose_ref_conflicting_relations_contradict(self, kb: Ontology) -> None:
        """Static relation predicate: conflicting targets flag a contradiction."""
        person = kb.create_entity("Person", author="alice@example.com")
        org1 = kb.create_entity("Organization", author="alice@example.com")
        org2 = kb.create_entity("Organization", author="alice@example.com")

        kb.propose_ref(person.id, "Person.employer", org1.id, "alice@example.com")
        kb.propose_ref(person.id, "Person.employer", org2.id, "alice@example.com")

        contradiction = kb.backend.get_open_contradiction("default", person.id, "Person.employer")
        assert contradiction is not None
        assert len(contradiction.member_ids) == 2

    def test_propose_ref_time_varying_supersedes(self, kb: Ontology) -> None:
        """Schema-declared time_varying relation predicate supersedes, not contradicts."""
        kb.create_principal("admin@example.com", kind="human", default_capability="admin")
        schema = SchemaIR(
            namespace="default",
            version=1,
            concepts={
                "Person": ConceptDef(
                    name="Person",
                    relations={
                        "employer": RelationDef(
                            name="employer",
                            target_concept="Organization",
                            temporality="time_varying",
                        ),
                    },
                ),
                "Organization": ConceptDef(name="Organization"),
            },
        )
        kb.apply_schema(schema, author="admin@example.com")

        person = kb.create_entity("Person", author="alice@example.com")
        org1 = kb.create_entity("Organization", author="alice@example.com")
        org2 = kb.create_entity("Organization", author="alice@example.com")

        kb.propose_ref(person.id, "Person.employer", org1.id, "alice@example.com")
        kb.propose_ref(person.id, "Person.employer", org2.id, "alice@example.com")

        contradiction = kb.backend.get_open_contradiction("default", person.id, "Person.employer")
        assert contradiction is None
        superseded = kb.assertions(
            subject=person.id, predicate="Person.employer", status="superseded"
        )
        assert len(superseded) == 1

    def test_propose_ref_ai_requires_review(self, kb: Ontology) -> None:
        """AI-authored relation proposals require review, same as literal proposals."""
        kb.create_principal(
            "bot@example.com", kind="ai", owner="alice@example.com", default_capability="propose"
        )
        person = kb.create_entity("Person", author="alice@example.com")
        org = kb.create_entity("Organization", author="alice@example.com")

        proposal, decision = kb.propose_ref(
            person.id, "Person.employer", org.id, "bot@example.com", model="test-model-v1"
        )

        assert proposal.state == "require_review"
        active = kb.assertions(subject=person.id, predicate="Person.employer", status="active")
        assert active == []

    def test_accept_proposal_replays_relation(self, kb: Ontology) -> None:
        """Full propose -> require_review -> accept round trip for a relation.

        Also covers acting_as survives replay for the ref branch (regression
        for the bug where accept_proposal dropped delegation provenance) —
        the literal-branch equivalent is pinned in test_trust_delegation.py's
        test_delegation_survives_the_review_accept_path.
        """
        kb.create_principal("carol@example.com", kind="human", default_capability="review")
        kb.create_principal(
            "bot@example.com", kind="ai", owner="alice@example.com", default_capability="propose"
        )
        person = kb.create_entity("Person", author="alice@example.com")
        org = kb.create_entity("Organization", author="alice@example.com")

        proposal, decision = kb.propose_ref(
            person.id,
            "Person.employer",
            org.id,
            "bot@example.com",
            acting_as="alice@example.com",
            model="test-model-v1",
        )
        assert proposal.state == "require_review"

        accepted = kb.accept_proposal(proposal.id, "carol@example.com")
        assert accepted.state == "accepted"

        active = kb.assertions(subject=person.id, predicate="Person.employer", status="active")
        assert len(active) == 1
        assert active[0].value_kind == "ref"
        assert active[0].value == org.id
        assert active[0].proposal_id == proposal.id
        assert active[0].acting_as == "alice@example.com"

    def test_query_assertions_by_subject(self, kb: Ontology) -> None:
        """Assertions can be queried by subject."""
        entity = kb.create_entity("Person", author="alice@example.com")
        kb.assert_literal(entity.id, "Person.name", "Ada", "Text", author="alice@example.com")
        kb.assert_literal(
            entity.id, "Person.born", "1815-12-10", "Date", author="alice@example.com"
        )

        assertions = kb.assertions(subject=entity.id)
        assert len(assertions) == 2
        assert {a.predicate for a in assertions} == {"Person.name", "Person.born"}

    def test_query_assertions_by_predicate(self, kb: Ontology) -> None:
        """Assertions can be queried by predicate."""
        e1 = kb.create_entity("Person", author="alice@example.com")
        e2 = kb.create_entity("Person", author="alice@example.com")

        kb.assert_literal(e1.id, "Person.name", "Ada", "Text", author="alice@example.com")
        kb.assert_literal(e2.id, "Person.name", "Grace", "Text", author="alice@example.com")

        assertions = kb.assertions(predicate="Person.name")
        assert len(assertions) == 2
        assert {a.value for a in assertions} == {"Ada", "Grace"}

    def test_assertions_default_to_active_only(self, kb: Ontology) -> None:
        """Assertions query defaults to active status only."""
        entity = kb.create_entity("Person", author="alice@example.com")
        assertion = kb.assert_literal(
            entity.id, "Person.name", "Ada", "Text", author="alice@example.com"
        )

        # Retract it
        kb.backend.set_assertion_status(assertion.id, "retracted")

        # Default query doesn't return retracted
        active = kb.assertions(subject=entity.id)
        assert len(active) == 0

        # Explicit status=None returns all
        all_assertions = kb.assertions(subject=entity.id, status=None)
        assert len(all_assertions) == 1
        assert all_assertions[0].status == "retracted"

    def test_end_to_end_workflow(self, kb: Ontology) -> None:
        """Complete workflow: create entity, assert properties, query."""
        # Create entity
        person = kb.create_entity(
            concept="Person",
            author="alice@example.com",
            natural_key="ada",
        )

        # Make assertions
        kb.assert_literal(
            person.id,
            "Person.name",
            "Ada Lovelace",
            "Text",
            author="alice@example.com",
            source="Wikipedia",
            confidence=1.0,
        )

        kb.assert_literal(
            person.id,
            "Person.born",
            "1815-12-10",
            "Date",
            author="alice@example.com",
            confidence=1.0,
        )

        # Query by natural key (simulate)
        retrieved = kb.get_entity(person.id)
        assert retrieved is not None
        assert retrieved.natural_key == "ada"

        # Get all facts about the person
        facts = kb.assertions(subject=person.id)
        assert len(facts) == 2

        # Verify provenance
        name_assertion = next(a for a in facts if a.predicate == "Person.name")
        assert name_assertion.author == "alice@example.com"
        assert name_assertion.source == "Wikipedia"
        assert name_assertion.confidence == 1.0

    def test_create_principal(self, kb: Ontology) -> None:
        """Principals can be created."""
        principal = kb.create_principal(
            "bob@example.com",
            kind="human",
            auth_method="oidc",
            default_capability="write",
            trust_level=10,
        )

        assert principal.id == "bob@example.com"
        assert principal.kind == "human"
        assert principal.default_capability == "write"
        assert principal.trust_level == 10

    def test_create_principal_persists(self, kb: Ontology) -> None:
        """Created principals are persisted."""
        kb.create_principal("bob@example.com", kind="human")

        retrieved = kb.get_principal("bob@example.com")
        assert retrieved is not None
        assert retrieved.id == "bob@example.com"

    def test_create_ai_principal_with_owner(self, kb: Ontology) -> None:
        """AI principals require an owner."""
        principal = kb.create_principal(
            "research-bot",
            kind="ai",
            owner="alice@example.com",
            auth_method="workload",
            metadata={"model": "claude-sonnet-4"},
        )

        assert principal.kind == "ai"
        assert principal.owner == "alice@example.com"
        assert principal.metadata["model"] == "claude-sonnet-4"


class TestModelCapture:
    """model is required for ai-kind authors on the governed proposal path
    (SPEC §7.4/§14.4); optional and never required for human/service authors.
    """

    def test_propose_ai_without_model_raises_validation_error(self, kb: Ontology) -> None:
        kb.create_principal(
            "bot@example.com", kind="ai", owner="alice@example.com", default_capability="propose"
        )
        entity = kb.create_entity("Person", author="alice@example.com")

        with pytest.raises(ValidationError, match="model is required for ai-kind authors"):
            kb.propose(entity.id, "Person.name", "Ada", "Text", "bot@example.com")

    def test_propose_ref_ai_without_model_raises_validation_error(self, kb: Ontology) -> None:
        kb.create_principal(
            "bot@example.com", kind="ai", owner="alice@example.com", default_capability="propose"
        )
        person = kb.create_entity("Person", author="alice@example.com")
        org = kb.create_entity("Organization", author="alice@example.com")

        with pytest.raises(ValidationError, match="model is required for ai-kind authors"):
            kb.propose_ref(person.id, "Person.employer", org.id, "bot@example.com")

    def test_propose_ai_with_model_succeeds_and_round_trips(self, kb: Ontology) -> None:
        kb.create_principal(
            "bot@example.com", kind="ai", owner="alice@example.com", default_capability="propose"
        )
        entity = kb.create_entity("Person", author="alice@example.com")

        proposal, decision = kb.propose(
            entity.id, "Person.name", "Ada", "Text", "bot@example.com", model="claude-sonnet-4"
        )
        assert proposal.state == "require_review"

        # Round-trips through accept_proposal's replay and SQLite persistence
        kb.create_principal("carol@example.com", kind="human", default_capability="review")
        kb.accept_proposal(proposal.id, "carol@example.com")
        active = kb.assertions(subject=entity.id, predicate="Person.name", status="active")
        assert len(active) == 1
        assert active[0].model == "claude-sonnet-4"

    def test_propose_human_author_model_optional(self, kb: Ontology) -> None:
        """Human/service authors are never required to pass model."""
        entity = kb.create_entity("Person", author="alice@example.com")

        proposal, decision = kb.propose(
            entity.id, "Person.name", "Ada", "Text", "alice@example.com"
        )
        assert proposal.state == "auto_accepted"
        active = kb.assertions(subject=entity.id, predicate="Person.name", status="active")
        assert active[0].model is None

    def test_assert_literal_model_optional_for_write_capable_human(self, kb: Ontology) -> None:
        """Direct writes are human/service-only (AI hard-blocked), so model is
        accepted but never required here — still round-trips when provided."""
        entity = kb.create_entity("Person", author="alice@example.com")

        assertion = kb.assert_literal(
            entity.id, "Person.name", "Ada", "Text", "alice@example.com", model="unused-model"
        )
        assert assertion.model == "unused-model"


class TestApplySchema:
    """Tests for Ontology.apply_schema (SPEC §6, capability-checked schema persistence)."""

    def test_admin_can_apply_first_schema_version(self, kb: Ontology) -> None:
        kb.create_principal("admin@example.com", kind="human", default_capability="admin")
        schema = SchemaIR(namespace="default", version=1, concepts={})

        applied = kb.apply_schema(schema, author="admin@example.com")

        assert applied.version == 1
        assert kb.backend.get_schema("default") is not None

    def test_non_admin_raises_capability_error(self, kb: Ontology) -> None:
        """alice@example.com defaults to 'propose' capability, not 'admin'."""
        schema = SchemaIR(namespace="default", version=1, concepts={})

        with pytest.raises(CapabilityError, match="lacks admin capability"):
            kb.apply_schema(schema, author="alice@example.com")

    def test_unknown_author_raises_auth_error(self, kb: Ontology) -> None:
        schema = SchemaIR(namespace="default", version=1, concepts={})

        with pytest.raises(AuthError, match="Principal not found"):
            kb.apply_schema(schema, author="nobody@example.com")

    def test_second_version_must_be_monotonic(self, kb: Ontology) -> None:
        kb.create_principal("admin@example.com", kind="human", default_capability="admin")
        kb.apply_schema(
            SchemaIR(namespace="default", version=1, concepts={}), author="admin@example.com"
        )

        applied = kb.apply_schema(
            SchemaIR(namespace="default", version=2, concepts={}), author="admin@example.com"
        )
        assert applied.version == 2

    def test_skipping_a_version_raises_schema_error(self, kb: Ontology) -> None:
        kb.create_principal("admin@example.com", kind="human", default_capability="admin")
        kb.apply_schema(
            SchemaIR(namespace="default", version=1, concepts={}), author="admin@example.com"
        )

        with pytest.raises(SchemaError, match="expected 2"):
            kb.apply_schema(
                SchemaIR(namespace="default", version=3, concepts={}), author="admin@example.com"
            )

    def test_first_version_must_be_one(self, kb: Ontology) -> None:
        kb.create_principal("admin@example.com", kind="human", default_capability="admin")

        with pytest.raises(SchemaError, match="expected 1"):
            kb.apply_schema(
                SchemaIR(namespace="default", version=2, concepts={}), author="admin@example.com"
            )


class TestAcceptProposalReResolvesTemporality:
    """accept_proposal must re-resolve temporality from the current schema at
    apply time, not trust the snapshot stored at propose time — otherwise a
    schema migration between propose and accept silently misroutes SPEC §10
    conflict handling (a static predicate could be superseded instead of
    flagged as a contradiction)."""

    def test_schema_change_between_propose_and_accept_uses_apply_time_temporality(
        self, kb: Ontology
    ) -> None:
        kb.create_principal("admin@example.com", kind="human", default_capability="admin")
        kb.create_principal("carol@example.com", kind="human", default_capability="review")
        kb.create_principal(
            "bot@example.com", kind="ai", owner="alice@example.com", default_capability="propose"
        )

        kb.apply_schema(
            SchemaIR(
                namespace="default",
                version=1,
                concepts={
                    "Person": ConceptDef(
                        name="Person",
                        properties={
                            "name": PropertyDef(
                                name="name", value_type="Text", temporality="static"
                            )
                        },
                    )
                },
            ),
            author="admin@example.com",
        )

        entity = kb.create_entity("Person", author="alice@example.com")
        kb.assert_literal(entity.id, "Person.name", "Ada", "Text", "alice@example.com")

        proposal, decision = kb.propose(
            entity.id, "Person.name", "Ava", "Text", "bot@example.com", model="test-model-v1"
        )
        assert proposal.state == "require_review"

        # Schema migrates to time_varying before the proposal is reviewed.
        kb.apply_schema(
            SchemaIR(
                namespace="default",
                version=2,
                concepts={
                    "Person": ConceptDef(
                        name="Person",
                        properties={
                            "name": PropertyDef(
                                name="name", value_type="Text", temporality="time_varying"
                            )
                        },
                    )
                },
            ),
            author="admin@example.com",
        )

        kb.accept_proposal(proposal.id, "carol@example.com")

        # Routed as time_varying (current schema at accept time): Ada
        # superseded, Ava active — NOT both flagged as a static contradiction.
        active = kb.assertions(subject=entity.id, predicate="Person.name", status="active")
        superseded = kb.assertions(subject=entity.id, predicate="Person.name", status="superseded")
        flagged = kb.assertions(subject=entity.id, predicate="Person.name", status="flagged")
        assert [a.value for a in active] == ["Ava"]
        assert [a.value for a in superseded] == ["Ada"]
        assert flagged == []


class TestFlagContradiction:
    """Ontology.flag_contradiction() — propose-level capability gate (ADR-0008)."""

    def _conflicting_assertions(self, kb: Ontology) -> tuple[str, str, str]:
        """Two active assertions on the same (subject, predicate) with different
        values, inserted directly (bypassing conflict routing) so they don't
        already form a contradiction before flag_contradiction() is called."""
        from ontolith.core import Assertion

        entity = kb.create_entity("Person", author="alice@example.com")
        a = Assertion(
            id=kb.id_provider.next(),
            namespace="default",
            subject=entity.id,
            predicate="Person.name",
            value_kind="literal",
            value_type="Text",
            value="Ada",
            author="alice@example.com",
            asserted_at=kb.clock.now(),
            status="active",
        )
        b = Assertion(
            id=kb.id_provider.next(),
            namespace="default",
            subject=entity.id,
            predicate="Person.name",
            value_kind="literal",
            value_type="Text",
            value="Ava",
            author="alice@example.com",
            asserted_at=kb.clock.now(),
            status="active",
        )
        kb.backend.put_assertion(a)
        kb.backend.put_assertion(b)
        return entity.id, a.id, b.id

    def test_read_capability_raises_capability_error(self, kb: Ontology) -> None:
        """Previously there was no capability check at all — the HIGH finding."""
        kb.create_principal("readonly@example.com", kind="human", default_capability="read")
        _, a_id, b_id = self._conflicting_assertions(kb)

        with pytest.raises(CapabilityError, match="lacks propose capability"):
            kb.flag_contradiction(a_id, b_id, "readonly@example.com")

    def test_propose_capability_succeeds(self, kb: Ontology) -> None:
        kb.create_principal("proposer@example.com", kind="human", default_capability="propose")
        _, a_id, b_id = self._conflicting_assertions(kb)

        contradiction, action = kb.flag_contradiction(a_id, b_id, "proposer@example.com")
        assert action == "created"
        assert set(contradiction.member_ids) >= {a_id, b_id}

    def test_unknown_author_raises_auth_error(self, kb: Ontology) -> None:
        _, a_id, b_id = self._conflicting_assertions(kb)
        with pytest.raises(AuthError, match="Principal not found"):
            kb.flag_contradiction(a_id, b_id, "nobody@example.com")

    def test_unknown_assertion_raises_not_found(self, kb: Ontology) -> None:
        _, a_id, _ = self._conflicting_assertions(kb)
        with pytest.raises(NotFoundError, match="Assertion not found"):
            kb.flag_contradiction(a_id, "nonexistent", "alice@example.com")

    def test_mismatched_subject_predicate_raises_validation_error(self, kb: Ontology) -> None:
        entity = kb.create_entity("Person", author="alice@example.com")
        a = kb.assert_literal(entity.id, "Person.name", "Ada", "Text", "alice@example.com")
        b = kb.assert_literal(entity.id, "Person.born", "1815", "Text", "alice@example.com")

        with pytest.raises(ValidationError, match="same subject and predicate"):
            kb.flag_contradiction(a.id, b.id, "alice@example.com")

    def test_rationale_persisted_in_contradiction_metadata(self, kb: Ontology) -> None:
        _, a_id, b_id = self._conflicting_assertions(kb)
        contradiction, _ = kb.flag_contradiction(
            a_id, b_id, "alice@example.com", rationale="Sources disagree"
        )
        assert contradiction.metadata.get("rationale") == "Sources disagree"

    def test_third_conflicting_assertion_extends_existing_contradiction(self, kb: Ontology) -> None:
        entity_id, a_id, b_id = self._conflicting_assertions(kb)
        first, _ = kb.flag_contradiction(a_id, b_id, "alice@example.com")

        c_assertion = kb.assert_literal(
            entity_id, "Person.name", "Eve", "Text", "alice@example.com"
        )
        second, action = kb.flag_contradiction(a_id, c_assertion.id, "alice@example.com")

        assert action == "extended"
        assert second.id == first.id
        assert set(second.member_ids) == {a_id, b_id, c_assertion.id}
