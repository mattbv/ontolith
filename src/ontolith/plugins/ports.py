"""Plugin port abstractions.

Defines the Protocol interfaces that plugins must implement.
"""

from typing import Protocol


class Embedder(Protocol):
    """Abstract port for embedding providers.

    Embedders convert text to vectors for semantic search.
    Default implementation will use sentence-transformers or OpenAI.

    Attributes:
        name: Embedder identifier
        dim: Vector dimensionality

    This is a stub for M0. Full interface will be defined in M1-M2.
    """

    name: str
    dim: int

    # Full interface in M1-M2:
    # def embed(self, texts: list[str]) -> list[list[float]]: ...


class PolicyStrategy(Protocol):
    """Abstract port for policy evaluation strategies.

    Policy strategies decide whether proposals should be auto-accepted,
    require review, or be rejected.

    This is a stub for M0. Full interface will be defined in M1 based on
    SPEC §9.2 requirements.
    """

    # Full interface in M1:
    # def evaluate(
    #     self, proposal: Proposal, principal: Principal, kb: ReadOnlyView
    # ) -> Decision: ...


__all__ = ["Embedder", "PolicyStrategy"]
