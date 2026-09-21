> **If you are reading this on a branch in `auxenceIAAC/GUI_NAMIBIA`, you are
> looking at a transfer artefact.**
>
> This work's real home is **`auxenceIAAC/node-ros-2026`**, branch
> `gui-integration`. It was pushed to `transfer/gui-integration` on the GUI
> repository only because the session that produced it could read the fork but
> had no credential to push to it. The branch carries the whole Njord
> workspace, which does not belong in the GUI repository — delete it once the
> commits are in the fork.
>
> ```bash
> # In a clone of auxenceIAAC/node-ros-2026:
> git remote add transfer https://github.com/auxenceIAAC/GUI_NAMIBIA.git
> git fetch transfer transfer/gui-integration
> git checkout -b gui-integration transfer/gui-integration
> git push -u origin gui-integration
> git remote remove transfer
>
> # Then drop the hop:
> git push https://github.com/auxenceIAAC/GUI_NAMIBIA.git \
>   --delete transfer/gui-integration
> ```
>
> Nothing was pushed to `NODE-Engineering-Club/node-ros-2026`.

# GUI integration — status

The mission GUI overlay merged into the Njord workspace on `gui-integration`.

**Read this before merging anywhere.** It says what was verified, what was
assumed, and what is still open.

Since the first version of this document, one round of work has closed the
largest open item and found several more:

- **§8** — the Pico firmware. Two versions had diverged, the flashed one was not
  the one the Jetson parsed, and nothing could tell them apart. `pico-node_v4`
  merges them and announces its version; pre-flight now **fails** on a mismatch.
- **§9** — the two paths to the motors. Decided: the Pico. Enforced by the
  launch file, not by luck.
- **§10** — the simulator now produces real serial text, so the layer where the
  mismatch lived is exercised by the test suite.
- **§11** — four other live defects found on the way, including a crash on the
  first real status line and a channel-8 alarm that could never fire.

One thing still needs a human decision (§7) and one still needs a capture from
the Jetson (§5.1).

---

## 1. What was verified, and what was not

| | State |
|---|---|
| Merge, both histories intact | **verified** — 127 club + 15 GUI + 1 merge = 143 commits |
| GUI Python test suite (no ROS) | **verified** — 517 passed, 1 skipped |
| Adapter and STATE-parser unit tests | **verified** — rewritten against the real v4 format |
| Serial path end to end, no hardware | **verified** — real bytes on a pty, parsed by the real parser (§10) |
| Firmware behaviour, host-compiled | **verified** — `make -C firmware/test`, 23 checks (§8) |
| Motor-path exclusivity | **verified** — every argument combination, and checked against a reintroduced collision (§9) |
| Frontend production build | **verified** — builds into `gui_backend/static/` |
| Firmware against a real Pico | **NOT VERIFIED** — nothing here has been flashed or run on hardware |
| Map tiles end to end | **verified** — rendered in a browser against the live tile servers, with attribution, sea marks and all four degraded states (§12) |
| Tile behaviour on a real degraded link | **NOT VERIFIED** — the profile gate is unit-tested, but nobody has watched it on a fading 4G link |
| `colcon build --symlink-install` | **NOT VERIFIED** — see §6 |
| Gazebo end-to-end | **NOT VERIFIED** — see §6 |

The build and the Gazebo run were attempted in a sandbox with no GPU and a
Python 3.11/3.12 mismatch against ROS Jazzy. They failed on a **competition**
package before reaching any GUI package, which means they told us nothing about
this merge. They belong on the Jetson, where the environment is already right.
The commands are in §6.

---

## 2. What conflicted in the merge, and how

`git merge gui/... --allow-unrelated-histories` produced exactly two conflicts.
Both are root files; no file under `src/` collided, because the package sets are
disjoint.

| File | Resolution |
|---|---|
| `README.md` | **Club's kept verbatim** — all 572 lines — with a 113-line GUI section appended after a horizontal rule. It is the team's reference document and nothing was removed from it. |
| `.gitignore` | **Union.** Club's entries first and untouched; the GUI's additions appended under a marked comment block so it is obvious which half is which. |

Three files arrived without conflict because the club repo had no equivalent:

| File | Note |
|---|---|
| `CLAUDE.md` | The club had none. Landed from the GUI side, then its header was rewritten to describe the **merged** workspace rather than a standalone overlay, and its "Boundaries" section now states the competition-package rules. |
| `.flake8` | The club had none, so the GUI's applies. It does **not** affect competition packages: those are linted by `colcon test` through `ament_flake8`, which uses its own configuration. |
| `.github/workflows/` | No collision — the club has `build.yaml`, the GUI had none. |

### One change made beyond straight conflict resolution

`pytest.ini` and `conftest.py` came from the GUI side with `testpaths = src` and
a `sys.path` insert for every package in `src/`. In the merged workspace that
would have swept the competition packages' ROS-dependent tests into a bare
`pytest`, so a missing `rclpy` would have looked like the GUI breaking the
club's tests. Both are now scoped to the GUI packages by name.

**Competition packages: `colcon test`. GUI packages: `pytest`.**

---

## 3. Competition packages touched

**One file, 27 additive lines, nothing removed.**

```
src/bringup/launch/njord.launch.py | 27 +++++++++++++++++++++++++++
```

- `enable_gui` launch argument, **default `false`**, following the existing
  per-subsystem flag pattern.
- `gui_port` launch argument, default `8090`.
- An `IncludeLaunchDescription` of `gui_backend/launch/gui.launch.py`, guarded
  by that flag.
- One import (`FindPackageShare`).

Nothing else under `src/control`, `src/perception`, `src/sensors`,
`src/mission`, `src/vision`, `src/description`, `src/boat_bt`, `src/fusion`,
`src/competition_manager`, `src/njord_msgs` or `src/calibration` is modified.
Verify with:

```bash
git diff --stat club/main..gui-integration -- src/control src/perception src/sensors \
  src/mission src/vision src/description src/boat_bt src/fusion \
  src/competition_manager src/njord_msgs src/calibration src/bringup
```

---

## 4. Topic mapping — verified against source, not against a running system

Every mapping below was read out of the club README and the node source. None of
it has been observed on a live topic. "Verified" in the table means *the message
type and field access were checked against the real message definition and unit
tested*; it does **not** mean data has flowed.

Configured in `src/gui_backend/config/topics.yaml`. Nothing is hard-coded.

| GUI stream | Topic | Type | Adapter | State |
|---|---|---|---|---|
| Position | `/gps_driver/gps_raw` | `sensor_msgs/NavSatFix` | `vessel_from_odometry` | verified |
| Position (map frame) | `/odometry/gps` | `nav_msgs/Odometry` | — | wired, unused |
| Speed, heading, attitude | `/odometry/filtered` | `nav_msgs/Odometry` | `vessel_from_odometry` | verified |
| IMU | `/imu_driver/imu_raw` | `sensor_msgs/Imu` | `vessel_from_odometry` | verified, fallback only |
| Lidar raw | `/lidar_driver/scan_raw` | `sensor_msgs/LaserScan` | `lidar_from_ros` | verified |
| Lidar filtered | `/obstacles/lidar` | `sensor_msgs/PointCloud2` | `obstacles_from_pointcloud` | verified |
| Obstacles fused | `/obstacles/fused` | `sensor_msgs/PointCloud2` | `obstacles_from_pointcloud` | verified, optional |
| Commanded effort | `/control/effort` | `geometry_msgs/Twist` | — | wired, unused |
| Vessel status | `/pico/status` | `std_msgs/String` | `pico_from_ros` | **ASSUMED — see §5** |
| Mode request | `/pico/mode_request` | `std_msgs/String` | — | verified against `pico_bridge` |

### Decisions worth knowing about

**Heading, speed and attitude come from one message.** `/odometry/filtered` is
the EKF output, so the three cannot drift apart the way four independent MAVROS
topics could. Heading source is reported to the GUI as `ekf`.

**ENU yaw is converted to a compass bearing** (`90 − yaw`). ROS is
counter-clockwise from east; a bearing is clockwise from north. Getting this
backwards would put the entire survey on the wrong side of the boat, plausibly,
with nothing on screen looking wrong — so it is pinned by a test at four
cardinal points.

**Course over ground is derived independently of heading**, from the same
message's twist rotated out of the body frame. Comparing two numbers that came
from the same one says nothing, and that comparison is the row the heading panel
exists for.

**The filtered obstacle set comes from perception, not from re-filtering the raw
scan here.** Filtering twice in two places eventually produces two different
answers about where the obstacles are, and the navigation stack's is the one
that matters.

**Satellite count is absent, not zero.** Neither `NavSatFix` nor `Odometry`
carries one and this stack has no MAVROS `GPSRAW`. "0 satellites" would read as
a GNSS failure and ground a healthy vessel, so the pre-flight reports SKIPPED
instead. Position accuracy is taken from `position_covariance` when it is
non-zero, and reported as a metre figure rather than converted into an HDOP by a
made-up UERE.

---

## 5. Still PROVISIONAL

Everything below is greppable:

```bash
grep -rn PROVISIONAL src/
```

### 5.1 The Pico `STATE` line format — now transcribed, still not captured

The format is no longer a guess. It is transcribed field by field from
`firmware/pico-node_v4/pico-node_v4.ino`, and the simulator emits it over a real
serial port so the parser is exercised on every test run (§10).

What remains open is narrower but real: **nobody has read a line off a physical
Pico.** Reading a firmware's source is not the same as reading its output.

- Parser: `src/gui_backend/gui_backend/core/pico_state.py` — still the only file
  that knows the wire format, and now single-format on purpose.
- `FORMAT_VERIFIED = False`.
- Pre-flight `pico.state_format` returns **WARN every run** until that flag is
  set, and **FAIL** on a line the parser cannot read.
- Pre-flight `pico.firmware_version` is **critical and FAILs** on a mismatch.

To close it, on the Jetson with the Pico connected:

```bash
ros2 topic echo /pico/status --field data
```

Paste a few real lines into `src/gui_backend/test/test_pico_state.py`, check
them against `parse_state_line`, and set `FORMAT_VERIFIED = True`.

### 5.2 The rest

| What | Where | Note |
|---|---|---|
| Sonar mounting angle and lever arm (Q2) | `src/omniscan_bridge/config/mounting.yaml` | `measured: false`. Pre-flight WARNs every run until measured. |
| Survey area and sonar range (Q1) | `src/asket_bringup/config/mission_defaults.yaml` | Tuned on site; range is adjustable from the GUI at runtime. |
| Battery capacity and hotel load (Q5) | `src/gui_backend/config/topics.yaml` | 1200 Wh / 85 W assumed. |
| EKF heading accuracy | `src/asket_common/asket_common/heading.py` | 3.0° nominal. The EKF publishes a pose covariance — somebody should confirm `robot_localization` is filling it in rather than leaving the default, then use it. Until then the panel marks the figure "assumed". |
| `OS3D_POINT_SET` layout (Q8) | `src/omniscan_bridge/core/ping_protocol.py` | From Cerulean's published docs, never run against a real device. |
| Relay and ESC meanings (Q7) | `src/asket_gui/src/lib/hull.js` | `RELAY_LABELS` is empty on purpose — no relay is given a name nobody has confirmed. |

---

## 6. What to run on the Jetson

Where I stopped. The environment there is already configured; nothing below
needs the workarounds that failed in the sandbox.

### 6.1 Build

```bash
cd ~/node-ros-2026            # or wherever the workspace lives
git fetch origin
git checkout gui-integration

# The frontend must be built before the backend can serve it.
cd src/asket_gui && npm install && npm run build && cd ../..

source /opt/ros/jazzy/setup.bash
rosdep install --from-paths src --ignore-src -y -r
colcon build --symlink-install
```

Then confirm no competition package regressed:

```bash
colcon test --packages-select control perception sensors mission fusion \
  competition_manager boat_bt njord_msgs bringup
colcon test-result --verbose
```

### 6.2 Tests without ROS and without hardware

These run anywhere, including a laptop:

```bash
pytest                                            # GUI packages only
python3 -m flake8 --max-line-length=100 src/gui_backend src/system_test
make -C firmware/test                             # the Pico firmware, host-compiled
```

The firmware tests need only a C++ compiler — no Pico, no Arduino toolchain.
See §8.

To watch the simulated Pico talk, without ROS or a boat:

```bash
python3 -m asket_sim.core.fake_pico_serial        # prints a port
cat /dev/pts/N                                    # the port it printed
```

### 6.3 Gazebo

```bash
source install/setup.bash
ros2 launch bringup njord.launch.py use_sim:=true enable_vision:=false enable_gui:=true
```

### 6.3b Which organ drives the motors

```bash
ros2 launch bringup njord.launch.py                      # motor_path:=pico, the default
ros2 launch bringup njord.launch.py motor_path:=pixhawk  # the old path
```

Inside Podman, pass the **resolved** serial device, not the symlink:

```bash
ros2 launch bringup njord.launch.py pico_device:=/dev/ttyACM0
```

See §9.

Then open **http://\<jetson\>:8090** from the laptop.

What to check, in order:

1. **Position** — the vessel appears on the map near the Trondheim datum, and
   the Vessel panel's position updates.
2. **Speed and heading** — the Heading panel shows a bearing with source
   `EKF (fused)`. Drive the boat and confirm heading and course over ground
   differ when it crabs, and agree in a straight line.
3. **Lidar** — the Obstacles panel shows returns, and `Returns N of 360 beams`
   rather than `0 of 0`. Toggle Filtered/Raw and confirm the two differ.
4. **Obstacles fused** — absent with `enable_vision:=false`; that is correct,
   and the panel should say "not sent" rather than showing zero.
5. **Pre-flight** — press Run pre-flight. Expect **two amber warnings**:
   `Sonar mounting geometry` (§5.2) and `Pico status format` (§5.1). Both are
   correct until somebody measures the boat and captures a STATE line.
6. **The Pico stream stays absent in sim** — expected. There is no Pico in
   Gazebo, so the vessel panel's mode and armed state read "not sent".

If the map is blank, that is the offline-tiles path, not a fault: see
`docs/SETUP.md` §5.

### 6.4 Port check

The GUI is on **8090**, chosen clear of `foxglove_bridge` (8765) and of the
commented-out `rosbridge_websocket` (9090) and `web_video_server` (8080) in
`njord.launch.py`. Confirm nothing else took it:

```bash
ss -lntp | grep -E '8090|8765|9090|8080'
curl -s http://<jetson>:8090/api/tiles/info     # from the laptop, not the Jetson
```

The backend binds `0.0.0.0`. If that curl times out and the Jetson is otherwise
reachable, it is a firewall, not the GUI.

---

## 7. Open question: what should "Cut propulsion" do?

**This needs a decision. It is not mine to make, and it was explicitly left out
of this round: a serial ESTOP verb is a safety-chain change and belongs on the
bench, with the boat out of the water, as its own step.**

### The problem

`pico_bridge` accepts exactly two mode words — `AUTO` and `MANUAL` — and logs a
warning for anything else. The GUI's "Cut propulsion" button has nothing to
send.

One correction to how this section used to read: `MODE_ESTOP` **does** exist in
the firmware, in all three versions, with disarm, relay open and a red light.
What does not exist is a *serial path into it*. The ESTOP is real; only the
transmitter can reach it. That is deliberate — see the three layers in
`docs/safety.md`.

### What it does in the interim

It requests **`MANUAL`**.

That is the safest thing available, for a specific reason. From `pico_bridge`'s
own docstring:

> *Moteurs appliqués côté Pico SEULEMENT si armé + AUTONOMOUS + 2s après relais.*

The Pico drives the thrusters only when armed **and** in AUTONOMOUS. Requesting
MANUAL therefore removes software authority over the thrusters entirely and
hands the boat back to the RC pilot.

**It does not stop the boat.** The hardware killswitch and RC channel 8 remain
the only things that cut propulsion, exactly as the safety rules require.

### How that is made visible

The button is **not** labelled "Cut propulsion" against this stack. The backend
declares what a propulsion cut can actually do in the `hello` message, and the
frontend labels the button from that rather than assuming a capability the
vessel may not have:

```yaml
# src/gui_backend/config/topics.yaml
estop:
  mode: mode_request_manual
  label: "Drop to MANUAL"
  effect: >-
    Hands control back to the RC pilot. The Pico only drives the thrusters when
    armed and in AUTONOMOUS, so this removes software authority — it does not
    stop the boat. The hardware killswitch and RC channel 8 are the only things
    that cut propulsion.
```

That `effect` sentence is rendered under the button. A button reading "Cut
propulsion" that quietly dropped the mode instead would be the most dangerous
thing on the screen.

In `asket_sim` the button keeps its real meaning, because the simulated Pico
does model a soft latch. The two are declared separately on purpose.

### The two options

**A — Add a `MODE ESTOP` path to firmware and `pico_bridge`.** The button means
what it says. Costs a firmware change plus a `pico_bridge` change, and
`pico_bridge` is a competition package this branch does not touch. Someone has
to own the firmware side and decide what ESTOP does at the Pico: neutral PWM,
relays open, or a latch that needs a physical reset.

**B — Keep the mode drop, permanently.** No firmware change. The GUI keeps
saying what it does. The soft ESTOP concept disappears from the GUI's
vocabulary, which is arguably more honest: the hardware killswitch is the ESTOP,
and having a second thing called ESTOP that is weaker invites confusion at
exactly the wrong moment.

I have deliberately not chosen. Whichever you pick, `estop.mode`, `estop.label`
and `estop.effect` in `topics.yaml` are the only things that change on the GUI
side.

---

## 8. Firmware, and the version check that closes this class of bug

### What was wrong

Two Pico firmwares had diverged and nobody could tell which was flashed.

| | `pico-node_v3` | `asket_ec_pico` |
|---|---|---|
| Header | `[REBUILT]` | `[REBUILT + FOXGLOVE]` |
| Lives in | `NODE-Engineering-Club/pico-node` | this repo, `GPS_Fix_and_Task`, `920773f` |
| Status line | `[STAT] Mode:3 Armed:Y …` | `STATE mode=3 armed=1 …` |
| Flashed? | **yes** | no |
| What the Jetson expected | — | **this one** |

`pico_bridge` filtered for lines starting `STATE`; the Pico emitted `[STAT]`.
Every status line was dropped **silently** — no counter, no log — and the
downlink kept working, so the boat moved and nothing looked broken. Nobody
subscribed to `/pico/status` before the GUI existed, and nothing launched
`pico_bridge` anyway.

### What is here now

`firmware/pico-node_v4/` merges the two. Its README has the full derivation;
the short version:

- **From FOXGLOVE:** `STATE key=value` with all its fields, the three-zone
  `update_state()`, the heartbeat, `MODE AUTO` / `MODE MANUAL`, and a real SBUS
  frame-validation fix (below).
- **From v3:** its comments. Nothing else — `update_motors()`, `updateBeeper()`
  and `startBeeps()` are byte-identical between the two files, so the pivot,
  the non-blocking beeper and the proportional mix were already in FOXGLOVE.
- **New:** `[VER] pico-node 4` at startup and `ver=4` first in every `STATE`
  line; a non-blocking serial reader.
- **Dropped:** `BENCH_NO_RC_OVERRIDE`, a FOXGLOVE compile flag that removed the
  hardware E-stop's authority outright.

### The version check

This is the part that matters more than any individual fix.

| Layer | What it does |
|---|---|
| Firmware | `[VER] pico-node 4` at boot, `ver=4` first in every `STATE` line |
| `pico_bridge` | logs the version; **counts** rejected lines and logs an error if it sees 20 with none kept |
| `pico_state.py` | `PROTOCOL_VERSION = 4`; sets `version_mismatch` on anything else |
| Pre-flight | `pico.firmware_version` is **critical** and **FAILs** on a mismatch |
| GUI | shows the version beside the mode; a mismatch force-opens the Vessel panel |

FAIL rather than WARN, deliberately: a version this software does not know is a
firmware whose field layout is unknown, so every other row in the report may
have been read against the wrong layout. A confident GO is then the most
dangerous output the check could produce.

**If you flash something other than v4, pre-flight will ground the boat.** That
is intended. Bump `FW_VERSION`, `PROTOCOL_VERSION`, `FIRMWARE_VERSION` in
`asket_sim/core/pico.py` and `EXPECTED_FIRMWARE_VERSION` in `checks.py`
together — a test asserts all four agree, and another reads the `.ino` to check
the thresholds still match.

### The SBUS fix, which was not asked for and is worth knowing about

v3 validated frames with `sbus_frame[24] != 0x00`. Byte 24 is the SBUS **end
byte**: `0x00` on Futaba, but `0x04` / `0x14` / `0x24` / `0x34` on FrSky and
others. Against such a receiver v3 rejects **100% of frames**, silently, with
the channels frozen at their last value.

Demonstrated by compiling v3 against the test stub:

```
v3, end byte 0x00 -> ch8 decoded as 1811  (frame ACCEPTED)
v3, end byte 0x04 -> ch8 decoded as 0     (frame REJECTED)
```

FOXGLOVE had already fixed it. v4 keeps the fix. **Worth checking which end
byte your receiver actually sends** — if it is not `0x00`, this explains more
than one past symptom.

---

## 9. Decided: the Pico is the motor path

`actuator_driver` and `pico_bridge` both subscribe to `/control/effort`. They
never collided only because nothing started `pico_bridge` — which is not
arbitration, and ends the moment somebody runs it by hand to make the GUI work.

**Decision: the Pico drives the motors.** The Pixhawk stays as a navigation
source (GNSS, IMU, heading) and commands nothing.

`njord.launch.py` now enforces it with one argument:

```bash
ros2 launch bringup njord.launch.py                      # motor_path:=pico, the default
ros2 launch bringup njord.launch.py motor_path:=pixhawk  # the old path, intact
```

Both nodes' conditions read that one argument and test it for different values,
so **no combination of launch arguments starts both**. `actuator_driver` is not
deleted; reverting is one argument, not an edit under time pressure.

`src/system_test/test/test_motor_path_exclusive.py` parses the launch file and
evaluates both conditions across every combination of the arguments involved.
It was checked against a deliberately reintroduced collision and fails on it.

`pico_bridge` also gained a `pico_device` launch argument, defaulting to
`/dev/ttyACM0`. **Podman resolves symlinks at launch: pass the container the
resolved `ttyACMn`, not `/dev/pico`.**

---

## 10. The simulator now produces real serial text

`asket_sim` published a structured `PicoStatus` straight onto the topic. That is
exactly why the `[STAT]`/`STATE` mismatch was invisible: the text layer did not
exist in simulation, so the layer where firmware and Jetson disagreed was never
exercised.

`asket_sim/core/fake_pico_serial.py` now puts real bytes on a real pty, the way
`fake_sonar_server` already does for the sonar. It speaks what v4 speaks —
`[VER]`, `STATE key=value` at 4 Hz, `L,R`, `PING`, `MODE AUTO` / `MODE MANUAL`,
`[ACK]`, `Invalid cmd:` — and applies the same three-zone arbitration, so the
GUI cannot pass a test the boat would fail.

```bash
python3 -m asket_sim.core.fake_pico_serial   # prints a port you can cat
```

`src/gui_backend/test/test_pico_serial_end_to_end.py` drives the whole chain
with no ROS and no hardware: pty → line splitting → `STATE` filter → parser →
payload. It is the test that did not exist.

### Host-side firmware tests

```bash
make -C firmware/test
```

Compiles the sketch against a stub Arduino core and drives it with synthetic
SBUS frames and serial input: mode arbitration in all three Ch8 zones, the
ESTOP path down to the relay pin, the serial reader on partial and over-long
lines, the light convention, the shape of the `STATE` line. It says nothing
about timing, electrical behaviour or the real USB stack.

---

## 11. Other defects found and fixed on the way

Not asked for, found while doing the above, all of them live:

- **`pico_payload` crashed on a real line.** It called
  `int(sample.rc_channel8_raw_pct)` on a field that is `None` whenever the line
  did not carry it. The first real `STATE` line would have taken the backend
  down. `bool(sample.rc_link_ok)` had the matching quieter bug: `bool(None)` is
  `False`, so a field that was never sent rendered as **"RC lost"** — the worst
  lie this panel can tell. Both now preserve `None`.

- **The channel 8 alarm never fired.** The field was named
  `rc_channel8_raw_pct` and the GUI alarmed below 25, but what arrives is a raw
  SBUS count of 172–1811. Every real value cleared the threshold, *including
  200*, which is the bottom of the travel and the exact position the alarm
  existed for. Renamed to `rc_channel8_raw` everywhere (message, payload,
  parser, checks, mock) and the comparison moved to the firmware's own
  threshold, `< 700`, made once in `pico_state` rather than re-derived in the
  GUI.

- **The light tower contract described a tower that does not exist.**
  `docs/light_tower.md` promised blue, blink patterns and a green recording
  flash. The firmware has three solid colours. Green meant "recording" in the
  document and "autonomous and armed, propellers may start" in the firmware —
  the dangerous direction. Both the document and
  `asket_bringup/config/light_tower.yaml` now mirror `set_light()`, and the
  yaml says in its header that it is a mirror and not a source.

- **Mode numbering differs between firmware and GUI.** The firmware counts
  `ESTOP=1, MANUAL=2, AUTONOMOUS=3`; `PicoStatus.msg` counts from 0. They are
  bridged by name, never by adding one, and both files now say so. An
  off-by-one here turns MANUAL into AUTONOMOUS on screen.

---

## 12. The map has a real basemap now

The map rendered a bare coordinate grid, because the GUI was designed on the
assumption that there is no internet in the field. There is — Namibia has 4G at
the launch point and usually from the shore station — so that assumption has
been replaced. `docs/map_tiles.md` is the full account; the parts that matter
for a review:

**The basemap is not OpenStreetMap's own tile server**, and the reason is
theirs, not ours. Their policy says *"Offline use is not permitted on
tile.openstreetmap.org"*, counts as bulk downloading *"any pre-emptive fetching
of tiles other than those a user is actively viewing"*, and does not recommend
caching proxies. The caching this GUI needs would breach all three. So the
basemap is **OpenFreeMap** — same OSM data, no key, no account, "no limits on
the number of map views or requests", self-hostable, and serving a ten-year
cache header. The nautical overlay is **OpenSeaMap**, off by default.

**Four sources, in order:** the Jetson's disk cache, the internet, the existing
`.mbtiles` file, the coordinate graticule. The offline path still works and is
still the answer for a survey with no coverage.

**Vector rather than raster**, chosen mostly so the basemap can be muted. This
map already carries five saturated overlay colours; a normal street map fights
all of them. It also means a 5 km survey box costs ~20 tiles (~250 kB) instead
of ~120 (~1.8 MB).

**Tiles are fetched only on a `full` link.** On `reduced` and beacon the map
lives on its cache and says so. One vector tile is fifteen seconds of a beacon
budget.

**Attribution was switched off** — `attributionControl: false` — which is an
ODbL breach the moment any OSM-derived tile renders. It is now on, and each
source carries its own string.

**One promise worth keeping visible:** nothing pre-fetches. There is no
seed-the-survey-box function and a test asserts there is no function that looks
like one. A survey box is twenty tiles; the saving is not worth the habit.

Also worth knowing: **the existing `.mbtiles` fallback may never have been
producible.** Nothing in the repo says where that file comes from, and the
obvious method — bulk-downloading from OSM — is what their policy forbids.
`pmtiles extract` against the Protomaps daily build gives a bbox cutout under
ODbL without scraping anybody. Not implemented; noted in `docs/map_tiles.md`.

---

## 13. Branch conflict warning

Checked against the club repo at time of writing:

**`GPS_Fix_and_Task` is 37 commits ahead of `main` and touches both files this
integration touches.**

| Branch | Ahead of `main` | Touches |
|---|---|---|
| `GPS_Fix_and_Task` | 37 | `pico_bridge.py`, `njord.launch.py` (+363 lines), `ekf.yaml`, `navsat.yaml`, `nav2_params.yaml` |
| `gazebo_debugg` | 5 | `pico_bridge.py`, `njord.launch.py` |
| `feat/report` | 2 | `njord.launch.py` |
| `piddebugg`, `Nav2-testing` | 1 | `pico_bridge.py`, `njord.launch.py` |

If `GPS_Fix_and_Task` merges first, expect a conflict in `njord.launch.py`
(mechanical, but larger than before — this round adds the `motor_path` argument
and a `pico_bridge` node) and **check its `pico_bridge` changes against §5.1 and
§8**, since anything altering the STATE contract changes what the parser reads.

That branch also carries `firmware/pico/asket_ec_pico.ino`, the FOXGLOVE
firmware. It is superseded by `firmware/pico-node_v4/`. Do not flash it.

---

## 14. Also flagged

`scripts/deploy-pi.sh` targets `pi@boat.local` and looks stale — this project
moved to a Jetson. Left untouched: it needs somebody who knows the current
deployment to decide what it should be.
