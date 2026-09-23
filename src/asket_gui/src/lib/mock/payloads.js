// Wire payloads, mirroring `gui_backend/core/payloads.py`.
//
// Same rules as the Python, for the same reasons:
//
//  * a value that is not available is `null`, never zero — on a boat, a zero is
//    a measurement;
//  * detail levels shrink the payload, they do not lie about it. A `minimal`
//    vessel frame has fewer fields; the fields it has mean exactly what they
//    mean at `full`.
//
// `test_mock_payload_shapes.py` compares the key sets produced here against the
// Python, per stream and per detail level, so a panel cannot come to depend on
// a field the real backend never sends.

const round = (value, digits) => {
  if (value === null || value === undefined || Number.isNaN(value)) return null;
  if (!Number.isFinite(value)) return null;
  const factor = 10 ** digits;
  return Math.round(value * factor) / factor;
};

export function vesselPayload(world, detail) {
  const { lat, lon } = world.position();
  const out = {
    lat: round(lat, 7),
    lon: round(lon, 7),
    heading_deg: round(world.headingDeg(), 1),
    heading_valid: world.headingValid(),
  };
  if (detail === 'minimal') return out;

  Object.assign(out, {
    cog_deg: round(world.cogDeg(), 1),
    sog_ms: round(world.sogMs(), 2),
    heading_source: world.headingSource(),
  });
  if (detail === 'reduced') return out;

  const { roll, pitch } = world.attitudeDeg();
  const gnss = world.gnss();
  Object.assign(out, {
    alt_m: 0,
    roll_deg: round(roll, 2),
    pitch_deg: round(pitch, 2),
    gnss_fix_type: gnss.fixType,
    num_sats: gnss.numSats,
    hdop: round(gnss.hdop, 2),
    distance_travelled_m: round(world.distanceTravelled, 1),
    on_survey: world.onSurvey(),
  });
  return out;
}

export function headingPayload(world, detail) {
  const heading = world.headingDeg();
  const cog = world.cogDeg();
  const sog = world.sogMs();
  const valid = world.headingValid();
  const meaningful = valid && sog >= 0.6;
  const divergence = valid ? ((heading - cog + 540) % 360) - 180 : null;
  const accuracy = world.headingAccuracyDeg();

  const out = {
    heading_deg: valid ? round(heading, 1) : null,
    source: world.headingSource(),
    valid,
    divergence_deg: round(divergence, 1),
    divergence_meaningful: meaningful,
    divergence_suspicious: Boolean(meaningful && Math.abs(divergence) > 15),
  };
  if (detail === 'full') {
    out.accuracy_deg = round(accuracy, 2);
    // Both simulated sources compute their own figure, so it is a reported one.
    // The flag exists for the real system: when MAVROS reports no accuracy the
    // backend substitutes a nominal one for the class of hardware, and the
    // panel labels that "assumed" rather than showing the two identically.
    out.accuracy_reported = valid && accuracy !== null;
    out.cog_deg = round(cog, 1);
    out.sog_ms = round(sog, 2);
    // The number that makes heading matter: about 90 cm per degree at 50 m.
    out.seabed_error_at_50m_m =
      accuracy === null ? null : round(50 * Math.tan((accuracy * Math.PI) / 180), 2);
  }
  return out;
}

const MODE_NAMES = { 0: 'ESTOP', 1: 'MANUAL', 2: 'AUTONOMOUS' };
const MODE_AUTONOMOUS = 2;

// Ch8 below this is MODE_ESTOP in the firmware. A raw SBUS count, not a
// percentage — mirrors CH8_ESTOP_MAX in world.js, pico.py and pico_state.py.
const CH8_ESTOP_MAX = 700;

// Ch7 above this is "armed" in the firmware. Mirrors ARM_THRESHOLD in the
// sketch and CH7_ARM_MIN in pico_state.py.
const CH7_ARM_MIN = 1000;

// Why the propellers cannot turn. The real backend computes this in
// `asket_common.mode_arbitration.arming_block_reason()` and ships the sentence;
// the mock has to produce the same sentences or a reviewer reads wording in
// mock mode that the boat will never send.
//
// `test_mock_payload_shapes.py` compares these four strings against the Python
// constants, so this is a mirror that cannot drift quietly.
const ARM_BLOCKED_RC_LOW = 'RC channel 7 disarmed, propulsion cannot start';
const ARM_BLOCKED_LATCHED =
  'e-stop latched, propulsion cannot start until the arm switch is cycled';
const ARM_BLOCKED_UNKNOWN = 'the vessel reports disarmed, so propulsion cannot start';
const ARM_BLOCKED_CONTRADICTORY =
  'the vessel reports disarmed although channel 7 is up — propulsion cannot '
  + 'start, and the two disagree';

export function armingBlockReason(armed, rcArmHigh, estopLatched) {
  if (armed === true) return null;
  // Not knowing is not evidence of a block: an unarmed-because-unheard-from
  // vessel gets no red line. This is the state every panel starts in.
  if (armed === null || armed === undefined) return null;
  if (rcArmHigh === false) return ARM_BLOCKED_RC_LOW;
  if (estopLatched === true) return ARM_BLOCKED_LATCHED;
  if (rcArmHigh === true) return ARM_BLOCKED_CONTRADICTORY;
  return ARM_BLOCKED_UNKNOWN;
}

export const ARM_BLOCK_STRINGS = {
  rc_low: ARM_BLOCKED_RC_LOW,
  latched: ARM_BLOCKED_LATCHED,
  unknown: ARM_BLOCKED_UNKNOWN,
  contradictory: ARM_BLOCKED_CONTRADICTORY,
};

export function picoPayload(world, detail) {
  const out = {
    mode: MODE_NAMES[world.mode] || 'UNKNOWN',
    armed: world.armed,
    estop_latched: world.estopLatched,
    // On every profile, beacon included: a GUI reading a firmware it does not
    // understand must be able to say so at any bandwidth, because every other
    // field here is then suspect.
    firmware_version: world.firmwareVersion,
    version_mismatch: false,
  };
  if (detail === 'minimal') return out;

  Object.assign(out, {
    rc_link_ok: world.rcLinkOk,
    rc_channel8_raw: world.rcChannel8,
    ch8_asserting_estop: world.rcChannel8 < CH8_ESTOP_MAX,
  });
  if (detail === 'reduced') return out;

  Object.assign(out, {
    // One relay, matching the firmware and asket_sim. This used to be four,
    // which rendered as `0/4 relays closed` — a false statement about the
    // e-stop path, in the mode the GUI gets reviewed in.
    relay_states: [world.armed],
    esc_status: [world.armed ? 0 : 1, world.armed ? 0 : 1],
    hardware_killswitch_engaged: false,
    rc_channel7_raw: world.rcChannel7,
    mode_requested_auto: world.mode === MODE_AUTONOMOUS,
    link_live: true,
    sbus_frames_ok: world.sbusFramesOk,
    sbus_frames_bad: 0,
    sbus_failsafe: !world.rcLinkOk,
    sbus_frame_lost: false,
    arming_block: armingBlockReason(
      world.armed,
      world.rcChannel7 > CH7_ARM_MIN,
      world.estopLatched,
    ),
  });
  return out;
}

export function powerPayload(world, detail) {
  const capacity = world.cfg.batteryCapacityWh;
  const soc = world.remainingWh / capacity;
  const ocv = 21 + (29.4 - 21) * (0.15 + 0.7 * soc + 0.15 * Math.tanh((soc - 0.5) * 6) / Math.tanh(3));
  const voltage = Math.max(21, Math.min(29.4, ocv));
  const current = world.avgPowerW / Math.max(1, voltage);

  const out = { state_of_charge: round(soc, 3), voltage: round(voltage, 2) };
  if (detail === 'minimal') return out;

  const enduranceS = world.avgPowerW > 1 ? (world.remainingWh / world.avgPowerW) * 3600 : null;
  Object.assign(out, { current: round(current, 2), endurance_s: round(enduranceS, 0) });
  if (detail === 'reduced') return out;

  Object.assign(out, {
    power_w: round(world.avgPowerW, 1),
    remaining_wh: round(world.remainingWh, 1),
    consumed_wh: round(world.consumedWh, 1),
  });

  // The comparison that actually matters: with one sonar covering one side the
  // survey distance roughly doubles, so "is there enough to finish" beats
  // "how much is left".
  const sog = world.sogMs();
  const remainingM = world.surveyRemainingM();
  if (sog > 0.1) {
    const surveyTimeS = remainingM / sog;
    out.survey_remaining_m = round(remainingM, 0);
    out.survey_remaining_s = round(surveyTimeS, 0);
    if (enduranceS !== null) {
      out.endurance_margin_s = round(enduranceS - surveyTimeS, 0);
      out.can_finish_survey = enduranceS > surveyTimeS;
    }
  }
  return out;
}

export function sonarPayload(world, detail) {
  const connected = !world.hasFault('sonar_dropout');
  const offset = Math.round(world.clockOffsetMs);
  const out = {
    connected,
    actual_ping_rate_hz: connected ? round(world.cfg.sonarPingRateHz, 2) : 0,
    clock_offset_ms: offset,
    clock_ok: Math.abs(offset) < 250,
    clock_compromised: Math.abs(offset) >= 1000,
  };
  if (detail !== 'full') return out;

  const { roll, pitch } = world.attitudeDeg();
  Object.assign(out, {
    commanded_ping_rate_hz: round(world.cfg.sonarPingRateHz, 2),
    ping_rate_ok: connected,
    points_per_ping: world.cfg.sonarPointsPerPing,
    valid_points_per_ping: connected ? Math.round(world.cfg.sonarPointsPerPing * 0.8) : 0,
    speed_of_sound: 1500,
    range_setting_m: round(world.cfg.sonarRangeM, 1),
    gain_setting: world.cfg.sonarGain,
    packet_loss_ratio: world.hasFault('sonar_packet_loss') ? 0.12 : 0,
    pitch_deg: round(pitch, 2),
    roll_deg: round(roll, 2),
    checksum_errors: 0,
    bytes_discarded: 0,
    seconds_since_data: connected ? 0 : 30,
    rate_from_device: connected,
  });
  return out;
}

export function lidarPayload(world, detail) {
  const scan = world.lidarScan();
  const step = { full: 1, reduced: 4, minimal: 12 }[detail];
  const decimate = (pairs) => pairs.filter((_, i) => i % step === 0);

  const out = {
    rotation_hz: round(scan.rotationHz, 2),
    points_per_revolution: scan.pointsPerRevolution ?? 0,
    // Beams swept, as against beams that returned something. Open water and a
    // blind sensor both report zero returns; only this tells them apart.
    beams_per_revolution: scan.beamsPerRevolution ?? 0,
    nearest_range_m: round(scan.nearestRangeM, 2),
    nearest_bearing_deg: round(scan.nearestBearingDeg, 1),
    filtered: decimate(scan.filtered),
  };
  // Raw only at full detail: the operator needs both to tell whether the sensor
  // or the filter is at fault, and it doubles the payload.
  if (detail === 'full') out.raw = decimate(scan.raw);
  return out;
}

export function linkPayload(world, detail, context) {
  const link = world.link();
  const out = {
    active_link: link.activeLink,
    quality: round(link.quality, 2),
    rtt_ms: round(link.rttMs, 0),
    profile: context.profile,
    profile_manual: context.manual,
  };
  if (detail === 'full') {
    Object.assign(out, {
      capacity_bytes_per_s: round(link.capacityBytesPerS, 0),
      rate_bytes_per_s: round(context.rateBytesPerS, 0),
      connected_clients: 1,
      distance_m: round(link.distanceM, 0),
      // What the radio sees. Null on a bearer the budget does not model, and
      // null is the honest answer there — a zero would render as "no signal".
      rssi_dbm: round(link.rssiDbm, 1),
      rssi_source: link.rssiSource ?? null,
      expected_rssi_dbm: round(link.expectedRssiDbm, 1),
      headroom_db: round(link.headroomDb, 1),
      mcs_index: link.mcsIndex,
      phy_mbps: round(link.phyMbps, 1),
      // Geometry, which needs no radio telemetry at all, and which is reported
      // even when the link is gone — that is when it is worth having.
      off_boresight_deg: round(link.offBoresightDeg, 1),
      sector_beamwidth_deg: round(link.sectorBeamwidthDeg, 0),
      vessel_off_boresight_deg: round(link.vesselOffBoresightDeg, 1),
      multipath_db: round(link.multipathDb, 1),
      sea_state_m: round(link.seaStateM, 2),
    });
  }
  return out;
}

export function missionPayload(world) {
  const elapsed = world.missionState === 'RECORDING'
    ? (world.utcMs - world.missionStartUtc) / 1000
    : 0;
  return {
    state: world.missionState,
    name: world.missionName,
    mission_dir: world.missionName ? `/data/missions/${world.missionName}` : '',
    elapsed_s: round(elapsed, 1),
    bytes_written: Math.round(world.missionBytes),
    disk_free_bytes: world.diskFreeBytes(),
    disk_total_bytes: world.cfg.diskTotalBytes,
    estimated_remaining_s:
      world.missionState === 'RECORDING'
        ? Math.round(Math.max(0, world.diskFreeBytes() - 1024 ** 3) / (3.2 * 1024 ** 2))
        : null,
    trajectory_records: Math.round(elapsed * 10),
    error_message: world.missionError || '',
    missions: world.missions,
    destinations: world.mockDestinations || [],
    no_fast_path_message:
      'Transfer unavailable — connect a USB drive or an Ethernet cable. Mission data is ' +
      'several gigabytes per hour and will not go over the wireless link.',
  };
}

export function trackPayload(world, cursor, step) {
  const slice = world.track.slice(cursor);
  return {
    from: cursor,
    total: world.track.length,
    points: step > 1 ? slice.filter((_, i) => i % step === 0) : slice,
  };
}

export function coveragePayload(world, cursor, step) {
  const slice = world.coverage.slice(cursor);
  return {
    from: cursor,
    total: world.coverage.length,
    side: world.cfg.sonarSide,
    segments: step > 1 ? slice.filter((_, i) => i % step === 0) : slice,
  };
}
