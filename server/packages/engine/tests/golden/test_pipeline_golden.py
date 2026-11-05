"""Golden-fixture comparison: ``run_pipeline`` vs the independent reference.

For every methodology variant in ``methodologies.json`` the runner's
``intervals``/``returns``/``contributions`` outputs must reproduce
``data/fixtures/golden/expected`` exactly for ids, timestamps and reason
strings, and within 1e-9 relative for floats (manifest tolerances).
"""

import numpy as np
import pytest
from load import (
    check_contract_frame,
    expected_frame,
    load_context,
    load_inputs,
    load_methodologies,
)

from callscore_core.frames import FrameContract
from callscore_engine.runner import SnapshotFrames, run_pipeline

VARIANTS = ("default", "decay", "fixed")
KINDS = ("intervals", "returns", "contributions")


def _assert_frame_matches(result, expected, contract: FrameContract, label: str) -> None:
    check_contract_frame(result, contract)
    assert result.height == expected.height, (
        f"{label}: {result.height} rows != expected {expected.height}"
    )
    for col in contract.columns:
        got = result[col.name]
        want = expected[col.name]
        assert got.dtype == want.dtype, f"{label}.{col.name}: dtype {got.dtype} != {want.dtype}"
        if col.dtype == "Float64":
            g = got.to_numpy().astype(float)
            w = want.to_numpy().astype(float)
            assert np.allclose(g, w, rtol=1e-9, atol=0.0, equal_nan=True), (
                f"{label}.{col.name}: float mismatch"
            )
        else:
            assert got.equals(want), f"{label}.{col.name}: value mismatch"


@pytest.mark.parametrize("variant", VARIANTS)
@pytest.mark.parametrize("kind", KINDS)
def test_golden_outputs_match_reference(variant: str, kind: str) -> None:
    frames = SnapshotFrames.from_mapping(load_inputs())
    result = run_pipeline(frames, load_methodologies()[variant], load_context())
    expected, contract = expected_frame(kind, variant)
    _assert_frame_matches(getattr(result, kind), expected, contract, f"{kind}/{variant}")
