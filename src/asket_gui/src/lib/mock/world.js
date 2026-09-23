// A simulated vessel, running entirely in the browser.
//
// This is a deliberate, smaller sibling of `asket_sim` in Python. It exists so
// the GUI can be developed and reviewed on a laptop with nothing installed but
// Node — no ROS 2, no Python backend, no Jetson, no boat.
//
// It is **not** a stub. It models the things the interface has to get right:
//
//  * a vessel following a lawnmower pattern in a cross-current, so course over
//    ground genuinely diverges from heading;
//  * a magnetometer whose error grows with throttle, which is the real failure
//    mode on this hull and the reason the heading panel exists;
//  * a Pico that takes time to confirm a mode change, and can fail to;
//  * a link built from a real budget — sector pattern, path loss, the
//    modulation staircase and the cliff under it — so it fails the way a
//    directional link over water actually fails, which is suddenly;
//  * a coverage ribbon that stops during turns, sonar dropouts and heading
//    loss, so gaps on the map are real gaps.
//
// What it deliberately does not model is anything the GUI cannot see. There is
// no hydrodynamics here, and there does not need to be.

import * as LB from './linkBudget.js';

const METRES_PER_DEG_LAT = (Math.PI * 6371008.8) / 180;

// Mirrors ALIGNMENT_LOST_DEG / ALIGNMENT_SLEW_DEG_PER_S in asket_sim/core/link.py.
// A full reversal, because a 120-degree sector with 19 dB in hand shrugs off a
// nudge — see the note there; it is a finding about the gear, not a tuning.
const ALIGNMENT_LOST_DEG = 180;
const ALIGNMENT_SLEW_DEG_PER_S = 90;

export const MODE_ESTOP = 0;
export const MODE_MANUAL = 1;
export const MODE_AUTONOMOUS = 2;
export const MODE_NAMES = { 0: 'ESTOP', 1: 'MANUAL', 2: 'AUTONOMOUS' };

// Raw SBUS counts, as pico-node_v4 reports them. Eleven bits clamped to this
// range with 991 at centre — NOT percentages. Mirrors SBUS_MIN/MID/MAX in
// asket_sim/core/pico.py and gui_backend/core/pico_state.py; a test compares
// the payloads these produce against the backend's, so the three cannot drift.
export const SBUS_MIN = 172;
export const SBUS_MID = 991;
export const SBUS_MAX = 1811;

// The firmware's own thresholds, on that scale. Ch8 below this is MODE_ESTOP.
export const CH8_ESTOP_MAX = 700;
export const CH7_ARM_MIN = 1000;

// FW_VERSION in firmware/pico-node_v4/pico-node_v4.ino.
export const FIRMWARE_VERSION = 4;

const wrap180 = (deg) => ((deg + 180) % 360 + 360) % 360 - 180;
const wrap360 = (deg) => ((deg % 360) + 360) % 360;
const bearingOf = (east, north) => wrap360((Math.atan2(east, north) * 180) / Math.PI);

function metresPerDegLon(lat) {
  return METRES_PER_DEG_LAT * Math.cos((lat * Math.PI) / 180);
}

/** Deterministic noise, so a reviewer sees the same run twice. */
function makeRandom(seed) {
  let state = seed >>> 0 || 1;
  return () => {
    state ^= state << 13;
    state ^= state >>> 17;
    state ^= state << 5;
    return ((state >>> 0) % 100000) / 100000;
  };
}

function gauss(random, sigma) {
  // Two uniforms into one normal. Good enough for sensor noise.
  const u = Math.max(1e-6, random());
  const v = random();
  return sigma * Math.sqrt(-2 * Math.log(u)) * Math.cos(2 * Math.PI * v);
}

export const DEFAULTS = {
  // Walvis Bay. PROVISIONAL, same as the Python — see docs/open_questions.md.
  originLat: -22.9576,
  originLon: 14.5053,
  surveyHeadingDeg: 20,
  lineLengthM: 300,
  lineSpacingM: 20,
  numLines: 6,
  sonarSide: 'starboard',
  //: How far outside the survey box the geofence sits.
  geofenceMarginM: 40,

  surveySpeedMs: 1.5,
  turnSpeedMs: 0.8,
  maxTurnRateDps: 25,
  waypointRadiusM: 4,

  currentBearingDeg: 210,
  currentSpeedMs: 0.25,

  magBiasDeg: 3,
  magThrottleGainDeg: 6,
  magNoiseDeg: 0.8,
  gnssCompassNoiseDeg: 0.2,

  waveHeightM: 0.4,
  wavePeriodS: 4.5,

  batteryCapacityWh: 1200,
  hotelLoadW: 85,
  propulsionMaxW: 600,

  // The shore station, 2.5 km off the survey box. Mirrors
  // DEFAULT_STATION_NORTH_M, and the distance is not arbitrary: with the
  // datasheet's real sensitivities this link holds ~55 dB of headroom at
  // 100 m against a sector whose front-to-back ratio is 25 dB, so close in
  // nothing can degrade it and every degraded state this GUI exists to render
  // was unreachable in the mock.
  stationEastM: 0,
  stationNorthM: -2500,
  stationHeightM: 2.9,
  stationBoresightDeg: 0,
  vesselAntennaHeightM: 1.0,
  waveHeightM: 0.5,

  sonarRangeM: 30,
  sonarGain: 4,
  sonarPingRateHz: 5,
  sonarPointsPerPing: 256,
  sonarTiltDeg: 35,
  seabedDepthM: 18,

  diskTotalBytes: 512 * 1024 ** 3,
  diskUsedBytes: 40 * 1024 ** 3,

  obstacles: [
    { east: 54.4, north: 122.1, radius: 1.2, name: 'channel buoy' },
    { east: 74.0, north: 239.0, radius: 3.5, name: 'rock' },
    { east: 132.0, north: 26.5, radius: 6.0, name: 'moored vessel' },
  ],
};

function buildPlan(cfg) {
  const along = {
    e: Math.sin((cfg.surveyHeadingDeg * Math.PI) / 180),
    n: Math.cos((cfg.surveyHeadingDeg * Math.PI) / 180),
  };
  const across = {
    e: Math.sin(((cfg.surveyHeadingDeg + 90) * Math.PI) / 180),
    n: Math.cos(((cfg.surveyHeadingDeg + 90) * Math.PI) / 180),
  };

  const waypoints = [];
  for (let i = 0; i < cfg.numLines; i += 1) {
    const offset = i * cfg.lineSpacingM;
    const base = { e: across.e * offset, n: across.n * offset };
    const far = {
      e: base.e + along.e * cfg.lineLengthM,
      n: base.n + along.n * cfg.lineLengthM,
    };
    const [a, b] = i % 2 === 1 ? [far, base] : [base, far];
    // The leg arriving at `a` is the turn from the previous line.
    waypoints.push({ ...a, line: i, onSurvey: false });
    waypoints.push({ ...b, line: i, onSurvey: true });
  }
  return waypoints;
}

export class MockWorld {
  constructor(options = {}) {
    this.cfg = { ...DEFAULTS, ...options };
    this.random = makeRandom(options.seed || 20260908);
    this.plan = buildPlan(this.cfg);

    this.t = 0;
    this.startUtcMs = Date.now();

    const first = this.plan[0];
    const second = this.plan[1];
    this.east = first.e;
    this.north = first.n;
    this.trueHeading = bearingOf(second.e - first.e, second.n - first.n);
    this.speed = 0;
    this.throttle = 0;
    this.wpIndex = 1;
    this.distanceTravelled = 0;
    this.finished = false;
    this.velocity = { e: 0, n: 0 };
    this.magWalk = 0;
    this.fade = 0;
    this.alignmentErrorDeg = 0;

    this.mode = MODE_MANUAL;
    this.armed = false;
    this.estopLatched = false;
    this.pendingMode = null;
    this.rcLinkOk = true;
    // Raw SBUS counts, as the firmware reports them: 172..1811, 991 at centre.
    // Not percentages. The firmware's thresholds are on this scale — Ch8 below
    // 700 is ESTOP, Ch7 above 1000 is armed — and an earlier version of this
    // file used 0..100, which is how a threshold of "< 25" came to be applied
    // to a raw count and never fire.
    this.rcChannel8 = SBUS_MAX;
    this.rcChannel7 = SBUS_MAX;
    this.firmwareVersion = FIRMWARE_VERSION;
    // Counted rather than faked: a radio that has stopped delivering frames
    // looks like this number standing still, and that is the only warning the
    // system gives before the link actually drops.
    this.sbusFramesOk = 0;

    this.remainingWh = this.cfg.batteryCapacityWh * 0.95;
    this.consumedWh = 0;
    this.avgPowerW = this.cfg.hotelLoadW;

    this.diskUsedBytes = this.cfg.diskUsedBytes;

    this.pingNumber = 0;
    this.clockOffsetMs = 0;

    this.track = [];
    this.coverage = [];
    this.lastLayerT = -1;

    this.missionState = 'IDLE';
    this.missionName = '';
    this.missionStartUtc = 0;
    this.missionBytes = 0;
    this.missions = [];

    // Scenario switches, driven by the dev control panel.
    this.faults = new Set();
    this.confirmDelayS = 0.6;
    this.confirmAlwaysFails = false;
  }

  /**
   * Timestamps are WALL clock, always.
   *
   * `t` is simulated seconds and drives the physics, so a time scale above 1
   * makes the vessel cover the survey faster. But the timestamps on the wire
   * must stay real: the client estimates clock skew from them, and a world
   * clock running ahead of the browser's would make every value render as
   * arriving from the future — i.e. permanently fresh, which is the one thing
   * data age must never be able to say wrongly.
   */
  get utcMs() {
    return Date.now();
  }

  // -- scenarios --------------------------------------------------------

  setFault(name, active) {
    if (active) this.faults.add(name);
    else this.faults.delete(name);
  }

  hasFault(name) {
    return this.faults.has(name);
  }

  requestMode(mode) {
    if (!(mode in MODE_NAMES)) return false;
    if (this.confirmAlwaysFails) {
      // Accepted, then silently lost. The nastiest case, and the one the GUI's
      // command timeout exists for.
      return true;
    }
    this.pendingMode = { mode, at: this.t + this.confirmDelayS };
    return true;
  }

  // -- stepping ---------------------------------------------------------

  step(dt) {
    this.t += dt;
    this.#applyFaults(dt);
    this.#stepVessel(dt);
    this.#stepPico(dt);
    this.#stepBattery(dt);
    this.#stepLink(dt);
    this.#stepSonar(dt);
    this.#accumulateLayers();
    this.#stepMission(dt);
  }

  #applyFaults(dt) {
    this.rcLinkOk = !this.hasFault('rc_link_loss');
    if (this.hasFault('clock_drift')) this.clockOffsetMs += 40 * dt;
    else this.clockOffsetMs = Math.max(0, this.clockOffsetMs - 200 * dt);

    if (this.hasFault('disk_full')) {
      this.diskUsedBytes = this.cfg.diskTotalBytes - 8 * 1024 ** 2;
    } else {
      this.diskUsedBytes = this.cfg.diskUsedBytes;
    }

    if (this.hasFault('low_battery') && this.remainingWh > this.cfg.batteryCapacityWh * 0.08) {
      this.remainingWh = this.cfg.batteryCapacityWh * 0.08;
    }
  }

  #stepVessel(dt) {
    const cfg = this.cfg;
    if (this.finished) {
      this.speed = Math.max(0, this.speed - 0.5 * dt);
      this.throttle = 0;
    } else {
      let target = this.plan[this.wpIndex];
      let toE = target.e - this.east;
      let toN = target.n - this.north;

      if (Math.hypot(toE, toN) < cfg.waypointRadiusM) {
        this.wpIndex += 1;
        if (this.wpIndex >= this.plan.length) {
          this.finished = true;
        } else {
          target = this.plan[this.wpIndex];
          toE = target.e - this.east;
          toN = target.n - this.north;
        }
      }

      if (!this.finished) {
        const desired = bearingOf(toE, toN);
        const error = wrap180(desired - this.trueHeading);
        const maxStep = cfg.maxTurnRateDps * dt;
        this.trueHeading = wrap360(
          this.trueHeading + Math.max(-maxStep, Math.min(maxStep, error)),
        );

        const want = Math.abs(error) < 20 ? cfg.surveySpeedMs : cfg.turnSpeedMs;
        this.speed += (want - this.speed) * Math.min(1, dt * 0.8);
        this.throttle = Math.min(
          1,
          (this.speed / cfg.surveySpeedMs) * 0.6 + Math.abs(error) / 180,
        );
      }
    }

    const rad = (this.trueHeading * Math.PI) / 180;
    const currentRad = (cfg.currentBearingDeg * Math.PI) / 180;
    const ve = this.speed * Math.sin(rad) + cfg.currentSpeedMs * Math.sin(currentRad);
    const vn = this.speed * Math.cos(rad) + cfg.currentSpeedMs * Math.cos(currentRad);
    this.east += ve * dt;
    this.north += vn * dt;
    this.distanceTravelled += Math.hypot(ve * dt, vn * dt);
    this.velocity = { e: ve, n: vn };
  }

  #stepPico(dt = 0) {
    // One decoded frame every 14 ms, as a real receiver delivers them.
    if (this.rcLinkOk) this.sbusFramesOk += Math.max(1, Math.round(dt / 0.014));

    if (!this.pendingMode) return;
    if (this.t < this.pendingMode.at) return;
    const { mode } = this.pendingMode;
    this.pendingMode = null;

    // Hardware wins: no software request leaves ESTOP while the RC channel is
    // asserting it. Software can observe the killswitch; it can never move it.
    const blocked = this.rcChannel8 < CH8_ESTOP_MAX;
    if (blocked && mode !== MODE_ESTOP) return;

    this.mode = mode;
    this.estopLatched = mode === MODE_ESTOP;
    this.armed = mode !== MODE_ESTOP;
  }

  #stepBattery(dt) {
    const cfg = this.cfg;
    const throttle = this.armed ? this.throttle : 0;
    const power = cfg.hotelLoadW + cfg.propulsionMaxW * throttle ** 3;
    const wh = (power * dt) / 3600;
    this.remainingWh = Math.max(0, this.remainingWh - wh);
    this.consumedWh += wh;
    this.avgPowerW += (power - this.avgPowerW) * Math.min(1, dt / 60);
  }

  #stepLink(dt) {
    // Ornstein-Uhlenbeck fading, advanced by time rather than by call, for the
    // same reason as the Python: a read that changes what it reads makes the
    // "slow" fade run as fast as whatever happens to sample it.
    const tau = 12;
    const decay = Math.exp(-dt / tau);
    // In decibels, like the Python, so a fade can push the link across a
    // modulation boundary rather than nudging an abstract quality score.
    this.fade = decay * this.fade + gauss(this.random, 1.5 * Math.sqrt(1 - decay * decay));

    // The tripod slews rather than teleporting, so the collapse has a shape.
    // The GUI has to be right about the second in the middle, not only about
    // the two ends.
    const target = this.hasFault('link_alignment_lost') ? ALIGNMENT_LOST_DEG : 0;
    const delta = target - this.alignmentErrorDeg;
    if (delta !== 0) {
      const travel = ALIGNMENT_SLEW_DEG_PER_S * dt;
      this.alignmentErrorDeg =
        Math.abs(delta) <= travel
          ? target
          : this.alignmentErrorDeg + Math.sign(delta) * travel;
    }
  }

  #stepSonar(dt) {
    if (this.hasFault('sonar_dropout')) return;
    this.pingNumber += Math.max(0, Math.round(this.cfg.sonarPingRateHz * dt));
  }

  #accumulateLayers() {
    // Cadenced on SIMULATED time, so a fast-forwarded survey still paints a
    // ribbon at the same spatial resolution rather than in 30 m steps.
    if (this.t - this.lastLayerT < 1) return;
    this.lastLayerT = this.t;

    const { lat, lon } = this.position();
    this.track.push([Number(lon.toFixed(7)), Number(lat.toFixed(7))]);
    if (this.track.length > 4000) this.track.splice(0, this.track.length - 4000);

    const wp = this.plan[Math.min(this.wpIndex, this.plan.length - 1)];
    const painting =
      wp.onSurvey &&
      !this.finished &&
      this.headingValid() &&
      !this.hasFault('sonar_dropout');

    if (!painting) {
      // A gap is a gap. With one sonar unit covering one side only, a ribbon
      // that closed over a dropout would claim seabed nobody ensonified.
      this.coverage.push(null);
    } else {
      const tilt = (this.cfg.sonarTiltDeg * Math.PI) / 180;
      const depth = this.cfg.seabedDepthM;
      const byRange = this.cfg.sonarRangeM * Math.sin(tilt);
      const byDepth = Math.min(this.cfg.sonarRangeM, depth / Math.cos(tilt)) * Math.sin(tilt);
      const halfWidth = Math.max(0, Math.min(byRange, byDepth));
      const nadir = depth * Math.tan(Math.max(0, tilt - (30 * Math.PI) / 180));
      this.coverage.push([
        Number(lat.toFixed(7)),
        Number(lon.toFixed(7)),
        Number(this.headingDeg().toFixed(1)),
        Number(halfWidth.toFixed(1)),
        Number(nadir.toFixed(1)),
      ]);
    }
    if (this.coverage.length > 4000) this.coverage.splice(0, this.coverage.length - 4000);
  }

  #stepMission(dt) {
    if (this.missionState !== 'RECORDING') return;
    // Roughly what a real mission writes: sonar dominates, at a few MB/s.
    this.missionBytes += 3.2 * 1024 ** 2 * dt;
    if (this.diskFreeBytes() < 1024 ** 3) {
      this.missionState = 'ERROR';
      this.missionError = `stopped: only ${(this.diskFreeBytes() / 1024 ** 2).toFixed(0)} MB left`;
    }
  }

  // -- observation ------------------------------------------------------

  position() {
    const lat = this.cfg.originLat + this.north / METRES_PER_DEG_LAT;
    const lon = this.cfg.originLon + this.east / metresPerDegLon(this.cfg.originLat);
    return { lat, lon };
  }

  headingValid() {
    return !this.hasFault('heading_invalid');
  }

  headingSource() {
    return this.headingValid() ? this.cfg.headingSource || 'magnetometer' : 'none';
  }

  headingAccuracyDeg() {
    if (!this.headingValid()) return null;
    if (this.headingSource() === 'gnss_compass') return this.cfg.gnssCompassNoiseDeg;
    return this.cfg.magBiasDeg + this.cfg.magThrottleGainDeg * this.throttle;
  }

  headingDeg() {
    if (this.headingSource() === 'gnss_compass') {
      return wrap360(this.trueHeading + gauss(this.random, this.cfg.gnssCompassNoiseDeg));
    }
    // Not white noise: a slow walk on top of a throttle-proportional term, so
    // averaging it away does not work — which is the point of the GNSS compass.
    this.magWalk = 0.98 * this.magWalk + gauss(this.random, 0.15);
    const error =
      this.cfg.magBiasDeg +
      this.cfg.magThrottleGainDeg * this.throttle +
      this.magWalk +
      gauss(this.random, this.cfg.magNoiseDeg);
    return wrap360(this.trueHeading + error);
  }

  cogDeg() {
    const sog = this.sogMs();
    return sog > 0.05 ? bearingOf(this.velocity.e, this.velocity.n) : this.trueHeading;
  }

  sogMs() {
    return Math.hypot(this.velocity.e, this.velocity.n);
  }

  attitudeDeg() {
    const phase = (2 * Math.PI * this.t) / this.cfg.wavePeriodS;
    return {
      roll: this.cfg.waveHeightM * 12 * Math.sin(phase) + gauss(this.random, 0.3),
      pitch: this.cfg.waveHeightM * 4 * Math.sin(phase * 0.7 + 1.1) + gauss(this.random, 0.2),
    };
  }

  gnss() {
    return this.hasFault('gnss_degraded')
      ? { fixType: 2, numSats: 4, hdop: 4.5 }
      : { fixType: 3, numSats: 14, hdop: 0.8 };
  }

  diskFreeBytes() {
    return Math.max(0, this.cfg.diskTotalBytes - this.diskUsedBytes);
  }

  /** The tripod, wherever the faults have left it pointing. */
  #station() {
    return {
      eastM: this.cfg.stationEastM,
      northM: this.cfg.stationNorthM,
      heightM: this.cfg.stationHeightM,
      boresightDeg: this.cfg.stationBoresightDeg + this.alignmentErrorDeg,
      antenna: LB.SHORE_ANTENNA,
    };
  }

  /** Slant range to the shore station. */
  distanceFromStationM() {
    return LB.geometry(
      this.#station(), this.east, this.north, this.cfg.vesselAntennaHeightM,
    ).rangeM;
  }

  /**
   * The shore link.
   *
   * Every number below the bearer name comes from the link budget rather than
   * from a curve fitted to look plausible, because the panel is about to show
   * decibels and an operator is going to make decisions on them. The geometry
   * half — bearing from the sector's boresight — is reported even when there is
   * no link at all, which is exactly when it is worth having.
   */
  link() {
    const geom = LB.geometry(
      this.#station(), this.east, this.north, this.cfg.vesselAntennaHeightM,
    );
    const base = {
      distanceM: geom.rangeM,
      offBoresightDeg: geom.offBoresightDeg,
      sectorBeamwidthDeg: LB.SHORE_ANTENNA.azimuthBeamwidthDeg,
      vesselOffBoresightDeg: geom.vesselOffBoresightDeg,
      rssiDbm: null,
      rssiSource: null,
      expectedRssiDbm: null,
      headroomDb: null,
      mcsIndex: null,
      phyMbps: null,
      multipathDb: null,
      seaStateM: this.hasFault('link_glassy_water')
        ? LB.GLASSY_WAVE_HEIGHT_M
        : this.cfg.waveHeightM,
    };

    if (this.hasFault('link_loss')) {
      return { ...base, activeLink: 'none', quality: 0, rttMs: Infinity, capacityBytesPerS: 0 };
    }
    if (this.hasFault('link_degraded')) {
      return { ...base, activeLink: '4g', quality: 0.55, rttMs: 180, capacityBytesPerS: 90000 };
    }

    const est = LB.estimate(this.#station(), this.east, this.north, {
      vesselHeightM: this.cfg.vesselAntennaHeightM,
      waveHeightM: this.hasFault('link_glassy_water')
        ? LB.GLASSY_WAVE_HEIGHT_M
        : this.cfg.waveHeightM,
    });
    // Fading applies to the path, not to a signal: each modulation transmits
    // at its own power, so there is no single "the signal" until one has been
    // picked.
    const pathGain = est.pathGainDb + this.fade;
    const mcs = LB.selectMcs(pathGain);
    const headroom = LB.headroomDb(pathGain);

    if (!mcs) {
      // The cliff. One directional link and no cellular fallback assumed, so
      // below the bottom modulation there is nothing — which is the case this
      // interface most needs to handle and the old model could not produce.
      // Headroom still goes out: "how far past the cliff" is exactly what
      // somebody wants to know once the link has gone.
      return {
        ...base, activeLink: 'none', quality: 0, rttMs: Infinity,
        capacityBytesPerS: 0, headroomDb: headroom,
      };
    }

    const quality = LB.qualityFromHeadroom(headroom);
    return {
      ...base,
      activeLink: 'wifi',
      quality,
      rttMs: 4 * (1 + 2 * (1 - quality) ** 2),
      capacityBytesPerS: LB.usableBytesPerS(mcs),
      rssiDbm: mcs.txPowerDbm + pathGain,
      rssiSource: 'measured',
      expectedRssiDbm: est.expectedRssiDbm,
      headroomDb: headroom,
      mcsIndex: mcs.index,
      phyMbps: LB.phyMbps(mcs),
      multipathDb: est.multipathDb,
    };
  }

  /** How far is left to drive, for the power panel's endurance comparison. */
  surveyRemainingM() {
    const total =
      this.cfg.lineLengthM * this.cfg.numLines +
      Math.max(0, this.cfg.numLines - 1) * this.cfg.lineSpacingM;
    return Math.max(0, total - this.distanceTravelled);
  }

  onSurvey() {
    const wp = this.plan[Math.min(this.wpIndex, this.plan.length - 1)];
    return Boolean(wp.onSurvey) && !this.finished;
  }

  /** The survey lines and geofence, for the map. */
  planGeoJson() {
    const rad = (this.cfg.surveyHeadingDeg * Math.PI) / 180;
    const lines = [];
    for (let i = 0; i + 1 < this.plan.length; i += 2) {
      const a = this.plan[i];
      const b = this.plan[i + 1];
      lines.push({
        index: i / 2,
        coords: [this.#toLonLat(a), this.#toLonLat(b)],
      });
    }
    // A geofence around the survey with a margin. The real one comes from the
    // mission plan; this exists so the layer is exercised rather than shipped
    // untested — an overlay nobody has ever seen render is an overlay that does
    // not work.
    const margin = this.cfg.geofenceMarginM;
    const along = { e: Math.sin(rad), n: Math.cos(rad) };
    const across = { e: Math.sin(rad + Math.PI / 2), n: Math.cos(rad + Math.PI / 2) };
    const width = (this.cfg.numLines - 1) * this.cfg.lineSpacingM;
    const corner = (u, v) => this.#toLonLat({
      e: along.e * u + across.e * v,
      n: along.n * u + across.n * v,
    });
    const geofence = [
      corner(-margin, -margin),
      corner(this.cfg.lineLengthM + margin, -margin),
      corner(this.cfg.lineLengthM + margin, width + margin),
      corner(-margin, width + margin),
    ];

    return {
      lines,
      sonar_side: this.cfg.sonarSide,
      line_spacing_m: this.cfg.lineSpacingM,
      total_survey_distance_m: this.cfg.lineLengthM * this.cfg.numLines,
      geofence,
    };
  }

  #toLonLat(point) {
    const lat = this.cfg.originLat + point.n / METRES_PER_DEG_LAT;
    const lon = this.cfg.originLon + point.e / metresPerDegLon(this.cfg.originLat);
    return [Number(lon.toFixed(7)), Number(lat.toFixed(7))];
  }

  /**
   * A lidar scan against the static obstacles.
   *
   * Returns `[bearingDeg, rangeM]` pairs, as the wire format does — most beams
   * over open water return nothing, and sending 720 nulls to say so is the kind
   * of waste that sinks a 4G link.
   */
  lidarScan() {
    // A stalled lidar sweeps no beams at all — which is what makes it
    // distinguishable from a spinning one over open water.
    if (this.hasFault('lidar_stall')) {
      return { raw: [], filtered: [], rotationHz: 0, pointsPerRevolution: 0,
               beamsPerRevolution: 0, nearestRangeM: null, nearestBearingDeg: null };
    }

    const cfg = this.cfg;
    const beams = 360;               // 1 degree; enough to look right, cheap to draw
    const maxRange = 30;
    const raw = [];
    const { roll } = this.attitudeDeg();
    const clutterP = Math.max(0, (Math.abs(roll) - 4) / 4) * 0.06;

    for (let i = 0; i < beams; i += 1) {
      const bodyBearing = i;
      const worldBearing = wrap360(this.trueHeading + bodyBearing);
      let range = this.#raycast(worldBearing, maxRange);

      if (range !== null) range += gauss(this.random, 0.02);
      if (this.random() < clutterP) range = 0.5 + this.random() * 3.5;
      if (range !== null && (range < 0.2 || range > maxRange)) range = null;
      if (this.random() < 0.01) range = null;

      if (range !== null) raw.push([bodyBearing, Number(range.toFixed(2))]);
    }

    // Same crude filter as the Python: a real obstacle subtends several
    // consecutive beams, a lone return is wave clutter. Kept legible so the
    // operator can compare it against the raw set and see what was removed.
    const filtered = [];
    let run = [];
    const flush = () => {
      if (run.length >= 3) filtered.push(...run);
      run = [];
    };
    for (const point of raw) {
      if (run.length && (point[0] - run[run.length - 1][0] > 1.5 ||
          Math.abs(point[1] - run[run.length - 1][1]) > 0.8)) {
        flush();
      }
      run.push(point);
    }
    flush();

    const nearest = filtered.reduce(
      (best, p) => (best === null || p[1] < best[1] ? p : best),
      null,
    );

    return {
      raw,
      filtered,
      rotationHz: 10 + gauss(this.random, 0.05),
      pointsPerRevolution: raw.length,
      beamsPerRevolution: beams,
      nearestRangeM: nearest ? nearest[1] : null,
      nearestBearingDeg: nearest ? nearest[0] : null,
    };
  }

  #raycast(bearingDeg, maxRange) {
    const rad = (bearingDeg * Math.PI) / 180;
    const dx = Math.sin(rad);
    const dy = Math.cos(rad);
    let best = null;
    for (const obstacle of this.cfg.obstacles) {
      const fx = this.east - obstacle.east;
      const fy = this.north - obstacle.north;
      const b = 2 * (fx * dx + fy * dy);
      const c = fx * fx + fy * fy - obstacle.radius * obstacle.radius;
      const disc = b * b - 4 * c;
      if (disc < 0) continue;
      const root = Math.sqrt(disc);
      for (const t of [(-b - root) / 2, (-b + root) / 2]) {
        if (t > 0 && t < maxRange && (best === null || t < best)) best = t;
      }
    }
    return best;
  }
}
