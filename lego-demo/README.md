# Building LEGO assemblies with PartCAD

A small gallery of assemblies that snap common LEGO bricks together using the
PartCAD **ASSY file format** and **interfaces** — no coordinates by hand.

The LDraw parts served by `//pub/universe/lego` already carry the LEGO **stud**
(male) and **anti-stud** (female) interfaces: the `ldraw_repo` plugin attaches
them to every rectangular Brick, Plate and Tile it serves, one instance per
stud, at coordinates derived from the part's `<type> A x B` name. So an assembly
just references the original LDraw parts and joins them with `connect:` — PartCAD
places each part so its anti-stud mates the stud below it.

## The interfaces

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

## The assemblies

All are marked `manufacturable: false` (they are demonstrations).

| Assembly | What it builds |
| --- | --- |
| `stack` | two 2×4 bricks (3001), aligned — a column two brick-heights tall |
| `staircase` | four 1×4 bricks (3010), each offset two studs and raised one brick |
| `mixed` | an arch: a 2×4 base, two 2×2 pillars, and a 2×4 brick bridging both |
| `pyramid` | a 2×4 base, two 2×2 bricks, a 2×4 plate deck, and a 2×2 tile cap |

## How to view

Run `pc` from the **repository root** (so `//pub/universe/lego` is the root and
the LDraw parts resolve) and address these assemblies as `lego-demo:<name>`:

```shell
pc inspect -a lego-demo:stack
pc inspect -a lego-demo:staircase
pc inspect -a lego-demo:mixed
pc inspect -a lego-demo:pyramid

# or render one to a PNG:
mkdir -p /tmp/pc-render
pc render -a -t png -O /tmp/pc-render lego-demo:pyramid
```

`pyramid` uses the LDraw **Plate** and **Tile** categories, whose full catalogs
the plugin enumerates and caches on first use, so its very first build is slow;
the other three use only the already-common **Brick** category.

## How the interfaces are attached

See `ldraw_repo.py` in the package root (`_lego_implements`): the stud grid is
computed analytically from the part description, so it is cheap even when a whole
category is enumerated, and it covers every rectangular Brick / Plate / Tile —
including the renamed classics (e.g. `3068` → `3068b`), whose `~Moved to` stubs
are followed to the real part's name.
