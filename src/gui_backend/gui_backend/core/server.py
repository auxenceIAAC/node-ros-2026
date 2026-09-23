"""FastAPI application: WebSocket API, offline tiles, and the frontend.

One process serves everything the laptop needs. There is no CDN in Namibia.

The WebSocket protocol is documented in ``docs/ws_protocol.md`` and summarised
here:

Client to server::

    {"type": "subscribe",   "streams": [{"name": "vessel", "rate_hz": 5, "detail": "full"}]}
    {"type": "unsubscribe", "streams": ["lidar"]}
    {"type": "set_profile", "profile": "reduced" | "auto"}
    {"type": "command",     "id": "...", "name": "set_mode", "args": {"mode": "MANUAL"}}
    {"type": "pong",        "id": 7}

Server to client::

    {"type": "hello",          ...}    once, on connect
    {"type": "subscribed",     ...}    what you are actually getting, and why
    {"type": "data",           "stream": ..., "source_utc_ms": ..., "server_utc_ms": ...}
    {"type": "command_result", "status": "pending" | "confirmed" | "failed"}
    {"type": "alarms",         "raised": [...], "cleared": [...]}
    {"type": "profile",        ...}    the link profile changed
    {"type": "ping",           "id": 7, "server_utc_ms": ...}
"""

from __future__ import annotations

import asyncio
import gzip
import logging
import time
import uuid
import zlib
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .hub import ClientSession, Hub
from .shaper import LinkShaper
from .tile_cache import TileCache, UpstreamTiles
from .tiles import MBTiles, TileService

#: How often the server measures each client's round trip. This is the number
#: automatic profile selection runs on, so it must not be so rare that a link
#: collapses between measurements.
PING_INTERVAL_S = 2.0

_log = logging.getLogger(__name__)


def check_static_dir(static: Path) -> list[str]:
    """Problems that would make the page load blank, stated before it does.

    A missing frontend is already reported clearly. This covers the case that
    is not: the directory is there, `index.html` serves, and the assets do not
    — so the browser gets a 200, the right title, and an empty page. That is a
    much worse failure than a 503, because it looks like the application has
    started.
    """
    problems: list[str] = []

    index = static / "index.html"
    if not index.is_file():
        problems.append(f"{index} is missing — run `npm run build` in src/asket_gui")
        return problems

    assets = static / "assets"
    if not assets.is_dir():
        problems.append(f"{assets} is missing; index.html has nothing to load")
        return problems

    files = sorted(assets.iterdir())
    if not files:
        problems.append(f"{assets} is empty; index.html has nothing to load")

    for entry in files:
        try:
            with entry.open("rb") as handle:
                handle.read(1)
        except OSError as exc:
            problems.append(f"{entry} cannot be read ({exc}) — a dangling symlink?")
    return problems


#: The GUI tells the server which link profile it is on, so the server can
#: decide whether a tile is worth the bandwidth. A header rather than a query
#: parameter on purpose: putting it in the URL would change every tile URL when
#: the profile changes, and MapLibre would re-request the whole viewport at the
#: exact moment the link is worst.
PROFILE_HEADER = "X-Asket-Profile"


def _decode(data: bytes, encoding: str) -> bytes:
    """Undo a content encoding for a client that cannot take it."""
    try:
        if encoding.lower() == "gzip":
            return gzip.decompress(data)
        if encoding.lower() == "deflate":
            return zlib.decompress(data)
    except (OSError, zlib.error):
        # Undecodable is not fatal: hand the bytes over labelled as they are
        # and let the client decide. Failing the request would take the map
        # down over one tile.
        return data
    return data


def create_app(
    hub: Hub,
    static_dir: str | Path | None = None,
    tiles_path: str | Path | None = None,
    ping_interval_s: float = PING_INTERVAL_S,
    shape_link: bool = False,
    tile_cache_path: str | Path | None = None,
    online_tiles: bool = True,
    tile_cache_max_bytes: int | None = None,
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        hub.start()
        yield
        await hub.stop()
        # Leave one file behind rather than a database and a write-ahead log,
        # so the tile cache can be copied to another machine or kept for the
        # next deployment — which is the cheapest answer to "what if there is
        # no signal at the launch point".
        app.state.tile_service.cache.checkpoint()

    app = FastAPI(title="Asket Mission GUI", version="0.1.0", lifespan=lifespan)
    tiles = MBTiles(tiles_path)
    cache_kwargs = {} if tile_cache_max_bytes is None else {"max_bytes": tile_cache_max_bytes}
    tile_service = TileService(
        cache=TileCache(tile_cache_path, **cache_kwargs),
        upstream=UpstreamTiles(),
        mbtiles=tiles,
        online=online_tiles,
    )
    app.state.hub = hub
    app.state.tiles = tiles
    app.state.tile_service = tile_service

    # -- plain HTTP -------------------------------------------------------

    @app.get("/api/health")
    async def health() -> JSONResponse:
        return JSONResponse(
            {
                "ok": True,
                "server_utc_ms": hub.source.now_utc_ms(),
                "clients": len(hub.clients),
                "profile": hub.selector.to_dict(),
                "source": hub.source.describe(),
            }
        )

    @app.get("/api/tiles/info")
    async def tiles_info(request: Request) -> JSONResponse:
        """Where the map is getting its tiles, and what it is not getting.

        The frontend needs the reasons, not just booleans. "No tile file at
        all", "the link is too poor to download tiles" and "the tile server is
        unreachable" call for three different actions on a beach, and the map
        has to be able to say which one it is rather than going quietly blank.
        """
        profile = request.headers.get(PROFILE_HEADER, "")
        return JSONResponse(tile_service.describe(profile or None))

    def _serve(result, cache_seconds: int, accept_encoding: str = "") -> Response:
        """One tile, with its encoding and its provenance intact.

        The provenance headers are not debug output. The GUI's standing rule is
        that it never presents stale data as current, and these are how the map
        knows whether what it just drew came off the network or out of a cache
        that may be weeks old.

        The encoding matters just as much. Vector tiles arrive gzipped and are
        stored that way, so both hops stay cheap — the 4G fetch and the shore
        link to the operator's laptop. Replaying the bytes without their
        ``Content-Encoding`` gives a map that renders nothing while every
        request returns 200, which reads as a styling bug and is not one.
        """
        if not result.found:
            # 204 rather than 404: a missing tile is normal — outside a partial
            # tile set, above a source's max zoom, or simply not cached yet on a
            # link too poor to fetch it. A wall of 404s in the browser console
            # hides the problems that matter.
            return Response(
                status_code=204,
                headers={"X-Asket-Tile-Origin": result.origin},
            )

        data = result.data
        encoding = result.content_encoding
        if encoding and encoding.lower() not in accept_encoding.lower():
            # A client that cannot take the encoding we hold. Rare — every
            # browser accepts gzip — but a curl with `--no-compressed` should
            # get something it can read rather than a binary surprise.
            data = _decode(data, encoding)
            encoding = ""

        headers = {
            "Cache-Control": f"public, max-age={cache_seconds}",
            "X-Asket-Tile-Origin": result.origin,
        }
        if encoding:
            headers["Content-Encoding"] = encoding
            # Any cache between here and the browser must key on the encoding,
            # or one client's gzip is served to another that asked for plain.
            headers["Vary"] = "Accept-Encoding"
        if result.age_ms is not None:
            headers["X-Asket-Tile-Age-Ms"] = str(result.age_ms)
        return Response(content=data, media_type=result.content_type, headers=headers)

    @app.get("/tiles/source/{source_id}/{z}/{x}/{y}.{ext}")
    async def source_tile(source_id: str, z: int, x: int, y: int, ext: str,
                          request: Request) -> Response:
        """A tile from one of the online sources, or the cache standing in.

        The fetch is synchronous inside ``TileService`` and is handed to a
        worker thread here: a tile server that has stopped answering must not
        stall the event loop that is also carrying the boat's telemetry.
        """
        profile = request.headers.get(PROFILE_HEADER, "")
        result = await asyncio.to_thread(
            tile_service.tile, source_id, z, x, y, profile or None
        )
        return _serve(result, 604800, request.headers.get("Accept-Encoding", ""))

    @app.get("/tiles/fonts/{fontstack}/{glyph_range}.pbf")
    async def glyphs(fontstack: str, glyph_range: str, request: Request) -> Response:
        """Label glyphs. Without them the map draws geometry and no names."""
        profile = request.headers.get(PROFILE_HEADER, "")
        result = await asyncio.to_thread(
            tile_service.glyphs, fontstack, glyph_range, profile or None
        )
        return _serve(result, 604800, request.headers.get("Accept-Encoding", ""))

    @app.get("/tiles/{z}/{x}/{y}.{ext}")
    async def tile(z: int, x: int, y: int, ext: str) -> Response:
        """The offline ``.mbtiles`` file.

        Unchanged, and still at its original URL. It is a raster layer drawn
        *beneath* the vector basemap, so where the vector data is missing the
        offline map shows through rather than leaving a hole.
        """
        return _serve(tile_service.mbtile(z, x, y), 604800)

    # -- WebSocket --------------------------------------------------------

    @app.websocket("/ws")
    async def websocket_endpoint(ws: WebSocket) -> None:
        await ws.accept()
        session = ClientSession(
            uuid.uuid4().hex[:8],
            remote=f"{ws.client.host}:{ws.client.port}" if ws.client else "",
            shaper=LinkShaper(enabled=shape_link),
        )
        await ws.send_json(hub.add_client(session))

        writer = asyncio.create_task(_writer(ws, session))
        video = asyncio.create_task(_video_writer(ws, session))
        pinger = asyncio.create_task(_pinger(ws, session, hub, ping_interval_s))
        try:
            while True:
                message = await ws.receive_json()
                response = _handle(hub, session, message)
                if response is not None:
                    session.send(response)
        except (WebSocketDisconnect, asyncio.CancelledError):
            pass
        except Exception:  # noqa: BLE001 - a bad frame must not take the server down
            pass
        finally:
            hub.remove_client(session.id)
            for task in (writer, video, pinger):
                task.cancel()

    # -- the frontend -----------------------------------------------------

    if static_dir and Path(static_dir).is_dir():
        static = Path(static_dir)
        for problem in check_static_dir(static):
            _log.error("frontend will not serve correctly: %s", problem)

        @app.get("/")
        async def index() -> FileResponse:
            return FileResponse(static / "index.html")

        # follow_symlink is load-bearing under `colcon build --symlink-install`.
        #
        # That mode installs the frontend as symlinks from
        # install/gui_backend/share/gui_backend/static/ into build/, and
        # StaticFiles' traversal guard rejects any path that resolves outside
        # the directory it was given — so every asset came back 404 while the
        # files themselves were present and readable. The `/` route below still
        # served index.html, so the page loaded with the right title and an
        # empty <div id="root">: a blank screen with a 200 on it.
        app.mount(
            "/",
            StaticFiles(directory=str(static), html=True, follow_symlink=True),
            name="static",
        )
    else:

        @app.get("/")
        async def no_frontend() -> JSONResponse:
            return JSONResponse(
                {
                    "error": "frontend not built",
                    "fix": "cd src/asket_gui && npm install && npm run build",
                },
                status_code=503,
            )

    return app


async def _writer(ws: WebSocket, session: ClientSession) -> None:
    """Drain the client's outbox onto the socket.

    Separate from the reader so that a client which stops reading cannot block
    the hub, and separate from the hub so that a slow socket cannot slow the
    simulation or the telemetry.

    When link shaping is on (sim only), frames are delayed and dropped here to
    match the simulated bearer. Delaying in the writer rather than in the hub is
    deliberate: the hub keeps producing at the negotiated rate, the outbox fills,
    and the oldest frames are discarded — which is exactly what a real narrow
    link does to a stream nobody is throttling.
    """
    import json

    try:
        while True:
            message = await session.outbox.get()

            if session.shaper.enabled:
                if session.shaper.should_drop():
                    continue
                encoded = json.dumps(message)
                delay = session.shaper.delay_for(len(encoded.encode("utf-8")))
                if delay > 0:
                    await asyncio.sleep(delay)
                await ws.send_text(encoded)
                continue

            await ws.send_json(message)
    except (WebSocketDisconnect, RuntimeError, asyncio.CancelledError):
        pass


async def _video_writer(ws: WebSocket, session: ClientSession) -> None:
    """Drain the client's video queue onto the socket, as binary.

    A second writer rather than a branch inside the first one, because the two
    have opposite jobs. The telemetry writer must not skip anything: a position
    or a mode change dropped is information lost. This one must not *wait* for
    anything: the queue is one frame deep and newest wins, so a link that
    cannot keep up costs dropped frames rather than a growing backlog of
    pictures that were true a moment ago.

    Awaiting the send matters more than it looks. It is what bounds the number
    of frames in flight to exactly one — the next is not taken off the queue
    until this one has been handed to the transport — so a collapsing link
    cannot stack 45 kB frames in front of the telemetry sharing the socket.
    """
    try:
        while True:
            frame = await session.video_outbox.get()
            if session.shaper.enabled:
                if session.shaper.should_drop():
                    continue
                delay = session.shaper.delay_for(len(frame))
                if delay > 0:
                    await asyncio.sleep(delay)
            await ws.send_bytes(frame)
    except (WebSocketDisconnect, RuntimeError, asyncio.CancelledError):
        pass


async def _pinger(
    ws: WebSocket, session: ClientSession, hub: Hub, interval_s: float = PING_INTERVAL_S
) -> None:
    """Measure round-trip time.

    This is what automatic profile selection actually runs on: what the
    operator's laptop experiences, not what the radio claims.
    """
    sequence = 0
    try:
        while True:
            sequence += 1
            session._ping_sent_s = time.monotonic()
            session.send(
                {
                    "type": "ping",
                    "id": sequence,
                    "server_utc_ms": hub.source.now_utc_ms(),
                }
            )
            await asyncio.sleep(interval_s)
    except asyncio.CancelledError:
        pass


def _handle(hub: Hub, session: ClientSession, message: dict) -> dict | None:
    """Handle one client message. Returns a reply, or ``None``."""
    kind = message.get("type")

    if kind == "subscribe":
        return hub.subscribe(session, message.get("streams") or [])

    if kind == "unsubscribe":
        return hub.unsubscribe(session, message.get("streams") or [])

    if kind == "set_profile":
        profile = message.get("profile")
        return {
            "type": "profile",
            "server_utc_ms": hub.source.now_utc_ms(),
            **hub.set_profile(None if profile in (None, "auto") else profile),
        }

    if kind == "command":
        result = hub.issue_command(
            message.get("name", ""), message.get("args") or {}, message.get("id")
        )
        return {"type": "command_result", "server_utc_ms": hub.source.now_utc_ms(), **result}

    if kind == "pong":
        sent = getattr(session, "_ping_sent_s", None)
        if sent is not None:
            session.rtt_ms = (time.monotonic() - sent) * 1000.0
            session.last_pong_s = time.monotonic()
        return None

    return {
        "type": "error",
        "server_utc_ms": hub.source.now_utc_ms(),
        "message": f"unknown message type {kind!r}",
    }
