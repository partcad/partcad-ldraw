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
import math
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
    """Where fetched LDraw files are kept: $PARTCAD_LDRAW_CACHE, else under XDG."""
    base = os.environ.get("PARTCAD_LDRAW_CACHE")
    if not base:
        xdg = os.environ.get("XDG_CACHE_HOME") or os.path.join(os.path.expanduser("~"), ".cache")
        base = os.path.join(xdg, "partcad-ldraw")
    return base


def _http_get(url):
    """The body of 'url' as text, retried with a backoff; None if every try fails."""
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
_LDU_MM = 0.4  # 1 LDraw Unit, the scale the wrapper meshes a .dat at
_STUD_MM = 8.0  # stud pitch in the meshed part-space (20 LDU * 0.4)
_HALF_STUD_MM = 4.0  # a 1-stud-wide part's axis to either of its faces
_MAX_STUDS = 48  # sanity bound on a length read out of a name
_STUD_IFACE = "//pub/universe/lego:stud"
_ANTI_IFACE = "//pub/universe/lego:anti-stud"
_PIN_IFACE = "//pub/universe/lego:technic-pin"
_PIN_HOLE_IFACE = "//pub/universe/lego:technic-pin-hole"
_AXLE_IFACE = "//pub/universe/lego:technic-axle"
_AXLE_HOLE_IFACE = "//pub/universe/lego:technic-axle-hole"
_DUPLO_STUD_IFACE = "//pub/universe/lego:duplo-stud"
_DUPLO_ANTI_IFACE = "//pub/universe/lego:duplo-anti-stud"
_NECK_IFACE = "//pub/universe/lego:minifig-neck"
_NECK_SOCKET_IFACE = "//pub/universe/lego:minifig-neck-socket"
_WAIST_IFACE = "//pub/universe/lego:minifig-waist"
_WAIST_SOCKET_IFACE = "//pub/universe/lego:minifig-waist-socket"
_GEAR_TOOTH_IFACE = "//pub/universe/lego:gear-tooth"
_GEAR_GAP_IFACE = "//pub/universe/lego:gear-gap"
_WHEEL_IFACE = "//pub/universe/lego:wheel-rim"
_TYRE_IFACE = "//pub/universe/lego:tyre-bore"
_RJ12_PLUG_IFACE = "//pub/universe/lego:rj12-plug"
_RJ12_SOCKET_IFACE = "//pub/universe/lego:rj12-socket"

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
# The mate flip itself, for a pair whose two ports sit on the same point facing
# along the same axis: composing it back leaves the connection an identity, which
# is how LDraw draws a tyre on a wheel and a hat on a head.
_TYRE_ROT = ((1, 1, 0), 180)

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
    # '+ 0.0' turns a negative zero back into a zero: a position mirrored out of
    # the geometry can arrive as -0.0, which is equal to 0.0 but does not read
    # like it in the config this ends up in.
    return [[round(float(v), 4) + 0.0 for v in position], list(axis), angle]


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
# "Technic Pin 3/4" is deliberately not among them: it is a full pin one way and
# a half one (connect3.dat) the other, and its name does not say which end is
# which, so "left" and "right" would promise a symmetry it does not have.
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


def _technic_pin_three_quarter(m):
    """Technic Pin 3/4: a full pin one way and a half one the other.

    The name does not say which end is which, which is why this was left out
    while the rules were names alone. LDraw's own geometry does: 32002 places
    "connect" (Technic Pin 1.0 with Base Collar) toward -X and "connect3"
    (Technic Pin 0.5) toward +X. Both collars sit on the origin, so the ports
    are where a full pin's would be; what the geometry settles is which end
    goes in only half way, and that is now in the instance names.
    """
    return {
        _PIN_IFACE: {
            "left": _port((0, 0, 0), _Z_TO_MINUS_X),
            "rightHalf": _port((0, 0, 0), _Z_TO_PLUS_X),
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
    (re.compile(r"^Technic\s+Pin\s+3/4$", re.IGNORECASE), _technic_pin_three_quarter),
    (re.compile(r"^Technic\s+Pin(?:\s+with\s+Friction(?:\s+and\s+Slots?)?)?$", re.IGNORECASE), _technic_pin),
)


def _name_implements(desc):
    """The instances a part's name determines, or None if it names no family."""
    for pattern, builder in _TECHNIC_RULES + _OTHER_RULES:
        m = pattern.match(desc)
        if m:
            return builder(m)
    return None


def _lego_implements(desc, pid=None):
    """The 'implements' block for a part, or None.

    Almost every part is served by its name alone: a description belongs to at
    most one family - a plain rectangular Brick / Plate / Tile, one of the
    Technic families, a gear, a Duplo brick, a minifig part, a wheel or a tyre -
    so the first match wins and nothing is fetched.

    The studs are the exception, and are read from the part's geometry, which
    is the only thing that knows where they actually are; so are the Mindstorms
    connectors. Both need the part's id as well as its name, so a caller
    without one gets only what the name gives.
    """
    if not desc:
        return None
    desc = desc.strip()
    implements = _brick_implements(desc) or _name_implements(desc)
    if implements is None and pid and _ELECTRIC_RE.match(desc):
        implements = _electric_implements(pid)
    if implements is None and pid and _HEADGEAR_RE.match(desc):
        implements = _headgear_implements(pid)
    if pid:
        implements = _with_geometry_studs(implements, pid)
    return implements


def _with_geometry_anti_studs(implements, pid):
    """Replace the name-derived anti-studs with the ones the underside has.

    Only when the tubes settle it; otherwise the name's answer stands, because
    an anti-stud no tube marks may still be there.
    """
    anti = _geometry_anti_studs(pid)
    if not anti:
        return implements
    implements = dict(implements) if implements else {}
    named = implements.get(_ANTI_IFACE)
    if named and sorted(named.values()) == sorted(anti.values()):
        return implements or None  # same ports: keep the names they had
    implements[_ANTI_IFACE] = anti
    return implements or None


def _with_geometry_studs(implements, pid):
    """Replace the name-derived studs with the ones the part actually has.

    Only the studs: the anti-stud grid underneath still comes from the name,
    because an underside tube sits between four studs rather than under one and
    turning those into ports is a separate piece of work. When the walk cannot
    see the whole part it returns None and the name's answer is left alone.
    """
    implements = _with_geometry_anti_studs(implements, pid)
    studs = _geometry_stud_implements(pid)
    if studs is None:
        return implements
    implements = dict(implements) if implements else {}
    named = implements.get(_STUD_IFACE)
    if named and sorted(named.values()) == sorted(studs.values()):
        # The geometry found exactly the studs the name did. Keep the names the
        # name rule gave them: a minifig head's single stud is "stud", not the
        # "c0r0" a grid would call it, and an assembly may already say so.
        return implements or None
    if studs:
        implements[_STUD_IFACE] = studs
    else:
        implements.pop(_STUD_IFACE, None)
    return implements or None


def _part_config(pid, meta):
    """The PartCAD config of one part: its wrapper, its .dat, and what it implements."""
    desc, author, lic = meta if meta else (None, None, None)
    config = {"type": ":ldraw", "dat": pid + ".dat"}
    if desc:
        config["desc"] = desc
    if author:
        config["author"] = author
    if lic:
        config["license"] = lic
    implements = _lego_implements(_effective_desc(desc), pid)
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
    """The 'ldraw.py' wrapper, base64-encoded, as the 'files/' key serves it."""
    with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "ldraw.py"), "rb") as f:
        return base64.b64encode(f.read()).decode()


# --- the key/value protocol -------------------------------------------------


def get(key):
    """Answer one key of the repository protocol; None if this package has no such key.

    The top level holds only the categories, and each category holds the parts
    of that category and the 'ldraw' partType that renders them.
    """
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


# --- Duplo ------------------------------------------------------------------
#
# The same connection as a stud, at twice the size: LDraw draws a Duplo brick
# with its studs on a 40 LDU (16 mm) grid and a body 48 LDU (19.2 mm) deep,
# both exactly double the system brick. Only the plain "Duplo Brick A x B" is
# taken: of the parts whose name carries a suffix as well, a quarter have a stud
# grid that is not the full A x B (hinge bases, curved tops, bricks with holes).
_DUPLO_MM = 16.0  # Duplo stud pitch (40 LDU * 0.4)
_DUPLO_HEIGHT_MM = 19.2  # Duplo brick body height (48 LDU * 0.4)
_DUPLO_RE = re.compile(r"^Duplo Brick\s+(\d+)\s*x\s*(\d+)\s*$", re.IGNORECASE)


def _duplo_implements(m):
    """Duplo Brick A x B: the stud grid, on the Duplo scale."""
    depth, length = int(m.group(1)), int(m.group(2))
    if not (1 <= depth <= _MAX_STUDS and 1 <= length <= _MAX_STUDS):
        return None
    xs = [round((i - (length - 1) / 2.0) * _DUPLO_MM, 4) for i in range(length)]
    zs = [round((i - (depth - 1) / 2.0) * _DUPLO_MM, 4) for i in range(depth)]
    y_bottom = round(-_DUPLO_HEIGHT_MM, 4)
    return {
        _DUPLO_STUD_IFACE: {
            "c%dr%d" % (ci, ri): _port((x, 0, z), _Z_TO_PLUS_Y) for ci, x in enumerate(xs) for ri, z in enumerate(zs)
        },
        _DUPLO_ANTI_IFACE: {
            "c%dr%d" % (ci, ri): _port((x, y_bottom, z), _ANTI_STUD_ROT)
            for ci, x in enumerate(xs)
            for ri, z in enumerate(zs)
        },
    }


# --- Minifig ----------------------------------------------------------------
#
# A minifig is a stack whose spacing is fixed by the system rather than by any
# one part's name: LDraw's own assembled minifigs (979, 980) place the torso
# 32 LDU above the hips and the head 28 LDU above the torso, and put the hat at
# the head's own origin. So every part of a class carries the same ports no
# matter what is printed on it, which is what makes hundreds of heads and torsos
# derivable from a name that says nothing but "Minifig Head".
#
# The planes below are those two offsets, split between the two parts of each
# joint: the head's rim rests on the torso's shoulder (the base of the neck
# stud, 4 LDU below the top of the torso), and the torso's bottom rests on the
# hips. The head's top is an ordinary system stud, so a hat is an anti-stud and
# the pair meets at the origin, exactly as 979 and 980 draw it.
_NECK_TORSO_MM = 1.6  # LDraw y = -4: the shoulder plane the head's rim sits on
_NECK_HEAD_MM = -9.6  # LDraw y = 24: the rim around the head's neck socket
_WAIST_HIPS_MM = 0.0  # LDraw y = 0: the top of the hips
_WAIST_TORSO_MM = -12.8  # LDraw y = 32: the bottom of the torso
# A head is standard when its name says nothing but "Minifig Head", "Female" or
# the pattern printed on it; the sculpted character heads (Yoda, the Simpsons,
# the cuboid ones) are a different shape and are left alone. A torso likewise,
# minus the handful that are a different part in all but name. Checked against
# the geometry of every part the three accept: 326 heads, 613 torsos and 225
# hips, every one of them where the rule says.
#
# Headgear is deliberately absent. It would be an ordinary anti-stud sharing the
# head's origin, but no name rule gets past about 96% of the 369 parts - police
# hats, pillboxes and a few "helmets" that are really heads sit differently - and
# a wrong port is worse than none.
_MINIFIG_HEAD_RE = re.compile(r"^Minifig Head(?:\s+Female)?(?:\s+with\b.*)?$", re.IGNORECASE)
_MINIFIG_TORSO_RE = re.compile(r"^Minifig Torso(?!\s+(?:Long|Short|Brick|Skeleton)\b)(?!.*\bIntegral\b)", re.IGNORECASE)
_MINIFIG_HIPS_RE = re.compile(r"^Minifig Hips\b", re.IGNORECASE)


def _minifig_head(m):
    """A minifig head: a neck socket below, an ordinary system stud on top."""
    return {
        _NECK_SOCKET_IFACE: {"neck": _port((0, _NECK_HEAD_MM, 0), _ANTI_STUD_ROT)},
        _STUD_IFACE: {"stud": _port((0, 0, 0), _Z_TO_PLUS_Y)},
    }


def _minifig_torso(m):
    """A minifig torso: the neck stud on top, the waist opening below."""
    return {
        _NECK_IFACE: {"neck": _port((0, _NECK_TORSO_MM, 0), _Z_TO_PLUS_Y)},
        _WAIST_SOCKET_IFACE: {"waist": _port((0, _WAIST_TORSO_MM, 0), _ANTI_STUD_ROT)},
    }


def _minifig_hips(m):
    """A pair of minifig hips: the waist the torso sits on."""
    return {_WAIST_IFACE: {"waist": _port((0, _WAIST_HIPS_MM, 0), _Z_TO_PLUS_Y)}}


# --- Gears ------------------------------------------------------------------
#
# A gear mesh is a port pair once each tooth and each gap is a port. LEGO gears
# are cut to one module: the pitch diameter in millimetres is the tooth count,
# so the pitch radius is 0.5 mm per tooth and two meshing gears sit (N1 + N2)/2
# millimetres apart - which is what bringing a tooth port and a gap port
# together produces, since each is on its own gear's pitch circle with its Z
# pointing outwards. Tooth centres are at 360k/N degrees and gaps half a pitch
# from them; both were measured on the LDraw gears themselves, which lie in the
# XY plane with the axle along Z.
_GEAR_MODULE_MM = 0.5  # pitch radius per tooth
# The tooth counts of the modern LEGO gear system. The bound is what keeps the
# vintage "Technic Gear 14 Tooth" (641), which is cut to a different module, out
# of a rule its name would otherwise fit.
_GEAR_TEETH = (8, 12, 16, 20, 24, 28, 36, 40)
_GEAR_RE = re.compile(
    r"^Technic Gear\s+(\d+)\s*Tooth" r"(?:\s+Double Bevel)?"
    # Suffixes that describe the hub rather than the teeth, so the pitch circle
    # is unchanged. Every part these accept was checked against its geometry.
    r"(?:\s+Reinforced|\s+Sliding|\s+with Clutch\b.*|\s+Clutch\b.*"
    r"|\s+with\s+\d+\s+Axleholes?|\s+with Single Axle Hole)*\s*$",
    re.IGNORECASE,
)


def _orientation_of(quaternion):
    """An (axis, angle) orientation, as '_port' wants it, from a quaternion."""
    w, x, y, z = quaternion
    w = max(-1.0, min(1.0, w))
    angle = math.degrees(2.0 * math.acos(w))
    sin_half = math.sqrt(max(0.0, 1.0 - w * w))
    if sin_half < 1e-9:
        return ((0, 0, 1), 0)
    axis = (round(x / sin_half, 6), round(y / sin_half, 6), round(z / sin_half, 6))
    return (axis, round(angle, 6))


# The port frame of a tooth or a gap. Its Z has to point radially outwards, and
# the roll is not free: a connection turns the source port by 180 degrees about
# [1,1,0], and for two meshed gears to stay coplanar - axles parallel, which is
# the whole point - that turn has to come out as a rotation about the axle. That
# holds exactly when the frame carries [1,1,0] onto the axle, which this one
# does, while carrying Z onto the outward radius at an angle of zero.
_ROOT_HALF = math.sqrt(0.5)
_GEAR_BASE_MATRIX = ((0.0, 0.0, 1.0), (_ROOT_HALF, -_ROOT_HALF, 0.0), (_ROOT_HALF, _ROOT_HALF, 0.0))


def _gear_port(index, count, offset_turns):
    """The port for tooth (offset 0) or gap (offset 0.5) number 'index'."""
    angle = 360.0 * (index + offset_turns) / count
    radius = count * _GEAR_MODULE_MM
    radians = math.radians(angle)
    position = (round(radius * math.cos(radians), 4), round(radius * math.sin(radians), 4), 0)
    turn = (
        (math.cos(radians), -math.sin(radians), 0.0),
        (math.sin(radians), math.cos(radians), 0.0),
        (0.0, 0.0, 1.0),
    )
    return _port(position, _orientation_of_matrix(_matrix_product(turn, _GEAR_BASE_MATRIX)))


def _gear_implements(m):
    """Technic Gear N Tooth: N tooth ports and N gap ports on the pitch circle."""
    count = int(m.group(1))
    if count not in _GEAR_TEETH:
        return None
    return {
        _GEAR_TOOTH_IFACE: {"t%d" % i: _gear_port(i, count, 0.0) for i in range(count)},
        _GEAR_GAP_IFACE: {"g%d" % i: _gear_port(i, count, 0.5) for i in range(count)},
    }


# --- Wheels and tyres -------------------------------------------------------
#
# A tyre goes on a wheel concentrically and LDraw draws the pair sharing one
# origin (4266c01, 22253c01 and 22969ac01 all place both at 0,0,0), so both
# ports sit on the origin with their Z along the axle and the mate comes out as
# the identity. The nominal fitting diameter is in each name - "Wheel W x D" and
# "Tyre W/ A x D" - and goes into the instance name, so an assembly says which
# size it means.
_WHEEL_RE = re.compile(r"^Wheel\s+([\d.]+)\s*x\s*([\d.]+)\b", re.IGNORECASE)
_TYRE_RE = re.compile(r"^Tyre\s+([\d.]+)\s*/\s*([\d.]+)\s*x\s*([\d.]+)\b", re.IGNORECASE)


def _diameter_instance(text):
    """ "30" or "17.6" -> the instance name "d30" / "d17.6"."""
    value = float(text)
    return "d" + (str(int(value)) if value == int(value) else str(value))


def _wheel_implements(m):
    """Wheel W x D: the rim, which a tyre of the same diameter goes onto."""
    return {_WHEEL_IFACE: {_diameter_instance(m.group(2)): _port((0, 0, 0), _Z_TO_PLUS_Z)}}


def _tyre_implements(m):
    """Tyre W/ A x D: the bore, at the origin its wheel puts the rim at."""
    return {_TYRE_IFACE: {_diameter_instance(m.group(3)): _port((0, 0, 0), _TYRE_ROT)}}


# --- ports read from the geometry -------------------------------------------
#
# The Mindstorms parts are the one family here whose name says nothing about
# where anything is: "Electric Mindstorms EV3 Large Motor" has no dimensions in
# it, and none of them puts a single connector in its own .dat - every one is
# inside a subpart, one to four levels down. So for these, and only these, the
# ports are read from the geometry.
#
# That is affordable because the walk does not have to fetch much. A connector
# is a *primitive*, and a primitive is identified by the reference line that
# names it, so primitives are leaves and are never fetched: only the part files
# and the 's\' subparts are. Each file lists its own references, so the whole of
# the next level is known as soon as the current one is parsed, and can be
# fetched in parallel. Measured against the LDraw library, that finds exactly
# the same connectors as an exhaustive walk while fetching 8 to 27 files for a
# Mindstorms part instead of 50 to 86, and 2 for an ordinary brick.
# The budget is a ceiling, not a spend: a part whose walk finishes in ten files
# reads ten of them whatever the ceiling is, so raising it costs anything only
# on the parts that were being cut off. Measured over the whole library, going
# from the original 4 / 64 to these numbers leaves 0 parts unfinished instead of
# 198, for 45,500 reads instead of 45,022 - 1.1% more, and 27 more distinct
# files. Depth on its own buys almost nothing (6 / 64 recovers 11 of the 198);
# it is the file count that binds. 12 / 4096 measured identical to this, so
# nothing is running away and being cut off further out.
_GEOMETRY_DEPTH = 8  # deeper than any part in the library needs
_GEOMETRY_FILES = 1024  # a bound on one part's walk, so a cycle cannot run away
# What a reference has to look like to be worth fetching: an LDraw part id, or a
# subpart of one. Everything else is a primitive, and a leaf.
#
# The 'u' prefix is part of the vocabulary: 625 of the files under parts/ are
# named u<digits>, and a handful of them carry connectors (u9449 and u9450, the
# RCX modules, hold two pin holes each). They are out of reach at the budget
# above and so this changes nothing today - it is here so that raising the
# budget does the right thing rather than a surprising one.
#
# A hyphen must NOT be allowed in, however tempting 's/883-1.dat' makes it look.
# The primitives are full of digit-initial hyphenated names - 4-4cyli, 1-4ndis,
# 2-4disc - and admitting those turns every primitive into something the walk
# descends into, which exhausts the file budget on geometry that holds no
# connectors: measured on the EV3 brick, 36 connectors become 4.
_PART_REF_RE = re.compile(r"^(?:s/)?u?\d[0-9a-z]*\.dat$", re.IGNORECASE)

# The connectors this walk understands, and where each one's port is in the
# primitive's own frame: the mouth (in LDU) and the axis that points out of the
# part, as a multiple of the primitive's own axes. Each was read off the LDraw
# parts that use it - the sockets, for instance, against the four faces of the
# EV3 and NXT bricks and the two motors, whose outer surface they land on
# exactly.
_GEOMETRY_CONNECTORS = {
    # a round Technic hole, mouth at the origin, opening away from +Y
    "peghole.dat": [(_PIN_HOLE_IFACE, (0, 0, 0), (0, -1, 0))],
    # a Technic hole through a beam: a mouth at either end of its Y axis
    "connhole.dat": [
        (_PIN_HOLE_IFACE, (0, 10, 0), (0, 1, 0)),
        (_PIN_HOLE_IFACE, (0, -10, 0), (0, -1, 0)),
    ],
    # the NXT cable socket: its mouth is 18 LDU back along its own -Z
    "54732.dat": [(_RJ12_SOCKET_IFACE, (0, 0, -18), (0, 0, -1))],
    # the EV3 cable socket: mouth at its origin
    "54732b.dat": [(_RJ12_SOCKET_IFACE, (0, 0, 0), (0, 0, -1))],
    # the plug on the end of a cable: the shoulder the socket stops it at
    "933.dat": [(_RJ12_PLUG_IFACE, (0, 0, -18), (0, 0, 1))],
}

# A cross-shaped axle hole. Unlike a peg hole, LDraw does not draw one of these
# with a single primitive: an axle hole is a *profile* spanning y in [0, 1] that
# its placement matrix stretches through the part, so its two mouths are the two
# ends of that stretch - the matrix supplies the length, and the axis it hands
# back is stretched with it and has to be normalised (_orientation_towards does
# that).
#
# Which primitive marks a hole needs care, because most of this family are faces
# of one rather than one each: "Side Edges", "Tooth Outer Edges" and "Tooth
# Surface" can appear four to a hole. Two spellings are one-per-hole - the whole
# forms ("Closed", "Open One Side", "Open Two Opposite Sides", "Reduced Closed",
# "Semi-Reduced", "Two-toothed Sliding") and the "Perimeter" face, which some
# parts use *instead* of a whole form: 32064b, "Technic Brick 1 x 2 with Reduced
# Axlehole", draws its hole entirely out of faces and would otherwise be missed.
# Measured over the library, 574 parts carry only whole forms, 128 only
# perimeters, and the 13 that carry both never put the two at the same place, so
# taking either as a hole does not count one twice.
_AXLE_HOLE_MOUTHS = [
    (_AXLE_HOLE_IFACE, (0, 0, 0), (0, -1, 0)),
    (_AXLE_HOLE_IFACE, (0, 1, 0), (0, 1, 0)),
]
for _axle_hole in (
    "axlehole",  # Technic Axle Hole Closed
    "axlehol4",  # ... Open One Side
    "axlehol5",  # ... Open Two Opposite Sides
    "axl2hole",  # ... Reduced Closed
    "axl3hole",  # ... Semi-Reduced
    "axl4hole",  # ... Two-toothed Sliding
    "axl2hol8",  # ... Reduced Perimeter
    "axl3hol8",  # ... Semi-Reduced Perimeter
    "axl5hol8",  # ... Rounded Perimeter
):
    _GEOMETRY_CONNECTORS[_axle_hole + ".dat"] = list(_AXLE_HOLE_MOUTHS)
del _axle_hole
_ELECTRIC_RE = re.compile(r"^Electric Mindstorms\b", re.IGNORECASE)


def _fetch_ldraw_file(name):
    """An LDraw part or subpart by reference name ('s/3001s01.dat'), cached."""
    name = name.replace("\\", "/").lower()
    return _cached(_DAT_URL + name, os.path.join("parts", *name.split("/")))


def _matrix_product(a, b):
    """The 3x3 matrix product 'a . b'."""
    return tuple(tuple(sum(a[i][k] * b[k][j] for k in range(3)) for j in range(3)) for i in range(3))


def _matrix_apply(a, v):
    """The 3-vector 'a . v'."""
    return tuple(sum(a[i][k] * v[k] for k in range(3)) for i in range(3))


_IDENTITY_MATRIX = ((1, 0, 0), (0, 1, 0), (0, 0, 1))


def _parse_references(text):
    """The sub-file references of an LDraw file: (name, matrix, translation)."""
    references = []
    for line in text.splitlines():
        fields = line.split()
        if len(fields) < 15 or fields[0] != "1":
            continue
        try:
            values = [float(v) for v in fields[2:14]]
        except ValueError:
            continue
        matrix = (tuple(values[3:6]), tuple(values[6:9]), tuple(values[9:12]))
        name = " ".join(fields[14:]).replace("\\", "/").lower()
        references.append((name, matrix, (values[0], values[1], values[2])))
    return references


def _walk_geometry(pid, visit):
    """Breadth first over a part's geometry, one parallel fetch per level.

    'visit(base, matrix, position)' is called for every reference the walk
    meets, with the reference's own basename and its placement in the part's
    frame; returning True claims it as a leaf. What is not claimed is followed
    when it names a part and dropped when it names a primitive.

    Returns True when the walk ran out of references rather than out of budget.
    That is what lets a caller tell "this part has none" from "I did not get to
    look", which matters when the answer is used instead of a name rule.
    """
    # (name, matrix, translation) of what is still to be read
    level = [(pid + ".dat", _IDENTITY_MATRIX, (0.0, 0.0, 0.0))]
    # A file is fetched once but traversed as often as it is referenced: the same
    # subpart at two different placements is two different sets of connectors,
    # which is how a cable ends up with a plug at each end.
    bodies_by_name = {}
    budget = _GEOMETRY_FILES
    complete = True
    for _ in range(_GEOMETRY_DEPTH + 1):
        if not level:
            return complete
        if budget <= 0:
            return False
        if len(level) > budget:
            complete = False
            level = level[:budget]
        budget -= len(level)
        wanted = list(dict.fromkeys(name for name, _, _ in level if name not in bodies_by_name))
        if wanted:
            with concurrent.futures.ThreadPoolExecutor(max_workers=_HEADER_WORKERS) as pool:
                for name, body in zip(wanted, pool.map(_fetch_ldraw_file, wanted), strict=True):
                    bodies_by_name[name] = body
        bodies = [bodies_by_name.get(name) for name, _, _ in level]
        following = []
        for (_, matrix, translation), body in zip(level, bodies, strict=True):
            if not body:
                continue
            for name, child_matrix, child_translation in _parse_references(body):
                composed = _matrix_product(matrix, child_matrix)
                moved = _matrix_apply(matrix, child_translation)
                position = tuple(moved[i] + translation[i] for i in range(3))
                if visit(name.split("/")[-1], composed, position):
                    continue
                if not _PART_REF_RE.match(name):
                    continue  # a primitive: a leaf, and never fetched
                following.append((name, composed, position))
        level = following
    return complete and not level


def _geometry_connectors(pid):
    """Every connector in a part's geometry, as (interface, position, axis).

    Breadth first, one parallel fetch per level, never descending into a
    primitive. Positions and axes come back in LDraw coordinates.
    """
    found = []

    def visit(base, composed, position):
        """Claim a connector primitive and record every mouth it carries."""
        connector = _GEOMETRY_CONNECTORS.get(base)
        if connector is None:
            return False
        for interface, mouth, axis in connector:
            offset = _matrix_apply(composed, mouth)
            found.append(
                (
                    interface,
                    tuple(position[i] + offset[i] for i in range(3)),
                    _matrix_apply(composed, axis),
                    composed,
                )
            )
        return True

    _walk_geometry(pid, visit)
    return found


def _orientation_towards(direction, roll_matrix):
    """An (axis, angle) whose Z is 'direction', keeping the connector's own roll."""
    length = math.sqrt(sum(v * v for v in direction))
    if length < 1e-9:
        return None
    z_axis = tuple(v / length for v in direction)
    # X from the connector's own X, made perpendicular to Z; Y completes the frame.
    x_raw = _matrix_apply(roll_matrix, (1, 0, 0))
    x_raw = tuple(x_raw[i] * 1.0 for i in range(3))
    projection = sum(x_raw[i] * z_axis[i] for i in range(3))
    x_axis = tuple(x_raw[i] - projection * z_axis[i] for i in range(3))
    scale = math.sqrt(sum(v * v for v in x_axis))
    if scale < 1e-6:  # X parallel to Z: any perpendicular will do
        x_axis = (1.0, 0.0, 0.0) if abs(z_axis[0]) < 0.9 else (0.0, 1.0, 0.0)
        projection = sum(x_axis[i] * z_axis[i] for i in range(3))
        x_axis = tuple(x_axis[i] - projection * z_axis[i] for i in range(3))
        scale = math.sqrt(sum(v * v for v in x_axis))
    x_axis = tuple(v / scale for v in x_axis)
    y_axis = (
        z_axis[1] * x_axis[2] - z_axis[2] * x_axis[1],
        z_axis[2] * x_axis[0] - z_axis[0] * x_axis[2],
        z_axis[0] * x_axis[1] - z_axis[1] * x_axis[0],
    )
    # columns are the images of X, Y and Z
    return _orientation_of_matrix(tuple((x_axis[i], y_axis[i], z_axis[i]) for i in range(3)))


def _orientation_of_matrix(m):
    """An (axis, angle) orientation from a rotation matrix, via its quaternion."""
    trace = m[0][0] + m[1][1] + m[2][2]
    if trace > 0:
        s = math.sqrt(trace + 1.0) * 2
        q = (0.25 * s, (m[2][1] - m[1][2]) / s, (m[0][2] - m[2][0]) / s, (m[1][0] - m[0][1]) / s)
    elif m[0][0] > m[1][1] and m[0][0] > m[2][2]:
        s = math.sqrt(1.0 + m[0][0] - m[1][1] - m[2][2]) * 2
        q = ((m[2][1] - m[1][2]) / s, 0.25 * s, (m[0][1] + m[1][0]) / s, (m[0][2] + m[2][0]) / s)
    elif m[1][1] > m[2][2]:
        s = math.sqrt(1.0 + m[1][1] - m[0][0] - m[2][2]) * 2
        q = ((m[0][2] - m[2][0]) / s, (m[0][1] + m[1][0]) / s, 0.25 * s, (m[1][2] + m[2][1]) / s)
    else:
        s = math.sqrt(1.0 + m[2][2] - m[0][0] - m[1][1]) * 2
        q = ((m[1][0] - m[0][1]) / s, (m[0][2] + m[2][0]) / s, (m[1][2] + m[2][1]) / s, 0.25 * s)
    return _orientation_of(q)


# What to call each instance a Mindstorms part's geometry yields. A connector
# whose interface is not named here is not served rather than named by accident.
_ELECTRIC_INSTANCE_PREFIX = {
    _PIN_HOLE_IFACE: "h",
    _AXLE_HOLE_IFACE: "a",
    _RJ12_SOCKET_IFACE: "socket",
    _RJ12_PLUG_IFACE: "plug",
}


def _electric_implements(pid):
    """The ports of a Mindstorms part, read from its geometry.

    LDraw coordinates become the meshed part space the wrapper produces -
    (x, -y, z) * 0.4 - and each connector's mouth becomes one instance. A hole
    contributes one instance per mouth, named h<i>; a socket or a plug is named
    after what it is, numbered in the order the walk meets them.
    """
    connectors = _geometry_connectors(pid)
    if not connectors:
        return None
    implements = {}
    counters = {}
    seen = set()
    for interface, position, axis, roll in sorted(connectors, key=lambda c: (c[0], c[1])):
        place = (round(position[0] * _LDU_MM, 4), round(-position[1] * _LDU_MM, 4), round(position[2] * _LDU_MM, 4))
        direction = (axis[0], -axis[1], axis[2])
        key = (interface, place, tuple(round(v, 3) for v in direction))
        if key in seen:
            continue
        seen.add(key)
        # the roll matrix travels into the part space the same way
        flip = ((1, 0, 0), (0, -1, 0), (0, 0, 1))
        orientation = _orientation_towards(direction, _matrix_product(flip, _matrix_product(roll, flip)))
        if orientation is None:
            continue
        prefix = _ELECTRIC_INSTANCE_PREFIX.get(interface)
        if prefix is None:
            continue  # an interface this rule does not know how to name
        index = counters.get(interface, 0)
        counters[interface] = index + 1
        implements.setdefault(interface, {})["%s%d" % (prefix, index)] = _port(place, orientation)
    return implements or None


# --- studs read from the geometry -------------------------------------------
#
# "Brick | Plate | Tile A x B" says how big a part is, not where its studs are.
# The two agree for a plain brick and part company everywhere else: a corner
# brick has three studs on an origin a rectangular one does not use, a "Plate
# 8 x 8 Round with 2 x 2 Centre Studs" has four rather than sixty-four, and a
# "Brick 1 x 1 with Studs on Four Sides" has five facing five ways, which no
# A x B rule can say at all. Counting the geometry of every part the rule
# matches, 216 of 2476 were getting a grid that is not theirs.
#
# So the studs are read from the geometry instead, by the same walk that reads
# the Mindstorms connectors. That works because LDraw draws every stud with a
# primitive and says in the primitive's own description what it is - "Stud",
# "Stud Open", "Stud Tube Solid", "Stud Group 2 x 2", "Stud Duplo Open" - so
# the classification below is a transcription of a vocabulary LDraw maintains
# rather than a guess about names. It is spelled out here rather than read at
# run time precisely so that the walk still never fetches a primitive.
#
# The "stu2*" spellings are LDraw's own low-resolution aliases of the "stud*"
# ones ("stu24a" is "stud4a"), so they are folded onto them instead of doubling
# the table.
_LOWRES_STUD_PREFIX = "stu2"

# "Stud", "Stud Open", and the rest of the male stud: what another part's
# anti-stud goes onto.
_MALE_STUDS = frozenset(
    """stud stud-logo stud-logo2 stud-logo3 stud-logo4 stud-logo5 stud10 stud13 stud15 stud17 stud17a
       stud2 stud2-logo stud2-logo2 stud2-logo3 stud2-logo4 stud2-logo5 stud26 stud2a stud6 stud6a
       stud9 studa studel studline studp01 studx studxa""".split()
)

# "Stud Tube ..." and "Stud Underside ...": the socket underneath. Read for the
# same cost as the studs, and not yet served - a tube sits *between* four studs
# rather than under one, so turning these into anti-stud ports is its own piece
# of work. Until then the anti-stud grid keeps coming from the name.
_STUD_TUBES = frozenset("""stud12 stud12a stud12s stud16 stud16a stud16od stud18a stud21a stud22a stud23 stud23d stud2s
       stud2s2 stud3 stud3a stud4 stud4a stud4f1n stud4f1s stud4f1w stud4f2n stud4f2s stud4f2w
       stud4f3n stud4f3s stud4f4n stud4f4s stud4f5n stud4h stud4o stud4od stud4s stud4s2""".split())

# "Stud Group A x B" and its relatives: A studs along Z by B along X on the
# 20 LDU grid, centred on the origin, exactly the A x B of a brick. Expanded
# here from the name rather than fetched, which is what keeps a group a leaf.
# A group is named after what it groups: "stug<x>-AxB" is a grid of "stud<x>",
# for every <x> LDraw uses - "" and "2" and "el" and "p01" for the male studs,
# "3" and "4" for the tubes, "10" and "15" for the ones cut for a round 2 x 2,
# and "7", "8", "19", "20" for Duplo and Scala. So the kind is looked up rather
# than listed again, which is what keeps a group belonging to another building
# system out without having to name it twice.
_STUD_GROUP_RE = re.compile(r"^stug([0-9a-z]*)-(\d+)x(\d+)$", re.IGNORECASE)
# "stug4.dat" is not a group of four: it is LDraw's alias for "stug-4x4", and
# "stug4a.dat" for "stug2-4x4".
_STUD_GROUP_ALIAS_RE = re.compile(r"^stug(\d+)(a?)$", re.IGNORECASE)
_LDU_STUD_PITCH = 20.0  # the stud grid in LDraw units (8 mm)
_ORIGIN_ONLY = ((0.0, 0.0, 0.0),)
_STUD_AXIS = (0.0, -1.0, 0.0)  # a stud points along -Y, which is up in LDraw

# The six axis-aligned facings, so a stud that points the way the name rule
# would have pointed it comes out with the very same orientation.
_AXIS_ORIENTATIONS = {
    (1, 0, 0): _Z_TO_PLUS_X,
    (-1, 0, 0): _Z_TO_MINUS_X,
    (0, 1, 0): _Z_TO_PLUS_Y,
    (0, -1, 0): _Z_TO_MINUS_Y,
    (0, 0, 1): _Z_TO_PLUS_Z,
    (0, 0, -1): _Z_TO_MINUS_Z,
}


def _ldu_grid(n):
    """The n stud centres along one axis of a group, in LDraw units."""
    return [(i - (n - 1) / 2.0) * _LDU_STUD_PITCH for i in range(n)]


def _stud_primitive(base):
    """('stud' or 'tube', offsets) for a stud primitive, else (None, None).

    'offsets' are where the studs sit inside the primitive, in LDraw units: the
    origin for a single stud, the whole grid for a group.
    """
    if not base.endswith(".dat"):
        return None, None
    stem = base[: -len(".dat")].lower()
    group = _STUD_GROUP_RE.match(stem)
    if group:
        kind = _stud_kind("stud" + group.group(1).lower())
        depth, length = int(group.group(2)), int(group.group(3))
        if kind is None or not (1 <= depth <= _MAX_STUDS and 1 <= length <= _MAX_STUDS):
            return None, None
        return kind, tuple((x, 0.0, z) for x in _ldu_grid(length) for z in _ldu_grid(depth))
    alias = _STUD_GROUP_ALIAS_RE.match(stem)
    if alias:
        side = int(alias.group(1))
        if not (1 <= side <= _MAX_STUDS):
            return None, None
        return "stud", tuple((x, 0.0, z) for x in _ldu_grid(side) for z in _ldu_grid(side))
    kind = _stud_kind(stem)
    return (kind, _ORIGIN_ONLY) if kind else (None, None)


def _stud_kind(stem):
    """'stud', 'tube' or None, for a stud primitive's name without its suffix."""
    if stem.startswith(_LOWRES_STUD_PREFIX):
        stem = "stud" + stem[len(_LOWRES_STUD_PREFIX) :]
    if stem in _MALE_STUDS:
        return "stud"
    if stem in _STUD_TUBES:
        return "tube"
    return None


def _geometry_studs(pid):
    """The male studs in a part's geometry, as (position, axis) in LDraw
    coordinates, and whether the walk saw the whole part.
    """
    found = []

    def visit(base, composed, position):
        """Claim a stud primitive, recording the male ones where they land."""
        kind, offsets = _stud_primitive(base)
        if kind is None:
            return False
        if kind == "stud":
            for offset in offsets:
                moved = _matrix_apply(composed, offset)
                found.append(
                    (
                        tuple(position[i] + moved[i] for i in range(3)),
                        _matrix_apply(composed, _STUD_AXIS),
                    )
                )
        return True  # a stud primitive is a leaf whichever kind it is

    return found, _walk_geometry(pid, visit)


def _stud_orientation(direction):
    """The orientation of a stud facing 'direction' in the meshed part space."""
    length = math.sqrt(sum(v * v for v in direction))
    if length < 1e-9:
        return None
    unit = tuple(v / length for v in direction)
    key = tuple(round(v) for v in unit)
    if key in _AXIS_ORIENTATIONS and all(abs(unit[i] - key[i]) < 1e-6 for i in range(3)):
        return _AXIS_ORIENTATIONS[key]
    return _orientation_towards(direction, _IDENTITY_MATRIX)


def _geometry_stud_implements(pid):
    """The 'stud' instances of a part read from its geometry, or None when the
    walk ran out of budget and the name rule should be left to stand.

    An empty result is an answer, not a failure: it says the walk saw the whole
    part and there is no stud on it.
    """
    studs, complete = _geometry_studs(pid)
    if not complete:
        return None
    ports = []
    for position, axis in studs:
        place = (
            round(position[0] * _LDU_MM, 4),
            round(-position[1] * _LDU_MM, 4),
            round(position[2] * _LDU_MM, 4),
        )
        orientation = _stud_orientation((axis[0], -axis[1], axis[2]))
        if orientation is not None:
            ports.append((place, orientation))
    return _stud_instances_by_grid(ports)


def _stud_instances_by_grid(ports):
    """Name studs c<col>r<row> by where they sit, not by the order they turned
    up, so a rectangular part keeps exactly the names it has always had.

    Columns run along X and rows along Z, as they do for a name-derived grid.
    A part whose studs do not fall on one plane can put two of them in the same
    column and row; the second and later ones take a numbered suffix.
    """
    xs = sorted({place[0] for place, _ in ports})
    zs = sorted({place[2] for place, _ in ports})
    instances = {}
    for place, orientation in sorted(ports):
        name = "c%dr%d" % (xs.index(place[0]), zs.index(place[2]))
        if name in instances:
            nth = 2
            while "%s_%d" % (name, nth) in instances:
                nth += 1
            name = "%s_%d" % (name, nth)
        instances[name] = _port(place, orientation)
    return instances


# --- anti-studs read from the geometry ---------------------------------------
#
# The underside is a harder read than the top, because LDraw's tubes do not sit
# where the anti-studs are. "Stud Tube Open" sits at the centre of a 2 x 2 of
# them and "Stud Tube Solid" between two, so the anti-studs are the corners
# around a tube rather than the tube itself. Which two a solid tube separates is
# not in its matrix - LDraw places every one of them the same way up - so the
# part's own studs say which axis, and a tube whose neighbours are not both on
# that lattice is not guessed at.
#
# The same primitives mean something else on a minifig's hat, where a single
# flipped open tube *is* the socket rather than the spacer between four. That is
# why this only runs on a part that has studs of its own to check the lattice
# against; the headgear rule below handles the other reading explicitly.
_SOLID_TUBES = ("stud3", "stud3a")  # "Stud Tube Solid": sits between two
_UNDERSIDE_CROSS = "stud12"  # "Stud Underside Cross": a different feature
_ANTI_PLANE_OFFSET = (0.0, -4.0, 0.0)  # a tube spans y in [-4, 0] in its own frame


def _tube_member(base):
    """The primitive a stud name stands for; a group resolves to what it groups."""
    stem = base[: -len(".dat")].lower() if base.lower().endswith(".dat") else base.lower()
    group = _STUD_GROUP_RE.match(stem)
    if group:
        stem = "stud" + group.group(1).lower()
    else:
        alias = _STUD_GROUP_ALIAS_RE.match(stem)
        if alias:
            stem = "stud2" if alias.group(2) else "stud"
    if stem.startswith(_LOWRES_STUD_PREFIX):
        stem = "stud" + stem[len(_LOWRES_STUD_PREFIX) :]
    return stem


def _geometry_anti_studs(pid):
    """The anti-stud instances of a part read from its underside tubes, or None
    when the geometry does not settle it and the name should be left to stand.

    None rather than an empty answer on purpose: unlike a stud, whose absence
    the walk can see, an anti-stud that no tube marks may still be there.
    """
    studs, tubes = [], []

    def visit(base, composed, position):
        """Claim a stud primitive, keeping the tubes and where the studs are."""
        kind, offsets = _stud_primitive(base)
        if kind is None:
            return False
        for offset in offsets:
            moved = _matrix_apply(composed, offset)
            place = tuple(position[i] + moved[i] for i in range(3))
            (studs if kind == "stud" else tubes).append((_tube_member(base), place, composed))
        return True

    if not _walk_geometry(pid, visit) or not tubes or not studs:
        return None

    lattice = {(round(p[0], 1), round(p[2], 1)) for _, p, _ in studs}
    half = _LDU_STUD_PITCH / 2.0
    found = {}
    for stem, place, composed in tubes:
        if stem == _UNDERSIDE_CROSS:
            return None  # not a tube between studs; do not guess at the rest
        far = _matrix_apply(composed, _ANTI_PLANE_OFFSET)
        # A tube must run straight down the part, or this is not an underside.
        if abs(far[0]) > 1e-6 or abs(far[2]) > 1e-6 or far[1] <= 0:
            return None
        y = place[1] + far[1]
        x, z = round(place[0], 1), round(place[2], 1)
        if stem in _SOLID_TUBES:
            along_x = {(round(x - half, 1), z), (round(x + half, 1), z)}
            along_z = {(x, round(z - half, 1)), (x, round(z + half, 1))}
            if along_x <= lattice and not along_z <= lattice:
                corners = along_x
            elif along_z <= lattice and not along_x <= lattice:
                corners = along_z
            else:
                return None  # the studs do not say which two this one separates
        else:
            corners = {(round(x + dx, 1), round(z + dz, 1)) for dx in (-half, half) for dz in (-half, half)}
        for corner in corners:
            found[corner] = y
    ports = [
        (
            (
                round(x * _LDU_MM, 4),
                round(-y * _LDU_MM, 4),
                round(z * _LDU_MM, 4),
            ),
            _ANTI_STUD_ROT,
        )
        for (x, z), y in found.items()
    ]
    return _stud_instances_by_grid(ports)


# --- headgear ----------------------------------------------------------------
#
# A hat, helmet or hairpiece attaches by one socket over the head's stud, which
# would be an ordinary anti-stud at the part's origin. It was left out while the
# rules were names alone, because no name rule gets past ~96% of these 369
# parts. The geometry says it outright: LDraw draws the socket with a single
# *open* tube, flipped, whose far end is the part's own origin - the opposite
# reading of the same primitive from a brick's underside, where an open tube is
# the spacer between four anti-studs. The name picks the family and the
# geometry confirms the socket, so neither has to be trusted alone.
_HEADGEAR_RE = re.compile(r"^Minifig (?:Hat|Headdress|Helmet|Cap|Hair|Headgear)\b", re.IGNORECASE)


def _headgear_implements(pid):
    """The anti-stud of a piece of headgear, read from its socket, or None."""
    tubes, studs = [], []

    def visit(base, composed, position):
        """Claim a stud primitive, keeping the tubes and noting any male stud."""
        kind, offsets = _stud_primitive(base)
        if kind is None:
            return False
        for offset in offsets:
            moved = _matrix_apply(composed, offset)
            place = tuple(position[i] + moved[i] for i in range(3))
            (studs if kind == "stud" else tubes).append((_tube_member(base), place, composed))
        return True

    if not _walk_geometry(pid, visit) or studs or len(tubes) != 1:
        return None
    stem, place, composed = tubes[0]
    if stem in _SOLID_TUBES or stem == _UNDERSIDE_CROSS:
        return None
    far = _matrix_apply(composed, _ANTI_PLANE_OFFSET)
    mouth = tuple(place[i] + far[i] for i in range(3))
    # The socket has to open at the part's own origin, which is what makes it
    # the thing that goes over a head rather than a tube inside a body.
    if any(abs(v) > 1.0 for v in mouth):
        return None
    return {_ANTI_IFACE: {"anti": _port((0.0, 0.0, 0.0), _ANTI_STUD_ROT)}}


# The families beyond Technic, each derived from the name in the same way. They
# are kept apart from the Technic table only because they are a different half
# of the LEGO system, not because they work differently.
_OTHER_RULES = (
    (_GEAR_RE, _gear_implements),
    (_DUPLO_RE, _duplo_implements),
    (_MINIFIG_HEAD_RE, _minifig_head),
    (_MINIFIG_TORSO_RE, _minifig_torso),
    (_MINIFIG_HIPS_RE, _minifig_hips),
    (_TYRE_RE, _tyre_implements),
    (_WHEEL_RE, _wheel_implements),
)
