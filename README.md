# LDraw for PartCAD

Exposes the [LDraw parts library](https://library.ldraw.org) as PartCAD parts,
published as `//pub/universe/lego/ldraw`. Every LDraw category becomes a
sub-package, and every part in it is a parametric `:ldraw` part that meshes the
LDraw `.dat` on demand.

Nothing is vendored: the category list, the per-category part lists (paginated
in full), the part metadata and the part geometry are all fetched from
ldraw.org and **cached on disk** on first use.

## How it works

Two mechanisms are combined:

1. **An external repository plugin** (`ldraw_repo.py`). It serves the package
   contents over PartCAD's key/value repository protocol:
   - the categories (from `parts/category-list`) as top-level sub-packages;
   - within each category, the **complete** list of parts (walking every page of
     `parts/list`), each with its **description, author and license** read from
     the part's `.dat` header.
   Category enumeration is lazy (per category) and every remote call — each list
   page and each `.dat` — is cached under `~/.cache/partcad-ldraw/`, consistent
   with how `partcad-bosl2` caches BOSL2.

2. **A `wrapper` partType** (`ldraw.py`). Each part's `type` is `:ldraw`, which
   resolves to this partType. The wrapper fetches the part's `.dat`, recursively
   resolves its sub-parts, meshes the triangles/quads, and returns the shape.

## Interfaces

The parts arrive knowing how they connect. The plugin attaches PartCAD
interfaces — declared in this package's `partcad.yaml` — to every part whose
LDraw name says exactly what it is, so an assembly can snap the original LDraw
parts together with `connect:` instead of placing them by hand:

| Interface | Attached to | One instance per |
| --- | --- | --- |
| `stud` / `anti-stud` | every rectangular `Brick`, `Plate` and `Tile`, the Technic bricks below, and the top of a minifig head | stud, on the top and the bottom plane |
| `technic-pin-hole` | `Technic Brick 1 x N with Hole(s)`, `Technic Beam N` | mouth of each round hole |
| `technic-axle-hole` | `Technic Brick 1 x N with ... Axlehole` | mouth of the cross hole |
| `technic-pin` | `Technic Pin`, `Technic Pin Long`, `Technic Pin 1/2`, and their friction variants | end of the pin |
| `technic-axle` | `Technic Axle N` | end of the shaft |
| `gear-tooth` / `gear-gap` | `Technic Gear N Tooth`, including the double-bevel and clutch variants | tooth, and each gap between two |
| `minifig-neck` / `-socket` | `Minifig Torso` / `Minifig Head` | the one joint |
| `minifig-waist` / `-socket` | `Minifig Hips` / `Minifig Torso` | the one joint |
| `duplo-stud` / `duplo-anti-stud` | `Duplo Brick A x B` | stud, on the 16 mm grid |
| `wheel-rim` / `tyre-bore` | `Wheel W x D` / `Tyre W/ A x D` | part, named for its fitting diameter |
| `rj12-plug` / `rj12-socket` | the Mindstorms cables, bricks, motors and sensors | plug, and each socket |

All but the last are derived analytically from the part's name — no geometry is
fetched — so attaching them costs nothing even when a whole category is
enumerated. What a connection leaves free is declared with it: a pin turns in a
round hole (`turnZ`), an axle slides through one (`moveZ`), and a minifig's head
and torso turn on their joints.

**Gears** are the interesting case: a mesh is a port pair once each tooth and
each gap is a port. LEGO gears are cut to one module, so a port on the pitch
circle sits half a millimetre per tooth from the centre, and bringing a tooth
port and a gap port together leaves the two gears the sum of their pitch radii
apart — the centre distance the pair is cut for — with their teeth lined up.

**Mindstorms** is the one family read from geometry rather than from its name,
because "Electric Mindstorms EV3 Large Motor" says nothing about where anything
is. See *Reading ports from geometry* below.

Coverage is deliberately narrow. A name that says more than the rule knows — a
bent beam, an axle with a stop, a brick with an open centre, a sculpted
character head, a Duplo brick with a curved top — is left alone rather than
guessed at, because its features are not where the plain name would put them.
Headgear is left out for the same reason: it would be an ordinary anti-stud at
the head's origin, but no name rule gets past ~96% of the 369 parts.

### Where the studs come from

`Brick | Plate | Tile A x B` says how big a part is, not where its studs are.
The two agree for a plain brick and part company everywhere else, so the studs
are read from the part's geometry instead — by the same walk that reads the
Mindstorms connectors below. Counting the geometry of all 2476 parts the name
rule matches, 216 of them (8.7%, and 39.5% of the Plates) were being given a
grid that is not theirs:

* `2357` "Brick 2 x 2 Corner" has three studs at `(0,0) (0,20) (20,0)`, where
  the rule put four at `(±10, ±10)` — a corner brick does not use the centred
  origin a rectangular one does, so every position was wrong, not just the
  count;
* `6177` "Plate 8 x 8 Round with 2 x 2 Centre Studs" was given 64 where it has
  4;
* `Brick 1 x 1 with Studs on Four Sides` has five studs facing five ways, which
  no `A x B` rule can express at all.

Filtering the names cannot fix that: a denylist of the shape words — `Corner`,
`Round`, `Bent`, `Curved`, `Wedge`, `Triangular`, `Octagonal`, `Headlight` —
removes 73 of the 216, leaves 143, and takes 372 *correct* grids with it.

Reading the geometry works because LDraw draws every stud with a primitive and
says in that primitive's own description what it is: `Stud`, `Stud Open`, `Stud
Tube Solid`, `Stud Group 2 x 2`, `Stud Duplo Open`. That is a closed vocabulary
LDraw maintains, so the plugin carries a transcription of it rather than a guess
about part names — and carries it as a table, so the walk still never fetches a
primitive. A group is expanded from its own name (`stug-1x4` is four studs along
X), and a group is named after what it groups, so `stug20-2x2` (Duplo) and
`stug19-1x2` (Scala) leave themselves out.

Over the whole library that is:

| | parts |
| --- | ---: |
| studs unchanged | 10,507 |
| studs **gained** — the name rule gave none | **3,362** |
| studs corrected — both had some, in different places | 539 |
| studs removed — the part has none | 15 |
| walk ran out of budget, name rule left to stand | 198 |

The 15 removals are all parts whose name says as much: "Brick 2 x 2 **no Studs**
with Pin Vertical", "Brick 2 x 4 with **Curved Top**", "Plate 1 x 1 with **Swirl
on Top**". A rectangular brick comes out byte-identical to what the name gave
it, instance names included, so an assembly that names `c0r0` keeps working.

It costs 0.30 extra distinct fetches per part over the `Brick`/`Plate`/`Tile`
families and 1.09 over the whole library, because the subparts and primitives
beneath them are shared and cached.

**The anti-studs still come from the name.** The same walk reads the underside
tubes for free, but a tube sits *between* four studs rather than under one, so
turning them into anti-stud ports is its own piece of work.

## Reading ports from geometry

The Mindstorms parts have no dimensions in their names and put no connector in
their own `.dat` — every one is inside a subpart, one to four levels down. So
for these, and only these, the ports are read from the geometry.

That is affordable because of one observation: **a connector is a primitive, and
a primitive is identified by the reference line that names it**. Primitives are
therefore leaves and are never fetched — only part files and `s\` subparts are.
Each file lists its own references, so the whole of the next level is known as
soon as the current one is parsed, and is fetched in parallel. Measured against
the LDraw library, that finds exactly the same connectors as an exhaustive walk:

| | files fetched, exhaustive | pruned | connectors found |
| --- | ---: | ---: | --- |
| EV3 brick (95646) | 86 | **27** | identical |
| NXT motor (53787) | 70 | **14** | identical |
| EV3 medium motor (99455) | 57 | **8** | identical |
| an ordinary brick (3001) | 10 | **2** | identical |

`lego-demo/` builds seven assemblies out of all this, and its `README.md`
describes the interfaces in detail.

## Layout

| Path | Purpose |
| --- | --- |
| `partcad.yaml` | `//pub/universe/lego`; the `ldraw` external dependency (the library), the `ldraw_repo` repository, the `ldraw` partType, and the LEGO interfaces. |
| `ldraw_repo.py` | Repository plugin: categories, paginated part lists, `.dat`-header metadata, the interfaces each part implements, the partType, and the wrapper file. |
| `ldraw.py` | The `:ldraw` partType wrapper: fetch + recursively mesh a `.dat`. |
| `lego-demo/` | Assemblies built purely out of those interfaces. |

## Usage

```shell
# Categories (top-level sub-packages)
pc list packages //pub/universe/lego/ldraw

# The complete part list of a category (first use fetches + caches it)
pc list parts //pub/universe/lego/ldraw/Brick

# Render any LDraw part to an image
pc inspect //pub/universe/lego/ldraw/Brick:3001

# The interfaces a part implements, and the assemblies built out of them
pc info //pub/universe/lego/ldraw/Technic:3701
pc inspect -a lego-demo:technic
```

## Caching

Everything fetched from ldraw.org is cached under
`~/.cache/partcad-ldraw/` (override with `PARTCAD_LDRAW_CACHE`):

```
category-list.html                 the category list
categories/<Category>/page-N.html  each part-list page
parts/<id>.dat                      each part (also used by the render)
```

## Requirements

Requires a PartCAD version that provides plugin-backed (`external`) packages and
`partTypes` (`partcad: ">=0.7.146"` in `partcad.yaml`). Because parts are fetched
on demand, first use of a part or category needs network access; afterwards the
cache is used.

## Licensing

The code here is Apache-2.0 (`LICENSE`). LDraw parts are **not** bundled; they
are fetched from ldraw.org and remain under their own per-part licenses
(CC BY 4.0 / CCAL), recorded in each part's `.dat` header. See `NOTICE`.
