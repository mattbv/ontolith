"""Ontolith CLI — command-line interface for knowledge base management."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from ontolith import Ontology

app = typer.Typer(
    name="ontolith",
    help="Ontolith knowledge base CLI.",
    no_args_is_help=True,
)
principal_app = typer.Typer(help="Manage principals.", no_args_is_help=True)
entity_app = typer.Typer(help="Manage entities.", no_args_is_help=True)
proposal_app = typer.Typer(help="Inspect proposals.", no_args_is_help=True)
contradiction_app = typer.Typer(help="Inspect contradictions.", no_args_is_help=True)
app.add_typer(principal_app, name="principal")
app.add_typer(entity_app, name="entity")
app.add_typer(proposal_app, name="proposal")
app.add_typer(contradiction_app, name="contradiction")

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
    """Create a new principal."""
    kb = _kb()
    try:
        p = kb.create_principal(
            principal_id,
            kind=kind,
            owner=owner,
            default_capability=capability,
            trust_level=trust_level,
        )
        typer.echo(f"Created principal: {p.id}  kind={p.kind}  capability={p.default_capability}")
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


# ─── query ────────────────────────────────────────────────────────────────────


@app.command("query")
def query_entities(
    concept: Annotated[str, typer.Argument(help="Concept to query (e.g. Person).")],
    where: Annotated[
        list[str] | None,
        typer.Option("--where", help="Equality filter as KEY=VALUE (repeatable)."),
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


__all__ = ["app"]
