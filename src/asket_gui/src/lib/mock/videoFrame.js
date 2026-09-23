/**
 * The camera frame format, browser side.
 *
 * Mirrors `gui_backend/core/video_frame.py`. Encoding lives here so that mock
 * mode puts genuinely binary frames on the wire and `connection.js` decodes
 * them exactly as it does from the vessel — the decoding is the part that can
 * be wrong, and mock mode is the only place most people will ever see it run.
 *
 * `[4 bytes big-endian header length][header JSON UTF-8][image bytes]`
 */

export const KIND_FRAME = 'camera_frame';
export const KIND_STATUS = 'camera_status';

export const REASON_LIVE = 'live';
export const REASON_DEAD = 'camera_dead';
export const REASON_FROZEN = 'camera_frozen';
export const REASON_NOT_STARTED = 'no_frames_yet';

export function encode(header, image = new Uint8Array(0)) {
  const blob = new TextEncoder().encode(JSON.stringify(header));
  const out = new Uint8Array(4 + blob.length + image.length);
  new DataView(out.buffer).setUint32(0, blob.length, false);
  out.set(blob, 4);
  out.set(image, 4 + blob.length);
  return out.buffer;
}

export function frameHeader({
  seq, sourceUtcMs, serverUtcMs, width, height, format, sizeBytes, detail, rateHz,
}) {
  return {
    type: KIND_FRAME,
    stream: 'camera',
    reason: REASON_LIVE,
    seq,
    source_utc_ms: sourceUtcMs,
    server_utc_ms: serverUtcMs,
    width,
    height,
    format,
    size_bytes: sizeBytes,
    detail,
    rate_hz: Math.round(rateHz * 1000) / 1000,
  };
}

export function statusHeader({
  reason, serverUtcMs, rateHz, lastFrameUtcMs = null, framesProduced = 0,
}) {
  return {
    type: KIND_STATUS,
    stream: 'camera',
    reason,
    server_utc_ms: serverUtcMs,
    rate_hz: Math.round(rateHz * 1000) / 1000,
    last_frame_utc_ms: lastFrameUtcMs,
    frames_produced: framesProduced,
  };
}
