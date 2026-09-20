"""The hub: nothing is pushed unless it was asked for, and never faster than
the link can carry."""

import pytest
from asket_sim.core.world import SimWorld, WorldConfig
from gui_backend.core import adapters
from gui_backend.core.hub import ClientSession, Hub
from gui_backend.core.sim_source import SimSource
from gui_backend.core.streams import PROFILE_MINIMAL, PROFILE_REDUCED


@pytest.fixture
def hub():
    return Hub(SimSource(SimWorld(WorldConfig()), time_scale=10.0))


@pytest.fixture
def client(hub):
    session = ClientSession("test")
    hub.add_client(session)
    return session


def drain(session):
    out = []
    while not session.outbox.empty():
        out.append(session.outbox.get_nowait())
    return out


def run(hub, seconds, start=1000.0, step=0.05):
    t = start
    for _ in range(int(seconds / step)):
        t += step
        hub.tick(t)
    return t


def run_collecting(hub, session, seconds, start=1000.0, step=0.05):
    """Run, draining the client every tick.

    The outbox is bounded and drops its oldest frames, so a test that runs for
    a while and drains at the end sees a window, not the whole conversation.
    Anything asserting on the *sequence* of frames has to keep up.
    """
    collected = []
    t = start
    for _ in range(int(seconds / step)):
        t += step
        hub.tick(t)
        collected += drain(session)
    return collected


def test_a_client_with_no_subscriptions_receives_no_data(hub, client):
    run(hub, 3.0)
    assert [m for m in drain(client) if m["type"] == "data"] == []


def test_subscribing_delivers_that_stream_and_only_that_stream(hub, client):
    hub.subscribe(client, [{"name": "vessel", "rate_hz": 5.0}])
    run(hub, 2.0)
    streams = {m["stream"] for m in drain(client) if m["type"] == "data"}
    assert streams == {"vessel"}


def test_the_negotiated_rate_is_the_rate_actually_delivered(hub, client):
    hub.subscribe(client, [{"name": "vessel", "rate_hz": 2.0}])
    drain(client)
    run(hub, 10.0)
    count = len([m for m in drain(client) if m["type"] == "data"])
    assert 16 <= count <= 24     # 2 Hz for 10 s, allowing for tick alignment


def test_a_client_asking_for_too_much_is_told_what_it_is_getting(hub, client):
    reply = hub.subscribe(client, [{"name": "vessel", "rate_hz": 50.0}])
    entry = reply["streams"][0]
    assert entry["granted"] and entry["rate_hz"] == 10.0
    assert "maximum" in entry["reason"]


def test_every_frame_carries_both_timestamps(hub, client):
    """source_utc_ms is when the value was produced; server_utc_ms is when it
    was sent. Data age depends entirely on keeping them apart."""
    hub.subscribe(client, [{"name": "vessel", "rate_hz": 5.0}])
    drain(client)
    run(hub, 1.0)
    frames = [m for m in drain(client) if m["type"] == "data"]
    assert frames
    for frame in frames:
        assert "source_utc_ms" in frame and "server_utc_ms" in frame
        assert frame["source_utc_ms"] <= frame["server_utc_ms"]


def test_unsubscribing_stops_the_stream(hub, client):
    hub.subscribe(client, [{"name": "vessel", "rate_hz": 5.0}])
    run(hub, 1.0)
    hub.unsubscribe(client, ["vessel"])
    drain(client)
    run(hub, 2.0)
    assert [m for m in drain(client) if m["type"] == "data"] == []


def test_forcing_a_profile_re_resolves_and_tells_the_client(hub, client):
    hub.subscribe(client, [{"name": "vessel", "rate_hz": 10.0},
                           {"name": "lidar", "rate_hz": 10.0}])
    drain(client)
    hub.set_profile(PROFILE_REDUCED)

    messages = [m for m in drain(client) if m["type"] == "subscribed"]
    assert messages
    by_name = {s["name"]: s for s in messages[-1]["streams"]}
    assert by_name["vessel"]["rate_hz"] == 2.0
    assert not by_name["lidar"]["granted"]


def test_a_stream_dropped_by_a_profile_stops_being_sent(hub, client):
    hub.subscribe(client, [{"name": "lidar", "rate_hz": 5.0}])
    run(hub, 1.0)
    hub.set_profile(PROFILE_MINIMAL)
    drain(client)
    run(hub, 3.0)
    assert [m for m in drain(client) if m.get("stream") == "lidar"] == []


def test_a_manual_profile_survives_automatic_selection(hub, client):
    """Auto-selection quietly overriding a human decision at the worst moment is
    how people learn to distrust automation."""
    hub.set_profile(PROFILE_MINIMAL)
    run(hub, 30.0)
    assert hub.selector.profile == PROFILE_MINIMAL
    assert hub.selector.manual


def test_releasing_a_manual_override_returns_to_automatic(hub, client):
    hub.set_profile(PROFILE_MINIMAL)
    assert hub.selector.manual
    hub.set_profile(None)
    assert not hub.selector.manual
    # Whether it then *recovers* depends on the link, which by this point in the
    # simulated survey is genuinely degrading as the vessel goes offshore. The
    # recovery logic itself is pinned down in test_link_profile.py.
    run(hub, 5.0)


def test_on_change_streams_are_not_resent_unchanged(hub, client):
    hub.subscribe(client, [{"name": "plan"}])
    run(hub, 5.0)
    plans = [m for m in drain(client) if m.get("stream") == "plan"]
    assert len(plans) == 1


def test_a_slow_client_loses_its_oldest_frames_not_the_newest(hub, client):
    """A stalled browser must never be able to stall the vessel's telemetry."""
    hub.subscribe(client, [{"name": "vessel", "rate_hz": 10.0}])
    run(hub, 60.0)                       # never drained
    assert client.dropped > 0
    assert client.outbox.qsize() <= ClientSession.QUEUE_LIMIT

    newest = None
    while not client.outbox.empty():
        newest = client.outbox.get_nowait()
    assert newest is not None


def test_a_mode_command_is_pending_then_confirmed_by_the_vessel(hub, client):
    result = hub.issue_command("set_mode", {"mode": "AUTONOMOUS"})
    assert result["status"] == "pending"

    run(hub, 3.0)
    results = [m for m in drain(client) if m["type"] == "command_result"]
    assert results and results[-1]["status"] == "confirmed"
    assert hub.source.state()["pico"]["mode"] == "AUTONOMOUS"


def test_an_invalid_mode_fails_at_once_rather_than_timing_out(hub, client):
    result = hub.issue_command("set_mode", {"mode": "SIDEWAYS"})
    assert result["status"] == "failed"


def test_cutting_propulsion_is_confirmed_by_mode_and_latch_together(hub, client):
    hub.issue_command("set_mode", {"mode": "AUTONOMOUS"})
    run(hub, 3.0)
    drain(client)

    assert hub.issue_command("cut_propulsion", {})["status"] == "pending"
    run(hub, 3.0)
    state = hub.source.state()["pico"]
    assert state["mode"] == "ESTOP" and state["estop_latched"]


def test_alarms_are_broadcast_on_transition_only(hub, client):
    drain(client)
    hub.issue_command("inject_fault", {"fault": "heading_invalid"})
    run(hub, 2.0)

    messages = [m for m in drain(client) if m["type"] == "alarms"]
    assert len(messages) == 1
    assert "heading_invalid" in [a["key"] for a in messages[0]["raised"]]

    run(hub, 3.0)
    assert [m for m in drain(client) if m["type"] == "alarms"] == []

    hub.issue_command("clear_fault", {"fault": "heading_invalid"})
    run(hub, 2.0)
    cleared = [m for m in drain(client) if m["type"] == "alarms"]
    assert cleared and "heading_invalid" in cleared[0]["cleared"]


def test_hello_describes_everything_a_client_needs_to_start(hub):
    session = ClientSession("hello")
    message = hub.add_client(session)
    assert message["type"] == "hello"
    assert "vessel" in message["streams"]
    assert message["profiles"] == ["full", "reduced", "minimal"]
    assert "alarm_thresholds" in message


def test_the_link_stream_carries_server_side_facts_the_source_cannot_know(hub, client):
    hub.subscribe(client, [{"name": "link", "rate_hz": 2.0}])
    drain(client)
    run(hub, 2.0)
    frames = [m for m in drain(client) if m.get("stream") == "link"]
    assert frames
    payload = frames[-1]["payload"]
    assert payload["profile"] in ("full", "reduced", "minimal")
    assert payload["connected_clients"] == 1
    assert payload["active_link"]


def test_a_granted_stream_that_produces_nothing_says_so(hub, client):
    """A subscription accepted and then silently empty is the worst of both
    worlds: the client believes it is being fed, and the operator reads the
    blank panel as 'nothing is happening'."""
    hub.subscribe(client, [{"name": "diagnostics", "rate_hz": 1.0}])
    drain(client)
    run(hub, 8.0)

    notices = [m for m in drain(client) if m["type"] == "stream_unavailable"]
    assert len(notices) == 1, "said once, not every tick"
    assert notices[0]["stream"] == "diagnostics"
    assert "not be running" in notices[0]["reason"]


def test_a_stream_that_does_produce_data_never_reports_unavailable(hub, client):
    hub.subscribe(client, [{"name": "vessel", "rate_hz": 2.0}])
    drain(client)
    run(hub, 10.0)
    assert [m for m in drain(client) if m["type"] == "stream_unavailable"] == []


def test_sonar_parameters_are_confirmed_by_the_sonar_not_by_sending(hub, client):
    """Same rule as vessel mode: a setting that never took effect must not read
    as applied. Surveying at the wrong range is not noticed until Windhoek."""
    result = hub.issue_command(
        "set_ping_parameters", {"range_m": 22.0, "gain": 6, "ping_rate_hz": 8.0}
    )
    assert result["status"] == "pending"

    run(hub, 2.0)
    results = [m for m in drain(client) if m["type"] == "command_result"]
    assert results and results[-1]["status"] == "confirmed"

    sonar = hub.source.state()["sonar"]
    assert sonar["range_setting_m"] == 22.0
    assert sonar["gain_setting"] == 6
    assert sonar["commanded_ping_rate_hz"] == 8.0


def test_a_sonar_setting_that_does_not_take_effect_fails(hub, client):
    """Ask for a rate the source will clamp, and the confirmation must not
    pretend it was applied."""
    hub.issue_command("set_ping_parameters", {"range_m": 22.0, "gain": 6, "ping_rate_hz": 0.0})
    run(hub, 5.0)
    results = [m for m in drain(client) if m["type"] == "command_result"]
    assert results and results[-1]["status"] == "failed"


def test_append_only_streams_send_increments_not_the_whole_history(hub, client):
    """Resending the whole coverage ribbon every frame reached 18 kB per frame
    after ninety seconds and would have been about a megabyte after three
    hours — the exact "works on the bench, collapses offshore" failure this
    whole design exists to prevent."""
    # Pin the profile: an automatic change is a legitimate resync, and this
    # test is about the steady state.
    hub.set_profile("full")
    hub.subscribe(client, [{"name": "coverage", "rate_hz": 2.0},
                           {"name": "track", "rate_hz": 2.0}])
    drain(client)
    frames = [m for m in run_collecting(hub, client, 40.0) if m["type"] == "data"]

    coverage = [m for m in frames if m["stream"] == "coverage"]
    assert len(coverage) > 5

    first, last = coverage[0], coverage[-1]
    assert first["payload"]["from"] == 0
    assert last["payload"]["from"] > 0, "later frames must start where the last ended"
    assert last["payload"]["total"] > len(last["payload"]["segments"])

    # The increment must not grow with the length of the mission.
    assert len(last["payload"]["segments"]) <= len(first["payload"]["segments"]) + 5


def test_increments_are_contiguous_so_the_client_can_splice_them(hub, client):
    hub.set_profile("full")
    hub.subscribe(client, [{"name": "track", "rate_hz": 2.0}])
    drain(client)
    frames = run_collecting(hub, client, 30.0)

    held = 0
    for message in [m for m in frames if m.get("stream") == "track"]:
        payload = message["payload"]
        assert payload["from"] == held, "a gap here would silently corrupt the track"
        held += len(payload["points"])


def test_a_profile_change_resyncs_rather_than_sending_an_unspliceable_increment(hub, client):
    """The detail level changes the decimation, so an increment computed at the
    old step cannot be appended to history built at the new one."""
    hub.set_profile("full")
    hub.subscribe(client, [{"name": "coverage", "rate_hz": 2.0}])
    run(hub, 20.0)
    drain(client)

    hub.set_profile(PROFILE_REDUCED)
    run(hub, 3.0)
    frames = [m for m in drain(client) if m.get("stream") == "coverage"]
    assert frames and frames[0]["payload"]["from"] == 0


def test_coverage_gaps_survive_onto_the_wire(hub, client):
    """A gap that renders as filled claims seabed nobody ensonified. With one
    sonar unit covering one side, that is the most expensive lie available."""
    hub.set_profile("full")
    hub.issue_command("inject_fault", {"fault": "sonar_dropout"})
    hub.subscribe(client, [{"name": "coverage", "rate_hz": 2.0}])
    drain(client)
    frames = run_collecting(hub, client, 20.0)

    segments = []
    for message in [m for m in frames if m.get("stream") == "coverage"]:
        segments += message["payload"]["segments"]
    assert any(s is None for s in segments), "the dropout must show as a gap"

    # A dropout is a CONTIGUOUS run of gaps, which is what distinguishes it on
    # the map from the single-sample gaps a turn produces. A ribbon that closed
    # over it would claim seabed nobody ensonified.
    longest = current = 0
    for segment in segments:
        current = current + 1 if segment is None else 0
        longest = max(longest, current)
    assert longest >= 3, f"the dropout closed over: longest gap run was {longest}"


def test_coverage_is_painted_only_on_the_ensonified_side(hub, client):
    """One sonar unit means one side. Painting both would hide every gap."""
    hub.set_profile("full")
    hub.subscribe(client, [{"name": "coverage", "rate_hz": 2.0}])
    drain(client)
    frames = run_collecting(hub, client, 20.0)

    payloads = [m["payload"] for m in frames if m.get("stream") == "coverage"]
    assert payloads
    assert payloads[0]["side"] == "starboard"

    # Every sample carries a positive half-width; the side is applied once, in
    # the envelope, so a per-sample sign error cannot flip part of the ribbon.
    for payload in payloads:
        for segment in payload["segments"]:
            if segment is not None:
                assert segment[3] > 0, "half width must be positive"


def test_coverage_stops_when_the_heading_goes_invalid(hub, client):
    """Data recorded without a trustworthy heading cannot be georeferenced, so
    painting it as covered would be a lie the operator acts on."""
    hub.set_profile("full")
    hub.subscribe(client, [{"name": "coverage", "rate_hz": 2.0}])
    drain(client)
    run_collecting(hub, client, 10.0)

    hub.issue_command("inject_fault", {"fault": "heading_invalid"})
    frames = run_collecting(hub, client, 15.0, start=1010.0)

    segments = []
    for message in [m for m in frames if m.get("stream") == "coverage"]:
        segments += message["payload"]["segments"]
    assert segments, "no coverage samples arrived at all"
    assert segments[-1] is None, "coverage must stop while the heading is invalid"


# -- recording ------------------------------------------------------------


def test_recording_is_confirmed_by_bytes_landing_not_by_the_request(hub, client):
    """'Recording' on screen must mean the recorder reports it is writing, not
    that a start request was accepted."""
    result = hub.issue_command("start_mission", {"name": "namibia"})
    assert result["status"] == "pending"

    run(hub, 2.0)
    results = [m for m in drain(client) if m["type"] == "command_result"]
    assert results and results[-1]["status"] == "confirmed"

    mission = hub.source.state()["mission"]
    assert mission["state"] == "RECORDING"
    assert mission["name"] == "namibia"


def test_a_recorded_mission_appears_in_the_list_and_is_complete(hub, client):
    hub.issue_command("start_mission", {"name": "listed"})
    run(hub, 5.0)
    hub.issue_command("stop_mission", {})
    run(hub, 2.0)

    missions = hub.source.state()["mission"]["missions"]
    assert [m["name"] for m in missions] == ["listed"]
    assert missions[0]["complete"] is True
    assert missions[0]["size_bytes"] > 0


def test_export_is_refused_while_recording(hub, client):
    hub.issue_command("start_mission", {"name": "busy"})
    run(hub, 3.0)
    result = hub.issue_command(
        "export_mission", {"path": hub.source.recorder.status.mission_dir,
                           "destination": "/tmp"}
    )
    assert result["status"] == "failed"
    assert "recording" in result["detail"]


def test_deleting_without_confirmation_is_refused(hub, client):
    result = hub.issue_command("delete_mission", {"path": "/tmp/whatever"})
    assert result["status"] == "failed"
    assert "confirmation" in result["detail"]


def test_the_disk_full_fault_stops_recording_and_raises_an_alarm(hub, client):
    hub.issue_command("start_mission", {"name": "diskfull"})
    run(hub, 3.0)
    drain(client)

    hub.issue_command("inject_fault", {"fault": "disk_full"})
    run(hub, 5.0)

    mission = hub.source.state()["mission"]
    assert mission["state"] == "ERROR"
    assert "left" in mission["error_message"]

    # The recorder is no longer "recording", so the pre-emptive disk_low alarm
    # has gone quiet — which is exactly why there is a separate alarm for a
    # recorder that stopped.
    alarms = [m for m in drain(client) if m["type"] == "alarms"]
    keys = {a["key"] for m in alarms for a in m["active"]}
    assert "recording_stopped" in keys


# -- pre-flight -----------------------------------------------------------


def test_the_preflight_runs_and_reaches_the_client(hub, client):
    hub.subscribe(client, [{"name": "diagnostics", "rate_hz": 1.0}])
    drain(client)
    hub.issue_command("run_system_test", {})
    frames = run_collecting(hub, client, 4.0)

    reports = [m for m in frames if m.get("stream") == "diagnostics"]
    assert reports
    payload = reports[-1]["payload"]
    assert "go" in payload and "summary" in payload
    assert payload["items"], "a report with no items is not a report"
    for item in payload["items"]:
        assert item["status"] in ("PASS", "WARN", "FAIL", "SKIPPED")
        assert item["message"]


def test_a_gnss_fault_turns_the_verdict_no_go_with_an_actionable_message(hub, client):
    hub.issue_command("inject_fault", {"fault": "gnss_degraded"})
    run(hub, 2.0)
    hub.issue_command("run_system_test", {})
    run(hub, 1.0)

    report = hub.source.last_report
    assert not report.go
    gnss = next(i for i in report.items if i.id == "gnss.fix")
    assert gnss.status == "FAIL"
    assert "satellites" in gnss.message
    assert gnss.remedy


def test_clock_drift_is_caught_by_the_preflight(hub, client):
    hub.issue_command("inject_fault", {"fault": "clock_drift"})
    run(hub, 40.0)
    hub.issue_command("run_system_test", {})
    run(hub, 1.0)

    clock = next(i for i in hub.source.last_report.items if i.id == "sonar.clock")
    assert clock.status == "FAIL"
    assert "un-georeferenceable" in clock.remedy


# -- the Jetson fault: a profile degraded before anybody connected ----------
#
# First run on real hardware: the page loaded, the socket upgraded, pings
# answered, and no data ever arrived. Three defects compounded, and all three
# are reproduced here because each on its own is survivable and together they
# are silent.


class _LinkOnlySource(SimSource):
    """A SimSource driven the way ``gui_backend_node`` drives ``RosSource``.

    The node measures the shore link once a second and reports
    ``quality = 1.0 if clients else 0.0``. That is the number that broke it.

    ``set_link_measurement`` exists on ``RosSource`` and not on ``SimSource``,
    which is part of why none of this showed up in sim: the sim path has no
    equivalent of the node's 1 Hz measurement at all, so automatic profile
    selection — the thing that runs in the field — was exercised by nothing.
    """

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._node_link = None

    def feed_node_measurement(self, clients: int) -> None:
        self._node_link = adapters.link_from_measurements(
            "wifi" if clients else "none", 1.0 if clients else 0.0, 0.0, 800_000.0
        )

    def state(self) -> dict:
        out = super().state()
        if self._node_link is not None:
            out["link_sample"] = self._node_link
        return out


def _node_like_hub():
    return Hub(_LinkOnlySource(SimWorld(WorldConfig()), time_scale=10.0))


def test_an_idle_server_does_not_degrade_its_own_profile():
    """The root cause.

    Nobody connected means the link is UNKNOWN, not bad. Degrading it serves
    nobody — there is no client to protect — and the only thing it can affect
    is the next client to arrive, which is exactly the damage it did.
    """
    hub = _node_like_hub()
    t = 1000.0
    for i in range(600):          # 30 s at 20 Hz, no clients
        t += 0.05
        if i % 20 == 0:
            hub.source.feed_node_measurement(clients=0)
        hub.tick(t)

    assert hub.selector.profile == "full", (
        f"an idle server talked itself down to {hub.selector.profile!r} "
        f"({hub.selector.reason}) before any browser opened"
    )


def test_the_first_browser_to_connect_gets_what_it_asked_for():
    """What the operator actually saw: a blank page on a healthy vessel."""
    hub = _node_like_hub()
    t = 1000.0
    for i in range(600):
        t += 0.05
        if i % 20 == 0:
            hub.source.feed_node_measurement(clients=0)
        hub.tick(t)

    session = ClientSession("browser")
    hub.add_client(session)
    message = hub.subscribe(session, [
        {"name": "vessel", "rate_hz": 5},
        {"name": "heading", "rate_hz": 2},
        {"name": "lidar", "rate_hz": 5},
        {"name": "sonar", "rate_hz": 1},
    ])
    refused = [s["name"] for s in message["streams"] if not s["granted"]]
    assert not refused, f"refused {refused} on a link nobody had even tried"


def test_a_denied_stream_is_remembered_and_comes_back():
    """A stream dropped from the session is a stream the server has forgotten
    the client ever wanted, so a recovering link has nothing to give back."""
    hub = _node_like_hub()
    session = ClientSession("browser")
    hub.add_client(session)

    hub.selector.force(PROFILE_MINIMAL)
    hub.subscribe(session, [{"name": "lidar", "rate_hz": 5}])
    assert "lidar" in session.subscriptions, "the request was forgotten outright"
    assert not session.subscriptions["lidar"].resolution.granted

    session.resubscribe("full")
    assert session.subscriptions["lidar"].resolution.granted, (
        "the link recovered and lidar never came back"
    )


def test_a_clamped_rate_is_not_permanent():
    """The ratchet: every degradation used to be forever, because the only rate
    still recorded anywhere was the clamped one."""
    hub = _node_like_hub()
    session = ClientSession("browser")
    hub.add_client(session)

    hub.subscribe(session, [{"name": "vessel", "rate_hz": 5}])
    assert session.subscriptions["vessel"].resolution.rate_hz == 5.0

    session.resubscribe(PROFILE_MINIMAL)
    clamped = session.subscriptions["vessel"].resolution.rate_hz
    assert clamped < 5.0, "the minimal profile should clamp this"

    session.resubscribe("full")
    assert session.subscriptions["vessel"].resolution.rate_hz == 5.0, (
        "recovery restored the clamped rate, not the requested one"
    )


def test_a_round_trip_through_every_profile_ends_where_it_started():
    """Degrade and recover repeatedly; the client must end up whole."""
    hub = _node_like_hub()
    session = ClientSession("browser")
    hub.add_client(session)

    asked = [
        {"name": "vessel", "rate_hz": 5}, {"name": "pico", "rate_hz": 2},
        {"name": "heading", "rate_hz": 2}, {"name": "lidar", "rate_hz": 5},
        {"name": "sonar", "rate_hz": 1}, {"name": "mission", "rate_hz": 1},
    ]
    hub.subscribe(session, asked)
    before = {
        n: s.resolution.rate_hz for n, s in session.subscriptions.items()
    }

    for profile in (PROFILE_REDUCED, PROFILE_MINIMAL, PROFILE_REDUCED, "full"):
        session.resubscribe(profile)

    after = {n: s.resolution.rate_hz for n, s in session.subscriptions.items()}
    assert after == before, "a link that recovered did not give back what it took"


# -- and when it does go wrong, it says so ---------------------------------


def test_a_client_getting_nothing_is_told_so(caplog):
    """The failure that took an hour to notice: a page that renders, a socket
    that stays up, pings that answer, and no data at all. It now says so on the
    page and in the node's log, because the operator and whoever reads the log
    can each see only half of the evidence."""
    hub = _node_like_hub()
    session = ClientSession("browser")
    hub.add_client(session)
    hub.selector.force(PROFILE_MINIMAL)
    hub.subscribe(session, [{"name": "lidar", "rate_hz": 5}])   # refused here
    drain(session)

    with caplog.at_level("WARNING"):
        run(hub, Hub.STARVED_AFTER_S + 2.0)

    notices = [m for m in drain(session) if m.get("type") == "notice"]
    assert notices, "the page was never told it is receiving nothing"
    assert notices[0]["code"] == "no_streams"
    # The reason has to name the profile: "no data" alone sends somebody to
    # check cables when the answer is in the link profile.
    assert "minimal" in notices[0]["detail"]
    assert any("receiving nothing" in r.getMessage() for r in caplog.records), (
        "nothing in the server log"
    )


def test_the_notice_is_sent_once_and_re_arms_on_recovery():
    """An alarm repeated every tick is an alarm nobody reads."""
    hub = _node_like_hub()
    session = ClientSession("browser")
    hub.add_client(session)
    hub.selector.force(PROFILE_MINIMAL)
    hub.subscribe(session, [{"name": "lidar", "rate_hz": 5}])

    run(hub, Hub.STARVED_AFTER_S + 2.0)
    first = [m for m in drain(session) if m.get("type") == "notice"]
    assert len(first) == 1

    run(hub, 10.0)
    assert not [m for m in drain(session) if m.get("type") == "notice"]

    # Link recovers: the stream comes back, and a later starvation would be
    # reported on its own account rather than suppressed by the earlier one.
    session.resubscribe("full")
    run(hub, 1.0)
    assert session.reported_starved is False


def test_a_healthy_client_is_never_accused_of_starving():
    hub = _node_like_hub()
    session = ClientSession("browser")
    hub.add_client(session)
    hub.subscribe(session, [{"name": "vessel", "rate_hz": 5}])

    collected = run_collecting(hub, session, Hub.STARVED_AFTER_S + 5.0)
    assert not [m for m in collected if m.get("type") == "notice"]
