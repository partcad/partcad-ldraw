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

## The assemblies

All are marked `manufacturable: false` (they are demonstrations).

| Assembly | What it builds |
| --- | --- |
| `stack` | two 2×4 bricks (3001), aligned — a column two brick-heights tall |
| `staircase` | four 1×4 bricks (3010), each offset two studs and raised one brick |
| `mixed` | an arch: a 2×4 base, two 2×2 pillars, and a 2×4 brick bridging both |
| `pyramid` | a 2×4 base, two 2×2 bricks, a 2×4 plate deck, and a 2×2 tile cap |
| `technic` | a pin holding a beam on a Technic brick, and an axle through an axle-hole brick that sits on that brick's studs |

## How to view

Run `pc` from the **repository root** (so `//pub/universe/lego` is the root and
the LDraw parts resolve) and address these assemblies as `lego-demo:<name>`:

```shell
pc inspect -a lego-demo:stack
pc inspect -a lego-demo:staircase
pc inspect -a lego-demo:mixed
pc inspect -a lego-demo:pyramid
pc inspect -a lego-demo:technic

# or render one to a PNG:
mkdir -p /tmp/pc-render
pc render -a -t png -O /tmp/pc-render lego-demo:pyramid
```

`pyramid` uses the LDraw **Plate** and **Tile** categories and `technic` the
**Technic** one, whose full catalogs the plugin enumerates and caches on first
use, so their very first build is slow; the other three use only the
already-common **Brick** category.

## How the interfaces are attached

See `ldraw_repo.py` in the package root (`_lego_implements`): every position is
computed analytically from the part description, so it is cheap even when a whole
category is enumerated. The stud grid covers every rectangular Brick / Plate /
Tile — including the renamed classics (e.g. `3068` → `3068b`), whose `~Moved to`
stubs are followed to the real part's name — and the Technic rules cover the
families whose name pins their geometry down exactly (`Technic Brick 1 x N with
Holes`, `Technic Beam N`, `Technic Axle N`, `Technic Pin`, and the axle-hole
bricks). A part whose name says more than that — a bent beam, an axle with a
stop — is left alone rather than guessed at.
