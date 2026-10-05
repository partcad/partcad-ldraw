# LDraw for PartCAD

Exposes the [LDraw parts library](https://library.ldraw.org) as PartCAD parts,
published as `//pub/universe/lego/ldraw`. Every LDraw category becomes a
sub-package, and every part in it is a parametric `:ldraw` part that meshes the
LDraw `.dat` on demand.

**No geometry is vendored.** Every `.dat` is fetched from ldraw.org on demand
and **cached on disk** under `~/.cache/partcad-ldraw/`.

What *does* ship with the package is an index of names: `parts-index.zip`,
which says which categories exist, which parts are in each, what every part is
called (with its author and licence) and what it connects with. It is 20,569
parts in about 1.1 MiB, built by
[`build_parts_index.py`](build_parts_index.py) and committed.

That index is what makes the package usable at all. PartCAD asks for a whole
category in order to resolve any single part in it, and answering that from the
network meant one HTTP request per part — 1324 of them for `Brick` — which
ldraw.org rate-limits long before it finishes. Rendering a single brick took
over twenty minutes on a cold cache, when it finished at all. Now it is a file
read.

It is a **zip with one member per category**, and not one compressed document,
because of how PartCAD asks. Every key is a separate run of the plugin — a
separate interpreter, keeping nothing from the last one — and listing the
library is hundreds of them: the metadata, the child list and one enumeration
per object kind, for each of the 92 categories. So whatever a key costs to read
is paid hundreds of times over. As one gzipped document that was 11.8 MB of
JSON to parse before any key could be answered, 1.1 s of it, nine tenths of it
the connection points of parts the key was not about. Split per category it is
1.4 ms for a key that names no category and about 20 ms for one that does.

## How it works

Two mechanisms are combined:

1. **An external repository plugin** (`ldraw_repo.py`). It serves the package
   contents over PartCAD's key/value repository protocol:
   - the categories as top-level sub-packages;
   - within each category, the **complete** list of parts, each with its
     **description, author and license**.
   All of that is read from the shipped index. A part the index does not have —
   an unofficial one, or one added to the library since the index was built —
   still resolves: the plugin falls back to fetching its `.dat` header, cached
   under `~/.cache/partcad-ldraw/` as before. Set `PARTCAD_LDRAW_IGNORE_INDEX=1`
   to bypass the index entirely and go to the network, which is how to check one
   against the other.

   Each package's metadata also declares `objectKinds` — that a category holds
   parts and a partType and none of PartCAD's other eight kinds of object — so
   that PartCAD stops asking after the ones that have never been there. That is
   four of the six keys a listing asks per category. A PartCAD too old to read
   it simply asks as it always did.

   Between the two — and PartCAD no longer loading the CAD kernel to serialize
   an answer that is a dict of strings — `pc list packages -r` over the whole
   library went from 7m51s to 15s on a four-core machine, and from 558 runs of
   this plugin to 278.

2. **A `wrapper` partType** (`ldraw.py`). Each part's `type` is `:ldraw`, which
   resolves to this partType. The wrapper fetches the part's `.dat`, recursively
   resolves its sub-parts, meshes the triangles/quads, and returns the shape.

   LDraw draws a part for a renderer, not for a kernel, so the surface it
   describes is not closed: a stud is a cylinder standing on a face nobody cut,
   the same corner is written to four decimals down two different reference
   paths, and a 16-sided wall meets a 48-sided floor. The wrapper closes that
   surface before it builds the shape — welding the near-coincident vertices,
   splitting an edge where another vertex sits on it, and filling each flat
   hole as the region its boundary loops enclose — and then makes one solid out
   of the closed shells, cutting the ones LDraw drew facing inward, which are
   its cavities, rather than fusing them.

   A part the wrapper cannot close is refused, with the reason, rather than
   handed back as a shell: a shell renders like the part and makes every
   boolean taken against it meaningless. Where the reason is a defect in one
   particular LDraw file, the file is mended as it is read, by a patch from the
   maintained list in `patches/` (see [Patches](#patches)).

## Patches

Most of what keeps an LDraw surface from closing is general, and is mended by a
rule that holds for every part. What is left is particular to one file: a face
its author left out, or two faces that were meant to meet and miss by a tenth of
an LDraw unit. No general rule can tell that from a part that really is open
there - a rule that guessed would guess wrong somewhere else - so those are
mended one file at a time, by a maintained list of patches applied as the file
is read. The library's file itself, and the copy in the cache, are left as they
are.

A patch is justified when, and only when:

- the part is refused (or wrong) because of a defect **in that LDraw file**,
  traced to its lines;
- the file is evidence for what was meant - the corner the next face starts
  from, the edge lines (`2`) it draws, the primitive that stops a step short of
  the one beside it - so the patch adds or corrects what the author evidently
  intended, and invents nothing;
- a general rule cannot or should not do it, because it would have to guess;
- the part then builds a solid whose volume agrees with an independent estimate
  of what its surface encloses, and whose extent is the LDraw geometry's.

A file whose header says it is unfinished (`Needs Work: Inner side not
modelled`) is not patched: there is nothing in it to say what the missing side
is. Prefer patching the part's own file or its own subpart over a primitive or
a subpart other parts share, and patch a shared one only if the change is right
for every part that uses it.

### Format

`patches/manifest.json` lists the patches, one entry per LDraw file:

```json
{
  "file": "s/919s01.dat",
  "sha256": "40fcc09f...",
  "patch": "s/919s01.dat.patch",
  "parts": ["58119"],
  "reason": ["What LDraw gets wrong, with the lines and primitives that show it,", "and what the patch changes."]
}
```

- `file` is the file as a part refers to it, lowercase with `/`: `58121.dat`,
  `s/919s01.dat`, `48/1-4disc.dat`.
- `sha256` pins the exact upstream text the patch was written against (the
  hash of its lines, so CRLF and LF line ends are the same file).
- `patch` is the patch file under `patches/`.
- `parts` are the library parts whose geometry reads the file, which are the
  parts whose cache key the patch goes into (see below).
- `reason` is for the reviewer: the defect, the evidence for it, and the change.

The patch file is LDraw lines under directives naming the upstream file's lines,
counted from 1 as an editor counts them:

```
0 // Notes for the reader, before the first directive; not copied.
0 !PATCH REPLACE 43
4 16 3.44415 -48.31492 20 3.56 -47.5 20 3.56 -47.5 8 3.44415 -48.31492 8
0 !PATCH AFTER 21
4 16 6.5 -8 34 6.5 -8 86 5 -8 86 5 -8 34
0 !PATCH DELETE 105
0 !PATCH ADD
1 16 0 0 0 1 0 0 0 1 0 0 0 1 4-4disc.dat
```

`AFTER n` inserts after line `n` (`AFTER 0` before the first), `REPLACE n` puts
the lines that follow in place of line `n`, `DELETE n` removes it, and `ADD`
appends to the end. Every number refers to the file as written, whatever an
earlier directive did, and everything after a directive is copied verbatim,
`0 BFC` statements included. Keep a patch to the lines it needs; where the file
evidently meant a primitive (a `4-4disc.dat` closing a `4-4cyli.dat`), add the
primitive rather than its triangles.

### Pinning

A patch is applied only to the text it was written against. When the library
changes the file, the hash no longer matches, and the patch is **not** applied:
the run says so on stderr and builds the file as the library has it, which may
mean the part is refused again until the patch is rewritten (or found to be no
longer needed). A patch that does not make sense for the text - a line it names
that is not there - is skipped the same way. Neither ever fails a build.

### Adding one

1. Trace the defect to its file and lines, and write the smallest patch that
   mends it.
2. Add the manifest entry, with the hash (`ldraw._content_hash(text)` of the
   upstream file) and the reason.
3. Fill in `parts`: `./build_parts_index.py --patch-users LIBRARY` reads every
   patch's file and writes the parts that reach it, from an unpacked library.
4. Check that the part builds, and how its volume and extent compare with an
   independent estimate; run the tests, which check the manifest (and, with
   `LDRAW_LIBRARY` set to an unpacked library, each patch against its file).
5. Raise `CACHE_VERSION` in `ldraw_repo.py` and `cacheVersion` in
   `partcad.yaml`.

### How a patch reaches a build, and the cache

`ldraw.py` reads the `patches/` directory beside it, which is what a checkout
and the tests have. PartCAD runs the wrapper elsewhere: it writes the file the
repository serves under `files/ldraw.py` into a directory of its own, with
nothing beside it. So the repository serves it with the patch list written into
it, in place of its `_EMBEDDED_PATCHES = None` line, and the copy that runs
carries every patch.

PartCAD keys a built part on its configuration, so a part built from a patched
file carries a `patches` parameter, a hash of every patch it reads: change a
patch and exactly the parts that read it are built again. A changed patch is
served only once the cache version is raised, since the repository's answers
and the wrapper are cached under it; raising it alone does not rebuild a part
whose configuration is unchanged.

### The list

| File | Parts | What it mends |
| --- | --- | --- |
| (none yet) | | |

## Which way is up

LDraw's up is `-Y`; PartCAD's is `+Z`. The wrapper reconciles them, so a part
served from here stands on the XY plane like every other PartCAD part, and the
stud grid is XY rather than XZ. In full, a point at LDraw `(x, y, z)` comes out
at `(x, -z, -y) * 0.4` millimetres.

Ports make the same turn, in `_port()` — a port is a position *and* a roll, and
both are turned, so a port goes on landing on the geometry it names. Nothing
about a *connection* changes: studs still mate anti-studs, and an assembly built
out of `connect:` is untouched by this.

What does change is an assembly that placed an LDraw part by hand. A `location:`
written to stand a part upright — a quarter turn about X — is now exactly that
turn too many, and should be deleted. Any other `location:` that *rotates* wants
re-deriving, because it composes with a part that starts out somewhere new. One
that only translates still means what it meant.

## Rebuilding the index

Run it when LDraw publishes a library update, and commit the result:

```sh
./build_parts_index.py                      # downloads complete.zip
./build_parts_index.py --archive complete.zip
```

A change to how connectors are *read* does not need all of that. `--refresh`
recomputes only the entries of the parts whose own files place one of the named
primitives, from any unpacked copy of the library, and takes a new entry only
where it keeps every port the old one had, instance names included; a part that
would lose or move a port is reported and left as it was, because that is a
change to the rules and belongs to a full rebuild. Nothing else in the index is
touched, so the listings and the archive are not needed:

```sh
./build_parts_index.py --refresh path/to/ldraw --reaching bush0.dat bush.dat
```

That is how the bush's axle hole (below) went in.

It takes names, authors and licences from
[`complete.zip`](https://library.ldraw.org/library/updates/complete.zip) — one
download carrying the whole official library, instead of ~25,000 per-part
requests — and takes which parts are in which category from ldraw.org's own
list pages, because that is what the package paths have always been built from
and the archive does not reproduce it. The archive holds 24,735 parts in
`parts/`; the site lists 20,569 of them. The difference is largely the 4,538
whose description carries a `~`, `=` or `_` marker — moved-to stubs, aliases
and colour variants — but not exactly, since some aliases *are* listed. Rather
than guess at that filter, the build reads the listings: ~1,000 requests at 25
rows a page, cached on disk between runs.

Twelve more categories appear in the site's filter dropdown (`Quatro`,
`Minifig Arm`, `Mursten` and nine others) but list no parts at all. They used to
become empty sub-packages; now they are simply absent.

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
and torso turn on their joints. An axle is also the one connector that carries
several parts on one end - the beams it runs through, bushes, gears and a wheel,
each at its own `moveZ` - so `technic-axle` says `multiConnect: true`, and
PartCAD's connectivity test does not report those parts as crowding one port. A pin
in a round hole is a snap fit - its slotted end is squeezed past the lip and
springs open behind it - so that mating says `snapIn: true`, and PartCAD's
interference test takes the ridge inside the lip for the joint it is. An axle
snaps past nothing, in a round hole or a cross one, so neither of its matings
says so: an axle that overlaps the part it goes through is reported.

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

A tube is also a tube. An open one's bore has 6 LDU of radius, which is the
stud's own, and LDraw says so itself in the help text of the two variants drawn
without their outer cylinder — `stud4o` and `stud4od` call the primitive
*a "antistud" to be used like a underside stud*. For a cone that bore is an
anti-stud in its own right, named `centre` so that the four round it keep the
grid names they have always had. A cone is where it matters, because a cone
narrows going up: a `Cone 2 x 2 x 2` has four anti-studs at the corners of its
base and one stud in the middle of its top, so without the bore two of them
cannot be joined by an interface at all — the four cells under the upper one and
the single stud on the lower one's top never coincide. Fourteen entries in the
index gain one, and it takes an anti-stud away from none of them.

It is claimed only where the tube stands on the part's own axis and opens into
the plane the cone's *name* puts its base in, since the walk reads stud
primitives and never sees how far down a part goes. That is what keeps
`Cone 4 x 4 x 3 on Brick 2 x 2 Round` out of it: the tube there is the round
brick's, one course below the top rather than three, up inside the cone's own
hollow where no stud reaches. A brick's centre tube has the very same bore and
is left unsaid, because a brick's top repeats its base and the bore would only
ever be a join half a stud out of step in both directions.

Which of the two an open tube is, is the base's to say. It is the spacer between
four cells under a part whose footprint really is 2 x 2 or bigger, and it is the
socket itself under a part one stud across, and no name tells the two apart.
`Plate 1 x 1 Round` stands on a ring 16 LDU across, and so does `Brick 1 x 1
Round with Hollow Stud`, and so does `Cone 2 x 2 x 2 Inverted` — whose 2 x 2 is
the rim it is inverted from — while the four cells the walk reads round the tube
are centred 14.1 LDU out from the axis, over nothing at all. A name gives a
part's bounding footprint and not which of its cells are solid: `Rock 4 x 4 x
0.667 Octagonal Bottom` stands on that same single ring, under four studs' worth
of rock.

So the base is measured: the plane a part stands on and how far it reaches from
its own axis there, out of the lines and faces its files draw themselves and the
size each primitive's name gives — `4-4cyli` is the unit cylinder, `4-4ring3`
runs from radius 3 to 4, `box3u2p` is inside the unit cube, a stud is 8 LDU
across and 4 along. A part whose base reaches over none of those four cells has
not got them, and what it has is the bore, on its own: the socket it is stacked
by, and the only join it has.

Sixty-eight entries change, four anti-studs down to one apiece, and every one of
them is a part that could not be stacked on itself before — the stud each offers
is in the middle of its top, where its own socket now is. Sixteen keep the name
`c0r0`, because the name rule had already given them one anti-stud in the middle
and the walk had been overruling it with four; the rest are `centre`, as the bore
is on a cone that has both.

Each of those bounds is an upper bound, and a primitive no bound reaches leaves
the base unmeasured rather than guessed at, so the measurement's own error is
always to find a part wider than it is — which leaves that part's anti-studs
exactly as the walk read them. A tube that opens anywhere but the plane the part
stands on settles nothing either.

The bore is still left unsaid under a brick two studs across or more, because a
brick's top repeats its base and that join would be half a stud out of step in
both directions. One stud across it is not out of step with anything: it is dead
under the part's own stud, which is where the part below puts its own.


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

One axle hole is drawn inside a primitive rather than by the part, and the walk
never opens a primitive: `bush0`, "Technic Bush without Collars", is an
`axlehol5` stretched through it, and `bush` is the same bush with its collars.
Between them they are the axle hole of every Technic cross block and of the
bushes themselves, so the cross blocks used to be served with their pin holes
and not the axle hole they are for — `6536`, "Cross Block 1 x 2 (Axle/Pin)", had
a pin hole beside nothing. Both are named in the vocabulary with the hole spelled
out, which gives 47 parts an axle hole each and changes no port anything had.

`lego-demo/` builds seven assemblies out of all this, and its `README.md`
describes the interfaces in detail.

## Layout

| Path | Purpose |
| --- | --- |
| `partcad.yaml` | `//pub/universe/lego`; the `ldraw` external dependency (the library), the `ldraw_repo` repository, the `ldraw` partType, and the LEGO interfaces. |
| `ldraw_repo.py` | Repository plugin: categories, paginated part lists, `.dat`-header metadata, the interfaces each part implements, the partType, and the wrapper file. |
| `ldraw.py` | The `:ldraw` partType wrapper: fetch + recursively mesh a `.dat`. |
| `patches/` | The maintained list of patches to LDraw files (see [Patches](#patches)). |
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
category-list.html                 the category list          (ldraw_repo.py)
categories/<Category>/page-N.html  each part-list page        (ldraw_repo.py)
parts/<id>.dat                     a part's header            (ldraw_repo.py)
<name>.dat, 48/<name>.dat, ...     each file the render reads (ldraw.py)
.missing/<same path>               a fetch that produced nothing
```

A fetch that produced nothing is cached too, and that matters as much as the
rest: ldraw.org rate-limits bursts, and meshing one part reads hundreds of
files. Without a record of what did not arrive, every part re-asks for every
primitive the part before it could not get, so a throttled machine never
converges - it repeats the burst that got it throttled, at up to sixteen
requests per file.

The two cases are not the same question, so they are not kept for the same
length of time. A **404** is an answer - the library has not got this file - and
is held for a week. A **429, a timeout or a reset** is the absence of an answer,
and is held for five minutes so the next run is a fresh attempt rather than this
one again. Telling them apart is also what decides what the render says when a
part will not come: "not found in the library" means the id is wrong, and "could
not be fetched" means try again.

Delete the cache directory to start over; deleting just `.missing/` retries
everything that failed without re-fetching what did not.

## Requirements

Requires a PartCAD version that provides plugin-backed (`external`) packages and
`partTypes` (`partcad: ">=0.7.146"` in `partcad.yaml`). Because parts are fetched
on demand, first use of a part or category needs network access; afterwards the
cache is used.

## Licensing

The code here is Apache-2.0 (`LICENSE`). LDraw parts are **not** bundled; they
are fetched from ldraw.org and remain under their own per-part licenses
(CC BY 4.0 / CCAL), recorded in each part's `.dat` header. See `NOTICE`.
