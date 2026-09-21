> **This work is home.** It lives in `auxenceIAAC/node-ros-2026`, branch
> `gui-integration`, which is where you should be reading it.
>
> The hop through `auxenceIAAC/GUI_NAMIBIA` is over. Earlier sessions were
> authorised only for the GUI repository and pushed to
> `transfer/gui-integration` there because they could not push to the fork;
> that branch's tip was `7db23c0`, and it is now merged here. The transfer
> branch is kept until Auxence has seen it landed, then deleted — nothing
> needs to be taken from it again.
>
> **It was not taken as-is, and that matters if you are comparing histories.**
> The fork's `gui-integration` had not been idle: it carried three commits of
> its own (`b9ab96a`, `432b623`, `226f6c4`) building a *different*
> reconciliation of the same dead-`/pico/status` problem — v3 firmware kept, a
> parser accepting both `[STAT]` and `STATE`, and serial `CMD MODE` /
> `CMD ESTOP` verbs. The two lines contradict each other, so the merge
> (`9ff9a15`) was resolved wholly in v4's favour: its tree is exactly
> `7db23c0`'s. Both parents remain reachable, so the v3 reasoning is still
> readable — it is simply not what this workspace builds.
>
> Two things were then ported forward from that line, rewritten against v4
> rather than transplanted: `asket_common.mode_arbitration` with its firmware
> cross-check (§14), and the arming-block explanation (§15). The serial ESTOP
> verb was deliberately left behind; see §7.
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
| Merge, both histories intact | **verified** — 150 commits, both parents reachable from `9ff9a15` |
| GUI Python test suite (no ROS) | **verified** — 572 passed, 1 skipped |
| Firmware arbitration vs. the Python mirror | **verified** — 448 cells, cell for cell, against the compiled sketch (§14) |
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
| ESC status codes beyond 0 (Q7) | `src/asket_gui/src/lib/hull.js` | Still uninterpreted. The firmware reports no ESC code over serial at all — `esc_status` arrives only from `asket_sim`. **The relay is no longer provisional**: there is one, GPIO21, cutting ESC power, confirmed in firmware source and now named. See §16. |
| **E-stop power feedback** | firmware `ESTOP_FEEDBACK_ENABLED` | **0.** Nothing confirms the ESC rail actually collapsed when the relay was commanded open. Pre-flight `pico.estop_feedback` WARNs every run until it is 1. See §16. |

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

---

## 14. Mode arbitration, and the check that keeps it honest

Ported forward from the fork's v3 line of work (see the banner at the top), and
**rewritten against v4 rather than transplanted**. This section exists because
copying it across unchanged would have been the most plausible mistake
available, and it would have rebuilt the exact failure this project has now hit
twice: two ends of a contract disagreeing with nothing noticing.

v3 and v4 do not arbitrate the same way.

| | v3 | v4 |
|---|---|---|
| Rule | the more restrictive of (channel 8, software request) | three zones, and the top one grants **permission** |
| Top zone | AUTONOMOUS unless clamped | MANUAL until the Jetson asks **and** the heartbeat is fresh |
| Software ESTOP | `CMD ESTOP`, accepted from any state | **none** — see §7 |
| Shape | a pure `arbitrate_mode()` | `update_state()`, reading globals |

So `asket_common/mode_arbitration.py` now mirrors `update_state()` statement
for statement — including the order of its last three lines, because the
failsafe override, the latch clear and the latch application are all
order-dependent and rearranging them changes what the boat does.

### How it is checked

v3's `arbitrate_mode()` was pure and could be lifted out of the `.ino` and
compiled on its own. v4's cannot. Rather than refactor the firmware for
testability — a firmware edit, and those belong on the bench with the boat out
of the water — `firmware/test/arbitration_table.cpp` compiles the **whole
sketch** against the stub Arduino core that `firmware/test/` already provides,
sets its globals directly, and prints the decision table.

`src/asket_common/test/test_firmware_arbitration_matches.py` drives the Python
mirror through the same inputs and compares `(mode, armed, latch-out)` on every
one.

**448 cells**: 7 channel-8 values (both zone interiors and both boundaries) ×
4 channel-7 values × request × heartbeat × receiver failsafe × latch.

```bash
make -C firmware/test table      # the table alone
pytest src/asket_common          # the comparison, and the constant mirrors
```

The check was confirmed able to fail, in both directions, before being trusted:
removing the heartbeat requirement from the Python mirror breaks 112 cells;
dropping the latch application breaks it; widening `MODE_LOW_MAX` from 700 to
800 *in the firmware* breaks it twice over; adding a serial ESTOP verb to the
firmware trips a dedicated guard. A cross-check nobody has seen fail is a
cross-check nobody should believe.

Beyond the table, every constant that exists in Python only because it cannot
be read off the wire is pinned to the sketch: the three channel thresholds, the
heartbeat timeout, the SBUS range, both 500 ms failsafes, and `FW_VERSION`
against `PROTOCOL_VERSION`. Where a value is spelled in two Python places as
well — `CH8_ESTOP_MAX` and `CH7_ARM_MIN` in `pico_state` — all three are
compared, because a two-way check passes happily while the third quietly
disagrees.

### The field-rename gap, now closed

`ver=` protects against the firmware changing shape *wholesale*: a parser that
does not know the version rejects the line. It does **not** protect against one
field being renamed inside a version — the parser matches whole keys, so a
renamed field silently becomes `None` and the panel reads "not sent" for a
vessel that is transmitting perfectly well.

That is a quieter version of §2a and the same species of bug, so every key in
`pico_state._FIELDS` is now asserted against the sketch's own `STATE` print.
Renaming `estoplatch` fails the test by name.

### Two guards that protect decisions rather than code

`test_the_firmware_still_has_no_serial_estop_verb` and
`test_the_bench_override_flag_has_not_come_back` both fail on a change that is
otherwise perfectly reasonable-looking. That is deliberate. The absence of a
serial path into `MODE_ESTOP` (§7) and the absence of `BENCH_NO_RC_OVERRIDE`
(§8) are decisions with reasons; if somebody reverses one, they should have to
delete a test that explains why, on purpose, rather than discover later that
nothing objected.

---

## 15. Why the propellers will not turn, said out loud

The other thing ported forward, and the one an operator meets most often.

**Arming is RC channel 7 and nothing else.** There is no software path to it,
and that is a decision rather than a gap: somebody is physically present to
launch this boat, and flipping that switch is their consent that the propellers
may turn. A mode arrives over a radio link from a laptop; consent does not. The
RC authorises; the GUI drives.

The cost is a state that reads exactly like a fault. A mission is permitted by
channel 8, requested, granted, confirmed in the `STATE` line — and nothing
moves, because the arm switch is down. An operator who cannot see why is left
guessing whether the boat ignored them or the switch did, and those two send
you to different places on the beach.

So `arming_block_reason()` computes it once, `pico_payload` ships it as a
sentence (or not at all), and it appears in the four places somebody could be
looking:

| Where | What it says |
|---|---|
| Mode panel, standing line | `RC channel 7 disarmed, propulsion cannot start` — above the buttons, because the state exists before anybody presses one |
| Autonomous button's confirm prompt | the same sentence, on the click where it matters |
| The command result | `Confirmed by the vessel — but RC channel 7 disarmed, propulsion cannot start` |
| Vessel panel, Disarmed chip | the same sentence, on hover |

**The Autonomous button is deliberately not disabled** while channel 7 is down.
Selecting AUTONOMOUS and then arming on the transmitter is the normal launch
sequence, with somebody standing next to the boat. Disabling the button would
break that sequence; annotating it makes the order legible.

Four causes are kept distinct rather than flattened: channel 7 down, a latched
e-stop (which names the way out — cycle the arm switch), a contradiction
(channel 7 up and the vessel disarmed anyway), and a disarmed vessel whose
cause cannot be identified. A vessel that has not reported its arming state
gets **no line at all**: not knowing is not evidence of a block, and it is the
state every panel starts in.

The sentence exists in two languages, because mock mode is how this GUI gets
reviewed and a reviewer must see the wording the boat will actually send. That
is a drift risk like any other, so `test_mock_payload_shapes.py` compares both
the four strings **and** the decision across Python and the JS mock — a mock
that says "blocked" where the backend says "fine" teaches the wrong reflex.

### One scenario that section 7's decision leaves open

**A mid-mission Jetson reboot, beyond RC range, cannot be recovered.** The
vessel loses SBUS, the failsafe stops propulsion, the Jetson comes back, the
GUI can request AUTONOMOUS again — and the vessel is disarmed or latched, with
nothing in software able to re-arm it. The latch clears only when the operator
cycles channel 7 or selects ESTOP on channel 8, both of which need a
transmitter within range of a boat that is not within range of one.

Recovery is a boat trip. This is the honest cost of the current safety chain
and it may well be the right cost — the alternative is software that can start
propellers with nobody present, which is what channel 7 exists to prevent.
**It is not this integration's decision.** Worth noticing, for whoever takes
it: the Pico does not reboot in that scenario, only the Jetson does, so the arm
state is live in the firmware throughout. What kills propulsion is the SBUS
failsafe, not the arming logic.


---

## 16. The e-stop path: one unverified claim, and one wrong number

Both of these are about the relay that cuts ESC power, which is the worst place
in this system to be silent or to be wrong. Neither changes how the boat
behaves; both change whether somebody can tell what it is doing.

### 16.1 Nothing verifies that the rail actually collapsed

`ESTOP_FEEDBACK_ENABLED` is **0** in `firmware/pico-node_v4/`. The GPIO20
divider trace is cut for bench testing, so `check_power_feedback()` compiles to
nothing.

**Commanding the relay open and observing the rail drop are two different
claims, and only the first is being made.** A green vessel panel is not
evidence of the second.

The pre-flight check `pico.estop_feedback` now **WARNs on every run** while that
is true, and says what is not verified rather than reporting that a flag is off
— a crew reading `ESTOP_FEEDBACK_ENABLED is 0` learns nothing.

It is amber rather than red on purpose. The vessel is not unsafe to operate:
the hardware killswitch and RC channel 8 both cut propulsion independently.
It is *unverified*, which is a different claim, and blocking launch on it would
teach the crew to discount the verdict — the same reasoning as the mounting
check in §5.2.

To close it: set the firmware flag to 1 **and**
`ESTOP_FEEDBACK_ENABLED` in `asket_common/mode_arbitration.py` to `True` in the
same change. The cross-check test (§14) fails if the two disagree, in either
direction — a stale warning trains people to ignore ambers, and a missing one
hides a real gap.

### 16.2 There is one relay, not four

The panel read `0/4 relays closed`. There is no fourth relay and there never
was: the `4` came from `num_relays` in the **simulator's** placeholder config,
and the GUI faithfully rendered whatever length of array arrived.

The real path was never wrong — `adapters.py` builds `[relay_closed]` from the
single `relay=` field — so this existed only in simulation and mock mode, which
is exactly where this GUI gets reviewed. The sentence it formed was about the
e-stop path: three quarters of a safety mechanism appearing not to exist.

Corrected at both ends (`asket_sim/core/pico.py` and the JS mock) and pinned by
a cross-language test, because fixing one and not the other would leave sim mode
and mock mode disagreeing about the e-stop path.

The relay is now **named** — `ESC power`, GPIO21 — because unlike the hull
wiring its function is confirmed in firmware source rather than guessed, and it
carries a description saying that open-while-disarmed is correct rather than a
fault. The summary line says `ESC power open` instead of counting to one.

What stays unnamed is what is still unconfirmed: any further relay this hull
might grow, and every ESC status code beyond 0. A panel that hedges about
everything teaches an operator to discount all of it, so the two are described
differently on purpose.

### 16.3 `ESC_ARM_DELAY_MS`, pinned

Restored as a mirror in `asket_common/mode_arbitration.py` and checked against
the sketch. It is how long the firmware holds both thrusters at neutral after
closing the relay, while the ESCs boot — unpinned, it is how software comes to
read a vessel correctly waiting out its arm window as a vessel ignoring its
throttle. An unpinned constant is how the next drift starts.
