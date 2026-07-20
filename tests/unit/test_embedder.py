"""Unit tests for the Embedder port (core/embedder.py, ADR-0020)."""

from __future__ import annotations

import math

import pytest

from ontolith.core import HashingEmbedder, LookupEmbedder


class TestHashingEmbedder:
    def test_deterministic_across_instances(self) -> None:
        a = HashingEmbedder(dim=64)
        b = HashingEmbedder(dim=64)
        assert a.embed(["Ada Lovelace"]) == b.embed(["Ada Lovelace"])

    def test_deterministic_across_calls(self) -> None:
        embedder = HashingEmbedder(dim=64)
        assert embedder.embed(["computing pioneer"]) == embedder.embed(["computing pioneer"])

    def test_output_is_unit_normalized(self) -> None:
        embedder = HashingEmbedder(dim=64)
        (vec,) = embedder.embed(["the quick brown fox jumps over the lazy dog"])
        norm = math.sqrt(sum(v * v for v in vec))
        assert norm == pytest.approx(1.0, abs=1e-9)

    def test_dim_matches_output_length(self) -> None:
        embedder = HashingEmbedder(dim=32)
        (vec,) = embedder.embed(["some text"])
        assert len(vec) == 32
        assert embedder.dim == 32

    def test_empty_string_yields_zero_vector(self) -> None:
        embedder = HashingEmbedder(dim=16)
        (vec,) = embedder.embed([""])
        assert vec == [0.0] * 16

    def test_whitespace_only_yields_zero_vector(self) -> None:
        embedder = HashingEmbedder(dim=16)
        (vec,) = embedder.embed(["   \t\n  "])
        assert vec == [0.0] * 16

    def test_different_texts_produce_different_vectors(self) -> None:
        embedder = HashingEmbedder(dim=64)
        a, b = embedder.embed(["Ada Lovelace", "Charles Babbage"])
        assert a != b

    def test_batch_matches_individual_calls(self) -> None:
        embedder = HashingEmbedder(dim=32)
        batch = embedder.embed(["one", "two", "three"])
        individual = [embedder.embed([t])[0] for t in ["one", "two", "three"]]
        assert batch == individual


class TestLookupEmbedder:
    def test_returns_mapped_vector(self) -> None:
        embedder = LookupEmbedder({"cat": [1.0, 0.0]}, dim=2)
        assert embedder.embed(["cat"]) == [[1.0, 0.0]]

    def test_unmapped_text_uses_default(self) -> None:
        embedder = LookupEmbedder({"cat": [1.0, 0.0]}, dim=2, default=[0.0, 0.0])
        assert embedder.embed(["dog"]) == [[0.0, 0.0]]

    def test_unmapped_text_without_default_raises(self) -> None:
        embedder = LookupEmbedder({"cat": [1.0, 0.0]}, dim=2)
        with pytest.raises(KeyError):
            embedder.embed(["dog"])

    def test_preserves_input_order(self) -> None:
        embedder = LookupEmbedder({"a": [1.0], "b": [2.0]}, dim=1)
        assert embedder.embed(["b", "a"]) == [[2.0], [1.0]]
