"""The tile source ladder, the cache, and the promises made to tile providers.

Nothing here touches the network: the upstream fetcher takes an injectable
opener, so every case below is a scripted server that can be made to fail, to
answer 304, or to hang, none of which is convenient to arrange with a real one.

Two groups of tests, with different reasons for existing.

The **ladder** tests pin behaviour an operator depends on: a cached tile
renders when the link is gone, a degraded profile does not spend bandwidth on
scenery, and a tile that cannot be had is reported as missing rather than
faked.

The **policy** tests pin promises made to somebody else. OpenFreeMap and
OpenSeaMap are free, keyless and run on donations, and the terms that make the
caching here legitimate also forbid things that would be easy to add later by
accident — a pre-seed loop, a library-default User-Agent, an unconditional
re-fetch. Those tests fail loudly so the promise has to be broken deliberately.
"""

from __future__ import annotations

import gzip
import io
import urllib.error

import pytest

from gui_backend.core import tiles as tiles_module
from gui_backend.core.tile_cache import (
    DEFAULT_USER_AGENT,
    TileCache,
    UpstreamTiles,
)
from gui_backend.core.tiles import BASEMAP, SEAMARK, TilePolicy, TileService

TILE = b"\x1a\x0evector tile bytes"


class FakeResponse:
    def __init__(self, data: bytes, headers: dict):
        self._data = data
        self.headers = headers

    def read(self) -> bytes:
        return self._data

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class ScriptedServer:
    """A tile server that does whatever the test needs it to."""

    def __init__(self, data: bytes = TILE, headers: dict | None = None):
        self.data = data
        self.headers = headers or {
            "Content-Type": "application/vnd.mapbox-vector-tile",
            "ETag": '"abc123"',
            "Last-Modified": "Sat, 13 Sep 2026 23:09:46 GMT",
        }
        self.requests: list[tuple[str, dict]] = []
        self.fail = False
        self.not_modified = False

    def __call__(self, request, timeout=None):
        self.requests.append((request.full_url, dict(request.headers)))
        if self.fail:
            raise urllib.error.URLError("Network is unreachable")
        if self.not_modified:
            raise urllib.error.HTTPError(
                request.full_url, 304, "Not Modified", {}, io.BytesIO(b"")
            )
        return FakeResponse(self.data, self.headers)


@pytest.fixture
def service(tmp_path):
    server = ScriptedServer()
    svc = TileService(
        cache=TileCache(tmp_path / "cache.sqlite"),
        upstream=UpstreamTiles(opener=server),
        mbtiles=None,
    )
    svc.server = server           # for the tests to drive
    return svc


# -- the ladder ------------------------------------------------------------


def test_a_cold_map_goes_upstream_and_keeps_what_it_gets(service):
    result = service.tile("basemap", 12, 2185, 1207, profile="full")
    assert result.origin == "upstream"
    assert result.data == TILE
    assert service.cache.stats()["entries"] == 1


def test_the_second_look_at_a_tile_costs_nothing(service):
    service.tile("basemap", 12, 2185, 1207, profile="full")
    before = len(service.server.requests)

    result = service.tile("basemap", 12, 2185, 1207, profile="full")
    assert result.origin == "cache"
    assert result.data == TILE
    assert len(service.server.requests) == before, "a fresh cached tile was re-fetched"


def test_a_cached_tile_still_renders_when_the_link_is_gone(service):
    """The whole reason the cache exists.

    A map that goes blank the moment the link drops is no better than the
    coordinate grid it replaced, and it drops mid-mission every time.
    """
    service.tile("basemap", 12, 2185, 1207, profile="full")
    service.server.fail = True
    service.cache.ttl_s = 0        # force a revalidation attempt

    result = service.tile("basemap", 12, 2185, 1207, profile="full")
    assert result.origin == "cache"
    assert result.data == TILE


def test_a_tile_never_seen_is_reported_missing_not_invented(service):
    service.server.fail = True
    result = service.tile("basemap", 12, 2185, 1207, profile="full")
    assert result.found is False
    assert result.origin == "miss"


def test_an_unknown_source_is_a_miss_not_a_crash(service):
    assert service.tile("nonsense", 1, 1, 1, profile="full").found is False


def test_above_a_sources_max_zoom_nothing_is_requested(service):
    """MapLibre overzooms vector tiles, so asking z18 of a z14 source is asking
    for a 404 whose answer we already know."""
    service.tile("basemap", 18, 100, 100, profile="full")
    assert service.server.requests == []


def test_the_raster_overlay_goes_deeper_than_the_vector_base(service):
    """Sea marks are raster and genuinely exist at z18."""
    service.tile("seamark", 17, 100, 100, profile="full")
    assert len(service.server.requests) == 1
    assert "openseamap" in service.server.requests[0][0]


# -- the link profile gate -------------------------------------------------


def test_tiles_are_fetched_only_on_a_full_link(service):
    for profile in ("reduced", "minimal"):
        service.server.requests.clear()
        service.tile("basemap", 12, 999, 999, profile=profile)
        assert service.server.requests == [], (
            f"{profile} spent bandwidth on scenery while telemetry wanted it"
        )


def test_but_a_cached_tile_renders_on_every_profile(service):
    """Suspending fetches must not blank the map. The boat's position relative
    to the coast matters more when the link is bad, not less."""
    service.tile("basemap", 12, 2185, 1207, profile="full")
    for profile in ("full", "reduced", "minimal"):
        result = service.tile("basemap", 12, 2185, 1207, profile=profile)
        assert result.data == TILE, f"the map went blank on {profile}"


def test_a_degraded_link_does_not_even_revalidate(service):
    """A 304 is cheap, not free. On a link we are protecting, the round trip
    is the cost, not the payload."""
    service.tile("basemap", 12, 2185, 1207, profile="full")
    service.cache.ttl_s = 0
    service.server.requests.clear()

    service.tile("basemap", 12, 2185, 1207, profile="reduced")
    assert service.server.requests == []


def test_the_suspension_is_explained_rather_than_silent(service):
    reason = TilePolicy.reason("minimal")
    assert "minimal" in reason
    assert reason, "a map that stops fetching without saying so is a map that looks broken"
    assert TilePolicy.reason("full") == ""


def test_a_client_that_states_no_profile_is_served(service):
    """curl, a test, a browser hitting the URL directly. Rare and deliberate."""
    assert service.tile("basemap", 12, 2185, 1207, profile=None).found


# -- promises to the tile providers ---------------------------------------


def test_the_user_agent_names_this_application(service):
    """Every policy worth respecting asks for one, and forbids library
    defaults or anything impersonating a browser."""
    service.tile("basemap", 12, 2185, 1207, profile="full")
    _, headers = service.server.requests[0]
    agent = headers.get("User-agent", "")
    assert "Asket" in agent
    assert "github.com" in agent, "no contact route in the User-Agent"
    assert "Mozilla" not in agent, "impersonating a browser"
    assert agent == DEFAULT_USER_AGENT


def test_a_revalidation_is_conditional(service):
    """If-None-Match is the difference between a header exchange and a whole
    tile on a link that is also carrying the boat's position."""
    service.tile("basemap", 12, 2185, 1207, profile="full")
    service.cache.ttl_s = 0
    service.server.requests.clear()

    service.tile("basemap", 12, 2185, 1207, profile="full")
    _, headers = service.server.requests[0]
    assert headers.get("If-none-match"), "re-fetched unconditionally"
    assert headers.get("If-modified-since"), "the other validator was dropped"


def test_a_server_offering_only_last_modified_is_still_revalidated(tmp_path):
    """Not every server sends an ETag. A self-hosted OpenFreeMap behind a plain
    file server may send only Last-Modified, and without it every revalidation
    would silently become a full re-download."""
    server = ScriptedServer(
        TILE,
        {
            "Content-Type": "application/vnd.mapbox-vector-tile",
            "Last-Modified": "Sat, 13 Sep 2026 23:09:46 GMT",
        },
    )
    service = TileService(
        cache=TileCache(tmp_path / "c.sqlite", ttl_s=0),
        upstream=UpstreamTiles(opener=server),
    )
    service.tile("basemap", 12, 1, 1, profile="full")
    server.requests.clear()

    service.tile("basemap", 12, 1, 1, profile="full")
    _, headers = server.requests[0]
    assert headers.get("If-modified-since")


def test_a_304_refreshes_the_copy_we_already_hold(service):
    service.tile("basemap", 12, 2185, 1207, profile="full")
    service.cache.ttl_s = 0
    service.server.not_modified = True

    result = service.tile("basemap", 12, 2185, 1207, profile="full")
    assert result.data == TILE
    assert result.origin == "cache"
    assert service.upstream.status.not_modified == 1

    # And the copy counts as fresh again, so the next look is free.
    service.cache.ttl_s = 3600
    service.server.requests.clear()
    service.tile("basemap", 12, 2185, 1207, profile="full")
    assert service.server.requests == []


def test_nothing_in_this_package_pre_fetches_tiles():
    """Bulk downloading is the one thing every tile policy forbids outright.

    There is no seed-the-survey-box function, and this test exists so that
    adding one has to be a decision rather than a convenience. A survey box is
    about twenty vector tiles; the saving is not worth the habit.
    """
    import inspect

    source = inspect.getsource(tiles_module) + inspect.getsource(
        __import__("gui_backend.core.tile_cache", fromlist=["x"])
    )
    for word in ("def seed", "def prefetch", "def pre_fetch", "def warm_cache"):
        assert word not in source, f"{word} looks like bulk downloading"


def test_every_source_carries_its_attribution():
    """ODbL is a licence condition, not a courtesy. Attribution travels with
    the source so a layer cannot be rendered without knowing what it owes."""
    for source in (BASEMAP, SEAMARK):
        assert source.attribution.strip()
    assert "OpenStreetMap" in BASEMAP.attribution
    assert "OpenSeaMap" in SEAMARK.attribution


def test_the_basemap_is_not_the_osm_standard_tile_server():
    """OSMF's policy forbids the offline caching this GUI is built around:
    "Offline use is not permitted on tile.openstreetmap.org." Pointing the
    basemap back at them would make every cached tile a breach."""
    assert "tile.openstreetmap.org" not in BASEMAP.url_template
    assert "openfreemap" in BASEMAP.url_template


# -- the cache itself ------------------------------------------------------


def test_a_compressed_tile_survives_the_round_trip(tmp_path):
    """Vector tiles arrive gzipped and are stored that way, so both hops stay
    cheap. Losing the encoding on the way back out gives a map that renders
    nothing while every request returns 200 — which reads as a styling bug and
    is not one. It did, once."""
    body = gzip.compress(TILE)
    server = ScriptedServer(
        body,
        {"Content-Type": "application/vnd.mapbox-vector-tile", "Content-Encoding": "gzip"},
    )
    service = TileService(
        cache=TileCache(tmp_path / "c.sqlite"),
        upstream=UpstreamTiles(opener=server),
    )

    fresh = service.tile("basemap", 12, 1, 1, profile="full")
    assert fresh.content_encoding == "gzip"
    assert gzip.decompress(fresh.data) == TILE

    cached = service.tile("basemap", 12, 1, 1, profile="full")
    assert cached.origin == "cache"
    assert cached.content_encoding == "gzip", "the encoding was lost in the cache"
    assert gzip.decompress(cached.data) == TILE


def test_the_cache_evicts_least_recently_used_rather_than_growing(tmp_path):
    cache = TileCache(tmp_path / "c.sqlite", max_bytes=4000)
    for n in range(20):
        cache.put("basemap", f"12/{n}/0", b"x" * 500, "application/octet-stream")
    assert cache.total_bytes() <= 4000
    assert cache.stats()["entries"] < 20


def test_a_cache_that_cannot_be_opened_degrades_rather_than_failing(tmp_path):
    """No cache is a slower map, not a broken one."""
    blocker = tmp_path / "blocked"
    blocker.write_text("not a directory")
    cache = TileCache(blocker / "sub" / "c.sqlite")
    assert cache.enabled is False
    assert cache.get("basemap", "1/1/1") is None
    cache.put("basemap", "1/1/1", b"x", "image/png")   # must not raise

    service = TileService(cache=cache, upstream=UpstreamTiles(opener=ScriptedServer()))
    assert service.tile("basemap", 12, 1, 1, profile="full").found


def test_a_schema_change_discards_the_old_cache_rather_than_migrating(tmp_path):
    """A cache is disposable. Carrying migration code for data that can be
    re-fetched in seconds is complexity with no upside."""
    import sqlite3

    path = tmp_path / "c.sqlite"
    TileCache(path).put("basemap", "1/1/1", b"x", "image/png")

    conn = sqlite3.connect(path)
    conn.execute("UPDATE schema SET version = 1")
    conn.commit()
    conn.close()

    reopened = TileCache(path)
    assert reopened.enabled
    assert reopened.get("basemap", "1/1/1") is None
    assert reopened.stats()["entries"] == 0


def test_an_unreachable_server_is_reported_with_a_reason(service):
    """"The map looks sparse" has several causes needing different actions, and
    the operator cannot tell them apart by looking."""
    service.server.fail = True
    service.tile("basemap", 12, 1, 1, profile="full")

    described = service.describe("full")
    assert described["upstream"]["reachable"] is False
    assert described["upstream"]["last_error"]
    assert described["fetching"] is True          # allowed, just not working


def test_describe_says_why_fetching_stopped(service):
    assert service.describe("full")["fetch_suspended_reason"] == ""
    assert "minimal" in service.describe("minimal")["fetch_suspended_reason"]

    service.online = False
    assert "switched off" in service.describe("full")["fetch_suspended_reason"]


def test_online_tiles_can_be_switched_off_entirely(service):
    """The configuration to use if a provider ever asks us to stop."""
    service.online = False
    service.tile("basemap", 12, 1, 1, profile="full")
    assert service.server.requests == []
