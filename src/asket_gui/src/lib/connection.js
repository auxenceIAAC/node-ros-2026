// The WebSocket client and the store behind it.
//
// Three responsibilities, and one of them is a safety rule.
//
// 1. Reconnect. The link will drop. Reconnection is automatic with backoff, and
//    while it is down the UI keeps showing the last values it had — clearly
//    marked as old. Blanking the screen would hide the last known position at
//    exactly the moment it becomes most valuable.
//
// 2. Estimate clock skew, so data age is honest (safety rule 7). Every frame
//    carries `source_utc_ms` (when the value was produced) and `server_utc_ms`
//    (when it was sent). Age is computed against the *server's* clock, not the
//    laptop's, because a laptop with a wrong clock would otherwise render every
//    value as fresh or as hours old, and an operator would have no way to tell.
//
//    Skew is estimated as max(server_utc_ms - local receive time) over a recent
//    window. Transmission delay only ever makes a frame look older, so the
//    maximum over several samples is the sample that suffered the least delay,
//    and therefore the best estimate of true skew.
//
// 3. Never fabricate. A stream with no data has no data; it does not have zero.
//
// The socket itself is pluggable. `transport` defaults to a real WebSocket; the
// mock mode supplies one that speaks the same protocol from inside the browser.
// Everything above the socket — accumulation of the append-only streams, clock
// skew, the command lifecycle, reconnection — is therefore shared, and the mock
// exercises the real client rather than a parallel one.

const RECONNECT_BACKOFF_MS = [500, 1000, 2000, 4000, 8000, 15000];
const SKEW_WINDOW = 32;

// Notices that a single incoming frame disproves.
const SILENCE_NOTICES = new Set(['no_streams', 'nothing_emitted', 'tick_failed']);

function initialState() {
  return {
    connected: false,
    connecting: true,
    attempts: 0,
    lastError: null,
    hello: null,
    profile: { profile: 'full', manual: false, reason: 'connecting' },
    subscriptions: {},
    estimatedBytesPerS: 0,
    streams: {},
    // The camera, which does not live in `streams` because its payload is a
    // blob URL rather than a JSON value, and because its age has to survive
    // the frames stopping.
    camera: null,
    alarms: [],
    commands: {},
    skewMs: 0,
    lastMessageAt: null,
    // When the server says it is serving us nothing, and why. The page can
    // work out that data is not arriving on its own; it cannot work out that
    // the link profile refused every stream, and that is the half that sends
    // you to the right place.
    notice: null,
    // When the first `data` frame arrived on this socket. Null while a socket
    // has been open but has never carried a payload — which is the state this
    // GUI spent an hour in on its first real deployment, indistinguishable
    // from a quiet vessel.
    firstDataAt: null,
    // Bumped when the answer to "are there offline tiles" changes, so the map
    // re-asks. Only the mock has cause to change it.
    tileGeneration: 0,
    // Bumped four times a second so that the store snapshot changes even when
    // no data is arriving. Without it, useSyncExternalStore sees the same
    // object reference, skips the render, and every "N s ago" label freezes at
    // whatever it last showed — exactly when the link has dropped and a growing
    // age is the single most important thing on the screen (safety rule 7).
    tick: 0,
  };
}

export class Connection {
  constructor(url, { transport, isMock = false, tileInfo = null } = {}) {
    this.url =
      url ||
      `${window.location.protocol === 'https:' ? 'wss' : 'ws'}://${window.location.host}/ws`;
    this.openTransport = transport || ((target) => new WebSocket(target));
    //: True only in mock mode. The dev control panel keys off this, and it is
    //: the one thing components are allowed to branch on.
    this.isMock = isMock;
    //: In mock mode there is no backend to answer /api/tiles/info, so the
    //: answer is supplied instead of fetched. Keeping this on the connection
    //: means the map component asks one place in both modes.
    this.tileInfo = tileInfo;
    this.state = initialState();
    this.listeners = new Set();
    this.desired = [];
    this.skewSamples = [];
    this.ws = null;
    this.reconnectTimer = null;
    this.closed = false;
    this.tickTimer = setInterval(() => this.#set({ tick: this.state.tick + 1 }), 250);
  }

  // -- React glue -------------------------------------------------------

  subscribeToStore = (listener) => {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  };

  getSnapshot = () => this.state;

  #set(partial) {
    this.state = { ...this.state, ...partial };
    this.#emit();
  }

  #emit() {
    for (const listener of this.listeners) listener();
  }

  // -- lifecycle --------------------------------------------------------

  connect() {
    this.closed = false;
    this.#open();
  }

  close() {
    this.closed = true;
    clearInterval(this.tickTimer);
    clearTimeout(this.reconnectTimer);
    if (this.ws) this.ws.close();
  }

  #open() {
    this.#set({ connecting: true });
    let ws;
    try {
      ws = this.openTransport(this.url);
    } catch (error) {
      this.#scheduleReconnect(error.message);
      return;
    }
    this.ws = ws;

    ws.onopen = () => {
      this.#set({
        connected: true, connecting: false, attempts: 0, lastError: null,
        firstDataAt: null, notice: null,
      });
      if (this.desired.length) this.subscribe(this.desired);
    };

    ws.onmessage = (event) => {
      let message;
      try {
        message = JSON.parse(event.data);
      } catch {
        return; // a malformed frame is not worth tearing the session down for
      }
      this.#handle(message);
    };

    ws.onerror = () => {
      // onclose always follows; recording the reason there avoids reporting
      // the same drop twice.
    };

    ws.onclose = () => {
      this.#set({ connected: false });
      this.#scheduleReconnect('connection closed');
    };
  }

  #scheduleReconnect(reason) {
    if (this.closed) return;
    const attempts = this.state.attempts + 1;
    const delay = RECONNECT_BACKOFF_MS[Math.min(attempts - 1, RECONNECT_BACKOFF_MS.length - 1)];
    this.#set({ connected: false, connecting: true, attempts, lastError: reason });
    clearTimeout(this.reconnectTimer);
    this.reconnectTimer = setTimeout(() => this.#open(), delay);
  }

  // -- inbound ----------------------------------------------------------

  #handle(message) {
    const now = Date.now();
    this.state.lastMessageAt = now;

    if (typeof message.server_utc_ms === 'number') {
      this.skewSamples.push(message.server_utc_ms - now);
      if (this.skewSamples.length > SKEW_WINDOW) this.skewSamples.shift();
      this.state.skewMs = Math.max(...this.skewSamples);
    }

    switch (message.type) {
      case 'hello':
        this.#set({ hello: message, profile: message.profile });
        break;

      case 'subscribed': {
        const subscriptions = { ...this.state.subscriptions };
        for (const entry of message.streams || []) subscriptions[entry.name] = entry;
        this.#set({
          subscriptions,
          profile: message.profile || this.state.profile,
          estimatedBytesPerS: message.estimated_bytes_per_s ?? 0,
        });
        break;
      }

      case 'profile':
        this.#set({
          profile: {
            profile: message.profile,
            manual: message.manual,
            reason: message.reason,
          },
        });
        break;

      case 'data': {
        const streams = { ...this.state.streams };
        streams[message.stream] = {
          // Append-only streams (track, coverage) arrive as increments and are
          // accumulated here; everything else carries a complete current value
          // and simply replaces the last one.
          payload: this.#accumulate(message),
          sourceUtcMs: message.source_utc_ms,
          serverUtcMs: message.server_utc_ms,
          detail: message.detail,
          receivedAt: now,
        };
        this.#set({
          streams,
          firstDataAt: this.state.firstDataAt ?? now,
          // A frame arriving disproves every "you are receiving nothing"
          // notice, so they clear here rather than waiting to be contradicted.
          // The server re-broadcasts `tick_failed` on a schedule, so if the
          // fault is ongoing the banner comes straight back — which is better
          // than one that sticks after the problem has passed and teaches the
          // crew to ignore the banner.
          notice: SILENCE_NOTICES.has(this.state.notice?.code)
            ? null
            : this.state.notice,
        });
        break;
      }

      case 'alarms':
        this.#set({ alarms: message.active || [] });
        break;

      case 'notice':
        this.#set({ notice: message });
        break;

      case 'command_result': {
        const commands = { ...this.state.commands };
        commands[message.id] = message;
        this.#set({ commands });
        break;
      }

      case 'ping':
        // Answer immediately so the server's round-trip measurement — which is
        // what automatic profile selection runs on — reflects the link and not
        // this tab's render loop.
        this.send({ type: 'pong', id: message.id });
        break;

      default:
        break;
    }
  }

  /**
   * Splice an increment onto what we already have.
   *
   * `from` says where the increment starts. If it starts beyond what we hold,
   * we have missed something — a dropped frame, or a reconnect — and the
   * accumulated history would be wrong. Rather than splice a hole and draw a
   * coverage ribbon that claims seabed nobody ensonified, the stream is reset
   * and a full resync requested. A visibly short history is recoverable; a
   * silently wrong one is not.
   */
  #accumulate(message) {
    const incoming = message.payload;
    if (!incoming || incoming.from === undefined) return incoming;

    const key = incoming.segments !== undefined ? 'segments' : 'points';
    const previous = this.state.streams[message.stream]?.payload;
    const held = previous?.[key] ?? [];

    if (incoming.from === 0) {
      return { ...incoming, [key]: incoming[key] };
    }
    if (incoming.from > held.length) {
      this.#resync(message.stream);
      return previous ?? { ...incoming, [key]: [] };
    }
    return {
      ...incoming,
      [key]: held.slice(0, incoming.from).concat(incoming[key]),
    };
  }

  #resync(stream) {
    const request = this.desired.find((s) => s.name === stream);
    if (!request) return;
    // Re-subscribing resets the server-side cursor, so the next frame is the
    // whole history rather than another increment we cannot place.
    this.send({ type: 'subscribe', streams: [request] });
  }

  // -- outbound ---------------------------------------------------------

  send(message) {
    if (this.ws && this.ws.readyState === WebSocket.OPEN) {
      this.ws.send(JSON.stringify(message));
      return true;
    }
    return false;
  }

  subscribe(streams) {
    this.desired = streams;
    return this.send({ type: 'subscribe', streams });
  }

  unsubscribe(names) {
    this.desired = this.desired.filter((s) => !names.includes(s.name));
    return this.send({ type: 'unsubscribe', streams: names });
  }

  setProfile(profile) {
    return this.send({ type: 'set_profile', profile });
  }

  command(name, args = {}) {
    const id = `${name}-${Date.now().toString(36)}`;
    const commands = { ...this.state.commands };
    // Recorded locally as pending so the button can show it immediately. This
    // is NOT the vessel's state — nothing displayed as vessel state changes
    // until the server reports a confirmation (safety rule 4).
    commands[id] = { id, name, args, status: 'pending', detail: 'sending…' };
    this.#set({ commands });
    if (!this.send({ type: 'command', id, name, args })) {
      commands[id] = { ...commands[id], status: 'failed', detail: 'no link to the vessel' };
      this.#set({ commands: { ...commands } });
    }
    return id;
  }

  // -- derived ----------------------------------------------------------

  // The server's current time, as best we can estimate it.
  serverNow() {
    return Date.now() + this.state.skewMs;
  }

  /**
   * Whether there is a usable offline map, and if not, why not.
   *
   * Asked of the connection rather than fetched directly by the map, so that
   * the map component is identical in both modes: in mock mode there is no
   * backend to answer, and a failed fetch would otherwise render as
   * "could not ask the backend", which is true but useless.
   */
  /** Tell the map to re-ask whether offline tiles are available. */
  notifyTilesChanged() {
    this.#set({ tileGeneration: this.state.tileGeneration + 1 });
  }

  async fetchTileInfo() {
    if (this.tileInfo) return this.tileInfo();
    try {
      const response = await fetch('/api/tiles/info');
      return await response.json();
    } catch {
      return {
        available: false,
        message: 'Could not ask the backend about map tiles.',
      };
    }
  }

  /**
   * One camera frame, or a header explaining why there is not one.
   *
   * `[4 bytes big-endian header length][header JSON][image bytes]` — the
   * format is defined once, in `gui_backend/core/video_frame.py`, and this is
   * the only place the browser reads it.
   *
   * The object URL is the part worth being careful about. Every frame
   * allocates one, and at 15 fps an unrevoked URL leaks a 45 kB blob fifteen
   * times a second until the tab closes. The previous frame's URL is revoked
   * here rather than in the panel, because the panel can unmount while frames
   * are still arriving and a leak that only shows up when somebody collapses a
   * panel is a leak nobody finds.
   */
  #handleVideo(buffer) {
    let header;
    let image;
    try {
      const bytes = new Uint8Array(buffer);
      const length = new DataView(buffer).getUint32(0, false);
      if (length > 8192 || bytes.length < 4 + length) return;
      header = JSON.parse(new TextDecoder().decode(bytes.subarray(4, 4 + length)));
      image = bytes.subarray(4 + length);
    } catch {
      return;
    }

    const previous = this.state.camera;
    const now = Date.now();

    if (header.type === 'camera_frame' && image.length > 0) {
      if (previous?.url) URL.revokeObjectURL(previous.url);
      this.#set({
        camera: {
          url: URL.createObjectURL(
            new Blob([image], { type: `image/${header.format}` }),
          ),
          seq: header.seq,
          // When the shutter fell, never when this arrived. A frame that spent
          // four seconds in a queue has to read as four seconds old.
          sourceUtcMs: header.source_utc_ms,
          width: header.width,
          height: header.height,
          sizeBytes: header.size_bytes,
          rateHz: header.rate_hz,
          reason: 'live',
          statusAt: now,
        },
      });
      return;
    }

    // A status frame: the vessel is alive and the camera is not. Whatever
    // picture we already have is KEPT — clearly labelled, it is what the
    // operator may choose to look at — but `reason` changes and `sourceUtcMs`
    // does not, so it goes on ageing from when it was taken.
    //
    // `statusAt` is what makes the diagnosis possible. While these keep
    // arriving the link is up and the camera is the problem; when they stop,
    // the link is. One timestamp could not tell those apart.
    this.#set({
      camera: {
        ...(previous ?? { url: null, seq: null, sourceUtcMs: null }),
        reason: header.reason,
        rateHz: header.rate_hz,
        framesProduced: header.frames_produced,
        lastFrameUtcMs: header.last_frame_utc_ms,
        statusAt: now,
      },
    });
  }
}

// Age of a stream's value, in milliseconds, or null if it has never arrived.
export function streamAgeMs(state, name) {
  const entry = state.streams[name];
  if (!entry) return null;
  return Date.now() + state.skewMs - entry.sourceUtcMs;
}

export function streamPayload(state, name) {
  return state.streams[name]?.payload ?? null;
}

/**
 * Age of the current camera picture in milliseconds, or null if there has
 * never been one.
 *
 * Not `streamAgeMs('camera')`: this stream is not in `state.streams`, and its
 * age has to keep climbing after the frames have stopped.
 */
export function cameraAgeMs(state) {
  const camera = state.camera;
  if (!camera?.sourceUtcMs) return null;
  return Date.now() + state.skewMs - camera.sourceUtcMs;
}

/**
 * Milliseconds since the backend last said anything about the camera at all.
 *
 * The other half of the diagnosis. A picture going stale while this stays
 * small means the vessel is talking and the camera is not; both going stale
 * together means the link has gone. Different problems, different places to
 * go, and from the shore they look identical without this.
 */
export function cameraSilenceMs(state) {
  const at = state.camera?.statusAt;
  return at ? Date.now() - at : null;
}
