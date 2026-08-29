#
# partcad-ldraw, 2026
#
# Licensed under Apache License, Version 2.0.
#
"""A PartCAD 'basic' repository plugin that serves the LDraw parts library.

It exposes the categories of https://library.ldraw.org as top-level
sub-packages of ``//pub/universe/lego/ldraw``, and, within each category, the
parts of that category (description, author and license taken from the LDraw
part header). Every part is a ``:ldraw`` partType part (see ``ldraw.py``), which
fetches and meshes the LDraw ``.dat`` on demand.

Nothing is vendored: the category list, the per-category part lists and the part
metadata are all fetched from ldraw.org and cached on disk on first use.

PartCAD runs this script once per data request (via ``runpy``); the answer is
returned in the global ``output`` as ``{"result": <value>}``. Keys are scoped by
sub-package, e.g. ``Brick/objects/part`` or ``Brick/files/ldraw.py``.
"""

import base64
import concurrent.futures
import os
import re
import time
import urllib.parse
import urllib.request

_BASE = "https://library.ldraw.org"
_CATEGORY_LIST_URL = _BASE + "/parts/category-list"
_PARTS_LIST_URL = _BASE + "/parts/list?tableFilters%5Bcategory%5D%5Bvalues%5D%5B0%5D="
_DAT_URL = _BASE + "/library/official/parts/"
_UA = "Mozilla/5.0 (PartCAD ldraw repository)"

# The ldraw.org parts list is paginated at a fixed 25 rows per page; enumerating
# a category walks every page. Metadata (description, author, license) is read
# from each part's .dat header, fetched concurrently. All of it - every list
# page and every .dat - is cached on disk, so the (possibly large) cost of a
# category is paid once. Enumeration is lazy per category (see PartCAD's
# ProjectExternalRepository), so importing the package does not trigger it.
_PER_PAGE = 25
_MAX_PAGES = 2000  # safety bound on pagination
# ldraw.org rate-limits bursts of concurrent requests, so keep the pool modest
# and retry with backoff; a dropped fetch would otherwise leave a part without
# its metadata.
_HEADER_WORKERS = 6
_HTTP_RETRIES = 4


# --- HTTP + cache -----------------------------------------------------------


def _cache_dir():
    base = os.environ.get("PARTCAD_LDRAW_CACHE")
    if not base:
        xdg = os.environ.get("XDG_CACHE_HOME") or os.path.join(os.path.expanduser("~"), ".cache")
        base = os.path.join(xdg, "partcad-ldraw")
    return base


def _http_get(url):
    for attempt in range(_HTTP_RETRIES):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": _UA})
            with urllib.request.urlopen(req, timeout=60) as resp:
                if resp.status == 200:
                    return resp.read().decode("latin-1")
        except Exception:
            pass
        time.sleep(0.5 * (attempt + 1))  # backoff between retries
    return None


def _cached(url, rel):
    """Fetch 'url' once, caching its body under '<cache>/<rel>'."""
    path = os.path.join(_cache_dir(), rel)
    if os.path.exists(path):
        with open(path, "r", encoding="latin-1") as f:
            return f.read()
    body = _http_get(url)
    if body is None:
        return None
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="latin-1") as f:
        f.write(body)
    return body


# --- LDraw catalog ----------------------------------------------------------


def _sanitize(category):
    """Category name -> a PartCAD sub-package name (no spaces)."""
    return re.sub(r"\s+", "-", category.strip())


def _categories():
    """Return {sub_package_name: ldraw_category} for every LDraw category."""
    html = _cached(_CATEGORY_LIST_URL, "category-list.html")
    if not html:
        return {}
    names = set(re.findall(r"tableFilters%5Bcategory%5D%5Bvalues%5D%5B0%5D=([A-Za-z0-9_%.\-]+)", html))
    cats = {}
    for enc in names:
        cat = urllib.parse.unquote(enc)
        cats[_sanitize(cat)] = cat
    return cats


def _part_ids(category):
    """Every part id (without '.dat') in a category, across all list pages.

    Each page is fetched at most once and cached on disk. Pagination stops when
    the reported total is reached, when a page yields no new ids, or at the
    safety bound.
    """
    ids = []
    seen = set()
    sub = _sanitize(category)
    total = None
    page = 1
    while page <= _MAX_PAGES:
        url = "%s%s&page=%d" % (_PARTS_LIST_URL, urllib.parse.quote(category), page)
        html = _cached(url, os.path.join("categories", sub, "page-%d.html" % page))
        if not html:
            break
        if total is None:
            m = re.search(r"of ([\d,]+)", html)
            if m:
                total = int(m.group(1).replace(",", ""))
        added = 0
        for m in re.finditer(r"/library/official/parts/([0-9A-Za-z._-]+)\.dat", html):
            pid = m.group(1)
            if pid not in seen:
                seen.add(pid)
                ids.append(pid)
                added += 1
        if added == 0:
            break  # no new parts on this page: past the end
        if total is not None and len(ids) >= total:
            break
        page += 1
    return ids


def _parse_header(text):
    """Parse (description, author, license) from an LDraw .dat header (pure).

    The header is the leading run of '0' meta lines: the first plain '0' line is
    the description, '0 Author:' the author and '0 !LICENSE' the license.
    """
    desc = author = lic = None
    for line in text.splitlines():
        s = line.strip()
        if not s.startswith("0"):
            if s:
                break  # a geometry line: past the header
            continue
        body = s[1:].strip()
        if desc is None and body and not body.startswith("!") and body.split(":")[0] not in ("Name", "Author"):
            desc = body
        elif body.startswith("Author:"):
            author = body[len("Author:") :].strip()
        elif body.startswith("!LICENSE"):
            lic = body[len("!LICENSE") :].strip()
    return desc, author, lic


def _dat_header(pid):
    """Return (description, author, license) from a part's .dat header, or None.

    None means the part could not be fetched (so a targeted single fetch of an
    unknown id fails cleanly instead of inventing a broken part).
    """
    text = _cached(_DAT_URL + pid + ".dat", os.path.join("parts", pid + ".dat"))
    if not text:
        return None
    return _parse_header(text)


# --- LEGO interfaces: studs and Technic -------------------------------------
#
# Every part whose LDraw description says exactly what it is carries the LEGO
# connection interfaces declared in the root package (//pub/universe/lego:stud,
# :anti-stud, :technic-pin, :technic-pin-hole, :technic-axle, :technic-axle-hole),
# one instance per feature. Everything is derived analytically from the part's
# name - no geometry is fetched - so this stays cheap even when a whole category
# is enumerated.
#
# LEGO uses a 20 LDU (8 mm) grid; the wrapper meshes a .dat as (x, -y, z) * 0.4,
# so +Y is up, a brick's top plane is y = 0 and its body hangs below it, and a
# part is A studs deep (Z) by B studs long (X).
#
# A connection brings the two ports together at one point with their Z axes
# anti-parallel, so a port sits on the surface where the two parts touch, with
# its Z axis pointing out of its own part - the way its partner arrives from.
# A stud port is therefore on the top plane pointing up, and the anti-stud port
# of the part above it is on that part's own bottom plane pointing down; the two
# meet, and the parts end up exactly one part-height apart.
_STUD_MM = 8.0  # stud pitch in the meshed part-space (20 LDU * 0.4)
_HALF_STUD_MM = 4.0  # a 1-stud-wide part's axis to either of its faces
_MAX_STUDS = 48  # sanity bound on a length read out of a name
_STUD_IFACE = "//pub/universe/lego:stud"
_ANTI_IFACE = "//pub/universe/lego:anti-stud"
_PIN_IFACE = "//pub/universe/lego:technic-pin"
_PIN_HOLE_IFACE = "//pub/universe/lego:technic-pin-hole"
_AXLE_IFACE = "//pub/universe/lego:technic-axle"
_AXLE_HOLE_IFACE = "//pub/universe/lego:technic-axle-hole"

# Port orientations, as the (axis, angle) half of an OCCT location: each turns
# the port's Z axis onto one of the part's own axes.
_Z_TO_PLUS_X = ((0, 1, 0), 90)
_Z_TO_MINUS_X = ((0, 1, 0), 270)
_Z_TO_PLUS_Y = ((1, 0, 0), 270)
_Z_TO_MINUS_Y = ((1, 0, 0), 90)
_Z_TO_PLUS_Z = ((0, 0, 1), 0)
_Z_TO_MINUS_Z = ((1, 0, 0), 180)
# Down (-Y) as well, but rolled a quarter turn: this is the stud orientation
# composed with the 180-degree flip about [1,1,0] that a connection applies, and
# composing it back is what leaves a stud/anti-stud mate a pure translation.
# Plain _Z_TO_MINUS_Y would stack the bricks correctly but turned 90 degrees.
_ANTI_STUD_ROT = ((1, 1, -1), 120)

# body height in the meshed part-space (mm) and whether the top face has studs
_LEGO_KINDS = {
    "brick": (9.6, True),
    "plate": (3.2, True),
    "tile": (3.2, False),  # a tile is smooth on top: anti-studs only
}
# "<type> A x B", optionally followed by a variant suffix ("with Groove", ...).
# A third dimension is rejected so a taller "Brick 1 x 2 x 5" is not mistaken for
# a plain 1 x 2 (its height, and thus its anti-stud plane, would be wrong). The
# lookahead has to reject a digit and a '.' as well as another "x": stopping at
# "x" alone lets the regex backtrack to a shorter second number and match
# "Plate 16 x 16 x 0.667" as a 16 x 1 plate.
_DIM_RE = re.compile(r"^(Brick|Plate|Tile)\s+(\d+)\s*x\s*(\d+)(?!\s*[.\dx])", re.IGNORECASE)
# A renamed part is a stub whose description redirects to its replacement, e.g.
# "~Moved to 3068b"; follow it so the classic ids (3023, 3068, ...) still get
# interfaces from the replacement's "<type> A x B" name.
_MOVED_RE = re.compile(r"^~?Moved to (\S+)", re.IGNORECASE)


def _effective_desc(desc, depth=0):
    """Follow '~Moved to <id>' redirects to the real part's description."""
    if not desc or depth > 3:
        return desc
    m = _MOVED_RE.match(desc.strip())
    if not m:
        return desc
    target = _dat_header(m.group(1))
    if target and target[0]:
        return _effective_desc(target[0], depth + 1)
    return desc


def _grid_positions(n):
    """The n stud centers along one axis, centered on the origin, in mm."""
    return [round((i - (n - 1) / 2.0) * _STUD_MM, 4) for i in range(n)]


def _port(position, orientation):
    """An OCCT location: a port at 'position' turned by 'orientation'."""
    axis, angle = orientation
    return [[round(float(v), 4) for v in position], list(axis), angle]


def _stud_instances(depth, length, height, has_studs):
    """The stud and anti-stud instances of a rectangular part, A (depth, Z) by
    B (length, X) studs, whose body hangs 'height' mm below its top plane.

    Instances are named c<col>r<row> (column along X, row along Z), so an
    assembly can pick a specific stud to offset one part on another's grid.
    """
    xs = _grid_positions(length)  # X (the B / length axis)
    zs = _grid_positions(depth)  # Z (the A / depth axis)
    y_bottom = round(-height, 4)

    implements = {}
    if has_studs:
        implements[_STUD_IFACE] = {
            "c%dr%d" % (ci, ri): _port((x, 0, z), _Z_TO_PLUS_Y) for ci, x in enumerate(xs) for ri, z in enumerate(zs)
        }
    implements[_ANTI_IFACE] = {
        "c%dr%d" % (ci, ri): _port((x, y_bottom, z), _ANTI_STUD_ROT)
        for ci, x in enumerate(xs)
        for ri, z in enumerate(zs)
    }
    return implements


def _brick_implements(desc):
    """The instances of a rectangular Brick / Plate / Tile, or None."""
    m = _DIM_RE.match(desc)
    if not m:
        return None
    depth, length = int(m.group(2)), int(m.group(3))
    if not (1 <= depth <= _MAX_STUDS and 1 <= length <= _MAX_STUDS):
        return None
    height, has_studs = _LEGO_KINDS[m.group(1).lower()]
    return _stud_instances(depth, length, height, has_studs)


# --- Technic ----------------------------------------------------------------
#
# Technic parts connect through holes rather than studs: a round pin hole takes
# a pin (or an axle, which then turns in it), and a cross-shaped axle hole takes
# an axle and drives it. Both are 4.8 mm across and go right through a part that
# is one stud thick, so each hole has two mouths and gets one instance per mouth.
#
# The families below are the ones whose geometry their name pins down exactly,
# and every position here was checked against the LDraw parts themselves:
#
#   Technic Brick 1 x N with Hole(s)   holes on the +-Z faces, 4 mm below the
#                                      top plane, plus the usual stud grid
#   Technic Brick 1 x 1/2 with Axlehole  one axle hole in the same place
#   Technic Beam N                     N holes along Z on the +-Y faces
#   Technic Axle N                     a shaft 8N mm long, centered
#   Technic Pin [Long]                 one pin end per side of the collar(s)
#   Technic Pin 1/2                    a pin one way, a stud the other
#
# A part whose name says anything else (a bent beam, an axle with a stop, a
# liftarm, a plate with holes at its ends) is left alone: its features are not
# where the plain name would put them.
_TECHNIC_HOLE_Y = -4.0  # the hole axis of a Technic brick, below its top plane


def _hole_pair(name, center, axis):
    """Both mouths of one through-hole, as {instance name: port}.

    The hole runs along 'axis' ("y" or "z") and is half a stud deep either side
    of 'center', which is where a mouth lands on the part's face. Each mouth is
    named after the face it is on, so an assembly can say which side of the part
    it is working from: "<name>-front"/"-back" for a hole along Z, and
    "<name>-top"/"-bottom" for one along Y.
    """
    x, y, z = center
    if axis == "z":
        return {
            name + "-front": _port((x, y, z + _HALF_STUD_MM), _Z_TO_PLUS_Z),
            name + "-back": _port((x, y, z - _HALF_STUD_MM), _Z_TO_MINUS_Z),
        }
    return {
        name + "-top": _port((x, y + _HALF_STUD_MM, z), _Z_TO_PLUS_Y),
        name + "-bottom": _port((x, y - _HALF_STUD_MM, z), _Z_TO_MINUS_Y),
    }


def _technic_brick_holes(m):
    """Technic Brick 1 x N with Hole(s): the stud grid plus the pin holes.

    The holes lie on the same 8 mm grid as the studs but half a step off it: a
    "with Holes" brick has one hole between every pair of studs. The two odd
    ones out are the shortest: "with Hole" (singular) is a single hole on the
    part's center line, and the 1 x 2 "with Holes" has its two holes under the
    studs rather than between them.
    """
    length, plural = int(m.group(1)), m.group(2).lower().endswith("s")
    if not 1 <= length <= _MAX_STUDS:
        return None
    height, has_studs = _LEGO_KINDS["brick"]
    implements = _stud_instances(1, length, height, has_studs)
    if not plural or length == 1:
        xs = [0.0]
    elif length == 2:
        xs = _grid_positions(2)
    else:
        xs = _grid_positions(length - 1)
    holes = {}
    for index, x in enumerate(xs):
        holes.update(_hole_pair("h%d" % index, (x, _TECHNIC_HOLE_Y, 0), "z"))
    implements[_PIN_HOLE_IFACE] = holes
    return implements


def _technic_brick_axlehole(m):
    """Technic Brick 1 x N with (Reduced / Open Sides / ...) Axlehole.

    Every one of them puts its single cross hole where the pin hole of a Technic
    brick goes; the variants differ only in the shape inside the hole.
    """
    length = int(m.group(1))
    height, has_studs = _LEGO_KINDS["brick"]
    implements = _stud_instances(1, length, height, has_studs)
    implements[_AXLE_HOLE_IFACE] = _hole_pair("axle", (0.0, _TECHNIC_HOLE_Y, 0), "z")
    return implements


def _technic_beam(m):
    """Technic Beam N: N holes on the 8 mm grid along the beam, through its
    faces. A beam lies along Z and is 8 mm thick along Y, so - unlike a brick's -
    its holes run along Y and it has no studs at all."""
    length = int(m.group(1))
    if not 1 <= length <= _MAX_STUDS:
        return None
    holes = {}
    for index, z in enumerate(_grid_positions(length)):
        holes.update(_hole_pair("h%d" % index, (0, 0, z), "y"))
    return {_PIN_HOLE_IFACE: holes}


def _technic_axle(m):
    """Technic Axle N: a shaft 8N mm long along X, centered on the origin.

    Each end is a port whose Z points at the other end, so connecting one lays
    the shaft into the hole with that end flush with the mouth.
    """
    length = int(m.group(1))
    if not 1 <= length <= _MAX_STUDS:
        return None
    half = round(length * _STUD_MM / 2.0, 4)
    return {
        _AXLE_IFACE: {
            "left": _port((-half, 0, 0), _Z_TO_PLUS_X),
            "right": _port((half, 0, 0), _Z_TO_MINUS_X),
        }
    }


def _technic_pin(m):
    """Technic Pin (2 modules): one collar, in the middle of the part, with a pin
    end either side of it."""
    return {
        _PIN_IFACE: {
            "left": _port((0, 0, 0), _Z_TO_MINUS_X),
            "right": _port((0, 0, 0), _Z_TO_PLUS_X),
        }
    }


def _technic_pin_long(m):
    """Technic Pin Long (3 modules): the collars are a module apart, so each is
    half a module either side of the middle."""
    half = round(_STUD_MM / 2.0, 4)
    return {
        _PIN_IFACE: {
            "left": _port((-half, 0, 0), _Z_TO_MINUS_X),
            "right": _port((half, 0, 0), _Z_TO_PLUS_X),
        }
    }


def _technic_pin_half(m):
    """Technic Pin 1/2: a pin one way and a stud the other, which is what makes
    it the part that joins the stud half of the system to the Technic half."""
    return {
        _PIN_IFACE: {"left": _port((0, 0, 0), _Z_TO_MINUS_X)},
        _STUD_IFACE: {"stud": _port((0, 0, 0), _Z_TO_PLUS_X)},
    }


# Each rule is a whole-description match: a name with anything else in it (a
# bumper holder, a stop, a bent arm) does not describe these coordinates. The
# longer names come first, so that a rule can be read without checking that no
# earlier one could have taken the description first.
_TECHNIC_RULES = (
    (re.compile(r"^Technic\s+Brick\s+1\s*x\s*(\d+)\s+with\s+(Holes?)$", re.IGNORECASE), _technic_brick_holes),
    (
        re.compile(r"^Technic\s+Brick\s+1\s*x\s*([12])\s+with\s+[A-Za-z\- ]*Axlehole\b.*$", re.IGNORECASE),
        _technic_brick_axlehole,
    ),
    (re.compile(r"^Technic\s+Beam\s+(\d+)$", re.IGNORECASE), _technic_beam),
    (re.compile(r"^Technic\s+Axle\s+(\d+)$", re.IGNORECASE), _technic_axle),
    (
        re.compile(r"^Technic\s+Pin\s+Long(?:\s+with\s+Friction(?:\s+and\s+Slots?)?)?$", re.IGNORECASE),
        _technic_pin_long,
    ),
    (re.compile(r"^Technic\s+Pin\s+1/2$", re.IGNORECASE), _technic_pin_half),
    (re.compile(r"^Technic\s+Pin(?:\s+3/4)?(?:\s+with\s+Friction(?:\s+and\s+Slots?)?)?$", re.IGNORECASE), _technic_pin),
)


def _technic_implements(desc):
    """The instances of a Technic part, or None if its name is not one of the
    families above."""
    for pattern, builder in _TECHNIC_RULES:
        m = pattern.match(desc)
        if m:
            return builder(m)
    return None


def _lego_implements(desc):
    """The 'implements' block for a part, or None.

    A description is either a plain rectangular Brick / Plate / Tile or one of
    the Technic families; nothing is both, so the first match wins.
    """
    if not desc:
        return None
    desc = desc.strip()
    return _brick_implements(desc) or _technic_implements(desc)


def _part_config(pid, meta):
    desc, author, lic = meta if meta else (None, None, None)
    config = {"type": ":ldraw", "dat": pid + ".dat"}
    if desc:
        config["desc"] = desc
    if author:
        config["author"] = author
    if lic:
        config["license"] = lic
    implements = _lego_implements(_effective_desc(desc))
    if implements:
        config["implements"] = implements
    return config


def _catalog(category):
    """{part_id: config} for a whole category, metadata from the .dat headers.

    The complete part list comes from every list page; description, author and
    license come from each part's .dat header, fetched concurrently. A part
    whose header could not be read is still listed (with just its type/dat).
    """
    ids = _part_ids(category)
    if not ids:
        return {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=_HEADER_WORKERS) as pool:
        metas = list(pool.map(_dat_header, ids))
    return {pid: _part_config(pid, meta) for pid, meta in zip(ids, metas)}


# --- partType wrapper file --------------------------------------------------

_PART_TYPE = {"kind": "wrapper", "path": "ldraw.py"}


def _ldraw_py_b64():
    with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "ldraw.py"), "rb") as f:
        return base64.b64encode(f.read()).decode()


# --- the key/value protocol -------------------------------------------------


def get(key):
    cats = _categories()
    first, _, sub = key.partition("/")

    if first not in cats:
        # Top-level package: its children are the categories; it holds no parts.
        if key == "deps":
            return sorted(cats)
        if key == "meta":
            return {"desc": "The LDraw parts library, by category."}
        if key == "objects/partType":
            return {"ldraw": dict(_PART_TYPE)}
        if key.startswith("objects/"):
            return {}
        return None

    category = cats[first]

    if sub == "deps":
        return []
    if sub == "meta":
        return {"desc": "LDraw parts in the '%s' category." % category}
    if sub == "objects/part":
        return _catalog(category)
    if sub.startswith("objects/part/"):
        pid = sub[len("objects/part/") :]
        header = _dat_header(pid)
        return _part_config(pid, header) if header is not None else None
    if sub == "objects/partType":
        return {"ldraw": dict(_PART_TYPE)}
    if sub.startswith("objects/partType/"):
        name = sub[len("objects/partType/") :]
        return dict(_PART_TYPE) if name == "ldraw" else None
    if sub.startswith("objects/"):
        return {}
    if sub == "files/ldraw.py":
        return _ldraw_py_b64()

    return None


if __name__ == "get":
    output = {"result": get(request["key"])}  # noqa: F821 - injected by the runtime
elif __name__ == "__main__":
    _cats = _categories()
    print("categories:", len(_cats))
    demo = "Brick" if "Brick" in _cats else sorted(_cats)[0]
    cat = _cats[demo]
    print("sample category:", demo, "->", cat)
    catalog = _catalog(cat)
    print("parts:", len(catalog))
    for pid, cfg in list(catalog.items())[:3]:
        print("  ", pid, "->", cfg)
else:
    output = {}
