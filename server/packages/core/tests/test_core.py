import callscore_core


def test_version_is_exposed() -> None:
    assert isinstance(callscore_core.__version__, str)
    assert callscore_core.__version__
