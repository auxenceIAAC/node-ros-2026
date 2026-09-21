"""A disk cache for map tiles, and the upstream fetcher that fills it.

Why a cache at all
==================

The GUI used to assume there is no internet in the field. There is: Namibia has
4G at the launch point and usually from the shore station. So the map can have a
real basemap instead of a coordinate grid — but the link will still drop
mid-mission, and a map that goes blank the moment it does is no better than the
grid it replaced.

Hence: every tile the operator actually looks at is written to disk on the
Jetson. The next request for it needs no network, survives a browser reload,
and serves every device that connects rather than one browser's volatile cache.

Why not tile.openstreetmap.org
==============================

Because this cache would violate their terms. The OSMF tile usage policy says,
in as many words:

    "Offline use is not permitted on tile.openstreetmap.org."

and counts as bulk downloading "any pre-emptive fetching of tiles other than
those a user is actively viewing", and says of caching proxies that they
"generally do not recommend" them. A disk cache that keeps rendering after the
link drops is offline use. You cannot have both that and their tiles.

So the basemap comes from **OpenFreeMap** instead: same OpenStreetMap data, no
account, no API key, no registration, explicitly "no limits on the number of map
views or requests", and it serves its tiles with ``Cache-Control: max-age=
315360000`` — ten years. A service that sets a ten-year max-age is asking to be
cached. It is also self-hostable, so if the public instance ever goes away the
fix is to run it ourselves rather than to rewrite this file.

The nautical overlay comes from OpenSeaMap, which is a small volunteer server:
cached hard, treated as best-effort, and never as a navigation authority.

What this file will not do
==========================

**It never pre-fetches.** There is no seed-the-survey-box function here, and
there should not be one. Only tiles a client has asked for are fetched, which is
the rule every tile provider's policy is built around, and it is not made less
true by the provider being permissive. A survey box costs about twenty vector
tiles anyway; the temptation to bulk-download is not worth the habit.

Storage
=======

One SQLite file, the same choice and for the same reason as ``MBTiles``: it is
in the standard library, it gives atomic writes and a single file to copy, and
pulling in a caching library for this would be an arm64 build dependency for
nothing.

A useful side effect of one file: the cache **is** an offline map. Copy it to
the next machine, or keep the one from a survey you have already flown, and the
basemap is there before the first byte of network.
"""

from __future__ import annotations

import sqlite3
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

#: Identifies this application to tile providers. Every policy worth respecting
#: asks for a User-Agent that names the app and offers a contact, and forbids
#: library defaults or anything impersonating a browser.
DEFAULT_USER_AGENT = (
    "AsketMissionGUI/0.1 (NODE Engineering Club seabed survey; "
    "+https://github.com/NODE-Engineering-Club)"
)

#: How long a cached tile is served without asking upstream whether it changed.
#: Basemaps move slowly — a coastline is not telemetry — and a revalidation that
#: returns 304 still costs a round trip on a link we are trying to protect.
DEFAULT_TTL_S = 30 * 24 * 3600

#: Stop the cache growing without bound. Eviction is least-recently-used.
DEFAULT_MAX_BYTES = 512 * 1024 * 1024

#: A tile request that has not answered in this long is a request competing with
#: telemetry for a link that is evidently struggling. Give up and use the cache.
DEFAULT_TIMEOUT_S = 6.0


#: Bumped whenever the stored columns change. A cache is disposable by
#: definition, so a schema change throws the old one away and refills it rather
#: than carrying migration code for data we can re-fetch in seconds.
SCHEMA_VERSION = 2


@dataclass(frozen=True)
class TileResult:
    """One tile, and — the part that matters — where it came from.

    The GUI's standing rule is that it never presents stale data as current.
    A month-old coastline is not the lie a month-old position would be, but the
    operator should still be able to tell, so provenance travels with the bytes
    rather than being inferred at the far end.
    """

    data: bytes | None
    #: "cache", "upstream", "mbtiles", or "miss".
    origin: str
    content_type: str = "application/octet-stream"
    #: Age of the stored copy. None when it came straight off the network.
    age_ms: int | None = None
    #: "gzip", "deflate" or "". Vector tiles arrive compressed and are **stored
    #: compressed**, then replayed with this header. That keeps both hops cheap
    #: — the 4G fetch and the shore link to the operator's laptop — and costs no
    #: CPU at either end, since the browser decompresses natively.
    content_encoding: str = ""

    @property
    def found(self) -> bool:
        return self.data is not None


@dataclass
class UpstreamStatus:
    """What happened the last time we tried to reach a tile server.

    Kept so the map can say *why* it is showing a cached basemap, rather than
    leaving the operator to guess whether the link is down or the feature is
    switched off.
    """

    reachable: bool | None = None       # None = not tried yet
    last_ok_utc_ms: int | None = None
    last_error: str = ""
    fetches: int = 0
    not_modified: int = 0               # 304s: cheap, and worth being visible
    failures: int = 0

    def to_dict(self) -> dict:
        return {
            "reachable": self.reachable,
            "last_ok_utc_ms": self.last_ok_utc_ms,
            "last_error": self.last_error,
            "fetches": self.fetches,
            "not_modified": self.not_modified,
            "failures": self.failures,
        }


class TileCache:
    """Tiles and assets on disk, in one SQLite file.

    Thread-safe: reads take their own per-thread connection, writes serialise on
    a lock. WAL is on so a read during a write does not block.
    """

    def __init__(
        self,
        path: str | Path | None,
        max_bytes: int = DEFAULT_MAX_BYTES,
        ttl_s: float = DEFAULT_TTL_S,
    ) -> None:
        self.path = Path(path) if path else None
        self.max_bytes = max_bytes
        self.ttl_s = ttl_s
        self._local = threading.local()
        self._write_lock = threading.Lock()
        self.enabled = False
        self.error = ""
        if self.path:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                self._initialise()
                self.enabled = True
            except (sqlite3.Error, OSError) as exc:
                # A cache we cannot open is a degraded map, not a dead one:
                # everything still works, it just goes to the network each time.
                self.error = f"{exc}"

    # -- plumbing ---------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(str(self.path), check_same_thread=False, timeout=5.0)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            # WAL keeps readers from blocking on a write, which matters when a
            # dozen tile requests arrive at once. But its default checkpoint
            # threshold is ~4 MB, and a cache whose data is mostly sitting in a
            # side file is not the single portable file this claims to be:
            # copying it to another machine would silently leave the tiles
            # behind. 64 pages keeps the main file current.
            conn.execute("PRAGMA wal_autocheckpoint=64")
            self._local.conn = conn
        return conn

    def _initialise(self) -> None:
        conn = self._connect()
        with self._write_lock:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS schema (version INTEGER NOT NULL);
                """
            )
            row = conn.execute("SELECT version FROM schema").fetchone()
            if row is not None and int(row[0]) != SCHEMA_VERSION:
                conn.executescript("DROP TABLE IF EXISTS blobs; DELETE FROM schema;")
                row = None
            if row is None:
                conn.execute("INSERT INTO schema (version) VALUES (?)", (SCHEMA_VERSION,))

            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS blobs (
                    source           TEXT NOT NULL,
                    key              TEXT NOT NULL,
                    data             BLOB NOT NULL,
                    content_type     TEXT NOT NULL DEFAULT '',
                    content_encoding TEXT NOT NULL DEFAULT '',
                    etag             TEXT NOT NULL DEFAULT '',
                    last_modified    TEXT NOT NULL DEFAULT '',
                    fetched_utc_ms   INTEGER NOT NULL,
                    used_utc_ms      INTEGER NOT NULL,
                    bytes            INTEGER NOT NULL,
                    PRIMARY KEY (source, key)
                );
                CREATE INDEX IF NOT EXISTS blobs_lru ON blobs (used_utc_ms);
                """
            )
            conn.commit()

    @staticmethod
    def tile_key(z: int, x: int, y: int) -> str:
        return f"{z}/{x}/{y}"

    # -- reading ----------------------------------------------------------

    def get(self, source: str, key: str) -> CachedBlob | None:
        """A stored blob, whatever its age.

        Whether an old copy is good enough is a policy question, and policy does
        not belong in a cache — see
        :class:`~gui_backend.core.tiles.TileService`.
        """
        if not self.enabled:
            return None
        try:
            row = self._connect().execute(
                "SELECT data, content_type, content_encoding, fetched_utc_ms, "
                "etag, last_modified FROM blobs WHERE source=? AND key=?",
                (source, key),
            ).fetchone()
        except sqlite3.Error:
            return None
        if row is None:
            return None

        now = _now_ms()
        # Touching on every read would mean a write per tile per pan, which on a
        # Jetson's SD card is a lot of wear for a marginal eviction decision.
        # Once an hour is enough to keep LRU meaningful.
        if now - int(row[3]) > 3600_000:
            self._touch(source, key, now)
        return CachedBlob(
            data=bytes(row[0]),
            content_type=str(row[1]),
            content_encoding=str(row[2]),
            age_ms=max(0, now - int(row[3])),
            etag=str(row[4]),
            last_modified=str(row[5]),
        )

    def _touch(self, source: str, key: str, now: int) -> None:
        try:
            with self._write_lock:
                conn = self._connect()
                conn.execute(
                    "UPDATE blobs SET used_utc_ms=? WHERE source=? AND key=?",
                    (now, source, key),
                )
                conn.commit()
        except sqlite3.Error:
            pass

    def is_fresh(self, age_ms: int) -> bool:
        """Whether a stored copy can be served without asking upstream.

        A TTL of zero or less means "always revalidate", which is what anybody
        setting it to 0 expects. Without the guard a copy fetched microseconds
        ago would count as fresh against a zero TTL and never be re-checked.
        """
        if self.ttl_s <= 0:
            return False
        return age_ms <= self.ttl_s * 1000

    # -- writing ----------------------------------------------------------

    def put(
        self,
        source: str,
        key: str,
        data: bytes,
        content_type: str,
        etag: str = "",
        last_modified: str = "",
        content_encoding: str = "",
    ) -> None:
        if not self.enabled or not data:
            return
        now = _now_ms()
        try:
            with self._write_lock:
                conn = self._connect()
                conn.execute(
                    "INSERT OR REPLACE INTO blobs "
                    "(source, key, data, content_type, content_encoding, etag, "
                    " last_modified, fetched_utc_ms, used_utc_ms, bytes) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (source, key, data, content_type, content_encoding, etag,
                     last_modified, now, now, len(data)),
                )
                conn.commit()
        except sqlite3.Error:
            return
        self._evict_if_needed()

    def refresh(self, source: str, key: str) -> None:
        """Mark a copy as re-confirmed, after upstream answered 304.

        This is the whole point of conditional requests: a few hundred bytes
        instead of a tile, and the copy we already hold becomes fresh again.
        """
        if not self.enabled:
            return
        now = _now_ms()
        try:
            with self._write_lock:
                conn = self._connect()
                conn.execute(
                    "UPDATE blobs SET fetched_utc_ms=?, used_utc_ms=? "
                    "WHERE source=? AND key=?",
                    (now, now, source, key),
                )
                conn.commit()
        except sqlite3.Error:
            pass

    def _evict_if_needed(self) -> None:
        total = self.total_bytes()
        if total <= self.max_bytes:
            return
        target = int(self.max_bytes * 0.9)   # evict below the line, not to it
        try:
            with self._write_lock:
                conn = self._connect()
                rows = conn.execute(
                    "SELECT source, key, bytes FROM blobs ORDER BY used_utc_ms ASC"
                ).fetchall()
                for source, key, size in rows:
                    if total <= target:
                        break
                    conn.execute(
                        "DELETE FROM blobs WHERE source=? AND key=?", (source, key)
                    )
                    total -= int(size)
                conn.commit()
        except sqlite3.Error:
            pass

    # -- reporting --------------------------------------------------------

    def total_bytes(self) -> int:
        if not self.enabled:
            return 0
        try:
            row = self._connect().execute("SELECT SUM(bytes) FROM blobs").fetchone()
        except sqlite3.Error:
            return 0
        return int(row[0] or 0)

    def checkpoint(self) -> None:
        """Fold the write-ahead log back into the main file.

        Called on shutdown so what is left on disk is one file that can be
        copied, kept between deployments, or handed to the next expedition.
        """
        if not self.enabled:
            return
        try:
            with self._write_lock:
                self._connect().execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except sqlite3.Error:
            pass

    def stats(self) -> dict:
        if not self.enabled:
            return {"enabled": False, "error": self.error, "entries": 0, "bytes": 0}
        try:
            row = self._connect().execute(
                "SELECT COUNT(*), COALESCE(SUM(bytes),0), MIN(fetched_utc_ms), "
                "MAX(fetched_utc_ms) FROM blobs"
            ).fetchone()
        except sqlite3.Error as exc:
            return {"enabled": False, "error": str(exc), "entries": 0, "bytes": 0}
        return {
            "enabled": True,
            "error": "",
            "path": str(self.path),
            "entries": int(row[0] or 0),
            "bytes": int(row[1] or 0),
            "max_bytes": self.max_bytes,
            "oldest_utc_ms": int(row[2]) if row[2] is not None else None,
            "newest_utc_ms": int(row[3]) if row[3] is not None else None,
        }


class UpstreamTiles:
    """Fetches one tile, politely.

    Uses ``urllib`` rather than a client library on purpose: this package's
    ``core/`` is plain Python with no third-party imports, so it is testable on a
    laptop with nothing installed, and the Jetson does not grow an arm64 build
    dependency for thirty lines of HTTP.

    Blocking, deliberately. The async wrapper lives in ``server.py``, which hands
    this to a worker thread; keeping the network call synchronous keeps this
    module free of an event loop and therefore trivially testable.
    """

    def __init__(
        self,
        user_agent: str = DEFAULT_USER_AGENT,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        opener=None,
    ) -> None:
        self.user_agent = user_agent
        self.timeout_s = timeout_s
        #: Injectable so tests never touch the network.
        self._opener = opener or urllib.request.urlopen
        self.status = UpstreamStatus()

    def fetch(self, url: str, etag: str = "", last_modified: str = "") -> FetchResult:
        """One HTTP GET, with the response's own encoding preserved.

        A failure is not an error worth raising. The link dropping mid-survey is
        the expected case, and the answer is always the same: use the cache.
        """
        request = urllib.request.Request(url, method="GET")
        request.add_header("User-Agent", self.user_agent)
        request.add_header("Accept-Encoding", "gzip, deflate, identity")
        # Conditional request. Every tile policy asks for this by name, and it
        # is the difference between a header exchange and a whole tile on a link
        # that is also carrying the boat's position.
        #
        # Both validators, because not every server offers both: OpenFreeMap
        # sends an ETag, OpenSeaMap sends an ETag and a Last-Modified, and a
        # self-hosted instance behind a plain file server may send only the
        # latter. Sending whichever we have costs nothing and a missing one
        # would silently turn every revalidation into a full re-download.
        if etag:
            request.add_header("If-None-Match", etag)
        if last_modified:
            request.add_header("If-Modified-Since", last_modified)

        try:
            with self._opener(request, timeout=self.timeout_s) as response:
                data = response.read()
                headers = response.headers
                self.status.reachable = True
                self.status.last_ok_utc_ms = _now_ms()
                self.status.last_error = ""
                self.status.fetches += 1
                return FetchResult(
                    data=data,
                    content_type=headers.get("Content-Type", "application/octet-stream"),
                    # urllib does NOT decompress: whatever arrived is what we
                    # hold, so the encoding has to travel with it. Getting this
                    # wrong serves gzip bytes labelled as a vector tile, and the
                    # map renders nothing at all while every request returns 200.
                    content_encoding=headers.get("Content-Encoding", ""),
                    etag=headers.get("ETag", ""),
                    last_modified=headers.get("Last-Modified", ""),
                )
        except urllib.error.HTTPError as exc:
            if exc.code == 304:
                self.status.reachable = True
                self.status.last_ok_utc_ms = _now_ms()
                self.status.last_error = ""
                self.status.not_modified += 1
                return FetchResult(not_modified=True)
            self.status.reachable = False
            self.status.last_error = f"HTTP {exc.code}"
            self.status.failures += 1
            return FetchResult()
        except (urllib.error.URLError, OSError, ValueError) as exc:
            self.status.reachable = False
            self.status.last_error = _short_error(exc)
            self.status.failures += 1
            return FetchResult()


@dataclass(frozen=True)
class CachedBlob:
    """What came out of the cache, with everything needed to replay it."""

    data: bytes
    content_type: str
    content_encoding: str
    age_ms: int
    etag: str
    last_modified: str = ""


@dataclass(frozen=True)
class FetchResult:
    """What came back from upstream, or did not."""

    data: bytes | None = None
    content_type: str = ""
    content_encoding: str = ""
    etag: str = ""
    last_modified: str = ""
    #: True when upstream answered 304: the copy we already hold is current.
    not_modified: bool = False


def _short_error(exc: Exception) -> str:
    """A reason an operator can act on, not a stack trace."""
    reason = getattr(exc, "reason", exc)
    text = str(reason).strip() or exc.__class__.__name__
    return text[:120]


def _now_ms() -> int:
    return int(time.time() * 1000)
