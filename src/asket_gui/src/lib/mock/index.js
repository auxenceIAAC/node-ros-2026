// Mock mode entry point.
//
// Builds a `Connection` whose socket is a `MockTransport` instead of a
// WebSocket. Everything above the socket is the real client, so what you are
// reviewing in mock mode is the actual interface — not a second implementation
// that happens to look like it.

import { Connection } from '../connection.js';
import { MockTransport } from './mockTransport.js';
import { MockWorld } from './world.js';

export { MockWorld } from './world.js';
export { SCENARIOS } from './scenarios.js';

/** Synthetic raster tiles, drawn in the browser. */
export const MOCK_TILE_PROTOCOL = 'mocktiles';

export function createMockConnection(options = {}) {
  const world = new MockWorld(options.world);
  let transport = null;

  const connection = new Connection('mock://asket', {
    isMock: true,
    transport: () => {
      transport = new MockTransport(world, { timeScale: options.timeScale ?? 1 });
      return transport;
    },
    // No backend to ask, so the answer is supplied. `tilesAvailable` is
    // switchable from the dev panel, because "the map degrades honestly with no
    // tiles" is a behaviour worth being able to see rather than take on trust.
    tileInfo: () =>
      Promise.resolve(
        world.mockTilesAvailable
          ? {
              // The same shape the backend returns, so the map panel has one
              // contract rather than two. Mock mode has no backend and no
              // internet, so it reports itself as offline with a local source.
              online: false,
              fetching: false,
              fetch_suspended_reason: '',
              profile: 'full',
              sources: [],
              upstream: { reachable: null, last_error: '', fetches: 0 },
              cache: { enabled: false, entries: 0, bytes: 0 },
              mbtiles: {
                available: true,
                name: 'Synthetic test tiles',
                format: 'png',
                bounds: null,
                message:
                  'Synthetic tiles generated in the browser. Not a real chart — they ' +
                  'exist so the tiled path can be reviewed without a backend.',
              },
            }
          : {
              online: false,
              fetching: false,
              fetch_suspended_reason: '',
              profile: 'full',
              sources: [],
              upstream: { reachable: null, last_error: '', fetches: 0 },
              cache: { enabled: false, entries: 0, bytes: 0 },
              mbtiles: {
                available: false,
                message:
                  'No basemap. The map shows a coordinate grid only. In the field ' +
                  'this is what you get with no link, no cache and no .mbtiles file.',
              },
            },
      ),
  });

  // The dev panel drives the world directly; the transport is exposed so it can
  // force a profile without going through the operator-facing controls.
  connection.mock = {
    world,
    get transport() {
      return transport;
    },
  };
  return connection;
}
