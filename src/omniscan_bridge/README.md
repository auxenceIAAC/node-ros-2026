# omniscan_bridge

Cerulean Omniscan 3D → ROS 2. Speaks the Ping Protocol over Ethernet
(port 62312), publishes vessel-frame points and a health topic that tells you
what the sonar is actually doing.

The unit is the **AIO variant**: beamforming happens inside the sonar's own
Pi 5, so the Jetson receives processed data. One unit, so **one side only** —
which is why coverage gaps are easy to leave and hard to notice.

## Published

| Topic | Type | Rate |
|---|---|---|
| `/sonar/points` | `sensor_msgs/PointCloud2` | up to 20 Hz, vessel frame, **not georeferenced** |
| `/sonar/status` | `asket_interfaces/SonarStatus` | 1 Hz |
| `/sonar/attitude` | `sensor_msgs/Imu` | as received |
| `/diagnostics` | `diagnostic_msgs/DiagnosticArray` | 1 Hz |

## Services

* `~/set_ping_parameters` — range, gain, ping rate. **Adjustable at runtime**,
  because the range is meant to be tuned on site rather than fixed at launch.
  Gain `-1` is auto, which is Cerulean's recommendation.
* `~/start_pinging`, `~/stop_pinging` — via the documented `ping_enable` flag.
  Sending a rate of zero would be a different thing, and not a supported one.

## Where the mounting geometry comes from

Two files, and the second wins where it has an answer.

`config/mounting.yaml` is the package's own default and lives in the source
tree. The **setup file** — written by the GUI's Setup page, at
`asket_common.setup_profile.default_setup_path()` — is applied on top of it,
field by field, by `core/mounting.py`.

Only fields somebody has actually *answered* take effect. Every field carries a
shipped default inside the setup profile, so taking values without checking
provenance would mean that saving anything at all on the Setup page silently
reverted every measurement in `mounting.yaml`. That is the one failure this
must not have.

The page never edits `mounting.yaml`. A page that wrote into six packages'
config files would have to know the layout of every one of them, every future
package would have to be taught about it, and a partly applied write would
leave the vessel in a state no file describes.

```bash
# What mounting.yaml says on its own, ignoring the Setup page:
ros2 run omniscan_bridge omniscan_bridge_node --ros-args -p setup_path:=none
```

`~/reload_mounting` re-reads both and reports the geometry digest it is now
running on. That digest is how the Setup page tells "the file was written" from
"the sonar is placing soundings by it" — it predicts the digest from the numbers
it sent and waits for this node to report that exact one. Nothing else counts as
the change having taken effect.

`measured` is earned by all six geometry fields. A tilt measured on the page
with the lever arm still on an unchecked default is not a measured geometry,
and the pre-flight's amber warning is right to stay up.

## What it does not do

**It does not georeference.** Points come out in the vessel frame. Position and
heading are recorded alongside the raw stream by `mission_recorder` and merged
afterwards on `utc_msec` (brief, section 5). Fusing here would make the point
cloud depend on GNSS availability, so a fix dropout would corrupt the data
rather than merely annotating it.

## The parser

`core/ping_protocol.py` is the codec — frame layout, checksums, payloads. It has
no ROS dependency and no third-party dependency, and the binary layout is
written out explicitly in the module docstring because it is meant to be read.

`core/parser.py` is what survives a real link: partial frames held until
complete, corrupt frames rejected with resynchronisation on the next marker,
truncation counted, and ping-number gaps measured so `packet_loss_ratio` is a
measurement rather than a guess.

Every layout is transcribed from **Cerulean's published documentation**
(docs.ceruleansonar.com) and cited against the message it defines. The
frame layout was confirmed byte for byte; several payload layouts had been
guessed wrong and are corrected — see the table in `docs/open_questions.md`.

Two things the documentation does not state, and which are therefore
assumptions:

* **Endianness.** Little-endian, as in the Blue Robotics protocol this
  descends from. A wrong guess fails immediately: a big-endian `num_points`
  would be astronomically large and the length cross-check would reject it.
* **`vec3`.** Not in the nomenclature table; taken as three `float`, the only
  reading consistent with the documented fixed payload sizes.

The parser still cross-checks the declared point count against the payload
length and flags a mismatch, because a weak checksum will not catch a
transposition and a wrong layout must fail loudly rather than produce a
plausible cloud of nonsense.

## The conversion

`core/geometry.py`. The sonar reports an angle and a time of flight; turning
that into a point is ours to do:

```
range = tof * speed_of_sound / 2
y     = range * sin(angle)        # sensor frame, lateral
z     = range * cos(angle)        # sensor frame, along the boresight
```

then mounting tilt → mounting yaw/pitch → lever arm from the GNSS antenna →
vessel attitude. Output is REP-103: x forward, y port, z up.

Two details worth knowing:

* The **speed of sound travels with every ping** and is used as reported.
  Assuming 1500 m/s when the sonar used 1520 puts a 20 m bottom 27 cm out,
  systematically, across the whole survey.
* The **lever arm** from the GNSS antenna to the transducer must be measured to
  the centimetre. It is a systematic offset that no post-processing will find.

### mounting.yaml, and the check that will not stop asking

The geometry lives in `config/mounting.yaml` rather than in node parameters,
because it needs to carry one extra thing: whether anybody has actually
measured it.

```yaml
measured: false          # <- the only line that matters
tilt_deg: 35.0
lever_x_m: -0.20
```

`core/mounting.py` loads it and returns the numbers **and their provenance**.
It never raises: taking the sonar down over a config typo is the wrong trade at
sea, so it falls back to defaults and records why. Making noise is the
pre-flight's job — `sonar.mounting` returns:

| | |
|---|---|
| `measured: true` | **PASS**, naming who measured it and when |
| `measured: false` | **WARN** — PROVISIONAL, on every run, until somebody measures the boat |
| no file at all | **WARN** — the geometry is a hard-coded guess |
| a file that will not load | **FAIL** — somebody may have measured it and the numbers are being ignored |
| an unrecognised field | **WARN**, naming it — `lever_y` is not `lever_y_m`, and the default would otherwise apply silently |

The provenance travels to the check through this node's `/diagnostics`, not by
the check re-reading the file. Editing `mounting.yaml` without restarting must
not turn the check green while this node is still applying the old numbers.

## The clock — and why this bridge cannot fix it

`clock_offset_ms` — the sonar's `utc_msec` against the Jetson's clock — matters
more than it looks. The whole post-mission fusion strategy rests on it. If it
drifts and nobody notices, the survey is not degraded, it is worthless, and
nobody finds out until the data is opened back home. So it is measured
continuously (as a **median**, so one late packet cannot cry wolf), surfaced in
`SonarStatus`, and escalated to an alarm in `/diagnostics`.

**There is no NTP packet.** Cerulean removed `SET_NTP_INFO`; the NTP server is
configured on the device itself, and its default is an internet host. There is
no internet in the field. So this is a **field setup step** — see
`docs/SETUP.md` — and all the bridge can do is measure the offset and shout.

The bridge does re-send the **ping parameters** on first contact and after every
reconnect: a device that reboots mid-mission comes back with defaults, and
silently surveying at the wrong range for the second half is exactly the failure
nobody notices in time.

## Testing in sim

No sonar, no socket, no ROS:

```bash
pytest src/omniscan_bridge
```

With a fake device on a real socket:

```bash
python3 -m asket_sim.core.fake_sonar_server --port 62312 &
ros2 run omniscan_bridge omniscan_bridge_node --ros-args -p host:=127.0.0.1
```

Against a synthetic raw file:

```bash
python3 -m asket_sim.make_sonar_raw --duration 60 --corrupt --out /tmp/damaged.bin
```

`test/test_end_to_end.py` runs that whole path — synthetic stream in,
vessel-frame point clouds and health out — including the damaged-stream case.
