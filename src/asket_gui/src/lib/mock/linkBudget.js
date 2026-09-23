/**
 * A mirror of `asket_common/link_budget.py`, not an independent implementation.
 *
 * Mock mode is how this interface gets reviewed, and the link panel is about to
 * show decibels, bearing from the sector's boresight and headroom above the
 * cliff. A mock that invented plausible-looking numbers for those would be
 * worse than one with no numbers at all: somebody would tune the panel's
 * thresholds against them, and the thresholds would be wrong on the boat.
 *
 * So this is a transcription. If you change one side, change the other:
 * `test_mock_link_budget_matches.py` runs this file under Node and compares it
 * with the Python at a spread of ranges and bearings, and it will fail rather
 * than let the two drift. That is the same arrangement that holds
 * `mode_arbitration.py` against the firmware, for the same reason.
 *
 * The whole budget is here, two-ray propagation included. An earlier draft left
 * the sea out on the grounds that the mock has no weather, and the comparison
 * test then had to pin a divergence and bound it — which is the shape of a
 * thing that later turns into a real difference nobody notices. Porting the
 * remaining eight lines was cheaper than explaining the exception, and it means
 * the multipath nulls are reviewable in the browser too.
 */

export const C_M_S = 299792458.0;
export const DEFAULT_FREQ_MHZ = 5500.0;
export const DEFAULT_WAVE_HEIGHT_M = 0.5;
/** Mirrors GLASSY_WAVE_HEIGHT_M in asket_sim/core/link.py. */
export const GLASSY_WAVE_HEIGHT_M = 0.02;

/** 802.11ax, 80 MHz, one spatial stream. Mirrors MCS_TABLE. */
export const MCS_TABLE = [
  { index: 0, minRssiDbm: -82.0, phyMbps: 36.0 },
  { index: 1, minRssiDbm: -79.0, phyMbps: 72.1 },
  { index: 2, minRssiDbm: -77.0, phyMbps: 108.1 },
  { index: 3, minRssiDbm: -74.0, phyMbps: 144.1 },
  { index: 4, minRssiDbm: -70.0, phyMbps: 216.2 },
  { index: 5, minRssiDbm: -66.0, phyMbps: 288.2 },
  { index: 6, minRssiDbm: -65.0, phyMbps: 324.3 },
  { index: 7, minRssiDbm: -64.0, phyMbps: 360.3 },
  { index: 8, minRssiDbm: -59.0, phyMbps: 432.4 },
  { index: 9, minRssiDbm: -57.0, phyMbps: 480.4 },
  { index: 10, minRssiDbm: -54.0, phyMbps: 540.4 },
  { index: 11, minRssiDbm: -52.0, phyMbps: 600.5 },
];

export const USABLE_FRACTION_OF_PHY = 0.45;
export const NOISE_FLOOR_DBM = MCS_TABLE[0].minRssiDbm;
export const FULL_QUALITY_HEADROOM_DB = 30.0;

export const SHORE_ANTENNA = {
  gainDbi: 15.0,
  azimuthBeamwidthDeg: 120.0,
  elevationBeamwidthDeg: 10.0,
  frontToBackDb: 25.0,
};

export const VESSEL_ANTENNA_OMNI = {
  gainDbi: 6.7,
  azimuthBeamwidthDeg: 360.0,
  elevationBeamwidthDeg: 360.0,
  frontToBackDb: 25.0,
};

export const wrap180 = (deg) => ((deg + 180) % 360 + 360) % 360 - 180;

function lobeLossDb(antenna, offAxisDeg, beamwidthDeg) {
  if (beamwidthDeg <= 0 || beamwidthDeg >= 360) return 0;
  const ratio = Math.abs(offAxisDeg) / (beamwidthDeg / 2);
  return Math.min(3.0 * ratio * ratio, antenna.frontToBackDb);
}

/** Gain in dBi at an off-boresight angle. Mirrors Antenna.gain_at. */
export function gainAt(antenna, offAzimuthDeg, offElevationDeg = 0) {
  let loss = lobeLossDb(antenna, offAzimuthDeg, antenna.azimuthBeamwidthDeg);
  loss += lobeLossDb(antenna, offElevationDeg, antenna.elevationBeamwidthDeg);
  return antenna.gainDbi - Math.min(loss, antenna.frontToBackDb);
}

export const wavelengthM = (freqMhz = DEFAULT_FREQ_MHZ) => C_M_S / (freqMhz * 1e6);

/** How much of the sea-surface reflection survives, 0..1. Mirrors Ament. */
export function specularCoefficient(
  rangeM, hStationM, hVesselM, waveHeightM, freqMhz = DEFAULT_FREQ_MHZ,
) {
  if (waveHeightM <= 0) return 1.0;
  const grazing = (hStationM + hVesselM) / Math.max(rangeM, 1.0);
  const roughness = (2 * Math.PI * waveHeightM * grazing) / wavelengthM(freqMhz);
  return Math.exp(-2 * roughness * roughness);
}

/**
 * Direct ray against sea-surface reflection, dB relative to free space.
 * Mirrors two_ray_db. Nulls on calm water at 2 h1 h2 / (n lambda); past the
 * last one, range costs double, which is where this link actually ends.
 */
export function twoRayDb(
  rangeM, hStationM, hVesselM, waveHeightM, freqMhz = DEFAULT_FREQ_MHZ,
) {
  const lam = wavelengthM(freqMhz);
  const pathDifference = (2 * hStationM * hVesselM) / Math.max(rangeM, 1.0);
  const phase = (2 * Math.PI * pathDifference) / lam;
  const rho = specularCoefficient(rangeM, hStationM, hVesselM, waveHeightM, freqMhz);
  const real = 1 - rho * Math.cos(phase);
  const imag = rho * Math.sin(phase);
  return 20 * Math.log10(Math.max(Math.hypot(real, imag), 1e-3));
}

export function fsplDb(rangeM, freqMhz = DEFAULT_FREQ_MHZ) {
  const d = Math.max(rangeM, 1.0);
  return 20 * Math.log10(d) + 20 * Math.log10(freqMhz * 1e6) - 147.55;
}

export function selectMcs(rssiDbm) {
  let best = null;
  for (const mcs of MCS_TABLE) {
    if (rssiDbm >= mcs.minRssiDbm) best = mcs;
  }
  return best;
}

export const headroomDb = (rssiDbm) => rssiDbm - NOISE_FLOOR_DBM;

export const qualityFromRssi = (rssiDbm) =>
  Math.max(0, Math.min(1, headroomDb(rssiDbm) / FULL_QUALITY_HEADROOM_DB));

export const usableBytesPerS = (mcs) =>
  (mcs ? (mcs.phyMbps * 1e6 * USABLE_FRACTION_OF_PHY) / 8 : 0);

/**
 * Where the boat is, as the radio sees it. Mirrors link_budget.geometry.
 *
 * `station` is `{eastM, northM, heightM, boresightDeg}`; `vesselHeightM` is the
 * boat's antenna height. Heading is absent on purpose: the boat's antenna is
 * modelled as an omni until somebody confirms the part, so heading contributes
 * nothing and must not appear to.
 */
export function geometry(station, eastM, northM, vesselHeightM = 1.0) {
  const de = eastM - station.eastM;
  const dn = northM - station.northM;
  const groundRange = Math.hypot(de, dn);
  // wrap360 of atan2(east, north), matching geo.enu_bearing_deg exactly: the
  // arguments are the other way round from the usual atan2(y, x) because a
  // compass bearing runs clockwise from north, not anticlockwise from east.
  const bearing = (((Math.atan2(de, dn) * 180) / Math.PI) % 360 + 360) % 360;
  const heightDifference = station.heightM - vesselHeightM;
  return {
    rangeM: Math.hypot(groundRange, heightDifference),
    bearingDeg: bearing,
    offBoresightDeg: wrap180(bearing - station.boresightDeg),
    depressionDeg:
      (Math.atan2(heightDifference, Math.max(groundRange, 1e-6)) * 180) / Math.PI,
    vesselOffBoresightDeg: null,
  };
}

/** The whole budget. Mirrors link_budget.estimate, minus the weather. */
export function estimate(station, eastM, northM, options = {}) {
  const {
    vesselHeightM = 1.0,
    vesselAntenna = VESSEL_ANTENNA_OMNI,
    txPowerDbm = 25.0,
    freqMhz = DEFAULT_FREQ_MHZ,
    waveHeightM = DEFAULT_WAVE_HEIGHT_M,
  } = options;

  const geom = geometry(station, eastM, northM, vesselHeightM);
  const pathLoss = fsplDb(geom.rangeM, freqMhz);
  const stationGain = gainAt(
    station.antenna || SHORE_ANTENNA,
    geom.offBoresightDeg,
    geom.depressionDeg,
  );
  const vesselGain = vesselAntenna.gainDbi;
  const peak = txPowerDbm + (station.antenna || SHORE_ANTENNA).gainDbi + vesselGain;

  const multipathDb = twoRayDb(
    geom.rangeM, station.heightM, vesselHeightM, waveHeightM, freqMhz,
  );

  const rssiDbm = txPowerDbm + stationGain + vesselGain - pathLoss + multipathDb;
  return {
    rssiDbm,
    expectedRssiDbm: peak - pathLoss,
    headroomDb: headroomDb(rssiDbm),
    quality: qualityFromRssi(rssiDbm),
    mcs: selectMcs(rssiDbm),
    multipathDb,
    patternLossDb:
      (station.antenna || SHORE_ANTENNA).gainDbi - stationGain,
    geometry: geom,
  };
}
