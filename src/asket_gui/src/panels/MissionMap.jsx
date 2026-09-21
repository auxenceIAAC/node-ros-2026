import maplibregl from 'maplibre-gl';
import 'maplibre-gl/dist/maplibre-gl.css';
import { useEffect, useRef, useState } from 'react';

import { coverageRibbon, lidarPoints } from '../lib/geometry.js';
import { blankStyle, buildStyle, graticule } from '../lib/mapStyle.js';
import { registerMockTileProtocol } from '../lib/mock/tiles.js';
import { streamPayload } from '../lib/connection.js';

/**
 * The primary panel.
 *
 * Vessel, heading, track, planned survey lines, coverage, lidar, geofence.
 *
 * THE BASEMAP HAS FOUR SOURCES, in this order of preference: the backend's
 * tile cache, the internet, a local .mbtiles file, and — when there is none of
 * those — a coordinate graticule. The ladder is implemented in
 * gui_backend/core/tiles.py; what happens here is deciding which layers to ask
 * for and telling the operator, in words, what they are actually looking at.
 *
 * It never silently shows an empty rectangle. An operator reads that as "no map
 * today" rather than "the link dropped" or "fix the tile file", and those call
 * for three different actions.
 */
export function MissionMap({ state, connection, follow, onFollowChange, showRawLidar = false }) {
  const container = useRef(null);
  const map = useRef(null);
  const [tiles, setTiles] = useState(null);
  const [ready, setReady] = useState(false);
  //: The sea-mark overlay is off by default. It is the most useful layer here
  //: for a survey, and it is also somebody else's volunteer data that lags the
  //: real world — so it is opt-in, and labelled when it is on.
  const [seamark, setSeamark] = useState(false);
  //: Which sources the style is built from. The map is rebuilt only when this
  //: changes, not on every poll, or it would tear down and recreate itself
  //: every twenty seconds in the middle of a survey.
  const [styleKey, setStyleKey] = useState(null);
  //: The latest tile status, read by the map-creation effect without being in
  //: its dependencies.
  const tilesRef = useRef(null);
  //: The link profile, read inside transformRequest. A ref because the
  //: callback is installed once and the profile changes under it.
  const profileRef = useRef('full');
  profileRef.current = state.profile?.profile || 'full';
  //: Camera carried across a style rebuild, so a link recovering mid-survey
  //: does not throw the operator back to the default view.
  const restoreCamera = useRef(null);
  //: The survey box is framed once, when the plan first arrives. Opening on a
  //: fixed zoom put a 300 m survey on screen as a 30 px smudge, which is not a
  //: map anyone can judge coverage from. Once only: re-framing under an
  //: operator who has zoomed in to look at a gap would be worse than the smudge.
  const framed = useRef(false);

  const vessel = streamPayload(state, 'vessel');
  const track = streamPayload(state, 'track');
  const plan = streamPayload(state, 'plan');
  const coverage = streamPayload(state, 'coverage');
  const lidar = streamPayload(state, 'lidar');

  // -- tile availability ------------------------------------------------

  // Polled rather than asked once. The answer changes during a mission: the
  // link drops, the cache fills, the profile suspends fetching. A map that
  // decided at startup what it could show would be describing a link that no
  // longer exists.
  useEffect(() => {
    let cancelled = false;
    let timer = null;

    const poll = async () => {
      let info;
      try {
        info = await connection.fetchTileInfo();
      } catch {
        info = { error: 'Could not ask the backend about map tiles.' };
      }
      if (cancelled) return;
      tilesRef.current = info;
      setTiles(info);
      const key = styleSignature(info, seamark, connection.isMock);
      setStyleKey((previous) => (previous === key ? previous : key));

      // No point polling on a beacon link: tile fetching is suspended there
      // anyway, the answer cannot change, and the poll itself is bandwidth the
      // telemetry wants.
      const idle = profileRef.current === 'minimal' ? null : 20000;
      if (idle) timer = setTimeout(poll, idle);
    };

    poll();
    return () => {
      cancelled = true;
      if (timer) clearTimeout(timer);
    };
  }, [connection, state.tileGeneration, seamark]);

  // -- map creation -----------------------------------------------------

  useEffect(() => {
    if (!container.current || styleKey === null || map.current) return;

    const info = tilesRef.current || {};
    const sources = availableSources(info, seamark, connection.isMock);

    // In mock mode the tiles are generated in the browser through a custom
    // protocol, so the tiled rendering path can be reviewed with no backend,
    // no .mbtiles file and no internet.
    if (connection.isMock) registerMockTileProtocol(maplibregl);

    const style = sources.any
      ? buildStyle({
          origin: window.location.origin,
          basemap: sources.basemap,
          seamark: sources.seamark,
          mbtiles: sources.mbtiles,
          mockTileUrl: sources.mock ? 'mocktiles://{z}/{x}/{y}' : '',
          basemapAttribution: attributionFor(info, 'basemap'),
          seamarkAttribution: attributionFor(info, 'seamark'),
        })
      : blankStyle();

    const instance = new maplibregl.Map({
      container: container.current,
      style,
      center: restoreCamera.current?.center || [14.5053, -22.9576],
      zoom: restoreCamera.current?.zoom ?? 14,
      bearing: restoreCamera.current?.bearing ?? 0,
      // ODbL is a licence, not a preference: OpenStreetMap-derived tiles may
      // not be shown without attribution. MapLibre collects the strings the
      // style declares per source, so a layer cannot be added without its
      // credit coming with it. This used to be switched off.
      attributionControl: { compact: true },
      // Tells the backend what the link can afford, so it can decide whether a
      // tile is worth fetching. A header rather than a query parameter: in the
      // URL it would change every tile's address the moment the profile
      // changed, and MapLibre would re-request the whole viewport at exactly
      // the wrong time.
      transformRequest: (url) =>
        url.startsWith(window.location.origin)
          ? { url, headers: { 'X-Asket-Profile': profileRef.current } }
          : { url },
      localIdeographFontFamily: false,
    });
    instance.addControl(new maplibregl.NavigationControl({ showCompass: true }), 'top-right');
    instance.addControl(new maplibregl.ScaleControl({ unit: 'metric' }), 'bottom-right');
    instance.on('dragstart', () => onFollowChange(false));

    instance.on('load', () => {
      addLayers(instance, !sources.any);
      map.current = instance;
      setReady(true);
    });

    return () => {
      // The style is rebuilt when the available sources change, which can
      // happen mid-survey as the link comes and goes. Carrying the camera
      // across means the operator does not get thrown back to the default view
      // because a tile server answered.
      try {
        restoreCamera.current = {
          center: instance.getCenter().toArray(),
          zoom: instance.getZoom(),
          bearing: instance.getBearing(),
        };
      } catch {
        // A map that never finished loading has no camera to keep.
      }
      instance.remove();
      map.current = null;
      setReady(false);
    };
  }, [styleKey, onFollowChange, connection, seamark]);

  // -- data into layers -------------------------------------------------

  useEffect(() => {
    const instance = map.current;
    if (!instance || !ready) return;

    setData(instance, 'plan', {
      type: 'FeatureCollection',
      features: (plan?.lines || []).map((line) => ({
        type: 'Feature',
        properties: { index: line.index },
        geometry: { type: 'LineString', coordinates: line.coords },
      })),
    });

    setData(instance, 'geofence', {
      type: 'FeatureCollection',
      features: plan?.geofence?.length
        ? [{ type: 'Feature', properties: {}, geometry: { type: 'LineString', coordinates: [...plan.geofence, plan.geofence[0]] } }]
        : [],
    });

    setData(
      instance,
      'coverage',
      coverageRibbon(coverage?.segments || [], coverage?.side),
    );

    setData(instance, 'track', {
      type: 'FeatureCollection',
      features: track?.points?.length
        ? [{ type: 'Feature', properties: {}, geometry: { type: 'LineString', coordinates: track.points } }]
        : [],
    });

    setData(
      instance,
      'lidar',
      lidarPoints(
        showRawLidar ? lidar?.raw || lidar?.filtered : lidar?.filtered,
        vessel?.lat,
        vessel?.lon,
        vessel?.heading_deg ?? 0,
      ),
    );

    const vesselFeatures = vessel?.lat
      ? [
          {
            type: 'Feature',
            properties: {
              heading: vessel.heading_deg ?? 0,
              // A vessel whose heading is not trustworthy must not be drawn
              // with a confident arrow.
              headingValid: vessel.heading_valid ? 1 : 0,
            },
            geometry: { type: 'Point', coordinates: [vessel.lon, vessel.lat] },
          },
        ]
      : [];
    setData(instance, 'vessel', { type: 'FeatureCollection', features: vesselFeatures });

    if (follow && vessel?.lat) {
      instance.easeTo({ center: [vessel.lon, vessel.lat], duration: 400 });
    }
    if (!framed.current && plan?.lines?.length) {
      framed.current = true;
      // Open on the whole job. Following is switched off for the same reason
      // the Fit survey button switches it off: centred on the vessel at survey
      // zoom, half the box is off screen, and judging coverage is the thing
      // this map is for. One press of Following vessel gets it back.
      onFollowChange(false);
      // Instant, not animated: the follow ease runs on every vessel sample and
      // cancels an in-flight fitBounds, leaving the survey a smudge.
      fitSurvey(instance, plan, coverage, 0);
    }
  }, [ready, vessel, track, plan, coverage, lidar, follow, showRawLidar, onFollowChange]);

  // -- graticule --------------------------------------------------------

  useEffect(() => {
    const instance = map.current;
    if (!instance || !ready) return;
    if (availableSources(tilesRef.current || {}, seamark, connection.isMock).any) return;
    const redraw = () => setData(instance, 'graticule', graticule(instance.getBounds()));
    redraw();
    instance.on('moveend', redraw);
    return () => instance.off('moveend', redraw);
  }, [ready, styleKey, seamark, connection]);

  // Recomputed each render so the controls agree with what is on screen: the
  // sea-mark toggle is meaningless without a basemap to overlay.
  const sources = availableSources(tiles || {}, seamark, connection.isMock);

  return (
    <div className="map-area">
      <div ref={container} className="map" />
      <div className="map-overlay">
        <button onClick={() => onFollowChange(!follow)}>
          {follow ? 'Following vessel' : 'Follow vessel'}
        </button>
        {/* Judging coverage means seeing the whole box at once. Following the
            vessel keeps you at a zoom where a gap two lines away is off
            screen. */}
        <button
          onClick={() => {
            onFollowChange(false);
            fitSurvey(map.current, plan, coverage);
          }}
          disabled={!plan?.lines?.length}
          title="Zoom out to the whole survey box, so gaps in coverage are visible"
        >
          Fit survey
        </button>
        {/* Off by default, and labelled when on: it is the most useful layer
            here, and it is also volunteer data that lags the real world. */}
        {!connection.isMock && sources.basemap && (
          <button
            onClick={() => setSeamark((on) => !on)}
            aria-pressed={seamark}
            title={
              seamark
                ? 'Hide depth contours and navigation marks'
                : 'Show OpenSeaMap depth contours and navigation marks. Not a navigation authority.'
            }
          >
            {seamark ? 'Sea marks on' : 'Sea marks'}
          </button>
        )}
      </div>
      {/* Five overlays in five colours is four too many to hold in your head at
          06:00. The legend is small, permanent, and uses the same colours the
          layers are painted with — they are declared once, below. */}
      <div className="map-legend">
        {LEGEND.map((item) => (
          <div key={item.label}>
            <span
              className={`swatch ${item.fill ? 'fill' : ''}`}
              style={{ background: item.color, opacity: item.opacity ?? 1 }}
            />
            {item.label}
          </div>
        ))}
      </div>
      {/* Said once, on the map, rather than stamped across every 256 px tile.
          It still has to be said: a synthetic basemap that looked like a chart
          would be the most dangerous thing this GUI could render. */}
      {connection.isMock && (
        <div className="map-note map-note-mock">
          <strong>Synthetic basemap — not a chart.</strong> Generated in this
          browser so the tiled rendering path can be reviewed with no backend
          and no internet.
        </div>
      )}
      {/* What the map is actually showing, in words. The whole point of the
          source ladder is that "map looks sparse" has several causes which
          need different actions, and the operator cannot tell them apart by
          looking. */}
      {!connection.isMock && <BasemapNote info={tiles} seamark={seamark} />}
      {tiles?.mbtiles?.available && tiles.mbtiles.bounds && vessel?.lat &&
        !tiles.online &&
        !withinBounds(tiles.mbtiles.bounds, vessel.lat, vessel.lon) && (
          <div className="map-note">
            <strong className="warnline">The vessel is outside the offline tiled area.</strong> With
            online tiles switched off the map will be blank here.
          </div>
        )}
    </div>
  );
}

/**
 * Which basemap sources are usable right now.
 *
 * "Online is configured" is not the same as "online works". A backend with
 * `online_tiles: true` and a dead 4G modem and an empty cache can render
 * nothing at all, and if the map believed the configuration it would show a
 * blank white rectangle — the one thing this panel must never do. So the
 * basemap counts as usable only when the tile server has actually answered, or
 * when there is something cached to fall back on.
 */
function availableSources(info, seamark, isMock) {
  // Mock mode has no backend and no internet: the synthetic tiles stand in for
  // the whole ladder. The dev panel can switch them off, and that has to keep
  // working — "the map degrades honestly with no tiles" is a behaviour worth
  // being able to see rather than take on trust.
  if (isMock) {
    const available = Boolean(info?.mbtiles?.available);
    return { any: available, mock: available, basemap: false, seamark: false, mbtiles: false };
  }

  const cached = (info?.cache?.entries || 0) > 0;
  const reachable = info?.upstream?.reachable;
  // reachable === null means "not tried yet": optimistic, because the first
  // request is what finds out, and a pessimistic default would never try.
  const basemap = Boolean(info?.online) && (reachable !== false || cached);
  const mbtiles = Boolean(info?.mbtiles?.available);

  return {
    any: basemap || mbtiles,
    mock: false,
    basemap,
    seamark: basemap && seamark,
    mbtiles,
  };
}

/** A string that changes only when the style would have to be rebuilt. */
function styleSignature(info, seamark, isMock) {
  const s = availableSources(info, seamark, isMock);
  return [s.mock, s.basemap, s.seamark, s.mbtiles].map(Number).join('');
}

function attributionFor(info, id) {
  return (info?.sources || []).find((s) => s.id === id)?.attribution || '';
}

/**
 * What the map is showing, and — when it matters — why it is not showing more.
 *
 * The rule this exists to keep: the map never presents a cached basemap as if
 * it were live. A month-old coastline is not the lie a month-old position would
 * be, but the operator should still be able to tell without having to guess
 * whether the link is down, the profile has suspended fetching, or there is
 * simply no map for this place.
 */
function BasemapNote({ info, seamark }) {
  if (!info) return null;
  if (info.error) {
    return (
      <div className="map-note">
        <strong className="warnline">Cannot reach the backend about map tiles.</strong>{' '}
        {info.error}
      </div>
    );
  }

  const sources = availableSources(info, seamark, false);
  const cache = info.cache || {};
  const upstream = info.upstream || {};

  if (!sources.any) {
    return (
      <div className="map-note">
        <strong className="warnline">No basemap.</strong> Showing a coordinate grid
        only.{' '}
        {info.online
          ? `The tile server is unreachable${upstream.last_error ? ` (${upstream.last_error})` : ''} and nothing is cached yet.`
          : 'Online tiles are switched off, and there is no offline tile file.'}
      </div>
    );
  }

  // Fetching suspended by the link profile. Not a fault — it is the GUI doing
  // what it was asked to — so it is said plainly rather than in alarm colours.
  if (info.online && !info.fetching && info.fetch_suspended_reason) {
    return (
      <div className="map-note">
        <strong>Map paused.</strong> {info.fetch_suspended_reason}
        {cache.newest_utc_ms ? ` Cached ${describeAge(Date.now() - cache.newest_utc_ms)}.` : ''}
      </div>
    );
  }

  // Online, allowed to fetch, and the network is not answering: everything on
  // screen is from the cache and may be old.
  if (info.online && upstream.reachable === false) {
    return (
      <div className="map-note">
        <strong className="warnline">Map is cached, not live.</strong> The tile server
        is unreachable{upstream.last_error ? ` (${upstream.last_error})` : ''}.{' '}
        {cache.entries
          ? `Showing ${cache.entries} stored tiles, newest ${describeAge(Date.now() - (cache.newest_utc_ms || Date.now()))}.`
          : 'Showing the offline tile file.'}
      </div>
    );
  }

  // The offline file, with no live source behind it. It is a real basemap and
  // a perfectly good one, but it is a snapshot taken at some point in the past
  // and it will not show a jetty built since. Saying so costs one line.
  if (!sources.basemap && sources.mbtiles) {
    return (
      <div className="map-note">
        <strong>Offline basemap.</strong>{' '}
        {info.online
          ? 'No tile server reachable, so the map is the pre-downloaded file — '
          : 'Online tiles are switched off, so the map is the pre-downloaded file — '}
        a snapshot, not a live map. {info.mbtiles?.message || ''}
      </div>
    );
  }

  if (seamark) {
    return (
      <div className="map-note">
        <strong>Sea marks shown.</strong> OpenSeaMap depth contours and navigation
        marks. Volunteer-maintained, lags the real world, and is{' '}
        <strong>not a navigation authority</strong> — do not plan a survey line from it.
      </div>
    );
  }

  return null;
}

/** "3 minutes ago", for somebody deciding whether to trust what they see. */
function describeAge(ms) {
  if (!Number.isFinite(ms) || ms < 0) return 'just now';
  const minutes = Math.round(ms / 60000);
  if (minutes < 1) return 'just now';
  if (minutes < 60) return `${minutes} min ago`;
  const hours = Math.round(minutes / 60);
  if (hours < 48) return `${hours} h ago`;
  return `${Math.round(hours / 24)} days ago`;
}

/** Zoom to the planned survey box plus whatever has been covered so far. */
function fitSurvey(map, plan, coverage, duration = 600) {
  if (!map || !plan?.lines?.length) return;
  const points = plan.lines.flatMap((line) => line.coords);
  points.push(...(plan.geofence || []));
  for (const segment of coverage?.segments || []) {
    if (segment) points.push([segment[1], segment[0]]);
  }
  if (!points.length) return;

  const lons = points.map((p) => p[0]);
  const lats = points.map((p) => p[1]);
  map.fitBounds(
    [
      [Math.min(...lons), Math.min(...lats)],
      [Math.max(...lons), Math.max(...lats)],
    ],
    // maxZoom keeps the camera inside the tile set's own range. A short survey
    // otherwise frames to a zoom past the deepest tile available, and MapLibre
    // upscales — a blurred basemap that looks like a rendering fault rather
    // than the edge of the data.
    { padding: 48, duration, maxZoom: 18 },
  );
}

function withinBounds(bounds, lat, lon) {
  const [west, south, east, north] = bounds;
  return lon >= west && lon <= east && lat >= south && lat <= north;
}

function setData(map, id, data) {
  const source = map.getSource(id);
  if (source) source.setData(data);
}

function empty() {
  return { type: 'FeatureCollection', features: [] };
}

//: The map's palette, declared once. `addLayers` paints from it and the legend
//: reads from it, so a colour cannot be changed in one place and not the other.
const COLOURS = {
  coverage: '#00a651',
  plan: '#0b57d0',
  geofence: '#8f3f00',
  track: '#000000',
  lidar: '#d34500',
  graticule: '#aeaeae',
};

const LEGEND = [
  { label: 'Covered', color: COLOURS.coverage, fill: true, opacity: 0.42 },
  { label: 'Planned line', color: COLOURS.plan },
  { label: 'Track', color: COLOURS.track },
  { label: 'Geofence', color: COLOURS.geofence },
  { label: 'Obstacle', color: COLOURS.lidar, fill: true },
];

function addLayers(map, withGraticule) {
  for (const id of ['graticule', 'geofence', 'plan', 'coverage', 'track', 'lidar', 'vessel']) {
    map.addSource(id, { type: 'geojson', data: empty() });
  }

  if (withGraticule) {
    map.addLayer({
      id: 'graticule',
      type: 'line',
      source: 'graticule',
      // Grey on purpose: the graticule is a reference frame, not data, and it
      // must not compete with the track drawn on top of it.
      paint: { 'line-color': COLOURS.graticule, 'line-width': 1 },
    });
  }

  // Coverage sits under everything else, but it is not background: with a
  // single sonar covering one side only, spotting a gap is the whole reason
  // this map exists. On white it has to be a saturated green rather than a
  // pale wash — and translucent enough that overlapping passes darken into a
  // deeper band, which is how an operator spots a line flown twice.
  map.addLayer({
    id: 'coverage',
    type: 'fill',
    source: 'coverage',
    paint: { 'fill-color': COLOURS.coverage, 'fill-opacity': 0.42 },
  });

  map.addLayer({
    id: 'plan',
    type: 'line',
    source: 'plan',
    paint: { 'line-color': COLOURS.plan, 'line-width': 2, 'line-dasharray': [3, 2] },
  });

  map.addLayer({
    id: 'geofence',
    type: 'line',
    source: 'geofence',
    // Thicker and more saturated than the planned lines: this is the boundary
    // the vessel must not cross, and a thin brown dotted line on white was the
    // quietest thing on the map. A long dash rather than a dot, so it reads as
    // a boundary at a glance and still cannot be mistaken for a survey line.
    //
    // Deliberately not red. Red means something is wrong here, and a boundary
    // sitting exactly where it has always sat is not something being wrong.
    paint: { 'line-color': COLOURS.geofence, 'line-width': 3, 'line-dasharray': [4, 2.5] },
  });

  map.addLayer({
    id: 'track',
    type: 'line',
    source: 'track',
    // Where the boat actually went, in black: it must read over coverage,
    // over the plan, and over the graticule, at any zoom.
    paint: { 'line-color': COLOURS.track, 'line-width': 1.6 },
  });

  map.addLayer({
    id: 'lidar',
    type: 'circle',
    source: 'lidar',
    paint: { 'circle-radius': 2.5, 'circle-color': COLOURS.lidar },
  });

  map.addLayer({
    id: 'vessel-halo',
    type: 'circle',
    source: 'vessel',
    paint: { 'circle-radius': 9, 'circle-color': COLOURS.plan, 'circle-opacity': 0.2 },
  });

  // The heading arrow is drawn as a triangle rotated by heading. When heading
  // is invalid it turns red and stops rotating — pointing an arrow confidently
  // in a direction we do not trust is exactly the lie this GUI must not tell.
  map.addLayer({
    id: 'vessel',
    type: 'symbol',
    source: 'vessel',
    layout: {
      'icon-image': 'vessel-arrow',
      'icon-rotate': ['case', ['==', ['get', 'headingValid'], 1], ['get', 'heading'], 0],
      'icon-rotation-alignment': 'map',
      'icon-allow-overlap': true,
      'icon-size': 1,
    },
  });

  map.addImage('vessel-arrow', arrowImage(), { pixelRatio: 2 });
}

/** A 32x32 arrow, generated so nothing has to be fetched. */
function arrowImage() {
  const size = 32;
  const data = new Uint8ClampedArray(size * size * 4);
  const put = (x, y, r, g, b, a) => {
    const i = (y * size + x) * 4;
    data[i] = r;
    data[i + 1] = g;
    data[i + 2] = b;
    data[i + 3] = a;
  };
  // Triangle pointing up (north at rotation 0).
  for (let y = 4; y < 28; y += 1) {
    const halfWidth = ((y - 4) / 24) * 9;
    for (let x = Math.round(16 - halfWidth); x <= Math.round(16 + halfWidth); x += 1) {
      const edge = Math.abs(x - 16) > halfWidth - 2 || y > 25;
      // Black outline, blue body — legible on white, on the green coverage
      // ribbon, and on a satellite tile alike.
      put(x, y, edge ? 0 : 11, edge ? 0 : 87, edge ? 0 : 208, 255);
    }
  }
  return { width: size, height: size, data };
}
