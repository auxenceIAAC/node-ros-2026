"""The browser's link budget must agree with the vessel's, number for number.

``asket_gui/src/lib/mock/linkBudget.js`` is a transcription of
``asket_common/link_budget.py``, not a second implementation — and a
transcription nobody checks is how two firmwares that nothing could tell apart
cost this project months. The same arrangement that holds
``mode_arbitration.py`` against the real sketch holds these two together:
the JavaScript is run under Node at a spread of ranges and bearings and the
answers are compared.

It matters more here than it looks. The link panel is about to show decibels,
headroom above the cliff, and bearing from the sector's boresight. Mock mode is
where this interface gets reviewed, so if the mock's numbers were merely
plausible, somebody would tune a threshold against them and the threshold would
be wrong on the boat. "Looks about right in the browser" is exactly the class
of agreement that has already failed twice.

There is no stated divergence between the two, deliberately. An earlier draft
left the two-ray term out of the JavaScript and had this file pin and bound the
difference; porting the remaining eight lines was cheaper than maintaining the
exception, and an exception in a drift test is the thing that later turns into
a drift nobody notices.
"""

import json
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest
from asket_common.link_budget import (
    MCS_TABLE,
    NOISE_FLOOR_DBM,
    USABLE_FRACTION_OF_PHY,
    Propagation,
    RadioConfig,
    ShoreStation,
    VesselRadio,
    headroom_db,
    quality_from_rssi,
    select_mcs,
)

GUI = Path(__file__).resolve().parents[2] / "asket_gui"
MOCK = GUI / "src" / "lib" / "mock"

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None or not MOCK.is_dir(),
    reason="node or the frontend mock is not present",
)

#: Station on the origin facing north, so a reader can check any row by hand.
STATION_EAST_M = 0.0
STATION_NORTH_M = 0.0
STATION_HEIGHT_M = 2.9
STATION_BORESIGHT_DEG = 0.0
VESSEL_HEIGHT_M = 1.0
#: Glassy, so the comparison runs through the multipath nulls rather than
#: past them — the deepest disagreement a transcription error could cause is
#: exactly there, where the signal swings 30 dB over a few tens of metres.
WAVE_HEIGHT_M = 0.02

#: How closely the two sides must agree, in decibels. The panel rounds to one
#: decimal place, so anything looser would let a visible difference through.
TOLERANCE_DB = 0.05

#: Ranges and bearings chosen to cross things rather than to sample evenly:
#: inside the sector and outside it, near the cliff and well past it, and a
#: couple of points behind the tripod.
POINTS = [
    (0.0, 100.0), (0.0, 400.0), (0.0, 900.0), (0.0, 1500.0),
    (0.0, 2000.0), (0.0, 2400.0), (0.0, 2600.0), (0.0, 4000.0),
    (0.0, 53.2), (0.0, 106.4), (0.0, 133.0), (0.0, 180.0),
    (300.0, 900.0), (780.0, 450.0), (900.0, 0.0), (1200.0, -300.0),
    (-300.0, 900.0), (-780.0, 450.0), (0.0, -800.0), (0.0, -2000.0),
]


def javascript_estimates() -> list[dict]:
    script = textwrap.dedent(
        f"""
        const LB = await import('{MOCK}/linkBudget.js');
        const station = {{
          eastM: {STATION_EAST_M}, northM: {STATION_NORTH_M},
          heightM: {STATION_HEIGHT_M}, boresightDeg: {STATION_BORESIGHT_DEG},
          antenna: LB.SHORE_ANTENNA,
        }};
        const points = {json.dumps(POINTS)};
        const out = points.map(([e, n]) => {{
          const est = LB.estimate(station, e, n, {{
            vesselHeightM: {VESSEL_HEIGHT_M}, waveHeightM: {WAVE_HEIGHT_M},
          }});
          return {{
            east: e, north: n,
            range_m: est.geometry.rangeM,
            bearing_deg: est.geometry.bearingDeg,
            off_boresight_deg: est.geometry.offBoresightDeg,
            depression_deg: est.geometry.depressionDeg,
            rssi_dbm: est.rssiDbm,
            expected_rssi_dbm: est.expectedRssiDbm,
            headroom_db: est.headroomDb,
            quality: est.quality,
            mcs_index: est.mcs === null ? null : est.mcs.index,
            multipath_db: est.multipathDb,
            usable_bytes_per_s: LB.usableBytesPerS(est.mcs),
          }};
        }});
        console.log(JSON.stringify(out));
        """
    )
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        capture_output=True, text=True, timeout=120,
    )
    if result.returncode != 0:
        pytest.fail(f"node failed:\n{result.stderr}")
    return json.loads(result.stdout)


def python_radio() -> RadioConfig:
    return RadioConfig(
        station=ShoreStation(
            east_m=STATION_EAST_M, north_m=STATION_NORTH_M,
            height_m=STATION_HEIGHT_M, boresight_deg=STATION_BORESIGHT_DEG,
        ),
        vessel=VesselRadio(height_m=VESSEL_HEIGHT_M),
        propagation=Propagation(wave_height_m=WAVE_HEIGHT_M),
    )


@pytest.fixture(scope="module")
def rows() -> list[dict]:
    return javascript_estimates()


def test_there_is_something_to_compare(rows):
    """Guard the guard: an empty list would make every check below vacuous."""
    assert len(rows) == len(POINTS)


def test_the_geometry_agrees(rows):
    radio = python_radio()
    for row in rows:
        estimate = radio.estimate_at(row["east"], row["north"])
        geom = estimate.geometry
        where = f"({row['east']:.0f}, {row['north']:.0f})"
        assert geom.range_m == pytest.approx(row["range_m"], abs=1e-6), where
        assert geom.bearing_deg == pytest.approx(row["bearing_deg"], abs=1e-9), where
        assert geom.off_boresight_deg == pytest.approx(
            row["off_boresight_deg"], abs=1e-9
        ), where
        assert geom.depression_deg == pytest.approx(row["depression_deg"], abs=1e-9), where


def test_the_signal_agrees(rows):
    """A tenth of a decibel. The panel rounds to one, so anything looser would
    let a visible difference through."""
    radio = python_radio()
    for row in rows:
        estimate = radio.estimate_at(row["east"], row["north"])
        where = f"({row['east']:.0f}, {row['north']:.0f})"
        assert estimate.multipath_db == pytest.approx(
            row["multipath_db"], abs=TOLERANCE_DB
        ), where
        assert estimate.rssi_dbm == pytest.approx(row["rssi_dbm"], abs=TOLERANCE_DB), where
        assert estimate.expected_rssi_dbm == pytest.approx(
            row["expected_rssi_dbm"], abs=TOLERANCE_DB
        ), where
        assert estimate.headroom_db == pytest.approx(row["headroom_db"], abs=TOLERANCE_DB), where
        assert estimate.quality == pytest.approx(row["quality"], abs=0.005), where


def test_the_same_points_pick_the_same_modulation(rows):
    """Including picking none. An off-by-one in the table would show up here as
    a browser that keeps a link the vessel has already lost."""
    radio = python_radio()
    for row in rows:
        estimate = radio.estimate_at(row["east"], row["north"])
        expected = estimate.mcs.index if estimate.mcs else None
        where = f"({row['east']:.0f}, {row['north']:.0f})"
        assert expected == row["mcs_index"], where
        assert estimate.usable_bytes_per_s == pytest.approx(
            row["usable_bytes_per_s"], rel=1e-9
        ), where


def test_the_points_actually_cross_the_cliff(rows):
    """Otherwise the agreement above is only about the easy half. There must be
    points on both sides, or this file is testing that two implementations
    agree about a link that always works."""
    alive = [r for r in rows if r["mcs_index"] is not None]
    dead = [r for r in rows if r["mcs_index"] is None]
    assert len(alive) >= 4, "no working points"
    assert len(dead) >= 2, "no points past the cliff — the comparison is half a test"


def test_the_points_cover_more_than_one_modulation(rows):
    """Same argument for the staircase: if every live point landed on the same
    rung, an error in the table would be invisible."""
    rungs = {r["mcs_index"] for r in rows if r["mcs_index"] is not None}
    assert len(rungs) >= 3, f"only {len(rungs)} distinct modulation(s) exercised"


def test_the_constants_themselves_match():
    """The table, the floor and the usable fraction, read back out of the
    JavaScript. Values agreeing at sixteen points would not catch a row added
    to one side and not the other."""
    script = textwrap.dedent(
        f"""
        const LB = await import('{MOCK}/linkBudget.js');
        console.log(JSON.stringify({{
          table: LB.MCS_TABLE.map((m) => [m.index, m.minRssiDbm, m.phyMbps]),
          floor: LB.NOISE_FLOOR_DBM,
          fraction: LB.USABLE_FRACTION_OF_PHY,
        }}));
        """
    )
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        capture_output=True, text=True, timeout=120,
    )
    if result.returncode != 0:
        pytest.fail(f"node failed:\n{result.stderr}")
    js = json.loads(result.stdout)

    assert js["table"] == [[m.index, m.min_rssi_dbm, m.phy_mbps] for m in MCS_TABLE]
    assert js["floor"] == NOISE_FLOOR_DBM
    assert js["fraction"] == USABLE_FRACTION_OF_PHY


def test_rate_selection_agrees_either_side_of_every_rung():
    """Swept rather than sampled, because rate selection is a step function and
    the interesting values are the steps. A tenth of a decibel either side of
    each boundary, plus the cliff."""
    script = textwrap.dedent(
        f"""
        const LB = await import('{MOCK}/linkBudget.js');
        const probes = [];
        for (const mcs of LB.MCS_TABLE) {{
          probes.push(mcs.minRssiDbm - 0.1, mcs.minRssiDbm, mcs.minRssiDbm + 0.1);
        }}
        probes.push(-120, -95, -30);
        console.log(JSON.stringify(probes.map((r) => {{
          const m = LB.selectMcs(r);
          return [r, m === null ? null : m.index,
                  LB.headroomDb(r), LB.qualityFromRssi(r)];
        }})));
        """
    )
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        capture_output=True, text=True, timeout=120,
    )
    if result.returncode != 0:
        pytest.fail(f"node failed:\n{result.stderr}")

    rows = json.loads(result.stdout)
    assert len(rows) == len(MCS_TABLE) * 3 + 3
    for rssi, index, head, quality in rows:
        mcs = select_mcs(rssi)
        assert (mcs.index if mcs else None) == index, f"at {rssi} dBm"
        assert headroom_db(rssi) == pytest.approx(head, abs=1e-9), f"at {rssi} dBm"
        assert quality_from_rssi(rssi) == pytest.approx(quality, abs=1e-9), f"at {rssi} dBm"
