"""Ontolith CLI — command-line interface for knowledge base management."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from ontolith import Ontology
from ontolith.schema.linkml import from_yaml

app = typer.Typer(
    name="ontolith",
    help="Ontolith knowledge base CLI.",
    no_args_is_help=True,
)
principal_app = typer.Typer(help="Manage principals.", no_args_is_help=True)
entity_app = typer.Typer(help="Manage entities.", no_args_is_help=True)
proposal_app = typer.Typer(help="Inspect and act on proposals.", no_args_is_help=True)
contradiction_app = typer.Typer(help="Inspect and act on contradictions.", no_args_is_help=True)
namespace_app = typer.Typer(help="Inspect namespaces.", no_args_is_help=True)
schema_app = typer.Typer(help="Inspect and apply the schema.", no_args_is_help=True)
app.add_typer(principal_app, name="principal")
app.add_typer(entity_app, name="entity")
app.add_typer(proposal_app, name="proposal")
app.add_typer(contradiction_app, name="contradiction")
app.add_typer(namespace_app, name="namespace")
app.add_typer(schema_app, name="schema")

# Module-level DB path, set by the root callback before any command runs.
_db_path: Path = Path("ontolith.db")


def _kb() -> Ontology:
    """Connect to the KB at the module-level `_db_path`."""
    return Ontology.connect(_db_path)


@app.callback()
def root(
    db: Annotated[
        Path,
        typer.Option("--db", envvar="ONTOLITH_DB", help="Path to database file."),
    ] = Path("ontolith.db"),
) -> None:
    """Ontolith — collaborative knowledge base."""
    global _db_path
    _db_path = db


# ─── principal ────────────────────────────────────────────────────────────────


@principal_app.command("create")
def principal_create(
    principal_id: Annotated[str, typer.Argument(help="Principal ID (email or slug).")],
    kind: Annotated[str, typer.Option("--kind", help="Principal type: human, ai, or service.")],
    author: Annotated[
        str | None,
        typer.Option(
            "--author",
            help=(
                "Admin-capability principal creating this one. Required except for the "
                "very first principal in a fresh database, which has no admin yet to "
                "name (bootstrap: use the SDK directly, `Ontology.create_principal(...)`, "
                "for that one call — matches this CLI's own implicitly-trusted local "
                "access, same as every other principal-mutating command here)."
            ),
        ),
    ] = None,
    owner: Annotated[
        str | None,
        typer.Option("--owner", help="Accountable owner (required for AI principals)."),
    ] = None,
    capability: Annotated[
        str,
        typer.Option(
            "--capability", help="Default capability: read, propose, write, review, admin."
        ),
    ] = "propose",
    trust_level: Annotated[int, typer.Option("--trust-level", help="Trust score 0-10.")] = 0,
) -> None:
    """Create a new principal. Requires an existing admin principal named via
    `--author` (KI-054) — the one exception is bootstrapping the very first
    principal in a fresh database, where `--author` may be omitted."""
    kb = _kb()
    try:
        if author is not None:
            kb.require_admin(author)
        elif kb.backend.list_principals():
            # kb.backend (the raw port method) rather than kb.list_principals
            # (which itself requires admin - can't use it to detect whether
            # bootstrapping is legitimate without an admin already existing).
            typer.echo(
                "Error: --author is required (omit only to create the very first "
                "principal in a brand-new database). If this database has no admin "
                "principal at all yet, use the SDK directly instead: "
                'Ontology.connect(...).create_principal(..., default_capability="admin").',
                err=True,
            )
            raise typer.Exit(1)
        p = kb.create_principal(
            principal_id,
            kind=kind,
            owner=owner,
            default_capability=capability,
            trust_level=trust_level,
            author=author,
        )
        typer.echo(f"Created principal: {p.id}  kind={p.kind}  capability={p.default_capability}")
    except typer.Exit:
        raise
    except Exception as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from None
    finally:
        kb.close()


@principal_app.command("list")
def principal_list(
    author: Annotated[
        str, typer.Option("--author", help="Admin-capability principal performing the lookup.")
    ],
) -> None:
    """List all principals (KI-022). Requires the `--author` principal to hold `admin` capability."""
    kb = _kb()
    try:
        # No empty-list case to handle: require_admin(author) guarantees at
        # least `author` itself exists as a principal.
        for p in kb.list_principals(author=author):
            typer.echo(f"{p.id}  kind={p.kind}  capability={p.default_capability}")
    except Exception as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from None
    finally:
        kb.close()


@principal_app.command("issue-token")
def principal_issue_token(
    principal_id: Annotated[str, typer.Argument(help="Principal ID to issue a token for.")],
    author: Annotated[
        str, typer.Option("--author", help="Admin-capability principal performing the issuance.")
    ],
) -> None:
    """Issue a new API-key token for a principal (ADR-0014).

    The raw token is printed ONCE — it is not stored anywhere and cannot be
    recovered. Save it immediately; use the printed credential ID (not the
    token) to revoke it later via `principal revoke-token`. Requires the
    `--author` principal to hold `admin` capability.
    """
    kb = _kb()
    try:
        token, credential_id = kb.issue_token(principal_id, author=author)
        typer.echo(f"Token for {principal_id}: {token}")
        typer.echo(f"Credential ID (for revocation): {credential_id}")
        typer.echo("Store this token now — it will not be shown again.")
    except Exception as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from None
    finally:
        kb.close()


@principal_app.command("revoke-token")
def principal_revoke_token(
    credential_id: Annotated[str, typer.Argument(help="Credential ID to revoke.")],
    author: Annotated[
        str, typer.Option("--author", help="Admin-capability principal performing the revocation.")
    ],
) -> None:
    """Revoke a previously issued API-key token by its credential ID.

    Requires the `--author` principal to hold `admin` capability.
    """
    kb = _kb()
    try:
        kb.revoke_token(credential_id, author=author)
        typer.echo(f"Revoked credential: {credential_id}")
    except Exception as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from None
    finally:
        kb.close()


@principal_app.command("list-tokens")
def principal_list_tokens(
    principal_id: Annotated[str, typer.Argument(help="Principal ID to list credentials for.")],
    author: Annotated[
        str, typer.Option("--author", help="Admin-capability principal performing the lookup.")
    ],
) -> None:
    """List credentials issued to a principal. Never shows the raw token or its hash.

    Requires the `--author` principal to hold `admin` capability.
    """
    kb = _kb()
    try:
        credentials = kb.list_tokens(principal_id, author=author)
        if not credentials:
            typer.echo(f"No credentials issued for {principal_id}.")
            return
        for c in credentials:
            status = f"revoked at {c.revoked_at.isoformat()}" if c.revoked_at else "active"
            typer.echo(f"{c.id}  issued={c.created_at.isoformat()}  {status}")
    except Exception as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from None
    finally:
        kb.close()


# ─── entity ───────────────────────────────────────────────────────────────────


@entity_app.command("create")
def entity_create(
    concept: Annotated[str, typer.Option("--concept", help="Concept name (e.g. Person).")],
    author: Annotated[str, typer.Option("--author", help="Principal ID of the author.")],
    key: Annotated[
        str | None,
        typer.Option("--key", help="Natural key within concept."),
    ] = None,
) -> None:
    """Create a new entity."""
    kb = _kb()
    try:
        entity = kb.create_entity(concept=concept, author=author, natural_key=key)
        typer.echo(f"Created entity: {entity.id}  concept={entity.concept}")
    except Exception as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from None
    finally:
        kb.close()


# ─── assert ───────────────────────────────────────────────────────────────────


@app.command("assert")
def assert_literal(
    subject: Annotated[str, typer.Argument(help="Subject entity ID.")],
    predicate: Annotated[str, typer.Argument(help="Predicate (e.g. Person.name).")],
    value: Annotated[str, typer.Argument(help="Literal value.")],
    value_type: Annotated[str, typer.Option("--type", help="Value type (Text, Integer, Date, …).")],
    author: Annotated[str, typer.Option("--author", help="Principal ID of the asserting author.")],
    confidence: Annotated[
        float | None,
        typer.Option("--confidence", help="Confidence score 0.0–1.0."),
    ] = None,
    source: Annotated[
        str | None,
        typer.Option("--source", help="Source of the information."),
    ] = None,
) -> None:
    """Make a literal assertion about an entity — a direct write (SPEC §9.3).

    Requires `write` (or `admin`) capability; bypasses the proposal queue but
    still passes through SPEC §10 conflict routing. For the governed
    propose → review → accept path, use the SDK's `Ontology.propose()`.
    """
    kb = _kb()
    try:
        a = kb.assert_literal(
            subject=subject,
            predicate=predicate,
            value=value,
            value_type=value_type,
            author=author,
            confidence=confidence,
            source=source,
        )
        typer.echo(f"Asserted: {a.id}  {a.predicate}={a.value!r}  by={a.author}")
    except Exception as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from None
    finally:
        kb.close()


# ─── assertions ───────────────────────────────────────────────────────────────


@app.command("assertions")
def list_assertions(
    subject: Annotated[
        str | None,
        typer.Option("--subject", help="Filter by subject entity ID."),
    ] = None,
    predicate: Annotated[
        str | None,
        typer.Option("--predicate", help="Filter by predicate."),
    ] = None,
    include_inactive: Annotated[
        bool,
        typer.Option("--all", help="Include non-active assertions."),
    ] = False,
) -> None:
    """List assertions, optionally filtered by subject or predicate."""
    kb = _kb()
    try:
        status = None if include_inactive else "active"
        results = kb.assertions(subject=subject, predicate=predicate, status=status)
        if not results:
            typer.echo("No assertions found.")
            return
        for a in results:
            conf = f"  confidence={a.confidence}" if a.confidence is not None else ""
            typer.echo(f"{a.id}  {a.predicate}={a.value!r}  by={a.author}  status={a.status}{conf}")
    except Exception as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from None
    finally:
        kb.close()


# ─── retract ──────────────────────────────────────────────────────────────────


@app.command("retract")
def retract_assertion(
    assertion_id: Annotated[str, typer.Argument(help="Assertion ID to retract.")],
    author: Annotated[
        str,
        typer.Option("--author", help="Principal ID of the retracting author."),
    ],
    acting_as: Annotated[
        str | None,
        typer.Option("--acting-as", help="Retract on behalf of another principal (delegation)."),
    ] = None,
) -> None:
    """Propose retraction of an assertion through the governed
    proposal/policy pipeline (SPEC §9). Does NOT delete or write directly —
    same propose/policy/conflict-routing pipeline as `ontolith assert`'s
    governed counterpart. May auto-accept, require review, or route through
    a contradiction's own capability floor depending on policy and the
    assertion's current state.
    """
    kb = _kb()
    try:
        proposal, decision = kb.retract(assertion_id, author=author, acting_as=acting_as)
        typer.echo(
            f"Retract proposed: {proposal.id}  state={proposal.state}  "
            f"decision={type(decision).__name__}  reason={proposal.policy_reason or '-'}"
        )
    except Exception as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from None
    finally:
        kb.close()


# ─── query ────────────────────────────────────────────────────────────────────


@app.command("query")
def query_entities(
    concept: Annotated[str, typer.Argument(help="Concept to query (e.g. Person).")],
    where: Annotated[
        list[str] | None,
        typer.Option(
            "--where",
            help="Filter as KEY=VALUE (repeatable). KEY may be a bare property/relation "
            "name (equality) or use a __contains/__gt/__lt/__gte/__lte suffix (KI-039), "
            "e.g. --where age__gte=18.",
        ),
    ] = None,
) -> None:
    """Query entities by concept and optional property filters."""
    kb = _kb()
    try:
        q = kb.query(concept)
        if where:
            filters: dict[str, str] = {}
            for item in where:
                if "=" not in item:
                    typer.echo(f"Invalid filter (expected KEY=VALUE): {item!r}", err=True)
                    raise typer.Exit(1)
                k, _, v = item.partition("=")
                filters[k.strip()] = v.strip()
            q = q.where(**filters)
        results = q.all()
        if not results:
            typer.echo("No entities found.")
            return
        for entity in results:
            typer.echo(f"{entity.id}  concept={entity.concept}  key={entity.natural_key or '-'}")
    except typer.Exit:
        raise
    except Exception as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from None
    finally:
        kb.close()


@app.command("reindex")
def reindex(
    concept: Annotated[
        str | None,
        typer.Option("--concept", help="Only re-index entities of this concept."),
    ] = None,
) -> None:
    """Re-embed entities' Text content into the vector index (SPEC §11.3)."""
    kb = _kb()
    try:
        count = kb.reindex(concept=concept)
        typer.echo(f"Reindexed {count} entit{'y' if count == 1 else 'ies'}.")
    except Exception as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from None
    finally:
        kb.close()


# ─── proposal ─────────────────────────────────────────────────────────────────


@proposal_app.command("list")
def list_proposals(
    state: Annotated[
        str | None,
        typer.Option("--state", help="Filter by state (default: require_review)."),
    ] = "require_review",
    all_states: Annotated[
        bool,
        typer.Option("--all", help="Show proposals in every state, ignoring --state."),
    ] = False,
) -> None:
    """List proposals, defaulting to those pending review.

    Without this, a reviewer has no way to discover what route_to_review
    (SPEC §10.3) routed to them short of querying the backend directly.
    ``--state pending`` merges ``require_review`` and ``changes_requested``
    — see ``Ontology.proposals`` (KI-027) for why that isn't the default.
    """
    kb = _kb()
    try:
        results = kb.proposals(state=None if all_states else state)
        if not results:
            typer.echo("No proposals found.")
            return
        for p in results:
            typer.echo(f"{p.id}  state={p.state}  author={p.author}  created={p.created_at}")
    except Exception as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from None
    finally:
        kb.close()


@proposal_app.command("accept")
def accept_proposal(
    proposal_id: Annotated[str, typer.Argument(help="Proposal ID to accept.")],
    reviewer: Annotated[
        str,
        typer.Option("--reviewer", "--author", help="Reviewer principal accepting this proposal."),
    ],
) -> None:
    """Accept a pending proposal, committing its operations (SPEC §9).

    Requires the `--reviewer` principal to hold `review` (or `admin`)
    capability, not be AI-kind, and not be the proposal's own author or
    delegate (SPEC §9.4). The proposal must be in `require_review` or
    `under_review` state — a `changes_requested` proposal must go through
    `proposal resubmit` (KI-027) first.
    """
    kb = _kb()
    try:
        proposal = kb.accept_proposal(proposal_id, reviewer)
        typer.echo(f"Accepted: {proposal.id}  state={proposal.state}")
    except Exception as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from None
    finally:
        kb.close()


@proposal_app.command("reject")
def reject_proposal(
    proposal_id: Annotated[str, typer.Argument(help="Proposal ID to reject.")],
    reviewer: Annotated[
        str,
        typer.Option("--reviewer", "--author", help="Reviewer principal rejecting this proposal."),
    ],
    reason: Annotated[
        str, typer.Option("--reason", help="Optional explanation for the rejection.")
    ] = "",
) -> None:
    """Reject a pending proposal. No operations are applied (SPEC §9).

    Requires the `--reviewer` principal to hold `review` (or `admin`)
    capability, not be AI-kind, and not be the proposal's own author or
    delegate (SPEC §9.4). The proposal must be in `require_review` or
    `under_review` state.
    """
    kb = _kb()
    try:
        proposal = kb.reject_proposal(proposal_id, reviewer, reason=reason)
        typer.echo(f"Rejected: {proposal.id}  state={proposal.state}")
    except Exception as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from None
    finally:
        kb.close()


@proposal_app.command("review")
def review_proposal(
    proposal_id: Annotated[str, typer.Argument(help="Proposal ID to request changes on.")],
    reviewer: Annotated[
        str,
        typer.Option("--reviewer", "--author", help="Reviewer principal requesting changes."),
    ],
    reason: Annotated[
        str, typer.Option("--reason", help="Optional explanation of what needs to change.")
    ] = "",
) -> None:
    """Request changes on a pending proposal — does not accept or reject it
    (SPEC §9.1/§9.4).

    Moves the proposal to `changes_requested`; its author or delegate can
    then move it back to `submitted` via `proposal resubmit` (KI-027).
    Requires the `--reviewer` principal to hold `review` (or `admin`)
    capability, not be AI-kind, and not be the proposal's own author or
    delegate. The proposal must be in `require_review` or `under_review`
    state.
    """
    kb = _kb()
    try:
        proposal = kb.request_changes(proposal_id, reviewer, reason=reason)
        typer.echo(f"Changes requested: {proposal.id}  state={proposal.state}")
    except Exception as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from None
    finally:
        kb.close()


@proposal_app.command("resubmit")
def resubmit_proposal(
    proposal_id: Annotated[str, typer.Argument(help="Proposal ID to resubmit.")],
    author: Annotated[
        str,
        typer.Option("--author", help="Proposal's own author (or delegate) resubmitting it."),
    ],
) -> None:
    """Resubmit a proposal after changes were requested (SPEC §9.1, KI-027).

    Only the proposal's own author or delegating principal may call this —
    the payload is replayed unedited through a fresh policy evaluation
    (which may auto-accept, require review again, or reject — resubmitting
    does not guarantee acceptance). The proposal must be in
    `changes_requested` state.
    """
    kb = _kb()
    try:
        proposal, decision = kb.resubmit(proposal_id, author)
        typer.echo(
            f"Resubmitted: {proposal.id}  state={proposal.state}  "
            f"decision={type(decision).__name__}  reason={proposal.policy_reason or '-'}"
        )
    except Exception as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from None
    finally:
        kb.close()


# ─── contradiction ────────────────────────────────────────────────────────────


@contradiction_app.command("list")
def list_contradictions(
    state: Annotated[
        str | None,
        typer.Option("--state", help="Filter by state (default: open)."),
    ] = "open",
    all_states: Annotated[
        bool,
        typer.Option("--all", help="Show contradictions in every state, ignoring --state."),
    ] = False,
) -> None:
    """List contradictions, defaulting to open (unresolved) ones."""
    kb = _kb()
    try:
        results = kb.contradictions(state=None if all_states else state)
        if not results:
            typer.echo("No contradictions found.")
            return
        for c in results:
            typer.echo(
                f"{c.id}  state={c.state}  subject={c.subject}  "
                f"predicate={c.predicate}  members={len(c.member_ids)}"
            )
    except Exception as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from None
    finally:
        kb.close()


@contradiction_app.command("flag")
def flag_contradiction(
    assertion_id_a: Annotated[str, typer.Argument(help="First assertion ID.")],
    assertion_id_b: Annotated[str, typer.Argument(help="Second, conflicting assertion ID.")],
    author: Annotated[
        str,
        typer.Option("--author", help="Principal ID flagging the contradiction."),
    ],
    rationale: Annotated[
        str | None,
        typer.Option("--rationale", help="Optional explanation for the flag."),
    ] = None,
) -> None:
    """Flag two assertions as contradictory, opening or extending a
    contradiction (SPEC §10.3). Requires propose capability or higher
    (ADR-0008) — the same tier as `ontolith retract`/`ontolith.propose`,
    not a reviewer-only action.
    """
    kb = _kb()
    try:
        contradiction, action = kb.flag_contradiction(
            assertion_id_a, assertion_id_b, author, rationale=rationale
        )
        typer.echo(
            f"{action.capitalize()}: {contradiction.id}  state={contradiction.state}  "
            f"members={len(contradiction.member_ids)}"
        )
    except Exception as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from None
    finally:
        kb.close()


@contradiction_app.command("resolve")
def resolve_contradiction(
    contradiction_id: Annotated[str, typer.Argument(help="Contradiction ID to resolve.")],
    winner: Annotated[
        str,
        typer.Option("--winner", help="Assertion ID of the winning member."),
    ],
    reviewer: Annotated[
        str,
        typer.Option("--reviewer", "--author", help="Reviewer principal resolving this."),
    ],
) -> None:
    """Resolve an open contradiction by selecting a winning assertion
    (SPEC §10.3). Every other member is retracted; the winner is
    reactivated. Requires `review` (or `admin`) capability, non-AI, and
    not a party to the contradiction (KI-026).
    """
    kb = _kb()
    try:
        contradiction = kb.resolve_contradiction(contradiction_id, winner, reviewer)
        typer.echo(f"Resolved: {contradiction.id}  state={contradiction.state}  winner={winner}")
    except Exception as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from None
    finally:
        kb.close()


# ─── namespace ────────────────────────────────────────────────────────────────


@namespace_app.command("list")
def list_namespaces() -> None:
    """List all registered namespaces (SPEC §12.2, KI-022).

    This project is still single-namespace throughout (ADR-0015) — today
    this always prints exactly one entry, the seeded default namespace.
    """
    kb = _kb()
    try:
        for n in kb.list_namespaces():
            typer.echo(f"{n.id}  created={n.created_at}")
    except Exception as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from None
    finally:
        kb.close()


# ─── schema ───────────────────────────────────────────────────────────────────


@schema_app.command("show")
def schema_show(
    namespace: Annotated[
        str,
        typer.Option("--namespace", help="Namespace to inspect."),
    ] = "default",
) -> None:
    """Print concepts, properties, and relations for the active schema (SPEC §14.2).

    Same field set as MCP's `ontolith.schema`/REST's `GET /schema` output
    (KI-029, KI-038) — the CLI had no way to inspect a registered schema at
    all before this command existed, unlike every other primary interface.
    Property/relation attribute order is normalized here (cardinality,
    temporality, required, [inverse]) rather than copied verbatim — REST's
    `PropertyOut`/`RelationOut` and MCP's dict output don't actually agree
    with each other on relation field order either, so there is no single
    "the" order to mirror.
    """
    kb = _kb()
    try:
        ir = kb.backend.get_schema(namespace)
        if ir is None:
            typer.echo(f"No schema registered for namespace {namespace!r}.")
            return
        typer.echo(f"namespace={ir.namespace}  version={ir.version}")
        for concept_name, concept_def in ir.concepts.items():
            typer.echo(f"\n{concept_name}")
            for prop_name, prop_def in concept_def.properties.items():
                typer.echo(
                    f"  {prop_name}: {prop_def.value_type}"
                    f"  cardinality={prop_def.cardinality}"
                    f"  temporality={prop_def.temporality}"
                    f"  required={prop_def.required}"
                )
            for rel_name, rel_def in concept_def.relations.items():
                typer.echo(
                    f"  {rel_name} -> {rel_def.target_concept}"
                    f"  cardinality={rel_def.cardinality}"
                    f"  temporality={rel_def.temporality}"
                    f"  required={rel_def.required}"
                    f"  inverse={rel_def.inverse}"
                )
    except Exception as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from None
    finally:
        kb.close()


@schema_app.command("migrate")
def schema_migrate(
    file: Annotated[Path, typer.Argument(help="Path to a LinkML-aligned YAML schema document.")],
    author: Annotated[
        str, typer.Option("--author", help="Admin-capability principal applying the schema.")
    ],
) -> None:
    """Apply a new schema version from a YAML file (SPEC §14.2, KI-048).

    Thin wrapper around `Ontology.apply_schema`: reads a LinkML-aligned YAML
    document (ADR-0013 dialect, `schema.linkml.from_yaml`) from `file` and
    persists it as a new schema version. The document's own `version:`/`id:`
    fields drive the applied namespace/version — `apply_schema`'s existing
    strict-monotonic check (exactly `current_latest + 1`, or `1` if no
    schema is registered yet) is unchanged by this command, so the file must
    already declare the correct next version. Requires `--author` to hold
    `admin` capability (SPEC §6).

    `file` must contain the *complete* schema for the namespace, not a
    delta — `apply_schema` replaces the namespace's active `SchemaIR`
    wholesale, it does not merge the new version with the prior one. A
    document that omits a concept the prior version declared makes that
    concept (and its predicates) unknown to schema-validated writes going
    forward, same as any other `apply_schema` caller.

    Does not migrate or backfill existing assertion data written under a
    prior schema version — applying a new version is purely additive to the
    schema's own version history (ADR-0034); property renames/type changes
    against already-stored data are explicitly out of scope (KI-048).
    """
    kb = _kb()
    try:
        text = file.read_text()
        schema = from_yaml(text)
        applied = kb.apply_schema(schema, author=author)
        typer.echo(f"Applied schema: namespace={applied.namespace}  version={applied.version}")
    except Exception as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from None
    finally:
        kb.close()


__all__ = ["app"]
