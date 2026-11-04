from pathlib import Path

from callscore_synth.cli import main


def test_cli_creates_db_file(tmp_path: Path, capsys) -> None:
    out = tmp_path / "callscore.db"
    assert main(["--scale", "small", "--out", str(out)]) == 0
    assert out.exists()
    assert "small" in capsys.readouterr().out


def test_cli_rejects_bad_scale(tmp_path: Path) -> None:
    import pytest

    with pytest.raises(SystemExit):
        main(["--scale", "huge", "--out", str(tmp_path / "x.db")])
