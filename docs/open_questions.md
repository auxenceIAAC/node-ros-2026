# Open questions

The architecture brief listed six; three more surfaced during implementation.

**All nine have answers.** Q3 and Q8 changed the design and are written up
below. Three remain open in the sense that only the hardware can close them —
the survey area (Q1), the mounting geometry (Q2) and the `pico_bridge`
interface (Q7) — and each is held by a config file plus a check that says so
out loud rather than by an assumption buried in code.

Anything still standing on a provisional value is marked `PROVISIONAL` in the
config file that holds it, so it is greppable:

```bash
grep -rn PROVISIONAL src/
```

Nothing provisional is baked into code — it lives in YAML and can be changed
without a rebuild.

| # | Question | Status | Where |
|---|---|---|---|
| 1 | Survey area depth and profile in Namibia (harbour vs open coast) | **Stays provisional — will be tuned on site.** 30 m sonar range, 8/15 m lidar alarm/warn radii. Sonar range, gain and rate are adjustable **at runtime from the GUI**, not only at launch. | `src/asket_bringup/config/mission_defaults.yaml`, GUI sonar panel |
| 2 | Sonar mounting angle and lever arm from the GNSS antenna | **Must be physically measured; still unknown.** 35° down, lever arm (0.20 m aft, 0.35 m starboard, 0.15 m below the antenna). The geometry now lives in its own file carrying one line — `measured: false` — and the pre-flight check `sonar.mounting` returns **WARN** naming the file until somebody sets it true, so it cannot be deployed on defaults by accident. A file that exists but fails to load is **FAIL**, not WARN: that is the case where somebody did measure the vessel and the numbers are being silently ignored. The check reads the provenance the **bridge reported**, not the file, so editing it without restarting cannot turn the check green while the old numbers are still in use. | `src/omniscan_bridge/config/mounting.yaml` |
| 3 | Can SonarView import an externally recorded trajectory to georeference a raw log? | **ANSWERED: no.** Position and heading must be inside the `.svlog` as `NMEA_WRAPPER` packets written at capture time; a log without them cannot be georeferenced or exported at all. Option B survives — the recorder still writes the two streams separately — and `mission_recorder.merge_svlog` merges them afterwards into a valid `.svlog` with `$GPGGA`/`$GPHDT` interleaved on `utc_msec`. Optional live interleaving is available as `write_live_svlog`. | `src/mission_recorder/core/svlog.py` |
| 4 | UM982 purchase confirmed? Antenna baseline length? | **Purchase decision pending; 1.0 m baseline confirmed as the right assumption.** The panel reads the *reported* accuracy from MAVROS whenever one is available rather than displaying a constant, and marks the figure **"assumed"** when the receiver reported none — a UM982 that has lost an antenna reports a degraded accuracy, and substituting the nominal 0.2° would hide exactly that. | `src/asket_bringup/config/mission_defaults.yaml` |
| 5 | Mission duration target | **3 h confirmed** for disk sizing. **Battery endurance binds before disk does**, by a wide margin — so the disk check reports *hours of recording* and names the battery when that is the shorter of the two, rather than letting "135 hours free" be read as an endurance. The power panel's endurance-versus-survey comparison is the other half. | `src/asket_bringup/config/mission_defaults.yaml` |
| 6 | Shore-based or from a support vessel? | **Shore-based, confirmed**, and now with named hardware: a MikroTik mANTBox 15s sector on a 2.9 m tripod ashore and a NetMetal ax on the boat, both WiFi 6. The link is modelled from a budget rather than a curve — see Q10 — and the thresholds in YAML now have decibels behind them. | `src/gui_backend/config/link_profiles.yaml`, `src/asket_common/asket_common/link_budget.py` |
| 6a | Is there a fallback bearer at the site at all? | Decides whether `reduced` and `minimal` are real states or theatre. With one directional link the realistic ladder is *working* and *gone*. | **Assumed none.** `LinkConfig.cellular_available` now defaults false, which is the harder case and the one the GUI must handle; the `link_degraded` fault still forces a 4G bearer so the `reduced` profile stays reviewable. Auxence is raising it with the club — it is a safety question before it is a GUI one. |

## What the Cerulean documentation corrected (Q8)

The layouts transcribed from the brief were wrong in ways that would have
produced plausible-looking nonsense. All are now taken from the published
definitions.

| | Transcribed from the brief | Actually |
|---|---|---|
| `OS3D_POINT_SET` header | 20 bytes: `u32 ping, float sos, u64 utc, u32 count` | **80 bytes**, `num_points` is a `u16` at offset 8, and it carries `pwr_up_msec`, `version`, `device_number` and three power thresholds |
| `pt_type` | 0 = no return, 1 = bottom | **0 = unclassified, 1 = bottom, 2 = water column.** Only bottom points belong in bathymetry — a fish is not seabed |
| `ATTITUDE_REPORT` | `float pitch, float roll` | **An up vector** (`vec3`) in the device frame, plus `utc_msec`, `pwr_up_msec`, `channel_number`. Angles are derived from it |
| `END_PING_INFO` | `ping_number, num_results, duration` | **80 bytes**, including `ping_hz_realized` — the rate the device actually achieved, which is better than timing arrivals here |
| `OS3D_SET_PING_PARAMETERS` | `u32 range_mm, u8 gain, float rate_hz` | **36 bytes**: `start_m`/`end_m` floats, `gain_index` **i16 where −1 is auto**, `msec_per_ping` (a period, not a rate), and a `ping_enable` flag. `enable_atof_data` **must be true or no points are produced at all** |
| `SET_NTP_URL` (18) | Sent at startup to point the sonar at our NTP server | **The packet was removed.** NTP is configured on the device and defaults to an internet host. See `docs/SETUP.md` — this is a field setup step the bridge cannot perform |
| Frame layout | Transcribed from the brief | **Confirmed correct**, byte for byte |

Sources: the Cerulean Ping Protocol "Universal Packet Format", "Index of packet
types", and the Omniscan 3D API pages, fetched September 2026.

## Added during implementation

| # | Question | Why it matters | Interim handling |
|---|---|---|---|
| 7 | What message type does the existing `pico_bridge` publish, and on what topic? (`ros2 topic info -v` output pending) | `gui_backend` must read confirmed vessel mode from it, and the brief forbids modifying `pico_bridge`. | Logical streams are mapped to topics/types/adapters in `src/gui_backend/config/topics.yaml`. In sim we publish `asket_interfaces/msg/PicoStatus`; pointing the adapter at the real message is a config change plus one adapter function. |
| 8 | Exact byte layout of `OS3D_POINT_SET` (3104), `ATTITUDE_REPORT` (504) and `END_PING_INFO` (3010) payloads. | The brief described the fields but not their order or widths. | **ANSWERED from Cerulean's published documentation, and the transcribed guesses were wrong.** All layouts are now taken from docs.ceruleansonar.com and cited in `ping_protocol.py`. See the table below for what changed. Two things the docs do not state remain assumptions: **endianness** (little-endian assumed) and **`vec3`** (three `float`, the only reading consistent with the fixed payload sizes). Both would fail loudly rather than silently. |
| 9 | Which Jetson mount point(s) count as the "USB fast path" for export? | **A headless Jetson has no desktop session, so nothing auto-mounts and `/media/*` is empty.** Detection also has to test the actual mount rather than the directory's existence, or it will offer the eMMC. | udev rule and systemd unit in `deploy/`, documented in `docs/SETUP.md`; detection verifies the mount. |
| 10 | The shore link's physical parameters: sector beamwidth, whether the boat's antenna is directional, and how high above the water it sits. | The link model is now the thing the camera panel's staleness thresholds get tuned against, so a wrong parameter here produces a GUI tuned for a failure the boat does not have — which is the mistake this whole exercise was meant to avoid. | **Modelled with published or conventional figures, every one marked `PROVISIONAL` in `link_budget.py`.** Three are worth checking before the first deployment, in order: the **sector width** (120° published for the mANTBox 15s, but a ~60° sector was what was described — it halves the working area before the tripod has to be turned); **whether the HGO-Antenna-OUT is directional** (if it is and it faces forward, it points away from the station on every outbound survey line and costs 25 dB — the mounting becomes a design decision, so the model defaults to an omni until somebody confirms the part); and the **boat antenna height** (1.0 m assumed; the two-ray term goes as the product of the two heights, so raising it is worth more than any transmit power we are allowed to use). |
| 11 | Where does the shore station stand, and which way does it face? | Bearing from the sector's boresight is pure geometry and needs no radio telemetry, which makes it the one link number that survives the MikroTik exposing nothing readable — and the only one that says what is *about* to happen rather than what already has. | **Not yet collected anywhere.** The tripod moves between missions and can be turned by hand mid-mission, so it is not a surveyed installation: it belongs on the mission setup page, alongside the sonar mounting geometry and the battery capacity. Until that exists the values are simulator config. |
