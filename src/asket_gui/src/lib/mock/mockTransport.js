// A backend, in the browser.
//
// Presents the same surface as a `WebSocket` and speaks the same protocol, so
// `Connection` cannot tell the difference and none of the panels are forked.
// Everything the real backend decides — what is granted, at what rate, at what
// detail, and why — is decided here too, from the generated policy table.
//
// It is not a recording. The world is simulated live, faults are injectable
// while it runs, and commands go through the same pending/confirmed/failed
// lifecycle. What you are reviewing is the interface, driven by data that
// behaves the way the vessel behaves.

import {
  MODE_AUTONOMOUS,
  MODE_ESTOP,
  MODE_MANUAL,
  MockWorld,
} from './world.js';
import { PROFILES, PROFILE_ORDER, STREAMS, estimateBytesPerS, resolve } from './policy.js';
import * as payloads from './payloads.js';
import * as VF from './videoFrame.js';

const TICK_MS = 100;

// Mirrors CAMERA_SIZES in gui_backend/core/sim_source.py. 640x480 is the
// sensor's native resolution — both the real calibration and the Gazebo
// model say so — and there is no higher rung to offer.
const CAMERA_SIZES = { full: [640, 480], reduced: [320, 240], minimal: [160, 120] };
const PING_INTERVAL_MS = 2000;
const MODE_VALUES = { ESTOP: MODE_ESTOP, MANUAL: MODE_MANUAL, AUTONOMOUS: MODE_AUTONOMOUS };

const OPEN = 1;
const CLOSED = 3;

export class MockTransport {
  /** @param {MockWorld} world shared with the dev control panel */
  constructor(world, { timeScale = 1 } = {}) {
    this.world = world;
    this.timeScale = timeScale;
    this.readyState = OPEN;

    this.onopen = null;
    this.onmessage = null;
    this.onclose = null;
    this.onerror = null;

    this.subscriptions = new Map();
    this.profile = 'full';
    this.profileManual = false;
    this.profileReason = 'mock mode starts on the full profile';
    this.pending = [];
    this.activeAlarms = [];
    this.pingSeq = 0;
    this.lastPingAt = 0;

    // Open on the next turn of the event loop, so a caller that assigns
    // handlers after constructing us still receives `hello`.
    setTimeout(() => this.#open(), 0);
  }

  // -- WebSocket surface -------------------------------------------------

  send(text) {
    let message;
    try {
      message = JSON.parse(text);
    } catch {
      return;
    }
    this.#handle(message);
  }

  close() {
    this.readyState = CLOSED;
    clearInterval(this.timer);
    if (this.onclose) this.onclose();
  }

  #emit(message) {
    if (this.readyState !== OPEN || !this.onmessage) return;
    this.onmessage({ data: JSON.stringify(message) });
  }

  #open() {
    if (this.onopen) this.onopen();
    this.#emit({
      type: 'hello',
      protocol_version: 1,
      client_id: 'mock',
      server_utc_ms: this.world.utcMs,
      profile: this.#profileState(),
      profiles: PROFILE_ORDER,
      streams: STREAMS,
      source: { mode: 'mock', origin: {
        lat: this.world.cfg.originLat, lon: this.world.cfg.originLon,
      }, sonar_side: this.world.cfg.sonarSide },
      alarm_thresholds: {},
    });
    this.timer = setInterval(() => this.#tick(), TICK_MS);
  }

  // -- inbound -----------------------------------------------------------

  #handle(message) {
    switch (message.type) {
      case 'subscribe':
        this.#subscribe(message.streams || []);
        break;
      case 'unsubscribe':
        for (const name of message.streams || []) this.subscriptions.delete(name);
        this.#emitSubscribed([...this.subscriptions.values()].map((s) => s.resolution));
        break;
      case 'set_profile':
        this.setProfile(message.profile);
        break;
      case 'command':
        this.#command(message);
        break;
      case 'pong':
        break;
      default:
        this.#emit({
          type: 'error',
          server_utc_ms: this.world.utcMs,
          message: `unknown message type '${message.type}'`,
        });
    }
  }

  #subscribe(requests) {
    const results = [];
    for (const request of requests) {
      const resolution = resolve(
        request.name, request.rate_hz, request.detail, this.profile,
      );
      if (resolution.granted) {
        this.subscriptions.set(request.name, {
          requested: request,
          resolution,
          nextDueMs: 0,
          cursor: 0,
          lastHash: null,
        });
      } else {
        this.subscriptions.delete(request.name);
      }
      results.push(resolution);
    }
    this.#emitSubscribed(results);
  }

  #emitSubscribed(results) {
    this.#emit({
      type: 'subscribed',
      server_utc_ms: this.world.utcMs,
      profile: this.#profileState(),
      // The wire field is `name`; `resolve()` returns it as `stream`, and the
      // client keys its subscription map off `name`.
      streams: results.map(({ stream, ...rest }) => ({ name: stream, ...rest })),
      estimated_bytes_per_s: Math.round(
        estimateBytesPerS([...this.subscriptions.values()].map((s) => s.resolution)),
      ),
    });
  }

  #profileState() {
    return { profile: this.profile, manual: this.profileManual, reason: this.profileReason };
  }

  // -- profile -----------------------------------------------------------

  setProfile(profile) {
    if (profile === null || profile === undefined || profile === 'auto') {
      this.profileManual = false;
      this.profileReason = 'returned to automatic selection';
    } else if (PROFILE_ORDER.includes(profile)) {
      this.profile = profile;
      this.profileManual = true;
      this.profileReason = 'set manually by the operator';
    }
    this.#reresolve();
    this.#emit({ type: 'profile', server_utc_ms: this.world.utcMs, ...this.#profileState() });
  }

  #reresolve() {
    const results = [];
    for (const [name, sub] of this.subscriptions) {
      const resolution = resolve(
        name, sub.requested.rate_hz, sub.requested.detail, this.profile,
      );
      sub.resolution = resolution;
      // A resync: the detail level changes the decimation, so an increment
      // computed at the old step cannot be appended to history built at the new.
      sub.cursor = 0;
      sub.nextDueMs = 0;
      sub.lastHash = null;
      results.push(resolution);
    }
    this.#emitSubscribed(results);
  }

  /** Automatic selection, from what the simulated link is doing. */
  #autoSelect() {
    if (this.profileManual) return;
    const link = this.world.link();
    let target = 'full';
    let reason = `round trip ${Math.round(link.rttMs)} ms, quality ${link.quality.toFixed(2)}`;
    if (link.quality <= 0) {
      target = 'minimal';
      reason = 'no link';
    } else if (link.rttMs > 600 || link.quality < 0.1) {
      target = 'minimal';
    } else if (link.rttMs > 150 || link.quality < 0.35) {
      target = 'reduced';
    }
    if (target === this.profile) return;
    this.profile = target;
    this.profileReason = reason;
    this.#reresolve();
    this.#emit({ type: 'profile', server_utc_ms: this.world.utcMs, ...this.#profileState() });
  }

  // -- commands ----------------------------------------------------------

  #command(message) {
    const { id, name, args = {} } = message;
    const now = this.world.utcMs;
    const result = (status, detail) =>
      this.#emit({
        type: 'command_result', server_utc_ms: this.world.utcMs,
        id, name, args, status, detail,
      });

    if (name === 'set_mode' || name === 'cut_propulsion') {
      const modeName = name === 'cut_propulsion' ? 'ESTOP' : String(args.mode || '').toUpperCase();
      const mode = MODE_VALUES[modeName];
      if (mode === undefined) {
        result('failed', `unknown mode '${args.mode}'`);
        return;
      }
      this.world.requestMode(mode);
      result('pending', 'sent to the Pico');
      // Confirmation comes from watching the vessel's own state, never from
      // the request having been accepted (safety rule 4).
      this.pending.push({
        id, name, args, issuedMs: now, timeoutMs: 3000,
        confirm: () => (name === 'cut_propulsion'
          ? this.world.mode === MODE_ESTOP && this.world.estopLatched
          : this.world.mode === mode),
      });
      return;
    }

    if (name === 'set_ping_parameters') {
      this.world.cfg.sonarRangeM = Number(args.range_m ?? this.world.cfg.sonarRangeM);
      this.world.cfg.sonarGain = Number(args.gain ?? this.world.cfg.sonarGain);
      this.world.cfg.sonarPingRateHz = Math.max(0.1, Number(args.ping_rate_hz ?? 5));
      result('pending', 'sent to the sonar');
      this.pending.push({
        id, name, args, issuedMs: now, timeoutMs: 3000,
        confirm: () => this.world.cfg.sonarRangeM === Number(args.range_m),
      });
      return;
    }

    if (name === 'start_mission') {
      if (this.world.diskFreeBytes() < 1024 ** 3) {
        result('failed', 'not enough disk to start a mission');
        return;
      }
      this.world.missionState = 'RECORDING';
      this.world.missionName = String(args.name || 'mission').replace(/[^\w.-]+/g, '_');
      this.world.missionStartUtc = now;
      this.world.missionBytes = 0;
      this.world.missionError = '';
      result('confirmed', 'recording');
      return;
    }

    if (name === 'stop_mission') {
      if (this.world.missionState !== 'RECORDING') {
        result('failed', 'no mission is recording');
        return;
      }
      this.world.missions.unshift({
        name: this.world.missionName,
        path: `/data/missions/${this.world.missionName}`,
        started_utc_ms: this.world.missionStartUtc,
        stopped_utc_ms: now,
        complete: true,
        size_bytes: Math.round(this.world.missionBytes),
        duration_s: (now - this.world.missionStartUtc) / 1000,
      });
      this.world.missionState = 'IDLE';
      this.world.missionName = '';
      result('confirmed', 'stopped');
      return;
    }

    if (name === 'run_system_test') {
      this.lastReport = this.#preflight();
      result('confirmed', this.lastReport.summary);
      return;
    }

    if (name === 'inject_fault') {
      this.world.setFault(String(args.fault), true);
      result('confirmed', `injected ${args.fault}`);
      return;
    }
    if (name === 'clear_fault') {
      if (['*', 'all', ''].includes(String(args.fault ?? ''))) this.world.faults.clear();
      else this.world.setFault(String(args.fault), false);
      result('confirmed', 'cleared');
      return;
    }

    result('failed', `command '${name}' is not available in mock mode`);
  }

  #updateCommands() {
    const now = this.world.utcMs;
    this.pending = this.pending.filter((command) => {
      if (command.confirm()) {
        this.#emit({
          type: 'command_result', server_utc_ms: now,
          id: command.id, name: command.name, args: command.args,
          status: 'confirmed', detail: 'confirmed by the vessel',
        });
        return false;
      }
      if (now - command.issuedMs > command.timeoutMs) {
        this.#emit({
          type: 'command_result', server_utc_ms: now,
          id: command.id, name: command.name, args: command.args,
          status: 'failed',
          detail: 'no confirmation from the vessel after 3 s — the vessel has NOT changed state',
        });
        return false;
      }
      return true;
    });
  }

  // -- alarms ------------------------------------------------------------

  #alarms() {
    const world = this.world;
    const out = [];
    const add = (key, severity, message, remedy) => out.push({ key, severity, message, remedy });

    const soc = world.remainingWh / world.cfg.batteryCapacityWh;
    if (soc <= 0.12) add('battery_critical', 'alarm', `Battery at ${(soc * 100).toFixed(0)}%`,
      'Return to shore now.');
    else if (soc <= 0.25) add('battery_low', 'warn', `Battery at ${(soc * 100).toFixed(0)}%`,
      'Check the remaining survey distance against the endurance estimate.');

    if (world.missionState === 'RECORDING' && world.diskFreeBytes() < 5 * 1024 ** 3) {
      add('disk_low', 'alarm',
        `${(world.diskFreeBytes() / 1024 ** 3).toFixed(1)} GB of disk left while recording`,
        'Stop the mission and export, or the recording will truncate.');
    }
    if (world.missionState === 'ERROR') {
      add('recording_stopped', 'alarm', `Recording stopped: ${world.missionError}`,
        'The survey is no longer being logged. Free space, then start a new mission.');
    }
    if (world.hasFault('sonar_dropout')) {
      add('sonar_unresponsive', 'alarm', 'Sonar is not sending data',
        'Check the sonar Ethernet cable and power. The survey is not being recorded.');
    }
    const offset = Math.round(world.clockOffsetMs);
    if (Math.abs(offset) >= 1000) {
      add('clock_drift', 'alarm', `Sonar clock is ${offset} ms from the Jetson's`,
        'Data recorded now cannot be georeferenced afterwards. Check the NTP server.');
    } else if (Math.abs(offset) >= 250) {
      add('clock_drift', 'warn', `Sonar clock drifting (${offset} ms)`,
        'Unusable past 1000 ms. Check NTP before it gets there.');
    }
    if (!world.headingValid()) {
      add('heading_invalid', 'alarm', 'Heading is invalid',
        'Sonar data recorded during this window is compromised. Note the time and re-run.');
    }
    if (!world.rcLinkOk) {
      add('rc_link_lost', 'alarm', 'RC link lost',
        'The hardware killswitch is out of range. Command authority requires an operator '
        + 'within RC range.');
    }
    if (world.link().activeLink === 'none') {
      add('link_lost', 'alarm', 'No data from the vessel',
        'Everything on screen is stale. Move towards the vessel or switch to cellular.');
    }
    return out;
  }

  #updateAlarms() {
    const current = this.#alarms();
    const previous = this.activeAlarms;
    const key = (list) => JSON.stringify(list.map((a) => [a.key, a.message]));
    if (key(previous) === key(current)) return;

    const previousKeys = new Set(previous.map((a) => a.key));
    const currentKeys = new Set(current.map((a) => a.key));
    this.activeAlarms = current;
    this.#emit({
      type: 'alarms',
      server_utc_ms: this.world.utcMs,
      raised: current.filter((a) => !previousKeys.has(a.key) ||
        JSON.stringify(previous.find((p) => p.key === a.key)) !== JSON.stringify(a)),
      cleared: [...previousKeys].filter((k) => !currentKeys.has(k)),
      active: current,
    });
  }

  // -- pre-flight --------------------------------------------------------

  #preflight() {
    const world = this.world;
    const gnss = world.gnss();
    const soc = world.remainingWh / world.cfg.batteryCapacityWh;
    const offset = Math.round(world.clockOffsetMs);
    const items = [];
    const add = (id, name, status, message, remedy = '') =>
      items.push({ id, name, status, message, remedy, measured_value: null, units: '',
                   active: false });

    add('pico.link', 'Pico link', 'PASS', 'Heartbeat 0.1 s ago');
    add('rc.link', 'RC link and killswitch',
      world.rcLinkOk ? 'PASS' : 'FAIL',
      world.rcLinkOk ? `Linked, channel 8 reads ${world.rcChannel8}%`
                     : 'No signal from the transmitter',
      world.rcLinkOk ? '' : 'Turn the transmitter on and check its aerial.');
    add('gnss.fix', 'GNSS fix',
      gnss.numSats >= 6 && gnss.fixType >= 3 ? 'PASS' : 'FAIL',
      gnss.numSats >= 6 ? `3D fix, ${gnss.numSats} satellites, HDOP ${gnss.hdop}`
                        : `${gnss.numSats} satellites, 6 required`,
      gnss.numSats >= 6 ? '' : 'Wait, or move the vessel clear of buildings and vehicles.');
    add('heading.valid', 'Heading',
      world.headingValid() ? 'PASS' : 'FAIL',
      world.headingValid() ? `${world.headingSource()} heading` : 'Heading is invalid',
      world.headingValid() ? '' : 'Sonar data recorded without a heading cannot be '
        + 'georeferenced. Check the compass calibration.');
    add('sonar.link', 'Sonar',
      world.hasFault('sonar_dropout') ? 'FAIL' : 'PASS',
      world.hasFault('sonar_dropout') ? 'The sonar is not sending data'
        : `${world.cfg.sonarPingRateHz} Hz, ${world.cfg.sonarPointsPerPing} points per ping`,
      world.hasFault('sonar_dropout') ? 'Check the sonar Ethernet cable and power.' : '');
    add('sonar.clock', 'Sonar clock (NTP)',
      Math.abs(offset) > 250 ? 'FAIL' : 'PASS',
      Math.abs(offset) > 250 ? `The sonar's clock is ${offset} ms from the Jetson's`
                             : `Within ${Math.abs(offset)} ms of the Jetson`,
      Math.abs(offset) > 250 ? 'Everything recorded will be un-georeferenceable. Check NTP.' : '');
    // Amber, every single time, until somebody measures the boat. This is the
    // real state of the repository's mounting.yaml, not a simulated fault:
    // there is nothing to toggle here because nothing in software can fix it.
    add('sonar.mounting', 'Sonar mounting geometry', 'WARN',
      'Mounting geometry is PROVISIONAL — mounting.yaml says nobody measured it',
      'Measure the tilt and the lever arm from the GNSS antenna to the transducer, to '
        + 'the centimetre, then set measured: true. Until then every sounding carries '
        + 'the same unknown offset.');
    // Hours of *recording*, not hours of survey: on a 500 GB drive the disk
    // figure is over a hundred and the battery is three. Naming which one binds
    // is the point (docs/open_questions.md Q5).
    const diskFree = world.diskFreeBytes();
    const diskHours = diskFree / (3.5 * 1024 ** 3);
    const enduranceH = world.avgPowerW > 1
      ? world.remainingWh / world.avgPowerW : null;
    add('disk.space', 'Disk space',
      diskFree > 20 * 1024 ** 3 ? 'PASS' : 'FAIL',
      `${(diskFree / 1024 ** 3).toFixed(0)} GB free `
        + (enduranceH !== null && enduranceH < diskHours
          ? `— ${diskHours.toFixed(0)} hours of recording, but the battery gives `
            + `${enduranceH.toFixed(1)}`
          : `— about ${diskHours.toFixed(0)} hours of recording`),
      diskFree > 20 * 1024 ** 3 ? ''
        : 'Export and delete an old mission before launching.');
    add('battery.charge', 'Battery',
      soc < 0.4 ? 'WARN' : 'PASS', `${(soc * 100).toFixed(0)}% charge`,
      soc < 0.4 ? 'Enough to launch, probably not enough to finish a survey.' : '');
    add('disk.speed', 'Disk write speed', 'SKIPPED', 'No data: not measured in mock mode',
      'Run the real backend on the Jetson to measure it.');

    const failures = items.filter((i) => i.status === 'FAIL');
    const warnings = items.filter((i) => i.status === 'WARN');
    const summary = failures.length
      ? `NO-GO — ${failures[0].message.replace(/\.$/, '')}`
        + (failures.length > 1 ? ` (and ${failures.length - 1} more)` : '')
      : warnings.length
        ? `GO with ${warnings.length} warning(s) — ${warnings[0].message.replace(/\.$/, '')}`
        : 'GO — everything checks out';

    return { go: failures.length === 0, summary, items,
             run_utc_ms: world.utcMs, duration_s: 0.4, history: { degrading: [] } };
  }

  // -- the loop ----------------------------------------------------------

  #tick() {
    // Physics runs at the time scale; the clock does not. See MockWorld.utcMs.
    this.world.step((TICK_MS / 1000) * this.timeScale);
    this.#autoSelect();
    this.#updateCommands();
    this.#updateAlarms();
    this.#push();

    const now = Date.now();
    if (now - this.lastPingAt > PING_INTERVAL_MS) {
      this.lastPingAt = now;
      this.pingSeq += 1;
      this.#emit({ type: 'ping', id: this.pingSeq, server_utc_ms: this.world.utcMs });
    }
  }

  #push() {
    const nowMs = this.world.utcMs;
    for (const [name, sub] of this.subscriptions) {
      if (!sub.resolution.granted) continue;
      if (name === 'camera') {
        this.#pushCamera(sub, nowMs);
        continue;
      }
      const spec = STREAMS[name];
      let payload;

      if (spec.on_change_only) {
        payload = this.#payload(name, sub);
        const hash = JSON.stringify(payload);
        if (hash === sub.lastHash) continue;
        sub.lastHash = hash;
      } else {
        if (sub.resolution.rate_hz <= 0 || nowMs < sub.nextDueMs) continue;
        sub.nextDueMs = nowMs + 1000 / sub.resolution.rate_hz;
        payload = this.#payload(name, sub);
      }
      if (payload === null) continue;

      this.#emit({
        type: 'data',
        stream: name,
        // When it was produced, and when it was sent. Kept apart, exactly as
        // the real backend does, because honest data age depends on it.
        source_utc_ms: nowMs,
        server_utc_ms: nowMs,
        detail: sub.resolution.detail,
        payload,
      });
    }
  }

  /**
   * Pixels, or a reason there are none — and unlike every other stream, one
   * or the other on EVERY due tick.
   *
   * Saying nothing is the right answer elsewhere; a stream with nothing to
   * report should let the panel age it out. It is the wrong answer here,
   * because the panel has to tell a vessel that has stopped sending from a
   * link that has stopped carrying, and from the shore those are identical.
   * The status frame is the difference, and it is eighty bytes.
   */
  #pushCamera(sub, nowMs) {
    if (sub.resolution.rate_hz <= 0 || nowMs < sub.nextDueMs) return;
    sub.nextDueMs = nowMs + 1000 / sub.resolution.rate_hz;

    const camera = this.world.camera;
    const state = this.world.cameraState();
    const size = CAMERA_SIZES[sub.resolution.detail] ?? CAMERA_SIZES.full;
    const frame = state === 'live' ? camera.frame(size[0], size[1]) : null;

    if (frame) {
      this.#emitBinary(VF.encode(
        VF.frameHeader({
          seq: frame.seq,
          sourceUtcMs: frame.utcMs,
          serverUtcMs: nowMs,
          width: frame.width,
          height: frame.height,
          format: frame.format,
          sizeBytes: frame.bytes.length,
          detail: sub.resolution.detail,
          rateHz: sub.resolution.rate_hz,
        }),
        frame.bytes,
      ));
      return;
    }

    let reason = VF.REASON_NOT_STARTED;
    if (state === 'dead') reason = VF.REASON_DEAD;
    else if (state === 'frozen') reason = VF.REASON_FROZEN;
    else if (camera.framesProduced > 0) {
      // Live, the shutter is running, and the encoder has not caught up with
      // this exposure yet. That is not a fault and must not be reported as
      // one: a single missed tick is what the panel's "late" rung is for, and
      // claiming "no frames yet" while fourteen have been taken would be a
      // contradiction on the face of it.
      return;
    }

    this.#emitBinary(VF.encode(VF.statusHeader({
      reason,
      serverUtcMs: nowMs,
      rateHz: sub.resolution.rate_hz,
      lastFrameUtcMs: camera.lastFrameUtcMs,
      framesProduced: camera.framesProduced,
    })));
  }

  #emitBinary(buffer) {
    if (!this.onmessage || this.readyState !== OPEN) return;
    this.onmessage({ data: buffer });
  }

  #payload(name, sub) {
    const detail = sub.resolution.detail;
    const world = this.world;
    const step = { full: 1, reduced: 3, minimal: 10 }[detail];

    switch (name) {
      case 'vessel': return payloads.vesselPayload(world, detail);
      case 'heading': return payloads.headingPayload(world, detail);
      case 'pico': return payloads.picoPayload(world, detail);
      case 'power': return payloads.powerPayload(world, detail);
      case 'sonar': return payloads.sonarPayload(world, detail);
      case 'lidar': return payloads.lidarPayload(world, detail);
      case 'mission': return payloads.missionPayload(world);
      case 'plan': return world.planGeoJson();
      case 'diagnostics': return this.lastReport ?? null;
      case 'link':
        return payloads.linkPayload(world, detail, {
          profile: this.profile,
          manual: this.profileManual,
          rateBytesPerS: estimateBytesPerS(
            [...this.subscriptions.values()].map((s) => s.resolution),
          ),
        });
      case 'track': {
        const payload = payloads.trackPayload(world, sub.cursor, step);
        sub.cursor = world.track.length;
        return payload;
      }
      case 'coverage': {
        const payload = payloads.coveragePayload(world, sub.cursor, step);
        sub.cursor = world.coverage.length;
        return payload;
      }
      default: return null;
    }
  }
}
