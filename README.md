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
| `stud` / `anti-stud` | every rectangular `Brick`, `Plate` and `Tile`, and the Technic bricks below | stud, on the top and the bottom plane |
| `technic-pin-hole` | `Technic Brick 1 x N with Hole(s)`, `Technic Beam N` | mouth of each round hole |
| `technic-axle-hole` | `Technic Brick 1 x N with ... Axlehole` | mouth of the cross hole |
| `technic-pin` | `Technic Pin`, `Technic Pin Long`, `Technic Pin 1/2`, and their friction variants | end of the pin |
| `technic-axle` | `Technic Axle N` | end of the shaft |

Every position is derived analytically from the part's name — no geometry is
fetched — so attaching them costs nothing even when a whole category is
enumerated. What a connection leaves free is declared with it: a pin turns in a
round hole (`turnZ`), and an axle slides through one (`moveZ`).

Coverage is deliberately narrow. A name that says more than the rule knows — a
bent beam, an axle with a stop, a brick with an open centre — is left alone
rather than guessed at, because its features are not where the plain name would
put them.

`lego-demo/` builds five assemblies out of this, and its `README.md` describes
the interfaces in detail.

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
