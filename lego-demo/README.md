# Building LEGO assemblies with PartCAD

A small gallery of assemblies that snap common LEGO bricks together using the
PartCAD **ASSY file format** and **interfaces** — no coordinates by hand.

The LDraw parts served by `//pub/universe/lego` already carry the LEGO
interfaces: the `ldraw_repo` plugin attaches them to the parts it serves, at
coordinates derived from each part's name. Every rectangular Brick, Plate and
Tile gets the **stud** (male) and **anti-stud** (female) interfaces, one
instance per stud; the Technic bricks, beams, pins and axles get the **Technic
pin**, **pin hole**, **axle** and **axle hole** interfaces, one instance per
feature. So an assembly just references the original LDraw parts and joins them
with `connect:` — PartCAD places each part so its interface mates the one it is
connected to.

## The stud interfaces

Defined in the package root (`//pub/universe/lego:stud` and `:anti-stud`). Every
port coordinate is in the meshed part-space, where the wrapper meshes each `.dat`
as `(x, −y, z) × 0.4`, so **+Y is up**:

| Interface | Role | Z axis | Where |
| --- | --- | --- | --- |
| `stud` | male | +Y (out of the top) | each top stud, on the top plane (y = 0) |
| `anti-stud` | female | −Y (into the underside) | each stud position, on the part's bottom plane (y = −9.6 mm brick, −3.2 mm plate/tile) |

With that pairing the 180° mate flip about `[1,1,0]` cancels the rotation, so the
upper part is translated by exactly `stud_pos − anti_stud_pos`: aligned studs
stack cleanly, and choosing a different stud offsets the upper part on the 8 mm
grid. Each part's own bottom-plane height is baked into its anti-stud ports, so a
3.2 mm plate seats correctly on a brick with no special-casing.

Each part exposes one instance per stud, named `c<col>r<row>` (column along X,
row along Z), so an assembly can pick a specific stud to place a part on:

```yaml
- part: //pub/universe/lego/ldraw/Brick:3001
  name: upper
  connect:
    with: //pub/universe/lego:anti-stud   # this part's underside
    withInstance: c0r0                     # its corner recess
    name: lower                            # the part already placed
    to: //pub/universe/lego:stud           # a stud on the lower part
    toInstance: c0r0                       # which stud (grid offset lives here)
```

## The Technic interfaces

The other half of the system. A Technic hole is 4.8 mm across and goes right
through a part one stud (8 mm) thick, so it has two mouths and the part carries
one instance per mouth — named after the face it is on, so an assembly can say
which side it is working from:

| Interface | Role | Where | Instances |
| --- | --- | --- | --- |
| `technic-pin-hole` | female | a round hole, through both faces | `h<i>-front`/`-back` on a brick (hole along Z), `h<i>-top`/`-bottom` on a beam (along Y) |
| `technic-pin` | male | the plane of the pin's collar, which stops it against the face | `left`, `right` |
| `technic-axle-hole` | female | a cross hole, through both faces | `axle-front`, `axle-back` |
| `technic-axle` | male | each end of the shaft, its Z pointing at the other end | `left`, `right` |

Unlike a stud, a Technic connection leaves something free, and the interfaces
declare it as a parameter that a `connect:` can set:

* `turnZ` — a pin (or an axle) in a round hole turns; this is the angle.
* `moveZ` — an axle is held by nothing along its length, so how far it lies
  through the hole is free. `0` leaves the end of the axle in the mouth of the
  hole, positive drives it further through, negative draws it back out.

Two beams are joined by *both* being connected to the pin between them, since a
hole is female on both sides — which is exactly how the parts work in the hand:

```yaml
- part: //pub/universe/lego/ldraw/Technic:32316
  name: beam
  connect:
    with: //pub/universe/lego:technic-pin-hole  # this beam's middle hole,
    withInstance: h2-bottom                     # entered from below
    withParams:
      turnZ: 90                                 # turned a quarter turn on the pin
    name: pin                                   # the pin already placed
    to: //pub/universe/lego:technic-pin
    toInstance: left                            # the end still sticking out
```

## The other systems

Beyond the studs and Technic, the plugin attaches four more pairs, each derived
the same way and each verified against the LDraw parts themselves:

| Interfaces | On | The connection |
| --- | --- | --- |
| `gear-tooth` / `gear-gap` | `Technic Gear N Tooth` (22 parts) | a mesh: `t<i>` is a tooth, `g<i>` the gap after it |
| `minifig-neck` / `minifig-neck-socket` | torso / head (939 parts) | the head turns on the torso |
| `minifig-waist` / `minifig-waist-socket` | hips / torso (838 parts) | the torso turns on the hips |
| `duplo-stud` / `duplo-anti-stud` | `Duplo Brick A x B` | the stud system at twice the size |
| `wheel-rim` / `tyre-bore` | `Wheel W x D` / `Tyre W/ A x D` | a tyre, concentric on its wheel |
| `rj12-plug` / `rj12-socket` | Mindstorms cables / bricks, motors, sensors | the cable a Mindstorms part is driven through |

A **gear mesh** is the one that looks least like a port pair and turns out to be
exactly one. LEGO gears are cut to a single module — the pitch diameter in
millimetres is the tooth count — so every tooth and every gap sits on a pitch
circle of half a millimetre per tooth, with its Z pointing radially outwards.
Bring a tooth port and a gap port together and the two gears end up the sum of
their pitch radii apart, which is the centre distance the pair is cut for, with
the second gear turned to mesh:

```yaml
- part: //pub/universe/lego/ldraw/Technic:3647   # 8 tooth
  name: pinion
  connect:
    with: //pub/universe/lego:gear-gap
    withInstance: g0
    name: drive                                   # the 24-tooth gear
    to: //pub/universe/lego:gear-tooth
    toInstance: t0                                # which tooth: it picks the angle
```

The **minifig** joints come from the system rather than from any one part's
name: LDraw's own assembled minifigs put the torso 32 LDU above the hips and the
head 28 LDU above the torso, and every torso carries the same joints whatever is
printed on it. Both turn, so `turnZ` poses the figure. A hat needs nothing new —
the top of a head is an ordinary `stud`.

A **tyre** goes on a wheel concentrically and LDraw draws the pair sharing one
origin, so the mate is the identity. The instance is named for the nominal
fitting diameter (`d30`), so an assembly says which size it means; PartCAD gates
compatibility by interface rather than by dimension, so it cannot reject a
mismatched pair for you.

The **Mindstorms** parts are the only ones whose ports are read from geometry
instead of from a name — see "Reading ports from geometry" in the package
`README.md`. Mechanically they are pure Technic (the EV3 brick has 16 pin
holes); the RJ12 socket is the one connection that is theirs alone.

## The assemblies

All are marked `manufacturable: false` (they are demonstrations).

| Assembly | What it builds |
| --- | --- |
| `stack` | two 2×4 bricks (3001), aligned — a column two brick-heights tall |
| `staircase` | four 1×4 bricks (3010), each offset two studs and raised one brick |
| `mixed` | an arch: a 2×4 base, two 2×2 pillars, and a 2×4 brick bridging both |
| `pyramid` | a 2×4 base, two 2×2 bricks, a 2×4 plate deck, and a 2×2 tile cap |
| `technic` | a pin holding a beam on a Technic brick, and an axle through an axle-hole brick that sits on that brick's studs |
| `gears` | an 8-tooth and a 16-tooth gear meshed onto a 24-tooth one, placed by naming teeth and gaps |
| `minifig` | hips, torso and head stacked on the minifig's own joints, with the head turned 45° |

## How to view

Run `pc` from the **repository root** (so `//pub/universe/lego` is the root and
the LDraw parts resolve) and address these assemblies as `lego-demo:<name>`:

```shell
pc inspect -a lego-demo:stack
pc inspect -a lego-demo:staircase
pc inspect -a lego-demo:mixed
pc inspect -a lego-demo:pyramid
pc inspect -a lego-demo:technic
pc inspect -a lego-demo:gears
pc inspect -a lego-demo:minifig

# or render one to a PNG:
mkdir -p /tmp/pc-render
pc render -a -t png -O /tmp/pc-render lego-demo:pyramid
```

`pyramid` uses the LDraw **Plate** and **Tile** categories, `technic` and
`gears` the **Technic** one and `minifig` the **Minifig** one, whose full
catalogs the plugin enumerates and caches on first use, so their very first
build is slow; the other three use only the already-common **Brick** category.

## How the interfaces are attached

See `ldraw_repo.py` in the package root (`_lego_implements`): every position is
computed analytically from the part description, so it is cheap even when a whole
category is enumerated. The stud grid covers every rectangular Brick / Plate /
Tile — including the renamed classics (e.g. `3068` → `3068b`), whose `~Moved to`
stubs are followed to the real part's name — and the Technic rules cover the
families whose name pins their geometry down exactly (`Technic Brick 1 x N with
Holes`, `Technic Beam N`, `Technic Axle N`, `Technic Pin`, and the axle-hole
bricks). The same holds for the gear, minifig, Duplo and wheel rules beside
them. A part whose name says more than that — a bent beam, an axle with a stop,
a sculpted character head — is left alone rather than guessed at.

The Mindstorms parts are the exception: their names carry no dimensions at all,
so their ports are read from their geometry by a bounded, parallel walk that
never fetches a primitive. The package `README.md` has the measurements.
