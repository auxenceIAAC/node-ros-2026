# gui_backend

FastAPI + WebSocket. Serves the API, the offline map tiles and the built
frontend from one process on the Jetson — there is no CDN in Namibia and no
second server to run.

## Running

Without ROS, against the simulator — this is the development path:

```bash
python3 -m gui_backend.core.app --sim --port 8080
python3 -m gui_backend.core.app --sim --time-scale 10   # survey faster than real time
python3 -m gui_backend.core.app --sim --tiles /data/maps/survey.mbtiles
```

With ROS:

```bash
ros2 run gui_backend gui_backend_node --ros-args \
  -p topics_config:=$(ros2 pkg prefix gui_backend)/share/gui_backend/config/topics.yaml
```

### Build the frontend BEFORE the workspace

Not a style preference — an ordering the build cannot recover from on its own:

```bash
cd src/asket_gui && npm install && npm run build   # first
cd ../.. && colcon build --symlink-install         # then this
```

`gui_backend/setup.py` collects the built frontend from `gui_backend/static/`
while it runs. Build the workspace first and there is nothing there to collect,
so colcon installs an empty static directory and the backend answers 503 with a
message about npm — which reads as "the frontend was never built" rather than
"it was built in the wrong order". If you have already built in the wrong
order, build the frontend and then rebuild the package:

```bash
colcon build --symlink-install --packages-select gui_backend
```

`setup.py` now prints a loud warning during `colcon build` when there is
nothing to install, so the mistake surfaces where it is made.

## The design decision that matters

**The backend never pushes anything by default.** Clients subscribe to named
streams at a requested rate and detail level; the server decides what it will
actually serve, and tells the client what it is getting and why.

Everything else follows from that: decimation is server-side and per client, so
two operators on different links can watch the same vessel at different rates;
a stream absent from a profile is refused with a reason rather than quietly
starved; and a subscription that is granted but produces nothing gets an
explicit `stream_unavailable` notice, because a silent empty panel reads as
"nothing is happening" rather than "that node is not running".

The protocol is documented in [`docs/ws_protocol.md`](../../docs/ws_protocol.md).

## Layout

| Module | Responsibility |
|---|---|
| `core/streams.py` | The stream registry and the profile policy. Adding a stream here is enough for it to be negotiable |
| `core/hub.py` | Clients, subscriptions, scheduling, decimation, backpressure |
| `core/payloads.py` | The wire shape of every message. Shared by both sources |
| `core/commands.py` | `pending` / `confirmed` / `failed`, and nothing else |
| `core/alarms.py` | The few conditions that cannot self-correct, each with a remedy |
| `core/link_profile.py` | Automatic profile selection, with hysteresis |
| `core/sim_source.py` | Data from `asket_sim`, in-process, no ROS |
| `core/ros_source.py` | Data from real topics |
| `core/adapters.py` | ROS messages → the shapes `payloads` expects |
| `core/tiles.py` | Offline MBTiles, read with stdlib `sqlite3` |
| `core/server.py` | The FastAPI app |

## Configuration

`config/topics.yaml` maps every logical source to a topic, a type and an
adapter. **No topic name or message type appears in the code.** That is not
tidiness: the real `pico_bridge` message is unknown to this repository and
`pico_bridge` must not be modified (`docs/open_questions.md` Q7), so pointing
the backend at it must be a config change plus at most one adapter function.

`config/link_profiles.yaml` holds the profile thresholds and the server's own
settings, including the path to the offline tile file.

## Degraded links

```bash
python3 -m gui_backend.core.app --sim --shape-link
```

`--shape-link` throttles the outbound socket to the simulated bearer — capacity,
latency and loss. Without it, "degraded link" in sim only means a smaller
subscription set on a gigabit loopback, and the case the whole design exists to
survive never gets exercised.

Drive it from the GUI's fault controls, or:

```bash
ros2 topic pub --once /sim/inject_fault std_msgs/String '{data: "link_degraded"}'
ros2 topic pub --once /sim/inject_fault std_msgs/String '{data: "link_loss"}'
```

On the beacon profile the whole subscription set costs well under 200 B/s
against a 1 kB/s budget, position keeps arriving, and everything not carried is
labelled as not carried rather than left blank.

## Testing in sim

```bash
pytest src/gui_backend        # no ROS, no browser, no hardware
```

The tests include the whole WebSocket path through the real FastAPI app: that
nothing is pushed before a subscription, that a client asking for too much is
told what it is getting, that a mode command goes `pending` → `confirmed` from
the vessel's own status, and that a slow client loses its oldest frames rather
than stalling the hub.

## Debugging "the page is up and nothing is arriving"

This failure has now happened twice in the field, from two unrelated causes,
and it looks identical both times: the page renders, the socket upgrades, the
pings answer, every panel says "not sent". That is also what a switched-off
boat looks like, which is why it costs hours rather than minutes.

**Open DevTools before you load the page.** Chrome's WebSocket frame inspector
only records frames from the moment DevTools was opened on that request. The
whole negotiation — `hello`, `subscribe`, `subscribed`, and any
`stream_unavailable` — happens in the first second. Open the panel afterwards
and you see ping/pong and conclude the client never subscribed. It did.

Then read, in this order:

1. **The banner at the top of the page.** The server says which half is broken:
   `no_streams` means the link profile refused the subscription, and
   `nothing_emitted` means the negotiation succeeded and the emit path is at
   fault. They send you to opposite ends of the system.
2. **The node's log.** The hub logs a warning naming the profile and the
   granted streams after 30 s of silence, and logs a full traceback the first
   time a tick raises. A tick that raises no longer ends the loop — it costs
   one stream for one tick and says so.
3. **`/api/health`.** Reports the live profile, its reason, the client count
   and the source's own description, with no browser involved.

```bash
curl -s http://<jetson>:8090/api/health | python3 -m json.tool
```
