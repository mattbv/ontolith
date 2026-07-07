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
app.add_typer(principal_app, name="principal")
app.add_typer(entity_app, name="entity")

# Module-level DB path, set by the root callback before any command runs.
_db_path: Path = Path("ontolith.db")


def _kb() -> Ontology:
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


__all__ = ["app"]
