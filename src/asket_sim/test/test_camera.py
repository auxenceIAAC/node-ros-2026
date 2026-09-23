"""The simulated camera, and the three states it has to be able to be in.

The camera panel's job is not to show a picture. It is to stop showing one when
frames have stopped arriving, and to be clear about *which* thing stopped —
the device, the Jetson, or the link — because those send an operator to three
different places.

None of that can be tuned against a source that always works, and until this
existed the only camera in the project was Gazebo's, which needs the whole
workspace built. That is the shape of every problem this project has already
had: the thing nobody can test until the beach is the thing that is broken on
the beach.
"""

import math
import struct
import zlib
from pathlib import Path

import pytest
import yaml
from asket_sim.core.camera import (
    NATIVE_HEIGHT,
    NATIVE_WIDTH,
    CameraConfig,
    CameraSim,
    Intrinsics,
)
from asket_sim.core.lidar import Obstacle
from asket_sim.core.world import SimWorld, WorldConfig

OBSTACLES = [
    Obstacle(4.0, 40.0, 1.2, "channel buoy"),
    Obstacle(-12.0, 90.0, 2.0, "moored boat"),
]


def exposed(cam: CameraSim, utc_ms: int = 1_789_932_011_300, **kwargs) -> None:
    cam.step(1.0, utc_ms, 0.0, 0.0, 0.0, obstacles=OBSTACLES, **kwargs)


@pytest.fixture
def camera() -> CameraSim:
    cam = CameraSim()
    exposed(cam)
    return cam


# -- it produces a picture -------------------------------------------------


def test_a_frame_is_a_valid_png(camera):
    frame = camera.frame()
    assert frame.data[:8] == b"\x89PNG\r\n\x1a\n"
    assert frame.image_format == "png"
    assert (frame.width, frame.height) == (NATIVE_WIDTH, NATIVE_HEIGHT)


def test_the_png_decodes_to_the_pixels_we_drew():
    """Not merely "starts with the right magic". A malformed IDAT or a wrong
    filter byte produces a file that passes a signature check and renders as
    nothing at all in a browser, which is precisely the failure this source
    exists to avoid being confused with."""
    cam = CameraSim(CameraConfig(width=64, height=48))
    exposed(cam)
    frame = cam.frame()

    chunks = {}
    offset = 8
    while offset < len(frame.data):
        length = struct.unpack(">I", frame.data[offset:offset + 4])[0]
        kind = frame.data[offset + 4:offset + 8]
        chunks[kind] = frame.data[offset + 8:offset + 8 + length]
        offset += 12 + length

    width, height, depth, colour = struct.unpack(">IIBB", chunks[b"IHDR"][:10])
    assert (width, height, depth, colour) == (64, 48, 8, 2)   # 8-bit RGB

    raw = zlib.decompress(chunks[b"IDAT"])
    assert len(raw) == height * (1 + width * 3)
    # Every scanline carries filter type 0; anything else and the row offsets
    # below are meaningless.
    assert all(raw[y * (1 + width * 3)] == 0 for y in range(height))

    top = raw[1:4]
    bottom = raw[(height - 1) * (1 + width * 3) + 1:][:3]
    assert top != bottom, "sky and sea are the same colour"


def test_nothing_has_been_produced_before_the_first_exposure():
    """None, not a black frame. "The camera has not produced a picture yet" and
    "here is a picture, and it is black" are different claims, and only one of
    them is true at startup."""
    assert CameraSim().frame() is None


def test_the_time_of_exposure_is_in_the_header_and_in_the_pixels(camera):
    """The burned-in clock is the only way to tell a panel that correctly
    reports a stale feed from a panel that has itself stopped updating: both
    show a still image. This checks the two clocks agree at source."""
    frame = camera.frame()
    assert frame.utc_ms == 1_789_932_011_300

    smaller = CameraSim(CameraConfig(width=320, height=240))
    exposed(smaller, utc_ms=1_789_932_011_300)
    first = smaller.frame().data
    exposed(smaller, utc_ms=1_789_932_019_900)
    assert smaller.frame().data != first, "the picture did not change with the clock"


def test_obstacles_are_placed_by_range(camera):
    """Through the measured camera matrix, because the detection overlay will
    eventually draw boxes on these pixels and a box computed one way over a
    picture drawn another way is a discrepancy nobody would track down."""
    near, far = sorted(camera.frame().objects, key=lambda o: o.range_m)
    assert near.name == "channel buoy"
    assert far.name == "moored boat"
    assert (near.x2 - near.x1) > (far.x2 - far.x1), "the nearer object is not larger"
    assert near.y2 > far.y2, "the nearer object's waterline is not lower in frame"


def test_something_astern_is_not_in_shot():
    cam = CameraSim()
    cam.step(1.0, 0, 0.0, 0.0, 0.0, obstacles=[Obstacle(0.0, -50.0, 2.0, "astern")])
    assert cam.frame().objects == ()


def test_pitching_the_bow_up_pushes_the_horizon_down():
    """A sign error here would be invisible in a still and obvious to anybody
    watching the picture and the attitude readout together."""
    def horizon_row(pitch):
        cam = CameraSim(CameraConfig(width=64, height=48))
        cam.step(1.0, 0, 0.0, 0.0, 0.0, pitch_deg=pitch)
        frame = cam.frame()
        raw = zlib.decompress(
            _idat(frame.data)
        )
        stride = 1 + frame.width * 3
        sky = raw[1:4]
        for y in range(frame.height):
            if raw[y * stride + 1:y * stride + 4] != sky:
                return y
        return frame.height

    assert horizon_row(10.0) > horizon_row(0.0) > horizon_row(-10.0)


def _idat(data: bytes) -> bytes:
    offset = 8
    while offset < len(data):
        length = struct.unpack(">I", data[offset:offset + 4])[0]
        if data[offset + 4:offset + 8] == b"IDAT":
            return data[offset + 8:offset + 8 + length]
        offset += 12 + length
    raise AssertionError("no IDAT")


# -- and costs nothing when nobody is watching -----------------------------


def test_stepping_renders_nothing(camera):
    """The architecture's first rule is that a stream with no subscriber costs
    nothing, and this is the one stream where that is measured in milliseconds
    rather than nanoseconds. ``step`` captures a pose; the picture is drawn on
    demand.

    The real source has the same obligation for a harder reason: an rclpy
    subscription to a 30 Hz raw image topic deserialises 27 MB/s whether or not
    anybody reads it.
    """
    cam = CameraSim()
    for _ in range(50):
        exposed(cam)
    assert cam._cache is None, "stepping rendered a frame nobody asked for"
    assert cam.frames_produced == 50


def test_the_same_frame_is_rendered_once(camera):
    first = camera.frame()
    assert camera.frame() is first

    exposed(camera, utc_ms=1_789_932_012_300)
    assert camera.frame() is not first


def test_asking_for_a_different_size_re_renders(camera):
    """The panel offers a quality ladder, so the same exposure gets asked for
    at more than one size. Returning the cached large one would silently ignore
    the negotiated resolution."""
    small = camera.frame(320, 240)
    assert (small.width, small.height) == (320, 240)
    assert small.size_bytes < camera.frame(640, 480).size_bytes


# -- the intrinsics are the real ones --------------------------------------


CALIBRATION = (
    Path(__file__).resolve().parents[2] / "bringup" / "config" / "front_camera.yaml"
)


@pytest.mark.skipif(not CALIBRATION.is_file(), reason="calibration file not present")
def test_the_simulator_projects_through_the_measured_lens():
    """``front_camera.yaml`` is a competition package's file and is not edited
    from here — but it is read, so a recalibration cannot silently leave the
    simulator projecting through the old lens while the boat uses the new one.
    """
    calibration = yaml.safe_load(CALIBRATION.read_text())
    fx, _, cx, _, fy, cy, *_ = calibration["camera_matrix"]["data"]
    measured = Intrinsics()

    assert measured.fx == pytest.approx(fx)
    assert measured.fy == pytest.approx(fy)
    assert measured.cx == pytest.approx(cx)
    assert measured.cy == pytest.approx(cy)
    assert (calibration["image_width"], calibration["image_height"]) == (
        NATIVE_WIDTH, NATIVE_HEIGHT,
    )


def test_native_resolution_is_the_ceiling():
    """Both the real calibration and the Gazebo sensor say 640x480, so there is
    no higher rung for the quality control to offer. Worth pinning before
    somebody designs a 1080p option."""
    assert (NATIVE_WIDTH, NATIVE_HEIGHT) == (640, 480)


# -- the three states ------------------------------------------------------


def stepped_world(*faults, seconds: float = 2.0) -> SimWorld:
    world = SimWorld(WorldConfig())
    for name in faults:
        world.inject_fault(name)
    for _ in range(int(seconds / 0.05)):
        world.step(0.05)
    return world


def test_a_healthy_camera_produces_frames():
    world = stepped_world()
    assert world.camera_state() == "live"
    assert world.camera.frames_produced > 0
    assert world.camera.frame() is not None


def test_a_dead_camera_never_produces_one():
    """The driver could not open the device. There is no stale frame to be
    misled by, which makes this the *easier* of the two failures — and a
    different message from the other."""
    world = stepped_world("camera_dead")
    assert world.camera_state() == "dead"
    assert world.camera.frames_produced == 0
    assert world.camera.frame() is None


def test_a_frozen_camera_keeps_its_last_frame():
    """The fault the whole freeze contract exists for. The picture in memory is
    perfectly good and is no longer true, and clearing it would let the panel
    pass a test it should fail — an empty panel is easy to render honestly.
    """
    world = stepped_world(seconds=2.0)
    before = world.camera.frame()
    assert before is not None

    world.inject_fault("camera_frozen")
    for _ in range(100):
        world.step(0.05)

    assert world.camera_state() == "frozen"
    assert world.camera.frame() is before, "the last good frame was thrown away"
    assert world.camera.frame().utc_ms == before.utc_ms


def test_the_frozen_frame_is_visibly_older_than_the_world():
    """Five seconds of world time with the picture's clock standing still. This
    is the state the panel has to notice, and the burned-in stamp is how a
    human confirms the panel noticed rather than taking its word."""
    world = stepped_world(seconds=2.0)
    world.inject_fault("camera_frozen")
    for _ in range(100):
        world.step(0.05)

    age_ms = world.snapshot().utc_ms - world.camera.frame().utc_ms
    assert age_ms > 4500, f"only {age_ms} ms of staleness after five seconds"


def test_unfreezing_starts_the_clock_again():
    world = stepped_world(seconds=2.0)
    world.inject_fault("camera_frozen")
    for _ in range(60):
        world.step(0.05)
    stale = world.camera.frame()

    world.clear_fault("camera_frozen")
    for _ in range(20):
        world.step(0.05)

    assert world.camera.frame().utc_ms > stale.utc_ms


def test_the_camera_state_reaches_the_snapshot():
    """So the backend can tell the panel which of the three silences it has.
    Without this the GUI can only report that nothing arrived, which is the
    one thing it already knew."""
    assert stepped_world().snapshot().camera_state == "live"
    assert stepped_world("camera_dead").snapshot().camera_state == "dead"
    assert stepped_world("camera_frozen").snapshot().camera_state == "frozen"
    assert stepped_world().snapshot().camera_frames > 0


def test_a_frozen_camera_does_not_stop_the_rest_of_the_vessel():
    """Guard against the obvious over-reach. The boat keeps running, the link
    keeps working, and only the picture is stale — which is what makes this
    fault deceptive and worth simulating."""
    world = stepped_world(seconds=2.0)
    world.inject_fault("camera_frozen")
    before = world.snapshot()
    for _ in range(100):
        world.step(0.05)
    after = world.snapshot()

    assert after.utc_ms > before.utc_ms
    assert after.vessel.distance_travelled_m > before.vessel.distance_travelled_m
    assert after.link.active_link == before.link.active_link


# -- size, honestly --------------------------------------------------------


def test_a_frame_is_small_but_this_says_nothing_about_a_real_camera(camera):
    """A synthetic scene of flat colours compresses to a few kilobytes where a
    photograph would take forty. Nothing may tune a link budget against these
    numbers — the negotiator's estimate is a static real-world figure — and
    this test exists to say so where somebody measuring the mock would look.
    """
    assert camera.frame().size_bytes < 60_000
    assert camera.frame().size_bytes > 200, "suspiciously empty for a 640x480 frame"


def test_rendering_is_quick_enough_to_do_at_frame_rate():
    """Not a benchmark — a floor. If a frame took a hundred milliseconds the
    simulator would fall behind its own clock and the staleness under test
    would be the simulator's, not the fault's.
    """
    import time

    cam = CameraSim()
    exposed(cam)
    start = time.perf_counter()
    for seq in range(10):
        exposed(cam, utc_ms=1_789_932_011_300 + seq * 200)
        cam.frame()
    per_frame_ms = (time.perf_counter() - start) * 100.0
    assert per_frame_ms < 60.0, f"{per_frame_ms:.0f} ms per frame"


def test_the_geometry_is_finite_wherever_the_boat_is():
    """Including looking straight at an obstacle from a metre away, which the
    projection divides by."""
    cam = CameraSim()
    for distance in (0.5, 1.0, 1.01, 5.0, 500.0):
        cam.step(
            1.0, 0, 0.0, 0.0, 0.0,
            obstacles=[Obstacle(0.0, distance, 1.0, "close")],
        )
        frame = cam.frame()
        assert frame is not None
        for obj in frame.objects:
            assert all(math.isfinite(v) for v in (obj.x1, obj.y1, obj.x2, obj.y2))
