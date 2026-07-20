"""Embedder port for hybrid retrieval (SPEC §11.3, §14).

The Embedder port converts text into vectors for semantic search over the
vector store (StorageBackend.vector_upsert/vector_search). It is a pure
text-to-vector transform with no knowledge of KB state — the same shape as
Clock and IdProvider, not a capability-scoped plugin actor like Importer or
Exporter (see plugins/ports.py's docstring for that distinction).

Every Embedder implementation MUST return L2-unit-normalized vectors: this
makes ranking by ascending L2 distance equivalent to ranking by descending
cosine similarity (for unit vectors u, v: ||u-v||^2 = 2 - 2*cos_sim(u, v)),
which lets StorageBackend.vector_search use one distance metric consistently
across backends without an explicit similarity-function parameter.
"""

import hashlib
import math
import re
from typing import Protocol

_TOKEN_RE = re.compile(r"\w+")


class Embedder(Protocol):
    """Converts text into L2-unit-normalized embedding vectors."""

    name: str
    dim: int

    def embed(self, texts: list[str]) -> list[list[float]]:
        """Embed a batch of texts.

        Args:
            texts: Texts to embed.

        Returns:
            One L2-unit-normalized vector of length `dim` per input text, in
            the same order. A text with no extractable signal (empty or
            whitespace-only) yields the zero vector, not an error.
        """
        ...


class HashingEmbedder:
    """Deterministic, dependency-free default Embedder.

    Implements the feature-hashing trick (Weinberger et al., 2009): each
    token hashes to a bucket index and a sign, avoiding the need for a
    pre-built vocabulary or any numeric library. This is a placeholder for
    real semantic quality, not a substitute for a model-backed Embedder —
    it guarantees deterministic, reproducible vectors for a given text, not
    meaningful semantic similarity between unrelated texts.

    Uses hashlib.sha256, not Python's built-in hash(), because str hashing
    is randomly salted per process (PYTHONHASHSEED) and would break
    determinism across runs.
    """

    name = "hashing-embedder"

    def __init__(self, dim: int = 256) -> None:
        """Initialize with a fixed output dimensionality.

        Args:
            dim: Length of each output vector.
        """
        self.dim = dim

    def embed(self, texts: list[str]) -> list[list[float]]:
        """Embed a batch of texts via feature hashing.

        Args:
            texts: Texts to embed.

        Returns:
            One L2-unit-normalized vector of length `dim` per input text.
        """
        return [self._embed_one(text) for text in texts]

    def _embed_one(self, text: str) -> list[float]:
        """Embed a single text via feature hashing."""
        vec = [0.0] * self.dim
        for token in _TOKEN_RE.findall(text.lower()):
            digest = hashlib.sha256(token.encode("utf-8")).digest()
            bucket = int.from_bytes(digest[:8], "big") % self.dim
            sign = 1.0 if digest[8] & 1 else -1.0
            vec[bucket] += sign

        norm = math.sqrt(sum(v * v for v in vec))
        if norm == 0.0:
            return vec
        return [v / norm for v in vec]


class LookupEmbedder:
    """Deterministic test double backed by an explicit text-to-vector map.

    Unlike HashingEmbedder, output is fully predictable — use this in tests
    that need to assert exact rank order from semantic search.
    """

    name = "lookup-embedder"

    def __init__(
        self, mapping: dict[str, list[float]], dim: int, default: list[float] | None = None
    ) -> None:
        """Initialize with an explicit text-to-vector mapping.

        Args:
            mapping: Exact text to vector lookup.
            dim: Length of vectors in `mapping` and `default`.
            default: Vector to return for unmapped texts, or None to raise.
        """
        self._mapping = mapping
        self.dim = dim
        self._default = default

    def embed(self, texts: list[str]) -> list[list[float]]:
        """Look up each text's vector.

        Args:
            texts: Texts to look up.

        Returns:
            The mapped (or default) vector per input text.

        Raises:
            KeyError: A text is not in `mapping` and no `default` was given.
        """
        result = []
        for text in texts:
            if text in self._mapping:
                result.append(self._mapping[text])
            elif self._default is not None:
                result.append(self._default)
            else:
                raise KeyError(f"LookupEmbedder: no vector mapped for {text!r}")
        return result


__all__ = ["Embedder", "HashingEmbedder", "LookupEmbedder"]
