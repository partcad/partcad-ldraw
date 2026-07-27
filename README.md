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

## Layout

| Path | Purpose |
| --- | --- |
| `partcad.yaml` | `//pub/universe/lego`; the `ldraw` external dependency (the library), the `ldraw_repo` repository, and the `ldraw` partType. |
| `ldraw_repo.py` | Repository plugin: categories, paginated part lists, `.dat`-header metadata, the partType, and the wrapper file. |
| `ldraw.py` | The `:ldraw` partType wrapper: fetch + recursively mesh a `.dat`. |

## Usage

```shell
# Categories (top-level sub-packages)
pc list packages //pub/universe/lego/ldraw

# The complete part list of a category (first use fetches + caches it)
pc list parts //pub/universe/lego/ldraw/Brick

# Render any LDraw part to an image
pc inspect //pub/universe/lego/ldraw/Brick:3001
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
