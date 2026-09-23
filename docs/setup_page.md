# The mission setup page

Design, before code. Everything here is a proposal; the parts I would argue
with are marked, and there are more of them than usual because the hardest
question this page raises — what a stranger sees on an empty one — has an
answer I only half like.

## The problem, in one paragraph

A growing list of values has to be told to this system before a mission, and
they currently live as YAML on the Jetson: sonar mounting geometry and lever
arm, battery capacity, survey area, sonar range and gain, and now the shore
station's position and bearing. Editing them needs an SSH session. Nobody in
the club except Auxence has one, which is why `measured: false` in
`omniscan_bridge/config/mounting.yaml` has survived two weeks of pre-flight
warnings naming that file on every single run. **A warning that names
something nobody can open is not a warning, it is background noise**, and the
whole club has learned to read past it.

## What this page is, and is not

It is a **preparation** page. Preparing a mission and running one are different
activities: preparation is calm, on the ground, with time to think; running is
tense and wants very few things on screen. Mixing them is what clutters a
cockpit, so this is a separate route — `/setup` on the same server, sharing
the same connection — and not a panel.

It is **not in the safety chain**, and nothing on it can arm the vessel, move
it, or change its mode. It cannot start or stop a mission. If it could make
the boat do something, it would have stopped being a preparation page.

It is also **not the mission planner**. That distinction gets its own section,
because the line is less obvious than it first looked.

## Two axes, not one

The working rule was: *does the value describe the world, or the equipment?*
World is planning, equipment is setup. That is right, and it is the answer to
**"which page?"**.

But it does not answer **"which section?"**, and the second question turns out
to matter more. The two tiers are decided by a different axis entirely:

> **The tier is the value's expected lifetime.**

* **Vessel** — changes when somebody rebuilds the boat. Mounting geometry,
  lever arm, battery capacity, camera calibration, antenna heights. A vessel
  value three months old is fine.
* **Deployment** — changes every time somebody sets up on a beach. Shore
  station position and bearing, sea state, channel width, local origin. A
  deployment value three months old is almost certainly wrong.

This is worth encoding rather than merely arranging, and it is the argument
for the split: it is the only thing that lets the page say *"this was last set
in July, and it is the kind of value that should not be"*. A flat list cannot
say that, and it is precisely what somebody arriving on a beach needs to know
when they cannot remember what was last set. Every field therefore carries a
date, and only the Deployment tier goes stale.

## What goes on it

### Vessel

| Field | Why it matters | Currently |
|---|---|---|
| Sonar tilt, lever arm (aft/starboard/below) | Every sounding carries this offset. Wrong here means data that cannot be georeferenced afterwards. | `mounting.yaml`, `measured: false` |
| `measured` / `measured_by` / `measured_utc` | Provenance. Not decoration — see below. | same |
| Battery capacity (Wh), hotel load (W) | Endurance is arithmetic on these. Wrong here means a confident wrong number. | `topics.yaml` |
| Shore antenna gain, beamwidths | Link budget. | `link_budget.py` defaults |
| Boat antenna height above water | Dominates useful range: the two-ray term goes as the product of the two heights. | guess |
| Camera intrinsics | Read-only here; produced by `calibration`. Shown so somebody can see whether it has ever been done. | `front_camera.yaml` |

### Deployment

| Field | Why it matters | Currently |
|---|---|---|
| Shore station position | Range and bearing from the sector's boresight. | `topics.yaml`, `configured: false` |
| Shore station bearing (boresight) | The number that turns "the link dropped" into "the link is going to drop". | same |
| Tripod height | Two-ray, and the close-in nulls. | default 2.9 m |
| Sea state | Decides whether the multipath nulls exist at all. | default 0.5 m |
| Channel width | 3 dB of range per doubling, against throughput. | default 40 MHz |
| Local origin (datum) | Everything in metres is relative to it. | derived, no override |
| Sonar range / gain **defaults** | Equipment settings derived from a world fact. | `mission_defaults.yaml` |

## The three places the rule broke

**Sonar range is both.** It is an equipment setting derived from a world fact
(depth). The page holds the *default*; the planner may override it per area,
because a box spanning 8 m and 40 m of water genuinely wants two settings and
forcing one is how a bad line gets recorded and nobody notices until
post-processing.

**The shore station is the exception that proves the page.** It describes
equipment, so by the world/equipment axis it is setup — but it moves every
mission and can be turned by hand mid-mission, so by lifetime it behaves like
planning. It fits neither "set once in the workshop" nor "part of the plan",
and that gap is exactly the shape of the page.

**"Survey area" hides a datum.** The area is planning. The **local origin** is
neither: it is the reference every recorded metre is relative to, fixed once
per mission and never moved. It is derived from the first fix, and overriding
it is a deliberate act behind a confirmation, because a human typing a
latitude is a human putting a typo into every metre of the mission.

## Arriving with nothing

This is the case that decides whether the rest of the club can use this, so it
gets the most space.

A fresh Jetson, a beach, somebody who is not Auxence. The two obvious answers
are both wrong. **A wizard** is wrong because this is done once and then
*edited* forever after, and a wizard you cannot skip is hostile on the second
use. **A form with twenty blank fields** is wrong because nothing on it says
which three matter today.

### 1. The page is the checklist

Fields are ordered by *what blocks what*, not by category. The top of the page
is not a form, it is a sentence and a short list:

> **Two things need filling in before this boat should survey.**
> One of them will ruin your data. The other will only make the screen less
> useful.

Everything already set collapses under a heading that says how many and when.
The ranking is the important part: **only the mounting geometry corrupts
recorded data**. A missing station position makes the link panel useless and
costs nothing that cannot be recovered afterwards. An operator with twenty
minutes of daylight needs to know which is which, and today nothing tells them
— all the warnings are amber and all of them name files.

### 2. Most of it is captured, not typed

The insight that makes an empty page tractable: **the boat is a GPS receiver,
and you are standing next to the tripod holding it.**

* **Station position** — put the vessel beside the tripod, press *Set from
  vessel*. Accuracy of a few metres against a 120° sector at 2.5 km is
  nothing. Nobody types a latitude.
* **Boresight** — sail out, aim the antenna at the boat, press *Set from
  vessel bearing*. The bearing from station to vessel becomes the boresight.
  This is not only easier than a compass reading, it is more accurate, and it
  measures the thing you actually care about rather than a proxy for it.
* **Local origin** — the first fix, automatically.
* **Sea state** — a three-way choice (*glassy / slight / moderate*), not a
  number in metres. Nobody knows the RMS wave height, and a field that invites
  a made-up number is worse than a coarse one that invites an honest answer.
* **Antenna and tripod heights** — a tape measure, typed once, Vessel tier.

Only the mounting geometry genuinely requires somebody with a tape measure and
a still boat, and that is the one that deserves the friction.

### 3. Provenance is a first-class field

Every value carries where it came from: `default`, `entered`, `captured`, or
`measured`. The page shows provenance next to the value, not just the value.

This is the mechanism that makes the whole thing work, and it generalises what
`mounting.yaml` already does with its `measured:` line. **A field still on
`default` is what the pre-flight warns about** — so "has anybody actually
checked this?" becomes a property of the data rather than a comment in a file,
and it is the same question for all twenty fields instead of one special case.

### 4. Saved deployments

A deployment can be saved by name (*Walvis Bay, north box*) and reloaded. The
first visit to a beach is the slow one; the twentieth is *load, adjust the
boresight, go*. Without this the page is a chore that gets skipped, which
returns us to `measured: false`.

## Pre-flight: from a warning to a task

The current `sonar.mounting` WARN reads:

> Measure the tilt and the lever arm from the GNSS antenna to the transducer,
> write them into `mounting.yaml`, and set `measured: true`.

Every word is true and it has achieved nothing in two weeks, because the
remedy names a file behind an SSH session. The fix is structural, not
editorial:

* `CheckResult` gains **`setup_field`** — the id of the field this check
  wants, e.g. `vessel.sonar.mounting`. Not a path. Not a sentence.
* The pre-flight panel renders any check carrying one as a **link that opens
  `/setup` focused on that field**, and the remedy is written in terms of the
  field: *"Sonar mounting geometry has never been measured."*
* The page shows the same fields flagged from the same source, so the two
  cannot disagree about what is outstanding.

Of the eighteen checks today, five would carry a `setup_field`:
`sonar.mounting`, `pico.estop_feedback` (which names a firmware flag and will
stay amber — correctly — but should say where), and the three that would be
added for station position, battery capacity and antenna height.

**The clearing has to stay honest.** `sonar.mounting` already reads the
provenance the *bridge reported*, not the file on disk, so editing the file
without restarting cannot turn the check green while the old numbers are in
use. That property becomes more important once a form can write the file, not
less: a form that appears to succeed while the node still uses the old values
is strictly worse than an SSH session, because it also destroys the operator's
reason to doubt.

## Storage

**One file per deployment on the Jetson**, overlaying the per-package defaults
rather than replacing them. The packages keep reading their own YAML; the
setup file is applied on top.

The alternative — the page writing directly into six packages' config files —
was rejected because it makes the page know the layout of every package, and
every future package has to be taught about the page. It also means a partly
applied write leaves the vessel in a state no file describes.

Format: plain YAML, with `written_utc_ms`, `written_by`, a content hash, and a
per-field provenance map. The hash is not security; it is so the pre-flight
can say *"this setup was written fourteen minutes ago"* rather than silently
accepting whatever is on disk.

## Transfer

Auxence asked for cable-only while the boat is alongside, and suggested the
Jetson enforce it rather than the UI. The second half is right and I would go
further; the first half I would change.

**"Cable" is not checkable, and checking it would be theatre.** Both a cable
and the WiFi link are IP to the Jetson; distinguishing them means inspecting
the interface a request arrived on, which breaks the moment somebody uses a
USB-Ethernet adapter, a different port, or a laptop bridging both. A check
that can be wrong in the permissive direction *and* the obstructive one is
worse than no check.

What is checkable, and is what actually matters:

> The Jetson accepts a setup push only when the Pico reports **disarmed** and
> the recorder reports **not recording**.

That is the real requirement — nobody should be able to change sonar mounting
geometry with the boat under way — it is enforced where it cannot be bypassed
by opening the page from a different machine, and it reads off the same
authority the rest of the GUI already trusts. The refusal must say which
condition failed, in the vessel's terms: *"The Pico reports armed. Setup
cannot be changed while the vessel is armed."*

Cable-only then becomes an operating convention rather than a mechanism, which
is the honest place for it.

## Where I would argue with myself

**1. Writing config that needs a restart is a trap with a new coat of paint.**
Nodes read their YAML at startup. A value entered on the page does not take
effect until `omniscan_bridge` restarts, so the page must either say so and
offer to restart, or push through ROS parameters. I would take the first —
explicit, with the check still reading reported provenance — but I do not like
it: the page will sometimes have to tell somebody that what they just did has
not happened yet. The alternative is worse (a live parameter push that half
succeeds), but this is the weakest part of the design and I would want it
looked at rather than waved through.

**2. `channel_width_mhz` is in the wrong tier, probably.** It is radio
configuration, which is Vessel by the equipment axis — but it changes with
site interference, which is Deployment by the lifetime axis. I have put it in
Deployment. It is the one field where the two axes genuinely conflict and I do
not have a principled answer, only a guess about which way it will actually be
used.

**3. Unmeasured mounting geometry is WARN, and perhaps it should be FAIL.**
It is the one value that corrupts recorded data rather than merely degrading
the display: survey on defaults and the soundings carry an unknown offset that
cannot be removed afterwards. The existing WARN was a deliberate choice and I
am not overriding it here, but the page makes the fix cheap for the first
time, which weakens the argument for tolerating it. Worth revisiting once
somebody has actually filled the page in once.

**4. Capturing the station position from the vessel has a failure mode.** If
the vessel's fix is poor when the button is pressed — alongside, under a
cliff, cold-started — the captured position is quietly wrong and everything
downstream inherits it. The capture must therefore record the fix quality at
the moment of capture and refuse below a threshold, and the page must show
*"captured at 14:02, 8 satellites, HDOP 1.1"* rather than just a number. A
captured value that looks identical to a measured one is the same class of
lie as a predicted RSSI shown as a measurement.

**5. I am not certain the page should be reachable while a mission is
running.** Read-only, clearly, for somebody who wants to check what a value
is. But a read-only route is a second rendering path for the same fields, and
two paths disagree eventually. Leaning towards: reachable, read-only, with the
editing controls absent rather than disabled — absent controls cannot be
re-enabled by a stale React state.

## What is deliberately left off

* Anything the vessel measures itself — position, heading, state of charge.
* Anything in the safety chain. Arming is RC channel 7 and nothing else.
* Mission execution controls: start, stop, abort.
* The survey area and line plan — that is the planner, and it is a separate
  piece of work.
* Network configuration. Tempting, and it is how you brick a Jetson on a
  beach.

## Build order

1. The provenance model and the setup file format, ROS-free and testable with
   no hardware. This is the part everything else reads.
2. `setup_field` on `CheckResult`, and the pre-flight panel rendering links.
   Small, and it is the change most likely to get `measured: false` cleared.
3. The page: Vessel and Deployment tiers, staleness dates, the "two things
   need filling in" header.
4. Capture-from-vessel for station position and boresight, with fix quality
   recorded.
5. The Jetson-side accept condition, and the refusal wording.
6. Saved deployments.

Steps 1–3 are laptop work against the simulator, which is the same split that
has worked for the last three pieces.

## Open questions this closes, and does not

Closes the *mechanism* for Q2 (mounting geometry) and Q11 (station position
and bearing) — both become fields somebody can fill in rather than files
somebody cannot open. It does not close either question: that still needs a
tape measure and a beach. What it removes is the excuse.
