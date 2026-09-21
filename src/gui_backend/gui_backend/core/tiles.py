"""Map tiles: where they come from, in what order, and what that costs.

The map has four possible sources and a strict order of preference. The order
is the whole design, so it is written here once:

1. **The disk cache on the Jetson.** Instant, needs no network, survives a
   browser reload, and serves every device that connects. See
   ``tile_cache.py`` for why it exists and which provider's terms permit it.

2. **Upstream, over the internet.** OpenFreeMap for the basemap, OpenSeaMap for
   the nautical overlay. Only tiles a client is actually looking at, never
   pre-fetched, and **only on a full-quality link** — see :class:`TilePolicy`.

3. **A local ``.mbtiles`` file.** The original offline path, kept. It is what
   we would use surveying somewhere with no coverage at all, and it costs
   nothing to keep working.

4. **A coordinate graticule**, drawn by the frontend when there is nothing
   else. Degraded, and visibly so — it never pretends to be a chart.

MBTiles stores rows in TMS order, with y counted from the south. Web map clients
count from the north. Getting that flip wrong produces a map that looks almost
right, which is worse than one that looks obviously wrong.
"""

from __future__ import annotations

import sqlite3
import threading
import urllib.parse
from dataclasses import dataclass
from pathlib import Path

from .tile_cache import CachedBlob, TileCache, TileResult, UpstreamTiles


@dataclass
class TileSetInfo:
    available: bool
    path: str = ""
    name: str = ""
    format: str = "png"
    min_zoom: int = 0
    max_zoom: int = 0
    #: [west, south, east, north] in degrees, if the file declares it.
    bounds: list[float] | None = None
    tile_count: int = 0
    message: str = ""

    def to_dict(self) -> dict:
        return {
            "available": self.available,
            "name": self.name,
            "format": self.format,
            "min_zoom": self.min_zoom,
            "max_zoom": self.max_zoom,
            "bounds": self.bounds,
            "tile_count": self.tile_count,
            "message": self.message,
        }

    def covers(self, lat: float, lon: float) -> bool:
        if not self.available or not self.bounds:
            return self.available
        west, south, east, north = self.bounds
        return west <= lon <= east and south <= lat <= north


class MBTiles:
    """Read-only MBTiles reader. Thread-safe by way of one connection per thread."""

    def __init__(self, path: str | Path | None) -> None:
        self.path = Path(path) if path else None
        self._local = threading.local()
        self.info = self._read_info()

    # -- connection -------------------------------------------------------

    def _connection(self) -> sqlite3.Connection | None:
        if not self.path or not self.path.is_file():
            return None
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(
                f"file:{self.path}?mode=ro", uri=True, check_same_thread=False
            )
            self._local.conn = conn
        return conn

    def _read_info(self) -> TileSetInfo:
        if not self.path:
            return TileSetInfo(
                False,
                message=(
                    "No offline tile file configured. The map will show a "
                    "coordinate grid only. Set tiles_path to an .mbtiles file "
                    "covering the survey area."
                ),
            )
        if not self.path.is_file():
            return TileSetInfo(
                False,
                path=str(self.path),
                message=(
                    f"Offline tile file {self.path} not found. The map will show "
                    "a coordinate grid only."
                ),
            )
        try:
            conn = self._connection()
            meta = {
                k: v for k, v in conn.execute("SELECT name, value FROM metadata")
            }
            zooms = conn.execute(
                "SELECT MIN(zoom_level), MAX(zoom_level), COUNT(*) FROM tiles"
            ).fetchone()
        except sqlite3.Error as exc:
            return TileSetInfo(
                False, path=str(self.path), message=f"Tile file unreadable: {exc}"
            )

        bounds = None
        if "bounds" in meta:
            try:
                bounds = [float(x) for x in meta["bounds"].split(",")]
            except ValueError:
                bounds = None

        min_zoom = int(meta.get("minzoom", zooms[0] or 0))
        max_zoom = int(meta.get("maxzoom", zooms[1] or 0))
        return TileSetInfo(
            available=bool(zooms[2]),
            path=str(self.path),
            name=meta.get("name", self.path.stem),
            format=meta.get("format", "png"),
            min_zoom=min_zoom,
            max_zoom=max_zoom,
            bounds=bounds,
            tile_count=int(zooms[2] or 0),
            message=(
                f"{zooms[2]} tiles, zoom {min_zoom}-{max_zoom}"
                if zooms[2]
                else "Tile file contains no tiles."
            ),
        )

    # -- tiles ------------------------------------------------------------

    def tile(self, z: int, x: int, y: int) -> bytes | None:
        """One tile, addressed in **XYZ** (y from the north) as clients use.

        The TMS flip happens here, once, rather than in the client where it
        would be easy to get subtly wrong.
        """
        conn = self._connection()
        if conn is None:
            return None
        tms_y = (1 << z) - 1 - y
        try:
            row = conn.execute(
                "SELECT tile_data FROM tiles "
                "WHERE zoom_level=? AND tile_column=? AND tile_row=?",
                (z, x, tms_y),
            ).fetchone()
        except sqlite3.Error:
            return None
        return bytes(row[0]) if row else None

    @property
    def content_type(self) -> str:
        return {
            "png": "image/png",
            "jpg": "image/jpeg",
            "jpeg": "image/jpeg",
            "webp": "image/webp",
            "pbf": "application/x-protobuf",
        }.get(self.info.format.lower(), "application/octet-stream")


# =========================================================================
#  Upstream sources
# =========================================================================

@dataclass(frozen=True)
class UpstreamSource:
    """One tile server we are willing to ask.

    ``attribution`` is not decoration. OpenStreetMap data is ODbL and the
    attribution is a licence condition, not a courtesy — it travels with the
    source so the frontend cannot render a layer without knowing what it owes.
    """

    id: str
    url_template: str
    content_type: str
    attribution: str
    max_zoom: int
    #: Shown in the GUI's layer control.
    label: str = ""
    #: What it is for, in one line, for somebody who has not read this file.
    note: str = ""

    def url(self, z: int, x: int, y: int) -> str:
        return (
            self.url_template
            .replace("{z}", str(z))
            .replace("{x}", str(x))
            .replace("{y}", str(y))
        )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "label": self.label,
            "attribution": self.attribution,
            "max_zoom": self.max_zoom,
            "note": self.note,
        }


#: The vector basemap.
#:
#: OpenFreeMap, not tile.openstreetmap.org, and the reason is in
#: ``tile_cache.py``: OSMF's policy forbids the offline caching this GUI needs,
#: OpenFreeMap invites it. Same OpenStreetMap data, no key, no account.
#:
#: ``/planet/latest`` is a moving target, which is fine — a basemap that lags a
#: few weeks behind is not a problem a survey notices, and our own TTL decides
#: when to revalidate rather than trusting their ten-year max-age.
BASEMAP = UpstreamSource(
    id="basemap",
    url_template="https://tiles.openfreemap.org/planet/latest/{z}/{x}/{y}.pbf",
    content_type="application/vnd.mapbox-vector-tile",
    attribution=(
        "OpenFreeMap © OpenMapTiles Data from © OpenStreetMap contributors"
    ),
    max_zoom=14,
    label="Basemap",
    note="Coastline, harbours, roads and place names. Rendered from vector data.",
)

#: The nautical overlay: depth contours, buoys, beacons, lights.
#:
#: Worth far more than street data for a bathymetric survey, and it is a
#: transparent PNG so it layers straight over the vector base.
#:
#: Treated as best-effort and never as a navigation authority. It is a small
#: volunteer server, the render lags the data by a long way — the tile sampled
#: while writing this carried ``Last-Modified: Aug 2025`` — and nothing here has
#: been checked against a real chart.
SEAMARK = UpstreamSource(
    id="seamark",
    url_template="https://tiles.openseamap.org/seamark/{z}/{x}/{y}.png",
    content_type="image/png",
    attribution="© OpenSeaMap contributors (CC-BY-SA)",
    max_zoom=18,
    label="Sea marks",
    note=(
        "Depth contours and navigation marks. Volunteer-maintained, lags the "
        "survey data, and is NOT a navigation authority."
    ),
)

#: Glyph ranges for map labels. Not a tile source, but fetched and cached the
#: same way: without them MapLibre draws the geometry and no place names, which
#: is most of what makes a basemap useful for orientation.
FONTS_URL_TEMPLATE = "https://tiles.openfreemap.org/fonts/{fontstack}/{range}.pbf"

UPSTREAM_SOURCES = {s.id: s for s in (BASEMAP, SEAMARK)}


# =========================================================================
#  Policy
# =========================================================================

class TilePolicy:
    """Whether we are allowed to go to the network right now.

    Tiles are nice to have. Position is not. On a link that is already
    struggling, a single vector tile is about a second of a ``reduced``
    profile's entire budget and fifteen seconds of a beacon's — so below full
    quality the map stops fetching and lives on what it has.

    The rule is deliberately one sentence long, because an operator has to hold
    it in their head on a beach: **tiles are fetched only on a full-quality
    link.** Everything already cached keeps rendering at every profile.
    """

    #: Profiles on which a network fetch is permitted.
    FETCHING_PROFILES = ("full",)

    @classmethod
    def may_fetch(cls, profile: str | None) -> bool:
        # An absent profile means a client that is not the GUI — curl, a test,
        # a browser hitting the URL directly. Those are rare and deliberate, so
        # they are allowed rather than silently served a blank tile.
        if not profile:
            return True
        return profile in cls.FETCHING_PROFILES

    @classmethod
    def reason(cls, profile: str | None) -> str:
        if cls.may_fetch(profile):
            return ""
        return (
            f"Map tiles are not being downloaded on the {profile} link profile, "
            f"so telemetry keeps the bandwidth. Tiles already cached still show."
        )


# =========================================================================
#  The service
# =========================================================================

class TileService:
    """The ladder, in one place.

    Everything above this is a source; this decides the order and the policy.
    Keeping the decision here rather than spreading it across routes is what
    makes it possible to state the order in one docstring and test it in one
    file.
    """

    def __init__(
        self,
        cache: TileCache,
        upstream: UpstreamTiles,
        mbtiles: MBTiles | None = None,
        online: bool = True,
        sources: dict[str, UpstreamSource] | None = None,
    ) -> None:
        self.cache = cache
        self.upstream = upstream
        self.mbtiles = mbtiles
        #: Master switch. Set False to force the pre-internet behaviour, which
        #: is the configuration to use if a provider ever asks us to stop.
        self.online = online
        self.sources = sources if sources is not None else dict(UPSTREAM_SOURCES)

    # -- tiles ------------------------------------------------------------

    def tile(self, source_id: str, z: int, x: int, y: int, profile: str | None = None):
        """One tile, from the best source available, with its provenance."""
        source = self.sources.get(source_id)
        if source is None:
            return TileResult(None, "miss")

        key = TileCache.tile_key(z, x, y)
        cached = self.cache.get(source_id, key)

        # 1. A fresh cached tile is the answer, and costs nothing.
        if cached is not None:
            if self.cache.is_fresh(cached.age_ms) or not self._may_go_upstream(profile):
                return _from_cache(cached, source.content_type)
        validators = (cached.etag, cached.last_modified) if cached else ("", "")

        # 2. Upstream, if the link can afford it. Requesting beyond a source's
        #    max zoom is asking for a 404 we already know the answer to: the
        #    frontend overzooms instead, which is what vector tiles are for.
        if self._may_go_upstream(profile) and z <= source.max_zoom:
            result = self._fetch(source, key, z, x, y, *validators)
            if result is not None:
                return result

        # 3. The stale cached copy, if the fetch failed or was not allowed. This
        #    is the link-drop case, and it is why any of this exists.
        if cached is not None:
            return _from_cache(cached, source.content_type)

        # 4. is not reached from here. The offline .mbtiles is a *raster*
        #    fallback under a *vector* basemap, so it cannot be substituted for
        #    a missing vector tile — it is served by mbtile() as its own layer.
        return TileResult(None, "miss")

    def mbtile(self, z: int, x: int, y: int) -> TileResult:
        """The offline ``.mbtiles`` file, as its own source.

        Kept separate from :meth:`tile` rather than folded into the ladder,
        because it is a *raster* fallback under a *vector* basemap: the frontend
        draws it as a layer beneath, so a hole in the vector data shows the
        offline map through it instead of showing nothing.
        """
        if self.mbtiles is None:
            return TileResult(None, "miss")
        data = self.mbtiles.tile(z, x, y)
        if data is None:
            return TileResult(None, "miss")
        return TileResult(data, "mbtiles", self.mbtiles.content_type, None)

    # -- fonts ------------------------------------------------------------

    def glyphs(self, fontstack: str, glyph_range: str, profile: str | None = None):
        """One glyph range, cached like a tile.

        Without these MapLibre renders geometry and no text. A coastline with no
        place names is still useful; a coastline with place names is what makes
        the map worth having, so they are cached as hard as the tiles are.
        """
        key = f"{fontstack}/{glyph_range}"
        default_type = "application/x-protobuf"
        cached = self.cache.get("fonts", key)
        if cached is not None:
            if self.cache.is_fresh(cached.age_ms) or not self._may_go_upstream(profile):
                return _from_cache(cached, default_type)
        validators = (cached.etag, cached.last_modified) if cached else ("", "")

        if self._may_go_upstream(profile):
            url = (
                FONTS_URL_TEMPLATE
                .replace("{fontstack}", urllib.parse.quote(fontstack))
                .replace("{range}", glyph_range)
            )
            fetched = self.upstream.fetch(url, *validators)
            if fetched.not_modified and cached is not None:
                self.cache.refresh("fonts", key)
                return _from_cache(cached, default_type, age_ms=0)
            if fetched.data:
                self.cache.put(
                    "fonts", key, fetched.data, fetched.content_type or default_type,
                    fetched.etag, fetched.last_modified, fetched.content_encoding,
                )
                return TileResult(
                    fetched.data, "upstream", fetched.content_type or default_type,
                    None, fetched.content_encoding,
                )

        if cached is not None:
            return _from_cache(cached, default_type)
        return TileResult(None, "miss")

    # -- internals --------------------------------------------------------

    def _may_go_upstream(self, profile: str | None) -> bool:
        return self.online and TilePolicy.may_fetch(profile)

    def _fetch(self, source, key, z, x, y, etag, last_modified=""):
        fetched = self.upstream.fetch(source.url(z, x, y), etag, last_modified)
        if fetched.not_modified:
            # The copy we hold is still current. This is the conditional
            # request paying for itself: a header exchange instead of a tile.
            self.cache.refresh(source.id, key)
            cached = self.cache.get(source.id, key)
            if cached is not None:
                return _from_cache(cached, source.content_type, age_ms=0)
            return None
        if fetched.data:
            self.cache.put(
                source.id, key, fetched.data,
                fetched.content_type or source.content_type,
                fetched.etag, fetched.last_modified, fetched.content_encoding,
            )
            return TileResult(
                fetched.data, "upstream", fetched.content_type or source.content_type,
                None, fetched.content_encoding,
            )
        return None

    # -- reporting --------------------------------------------------------

    def describe(self, profile: str | None = None) -> dict:
        """Everything the map needs to say honestly what it is showing."""
        return {
            "online": self.online,
            "fetching": self._may_go_upstream(profile),
            "fetch_suspended_reason": (
                "" if self._may_go_upstream(profile)
                else (TilePolicy.reason(profile) if self.online
                      else "Online tiles are switched off in the configuration.")
            ),
            "profile": profile or "",
            "sources": [s.to_dict() for s in self.sources.values()],
            "upstream": self.upstream.status.to_dict(),
            "cache": self.cache.stats(),
            "mbtiles": (
                self.mbtiles.info.to_dict()
                if self.mbtiles
                else {"available": False, "message": "No offline tile file configured."}
            ),
        }


def _from_cache(blob: CachedBlob, fallback_type: str, age_ms: int | None = None) -> TileResult:
    """A cached blob as a served result, encoding intact.

    The encoding has to survive the round trip. Serving a gzipped vector tile
    without its ``Content-Encoding`` produces a map that renders nothing while
    every request returns 200 — which looks like a styling bug and is not one.
    """
    return TileResult(
        data=blob.data,
        origin="cache",
        content_type=blob.content_type or fallback_type,
        age_ms=blob.age_ms if age_ms is None else age_ms,
        content_encoding=blob.content_encoding,
    )
