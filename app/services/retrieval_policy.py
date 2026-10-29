"""What a knowledge search may do beyond the always-on rules (G9, RQ-12 .. RQ-20).

Every behaviour that changes what an agent retrieves or says and needs a
calibration this repository does not have yet is a field here, with the
default the remediation locked:

| Field | Default | Why |
|---|---|---|
| `hybrid` | on | exact identifiers; needs no calibration (RQ-15) |
| `rerank` | off | a model call per search; needs a latency and quality check (RQ-16) |
| `confidence_tiers` | off | thresholds come from a real distance distribution (RQ-13) |
| `passage_margin` | none | the same (FX-3.16) |
| `knowledge_base_ids` | none | the agent's grant, when it names a scope (RQ-18) |

Resolved per search from the deployment's settings, the workspace's own
overrides (`tenants.knowledge_settings`) and the agent's grant; never from
anything a model wrote.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class RetrievalPolicy:
    """The switches and cut-offs one search runs under."""

    hybrid: bool = True
    rerank: bool = False
    confidence_tiers: bool = False
    #: Best-distance cut-offs for the tiers of the query's embedding model.
    #: Both None while uncalibrated, which keeps tiers off even when enabled.
    strong_distance: float | None = None
    partial_distance: float | None = None
    #: How much worse than the best passage a passage may be and still be sent.
    passage_margin: float | None = None
    #: The knowledge bases the agent may read; None for the whole workspace.
    knowledge_base_ids: tuple[uuid.UUID, ...] | None = None

    @property
    def tiers_active(self) -> bool:
        """Tiers only count when they are on *and* calibrated."""
        return (
            self.confidence_tiers
            and self.strong_distance is not None
            and self.partial_distance is not None
        )


__all__ = ["RetrievalPolicy"]
