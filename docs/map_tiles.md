# The map basemap

The map used to render a bare coordinate grid, because the GUI was built on the
assumption that there is no internet in the field. There is: Namibia has 4G at
the launch point and usually from the shore station. This document is what
replaced that assumption, and — more importantly — what it costs somebody else.

## The source ladder

Four sources, strict order of preference:

| | Source | When |
|---|---|---|
| 1 | **Disk cache on the Jetson** | Always tried first. Instant, needs no network, survives a browser reload, shared by every device that connects. |
| 2 | **Online tiles** | Only tiles somebody is looking at, and only on a full-quality link. |
| 3 | **Local `.mbtiles`** | The original offline path. Kept for a survey somewhere with no coverage. |
| 4 | **Coordinate graticule** | When there is nothing else. Degraded, and visibly so. |

Implemented in `gui_backend/core/tiles.py`. Nothing decides this in the
frontend; the frontend asks and is told.

## Why not OpenStreetMap's own tiles

This was the obvious first choice and it is the wrong one, because the caching
above would breach the OSMF tile usage policy. That policy says:

> Offline use is not permitted on `tile.openstreetmap.org`.

and counts as bulk downloading

> any pre-emptive fetching of tiles other than those a user is actively viewing

and says of caching proxies that they "generally do not recommend" them.

A disk cache that keeps rendering after the link drops **is** offline use. You
cannot have both that and their tiles, and "we are only a student club" is not
a reason to take a free service for more than it offers.

## What we use instead

### Basemap — OpenFreeMap

`https://tiles.openfreemap.org/planet/latest/{z}/{x}/{y}.pbf`

Same OpenStreetMap data. No account, no registration, no API key, no cookies,
and explicitly "no limits on the number of map views or requests". Commercial
use is permitted and the whole stack is self-hostable, so if the public
instance ever goes away the fix is to run our own rather than to rewrite the
client.

It serves tiles with `Cache-Control: max-age=315360000` — ten years. A service
setting a ten-year max-age is asking to be cached.

**Vector, not raster**, and that was a deliberate choice beyond the licensing:

- The basemap has to *recede*. This map already carries coverage, planned
  lines, geofence, track, lidar returns and the vessel, in green, blue, brown,
  black and orange. A standard raster street map fights every one of them. A
  vector style can be muted — near-white land, desaturated blue-grey water,
  grey woodland because green is taken — so the survey stays the loudest thing
  on screen.
- One z14 tile renders at every zoom above it. A 5 km × 5 km survey box costs
  roughly **20 tiles, about 250 kB**, against ~120 raster tiles and ~1.8 MB.

The style is hand-written in `asket_gui/src/lib/mapStyle.js` rather than
fetched, so it works offline and points every URL at our own backend.

### Nautical overlay — OpenSeaMap

`https://tiles.openseamap.org/seamark/{z}/{x}/{y}.png`

Transparent PNG, ~1.7 kB a tile, layered over the vector base. Depth contours,
buoys, beacons and lights — worth considerably more than street data for a
bathymetric survey.

**Off by default, and labelled when on.** It is a small volunteer server and
the render lags the data badly: the tile sampled while writing this carried
`Last-Modified: Aug 2025`. Treat it as orientation, never as a navigation
authority, and never plan a survey line from it.

### Glyphs

`https://tiles.openfreemap.org/fonts/{fontstack}/{range}.pbf`, cached like
tiles. Without them MapLibre draws the geometry and no place names, and place
names are most of what makes a basemap useful for saying where you are over a
radio.

No sprite sheet is fetched: the muted style uses no icons, which saves 77 kB
and two proxy routes.

## What we promise in return

These are in `test_tile_service.py` so that breaking one has to be deliberate.

- **A User-Agent that names this application** and offers a contact URL. Never
  a library default, never anything impersonating a browser.
- **Conditional requests.** `If-None-Match` *and* `If-Modified-Since`, so a
  revalidation is a header exchange rather than a re-download. Not every server
  offers both.
- **No pre-fetching, ever.** Only tiles a client has actually requested. There
  is no seed-the-survey-box function and there should not be one — a survey box
  is twenty tiles, and the saving is not worth the habit.
- **Real caching**, honouring the cache lifetime rather than re-asking.
- **Attribution on the map**, which is an ODbL condition and not a courtesy.
  `attributionControl` used to be switched off; it is now on, and each source
  carries its own string so a layer cannot render without its credit.

If a provider ever asks us to stop: set `online_tiles: false`. That restores
the offline-only behaviour in one line, with no code change.

## Bandwidth

Tiles are nice to have. Position is not.

| Profile | Tiles |
|---|---|
| `full` | Fetched and cached normally. |
| `reduced` | **Cache only.** No network fetches, not even a revalidation. |
| `minimal` (beacon) | Cache only, and the map says why it stopped. |

One vector tile is about a second of a `reduced` budget and fifteen seconds of
a beacon's, so this is not marginal. Everything already cached keeps rendering
at every profile — the boat's position relative to the coast matters *more*
when the link is bad, not less.

The browser tells the backend its profile in an `X-Asket-Profile` header rather
than a query parameter: in the URL it would change every tile's address the
moment the profile changed, and MapLibre would re-request the whole viewport at
exactly the wrong moment.

## Saying what it is showing

The map never presents a cached basemap as live. `/api/tiles/info` reports the
state and the map says, in words, which of these it is:

- *"Map paused"* — the link profile suspended fetching. Not a fault.
- *"Map is cached, not live"* — the tile server is unreachable, with the error
  and the age of the newest cached tile.
- *"Offline basemap"* — showing the `.mbtiles` file: a snapshot, not a live map.
- *"No basemap"* — nothing available, coordinate grid only, and why.

Every tile response also carries `X-Asket-Tile-Origin` (`cache` / `upstream` /
`mbtiles` / `miss`) and `X-Asket-Tile-Age-Ms`.

## Configuration

```yaml
server:
  online_tiles: true
  tile_cache_path: "/data/maps/tile-cache.sqlite"
  tile_cache_max_bytes: 536870912
  tiles_path: "/data/maps/survey.mbtiles"
```

The cache is one SQLite file, so it can be copied between machines or kept
between deployments. **A cache from a survey already flown is a basemap that is
there before the first byte of network** — which is the cheapest possible
answer to "what if there is no signal at the launch point".

That claim needed work to be true: SQLite in WAL mode keeps recent writes in a
side file, and an early version of this left 745 kB of tiles in `-wal` with a
4 kB main file. Copying it produced an empty cache. The backend now checkpoints
on shutdown and keeps the log short while running, so **shut the GUI down
cleanly before copying the file** and you get all of it.

## Producing an offline `.mbtiles`

Worth stating because the obvious method is forbidden: **do not bulk-download
from `tile.openstreetmap.org`.** Their policy rules it out in as many words,
and it is exactly the behaviour that gets a free service withdrawn.

Use `pmtiles extract` against the Protomaps daily OSM build instead. It gives a
bounding-box cutout under ODbL without scraping anybody's tile server, and it
is a single file like MBTiles. Nothing in this repository produces one yet.

## Testing

```bash
pytest src/gui_backend/test/test_tile_service.py    # no network, scripted server
python3 -m gui_backend.core.app --sim --tile-cache /tmp/tiles.sqlite
```

The tests use an injectable opener, so a dead link, a 304 and a corrupted
response are all arranged without touching the internet.
