/**
 * A camera, in the browser.
 *
 * The mirror of `asket_sim/core/camera.py`, drawn on a canvas instead of
 * written out as PNG bytes by hand. Same scene, same burned-in clock, same
 * three states, and — importantly — the same binary frame format, so
 * `dev:mock` exercises the real decoding path in `connection.js` rather than a
 * shortcut around it.
 *
 * That last point is the whole reason this file is not simply a static image.
 * The camera panel's job is to refuse to show a stale picture, and the code
 * that decides "stale" reads a header off a binary frame. A mock that handed
 * the panel a URL directly would leave that code — the part that can actually
 * be wrong — unexercised in the only environment where anybody reviews this
 * interface.
 *
 * **The burned-in clock matters more here than anywhere.** A panel that has
 * correctly detected a frozen feed and a panel that has itself stopped
 * re-rendering produce the same still image. Freeze the camera from the dev
 * panel: the number in the corner of the picture must stop while the age
 * beside it climbs. If both stop, the panel is the thing that is broken.
 *
 * Node has no canvas, so `frame()` returns null there. The cross-language
 * checks compare headers, which need no pixels.
 */

export const NATIVE_WIDTH = 640;
export const NATIVE_HEIGHT = 480;

// Mirrors Intrinsics in asket_sim/core/camera.py, itself transcribed from
// bringup/config/front_camera.yaml. Projection has to match the Python or the
// two simulators would place the same obstacle in different pixels.
const FX = 700.2277305628887;
const FY = 696.4669027105944;
const CX = 294.55943543522966;
const CY = 226.595388208691;

const SKY = 'rgb(150, 170, 190)';
const SEA_FAR = 'rgb(92, 118, 138)';
const SEA_NEAR = 'rgb(44, 74, 96)';
const OBJECT = 'rgb(30, 30, 34)';

function makeCanvas(width, height) {
  if (typeof OffscreenCanvas !== 'undefined') return new OffscreenCanvas(width, height);
  if (typeof document === 'undefined') return null;
  const canvas = document.createElement('canvas');
  canvas.width = width;
  canvas.height = height;
  return canvas;
}

export class MockCamera {
  constructor({ frameRateHz = 30 } = {}) {
    this.frameRateHz = frameRateHz;
    this.seq = 0;
    this.lastFrameUtcMs = null;
    this.framesProduced = 0;
    this.nextDueMs = 0;
    this.exposure = null;
    this.blob = null;
    this.renderedSeq = null;
    this.renderedSize = null;
    this.pending = false;
  }

  /**
   * Advance the shutter. `running` false is a camera producing nothing — dead
   * or frozen — and the last exposure is deliberately KEPT, because that is
   * exactly the state the freeze contract exists for: a perfectly good picture
   * that is no longer true.
   */
  step(utcMs, { east, north, headingDeg, pitchDeg = 0, obstacles = [], running = true }) {
    if (!running || utcMs < this.nextDueMs) return;
    this.nextDueMs = utcMs + 1000 / Math.max(0.1, this.frameRateHz);
    this.seq += 1;
    this.framesProduced += 1;
    this.lastFrameUtcMs = utcMs;
    this.exposure = { utcMs, east, north, headingDeg, pitchDeg, obstacles, seq: this.seq };
  }

  /**
   * The latest picture as encoded bytes, or null.
   *
   * Rendering is lazy and the conversion to bytes is asynchronous, so a newly
   * due frame becomes available on the tick after it was taken. One tick of
   * latency in the mock is invisible and buys a code path that matches the
   * real one, where the encode also does not happen until something asks.
   */
  frame(width = NATIVE_WIDTH, height = NATIVE_HEIGHT) {
    const exposure = this.exposure;
    if (!exposure) return null;

    const size = `${width}x${height}`;
    if (this.renderedSeq !== exposure.seq || this.renderedSize !== size) {
      this.#render(exposure, width, height, size);
    }
    if (!this.blob || this.blob.seq !== exposure.seq || this.blob.size !== size) {
      return null;
    }
    return {
      seq: exposure.seq,
      utcMs: exposure.utcMs,
      width,
      height,
      format: 'png',
      bytes: this.blob.bytes,
    };
  }

  #render(exposure, width, height, size) {
    const canvas = makeCanvas(width, height);
    if (!canvas) return;                     // Node: headers only, no pixels
    this.renderedSeq = exposure.seq;
    this.renderedSize = size;

    const ctx = canvas.getContext('2d');
    const scale = width / NATIVE_WIDTH;
    const fy = FY * scale;
    const horizon = Math.max(
      1,
      Math.min(
        height - 1,
        Math.round(CY * scale + fy * Math.tan((exposure.pitchDeg * Math.PI) / 180)),
      ),
    );

    ctx.fillStyle = SKY;
    ctx.fillRect(0, 0, width, horizon);
    const sea = ctx.createLinearGradient(0, horizon, 0, height);
    sea.addColorStop(0, SEA_FAR);
    sea.addColorStop(1, SEA_NEAR);
    ctx.fillStyle = sea;
    ctx.fillRect(0, horizon, width, height - horizon);

    ctx.fillStyle = OBJECT;
    for (const box of projected(exposure, width, height, horizon, scale)) {
      ctx.fillRect(box.x1, box.y1, box.x2 - box.x1, box.y2 - box.y1);
    }

    stamp(ctx, width, height, exposure.utcMs);
    this.#toBytes(canvas, exposure.seq, size);
  }

  #toBytes(canvas, seq, size) {
    const done = (blob) => {
      if (!blob) return;
      blob.arrayBuffer().then((buffer) => {
        this.blob = { seq, size, bytes: new Uint8Array(buffer) };
      });
    };
    if (typeof canvas.convertToBlob === 'function') {
      canvas.convertToBlob({ type: 'image/png' }).then(done);
    } else if (typeof canvas.toBlob === 'function') {
      canvas.toBlob(done, 'image/png');
    }
  }
}

/** Mirrors CameraSim._project: obstacles through the measured camera matrix. */
function projected(exposure, width, height, horizon, scale) {
  const fx = FX * scale;
  const cx = CX * scale;
  const fy = FY * scale;
  const heading = (exposure.headingDeg * Math.PI) / 180;
  const sinH = Math.sin(heading);
  const cosH = Math.cos(heading);

  const boxes = [];
  for (const obstacle of exposure.obstacles) {
    const de = obstacle.east - exposure.east;
    const dn = obstacle.north - exposure.north;
    const forward = de * sinH + dn * cosH;
    const starboard = de * cosH - dn * sinH;
    if (forward < 1) continue;

    const u = cx + fx * (starboard / forward);
    const radiusPx = (fx * obstacle.radius) / forward;
    const base = horizon + (fy * 1.2) / forward;
    const box = {
      x1: Math.round(u - radiusPx),
      x2: Math.round(u + radiusPx),
      y1: Math.round(base - 2 * radiusPx),
      y2: Math.round(base),
      range: Math.hypot(forward, starboard),
    };
    if (box.x2 < 0 || box.x1 >= width || box.y2 < 0 || box.y1 >= height) continue;
    boxes.push(box);
  }
  boxes.sort((a, b) => b.range - a.range);
  return boxes;
}

/**
 * Burn the time of exposure into the picture.
 *
 * The one thing here that is not about looking like a camera, and the only
 * way to tell a correctly-reported freeze from a panel that has itself
 * stopped: both render a still image, and only one of them has a stopped clock
 * inside it.
 */
function stamp(ctx, width, height, utcMs) {
  const seconds = Math.floor(utcMs / 1000);
  const text = [
    String(Math.floor(seconds / 3600) % 24).padStart(2, '0'),
    String(Math.floor(seconds / 60) % 60).padStart(2, '0'),
    String(seconds % 60).padStart(2, '0'),
  ].join(':') + `.${Math.floor((utcMs % 1000) / 100)}`;

  const size = Math.max(10, Math.round(height / 24));
  ctx.font = `${size}px monospace`;
  const metrics = ctx.measureText(text);
  const pad = Math.round(size / 3);
  ctx.fillStyle = 'rgb(0, 0, 0)';
  ctx.fillRect(pad, height - size - pad * 3, metrics.width + pad * 2, size + pad * 2);
  ctx.fillStyle = 'rgb(255, 255, 255)';
  ctx.fillText(text, pad * 2, height - pad * 2 - size / 5);
}
