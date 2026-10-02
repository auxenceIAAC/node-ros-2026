# The mission planner — thinking, before any code

Asked for after the setup page, on the same terms: what the workflow looks
like, what already exists, what the hard parts are, and where I would disagree
with the sketch. No code yet.

The sketch was:

> Draw a survey area on the map (rectangle that can be rotated), set line
> spacing and direction, see the generated lawnmower lines, estimated distance,
> estimated duration, whether the battery covers it, save and reload a plan.

Most of that survives. One part of it is wrong in a way that matters, and the
reason is the single sonar.

---

## Start here: the pattern we already generate does not work

`asket_common/survey.py` already builds a lawnmower. It is a textbook
boustrophedon: lines at `i × spacing` across the box, every other line
reversed so the vessel does not fly back to the start of each one. For a
dual-head sonar painting a swath centred on the track, that is correct.

We have one head, looking to starboard. The swath is not centred on the track;
it is entirely on one side. And the side a starboard swath lands on **flips
with the vessel's heading**. Reversing every other line therefore reverses
which strip that line covers.

I measured it rather than argued it. Six lines, spacing set to the swath width
(12.6 m, from a 30 m range at 35° tilt in 18 m of water), over a 63 m box:

| | |
|---|---|
| Covered at least once | **60 %** |
| Covered **twice** | **60 %** |
| Never ensonified | **40 %** |

Every metre that gets covered gets covered twice, and two fifths of the box is
not covered at all. The intervals come out as `[0–12.6] [0–12.6] [25.2–37.8]
[25.2–37.8] [50.4–63]`: lines pair up, each pair painting the same strip twice,
then the pattern jumps a whole strip.

This is not a planner bug waiting to happen. It is in the repository now, it is
what `asket_sim` drives, and it is what the `plan` stream publishes to the map.
Nobody has noticed because no real survey has been flown and because the
coverage ribbon is painted from the same assumption as the plan — so on screen
it looks consistent with itself.

**The planner's first job is to not do this.** Everything below follows from
that.

## The pattern that does work

The constraint, stated plainly: a pass covers the strip *to starboard of the
track*, and which side that is depends on the heading. So:

* A northbound pass at `x` covers `[x, x + w]`.
* A southbound pass at `x` covers `[x − w, x]`.

To tile strips `[k·w, (k+1)·w]` with no gap and no overlap, each strip needs
either a northbound pass at `k·w` or a southbound pass at `(k+1)·w`. Try to
alternate direction on consecutive *distinct* track positions and it does not
close: the arithmetic forces two consecutive passes onto the same track line.

Which is the answer, not the obstacle. **Drive each track in both directions.**
Tracks at `2w` spacing; the southbound pass covers the strip to the west, the
northbound pass covers the strip to the east, and together each track paints a
`2w` band centred on itself. Tracks at `2w` tile exactly:

| | Passes | Covered | Double-covered | Box width |
|---|---|---|---|---|
| Existing boustrophedon | 6 | 60 % | 60 % | 63 m |
| Each track driven both ways | 6 | **100 %** | **0 %** | 76 m |

Same six passes, same 2.4 km of survey line, no gaps, and it covers a box 20 %
wider. The pattern is strictly better, not a trade.

What it costs is the turn. At the end of a track you turn 180° and come back
down the same line, which is the tightest turn available; then at the far end
you translate `2w` across to the next track. So the turns alternate between a
U-turn and a normal cross-leg, rather than being all cross-legs. For an ASV
with a turning circle smaller than `2w` — which at a 25 m track spacing is
nearly any hull — that is fine, and the turn happens outside the box anyway if
the lines are extended (below).

A second option exists and I would not take it: **run every line in the same
direction** and treat the return as a transit. It tiles at spacing `w`, it
needs no U-turns, and it throws away half the distance — the return leg covers
a strip too, if you let it, and refusing to let it is just a worse version of
the same plan. I mention it because it is the obvious first idea and it is
worth having a reason to reject.

> **The headline number for the operator:** a single-sided sonar doubles the
> distance for a given area against a dual-head one. Not the line count over a
> fixed box — the *distance*, and therefore the battery. That is the sentence
> the planner needs to make unmissable, because it is the difference between a
> box that fits in an afternoon and one that does not.

## What already exists

In `asket_common/survey.py`:

* `SurveyPlan` — the boustrophedon above. **Its `_build()` is the thing to
  replace**, and the dataclass around it is mostly reusable: origin, heading,
  line length, spacing, count, start corner, sonar side, swath width,
  waypoints.
* `swath_half_width_m(range_m, tilt_deg, depth_m)` — already does the honest
  thing of returning the lesser of range-limited and depth-limited reach.
* `swath_polygon(...)` — near/far edge pair per ping, stitched into a ribbon by
  the coverage layer. Already side-aware.
* `line_segments_geo()`, `total_survey_distance_m()`,
  `total_track_distance_m()`.

In `asket_common/geo.py`: `LocalOrigin` (geodetic ↔ local ENU),
`bearing_to_enu`, `enu_bearing_deg`, `distance_m`, the angle wrappers.

Elsewhere: the `plan` stream already exists in `streams.py` and
`sim_source.py`, with a `plan_payload`; the map already renders planned lines
and a coverage ribbon; `power_payload` already computes
`survey_remaining_m`, `endurance_margin_s` and `can_finish_survey` when it is
given a remaining distance and a speed.

So the planner is less new machinery than it looks. What is genuinely new is
the drawing interaction, the plan file, and the arithmetic in the panel.

## Workflow

1. **Draw the box.** Click one corner, drag to the opposite corner, then a
   rotation handle. Three points of interaction, no numeric entry required —
   but every value is editable as a number as well, because "0.5 km × 1.2 km
   at 030°" is how somebody describes a box to a colleague on the phone.
2. **The planner picks the line direction**, defaulting to the box's long axis,
   because that minimises turns. A toggle runs them across instead. This is one
   control, not two: the box rotation and the line bearing are the same number
   unless you ask for the cross case.
3. **Spacing is derived, not typed.** `2 × swath_half_width_m(range, tilt,
   depth)` from the setup values, shown as a number the operator can override.
   Typing it first is the wrong default: the number that matters is the swath,
   and the operator should see that the spacing came from it.
4. **The plan appears**: lines, pass order, turn geometry, and the strip each
   pass covers. Distance, duration at the planned speed, and the battery
   figure.
5. **Save**, with a name. **Reload** from a list.

Not a wizard, same argument as the setup page: after the first time, every
visit is to change one number and look at the consequence.

## The hard parts

### 1. The depth you assume decides the spacing, and nobody knows it

`swath_half_width_m` needs a depth. The operator is asked for an expected
depth; in Namibia the real depth varies across the box. Assume 18 m and survey
in 12 m and the swath is narrower than planned — the lines are too far apart
and there are gaps, **and the coverage ribbon will not show them**, because
today the ribbon is painted from the same assumed swath as the plan. The
display agrees with the plan and both are wrong.

This is the most important thing in this document after the pattern. The fix is
not in the planner:

> **The coverage ribbon must be painted from the measured swath — the farthest
> valid return in each ping — not from the planned one.**

Then a shallow patch shows as a ribbon that narrows, a gap shows as a gap, and
the operator can add infill lines while still on site rather than discovering
it in Windhoek. Until that is true, a planner that computes spacing from an
assumed depth is building a number nothing will ever check.

I would do that change *before* the planner, or at least alongside it. It is
the difference between a planning tool and a planning tool that can be trusted.

### 2. "Whether the battery covers it" is the wrong question

It is the right concern asked at the wrong moment, and a yes/no hides what the
operator needs.

* The battery state at planning time is not the state at the start.
* A binary answer is unstable: it flips between yes and no on a 2 % change in
  consumption, which trains people to ignore it.
* It answers a question nobody can act on. "No" does not say how much to cut.

What I would show instead, in this order:

1. **Distance and duration**, split into on-survey and turns, because the turn
   fraction is what tells you whether the box shape is sensible.
2. **The plan as a percentage of present endurance**, with the consumption
   figure it used — a number, not a verdict.
3. **The abort line.** *"From line 14 onward you no longer have the range to
   return to the launch point."* That is the number that matters at sea, it is
   computable from the same arithmetic, and it is actionable in a way "can
   finish: false" is not.

`power_payload` already produces `endurance_margin_s` and
`can_finish_survey`; I would keep both and stop leading with the boolean.

### 3. A saved plan must survive the origin moving

`SurveyPlan` is in local ENU with a `LocalOrigin`. The local origin is a
**Deployment-tier setup field taken from the first fix** — so it changes
between missions, by design.

Save a plan in ENU and reload it next week against a new origin and the box
silently moves, by however far the two first fixes differ. Hundreds of metres
is easy.

So: **plans are stored in geographic coordinates.** Corners as lat/lon, bearing
as a true bearing, and the ENU form is derived on load against whatever origin
is current. The file should also record the origin it was planned against, not
to use it but so the plan can say *"planned against an origin 340 m from
today's"* — which is information, not an error.

### 4. Line direction is also a link decision

Nobody has said this and it falls out of the antenna work. The shore station is
directional, and range from it drives the link profile. Lines that run
**radially away from the station** put the vessel at maximum range at the far
end of every single line: the link degrades, recovers, degrades, recovers, once
per line, and video drops out at the same point every time. Lines that run
**across** the bearing hold range roughly constant along each line and step it
between lines.

Neither is wrong, and it should not be a constraint — but the planner knows the
station position (it is a setup field) and can say which the plan is, so the
choice is made rather than discovered. One sentence under the direction
control: *"Lines run radially from the shore station; expect the link to be
poorest at the end of every line."*

### 5. Run-in and run-out

A real survey line is longer than the box. The vessel needs to be settled on
heading before it enters, or the first stretch of each line is flown off-course
and the swath is skewed exactly where the plan says it is clean. Lines should
extend past the box by a run-in distance (a turn diameter is the usual rule),
with the on-survey flag false for the extensions — which `Waypoint.on_survey`
already models.

This also moves the U-turns outside the box, which answers the one real
objection to the both-ways pattern.

### 6. A survey box is not a geofence

The `plan` stream mentions a geofence and it must not become the survey
rectangle. A geofence is a safety boundary; a survey box is a work area, and
you want the vessel to leave it on every turn. Conflating them either makes the
geofence useless or makes every turn a violation. Separate objects, and I would
not build the geofence in this piece of work at all.

## Where I would disagree with the sketch

**"Set line spacing and direction"** — spacing should be derived from the
swath and shown with its working, not typed. The operator who types 25 m
because the last survey used 25 m will get gaps the first time the depth
changes.

**"Rectangle that can be rotated"** — I proposed this and I still would, but I
would be clear that the rectangle is a *v1 restriction, not a model*. The real
survey area in Namibia will not be rectangular. The plan file should therefore
store a **polygon**, with the UI only able to draw a rotated rectangle for now.
Storing a rectangle means the file format changes the day somebody needs an
L-shape, and plans saved before that day become unreadable. A four-vertex
polygon costs nothing today and buys that.

**"Whether the battery covers it"** — as above: show the margin and the abort
line, not a boolean.

**"Estimated duration"** — at what speed? Survey speed is a real planning
input (it trades ping density against time) and it is not currently a setup
field. It should be one, or a plan field. Without it the duration is computed
from whatever the vessel happens to be doing, which is 0 kn on the pontoon.

## What I would leave out of v1

* **Obstacle avoidance in the plan.** The lidar and `boat_bt` handle obstacles
  at run time. A planner that routed around charted obstacles would need charts
  we do not have.
* **Automatic infill.** Re-planning gaps from measured coverage is the obvious
  follow-on and depends entirely on (1) being done first.
* **Tide and current.** Current affects track-keeping and therefore coverage,
  and modelling it without data is theatre.
* **Multi-box missions.** One box, one plan.

## Build order

1. **Honest coverage from measured returns** (hard part 1). Not strictly part
   of the planner; it is what makes the planner checkable, and it is testable in
   sim today.
2. **The single-sided pattern** in `asket_common/survey.py`, replacing
   `_build()`, with the coverage tiling asserted numerically — the table at the
   top of this document is the test.
3. **The plan file**: geographic polygon, bearing, spacing, speed, provenance,
   and the origin it was planned against.
4. **The panel arithmetic**: distance split, duration, margin, abort line.
5. **The drawing interaction** on the map.
6. **Save and reload.**

Steps 1–4 are laptop work against the simulator with no hardware, which is the
same split that has worked for the setup page, the camera and the link model.

## The one thing I would want confirmed before starting

The both-ways pattern assumes **the vessel can hold the same track line in both
directions to within a fraction of the swath width**. If track-keeping is worse
than that — a cross-current, a slow heading loop — the two halves of each band
will not meet and there will be a seam down the middle of every track.

That is a question about the control loop, not the planner, and I cannot
answer it from here. If the answer is no, the fallback is the same-direction
pattern with transit returns: half the efficiency, no seam. The planner should
be able to produce either, and the choice should be a setting rather than
baked in.
