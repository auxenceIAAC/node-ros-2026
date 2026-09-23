"""The wire format for camera frames, and the one reason it is not JSON.

Every other stream in this system is a JSON object on a text WebSocket, and
that is right for them: a few hundred bytes of numbers, readable in a browser's
network tab, trivially comparable across languages. A picture is not that.

A frame is::

    [4 bytes, big-endian, header length][header, JSON, UTF-8][image bytes]

Base64 inside the ordinary JSON envelope was the obvious alternative and would
have needed no new code anywhere. It was rejected for two reasons, and
*bandwidth was not really one of them* — with the directional link the 33%
overhead costs about half a megabit, which this link does not notice:

* **Parsing.** A 60 kB base64 string has to be escaped by ``json.dumps`` on the
  Jetson and parsed by ``JSON.parse`` in the browser, five to fifteen times a
  second, and then decoded again. Binary skips all three.
* **Copies.** base64 -> string -> data URL -> decoded bitmap is several copies
  of the same image in flight at once, on a laptop that is also drawing a map.

The header is JSON rather than a packed struct because it is small, it changes
as the panel grows, and a format whose meaning you can read off the wire is
worth a few dozen bytes. The image bytes are whatever the source produced:
PNG from the simulator, which has only the standard library, and JPEG from the
vessel, whose ROS environment already has OpenCV. The header says which, so
the panel never has to care — ``createImageBitmap`` takes both.

**What is deliberately in the header rather than anywhere else:** the time the
picture was taken, its sequence number, and why a frame is missing when one is.
The panel's whole job is to refuse to present a stale picture as a current one,
and it can only do that if every frame carries its own age and if the gaps
carry a reason. So this module also defines the *status* frame — a header with
no image — which is what goes out when the camera has nothing to give. That
frame is about eighty bytes, and it is the difference between "the boat stopped
sending" and "the link stopped carrying": if the status frames are still
arriving, it is the camera; if they are not, it is the link. Without it, both
look identical from the shore, and they send you to different places.
"""

from __future__ import annotations

import json
import struct

#: Set on a frame that carries pixels.
KIND_FRAME = "camera_frame"
#: Set on a frame that does not, and says why.
KIND_STATUS = "camera_status"

#: Why there is no picture. Each is a different problem with a different fix,
#: and collapsing them into "no video" would throw away the only part an
#: operator can act on.
REASON_LIVE = "live"                    #: A picture is attached.
REASON_DEAD = "camera_dead"             #: The driver never opened the device.
REASON_FROZEN = "camera_frozen"         #: It produced frames and stopped.
REASON_NOT_STARTED = "no_frames_yet"    #: Running, nothing produced yet.

#: Anything longer than this is not a header we wrote. Guards a malformed or
#: hostile first four bytes from being used to allocate.
MAX_HEADER_BYTES = 8192


def encode(header: dict, image: bytes = b"") -> bytes:
    """Pack a header and an optional image into one binary frame."""
    blob = json.dumps(header, separators=(",", ":")).encode("utf-8")
    if len(blob) > MAX_HEADER_BYTES:
        raise ValueError(f"camera header is {len(blob)} bytes; something is wrong")
    return struct.pack(">I", len(blob)) + blob + image


def decode(frame: bytes) -> tuple[dict, bytes]:
    """Unpack one. Raises ``ValueError`` on anything malformed.

    Used by the tests and by nothing on the hot path — the browser does this in
    a few lines of its own. It exists here so that the format has exactly one
    definition and the tests check the real thing rather than a description
    of it.
    """
    if len(frame) < 4:
        raise ValueError("frame is shorter than its own length prefix")
    (length,) = struct.unpack(">I", frame[:4])
    if length > MAX_HEADER_BYTES or len(frame) < 4 + length:
        raise ValueError(f"header length {length} does not fit in {len(frame)} bytes")
    return json.loads(frame[4:4 + length].decode("utf-8")), frame[4 + length:]


def frame_header(
    *,
    seq: int,
    source_utc_ms: int,
    server_utc_ms: int,
    width: int,
    height: int,
    image_format: str,
    size_bytes: int,
    detail: str,
    rate_hz: float,
) -> dict:
    """The header on a frame that carries a picture.

    ``source_utc_ms`` is when the shutter fell, never when we encoded or sent
    it. ``rate_hz`` is what was actually negotiated, which the panel needs in
    order to know how late is late: two missed frames at 10 fps is a fifth of a
    second and means nothing, and at 1 fps it is two seconds and means a great
    deal.
    """
    return {
        "type": KIND_FRAME,
        "stream": "camera",
        "reason": REASON_LIVE,
        "seq": seq,
        "source_utc_ms": source_utc_ms,
        "server_utc_ms": server_utc_ms,
        "width": width,
        "height": height,
        "format": image_format,
        "size_bytes": size_bytes,
        "detail": detail,
        "rate_hz": round(rate_hz, 3),
    }


def status_header(
    *,
    reason: str,
    server_utc_ms: int,
    rate_hz: float,
    last_frame_utc_ms: int | None = None,
    frames_produced: int = 0,
) -> dict:
    """The header on a frame that carries no picture, and why.

    Sent at the negotiated cadence rather than once, because its arrival is
    itself the signal. A panel that stops receiving these knows the link has
    gone; one that keeps receiving them knows the vessel is alive and the
    camera is not. Eighty bytes to tell two identical-looking silences apart.
    """
    return {
        "type": KIND_STATUS,
        "stream": "camera",
        "reason": reason,
        "server_utc_ms": server_utc_ms,
        "rate_hz": round(rate_hz, 3),
        "last_frame_utc_ms": last_frame_utc_ms,
        "frames_produced": int(frames_produced),
    }
