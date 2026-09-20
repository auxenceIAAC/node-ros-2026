"""The HTTP and WebSocket surface, exercised through the real app."""

import sqlite3

import pytest
from asket_sim.core.world import SimWorld, WorldConfig
from fastapi.testclient import TestClient
from gui_backend.core.hub import Hub
from gui_backend.core.server import create_app
from gui_backend.core.sim_source import SimSource


@pytest.fixture
def app(tmp_path):
    hub = Hub(SimSource(SimWorld(WorldConfig()), time_scale=20.0), tick_hz=50.0)
    return create_app(hub, static_dir=None, tiles_path=None, ping_interval_s=0.05)


@pytest.fixture
def client(app):
    with TestClient(app) as c:
        yield c


def test_health_reports_the_source_and_profile(client):
    body = client.get("/api/health").json()
    assert body["ok"] is True
    assert body["source"]["mode"] == "sim"
    assert body["profile"]["profile"] in ("full", "reduced", "minimal")


def test_missing_tiles_are_explained_rather_than_merely_absent(client):
    """On a beach, 'no tile file at all' and 'no tiles for this area' call for
    different actions."""
    info = client.get("/api/tiles/info").json()
    assert info["available"] is False
    assert "coordinate grid" in info["message"]


def test_a_missing_tile_is_no_content_not_an_error(client):
    """A wall of 404s in the console hides real problems."""
    assert client.get("/tiles/10/511/511.png").status_code == 204


def test_tiles_are_served_from_an_mbtiles_file(tmp_path):
    path = tmp_path / "survey.mbtiles"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE metadata (name text, value text)")
    conn.execute(
        "CREATE TABLE tiles (zoom_level int, tile_column int, tile_row int, tile_data blob)"
    )
    conn.executemany(
        "INSERT INTO metadata VALUES (?,?)",
        [("name", "walvis"), ("format", "png"), ("minzoom", "8"), ("maxzoom", "14")],
    )
    # XYZ (z=10, x=3, y=5) is TMS row (2^10 - 1) - 5 = 1018.
    conn.execute("INSERT INTO tiles VALUES (10, 3, 1018, ?)", (b"\x89PNG-data",))
    conn.commit()
    conn.close()

    hub = Hub(SimSource(SimWorld(WorldConfig())))
    with TestClient(create_app(hub, tiles_path=path, ping_interval_s=0.05)) as client:
        assert client.get("/api/tiles/info").json()["available"] is True
        response = client.get("/tiles/10/3/5.png")
        assert response.status_code == 200
        assert response.content == b"\x89PNG-data"
        # And the flip is not accidentally symmetric.
        assert client.get("/tiles/10/3/1018.png").status_code == 204


def test_without_a_built_frontend_the_root_says_how_to_build_it(client):
    body = client.get("/").json()
    assert "npm run build" in body["fix"]


# -- WebSocket -----------------------------------------------------------


def receive_until(ws, predicate, limit=400):
    for _ in range(limit):
        message = ws.receive_json()
        if predicate(message):
            return message
    raise AssertionError("expected message never arrived")


def test_a_client_is_greeted_with_everything_it_needs(client):
    with client.websocket_connect("/ws") as ws:
        hello = ws.receive_json()
        assert hello["type"] == "hello"
        assert hello["protocol_version"] == 1
        assert "vessel" in hello["streams"]
        assert hello["source"]["mode"] == "sim"


def test_nothing_is_pushed_before_a_subscription(client):
    """The single most important property of the whole backend."""
    with client.websocket_connect("/ws") as ws:
        ws.receive_json()  # hello
        for _ in range(30):
            message = ws.receive_json()
            assert message["type"] != "data", "the backend pushed unsolicited data"


def test_subscribing_yields_data_with_both_timestamps(client):
    with client.websocket_connect("/ws") as ws:
        ws.receive_json()
        ws.send_json({"type": "subscribe",
                      "streams": [{"name": "vessel", "rate_hz": 10.0}]})
        confirmation = receive_until(ws, lambda m: m["type"] == "subscribed")
        assert confirmation["streams"][0]["granted"]

        frame = receive_until(ws, lambda m: m.get("stream") == "vessel")
        assert frame["payload"]["lat"] is not None
        assert frame["source_utc_ms"] <= frame["server_utc_ms"]


def test_a_client_is_told_when_it_is_being_given_less_than_it_asked_for(client):
    with client.websocket_connect("/ws") as ws:
        ws.receive_json()
        ws.send_json({"type": "set_profile", "profile": "minimal"})
        receive_until(ws, lambda m: m["type"] == "profile")

        ws.send_json({"type": "subscribe",
                      "streams": [{"name": "vessel", "rate_hz": 10.0},
                                  {"name": "lidar", "rate_hz": 10.0}]})
        confirmation = receive_until(ws, lambda m: m["type"] == "subscribed")
        by_name = {s["name"]: s for s in confirmation["streams"]}
        assert by_name["vessel"]["rate_hz"] == 0.2
        assert by_name["vessel"]["reason"]
        assert not by_name["lidar"]["granted"]
        assert "not carried" in by_name["lidar"]["reason"]


def test_a_mode_command_reports_pending_then_confirmed(client):
    with client.websocket_connect("/ws") as ws:
        ws.receive_json()
        ws.send_json({"type": "command", "id": "c1", "name": "set_mode",
                      "args": {"mode": "AUTONOMOUS"}})

        first = receive_until(ws, lambda m: m["type"] == "command_result")
        assert first["status"] == "pending"

        final = receive_until(
            ws, lambda m: m["type"] == "command_result" and m["status"] != "pending"
        )
        assert final["status"] == "confirmed"


def test_an_unknown_message_type_is_reported_not_ignored(client):
    with client.websocket_connect("/ws") as ws:
        ws.receive_json()
        ws.send_json({"type": "teleport"})
        error = receive_until(ws, lambda m: m["type"] == "error")
        assert "teleport" in error["message"]


def test_the_server_measures_round_trip_time(client):
    with client.websocket_connect("/ws") as ws:
        ws.receive_json()
        ping = receive_until(ws, lambda m: m["type"] == "ping")
        ws.send_json({"type": "pong", "id": ping["id"]})


# -- serving the frontend out of a --symlink-install workspace -------------
#
# On the first Jetson deployment the page loaded with the right title and an
# empty <div id="root">. `colcon build --symlink-install` installs the built
# frontend as symlinks into build/, and StaticFiles' traversal guard refuses
# any path that resolves outside the directory it was given, so every asset was
# a 404 while `/` kept serving index.html. A blank page with a 200 on it.


def _built_frontend(root, *, symlinked: bool):
    """A static dir shaped like colcon's, either copied or symlinked."""
    real = root / "real"
    (real / "assets").mkdir(parents=True)
    (real / "index.html").write_text(
        '<html><head><title>Asket</title></head>'
        '<body><div id="root"></div><script src="/assets/index.js"></script></body></html>'
    )
    (real / "assets" / "index.js").write_text("console.log('asket');")
    (real / "assets" / "index.css").write_text("body{}")

    if not symlinked:
        return real

    installed = root / "installed"
    (installed / "assets").mkdir(parents=True)
    (installed / "index.html").symlink_to(real / "index.html")
    for name in ("index.js", "index.css"):
        (installed / "assets" / name).symlink_to(real / "assets" / name)
    return installed


@pytest.mark.parametrize("symlinked", [False, True], ids=["copied", "symlink-install"])
def test_the_assets_are_served_however_colcon_installed_them(tmp_path, symlinked):
    static = _built_frontend(tmp_path, symlinked=symlinked)
    hub = Hub(SimSource(SimWorld(WorldConfig()), time_scale=20.0), tick_hz=50.0)
    app = create_app(hub, static_dir=str(static), tiles_path=None)

    with TestClient(app) as c:
        assert c.get("/").status_code == 200
        for asset in ("/assets/index.js", "/assets/index.css"):
            response = c.get(asset)
            assert response.status_code == 200, (
                f"{asset} is a 404 — index.html still serves, so the page loads "
                "blank with a 200 on it, which reads like a broken app rather "
                "than a broken install"
            )


def test_a_readable_frontend_reports_no_problems(tmp_path):
    from gui_backend.core.server import check_static_dir

    assert check_static_dir(_built_frontend(tmp_path, symlinked=True)) == []


def test_a_dangling_asset_is_reported_rather_than_served_blank(tmp_path):
    """The case that is worse than a missing frontend, because it looks like
    the application started."""
    from gui_backend.core.server import check_static_dir

    static = _built_frontend(tmp_path, symlinked=True)
    (tmp_path / "real" / "assets" / "index.js").unlink()   # build/ cleaned out

    problems = check_static_dir(static)
    assert problems, "a dangling asset symlink was not reported"
    assert "index.js" in problems[0]


def test_a_frontend_that_was_never_built_says_which_command_to_run(tmp_path):
    from gui_backend.core.server import check_static_dir

    (tmp_path / "empty").mkdir()
    problems = check_static_dir(tmp_path / "empty")
    assert problems and "npm run build" in problems[0]
