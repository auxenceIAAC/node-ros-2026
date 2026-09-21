// MapLibre styles, built here rather than fetched.
//
// Every URL in these styles points at our own backend, which proxies and caches
// upstream (see gui_backend/core/tiles.py). Nothing is fetched from a third
// party by the browser directly, so there is one cache, one User-Agent, and one
// place that honours conditional requests — and the map keeps working on a
// laptop that has no internet of its own.
//
// WHY THE BASEMAP IS GREY
// =======================
//
// This map's job is not to show streets. It is to show a survey: coverage,
// planned lines, the geofence, the track, obstacles, the vessel. Those are
// drawn in saturated green, blue, brown, black and orange, and a normal
// basemap — green parks, blue water, yellow arterials — competes with every one
// of them.
//
// So land is near-white, water is a desaturated blue-grey deliberately far from
// the plan's blue, and woodland is grey rather than green because coverage is
// green. The basemap recedes; the survey is the loudest thing on screen. The
// one exception is the coastline, which gets a definite stroke: for a boat it
// is the most useful line on the map.
//
// If there are no tiles at all the map draws a coordinate graticule instead. It
// never silently shows a blank rectangle.

/** Muted palette, chosen against the overlay colours in MissionMap.jsx. */
const LAND = '#f7f7f5';
const LANDCOVER = '#efeeea';
const LANDUSE = '#f3f2ef';
const BUILDING = '#e9e7e2';
const WATER = '#dce4ea';
const COASTLINE = '#9fb0bc';
const ROAD_MINOR = '#ffffff';
const ROAD_MAJOR = '#f0eeea';
const ROAD_CASING = '#e2dfd9';
const BOUNDARY = '#cfcbc4';
const LABEL = '#4a4a4a';
const LABEL_HALO = '#ffffff';
const WATER_LABEL = '#6d8494';

/** The font stack. One stack, so only one set of glyph ranges is ever fetched. */
const FONT = ['Noto Sans Regular'];

/**
 * The whole style, assembled from whichever sources are actually available.
 *
 * @param {object} opts
 * @param {string} opts.origin       where the backend lives
 * @param {boolean} opts.basemap     serve the online vector basemap
 * @param {boolean} opts.seamark     add the OpenSeaMap overlay
 * @param {boolean} opts.mbtiles     an offline .mbtiles file is present
 * @param {string}  opts.mockTileUrl mock-mode synthetic raster protocol, if any
 * @param {string}  opts.basemapAttribution  licence text from the backend
 * @param {string}  opts.seamarkAttribution
 */
export function buildStyle({
  origin,
  basemap = false,
  seamark = false,
  mbtiles = false,
  mockTileUrl = '',
  basemapAttribution = '',
  seamarkAttribution = '',
} = {}) {
  const base = origin || (typeof window !== 'undefined' ? window.location.origin : '');
  const sources = {};
  const layers = [];

  // The vector basemap needs glyphs for its labels. Pointing at our proxy
  // means they are cached with everything else: without them MapLibre draws
  // the geometry and no place names, which is most of what makes a basemap
  // useful for working out where you are.
  const glyphs = `${base}/tiles/fonts/{fontstack}/{range}.pbf`;

  // Land colour. Omitted when an offline raster is underneath, or it would
  // paint over it.
  if (!mbtiles && !mockTileUrl) {
    layers.push({
      id: 'background',
      type: 'background',
      paint: { 'background-color': LAND },
    });
  }

  // The offline .mbtiles file, and the mock-mode synthetic tiles, are raster
  // and sit at the bottom. Where the vector data above them is missing they
  // show through, which is the point: a hole in one source is covered by the
  // other rather than leaving a blank square.
  if (mockTileUrl) {
    sources.mock = {
      type: 'raster', tiles: [mockTileUrl], tileSize: 256, minzoom: 0, maxzoom: 20,
    };
    layers.push({ id: 'mock', type: 'raster', source: 'mock' });
  } else if (mbtiles) {
    sources.offline = {
      type: 'raster',
      tiles: [`${base}/tiles/{z}/{x}/{y}.png`],
      tileSize: 256,
      minzoom: 0,
      maxzoom: 20,
      attribution: '© OpenStreetMap contributors',
    };
    layers.push({ id: 'offline', type: 'raster', source: 'offline' });
  }

  if (basemap) {
    sources.openmaptiles = {
      type: 'vector',
      tiles: [`${base}/tiles/source/basemap/{z}/{x}/{y}.pbf`],
      minzoom: 0,
      // OpenFreeMap's planet build stops at 14 and MapLibre overzooms above it.
      // That is the efficiency of vector tiles: one tile serves every zoom
      // above its own, so a survey box costs about twenty tiles in total.
      maxzoom: 14,
      attribution: basemapAttribution || 'Data from © OpenStreetMap contributors',
    };
    layers.push(...basemapLayers());
  }

  // Sea marks last, so depth contours and buoys sit above the land but below
  // everything the survey itself draws.
  if (seamark) {
    sources.seamark = {
      type: 'raster',
      tiles: [`${base}/tiles/source/seamark/{z}/{x}/{y}.png`],
      tileSize: 256,
      minzoom: 0,
      maxzoom: 18,
      attribution: seamarkAttribution || '© OpenSeaMap contributors',
    };
    layers.push({
      id: 'seamark',
      type: 'raster',
      source: 'seamark',
      paint: { 'raster-opacity': 0.9 },
    });
  }

  return { version: 8, glyphs, sources, layers };
}

/**
 * The basemap itself, against the OpenMapTiles schema.
 *
 * Kept deliberately short. Every layer is one an operator would miss if it were
 * gone; nothing is here because a general-purpose map would have it.
 */
function basemapLayers() {
  return [
    {
      id: 'landcover',
      type: 'fill',
      source: 'openmaptiles',
      'source-layer': 'landcover',
      paint: { 'fill-color': LANDCOVER, 'fill-opacity': 0.7 },
    },
    {
      id: 'landuse',
      type: 'fill',
      source: 'openmaptiles',
      'source-layer': 'landuse',
      paint: { 'fill-color': LANDUSE },
    },
    {
      id: 'water',
      type: 'fill',
      source: 'openmaptiles',
      'source-layer': 'water',
      // Intermittent water is drawn fainter: a salt pan that is dry for most of
      // the year should not read as somewhere the boat can go.
      paint: {
        'fill-color': WATER,
        'fill-opacity': ['case', ['==', ['get', 'intermittent'], 1], 0.45, 1],
      },
    },
    {
      // The coastline. The most useful line on the map for a boat, so it is the
      // one part of the basemap allowed to be definite.
      id: 'water-outline',
      type: 'line',
      source: 'openmaptiles',
      'source-layer': 'water',
      paint: {
        'line-color': COASTLINE,
        'line-width': ['interpolate', ['linear'], ['zoom'], 6, 0.5, 12, 1.1, 16, 1.6],
      },
    },
    {
      id: 'waterway',
      type: 'line',
      source: 'openmaptiles',
      'source-layer': 'waterway',
      paint: {
        'line-color': COASTLINE,
        'line-width': ['interpolate', ['linear'], ['zoom'], 8, 0.4, 16, 1.4],
      },
    },
    {
      id: 'building',
      type: 'fill',
      source: 'openmaptiles',
      'source-layer': 'building',
      minzoom: 13,
      paint: { 'fill-color': BUILDING },
    },
    {
      // Road casing, so roads read as lines rather than as gaps in the land.
      id: 'road-casing',
      type: 'line',
      source: 'openmaptiles',
      'source-layer': 'transportation',
      minzoom: 11,
      filter: ['!in', 'class', 'ferry', 'path', 'track'],
      layout: { 'line-cap': 'round', 'line-join': 'round' },
      paint: {
        'line-color': ROAD_CASING,
        'line-width': ['interpolate', ['linear'], ['zoom'], 11, 1.2, 16, 5],
      },
    },
    {
      id: 'road',
      type: 'line',
      source: 'openmaptiles',
      'source-layer': 'transportation',
      minzoom: 11,
      filter: ['!in', 'class', 'ferry', 'path', 'track'],
      layout: { 'line-cap': 'round', 'line-join': 'round' },
      paint: {
        'line-color': [
          'match', ['get', 'class'],
          ['motorway', 'trunk', 'primary'], ROAD_MAJOR,
          ROAD_MINOR,
        ],
        'line-width': ['interpolate', ['linear'], ['zoom'], 11, 0.6, 16, 3.5],
      },
    },
    {
      id: 'boundary',
      type: 'line',
      source: 'openmaptiles',
      'source-layer': 'boundary',
      filter: ['<=', 'admin_level', 4],
      paint: {
        'line-color': BOUNDARY,
        'line-width': 0.8,
        'line-dasharray': [3, 2],
      },
    },
    {
      // Place names are the reason to have a basemap at all: they turn a shape
      // into a location somebody can say out loud over a radio.
      id: 'place-label',
      type: 'symbol',
      source: 'openmaptiles',
      'source-layer': 'place',
      filter: ['in', 'class', 'city', 'town', 'village', 'suburb', 'hamlet'],
      layout: {
        'text-field': ['get', 'name'],
        'text-font': FONT,
        'text-size': [
          'match', ['get', 'class'],
          'city', 13,
          'town', 11.5,
          10,
        ],
        'text-max-width': 8,
        'text-padding': 4,
      },
      paint: {
        'text-color': LABEL,
        'text-halo-color': LABEL_HALO,
        'text-halo-width': 1.4,
      },
    },
    {
      id: 'water-label',
      type: 'symbol',
      source: 'openmaptiles',
      'source-layer': 'water_name',
      layout: {
        'text-field': ['get', 'name'],
        'text-font': FONT,
        'text-size': 11,
        'text-max-width': 8,
        'symbol-placement': 'point',
      },
      paint: {
        'text-color': WATER_LABEL,
        'text-halo-color': LABEL_HALO,
        'text-halo-width': 1.2,
      },
    },
  ];
}

/** Nothing but a background. The graticule is drawn over it by MissionMap. */
export function blankStyle() {
  return {
    version: 8,
    sources: {},
    layers: [
      { id: 'background', type: 'background', paint: { 'background-color': '#ffffff' } },
    ],
  };
}

/**
 * A latitude/longitude graticule as GeoJSON.
 *
 * Drawn when there are no tiles. Spacing adapts to the view so the grid stays
 * readable from a whole survey box down to a single line.
 */
export function graticule(bounds, targetLines = 8) {
  const west = bounds.getWest();
  const east = bounds.getEast();
  const south = bounds.getSouth();
  const north = bounds.getNorth();

  const step = niceStep(Math.max(east - west, north - south) / targetLines);
  const features = [];

  for (let lon = Math.ceil(west / step) * step; lon <= east; lon += step) {
    features.push({
      type: 'Feature',
      properties: { label: `${lon.toFixed(decimalsFor(step))}°` },
      geometry: { type: 'LineString', coordinates: [[lon, south], [lon, north]] },
    });
  }
  for (let lat = Math.ceil(south / step) * step; lat <= north; lat += step) {
    features.push({
      type: 'Feature',
      properties: { label: `${lat.toFixed(decimalsFor(step))}°` },
      geometry: { type: 'LineString', coordinates: [[west, lat], [east, lat]] },
    });
  }
  return { type: 'FeatureCollection', features };
}

function niceStep(raw) {
  const magnitude = 10 ** Math.floor(Math.log10(raw));
  for (const multiple of [1, 2, 5, 10]) {
    if (raw <= magnitude * multiple) return magnitude * multiple;
  }
  return magnitude * 10;
}

function decimalsFor(step) {
  return Math.max(0, Math.min(6, Math.ceil(-Math.log10(step)) + 1));
}
