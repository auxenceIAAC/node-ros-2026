# asket_gui

The mission cockpit. React + MapLibre GL, built with Vite, served by
`gui_backend`.

```bash
npm install
npm run dev:mock  # the whole GUI, on a laptop, with nothing else installed
npm run dev       # against a real backend on :8080
npm run build     # output goes straight into gui_backend/gui_backend/static/
```

## Mock mode — run the GUI with no ROS 2, no Python and no boat

```bash
cd src/asket_gui
npm install
npm run dev:mock          # http://localhost:5173
```

That is the whole setup — Node and nothing else. No ROS 2, no backend process,
no Jetson, no hardware, no internet, no `colcon build`. Everything on screen is
generated in the browser. Verified: `node --version` 22, `npm install`,
`npm run dev:mock`, open the URL it prints.

### Start here: press "Everything working"

The dev panel is the third column, on the right, and it only exists in mock
mode. Its first control, under **Baseline**, is **Everything working**.

Press it before anything else. A freshly started simulator sits in MANUAL,
disarmed and idle, so three panels read "not sent" and the pre-flight says no
report exists — nothing is wrong, and it is indistinguishable from a vessel
that is switched off. That button puts the boat autonomous, armed, recording
and pre-flighted, which is what a good day looks like.

It matters in that order. Every other control on the panel makes something
*worse*, and you cannot tell whether a degraded state reads correctly without
having seen the healthy one first.

The backend has the same baseline, through the same three commands:

```bash
python3 -m gui_backend.core.app --sim --healthy    # http://localhost:8080
```

That one needs Python and the GUI packages on `PYTHONPATH`, but still no ROS
and no boat. Use it when you want the real payload builders and the real
WebSocket rather than the browser mock.

**Mock mode is permanent, not a throwaway.** It is how the interface gets
developed and reviewed without taking the vessel out.

### How it avoids being a fake

A mock that behaves differently from the real system is worse than no mock,
because it gets reviewed and believed. Three things stop that here:

* **The components are not forked.** Mock mode swaps the *socket*, not the app.
  `Connection` takes a transport; mock mode hands it one that speaks the same
  protocol from inside the browser. Everything above the socket — clock skew,
  the append-only stream splicing, the command lifecycle, reconnection — is the
  real client code.
* **The negotiation table is generated, not transcribed.**
  `src/lib/mock/streamPolicy.json` comes from `gui_backend/core/streams.py`, and
  `test_mock_policy.py` fails if the two drift. Regenerate with:

  ```bash
  python3 -m gui_backend.tools.export_stream_policy \
      --out src/asket_gui/src/lib/mock/streamPolicy.json
  ```

* **The payload shapes are checked across languages.**
  `test_mock_payload_shapes.py` runs the JavaScript payload builders under Node
  and compares their key sets with the Python ones, per stream and per detail
  level — so a panel cannot come to depend on a field the vessel never sends.

### The dev control panel

Visible only in mock mode (`connection.isMock`, set only by the mock factory).
Every control names the behaviour it is there to demonstrate, so somebody who
did not write the GUI can tell whether what they are looking at is right.

| Group | Triggers |
|---|---|
| Link | 4G link, link lost |
| Profile | Full / Reduced / Beacon / Auto (manual override) |
| Sensors | Sonar dropout, heading invalid, GNSS degraded, sonar clock drift, lidar stalled, RC link lost |
| Resources | Disk full, low battery |
| Mode commands | Confirmation slow (2.5 s), confirmation never arrives |
| Map | Basemap present / absent |
| Survey speed | 1× / 4× / 12× — speeds the *vessel*, never the clock |
| Heading source | Magnetometer vs GNSS compass, to compare seabed error at 50 m |

The time scale deliberately does not touch the clock. Timestamps stay real, so
data age keeps meaning what it says; only the vessel moves faster.

### Offline maps in mock mode

Both paths are switchable, because "the map degrades honestly with no tiles" is
a behaviour worth being able to *see* rather than take on trust:

* **Tiles absent** (default) — the map falls back to a labelled coordinate
  graticule and says why. This is what the field looks like if the `.mbtiles`
  for the survey area is missing from the Jetson.
* **Tiles present** — synthetic raster tiles generated in the browser through a
  MapLibre custom protocol. Nothing is fetched and nothing is bundled. They are
  stamped `SYNTHETIC — not a chart` so they cannot be mistaken for bathymetry;
  they exist so the tiled rendering path can be reviewed with no file and no
  internet.

The dependency tree is deliberately small — React, MapLibre and Vite, nothing
else. It has to build offline on arm64, and every kilobyte of it crosses the
shore link on first load.

## Layout priority

Map first, vessel state always visible, everything else secondary. Designed for
a laptop screen outdoors in bright sunlight: high contrast, large type for the
values that matter, and no low-contrast greys used to carry meaning.

## Where the safety rules live

| Rule | Where |
|---|---|
| 3 — the soft ESTOP is "Cut propulsion", never "Emergency stop" | `src/lib/labels.js` owns every command string, so the wording is one decision in one place |
| 4 — displayed state is confirmed state | `panels/VesselState.jsx` renders only what the Pico reports. `panels/ModeCommands.jsx` renders the *command*, separately, and never touches the displayed mode |
| 5 — two-step confirmation | `components/ConfirmButton.jsx`. Arm, then confirm; the armed state times out on its own |
| 7 — data age is always visible | `components/Value.jsx` and `lib/staleness.js`. Stale values are degraded loudly — struck through and red — because nobody notices a slightly different grey in sunlight |

## The basemap

There *is* internet in the field — Namibia has 4G at the launch point — so the
map has a real basemap rather than a coordinate grid. It comes through the
backend in four steps of preference: the Jetson's tile cache, the internet, a
local `.mbtiles` file, and a coordinate graticule when there is nothing else.
`docs/map_tiles.md` has the reasoning, including why the basemap is not
OpenStreetMap's own tile server.

No style, glyph or sprite is fetched from a third party by the browser. The
style is written in `lib/mapStyle.js` and every URL in it points at our own
backend, so there is one cache and the map works on a laptop with no internet
of its own.

The basemap is deliberately **grey**. This map carries coverage, planned lines,
geofence, track, lidar and the vessel in five saturated colours, and a normal
street map competes with all of them. Land is near-white, water is a
blue-grey chosen to stay away from the plan's blue, and woodland is grey
because green is taken. The one exception is the coastline, which gets a
definite stroke — for a boat it is the most useful line on the map.

When there is no basemap at all the map draws a **coordinate graticule** and
says so in plain words. It never silently shows an empty rectangle — an
operator would read that as "no map today" rather than "the link dropped", and
those need different actions.

The vessel's heading arrow stops rotating and turns red when heading is
invalid. Pointing an arrow confidently in a direction we do not trust is exactly
the lie this GUI must not tell.

## Coverage

The coverage overlay is built as a **ribbon of quads between consecutive
samples**, not one filled polygon. A gap in the data therefore renders as a gap
in the ribbon. With one sonar unit covering one side only, a gap that renders as
filled is the single most expensive way this GUI could mislead an operator —
you find out back in Windhoek.

## Bandwidth

`track` and `coverage` are **append-only** streams: the server sends only what
is new since this client last heard, and the client splices it on. Sending the
whole history each frame reached 18 kB per frame after ninety seconds and would
have been about a megabyte per frame after three hours — the exact
"works on the bench, collapses offshore" failure the architecture is meant to
prevent. Measured against the simulator, the full subscription set costs about
6–8 kB/s and stays flat as the mission runs.

If an increment arrives that cannot be spliced onto what we hold — a dropped
frame, or a reconnect — the stream is reset and a full resync requested. A
visibly short history is recoverable; a silently wrong coverage ribbon is not.

The one thing that does grow with mission length is the **initial** resync a
newly connected client receives. At `reduced` detail a three-hour survey is
roughly 160 kB, which is about a second and a half of 4G.

## Why it is white

A single light theme, no dark mode, no toggle. This runs on a laptop on a
Namibian beach at midday, and in strong sun a dark screen stops being a screen
and becomes a mirror.

The rule the stylesheet is built on: **lightness never carries meaning.** A
mid-grey label on white is the first thing sunlight takes, and labels are the
words that say what a number means. De-emphasis is done with size and weight
instead. Grey appears in exactly two places, both saying *this is not a live
value* — a field that was never sent, and the map graticule.

Colour is spent only where it has to survive a glance: red for alarms,
failures, ESTOP and Cut propulsion; amber for warnings and stale data; green
for a status dot, never for text; purple for mock mode, which has to be
unmissable. Borders are 1px black hairlines, and there are no gradients,
shadows or decorative rounding.

Map layer colours are declared once in `panels/MissionMap.jsx` and read by both
the layer paint and the legend, so the legend cannot drift from what is drawn.

## Layout

Three columns: map, cockpit, and — in mock mode only — the dev panel.

The cockpit is the larger share. The map needs enough width to judge coverage;
it does not need most of the screen, and it used to take 60% of it to show a
vessel and a coverage ribbon a couple of hundred pixels across while Power,
Sonar, Recording, Pre-flight and Link all sat below the fold.

Three values must never require scrolling, so they live in the top bar and stay
there whatever the scroll position: **battery** (charge and endurance),
**recording** (state and elapsed) and **link** (bearer and profile). That strip
is a summary, not a second source — every value in it reads the same payload as
the panel that owns it, and `test_gui_single_source.py` fails if that stops
being true.

## Collapsing detail

Forty-five numbers were on screen at once. Nobody monitors forty-five: an
operator watches five and ignores the rest, so the other forty were burying the
five that mattered.

The split is not by volume. It is by one question — **would a change in this
value require somebody to act?** Values that trigger action stay out; values
that explain or detail fold away. So Power shows the charge and the verdict, and
folds voltage, current, draw, endurance and the survey comparison; Heading shows
the bearing and its source, and folds accuracy, seabed error and course.

Two rules make that safe, and `test_gui_single_source.py` fails if either
breaks:

1. **A collapsed panel shows a verdict, not raw data.** "Enough charge to finish
   the planned survey" is worth more than "95%", because 95% does not say
   whether it is enough.
2. **Anything off-nominal forces itself open**, with the reason on the header.
   A fault behind a disclosure triangle is a hidden fault. While a panel is held
   open the operator's preference is not overwritten, so when the condition
   clears it folds back to however they had left it.

One exception is written into the code rather than left to the rule: the
**sonar clock offset** stays visible whatever else folds. It is the only value
whose drift ruins an entire dataset with no other symptom — the survey looks
perfect on the day and is un-georeferenceable back home — and behind a
disclosure triangle nobody would ever look at it.

Open/closed state is per panel and survives a reload (`lib/collapse.js`).

## One value, one place

Heading used to appear in both the vessel panel and the heading panel, read
from two streams at two rates. The two rows sat next to each other reading 029°
and 028°. An operator cannot tell which to believe, so they stop believing
both — worse than either number being slightly off.

Heading now lives only in its own panel, which falls back to the `vessel`
stream on the beacon profile (where the `heading` stream is not carried at all)
and says on screen that it has done so. Hull roll/pitch and **transducer**
pitch/roll are different sensors and are labelled apart for the same reason.

## Data age

Every live value carries its age. Age is computed against an estimate of the
*server's* clock rather than the laptop's, so a laptop with a wrong clock
cannot make everything look permanently fresh. See `lib/connection.js`.

Staleness thresholds scale with the negotiated rate: a 0.2 Hz stream on the
beacon profile is not stale at four seconds old, and painting the screen red
because the link is working exactly as negotiated would teach the operator to
ignore the colour.

The header reports what has actually been **received**, not what the socket
believes about itself. A TCP connection stays open for tens of seconds after
the link behind it has gone, and for all of that time `connected` is true while
nothing is arriving.
