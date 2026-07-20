"""Conformance kit: StorageBackend.vector_upsert/vector_search (SPEC §12.3, ADR-0020).

Parametrized over every registered backend via `make_kb` (conformance/conftest.py).
Vectors here test the raw port directly — no Embedder involved — one invariant
per test, mirroring how the plugin contract kit is structured.
"""

from __future__ import annotations

import pytest

from conformance.conftest import KbFactory
from ontolith.core import ValidationError


def test_upsert_then_search_returns_nearest_first(make_kb: KbFactory) -> None:
    kb = make_kb()
    kb.backend.vector_upsert("entity", "near", [1.0, 0.0, 0.0])
    kb.backend.vector_upsert("entity", "mid", [0.0, 1.0, 0.0])
    kb.backend.vector_upsert("entity", "far", [0.0, 0.0, 1.0])

    results = kb.backend.vector_search("entity", [0.9, 0.1, 0.0], k=3)

    assert [id_ for id_, _ in results] == ["near", "mid", "far"]
    distances = [d for _, d in results]
    assert distances == sorted(distances)


def test_upsert_is_actually_upsert(make_kb: KbFactory) -> None:
    kb = make_kb()
    kb.backend.vector_upsert("entity", "a", [1.0, 0.0, 0.0])
    kb.backend.vector_upsert("entity", "a", [0.0, 1.0, 0.0])

    results = kb.backend.vector_search("entity", [0.0, 1.0, 0.0], k=10)

    assert len(results) == 1
    assert results[0][0] == "a"
    assert results[0][1] == pytest.approx(0.0, abs=1e-6)


def test_search_respects_k(make_kb: KbFactory) -> None:
    kb = make_kb()
    for i in range(5):
        kb.backend.vector_upsert("entity", f"e{i}", [float(i), 0.0])

    results = kb.backend.vector_search("entity", [0.0, 0.0], k=2)

    assert len(results) == 2


def test_search_on_empty_scope_returns_empty_list(make_kb: KbFactory) -> None:
    kb = make_kb()
    assert kb.backend.vector_search("entity", [1.0, 0.0], k=5) == []


def test_scopes_are_isolated(make_kb: KbFactory) -> None:
    kb = make_kb()
    kb.backend.vector_upsert("entity", "shared-id", [1.0, 0.0])
    kb.backend.vector_upsert("assertion", "shared-id", [1.0, 0.0])

    assert len(kb.backend.vector_search("entity", [1.0, 0.0], k=10)) == 1
    assert len(kb.backend.vector_search("assertion", [1.0, 0.0], k=10)) == 1


def test_unknown_scope_rejected(make_kb: KbFactory) -> None:
    kb = make_kb()
    with pytest.raises(ValidationError):
        kb.backend.vector_upsert("bogus", "a", [1.0, 0.0])
    with pytest.raises(ValidationError):
        kb.backend.vector_search("bogus", [1.0, 0.0], k=5)


def test_dimension_mismatch_rejected(make_kb: KbFactory) -> None:
    kb = make_kb()
    kb.backend.vector_upsert("entity", "a", [1.0, 0.0, 0.0])

    with pytest.raises(ValidationError):
        kb.backend.vector_upsert("entity", "b", [1.0, 0.0])
    with pytest.raises(ValidationError):
        kb.backend.vector_search("entity", [1.0, 0.0], k=5)


def test_vectors_survive_reconnect(make_kb: KbFactory) -> None:
    kb = make_kb()
    kb.backend.vector_upsert("entity", "a", [1.0, 0.0, 0.0])
    path = make_kb.last_path
    kb.backend.close()

    kb2 = make_kb(reuse_path=path)
    results = kb2.backend.vector_search("entity", [1.0, 0.0, 0.0], k=1)

    assert len(results) == 1
    assert results[0][0] == "a"
