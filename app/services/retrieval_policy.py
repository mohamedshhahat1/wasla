"""What a knowledge search, and the turn around it, may do beyond the always-on rules (G9).

Every behaviour that changes what an agent retrieves or says and needs a
calibration this repository does not have yet is a switch here, with the
default the remediation locked (RQ-12 .. RQ-20):

| Switch | Default | Why |
|---|---|---|
| `hybrid` | on | exact identifiers; needs no calibration (RQ-15) |
| `rerank` | off | a model call per search; unmeasured on real traffic (RQ-16) |
| `confidence_tiers` | off | cut-offs come from a real distance distribution (RQ-13) |
| `number_grounding` | observe | counts, changes nothing (RQ-19) |
| `pre_retrieval` | off | and inert until tiers are calibrated (RQ-20) |
| `parallel_sentiment` | off | changes AI-04's order; sending stays gated (RQ-20) |

Resolved per turn from the deployment's settings and the workspace's own
overrides (`tenants.knowledge_settings`), and the scope from the agent's grant;
never from anything a model wrote.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Final, Literal

from app.core.config import Settings

GroundingMode = Literal["off", "observe", "correct", "strict"]
GROUNDING_MODES: Final = ("off", "observe", "correct", "strict")
#: The switches a workspace may override, and nothing else.
WORKSPACE_SWITCHES: Final = (
    "hybrid",
    "rerank",
    "confidence_tiers",
    "number_grounding",
    "pre_retrieval",
    "parallel_sentiment",
)


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


@dataclass(frozen=True, slots=True)
class TurnFeatures:
    """The turn-level switches (RQ-19, RQ-20)."""

    number_grounding: GroundingMode = "observe"
    pre_retrieval: bool = False
    parallel_sentiment: bool = False


@dataclass(frozen=True, slots=True)
class KnowledgeSwitches:
    """Everything resolved for one workspace: the search's policy and the turn's features."""

    policy: RetrievalPolicy = field(default_factory=RetrievalPolicy)
    features: TurnFeatures = field(default_factory=TurnFeatures)
    rerank_model: str | None = None
    rerank_timeout_seconds: float = 1.5


def resolve(
    settings: Settings,
    overrides: Mapping[str, Any] | None,
    *,
    embedding_model: str,
) -> KnowledgeSwitches:
    """The deployment's defaults with the workspace's overrides applied.

    An override of an unknown switch, or of the wrong type, is ignored rather
    than trusted: the column is written only through a validated schema, and
    this is the backstop for one written any other way.
    """
    chosen = dict(overrides or {})

    def flag(name: str, default: bool) -> bool:
        value = chosen.get(name)
        return value if isinstance(value, bool) else default

    grounding = chosen.get("number_grounding")
    mode: GroundingMode = (
        grounding if grounding in GROUNDING_MODES else settings.rag_number_grounding
    )
    cut_offs = settings.rag_confidence_tiers.get(embedding_model)
    rerank_model = settings.rag_rerank_model or None
    rerank = flag("rerank", settings.rag_rerank_enabled) and rerank_model is not None
    return KnowledgeSwitches(
        policy=RetrievalPolicy(
            hybrid=flag("hybrid", settings.rag_hybrid_enabled),
            rerank=rerank,
            confidence_tiers=flag("confidence_tiers", settings.rag_confidence_tiers_enabled),
            strong_distance=cut_offs[0] if cut_offs else None,
            partial_distance=cut_offs[1] if cut_offs else None,
            passage_margin=settings.rag_passage_margin,
        ),
        features=TurnFeatures(
            number_grounding=mode,
            pre_retrieval=flag("pre_retrieval", settings.rag_preretrieval_enabled),
            parallel_sentiment=flag("parallel_sentiment", settings.rag_parallel_sentiment_enabled),
        ),
        rerank_model=rerank_model,
        rerank_timeout_seconds=settings.rag_rerank_timeout_seconds,
    )


#: The one per-grant setting this build acts on (RQ-18): which knowledge bases
#: an agent's `search_knowledge` reads.
KNOWLEDGE_SCOPE_KEY: Final = "knowledge_base_ids"


def knowledge_scope(config: dict[str, Any] | None) -> tuple[uuid.UUID, ...] | None:
    """The knowledge bases a `search_knowledge` grant is scoped to; None for all of them.

    Read from a grant that `grant_tool` validated. A value that is no longer a
    list of ids - written by hand, or by an older build - scopes to nothing
    rather than to everything: the safe direction for a setting that exists to
    narrow what an agent reads.
    """
    if not config or KNOWLEDGE_SCOPE_KEY not in config:
        return None
    raw = config[KNOWLEDGE_SCOPE_KEY]
    if not isinstance(raw, list):
        return ()
    try:
        return tuple(uuid.UUID(str(value)) for value in raw)
    except ValueError:
        return ()


__all__ = [
    "GROUNDING_MODES",
    "KNOWLEDGE_SCOPE_KEY",
    "WORKSPACE_SWITCHES",
    "GroundingMode",
    "KnowledgeSwitches",
    "RetrievalPolicy",
    "TurnFeatures",
    "knowledge_scope",
    "resolve",
]
