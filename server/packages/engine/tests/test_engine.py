import polars as pl

from callscore_engine import score_frame


def test_score_frame_is_identity_for_now() -> None:
    frame = pl.DataFrame({"a": [1, 2, 3]})
    assert score_frame(frame).equals(frame)
