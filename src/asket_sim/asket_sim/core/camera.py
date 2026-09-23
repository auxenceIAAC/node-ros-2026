"""Simulated forward camera.

Why a simulated camera exists at all, given that Gazebo already publishes one.

The camera panel's hardest requirement is not to show a picture. It is to
**stop** showing one: to notice that frames have stopped arriving and refuse to
keep presenting the last good frame as though it were current. A frozen video
feed is the most convincing lie this interface could tell, and on the hardware
this boat now carries the link does not fade — it works and then it stops, in
about two seconds.

Gazebo can produce that picture, but only on a machine with the whole workspace
built. This project has been bitten three times by a failure mode that
simulation exercised and the field did not, and a camera panel that can only be
tested with hardware attached — or with a full ROS 2 install — is one more
thing nobody checks until the beach. So the camera is a first-class simulated
source like the sonar and the lidar, and the freeze contract is reviewable with
`npm run dev:mock` or `python3 -m gui_backend.core.app --sim`.

Two things this is careful about:

**Standard library only.** Every other module in ``core/`` manages it, and the
promise that ``pytest`` at the repo root runs the whole suite with nothing
installed is worth more than a nicer renderer. So the frames are PNG, written
with :mod:`zlib` and :mod:`struct`, rather than JPEG — browsers decode both and
the wire format is declared per frame. The real vessel encodes JPEG with the
OpenCV that its ROS environment already has, on a path that laptops never run.

**A burned-in timestamp.** The frame carries the time in its own pixels as well
as in its header. That is what makes a freeze verifiable by eye rather than by
trusting the thing under test: inject ``camera_frozen``, and the number in the
corner of the picture stops while the age badge beside it climbs. A panel that
reported staleness correctly *and* a panel that had quietly stopped updating
both look like a still image; only the burned-in clock tells them apart.

These frames are **not representative of a real camera's size.** A synthetic
scene of flat colours compresses to a few kilobytes where a photograph would
take forty. Nothing should tune a link budget against them — the estimate the
negotiator uses is a static real-world figure in ``streams.py``, not whatever
this happens to emit.
"""

from __future__ import annotations

import math
import struct
import zlib
from dataclasses import dataclass, field

#: Both the Gazebo sensor in ``asket.urdf.xacro`` and the real calibration in
#: ``bringup/config/front_camera.yaml`` say 640x480. There is no higher rung to
#: offer, which is a useful thing to know before anybody asks for 1080p.
NATIVE_WIDTH = 640
NATIVE_HEIGHT = 480


@dataclass(frozen=True)
class Intrinsics:
    """The measured camera matrix.

    Transcribed from ``src/bringup/config/front_camera.yaml``, which is a
    competition package and is not modified from here. ``test_camera.py``
    reads that file and compares, so a recalibration cannot silently leave the
    simulator projecting through the old lens.
    """

    fx: float = 700.2277305628887
    fy: float = 696.4669027105944
    cx: float = 294.55943543522966
    cy: float = 226.595388208691


@dataclass
class CameraConfig:
    width: int = NATIVE_WIDTH
    height: int = NATIVE_HEIGHT
    intrinsics: Intrinsics = field(default_factory=Intrinsics)
    #: How often the driver produces a frame. The real one runs a 30 Hz timer;
    #: what reaches the shore is decided by the stream negotiation, not here.
    frame_rate_hz: float = 30.0
    #: zlib level. 1 rather than 6: these are flat synthetic scenes, the
    #: difference in size is small and the difference in time is not.
    compression: int = 1

    #: Colours, BGR-free — this module works in RGB throughout and converts
    #: nowhere, which is one fewer place to get a channel order wrong.
    sky: tuple[int, int, int] = (150, 170, 190)
    sea_near: tuple[int, int, int] = (44, 74, 96)
    sea_far: tuple[int, int, int] = (92, 118, 138)


@dataclass(frozen=True)
class ProjectedObject:
    """Something the camera can see, in pixels.

    Produced whether or not anything draws boxes. The simulator has to project
    the obstacle to paint it at all, so the rectangle is free — and when the
    detection overlay lands it must come from the same projection, or the boxes
    and the pixels will disagree by a few pixels forever.
    """

    name: str
    x1: int
    y1: int
    x2: int
    y2: int
    range_m: float


@dataclass(frozen=True)
class CameraFrame:
    #: The time the picture was taken. Never the time it was encoded or sent:
    #: the panel renders age from this, and a frame that spent four seconds in
    #: a queue must read as four seconds old.
    utc_ms: int
    seq: int
    width: int
    height: int
    image_format: str
    data: bytes
    objects: tuple[ProjectedObject, ...]

    @property
    def size_bytes(self) -> int:
        return len(self.data)


@dataclass(frozen=True)
class _Exposure:
    """What the camera saw when the shutter fell. A few floats, captured
    eagerly; the picture itself is rendered only if somebody asks for it."""

    utc_ms: int
    seq: int
    east_m: float
    north_m: float
    heading_deg: float
    pitch_deg: float
    roll_deg: float
    obstacles: tuple


class CameraSim:
    """A forward camera that renders only when somebody is watching.

    ``step()`` advances the shutter and captures a pose when a frame falls due;
    ``frame()`` renders it. The split is the same discipline the link has, for
    a stronger reason here: rendering costs milliseconds rather than
    nanoseconds, and the architecture's first rule is that a stream with no
    subscriber costs nothing. An unwatched camera in this simulator does
    approximately no work, which is also how the real one is meant to behave —
    ``RosSource`` subscribes to the image topic only while the stream is
    granted, because deserialising 30 Hz of raw frames costs 27 MB/s whether
    or not anybody reads them.
    """

    def __init__(self, config: CameraConfig | None = None) -> None:
        self.cfg = config or CameraConfig()
        self._t = 0.0
        self._next_due_t = 0.0
        self._seq = 0
        self._exposure: _Exposure | None = None
        self._rendered: tuple[int, int, int] | None = None
        self._cache: CameraFrame | None = None

    # -- simulation -------------------------------------------------------

    def step(
        self,
        dt: float,
        utc_ms: int,
        east_m: float,
        north_m: float,
        heading_deg: float,
        pitch_deg: float = 0.0,
        roll_deg: float = 0.0,
        obstacles=(),
        running: bool = True,
    ) -> None:
        """Advance the shutter.

        ``running`` false is the camera producing nothing — a dead device, or a
        frozen one. The last exposure is deliberately kept rather than cleared:
        that is exactly the state the freeze contract exists for, where a
        perfectly good picture is still in memory and is no longer true.
        """
        self._t += dt
        if not running or self._t < self._next_due_t:
            return
        self._next_due_t = self._t + 1.0 / max(0.1, self.cfg.frame_rate_hz)
        self._seq += 1
        self._exposure = _Exposure(
            utc_ms=utc_ms, seq=self._seq,
            east_m=east_m, north_m=north_m, heading_deg=heading_deg,
            pitch_deg=pitch_deg, roll_deg=roll_deg,
            obstacles=tuple(obstacles),
        )

    # -- observation ------------------------------------------------------

    @property
    def frames_produced(self) -> int:
        return self._seq

    def frame(self, width: int | None = None, height: int | None = None) -> CameraFrame | None:
        """The latest picture, rendered on demand and cached.

        None when the camera has produced nothing yet, which is a different
        thing from a stale frame and the panel says so differently.
        """
        exposure = self._exposure
        if exposure is None:
            return None
        width = width or self.cfg.width
        height = height or self.cfg.height

        key = (exposure.seq, width, height)
        if self._rendered == key and self._cache is not None:
            return self._cache

        self._cache = self._render(exposure, width, height)
        self._rendered = key
        return self._cache

    # -- rendering --------------------------------------------------------

    def _render(self, exposure: _Exposure, width: int, height: int) -> CameraFrame:
        cfg = self.cfg
        # Scale from the resolution the lens was calibrated at, not from
        # whatever this CameraSim happens to be configured for. Getting this
        # wrong put the horizon off the bottom of any frame smaller than
        # native, because a 480-row calibration's principal point does not fit
        # in a 48-row picture.
        scale = width / NATIVE_WIDTH
        fy = cfg.intrinsics.fy * scale
        cy = cfg.intrinsics.cy * scale

        # Where the horizon falls. Pitching the bow up pushes it down the
        # frame, which is the sign that reads correctly when somebody watches
        # the picture and the attitude readout at the same time.
        horizon = int(cy + fy * math.tan(math.radians(exposure.pitch_deg)))
        horizon = max(1, min(height - 1, horizon))

        rows = self._sea_and_sky(width, height, horizon)
        objects = self._project(exposure, width, height, horizon)
        for obj in objects:
            _fill(rows, width, height, obj.x1, obj.y1, obj.x2, obj.y2, (30, 30, 34))
        _stamp(rows, width, height, exposure.utc_ms)

        return CameraFrame(
            utc_ms=exposure.utc_ms,
            seq=exposure.seq,
            width=width,
            height=height,
            image_format="png",
            data=_encode_png(width, height, rows, self.cfg.compression),
            objects=objects,
        )

    def _sea_and_sky(self, width: int, height: int, horizon: int) -> list[bytearray]:
        cfg = self.cfg
        sky_row = bytes(cfg.sky) * width
        rows = [bytearray(sky_row) for _ in range(horizon)]
        below = max(1, height - horizon)
        for y in range(below):
            # Sea darkens toward the bow, which gives the eye something to tell
            # one frame from another when the boat is not moving much.
            t = y / below
            colour = bytes(
                int(far + (near - far) * t)
                for far, near in zip(cfg.sea_far, cfg.sea_near)
            )
            rows.append(bytearray(colour * width))
        return rows

    def _project(
        self, exposure: _Exposure, width: int, height: int, horizon: int
    ) -> tuple[ProjectedObject, ...]:
        """Obstacles from the world, through the real camera matrix.

        Through the measured intrinsics rather than a convenient approximation
        because the detection overlay will eventually draw boxes on these
        pixels, and a box computed one way over a picture drawn another way is
        a discrepancy nobody would ever track down.
        """
        cfg = self.cfg
        scale = width / NATIVE_WIDTH
        fx = cfg.intrinsics.fx * scale
        cx = cfg.intrinsics.cx * scale
        fy = cfg.intrinsics.fy * scale

        heading = math.radians(exposure.heading_deg)
        sin_h, cos_h = math.sin(heading), math.cos(heading)

        seen: list[ProjectedObject] = []
        for obstacle in exposure.obstacles:
            de = obstacle.east_m - exposure.east_m
            dn = obstacle.north_m - exposure.north_m
            # Into the boat's frame: forward along the heading, starboard to
            # the right of it.
            forward = de * sin_h + dn * cos_h
            starboard = de * cos_h - dn * sin_h
            if forward < 1.0:
                continue                      # behind, or on top of us

            u = cx + fx * (starboard / forward)
            radius_px = fx * obstacle.radius_m / forward
            # Waterline at the horizon, so a nearer object sits lower and
            # bigger. Crude, and enough: this is a transport test, not a
            # rendering engine.
            base = horizon + fy * 1.2 / forward
            x1 = int(u - radius_px)
            x2 = int(u + radius_px)
            y2 = int(base)
            y1 = int(base - 2.0 * radius_px)
            if x2 < 0 or x1 >= width or y2 < 0 or y1 >= height:
                continue
            seen.append(
                ProjectedObject(
                    name=obstacle.name or "object",
                    x1=x1, y1=y1, x2=x2, y2=y2,
                    range_m=math.hypot(forward, starboard),
                )
            )
        seen.sort(key=lambda o: -o.range_m)     # far ones painted first
        return tuple(seen)


# -- pixels ----------------------------------------------------------------


def _fill(rows, width, height, x1, y1, x2, y2, colour) -> None:
    x1 = max(0, min(width, x1))
    x2 = max(0, min(width, x2))
    y1 = max(0, min(height, y1))
    y2 = max(0, min(height, y2))
    if x2 <= x1 or y2 <= y1:
        return
    patch = bytes(colour) * (x2 - x1)
    for y in range(y1, y2):
        rows[y][x1 * 3:x2 * 3] = patch


#: A 3x5 bitmap digit, as five rows of three bits. Small enough to be worth
#: writing out rather than pulling in a font, and the only text this renderer
#: ever needs.
_DIGITS = {
    "0": (0b111, 0b101, 0b101, 0b101, 0b111),
    "1": (0b010, 0b110, 0b010, 0b010, 0b111),
    "2": (0b111, 0b001, 0b111, 0b100, 0b111),
    "3": (0b111, 0b001, 0b111, 0b001, 0b111),
    "4": (0b101, 0b101, 0b111, 0b001, 0b001),
    "5": (0b111, 0b100, 0b111, 0b001, 0b111),
    "6": (0b111, 0b100, 0b111, 0b101, 0b111),
    "7": (0b111, 0b001, 0b010, 0b010, 0b010),
    "8": (0b111, 0b101, 0b111, 0b101, 0b111),
    "9": (0b111, 0b101, 0b111, 0b001, 0b111),
    ":": (0b000, 0b010, 0b000, 0b010, 0b000),
    ".": (0b000, 0b000, 0b000, 0b000, 0b010),
}


def _stamp(rows, width, height, utc_ms: int, pixel: int = 3) -> None:
    """Burn the time of exposure into the picture.

    The one thing in this module that is not about looking like a camera. A
    panel that has correctly detected a stale feed and a panel that has itself
    quietly stopped updating produce the same still image; the only way to tell
    them apart from the outside is a clock inside the picture. Freeze the feed
    from the dev panel and this number stops while the age badge climbs.
    """
    seconds = utc_ms // 1000
    text = (
        f"{seconds // 3600 % 24:02d}:{seconds // 60 % 60:02d}:"
        f"{seconds % 60:02d}.{utc_ms % 1000 // 100}"
    )
    glyph_w = (3 + 1) * pixel
    box_w = glyph_w * len(text) + pixel * 2
    box_h = 5 * pixel + pixel * 2
    x0, y0 = pixel * 2, height - box_h - pixel * 2
    if box_w > width or y0 < 0:
        return

    _fill(rows, width, height, x0, y0, x0 + box_w, y0 + box_h, (0, 0, 0))
    for index, char in enumerate(text):
        glyph = _DIGITS.get(char)
        if glyph is None:
            continue
        gx = x0 + pixel + index * glyph_w
        gy = y0 + pixel
        for row, bits in enumerate(glyph):
            for col in range(3):
                if bits & (1 << (2 - col)):
                    _fill(
                        rows, width, height,
                        gx + col * pixel, gy + row * pixel,
                        gx + (col + 1) * pixel, gy + (row + 1) * pixel,
                        (255, 255, 255),
                    )


def _chunk(kind: bytes, payload: bytes) -> bytes:
    return (
        struct.pack(">I", len(payload))
        + kind
        + payload
        + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)
    )


def _encode_png(width: int, height: int, rows, level: int = 1) -> bytes:
    """A minimal PNG, standard library only.

    Chosen over JPEG because every other module in ``core/`` manages without a
    third-party import and the promise that the whole suite runs on a bare
    Python is worth more than a photographic codec for pictures of flat blue.
    Browsers decode both, and the frame declares which it is, so the panel
    never has to care.
    """
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)  # 8-bit RGB
    raw = b"".join(b"\x00" + bytes(row) for row in rows)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", header)
        + _chunk(b"IDAT", zlib.compress(raw, level))
        + _chunk(b"IEND", b"")
    )
