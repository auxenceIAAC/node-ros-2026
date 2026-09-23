"""Camera frames on the wire, and the silences between them.

The panel's job is not to show a picture. It is to refuse to show a stale one,
and to say which of several things went wrong when there is nothing to show at
all. Both of those are decided here, in what the backend does and does not put
on the wire — a panel cannot distinguish silences the protocol has already
collapsed into one.

Four states, and they send an operator to four different places:

* **A frame.** Pixels, with the time the shutter fell.
* **Dead.** The driver never opened the device. No picture has ever existed,
  so there is nothing stale to be fooled by — the easy case.
* **Frozen.** Frames were arriving and stopped. A perfectly good picture is
  still in memory and is no longer true. This is the hard one.
* **Nothing at all.** No status frames either, which means the link went. The
  backend cannot report this; its *absence* is the report, which is why the
  status frames have to keep arriving when everything else is fine.
"""

import asyncio

import pytest
from asket_sim.core.world import SimWorld, WorldConfig
from gui_backend.core import video_frame
from gui_backend.core.hub import ClientSession, Hub
from gui_backend.core.sim_source import SimSource
from gui_backend.core.streams import STREAMS


def running_hub(*faults, warmup_s: float = 3.0, rate_hz: float = 5.0):
    source = SimSource(SimWorld(WorldConfig()), time_scale=1.0)
    for name in faults:
        source.world.inject_fault(name)

    hub = Hub(source, tick_hz=20.0)
    session = ClientSession("browser")
    hub.add_client(session)
    hub.subscribe(session, [{"name": "camera", "rate_hz": rate_hz}])

    t = 1000.0
    for _ in range(int(warmup_s / 0.05)):
        t += 0.05
        hub.tick(t)
    return hub, session, source, t


def drain(session) -> list[tuple[dict, bytes]]:
    frames = []
    while not session.video_outbox.empty():
        frames.append(video_frame.decode(session.video_outbox.get_nowait()))
    return frames


def run(hub, session, start_t: float, seconds: float) -> list[tuple[dict, bytes]]:
    """Tick, draining as we go — the queue is one deep, so a test that ticked
    for five seconds and then looked would find exactly one frame and would be
    measuring the queue rather than the stream."""
    collected = []
    t = start_t
    for _ in range(int(seconds / 0.05)):
        t += 0.05
        hub.tick(t)
        collected.extend(drain(session))
    return collected


# -- the format ------------------------------------------------------------


def test_a_frame_round_trips():
    header = video_frame.frame_header(
        seq=7, source_utc_ms=1_789_932_011_300, server_utc_ms=1_789_932_011_420,
        width=640, height=480, image_format="png", size_bytes=3,
        detail="full", rate_hz=5.0,
    )
    decoded, image = video_frame.decode(video_frame.encode(header, b"abc"))
    assert decoded == header
    assert image == b"abc"


def test_a_truncated_frame_is_rejected_rather_than_half_read():
    whole = video_frame.encode({"type": "camera_status"}, b"xxxx")
    with pytest.raises(ValueError):
        video_frame.decode(whole[:3])
    with pytest.raises(ValueError):
        video_frame.decode(whole[:6])


def test_an_absurd_header_length_is_refused_before_it_is_allocated():
    with pytest.raises(ValueError):
        video_frame.decode(b"\xff\xff\xff\xff" + b"{}")


def test_the_header_stays_small_enough_to_be_worth_sending_often():
    """The status frame's whole justification is that it is cheap enough to
    send at frame rate. If it grew to a kilobyte the argument would collapse."""
    header = video_frame.status_header(
        reason=video_frame.REASON_FROZEN, server_utc_ms=1_789_932_011_420,
        rate_hz=5.0, last_frame_utc_ms=1_789_932_008_000, frames_produced=91,
    )
    assert len(video_frame.encode(header)) < 200


# -- pixels ----------------------------------------------------------------


def test_a_healthy_camera_puts_pictures_on_the_wire():
    hub, session, _, t = running_hub()
    frames = run(hub, session, t, 2.0)

    pictures = [(h, img) for h, img in frames if h["type"] == video_frame.KIND_FRAME]
    assert len(pictures) >= 5, f"only {len(pictures)} frames in two seconds at 5 Hz"
    header, image = pictures[-1]
    assert image[:8] == b"\x89PNG\r\n\x1a\n"
    assert header["size_bytes"] == len(image)
    assert (header["width"], header["height"]) == (640, 480)


def test_every_frame_carries_the_time_the_shutter_fell():
    """Not the time we encoded or sent it. The panel renders age from this, and
    a frame that spent four seconds in a queue has to read as four seconds
    old — otherwise a slow link renders as fresh data that happens to be
    wrong."""
    hub, session, source, t = running_hub()
    frames = run(hub, session, t, 2.0)
    for header, _ in frames:
        if header["type"] != video_frame.KIND_FRAME:
            continue
        assert header["source_utc_ms"] <= header["server_utc_ms"]
        assert header["source_utc_ms"] > 0


def test_the_sequence_number_advances():
    hub, session, _, t = running_hub()
    seqs = [
        h["seq"] for h, _ in run(hub, session, t, 2.0)
        if h["type"] == video_frame.KIND_FRAME
    ]
    assert seqs == sorted(seqs)
    assert len(set(seqs)) > 1, "the same picture is being sent over and over"


def test_the_negotiated_rate_travels_with_the_frame():
    """The panel needs it to know how late is late. Two missed frames at 15 Hz
    is a seventh of a second and means nothing; at 1 Hz it is two seconds and
    means a great deal."""
    hub, session, _, t = running_hub(rate_hz=3.0)
    header, _ = run(hub, session, t, 2.0)[-1]
    assert header["rate_hz"] == pytest.approx(3.0)


def test_frames_arrive_at_about_the_negotiated_rate():
    hub, session, _, t = running_hub(rate_hz=4.0)
    pictures = [h for h, _ in run(hub, session, t, 4.0)
                if h["type"] == video_frame.KIND_FRAME]
    assert 12 <= len(pictures) <= 20, f"{len(pictures)} frames in four seconds at 4 Hz"


# -- the silences ----------------------------------------------------------


def test_a_dead_camera_says_so_rather_than_saying_nothing():
    """The driver could not open the device. Saying nothing would be
    indistinguishable from the link having gone, and those are different
    problems with different fixes."""
    hub, session, _, t = running_hub("camera_dead")
    frames = run(hub, session, t, 2.0)

    assert frames, "a dead camera produced no traffic at all"
    assert all(h["type"] == video_frame.KIND_STATUS for h, _ in frames)
    assert all(h["reason"] == video_frame.REASON_DEAD for h, _ in frames)
    assert all(img == b"" for _, img in frames)
    assert frames[-1][0]["frames_produced"] == 0


def test_a_frozen_camera_stops_sending_pictures_and_says_why():
    """The hard case. There is a perfectly good picture in memory and it is no
    longer true, so it is not sent again — re-sending it would make a frozen
    camera indistinguishable from a working one on any panel that did not
    check the sequence number, and this is exactly the case that must not
    depend on the panel being careful."""
    hub, session, source, t = running_hub()
    assert any(h["type"] == video_frame.KIND_FRAME for h, _ in run(hub, session, t, 1.0))

    source.world.inject_fault("camera_frozen")
    frames = run(hub, session, t + 1.0, 3.0)

    assert frames
    assert all(h["type"] == video_frame.KIND_STATUS for h, _ in frames), (
        "a frozen camera is still putting pictures on the wire"
    )
    assert all(h["reason"] == video_frame.REASON_FROZEN for h, _ in frames)


def test_a_frozen_camera_still_reports_when_its_last_picture_was_taken():
    """So the panel can say "4.2 s old" rather than "no video". The operator
    needs the number: a picture two seconds old on a boat at survey speed is
    usable, and one from thirty seconds ago is not."""
    hub, session, source, t = running_hub()
    run(hub, session, t, 1.0)
    source.world.inject_fault("camera_frozen")

    header, _ = run(hub, session, t + 1.0, 3.0)[-1]
    assert header["last_frame_utc_ms"] is not None
    assert header["frames_produced"] > 0


def test_the_status_frames_keep_arriving_which_is_the_whole_point():
    """A panel that stops receiving these knows the link has gone; one that
    keeps receiving them knows the vessel is alive and the camera is not.
    Without them both silences look identical from the shore."""
    hub, session, source, t = running_hub()
    source.world.inject_fault("camera_frozen")
    frames = run(hub, session, t, 4.0)
    assert len(frames) >= 12, f"only {len(frames)} status frames in four seconds at 5 Hz"


def test_recovery_puts_pictures_back_without_a_resubscribe():
    hub, session, source, t = running_hub()
    source.world.inject_fault("camera_frozen")
    run(hub, session, t, 2.0)

    source.world.clear_fault("camera_frozen")
    after = run(hub, session, t + 2.0, 2.0)
    assert any(h["type"] == video_frame.KIND_FRAME for h, _ in after)


# -- and it does not cost the rest of the cockpit anything -----------------


def test_video_has_its_own_queue():
    """The telemetry outbox evicts the oldest when it fills. A 45 kB picture
    occupies one slot exactly as a 200-byte `pico` frame does, so sharing the
    queue means a degrading link drops the boat's position to make room for a
    picture — which is precisely backwards."""
    hub, session, _, t = running_hub()
    run(hub, session, t, 1.0)

    while not session.outbox.empty():
        message = session.outbox.get_nowait()
        assert message.get("stream") != "camera", "a picture went out on the telemetry queue"


def test_only_one_frame_is_ever_in_flight():
    """Depth one, newest wins. A slow link costs dropped frames, which is what
    a slow link should cost; a backlog would cost the operator a picture that
    was true a moment ago being presented as one that is true now."""
    hub, session, _, t = running_hub(rate_hz=15.0)
    tick = t
    for _ in range(40):                       # two seconds, draining nothing
        tick += 0.05
        hub.tick(tick)
    assert session.video_outbox.qsize() == 1
    assert session.video_dropped > 0, "nothing was dropped, so nothing was throttled"


def test_a_client_that_never_reads_does_not_stall_the_hub():
    hub, session, _, t = running_hub(rate_hz=15.0)
    tick = t
    for _ in range(200):
        tick += 0.05
        hub.tick(tick)      # must not raise or block
    assert session.video_outbox.qsize() == 1


def test_an_unsubscribed_camera_renders_nothing():
    """The architecture's first rule, on the one stream where it costs
    milliseconds rather than nanoseconds. The simulated world captures a pose
    when the shutter falls and draws the picture only when a subscription asks
    for it."""
    source = SimSource(SimWorld(WorldConfig()), time_scale=1.0)
    hub = Hub(source, tick_hz=20.0)
    session = ClientSession("browser")
    hub.add_client(session)
    hub.subscribe(session, [{"name": "vessel"}])       # everything but the camera

    t = 1000.0
    for _ in range(60):
        t += 0.05
        hub.tick(t)

    assert source.world.camera._cache is None, "a frame was rendered for nobody"
    assert source.world.camera.frames_produced > 0, "the shutter is not running at all"


# -- the negotiation -------------------------------------------------------


def test_the_camera_is_not_carried_on_a_narrow_link():
    """Absent from the reduced profile's policies, so resolve() refuses it with
    that profile's own sentence rather than granting a trickle nobody can use.
    A single 320x240 frame is most of a second's budget there."""
    from gui_backend.core.streams import resolve

    assert resolve("camera", None, None, "full").granted
    for profile in ("reduced", "minimal"):
        resolution = resolve("camera", None, None, profile)
        assert not resolution.granted
        assert profile in resolution.reason


def test_the_full_profile_can_actually_afford_it():
    """Raised from 200 kB/s, which was set when "fast WiFi" meant an access
    point on the beach. Video alone at its default rate would have eaten the
    whole of the old figure."""
    from gui_backend.core.streams import PROFILES, estimate_bytes_per_s, resolve

    spec = STREAMS["camera"]
    everything = [resolve(name, None, None, "full") for name in STREAMS]
    assert estimate_bytes_per_s(everything) < PROFILES["full"].budget_bytes_per_s
    assert spec.typical_bytes * spec.default_rate_hz > 200_000, (
        "the camera no longer justifies the raised budget; re-check it"
    )


def test_the_size_estimate_is_a_real_camera_not_the_simulated_one():
    """The simulator's flat synthetic frames compress to about 7 kB where a
    photograph takes forty. Budgeting against the mock would understate this
    stream sixfold, and the mock is where this GUI gets reviewed."""
    hub, session, _, t = running_hub()
    pictures = [img for h, img in run(hub, session, t, 1.0)
                if h["type"] == video_frame.KIND_FRAME]
    assert pictures
    simulated = max(len(img) for img in pictures)
    assert simulated < STREAMS["camera"].typical_bytes, (
        "the estimate is no longer conservative against the simulator"
    )


def test_the_writer_sends_binary_rather_than_text():
    """Guard on the transport, not on the format: the whole reason for a second
    writer is that these go out as bytes."""
    from gui_backend.core.server import _video_writer

    sent: list[bytes] = []

    class FakeSocket:
        async def send_bytes(self, data):
            sent.append(data)
            raise asyncio.CancelledError

    session = ClientSession("browser")
    session.send_video(video_frame.encode({"type": "camera_status"}, b"pixels"))
    asyncio.run(_video_writer(FakeSocket(), session))

    assert sent, "the video writer sent nothing"
    assert isinstance(sent[0], bytes)
    assert video_frame.decode(sent[0])[1] == b"pixels"


# -- the browser must frame them identically -------------------------------


def test_the_browser_encodes_the_same_frame_format():
    """``videoFrame.js`` is a transcription of ``video_frame.py``, and mock
    mode is where most people will ever watch this decode. A browser that
    framed its headers differently would leave the real decoding path — the
    part that can actually be wrong — unexercised everywhere it gets reviewed.
    """
    import json
    import shutil
    import subprocess
    import textwrap
    from pathlib import Path

    mock = Path(__file__).resolve().parents[2] / "asket_gui" / "src" / "lib" / "mock"
    if shutil.which("node") is None or not mock.is_dir():
        pytest.skip("node or the frontend mock is not present")

    script = textwrap.dedent(
        f"""
        const VF = await import('{mock}/videoFrame.js');
        const frame = VF.frameHeader({{
          seq: 7, sourceUtcMs: 1789932011300, serverUtcMs: 1789932011420,
          width: 640, height: 480, format: 'png', sizeBytes: 3,
          detail: 'full', rateHz: 5,
        }});
        const status = VF.statusHeader({{
          reason: VF.REASON_FROZEN, serverUtcMs: 1789932011420, rateHz: 5,
          lastFrameUtcMs: 1789932008000, framesProduced: 91,
        }});
        const encoded = Array.from(
          new Uint8Array(VF.encode(frame, new Uint8Array([97, 98, 99]))),
        );
        console.log(JSON.stringify({{ frame, status, encoded }}));
        """
    )
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        capture_output=True, text=True, timeout=120,
    )
    if result.returncode != 0:
        pytest.fail(f"node failed:\n{result.stderr}")
    js = json.loads(result.stdout)

    assert js["frame"] == video_frame.frame_header(
        seq=7, source_utc_ms=1789932011300, server_utc_ms=1789932011420,
        width=640, height=480, image_format="png", size_bytes=3,
        detail="full", rate_hz=5.0,
    )
    assert js["status"] == video_frame.status_header(
        reason=video_frame.REASON_FROZEN, server_utc_ms=1789932011420,
        rate_hz=5.0, last_frame_utc_ms=1789932008000, frames_produced=91,
    )
    # Byte for byte, not merely key for key: a differing separator or key order
    # would still parse and would still be a divergence.
    assert bytes(js["encoded"]) == video_frame.encode(js["frame"], b"abc")


def test_the_reasons_are_spelled_the_same_on_both_sides():
    """These strings are compared in the panel's branching. A typo on one side
    would silently land every silence in the default branch."""
    import json
    import shutil
    import subprocess
    import textwrap
    from pathlib import Path

    mock = Path(__file__).resolve().parents[2] / "asket_gui" / "src" / "lib" / "mock"
    if shutil.which("node") is None or not mock.is_dir():
        pytest.skip("node or the frontend mock is not present")

    result = subprocess.run(
        ["node", "--input-type=module", "-e", textwrap.dedent(f"""
            const VF = await import('{mock}/videoFrame.js');
            console.log(JSON.stringify([
              VF.KIND_FRAME, VF.KIND_STATUS, VF.REASON_LIVE,
              VF.REASON_DEAD, VF.REASON_FROZEN, VF.REASON_NOT_STARTED,
            ]));
        """)],
        capture_output=True, text=True, timeout=120,
    )
    if result.returncode != 0:
        pytest.fail(f"node failed:\n{result.stderr}")

    assert json.loads(result.stdout) == [
        video_frame.KIND_FRAME, video_frame.KIND_STATUS, video_frame.REASON_LIVE,
        video_frame.REASON_DEAD, video_frame.REASON_FROZEN,
        video_frame.REASON_NOT_STARTED,
    ]
