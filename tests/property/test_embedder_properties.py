"""Property-based tests for HashingEmbedder (core/embedder.py, ADR-0020).

HashingEmbedder is a correctness-critical building block for hybrid
retrieval: StorageBackend.vector_search's distance-metric equivalence
(ascending L2 distance == descending cosine similarity) depends on every
Embedder always returning L2-unit-normalized vectors. These properties
exercise that contract, plus determinism, across arbitrary text input.
"""

from __future__ import annotations

import math

from hypothesis import given
from hypothesis import strategies as st

from ontolith.core import HashingEmbedder

_embedder = HashingEmbedder(dim=64)

_texts = st.text(max_size=200)


@given(text=_texts)
def test_output_is_zero_or_unit_norm(text: str) -> None:
    (vec,) = _embedder.embed([text])
    norm = math.sqrt(sum(v * v for v in vec))
    assert norm == 0.0 or abs(norm - 1.0) < 1e-9


@given(text=_texts)
def test_embed_is_deterministic(text: str) -> None:
    assert _embedder.embed([text]) == _embedder.embed([text])


@given(text=_texts)
def test_output_length_matches_dim(text: str) -> None:
    (vec,) = _embedder.embed([text])
    assert len(vec) == _embedder.dim


@given(texts=st.lists(_texts, max_size=10))
def test_batch_embed_matches_per_item_embed(texts: list[str]) -> None:
    assert _embedder.embed(texts) == [_embedder.embed([t])[0] for t in texts]
