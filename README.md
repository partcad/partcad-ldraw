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
Headgear used to be left out for the same reason — no name rule gets past ~96%
of the 371 parts — and is now served by name *and* geometry together: the name
picks the family, and the socket has to be there in the geometry for a port to
be emitted. LDraw draws it as a single open tube, flipped, whose far end is the
part's own origin, which is the opposite reading of the same primitive from a
brick's underside. 284 of the 371 are confirmed that way; the other 87, whose
geometry shows no single socket, are still left alone.

`Technic Pin 3/4` came back for the same reason. Its name does not say which end
is the short one, but 32002 places `connect` (Technic Pin 1.0) toward -X and
`connect3` (Technic Pin 0.5) toward +X, so the instances can be named for it.

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
| studs unchanged | 10,600 |
| studs **gained** — the name rule gave none | **3,466** |
| studs corrected — both had some, in different places | 540 |
| studs removed — the part has none | 15 |
| walk ran out of budget, name rule left to stand | 0 |

The 15 removals are all parts whose name says as much: "Brick 2 x 2 **no Studs**
with Pin Vertical", "Brick 2 x 4 with **Curved Top**", "Plate 1 x 1 with **Swirl
on Top**". A rectangular brick comes out byte-identical to what the name gave
it, instance names included, so an assembly that names `c0r0` keeps working.

It costs 0.30 extra distinct fetches per part over the `Brick`/`Plate`/`Tile`
families and 1.09 over the whole library, because the subparts and primitives
beneath them are shared and cached.

### What the walk's budget is for

`_GEOMETRY_DEPTH` and `_GEOMETRY_FILES` bound one part's walk so a cycle cannot
run away. They are a ceiling rather than a spend: a part whose walk finishes in
ten files reads ten of them whatever the ceiling is, so raising it costs
anything only on the parts that were being cut off. That is why they are set
high enough that nothing in the library is cut off at all — 8 and 1024, where
the original 4 and 64 left 198 parts unfinished. Over the whole library that
costs 45,500 reads instead of 45,022, 1.1% more, and 27 more distinct files.

Depth on its own buys almost nothing — 6 / 64 recovers 11 of the 198 — so it is
the file count that binds, and 12 / 4096 measures identical to 8 / 1024, which
is what says nothing is running away further out. There is no per-family table
and there does not need to be one.

The "walk did not finish, so the name rule stands" fallback stays even though no
part in the library reaches it today. A part added tomorrow could be deeper, and
losing its studs quietly is the failure worth keeping a guard against.

### Where the anti-studs come from

The same walk, and the same reasoning, with one twist: LDraw's tubes do not sit
where the anti-studs are. `Stud Tube Open` sits at the centre of a 2 x 2 of them
and `Stud Tube Solid` between two, so the anti-studs are the corners *around* a
tube rather than the tube itself, on the plane the tube's far end reaches — 24
LDU down for a brick, 8 for a plate. Which two a solid tube separates is not in
its matrix, because LDraw places every one of them the same way up, so the
part's own studs say which axis and a tube whose neighbours are not both on that
lattice is not guessed at.

Over the library that leaves 12,705 parts unchanged, corrects 132, adds an
underside to 1,784 that had none, and — the property that matters — takes one
away from nobody. The corner brick gets three anti-studs under its three studs
where the name put four in a square.

Where the tubes do not settle it, the name still stands. That is deliberate and
not the same rule as for studs: the walk can *see* that a part has no studs, but
an anti-stud that no tube happens to mark may still be there.

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

### The Technic connectors come from the geometry too

Every part's peg holes, axle holes, pins and axles are read the same way. The
name rules that used to be the only source reached **80** parts; the geometry
reaches every part that draws a connector, and adds **1,520** of them — 9,931
port instances in all. Not one part loses a port it had, and every one of the 80
comes out byte-identical, instance names included, so an assembly that says
`left` or `h0-top` keeps working.

That took the vocabulary being read rather than guessed at, because a Technic
feature is not one primitive the way a stud is:

| primitive | is | ports |
| --- | --- | ---: |
| `peghole` … `peghole6` | a mouth cap on the surface | 1 |
| `beamhole`, `connhole`, `connhol2` | a hole right through | 2 |
| `connect*`, `confric*` | one end of a pin | 1 |
| `axle` | a shaft, stretched by its matrix | 2 |
| `confricrib*`, `connectcollar*`, `connectslit*` | pieces of a pin | 0 |
| `confric8`, `confric9` | the *middle* of a long pin | 0 |

The last two rows are the trap. `6558`, "Technic Pin Long with Friction and
Slot", places a "Middle Slotted" section at the same spot as its left end, and
counting it gives the pin a third port on top of the two it has.

Two conventions had to be matched rather than invented. A pin's port sits at the
primitive's own origin, not on the collar face 2 LDU along it — `43093` puts its
collar disc at the origin, and moving the port would shift every pin joint by
0.8 mm. And an axle's two end ports face *inward*, at each other, because an
axle is pushed into a hole; there is a test named for it.

`connhol3` ("Connector Hole One-Sided", 355 uses) and `axlehol8` ("Axle
Perimeter", 380 uses) are deliberately left out: which end of the first is a
mouth is not settled, and whether the second appears once per axle is not
established. Leaving them out costs coverage on the parts that use only those;
guessing would put ports in the wrong place, which is worse.

Those parts carry axle holes as well as peg holes, and an axle hole is not one
primitive. LDraw draws it as a profile that its placement matrix stretches
through the part — the two mouths are the two ends of that stretch — and most of
the family are *faces* of one hole rather than one each, with "Side Edges" and
"Tooth Surface" appearing four to a hole. Two spellings appear once per hole:
the whole forms, and the "Perimeter" face, which some parts use instead of a
whole form. `32064b`, "Technic Brick 1 x 2 with Reduced Axlehole", draws its hole
out of faces alone and a whole-form-only reading misses it entirely. Over the
library 574 parts carry only whole forms, 128 only perimeters, and the 13 with
both never put the two in the same place, so taking either as a hole never
counts one twice.

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
