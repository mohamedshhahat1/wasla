"""The structural retrieval evaluation as a gate (FX-3.1, IMP-01).

Fake embeddings: these numbers measure structure, not semantic quality (see
`rag_eval`). The gates hold what the code has achieved so far - each phase of
the RAG remediation tightened them - so a regression in what is *in* the index
fails here before it reaches a real model.

`WASLA_RAG_EVAL_OUT=<dir>` writes the run's table there, which is how the
remediation reports recorded the baseline and the after columns.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from tests.integration.rag_eval import evaluate, measure
from tests.integration.rag_eval_part2 import measure_part2
from tests.integration.rag_harness import Indexing

pytestmark = pytest.mark.integration

# Ceilings (lower is better) and floors (higher is better) the code has reached.
# Baseline at 7cdf863: intact 250/261, top-5 225, rank-1 152, presentation 2,
# stale 1, duplicates 25 slots, empty 6, numbers cut 9, words cut 68,
# headings 9. Each remediation phase that improves a number moves its gate.
# Phase 3 (normalisation, page structure): presentation 0, intact 252, top-5
# 241, rank-1 171; words cut 70 - the old fixed-width cuts landing two words
# differently in the normalised text, which Phase 4's chunker removes.
# Phase 4 (structure-aware chunker): numbers, words and headings 0, duplicate
# slots 2 (the cross-KB copy, RV-07), intact 255, rank-1 193. The six spans
# still not intact are Arabic-Indic numbers pypdf reversed or dropped in two
# producers' PDFs - flagged as extraction warnings, not repairable from text.
# Phase 6 (document lifecycle): the second price list replaces the first and
# the cross-KB copy is refused - stale versions 0, duplicate slots 0.
CEILINGS = {
    "presentation": 0,
    "stale": 0,
    "duplicates": 0,
    "empty": 6,
    "numbers_cut": 0,
    "words_cut": 0,
    "headed_without": 0,
}
FLOORS = {"intact": 255, "hit": 241, "rank1": 193}


async def test_the_structural_evaluation(indexing: Indexing) -> None:
    tenant_id = await indexing.workspace()
    run = await evaluate(indexing, tenant_id)
    out = os.environ.get("WASLA_RAG_EVAL_OUT")
    if out:
        target = Path(out)
        target.mkdir(parents=True, exist_ok=True)
        label = os.environ.get("WASLA_RAG_EVAL_LABEL", "run")
        (target / f"{label}.md").write_text(run.table(label), encoding="utf-8")
    raw = run.metrics["_raw"]
    part2 = await measure_part2(indexing, tenant_id, run)
    if out:
        (Path(out) / f"{label}-part2.md").write_text(part2.table(label), encoding="utf-8")

    # Deterministic: the same corpus asked again is measured the same way.
    again = await measure(indexing, tenant_id, run)
    assert again.metrics == run.metrics

    assert raw["answerable"] >= 200
    for name, ceiling in CEILINGS.items():
        assert raw[name] <= ceiling, (name, raw[name])
    for name, floor in FLOORS.items():
        assert raw[name] >= floor, (name, raw[name])
