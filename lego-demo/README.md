# LEGO interfaces demo

A self-contained PartCAD test bench that enriches common LEGO bricks from the
LDraw package with **connection interfaces** (studs and anti-studs) and mates
them into assemblies purely by connection — no hand-placed coordinates in the
`.assy` files.

It was authored by following the `/pc:add-interfaces` skill: render each brick,
read the exact stud coordinates from the LDraw `.dat` source, design a
male/female interface pair, attach it to each brick with `implements:`, then
prove it by mating bricks in throwaway assemblies and viewing the renders.

## What it shows

The LDraw bricks are served dynamically by the `//pub/universe/lego` plugin, so
there is no static part config to edit. Each brick is therefore **enriched** in
this package (`type: enrich`, pointing at `//pub/universe/lego/ldraw/<Cat>:<id>`)
with an added `implements:` block. Because this package is a sub-package of
`//pub/universe/lego`, the upstream parts resolve directly.

### Interfaces

Two complementary interfaces (defined in `partcad.yaml`), following the PartCAD
port convention (male Z outward, female Z inward, opposite Z when mated):

| Interface | Role | Z axis | Placed at |
| --- | --- | --- | --- |
| `lego-stud` | male | +Y (up), out of the top | each top stud, on the top plane (y = 0) |
| `lego-anti-stud` | female | −Y (down), into the underside | each stud position, on the part's bottom plane (y = −9.6 mm for a brick, −3.2 mm for a plate/tile) |

Coordinates are in PartCAD meshed part-space millimeters — the LDraw package
meshes each `.dat` as `(x, −y, z) × 0.4`, so **+Y is up**. LEGO's 8 mm stud grid
becomes studs spaced 8 mm apart; a stud port uses rotation `270°` about `[1,0,0]`
(Z → +Y) and the matching anti-stud uses `120°` about `[1,1,−1]` (Z → −Y). With
that pairing the 180° mate flip cancels the rotation, so an upper brick is
translated by exactly `stud_position − anti_stud_position`: aligned studs give a
clean vertical stack, and choosing a different stud gives an 8 mm-grid offset.
The stacking height is carried by each part's own bottom-plane `y`, so a plate
(3.2 mm) seats correctly on a brick without any special-casing.

### Enriched parts (13)

| Part | LDraw id | Category |
| --- | --- | --- |
| `brick-1x1` | 3005 | Brick |
| `brick-1x2` | 3004 | Brick |
| `brick-1x3` | 3622 | Brick |
| `brick-1x4` | 3010 | Brick |
| `brick-1x6` | 3009 | Brick |
| `brick-1x8` | 3008 | Brick |
| `brick-2x2` | 3003 | Brick |
| `brick-2x3` | 3002 | Brick |
| `brick-2x4` | 3001 | Brick |
| `brick-2x10` | 3006 | Brick |
| `plate-1x2` | 3023 | Plate |
| `plate-2x4` | 3020 | Plate |
| `tile-2x2` | 3068 | Tile (smooth top: anti-studs only) |

### Assemblies

| Assembly | What it demonstrates |
| --- | --- |
| `stack` | two 2×4 bricks mated stud→anti-stud, perfectly aligned (column two brick-heights tall) |
| `staircase` | four 1×4 bricks, each offset two studs and raised one brick, chained by mating |
| `mixed` | different sizes joined into an arch: a 2×4 base, two 2×2 pillars, and a 2×4 brick bridging across both pillars |
| `pyramid` | bricks + plate + tile: a 2×4 base carries two 2×2 bricks, a 2×4 plate bridges across both, and a 2×2 tile caps it |

> Note: `pyramid` uses the LDraw **Plate** and **Tile** categories. The
> `//pub/universe/lego` plugin enumerates (and caches) a category's full part
> catalog the first time any part in it is used, so the very first build of
> `pyramid` is slow; `stack`, `staircase` and `mixed` use only the already-common
> **Brick** category.

## How to view

Run `pc` from the **repository root** (so `//pub/universe/lego` is the root and
the upstream parts resolve) and address these objects as `lego-demo:<name>`:

```shell
# Inspect an enriched part (its ports are visualized):
pc inspect lego-demo:brick-2x4

# Inspect a connected assembly interactively:
pc inspect -a lego-demo:stack
pc inspect -a lego-demo:staircase
pc inspect -a lego-demo:mixed

# Or render one to a PNG:
mkdir -p /tmp/pc-render
pc render -a -t png -O /tmp/pc-render lego-demo:mixed
```

The assemblies are marked `manufacturable: false` (they are demonstrations, not
catalog items), so `pc test -a lego-demo:<name>` passes as a build gate.
