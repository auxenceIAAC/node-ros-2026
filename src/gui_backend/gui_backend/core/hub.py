"""The broadcaster.

Owns the clients, their subscriptions, and the decision about what actually goes
out. It is the place where "the backend must never push everything by default"
stops being a principle and becomes code.

Shape of the loop, once per tick:

1. advance the source;
2. resolve alarms and any command confirmations;
3. for each client, for each subscribed stream that is **due**, take a snapshot
   at that client's negotiated detail level and enqueue it.

Decimation is therefore server-side and per client, which is the only place it
can be: two operators on different links must be able to watch the same vessel
at different rates.

Backpressure: each client has a bounded outbound queue. A client that cannot
keep up loses its oldest frames, not the newest, and is told how many. A stalled
browser must never be able to stall the vessel's telemetry.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass

from . import alarms as alarms_mod
from .commands import (
    CMD_CUT_PROPULSION,
    CMD_SET_MODE,
    CMD_SET_PING_PARAMETERS,
    CMD_SET_PROFILE,
    CMD_START_MISSION,
    CMD_STOP_MISSION,
    CommandManager,
    STATUS_FAILED,
    mode_confirmed,
    ping_parameters_confirmed,
    propulsion_cut_confirmed,
    recording_confirmed,
)
from . import video_frame
from .link_profile import ProfileSelector
from .shaper import LinkShaper
from .source import DataSource
from .streams import (
    PROFILE_ORDER,
    STREAMS,
    Resolution,
    estimate_bytes_per_s,
    resolve,
)

PROTOCOL_VERSION = 1

#: Surfaces through ROS as well: the node runs uvicorn in a thread and
#: stdlib logging reaches the same stderr that `output="screen"` captures.
_log = logging.getLogger(__name__)


@dataclass
class Subscription:
    resolution: Resolution
    #: What the client actually asked for, kept verbatim and never overwritten
    #: by what a degraded profile allowed.
    #:
    #: Without this the clamp ratchets: a client that asked for vessel at 5 Hz
    #: and was held to 0.2 Hz by the `minimal` profile came back at 0.2 Hz when
    #: the link recovered, because the only rate still recorded anywhere was
    #: the clamped one. Every degradation was permanent.
    requested_rate_hz: float | None = None
    requested_detail: str | None = None
    #: When this stream is next due, on the hub's tick clock. Zero means "due
    #: immediately" and is deliberately origin-independent: a subscription
    #: arriving before the first tick must not be scheduled against a clock
    #: that has not started yet.
    next_due_s: float = 0.0
    #: For on-change streams, the payload last sent, so we can skip repeats.
    last_payload_hash: int | None = None
    sent: int = 0
    #: Hub-clock time this subscription first came up for delivery, stamped on
    #: the first tick rather than at subscribe time so it cannot be recorded
    #: against a clock that has not started yet.
    first_tick_s: float | None = None
    #: Whether the client has already been told this stream produces nothing.
    reported_unavailable: bool = False
    #: Position in an append-only stream. Reset to 0 on a profile change so the
    #: client gets a full resync at the new detail level rather than an
    #: increment it cannot splice onto what it already has.
    cursor: int = 0


class ClientSession:
    """One browser."""

    #: Small on purpose. A client more than a couple of seconds behind is not
    #: going to catch up, and holding frames for it just makes what it does
    #: eventually see older.
    QUEUE_LIMIT = 64

    def __init__(self, client_id: str, remote: str = "", shaper: LinkShaper | None = None) -> None:
        self.id = client_id
        self.remote = remote
        #: Outbound shaping. Off unless the backend was started in sim with
        #: shaping enabled; RosSource never turns it on.
        self.shaper = shaper or LinkShaper(enabled=False)
        self.subscriptions: dict[str, Subscription] = {}
        self.outbox: asyncio.Queue[dict] = asyncio.Queue(maxsize=self.QUEUE_LIMIT)
        #: Video has its own queue, one frame deep, and its own writer.
        #:
        #: Not tidiness. The telemetry outbox holds 64 messages and evicts the
        #: oldest when it fills; a 45 kB camera frame occupies one slot exactly
        #: as a 200-byte `pico` frame does, but takes hundreds of times longer
        #: to write. Sharing the queue means that when the link degrades, the
        #: eviction falls on *telemetry* — the boat's position and mode go off
        #: the screen so that a picture can arrive, which is precisely
        #: backwards.
        #:
        #: Depth one, newest wins, so at most a single frame is ever in flight
        #: and a backlog cannot form. A slow link then costs dropped frames,
        #: which is what a slow link should cost.
        self.video_outbox: asyncio.Queue[bytes] = asyncio.Queue(maxsize=1)
        self.video_dropped = 0
        self.dropped = 0
        self.connected_at_s = time.monotonic()
        self.rtt_ms: float | None = None
        self.last_pong_s: float = time.monotonic()
        self.bytes_estimate_per_s = 0.0
        #: Hub-clock time this client was first seen by a tick. Stamped on
        #: the tick rather than at construction so starvation is measured
        #: on the same clock as everything else in this file.
        self.first_tick_s: float | None = None
        #: Whether this client has already been told it is getting nothing.
        self.reported_starved: bool = False

    def send(self, message: dict) -> None:
        """Enqueue, dropping the oldest frame if the client is behind."""
        try:
            self.outbox.put_nowait(message)
        except asyncio.QueueFull:
            try:
                self.outbox.get_nowait()
                self.outbox.put_nowait(message)
            except (asyncio.QueueEmpty, asyncio.QueueFull):
                pass
            self.dropped += 1

    def send_video(self, frame: bytes) -> None:
        """Enqueue a frame, discarding any older one still waiting.

        The opposite policy to :meth:`send`. Telemetry keeps a short history
        because a position from two seconds ago is still worth having; a
        picture from two seconds ago is worth less than the one behind it, and
        showing it would be the lie this panel exists to avoid.
        """
        if self.video_outbox.full():
            try:
                self.video_outbox.get_nowait()
                self.video_dropped += 1
            except asyncio.QueueEmpty:
                pass
        try:
            self.video_outbox.put_nowait(frame)
        except asyncio.QueueFull:
            self.video_dropped += 1

    def resubscribe(self, profile: str, now_s: float = 0.0) -> list[Resolution]:
        """Re-resolve every subscription against a new profile.

        Called on a profile change. Rates and detail levels move; what the
        client asked for originally is preserved, so a link that recovers gives
        back what it took away.
        """
        results = []
        for name, sub in list(self.subscriptions.items()):
            new = resolve(name, sub.requested_rate_hz, sub.requested_detail, profile)
            was_granted = sub.resolution.granted
            sub.resolution = new
            if new.granted:
                sub.next_due_s = 0.0   # deliver once immediately at the new rate
                sub.cursor = 0         # resync: the detail level may have changed
                if not was_granted:
                    # Coming back from a denial: let it report going quiet again
                    # on its own merits rather than staying silent because it
                    # once did.
                    sub.first_tick_s = None
                    sub.reported_unavailable = False
            results.append(new)
        self.bytes_estimate_per_s = estimate_bytes_per_s(
            [s.resolution for s in self.subscriptions.values()]
        )
        return results


class Hub:
    def __init__(
        self,
        source: DataSource,
        tick_hz: float = 20.0,
        selector: ProfileSelector | None = None,
        commands: CommandManager | None = None,
        thresholds: alarms_mod.AlarmThresholds | None = None,
    ) -> None:
        self.source = source
        self.tick_interval_s = 1.0 / tick_hz
        self.selector = selector or ProfileSelector()
        self.commands = commands or CommandManager()
        self.thresholds = thresholds or alarms_mod.AlarmThresholds()

        self.clients: dict[str, ClientSession] = {}
        # One clock for the whole hub. tick() sets it; everything else reads it.
        # Mixing an injected tick clock with time.monotonic() elsewhere makes
        # scheduling depend on process uptime, which is untestable and, on a
        # Jetson that has been up for a week, subtly wrong.
        self._now_s: float = time.monotonic()
        self._active_alarms: list[alarms_mod.Alarm] = []
        self._running = False
        self._task: asyncio.Task | None = None
        #: Ticks that raised. Non-zero means streams are being lost.
        self._tick_failures = 0
        self._tick_failures_reported = 0

    # -- clients ----------------------------------------------------------

    def add_client(self, session: ClientSession) -> dict:
        self.clients[session.id] = session
        return self.hello(session)

    def remove_client(self, client_id: str) -> None:
        self.clients.pop(client_id, None)

    def hello(self, session: ClientSession) -> dict:
        """The first message a client gets. Everything it needs to start."""
        return {
            "type": "hello",
            "protocol_version": PROTOCOL_VERSION,
            "client_id": session.id,
            "server_utc_ms": self.source.now_utc_ms(),
            "profile": self.selector.to_dict(),
            "profiles": list(PROFILE_ORDER),
            "streams": {
                name: {
                    "description": spec.description,
                    "default_rate_hz": spec.default_rate_hz,
                    "max_rate_hz": spec.max_rate_hz,
                    "critical": spec.critical,
                    "on_change_only": spec.on_change_only,
                }
                for name, spec in STREAMS.items()
            },
            "source": self.source.describe(),
            "alarm_thresholds": vars(self.thresholds),
        }

    # -- subscription -----------------------------------------------------

    def subscribe(self, session: ClientSession, requests: list[dict]) -> dict:
        results = []
        for req in requests:
            name = req.get("name", "")
            res = resolve(
                name, req.get("rate_hz"), req.get("detail"), self.selector.profile
            )
            # Kept even when refused. A stream dropped from the session is a
            # stream the server has forgotten the client ever wanted, so a
            # recovering link has nothing to give back — which is how eight of
            # twelve streams stayed dark on the Jetson for as long as the page
            # was open. Denied subscriptions are inert: `_push_streams` skips
            # anything not granted, and `estimate_bytes_per_s` ignores it.
            if name in STREAMS:
                session.subscriptions[name] = Subscription(
                    res,
                    requested_rate_hz=req.get("rate_hz"),
                    requested_detail=req.get("detail"),
                )
            else:
                session.subscriptions.pop(name, None)
            results.append(res)

        session.bytes_estimate_per_s = estimate_bytes_per_s(
            [s.resolution for s in session.subscriptions.values()]
        )
        return self._subscription_message(session, results)

    def unsubscribe(self, session: ClientSession, names: list[str]) -> dict:
        for name in names:
            session.subscriptions.pop(name, None)
        session.bytes_estimate_per_s = estimate_bytes_per_s(
            [s.resolution for s in session.subscriptions.values()]
        )
        return self._subscription_message(
            session, [s.resolution for s in session.subscriptions.values()]
        )

    def _subscription_message(self, session: ClientSession, results) -> dict:
        return {
            "type": "subscribed",
            "server_utc_ms": self.source.now_utc_ms(),
            "profile": self.selector.to_dict(),
            # Every resolution carries its reason. A client that asked for 10 Hz
            # and is getting 0.2 Hz must be able to say so on screen.
            "streams": [
                {
                    "name": r.stream,
                    "granted": r.granted,
                    "rate_hz": r.rate_hz,
                    "detail": r.detail,
                    "reason": r.reason,
                }
                for r in results
            ],
            "estimated_bytes_per_s": round(session.bytes_estimate_per_s),
        }

    # -- profile ----------------------------------------------------------

    def set_profile(self, profile: str | None) -> dict:
        """``None`` releases a manual override back to automatic."""
        now_s = self._now_s
        if profile is None:
            self.selector.release()
        else:
            self.selector.force(profile)
        for session in self.clients.values():
            results = session.resubscribe(self.selector.profile, now_s)
            session.send(self._subscription_message(session, results))
        return self.selector.to_dict()

    def _auto_select(self, now_s: float) -> None:
        # Nobody connected means the link is UNKNOWN, not bad.
        #
        # This guard is the fix for a fault that made the GUI unusable on the
        # Jetson. `gui_backend_node` measures the shore link once a second and
        # reports `quality = 1.0 if clients else 0.0`; with no client attached
        # that 0.0 reached the selector as `connected=False`, which targets the
        # `minimal` profile. Two seconds after the node started — long before
        # anybody opened a browser — the profile had degraded to `minimal` on
        # the strength of nothing having connected yet.
        #
        # The first client then subscribed under that profile and had eight of
        # its twelve streams refused outright. Degrading a link nobody is using
        # serves nobody: the only thing it can affect is the *next* client to
        # arrive, which is precisely the damage. Profile selection is about
        # serving clients, so with none there is nothing to select for and the
        # last real measurement stands.
        if not self.clients:
            return

        state = self.source.state()
        link = state.get("link_sample")
        if link is None:
            return

        # Shape the wire to whatever the simulated bearer currently is, so the
        # link genuinely narrows as the vessel goes offshore rather than merely
        # being described as narrow.
        for session in self.clients.values():
            if session.shaper.enabled:
                session.shaper.update(
                    capacity_bytes_per_s=link.capacity_bytes_per_s,
                    rtt_ms=link.rtt_ms,
                    loss_ratio=max(0.0, 0.15 * (1.0 - link.quality)),
                )
        # Prefer a measured client round trip over the simulated bearer figure:
        # what the operator's laptop experiences is the thing that matters.
        rtts = [c.rtt_ms for c in self.clients.values() if c.rtt_ms is not None]
        rtt = min(rtts) if rtts else link.rtt_ms
        if self.selector.update(rtt, link.quality, now_s, connected=link.quality > 0.0):
            for session in self.clients.values():
                results = session.resubscribe(self.selector.profile, now_s)
                session.send(self._subscription_message(session, results))
            self.broadcast(
                {
                    "type": "profile",
                    "server_utc_ms": self.source.now_utc_ms(),
                    **self.selector.to_dict(),
                }
            )

    # -- commands ---------------------------------------------------------

    def issue_command(self, name: str, args: dict, command_id: str | None = None) -> dict:
        """Send a command. Returns its *pending* state, never a success."""
        now_utc = self.source.now_utc_ms()

        if name == CMD_SET_PROFILE:
            profile = args.get("profile")
            self.set_profile(None if profile in (None, "auto") else profile)
            cmd = self.commands.issue(name, args, now_utc, command_id=command_id)
            return cmd.to_dict()

        confirm = None
        if name == CMD_SET_MODE:
            confirm = mode_confirmed(str(args.get("mode", "")).upper())
        elif name == CMD_CUT_PROPULSION:
            confirm = propulsion_cut_confirmed
        elif name == CMD_SET_PING_PARAMETERS:
            # The sonar is the authority on its own settings, exactly as the
            # Pico is on the vessel's mode. A range the operator asked for that
            # never took effect must show as failed, not as applied — silently
            # surveying at the wrong range is a failure nobody notices until
            # the data is opened back home.
            confirm = ping_parameters_confirmed(
                float(args.get("range_m", 0.0)),
                int(args.get("gain", 0)),
                float(args.get("ping_rate_hz", 0.0)),
            )
        elif name in (CMD_START_MISSION, CMD_STOP_MISSION):
            # Confirmed by the recorder's own reported state, not by the call
            # returning. "Recording" on screen has to mean bytes are landing on
            # the disk, not that a request was accepted.
            confirm = recording_confirmed(name == CMD_START_MISSION)

        cmd = self.commands.issue(name, args, now_utc, confirm=confirm, command_id=command_id)
        outcome = self.source.send_command(name, args)
        if not outcome.accepted:
            self.commands.fail(cmd, outcome.detail or "rejected", now_utc)
        elif cmd.status != STATUS_FAILED and confirm is not None:
            cmd.detail = outcome.detail or "sent, waiting for the vessel to confirm"
        return cmd.to_dict()

    # -- the loop ---------------------------------------------------------

    def tick(self, now_s: float | None = None) -> None:
        """One pass of the broadcast loop. Never raises.

        The guard is here rather than in :meth:`run` so that every caller gets
        the property, and because a hub that stops ticking stops being a hub:
        this loop had no guard at all, so one exception under it ended the
        asyncio task outright. asyncio does not surface a dead task's exception
        until it is garbage collected, uvicorn at ``log_level=warning`` never
        showed it, and the per-connection ping task kept answering — so the
        socket stayed up, looked healthy, and carried nothing. It took a real
        Gazebo fix to trigger: ``int(None)`` on a satellite count that is
        absent by design.

        Catching broadly is deliberate. The alternative is that the *next*
        unanticipated conversion error also takes the vessel's telemetry down
        without a word, and nobody on a beach can debug what was never
        reported. :meth:`_push_streams` isolates failures per stream first, so
        this backstop only catches the rest of the tick.
        """
        self._now_s = now_s if now_s is not None else time.monotonic()
        now_s = self._now_s
        try:
            self.source.step(now_s)
            now_utc = self.source.now_utc_ms()

            self._auto_select(now_s)
            self._update_commands(now_utc)
            self._update_alarms(now_utc)
            self._push_streams(now_s, now_utc)
            self._report_starved_clients(now_s, now_utc)
        except Exception:  # noqa: BLE001 - see the docstring
            self._tick_failures += 1
            self._report_tick_failure()

    def _update_commands(self, now_utc: int) -> None:
        state = self.source.state()
        for cmd in self.commands.update(state, now_utc):
            self.broadcast(
                {"type": "command_result", "server_utc_ms": now_utc, **cmd.to_dict()}
            )

    def _update_alarms(self, now_utc: int) -> None:
        current = alarms_mod.evaluate(self.source.alarm_state(), self.thresholds)
        raised, cleared = alarms_mod.diff(self._active_alarms, current)
        self._active_alarms = current
        if not raised and not cleared:
            return
        self.broadcast(
            {
                "type": "alarms",
                "server_utc_ms": now_utc,
                "raised": [a.to_dict() for a in raised],
                "cleared": cleared,
                "active": [a.to_dict() for a in current],
            }
        )

    def _push_streams(self, now_s: float, now_utc: int) -> None:
        for session in self.clients.values():
            for name, sub in session.subscriptions.items():
                if not sub.resolution.granted:
                    continue
                if sub.first_tick_s is None:
                    sub.first_tick_s = now_s
                try:
                    self._push_one(session, name, sub, now_s, now_utc)
                except Exception:  # noqa: BLE001
                    # Isolated per stream so that one bad conversion costs one
                    # stream, not the whole broadcast. A malformed lidar scan
                    # must not be able to take the vessel's position off the
                    # screen — they have nothing to do with each other, and on
                    # a beach that distinction is the difference between a
                    # degraded panel and a blank one.
                    self._tick_failures += 1
                    self._report_tick_failure()

    def _push_one(
        self,
        session: ClientSession,
        name: str,
        sub: Subscription,
        now_s: float,
        now_utc: int,
    ) -> None:
        spec = STREAMS[name]

        if name == "camera":
            self._push_camera(session, sub, now_s, now_utc)
            return

        if spec.on_change_only:
            sample = self.source.snapshot(name, sub.resolution.detail, sub.cursor)
            if sample is None:
                self._report_unavailable(session, name, sub, now_s, now_utc)
                return
            digest = hash(repr(sample.payload))
            if digest == sub.last_payload_hash:
                return
            sub.last_payload_hash = digest
        else:
            if sub.resolution.rate_hz <= 0.0 or now_s < sub.next_due_s:
                return
            sub.next_due_s = now_s + 1.0 / sub.resolution.rate_hz
            sample = self.source.snapshot(name, sub.resolution.detail, sub.cursor)
            if sample is None:
                self._report_unavailable(session, name, sub, now_s, now_utc)
                return

        if name == "link":
            sample.payload.update(self._link_context(session))

        if sample.cursor is not None:
            sub.cursor = sample.cursor

        sub.sent += 1
        session.send(
            {
                "type": "data",
                "stream": name,
                # When it was PRODUCED. The client renders age from
                # this, so a slow link shows as stale data instead of
                # as fresh data that happens to be wrong.
                "source_utc_ms": sample.source_utc_ms,
                # When it was SENT, so the client can estimate clock
                # skew and not depend on the laptop's clock being right.
                "server_utc_ms": now_utc,
                "detail": sub.resolution.detail,
                "payload": sample.payload,
            }
        )

    def _push_camera(
        self, session: ClientSession, sub: Subscription, now_s: float, now_utc: int
    ) -> None:
        """Pixels, or a reason there are none.

        Unlike every other stream, this one sends something on *every* due
        tick whether or not there is a picture. ``_report_unavailable`` is the
        right answer elsewhere — a stream that has nothing to say should say
        nothing and let the panel age out. It is the wrong answer here,
        because the panel has to tell a vessel that has stopped sending from a
        link that has stopped carrying, and from the shore those look
        identical. The status frame is eighty bytes and it is the whole
        difference.
        """
        if sub.resolution.rate_hz <= 0.0 or now_s < sub.next_due_s:
            return
        sub.next_due_s = now_s + 1.0 / sub.resolution.rate_hz

        sample = self.source.snapshot("camera", sub.resolution.detail, sub.cursor)
        if sample is None:
            # The source does not produce this stream at all — no camera
            # configured, or a RosSource with nothing subscribed. That is a
            # different silence again, and the ordinary machinery words it.
            self._report_unavailable(session, "camera", sub, now_s, now_utc)
            return

        payload = sample.payload
        image = payload.get("image") or b""
        if image:
            header = video_frame.frame_header(
                seq=payload["seq"],
                source_utc_ms=sample.source_utc_ms,
                server_utc_ms=now_utc,
                width=payload["width"],
                height=payload["height"],
                image_format=payload["format"],
                size_bytes=len(image),
                detail=sub.resolution.detail,
                rate_hz=sub.resolution.rate_hz,
            )
            sub.sent += 1
        else:
            header = video_frame.status_header(
                reason=payload.get("reason", video_frame.REASON_NOT_STARTED),
                server_utc_ms=now_utc,
                rate_hz=sub.resolution.rate_hz,
                last_frame_utc_ms=payload.get("last_frame_utc_ms"),
                frames_produced=payload.get("frames_produced", 0),
            )
        session.send_video(video_frame.encode(header, image))

    def _report_tick_failure(self) -> None:
        """Log the traceback and tell every client, without flooding either.

        Rate-limited by powers of ten rather than by a clock: the first failure
        carries a full traceback, then the tenth, the hundredth and so on. A
        fault that fires twenty times a second stays one line in the log, and a
        fault that fires once still gets one.
        """
        count = self._tick_failures
        if count not in (1, 10, 100, 1000) and count % 10_000:
            return
        self._tick_failures_reported = count
        _log.exception(
            "hub tick raised (%d time(s)); streams are being dropped. "
            "The loop is still running — this is reported rather than fatal.",
            count,
        )
        self.broadcast(
            {
                "type": "notice",
                "server_utc_ms": self.source.now_utc_ms(),
                "severity": "alarm",
                "code": "tick_failed",
                "text": "The server is failing to assemble telemetry.",
                "detail": (
                    f"{count} tick(s) have raised. Data may be missing or "
                    "stale; the server log has the traceback."
                ),
            }
        )

    #: How long a client may hold a socket with nothing granted before we say so.
    #:
    #: A connected client receiving nothing is the hardest failure in this
    #: system to notice, because it looks exactly like a quiet vessel: the page
    #: renders, the socket stays up, the pings answer. On the Jetson it took an
    #: hour to even see. Thirty seconds is long enough that a client mid-
    #: negotiation is not accused of it, and short enough to catch before
    #: anybody starts reading launch files.
    STARVED_AFTER_S = 30.0

    def _report_starved_clients(self, now_s: float, now_utc: int) -> None:
        """Say so when a connected client is receiving nothing.

        Both shapes of it, because the symptom on screen is identical and the
        causes are not:

        * **nothing granted** — the client asked and the link profile refused;
        * **granted and never emitted** — the negotiation succeeded and the
          emit path is broken. This is the one that hid a dead tick loop behind
          a healthy-looking socket.

        Said in both directions, because neither audience can see the other's
        evidence: the operator at the browser knows the page is blank but not
        why, and whoever reads the node's log can see the profile but not what
        the page looks like.
        """
        for session in self.clients.values():
            if session.first_tick_s is None:
                session.first_tick_s = now_s

            granted = [
                name for name, sub in session.subscriptions.items()
                if sub.resolution.granted
            ]
            emitted = sum(sub.sent for sub in session.subscriptions.values())

            if emitted:
                # Data is flowing. Re-arm so a later stall is reported on its
                # own account rather than suppressed by an earlier one.
                session.reported_starved = False
                continue
            if session.reported_starved:
                continue
            if now_s - session.first_tick_s < self.STARVED_AFTER_S:
                continue

            session.reported_starved = True
            asked = len(session.subscriptions)
            if granted:
                detail = (
                    f"{len(granted)} stream(s) granted and none ever emitted "
                    f"({', '.join(sorted(granted)[:6])}). The negotiation "
                    "succeeded, so this is the server's emit path, not the "
                    "link profile"
                )
                if self._tick_failures:
                    detail += f"; {self._tick_failures} tick(s) have raised"
            elif asked:
                detail = (
                    f"asked for {asked} stream(s), granted none; "
                    f"link profile is {self.selector.profile!r} "
                    f"({self.selector.reason})"
                )
            else:
                detail = "the client has not subscribed to anything"

            _log.warning(
                "client %s has been connected %.0f s and is receiving nothing: %s",
                session.id, now_s - session.first_tick_s, detail,
            )
            session.send(
                {
                    "type": "notice",
                    "server_utc_ms": now_utc,
                    "severity": "alarm",
                    "code": "no_streams" if not granted else "nothing_emitted",
                    "text": "Connected, but receiving no data.",
                    "detail": detail,
                }
            )

    #: How long a granted stream may produce nothing before the client is told.
    UNAVAILABLE_AFTER_S = 5.0

    def _report_unavailable(
        self,
        session: ClientSession,
        name: str,
        sub: Subscription,
        now_s: float,
        now_utc: int,
    ) -> None:
        """Say so when a granted stream is producing nothing.

        A subscription that was accepted and then silently delivers nothing is
        the worst of both worlds: the client believes it is being fed, and the
        operator reads an empty panel as "nothing is happening" rather than
        "this source is not running". Said once, not every tick.
        """
        if sub.sent or sub.reported_unavailable:
            return
        if now_s - (sub.first_tick_s or now_s) < self.UNAVAILABLE_AFTER_S:
            return
        sub.reported_unavailable = True
        session.send(
            {
                "type": "stream_unavailable",
                "server_utc_ms": now_utc,
                "stream": name,
                "reason": (
                    f"subscribed and granted, but no {name} data has been produced. "
                    "The node that publishes it may not be running."
                ),
            }
        )

    def _link_context(self, session: ClientSession) -> dict:
        """Server-side facts the source cannot know."""
        context = {
            "profile": self.selector.profile,
            "profile_manual": self.selector.manual,
            "connected_clients": len(self.clients),
            "rate_bytes_per_s": round(
                sum(c.bytes_estimate_per_s for c in self.clients.values())
            ),
        }
        if session.shaper.enabled:
            context["shaping"] = session.shaper.to_dict()
        return context

    def broadcast(self, message: dict) -> None:
        for session in self.clients.values():
            session.send(message)

    async def run(self) -> None:
        self._running = True
        next_tick = time.monotonic()
        while self._running:
            now = time.monotonic()
            self.tick(now)
            next_tick += self.tick_interval_s
            # If we have fallen behind, skip forward rather than trying to
            # catch up: a backlog of ticks would produce a burst of stale data.
            delay = max(0.0, next_tick - time.monotonic())
            if next_tick < now:
                next_tick = now + self.tick_interval_s
            await asyncio.sleep(delay)

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.get_event_loop().create_task(self.run())

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
