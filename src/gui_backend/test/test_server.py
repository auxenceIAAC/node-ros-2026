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
    """On a beach, 'no tile file at all', 'the link is too poor to download
    tiles' and 'the tile server is unreachable' call for three different
    actions, so the endpoint reports each separately rather than one boolean."""
    info = client.get("/api/tiles/info").json()
    assert info["mbtiles"]["available"] is False
    assert info["mbtiles"]["message"]
    assert "cache" in info and "upstream" in info
    assert isinstance(info["online"], bool)


def test_the_tile_endpoint_reports_the_link_profile_back(client):
    """The map has to be able to say *why* it stopped fetching, and the answer
    depends on the caller's profile rather than on server state."""
    full = client.get("/api/tiles/info", headers={"X-Asket-Profile": "full"}).json()
    beacon = client.get("/api/tiles/info", headers={"X-Asket-Profile": "minimal"}).json()

    assert full["fetching"] is True
    assert full["fetch_suspended_reason"] == ""
    assert beacon["fetching"] is False
    assert "minimal" in beacon["fetch_suspended_reason"]


def test_a_tile_says_where_it_came_from(client):
    """Provenance travels with the bytes. The GUI never presents a cached
    basemap as live, and this is how it knows which it has."""
    response = client.get("/tiles/10/511/511.png")
    assert response.headers["X-Asket-Tile-Origin"] == "miss"


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
        assert client.get("/api/tiles/info").json()["mbtiles"]["available"] is True
        response = client.get("/tiles/10/3/5.png")
        assert response.status_code == 200
        assert response.content == b"\x89PNG-data"
        assert response.headers["X-Asket-Tile-Origin"] == "mbtiles"
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
