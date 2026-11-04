from pathlib import Path

from fastapi.testclient import TestClient

from callscore_api import create_app


def test_health() -> None:
    client = TestClient(create_app())
    resp = client.get("/api/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert "version" in body


def test_spa_fallback(tmp_path: Path) -> None:
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<html>spa</html>")
    (dist / "favicon.svg").write_text("<svg/>")
    (tmp_path / "secret.txt").write_text("secret")
    client = TestClient(create_app(web_dist=dist))

    assert client.get("/leaderboard").text == "<html>spa</html>"
    assert client.get("/favicon.svg").text == "<svg/>"
    assert client.get("/api/nope").status_code == 404
    assert "secret" not in client.get("/..%2Fsecret.txt").text
