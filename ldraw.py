#
# The ":ldraw" partType wrapper.
#
# Runs inside the PartCAD sandbox (see wrappers/wrapper_part_type.py) with the
# part 'request' in globals and the run name '__partcad_part__'. It resolves an
# LDraw part (.dat) from the LDraw parts library, recursively meshes it into
# triangles, writes an STL, and imports it with build123d to return the part's
# shape (the same mesh-import path the OpenSCAD factory uses, which renders
# reliably - unlike a hand-built raw triangulation face).
#
# The LDraw source is fetched on demand from https://library.ldraw.org and
# cached locally; nothing is vendored.
#
# LDraw stores no normals. Which side of a surface is its outside is given
# entirely by the order the vertices are written in, together with the BFC meta
# statements that say what that order means, so a mesh built without reading
# them is not a solid even when it looks like one. See _mesh().
#
# Nor is that surface closed as drawn: LDraw stands a stud on a face it does
# not cut, writes the same corner to four decimals down two different paths,
# and joins a 16-sided wall to a 48-sided floor. A mesh import of it therefore
# comes back as a SHELL, or as a SOLID that fails BRepCheck, and every boolean
# taken against either is meaningless. Closing it is _close_mesh(), and
# building the solid the closed mesh describes is _solid_from_mesh().
#
import math
import os
import re
import struct
import time
import urllib.error
import urllib.request

_LDRAW_BASE = "https://library.ldraw.org/library"
_LDRAW_SUBDIRS = ["official/parts", "official/p", "unofficial/parts", "unofficial/p"]
_LDRAW_UA = "Mozilla/5.0 (PartCAD ldraw partType)"
# 1 LDraw Unit = 0.4 mm; LDraw uses -Y as up, so flip Y for a Z-is-up render.
_LDU_MM = 0.4
# A dropped request costs geometry rather than time, so retry before giving up.
_FETCH_RETRIES = 4
_FETCH_BACKOFF = 0.5
# How long one request may take, and how long the whole search for one file may
# take however many requests that is. The second is the one that matters: four
# attempts over four subdirectories is sixteen requests, and at sixty seconds
# each a single file the server neither serves nor refuses costs sixteen
# minutes - inside a part that references hundreds of them, inside a 'pc test'
# with a deadline of its own. The budget is far above a healthy fetch (well
# under a second) and far below that.
_FETCH_TIMEOUT = 60
_FETCH_SECONDS = 120.0

# Why a failed fetch is remembered, and why the two kinds are remembered for
# different lengths of time. This mirrors 'ldraw_repo.py', deliberately and of
# necessity: this file is served to the PartCAD sandbox on its own (the
# 'files/ldraw.py' key), with nothing beside it to import, so the two cannot
# share one implementation. Keep them in step.
#
# A 404 is an answer - the library has not got this file - and is worth
# remembering for a while. A timeout or a 429 is the absence of an answer, and
# is worth retrying soon. Telling them apart is what stops a throttled run
# reporting a part that is in the library as missing from it, which is what
# "LDraw part not found in the library: 3020.dat" was: Plate 2 x 4, fetched
# while ldraw.org was refusing the burst this very function had just made.
_MISSING = "missing"  # the server said no such file
_UNAVAILABLE = "unavailable"  # the server said nothing we could use
_NEGATIVE_TTL = {_MISSING: 7 * 24 * 3600, _UNAVAILABLE: 300}

# A primitive is a file under the library's 'p/' rather than its 'parts/', and
# knowing which to ask first halves the requests a part's references cost: a
# 2 x 2 round brick is mostly primitives, and every one of them used to be
# asked of 'official/parts' before 'official/p'. The three forms are the
# fraction primitives ('3-16ndis.dat', '4-4cyli.dat'), the resolution
# subdirectories ('48/4-4disc.dat', '8/...'), and nothing else - a name this
# does not recognise keeps the order it always had, and every subdirectory is
# still tried either way, so a wrong guess costs one request and never an
# answer.
_PRIMITIVE_RE = re.compile(r"^(?:\d+-\d+|(?:48|8)/)")


class LDrawNotSolid(Exception):
    """A part whose surface could not be turned into a solid.

    Raised rather than handing back the surface: a shell measures, renders and
    exports like the part, and every boolean taken against it - interference,
    mass, a cut - answers with a number that means nothing. A part that fails
    says so; one that is wrong says nothing.
    """


class LDrawSubfileMissing(Exception):
    """A subfile or primitive a part is built from could not be fetched."""

    def __init__(self, name):
        super().__init__("LDraw subfile could not be fetched: %s" % name)
        self.name = name


def _ldraw_cache_dir():
    base = os.environ.get("PARTCAD_LDRAW_CACHE")
    if not base:
        xdg = os.environ.get("XDG_CACHE_HOME") or os.path.join(os.path.expanduser("~"), ".cache")
        base = os.path.join(xdg, "partcad-ldraw")
    return base


def _negative_path(cache, key):
    return os.path.join(cache, ".missing", key)


def _negative_is_fresh(cache, key):
    """Whether this was already asked for recently enough not to ask again."""
    try:
        with open(_negative_path(cache, key), "r", encoding="latin-1") as f:
            reason, expires = f.read().split(None, 1)
        return time.time() < float(expires)
    except (OSError, ValueError):
        return False


def _remember_negative(cache, key, reason):
    """Record that this file could not be had, and for how long to believe it."""
    path = _negative_path(cache, key)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="latin-1") as f:
            f.write("%s %f" % (reason, time.time() + _NEGATIVE_TTL[reason]))
    except OSError:
        pass  # an unwritable cache is a slow run, not a broken one


def _forget_negative(cache, key):
    try:
        os.unlink(_negative_path(cache, key))
    except OSError:
        pass


def _retry_after(error, default):
    """How long a 429 asked us to wait, in seconds; 'default' if it did not say."""
    value = error.headers.get("Retry-After") if error.headers else None
    if not value:
        return default
    try:
        return min(float(value), 60.0)  # a delay-seconds form; ignore absurd ones
    except (TypeError, ValueError):
        return default  # an HTTP-date form, which is not worth parsing here


def _subdir_order(key):
    """The subdirectories to search, likeliest first. Always all four of them."""
    if not _PRIMITIVE_RE.match(key):
        return _LDRAW_SUBDIRS
    return sorted(_LDRAW_SUBDIRS, key=lambda sub: not sub.endswith("/p"))


def _ldraw_get(key, deadline=None):
    """Fetch one LDraw file from the library. Returns (text, None) or (None, reason).

    The reason separates 'the library has not got this' from 'the library did
    not answer', because the two are worth remembering for different lengths of
    time and because only the first is true of the part. Before this told them
    apart, a burst that ldraw.org throttled was reported as a part that does not
    exist - and nothing downstream could tell the difference either.

    A 404 settles one subdirectory and stops it being asked again; a file all
    four have refused is missing, and is answered without another round. Only
    the inconclusive answers - a 429, a timeout, a reset - are retried, and a
    429 is waited out for as long as the server asks.
    """
    subdirs = _subdir_order(key)
    missing = set()
    if deadline is None:
        deadline = time.monotonic() + _FETCH_SECONDS
    for attempt in range(_FETCH_RETRIES):
        delay = _FETCH_BACKOFF * (attempt + 1)
        for sub in subdirs:
            if sub in missing:
                continue
            if time.monotonic() > deadline:
                return None, _UNAVAILABLE
            url = "%s/%s/%s" % (_LDRAW_BASE, sub, key)
            try:
                req = urllib.request.Request(url, headers={"User-Agent": _LDRAW_UA})
                with urllib.request.urlopen(req, timeout=_FETCH_TIMEOUT) as resp:
                    if resp.status == 200:
                        return resp.read().decode("latin-1"), None
            except urllib.error.HTTPError as e:
                if e.code == 404:
                    missing.add(sub)  # an answer, not a failure: do not ask again
                    continue
                if e.code == 429:
                    delay = max(delay, _retry_after(e, delay))
            except Exception:
                pass
        if len(missing) == len(subdirs):
            return None, _MISSING
        if attempt + 1 < _FETCH_RETRIES:
            time.sleep(delay)
    return None, _UNAVAILABLE


def _fetch_failure(name, cache):
    """Why the last fetch of 'name' produced nothing, or None if it did not say.

    'The library has not got this part' and 'the library would not answer' are
    different things to be told. The second is the one a log needs to show as
    what it is: it means the run was throttled or the site was down, not that
    somebody referenced a part that does not exist - and a reader who is told
    the wrong one of those goes looking in the wrong place. The answer is read
    back out of the negative entry '_ldraw_fetch' has just written, so that
    there is one place the distinction is made.
    """
    key = name.replace("\\", "/").lower()
    try:
        with open(_negative_path(cache, key), "r", encoding="latin-1") as f:
            return f.read().split(None, 1)[0]
    except (OSError, IndexError):
        return None


def _ldraw_fetch(name, cache):
    """Return the text of an LDraw file, fetching+caching it on first use.

    Retried with a backoff, because a part is mostly references and a dropped
    request means a hole in the geometry rather than a slower render:
    library.ldraw.org rate-limits bursts, and a 2 x 2 round brick that loses
    4-4cyli.dat meshes into a flat disc.

    A fetch that produced nothing is remembered too, under '<cache>/.missing',
    so that a throttled run leaves something behind and the next reference to
    the same file - in this part, in the next part, in the next process - is not
    the same burst again. Without it a rate-limited machine never converges:
    every part re-asks for every primitive the one before it could not get, at
    up to sixteen requests a time.
    """
    key = name.replace("\\", "/").lower()
    cached = os.path.join(cache, key)
    if os.path.exists(cached):
        with open(cached, "r", encoding="latin-1") as f:
            return f.read()
    if _negative_is_fresh(cache, key):
        return None
    data, reason = _ldraw_get(key)
    if data is None:
        _remember_negative(cache, key, reason)
        return None
    _forget_negative(cache, key)
    try:
        os.makedirs(os.path.dirname(cached), exist_ok=True)
        with open(cached, "w", encoding="latin-1") as f:
            f.write(data)
    except OSError:
        pass  # an unwritable cache is a slow run, not a broken one
    return data


_IDENT = ((1, 0, 0), (0, 1, 0), (0, 0, 1))


def _mat_mul(a, b):
    return tuple(tuple(sum(a[i][k] * b[k][j] for k in range(3)) for j in range(3)) for i in range(3))


def _det(m):
    """The determinant of an orientation matrix, whose sign is its handedness.

    LDraw makes a mirrored copy of a subfile by placing it with a mirrored
    matrix rather than by writing a mirrored file, so a negative determinant
    is the ordinary way a part gets its left half and not a rarity: 8 of the
    12 references in 2357.dat, Brick 2 x 2 Corner, are mirrored.
    """
    return (
        m[0][0] * (m[1][1] * m[2][2] - m[1][2] * m[2][1])
        - m[0][1] * (m[1][0] * m[2][2] - m[1][2] * m[2][0])
        + m[0][2] * (m[1][0] * m[2][1] - m[1][1] * m[2][0])
    )


def _mat_vec(a, v):
    return tuple(sum(a[i][k] * v[k] for k in range(3)) for i in range(3))


def _compose(pm, pt, cm, ct):
    m = _mat_mul(pm, cm)
    mv = _mat_vec(pm, ct)
    return m, (mv[0] + pt[0], mv[1] + pt[1], mv[2] + pt[2])


def _xform(m, t, p):
    r = _mat_vec(m, p)
    # Still LDraw units on LDraw's axes: _scaled() turns them upright and
    # converts to millimetres, at STL time.
    return (r[0] + t[0], r[1] + t[1], r[2] + t[2])


def _mesh(text, m, t, tris, cache, invert=False, uncertified=None):
    """Mesh one LDraw file into 'tris', resolving BFC winding as it goes.

    Reading the vertices in the order they are written and leaving it at that
    gets a mesh whose triangles disagree with each other about which way is
    out. That looks right and measures a right bounding box, which is why it
    went unnoticed, but it is not a solid: Brick 2 x 4 came out with a volume
    of -1939.6 mm^3, failed BRepCheck_Analyzer, and shared -2282.9 mm^3 with a
    copy of itself standing 100 mm away.

    Three things reverse the sense of a file's winding, and they compound. The
    file may declare itself CW rather than CCW. The parent may have asked for
    this reference with '0 BFC INVERTNEXT'. The matrix that placed it may be a
    mirror. What matters is only whether they add up to a reversal, so each is
    a boolean and 'flip' is the exclusive-or of the three.

    'invert' carries the INVERTNEXT half of that down the reference branch,
    because the spec has it accumulate - a subfile of an inverted file is
    itself inverted. The mirror half needs no carrying: the determinant of the
    accumulated matrix is already the product of every determinant above it.

    What this does not get is a closed surface, and it is worth being plain
    about that. LDraw draws a stud as a cylinder with no bottom, standing on a
    face it does not cut, and a stud tube the same way, so a part is left with
    an open ring at every one of them - 224 of Brick 2 x 4's 2100 edges. The
    winding is consistent everywhere now, which is what makes a boolean mean
    something; whether a kernel then sews a watertight solid out of a mesh with
    those rings in it is still the kernel's business and sometimes it does not.

    The specification is https://www.ldraw.org/article/415.html.
    """
    # A mirrored placement turns everything drawn beneath it inside out.
    mirrored = _det(m) < 0.0
    # CCW is the winding CERTIFY implies when it names none, and what all but
    # 28 of the 3353 files on hand declare. A file that certifies nothing has
    # no winding worth trusting: it is read as CCW, which is right wherever its
    # author happened to be consistent, and 'uncertified' is set so that
    # '_orient_consistently' can settle it from the mesh instead.
    winding_cw = False
    nocertify = False
    invertnext = False
    certified = False
    for line in text.splitlines():
        f = line.split()
        if not f:
            continue
        code = f[0]
        if code == "0":
            opts = [w.upper() for w in f[1:]]
            # Once a file has said NOCERTIFY its other BFC statements are to
            # be ignored, so the flags stop moving and only the mirror and the
            # inherited inversion still apply to it.
            if opts[:1] != ["BFC"] or nocertify:
                continue
            if "NOCERTIFY" in opts:
                nocertify = True
            if "CERTIFY" in opts:
                certified = True
            if "INVERTNEXT" in opts:
                invertnext = True
            # CERTIFY carries the winding for the file, and a bare CW or CCW
            # later on changes it for the polygons that follow. Both spellings
            # arrive here the same way, which is what the spec asks for.
            if "CW" in opts:
                winding_cw = True
            elif "CCW" in opts:
                winding_cw = False
            # CLIP and NOCLIP say whether a renderer may discard back faces.
            # They say nothing about winding and we discard nothing, so they
            # are read and dropped on purpose rather than by omission.
            continue
        flip = invert ^ mirrored ^ winding_cw
        if code == "1" and len(f) >= 15:
            v = list(map(float, f[2:14]))
            cm = (tuple(v[3:6]), tuple(v[6:9]), tuple(v[9:12]))
            ct = (v[0], v[1], v[2])
            nm, nt = _compose(m, t, cm, ct)
            subname = " ".join(f[14:])
            subtext = _ldraw_fetch(subname, cache)
            if subtext is None:
                # Never mesh on regardless: an LDraw part is mostly references,
                # so a subfile that cannot be read is a missing piece of the
                # part, and skipping it quietly returns a wrong shape that
                # looks like a right one - and gets cached as such.
                raise LDrawSubfileMissing(subname)
            _mesh(subtext, nm, nt, tris, cache, invert ^ invertnext, uncertified)
        elif code == "3" and len(f) >= 11:
            v = list(map(float, f[2:11]))
            p1, p2, p3 = v[0:3], v[3:6], v[6:9]
            if flip:
                p2, p3 = p3, p2
            tris.append((_xform(m, t, p1), _xform(m, t, p2), _xform(m, t, p3)))
        elif code == "4" and len(f) >= 14:
            v = list(map(float, f[2:14]))
            p1, p2, p3, p4 = v[0:3], v[3:6], v[6:9], v[9:12]
            if flip:
                # Reverse the quad and then split it, rather than reversing
                # the two halves on their own: they share the p1-p3 diagonal
                # and have to go on agreeing about it.
                p2, p4 = p4, p2
            tris.append((_xform(m, t, p1), _xform(m, t, p2), _xform(m, t, p3)))
            tris.append((_xform(m, t, p1), _xform(m, t, p3), _xform(m, t, p4)))
        # INVERTNEXT reaches exactly one subfile reference. Clearing it after
        # every operational line, and not only after a type 1, is what keeps a
        # stray one from leaking into a later reference.
        invertnext = False

    # Decided once the whole file has been read: 'CERTIFY' may come after the
    # first polygon, and 'NOCERTIFY' settles it whatever else the file says.
    if uncertified is not None and (nocertify or not certified):
        uncertified.append(True)


def _scaled(p):
    # 1 LDU is 0.4 mm, and LDraw's up is -Y. Negating Y makes up +Y; a further
    # quarter turn about X makes it +Z, which is where PartCAD's is, so the part
    # comes out standing on the XY plane the way every other PartCAD part does.
    # An assembly that uses one no longer has to turn it itself. Written out,
    # the two steps together are (x, -z, -y).
    #
    # This is still a reflection, exactly as negating Y alone was - the vertex
    # swap in _write_binary_stl() answers for it and goes on answering for it.
    return (p[0] * _LDU_MM, -p[2] * _LDU_MM, -p[1] * _LDU_MM)


def _normal(a, b, c):
    """The unit normal of a triangle, by the right-hand rule on its winding.

    Computed from the three vertices rather than carried from anywhere, so the
    normal an STL facet records cannot come to disagree with the order of the
    vertices written beside it. That is worth having because importers differ
    on which of the two they believe.
    """
    ux, uy, uz = b[0] - a[0], b[1] - a[1], b[2] - a[2]
    vx, vy, vz = c[0] - a[0], c[1] - a[1], c[2] - a[2]
    nx, ny, nz = uy * vz - uz * vy, uz * vx - ux * vz, ux * vy - uy * vx
    length = (nx * nx + ny * ny + nz * nz) ** 0.5
    if length < 1e-9:
        return None  # degenerate
    return (nx / length, ny / length, nz / length)


# ---------------------------------------------------------------------------
# Closing the surface.
#
# What _mesh() hands over is a consistent surface but not a closed one, and a
# kernel will not make a solid out of a surface with holes in it. The holes
# are of three kinds and they want three different answers.
#
# The first is rounding. LDraw writes coordinates to four or five decimals and
# a reference scales them, so the same corner reached through two primitives
# comes out as (-5.5433, 12, -2.2961) down one path and (-5.5434, 12, -2.2962)
# down the other. _weld() puts those back together.
#
# The second is a resolution seam. A part that draws its wall with a 16-sided
# cylinder and its floor with a 48-sided disc leaves every fourth vertex of
# the floor sitting in the middle of a wall edge rather than at its end.
# _split_t_junctions() cuts the edge at the vertex so the two agree.
#
# The third is a hole LDraw means: a stud is a cylinder with no bottom
# standing on a face that is not cut, so the part carries an open ring at
# every stud and every stud tube. _cap_planar_loops() fills those - and fills
# them as regions rather than as discs, because an underside leaves a ring
# between two circles and a round brick leaves four slivers between a circle
# and a rounded square, and a disc over either of those is wrong.
#
# What is left after that is a set of closed shells that overlap: the body,
# a shell per stud standing on it, a shell per tube inside it, and sometimes
# a shell around a cavity, which LDraw draws facing inward and which is a
# hole rather than a body. _solid_from_mesh() reads that facing off the sign
# of each shell's volume and fuses or cuts accordingly, largest first, so a
# tube inside a cavity survives the cavity being taken out.
#
# A part this cannot close is handed back to the mesh import unchanged.

# Two vertices this close are one vertex. 0.05 LDU is 20 micrometres, which is
# far below anything LDraw draws and far above the rounding above.
_WELD_LDU = 0.05
# A vertex this close to an edge is taken to be on it. Seams between
# primitives of different resolution are wider than plain rounding, which is
# why this is its own, looser number.
_TJUNCTION_LDU = 0.2
# A boundary loop that departs from its own plane by more than this is left
# open rather than filled with a guess.
_PLANAR_LDU = 0.05
# By the time the mesh is sewn its triangles share vertices exactly, so the
# kernel is given only enough room for the conversion to millimetres.
_SEW_TOL_MM = 1e-4
# How far from flat a hole the plain capping left may be and still be capped
# on the way to a solid, by a fan from its centre. A Technic friction pin
# leaves its ridges' footprints open, 0.26 LDU off flat; the side holes of the
# Power Functions servo leave loops 9.7 LDU off flat, which are the walls of a
# pin hole's counterbore and nothing a fan describes - capping those would
# seal the hole a pin goes into. 0.5 LDU is 0.2 mm.
_SHALLOW_LDU = 0.5
# How wide a hole may be and still be stitched shut, however far from flat it
# runs. Most of what is left open is not a missing face but a seam: two of
# LDraw's surfaces that meet a fraction of a millimetre apart rather than on
# shared vertices, typically where a 16-sided circle meets a 48-sided one and
# the chord stands off the arc by more than _TJUNCTION_LDU. Measured on the
# parts of the LEGO F1 car, those seams are 0.56 to 0.98 LDU across and
# anything wider (1.2 LDU up) is a face that is missing. Stitching moves no
# surface; the triangles it adds span the gap and are no further from either
# side than the gap is wide. 1 LDU is 0.4 mm.
_SEAM_LDU = 1.0
# How far a solid built from regions may stray from the volume the surface
# itself encloses before it is taken to have lost (or gained) part of the
# part: the larger of this fraction and three standard errors of the sample.
_VOLUME_SLACK = 0.1
_VOLUME_SAMPLES = 2000


def _weld(tris, tol):
    """Merge vertices that differ only in LDraw's last written digit."""
    cell = tol * 2.0
    grid = {}

    def rep(p):
        k = (int(math.floor(p[0] / cell)), int(math.floor(p[1] / cell)), int(math.floor(p[2] / cell)))
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for dz in (-1, 0, 1):
                    for r in grid.get((k[0] + dx, k[1] + dy, k[2] + dz), ()):
                        if abs(r[0] - p[0]) <= tol and abs(r[1] - p[1]) <= tol and abs(r[2] - p[2]) <= tol:
                            return r
        grid.setdefault(k, []).append(p)
        return p

    out = []
    for a, b, c in tris:
        a, b, c = rep(a), rep(b), rep(c)
        # Welding is what turns a sliver into nothing, so a triangle that has
        # lost a vertex to it is dropped here rather than carried along.
        if a == b or b == c or a == c:
            continue
        out.append((a, b, c))
    return out


def _half_edges(tris):
    """Map every directed edge to the triangles that walk it that way."""
    he = {}
    for i, t in enumerate(tris):
        for j in range(3):
            he.setdefault((t[j], t[(j + 1) % 3]), []).append(i)
    return he


def _unmatched(he):
    """The directed edges with no triangle walking them back: the boundary."""
    free = {}
    for e, owners in he.items():
        opposite = len(he.get((e[1], e[0]), ()))
        if len(owners) > opposite:
            free[e] = len(owners) - opposite
    return free


def _point_on_segment(p, a, b, tol):
    """Where p falls along a-b, if it is on it and not at either end."""
    dx, dy, dz = b[0] - a[0], b[1] - a[1], b[2] - a[2]
    length = dx * dx + dy * dy + dz * dz
    if length < 1e-18:
        return None
    t = ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy + (p[2] - a[2]) * dz) / length
    if t <= 1e-6 or t >= 1.0 - 1e-6:
        return None
    qx, qy, qz = a[0] + t * dx, a[1] + t * dy, a[2] + t * dz
    if (p[0] - qx) ** 2 + (p[1] - qy) ** 2 + (p[2] - qz) ** 2 > tol * tol:
        return None
    return t


def _split_t_junctions(tris, tol, passes=3):
    """Cut a boundary edge wherever another boundary vertex sits on it.

    A 2 x 2 round brick draws its wall as a 16-sided cylinder and the plate
    below it with a vertex at every 22.5 degrees plus four more on the axes.
    The four extra ones land in the middle of a wall edge, so the wall and the
    plate share a line without sharing edges and nothing sews. Splitting the
    wall edge at them costs four triangles and closes the seam.
    """
    split = 0
    for _ in range(passes):
        he = _half_edges(tris)
        free = _unmatched(he)
        if not free:
            break
        cell = max(tol, 1e-9) * 2.0
        grid = {}
        for e in free:
            for v in e:
                grid.setdefault((int(v[0] // cell), int(v[1] // cell), int(v[2] // cell)), set()).add(v)
        cuts = {}
        for a, b in free:
            lo = [min(a[k], b[k]) - tol for k in range(3)]
            hi = [max(a[k], b[k]) + tol for k in range(3)]
            found = []
            for gx in range(int(lo[0] // cell), int(hi[0] // cell) + 1):
                for gy in range(int(lo[1] // cell), int(hi[1] // cell) + 1):
                    for gz in range(int(lo[2] // cell), int(hi[2] // cell) + 1):
                        for v in grid.get((gx, gy, gz), ()):
                            if v is a or v is b:
                                continue
                            t = _point_on_segment(v, a, b, tol)
                            if t is not None:
                                found.append((t, v))
            if found:
                found.sort()
                cuts[(a, b)] = [v for _, v in found]
        if not cuts:
            break
        out = []
        for t in tris:
            pieces = None
            for j in range(3):
                e = (t[j], t[(j + 1) % 3])
                if e in cuts:
                    chain = [e[0]] + cuts[e] + [e[1]]
                    apex = t[(j + 2) % 3]
                    pieces = [(chain[k], chain[k + 1], apex) for k in range(len(chain) - 1)]
                    split += len(pieces) - 1
                    break
            out.extend(pieces if pieces else [t])
        tris[:] = out
    return split


def _split_pinched(loop):
    """Split a loop that runs through the same point twice into simple ones."""
    out = []
    stack = []
    at = {}
    for v in loop:
        if v in at:
            i = at[v]
            sub = stack[i:]
            for w in sub[1:]:
                at.pop(w, None)
            del stack[i:]
            if len(sub) >= 3:
                out.append(sub)
        at[v] = len(stack)
        stack.append(v)
    if len(stack) >= 3:
        out.append(stack)
    return out


def _boundary_loops(tris):
    """Chain the unmatched half-edges into loops around each hole.

    Which edge continues the boundary is a question about the surface and not
    about the list: at a vertex where several holes meet, the one that follows
    is found by turning around that vertex through the triangles that do exist
    until the next edge that has nothing on its far side.
    """
    he = _half_edges(tris)
    free = _unmatched(he)
    remaining = dict(free)

    def following(a, b):
        cur = (a, b)
        for _ in range(256):
            owners = he.get(cur)
            if not owners:
                return None
            t = tris[owners[0]]
            third = t[(t.index(cur[0]) + 2) % 3]
            if (b, third) in free:
                return (b, third)
            cur = (third, b)
        return None

    loops = []
    while True:
        start = None
        for e, n in remaining.items():
            if n:
                start = e
                break
        if start is None:
            break
        loop = []
        cur = start
        closed = True
        while True:
            if not remaining.get(cur):
                closed = False
                break
            remaining[cur] -= 1
            loop.append(cur[0])
            nxt = following(cur[0], cur[1])
            if nxt is None:
                closed = False
                break
            if nxt == start:
                break
            cur = nxt
        if closed:
            loops.extend(L for L in _split_pinched(loop) if len(L) >= 3)
    return loops


def _newell(pts):
    """The area vector of a closed polygon, whatever plane it lies in."""
    nx = ny = nz = 0.0
    n = len(pts)
    for i in range(n):
        a = pts[i]
        b = pts[(i + 1) % n]
        nx += (a[1] - b[1]) * (a[2] + b[2])
        ny += (a[2] - b[2]) * (a[0] + b[0])
        nz += (a[0] - b[0]) * (a[1] + b[1])
    return nx, ny, nz


def _plane_basis(n):
    ax = (1.0, 0.0, 0.0) if abs(n[0]) < 0.9 else (0.0, 1.0, 0.0)
    e1 = (n[1] * ax[2] - n[2] * ax[1], n[2] * ax[0] - n[0] * ax[2], n[0] * ax[1] - n[1] * ax[0])
    length = math.sqrt(e1[0] ** 2 + e1[1] ** 2 + e1[2] ** 2)
    e1 = (e1[0] / length, e1[1] / length, e1[2] / length)
    e2 = (n[1] * e1[2] - n[2] * e1[1], n[2] * e1[0] - n[0] * e1[2], n[0] * e1[1] - n[1] * e1[0])
    return e1, e2


def _area2(poly):
    s = 0.0
    n = len(poly)
    for i in range(n):
        a = poly[i]
        b = poly[(i + 1) % n]
        s += a[0] * b[1] - b[0] * a[1]
    return s / 2.0


def _turn(o, a, b):
    return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])


def _within(p, a, b, c, eps):
    d1, d2, d3 = _turn(a, b, p), _turn(b, c, p), _turn(c, a, p)
    return (d1 > eps and d2 > eps and d3 > eps) or (d1 < -eps and d2 < -eps and d3 < -eps)


def _regions(loops2d):
    """Re-walk coplanar loops as one subdivision of their plane.

    Two loops that run through the same point are not two regions but one
    region pinched there, and which way each of them turns at that point is
    decided by the angles of the edges meeting it rather than by which loop
    they arrived in. A 2 x 2 round brick's underside is the case: a circle and
    a rounded square touching at four corners, with four slivers between them.
    """
    seq = []
    at = {}

    def index(p):
        if p not in at:
            at[p] = len(seq)
            seq.append(p)
        return at[p]

    half = []
    for L in loops2d:
        n = len(L)
        for i in range(n):
            half.append((index(L[i]), index(L[(i + 1) % n])))
    if len(seq) == sum(len(L) for L in loops2d):
        return [list(L) for L in loops2d]  # nothing shared, nothing to re-walk
    out = {}
    for u, v in half:
        out.setdefault(u, []).append(v)
    angle = {}
    for u, vs in out.items():
        for v in vs:
            angle[(u, v)] = math.atan2(seq[v][1] - seq[u][1], seq[v][0] - seq[u][0])
    nxt = {}
    for u, v in half:
        back = math.atan2(seq[u][1] - seq[v][1], seq[u][0] - seq[v][0])
        pick = None
        for w in out.get(v, ()):
            if angle[(v, w)] < back - 1e-12 and (pick is None or angle[(v, w)] > angle[(v, pick)]):
                pick = w
        if pick is None and out.get(v):
            pick = max(out[v], key=lambda w: angle[(v, w)])
        if pick is not None:
            nxt[(u, v)] = (v, pick)
    seen = set()
    regions = []
    for h in half:
        if h in seen:
            continue
        loop = []
        cur = h
        while cur is not None and cur not in seen:
            seen.add(cur)
            loop.append(seq[cur[0]])
            cur = nxt.get(cur)
        if len(loop) >= 3:
            regions.append(loop)
    return regions


def _bridge(outer, hole):
    """Join a hole to the boundary around it so one polygon remains."""
    mi = max(range(len(hole)), key=lambda i: (hole[i][0][0], hole[i][0][1]))
    M = hole[mi][0]
    best = None
    n = len(outer)
    for i in range(n):
        a, b = outer[i][0], outer[(i + 1) % n][0]
        if (a[1] > M[1]) == (b[1] > M[1]):
            continue
        x = a[0] + (M[1] - a[1]) / (b[1] - a[1]) * (b[0] - a[0])
        if x < M[0]:
            continue
        if best is None or x < best[0]:
            best = (x, i if a[0] > b[0] else (i + 1) % n)
    if best is None:
        best = (0.0, min(range(n), key=lambda i: (outer[i][0][0] - M[0]) ** 2 + (outer[i][0][1] - M[1]) ** 2))
    k = best[1]
    return outer[: k + 1] + hole[mi:] + hole[: mi + 1] + outer[k:]


def _ear_clip(poly, eps):
    idx = list(range(len(poly)))
    out = []
    guard = 0
    limit = 4 * len(poly) * len(poly) + 8
    while len(idx) > 3 and guard < limit:
        guard += 1
        cut = False
        for k in range(len(idx)):
            i0, i1, i2 = idx[k - 1], idx[k], idx[(k + 1) % len(idx)]
            a, b, c = poly[i0], poly[i1], poly[i2]
            if _turn(a, b, c) <= eps:
                continue
            if any(_within(poly[j], a, b, c, eps) for j in idx if j not in (i0, i1, i2)):
                continue
            out.append((i0, i1, i2))
            idx.pop(k)
            cut = True
            break
        if not cut:
            # Nothing is a clean ear, which happens where a region is a
            # sliver. Take any corner that turns the right way: the cap is
            # then a worse shape but still the right surface.
            for k in range(len(idx)):
                i0, i1, i2 = idx[k - 1], idx[k], idx[(k + 1) % len(idx)]
                if _turn(poly[i0], poly[i1], poly[i2]) > eps:
                    out.append((i0, i1, i2))
                    idx.pop(k)
                    cut = True
                    break
        if not cut:
            break
    if len(idx) == 3 and abs(_turn(poly[idx[0]], poly[idx[1]], poly[idx[2]])) > eps:
        out.append(tuple(idx))
        return out
    if len(idx) < 3:
        return out
    # What is left has no area: vertices on one line. A hole whose side runs
    # straight through several vertices ends this way whenever the clipping
    # starts at the far corner - the Power Functions battery box's end recesses
    # do, 8 vertices with four of them on one edge - and the ears already taken
    # cover the whole hole. Refusing there left the box open. Instead each
    # leftover vertex is put into the ear edge it lies on, the way a T-junction
    # is split, so the cap meets the surface around it vertex for vertex.
    if abs(_area2([poly[i] for i in idx])) > eps * len(idx):
        return None
    for j in idx:
        for k, (i0, i1, i2) in enumerate(out):
            if j in (i0, i1, i2):
                continue
            for a, b, c in ((i0, i1, i2), (i1, i2, i0), (i2, i0, i1)):
                pa, pb, pj = poly[a], poly[b], poly[j]
                along = (pj[0] - pa[0]) * (pb[0] - pa[0]) + (pj[1] - pa[1]) * (pb[1] - pa[1])
                span = (pb[0] - pa[0]) ** 2 + (pb[1] - pa[1]) ** 2
                if abs(_turn(pa, pb, pj)) <= eps and 0.0 < along < span:
                    out[k] = (a, j, c)
                    out.append((j, b, c))
                    break
            else:
                continue
            break
    return out


def _encloses(p, poly):
    inside = False
    n = len(poly)
    for i in range(n):
        a = poly[i]
        b = poly[(i + 1) % n]
        if (a[1] > p[1]) != (b[1] > p[1]):
            if a[0] + (p[1] - a[1]) * (b[0] - a[0]) / (b[1] - a[1]) > p[0]:
                inside = not inside
    return inside


def _cap_one_plane(loops, normal):
    """Triangulate the region these coplanar boundary loops enclose."""
    e1, e2 = _plane_basis(normal)
    back = {}
    flat = []
    for L in loops:
        f = []
        for p in L:
            q = (
                p[0] * e1[0] + p[1] * e1[1] + p[2] * e1[2],
                p[0] * e2[0] + p[1] * e2[1] + p[2] * e2[2],
            )
            back[q] = p
            f.append(q)
        flat.append(f)
    outers = []
    holes = []
    for region in _regions(flat):
        (outers if _area2(region) > 0 else holes).append(region)
    if not outers:
        return []
    owner = {}
    for h in holes:
        centre = (sum(p[0] for p in h) / len(h), sum(p[1] for p in h) / len(h))
        pick = None
        for i, o in enumerate(outers):
            if _encloses(centre, o):
                area = abs(_area2(o))
                if pick is None or area < pick[1]:
                    pick = (i, area)
        if pick is not None:
            owner.setdefault(pick[0], []).append(h)
    caps = []
    for i, o in enumerate(outers):
        merged = [(p, back[p]) for p in o]
        for h in sorted(owner.get(i, ()), key=lambda h: -max(p[0] for p in h)):
            merged = _bridge(merged, [(p, back[p]) for p in h])
        flat2 = [x[0] for x in merged]
        space = [x[1] for x in merged]
        scale = max(1.0, max(abs(p[0]) for p in flat2), max(abs(p[1]) for p in flat2))
        ears = _ear_clip(flat2, 1e-12 * scale * scale)
        if ears is None:
            return []
        caps.extend((space[a], space[b], space[c]) for a, b, c in ears)
    return caps


def _cap_planar_loops(tris, planar_tol):
    """Fill every flat hole in the mesh. Returns what was left open."""
    planes = {}
    left = 0
    for loop in _boundary_loops(tris):
        # The cap has to walk the loop the other way round, so that the edges
        # it brings are the ones the surface is missing.
        cap = list(reversed(loop))
        nx, ny, nz = _newell(cap)
        size = math.sqrt(nx * nx + ny * ny + nz * nz)
        if size < 1e-12:
            left += 1
            continue
        n = (nx / size, ny / size, nz / size)
        cx = sum(p[0] for p in cap) / len(cap)
        cy = sum(p[1] for p in cap) / len(cap)
        cz = sum(p[2] for p in cap) / len(cap)
        if max(abs((p[0] - cx) * n[0] + (p[1] - cy) * n[1] + (p[2] - cz) * n[2]) for p in cap) > planar_tol:
            left += 1
            continue
        d = cx * n[0] + cy * n[1] + cz * n[2]
        # A hole and the holes inside it face opposite ways, so the key has to
        # ignore the sign to gather them into the same plane.
        s = 1.0
        if n[0] < -1e-9 or (abs(n[0]) <= 1e-9 and (n[1] < -1e-9 or (abs(n[1]) <= 1e-9 and n[2] < 0))):
            s = -1.0
        key = (round(n[0] * s, 4), round(n[1] * s, 4), round(n[2] * s, 4), round(d * s, 4))
        planes.setdefault(key, []).append((cap, n, size / 2.0))
    for items in planes.values():
        # The widest loop in the plane is the one around the outside, and its
        # sense is the sense the whole cap takes.
        ref = max(items, key=lambda it: it[2])[1]
        made = _cap_one_plane([cap for cap, _, _ in items], ref)
        if made:
            tris.extend(made)
        else:
            left += len(items)
    return left


def _orient_consistently(tris):
    """Make every triangle agree with its neighbours about which way is out.

    For a file that certifies its winding, the order the vertices are written
    in *is* the answer and '_mesh' has already applied it. A file that
    certifies nothing - '0 BFC NOCERTIFY', or no BFC line at all - says only
    that its author never promised, and such a file may well wind one triangle
    one way and the triangle beside it the other. Read literally it meshes to a
    surface that is not orientable, which sews into nothing and measures a
    volume with no meaning.

    The mesh itself settles it. Two triangles that share an edge agree when
    they traverse that edge in opposite directions, the way the two sides of a
    seam run opposite ways round a garment; when they traverse it the same way,
    one of them is inside out. So walk the surface from any triangle, flipping
    whatever disagrees with what it was reached from, and the component comes
    out consistent. Which of the two consistent answers it is does not matter:
    '_solid_from_mesh' reads the sign of each closed shell's volume and turns
    the whole shell over if it faces in.

    A surface may arrive in several pieces - the body, a stud, a tube - so
    every component is walked from a seed of its own. An edge shared by more
    than two triangles is non-manifold and nothing can be concluded from it, so
    it is left alone rather than guessed at.
    """
    edges = {}
    for i, (a, b, c) in enumerate(tris):
        for u, v in ((a, b), (b, c), (c, a)):
            edges.setdefault(frozenset((u, v)), []).append((i, u, v))

    flipped = [False] * len(tris)
    seen = [False] * len(tris)
    changed = 0
    for seed in range(len(tris)):
        if seen[seed]:
            continue
        seen[seed] = True
        stack = [seed]
        while stack:
            i = stack.pop()
            a, b, c = tris[i]
            if flipped[i]:
                a, b, c = a, c, b
            for u, v in ((a, b), (b, c), (c, a)):
                users = edges.get(frozenset((u, v)), ())
                if len(users) != 2:
                    continue
                for j, ju, jv in users:
                    if j == i or seen[j]:
                        continue
                    seen[j] = True
                    # 'i' traverses this edge u->v. A neighbour that agrees
                    # traverses it v->u; one that traverses it u->v as well is
                    # inside out relative to 'i'. 'j' has not been reached
                    # before, so it is still in the winding it was read in.
                    if (ju, jv) == (u, v):
                        flipped[j] = True
                        changed += 1
                    stack.append(j)

    if not changed:
        return tris
    return [(a, c, b) if flipped[i] else (a, b, c) for i, (a, b, c) in enumerate(tris)]


def _close_mesh(tris, orient=False):
    """Weld, mend and cap a meshed part. Returns None if it stays open.

    'orient' settles the winding from the mesh rather than from the file, for
    a file that never certified its own. It runs after the weld, because that
    is what makes two triangles share an edge exactly, and then again after the
    T-junction split, because splitting is what first gives some of them an
    edge to share: where a 48-sided primitive meets a 16-sided one, the coarse
    edge spans three fine ones and the two surfaces are separate components
    until the split puts the missing vertices in. Reconciled only before it,
    the seam between them keeps whatever disagreement it had.

    The first pass is still worth making. Splitting preserves the winding of
    the triangle it splits, so everything settled before the split stays
    settled, and reaching the second pass with most of the mesh already
    consistent is what keeps it to the seams.
    """
    tris = _weld(tris, _WELD_LDU)
    if orient:
        tris = _orient_consistently(tris)
    _split_t_junctions(tris, _TJUNCTION_LDU)
    if orient:
        tris = _orient_consistently(tris)
    if _cap_planar_loops(tris, _PLANAR_LDU):
        return None
    if _boundary_loops(tris):
        return None
    return tris


def _solid_from_mesh(tris):
    """Sew a closed mesh into one solid: shells that face in are holes.

    LDraw marks the inside of a cavity by drawing it with the winding turned
    round, which is what '0 BFC INVERTNEXT' is mostly for, so once the surface
    is closed the shells arrive already labelled. A shell whose volume comes
    out positive is a body - the part, a stud standing on it, a tube inside
    it - and one whose volume comes out negative is a hole in whatever
    contains it. Working from the largest outwards is what lets a tube that
    sits inside a cavity survive the cavity being cut away.
    """
    from OCP.BRep import BRep_Tool
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut, BRepAlgoAPI_Fuse
    from OCP.BRepCheck import BRepCheck_Analyzer
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeFace, BRepBuilderAPI_MakePolygon, BRepBuilderAPI_Sewing
    from OCP.BRepGProp import BRepGProp
    from OCP.gp import gp_Pnt
    from OCP.GProp import GProp_GProps
    from OCP.ShapeFix import ShapeFix_Solid
    from OCP.TopAbs import TopAbs_SHELL, TopAbs_SOLID
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopoDS import TopoDS
    from OCP.TopTools import TopTools_ListOfShape

    def volume(shape):
        props = GProp_GProps()
        BRepGProp.VolumeProperties_s(shape, props)
        return props.Mass()

    # The mesh shares its vertices exactly by now, so the sewer is asked not
    # to cut edges: left to it, it splits edges that already matched and
    # leaves faces disagreeing about which way they face.
    sewer = BRepBuilderAPI_Sewing(_SEW_TOL_MM, True, True, False, False)
    for a, b, c in tris:
        # _scaled() is a reflection, and a reflection turns every triangle
        # inside out, so two vertices are swapped back as it happens - the
        # same bargain _write_binary_stl() makes.
        a, b, c = _scaled(a), _scaled(c), _scaled(b)
        if _normal(a, b, c) is None:
            continue
        wire = BRepBuilderAPI_MakePolygon(gp_Pnt(*a), gp_Pnt(*b), gp_Pnt(*c), True).Wire()
        face = BRepBuilderAPI_MakeFace(wire)
        if face.IsDone():
            sewer.Add(face.Face())
    sewer.Perform()

    bodies = []
    holes = []
    explorer = TopExp_Explorer(sewer.SewedShape(), TopAbs_SHELL)
    while explorer.More():
        shell = TopoDS.Shell_s(explorer.Current())
        explorer.Next()
        if not BRep_Tool.IsClosed_s(shell):
            return None  # something the mesh repair missed; use the old path
        facing = volume(shell)
        solid = ShapeFix_Solid().SolidFromShell(shell)
        if volume(solid) < 0.0:
            solid = TopoDS.Solid_s(solid.Reversed())
        (bodies if facing >= 0.0 else holes).append((abs(facing), solid))
    if not bodies:
        return None

    steps = sorted(
        [(v, s, False) for v, s in bodies] + [(v, s, True) for v, s in holes],
        key=lambda step: -step[0],
    )
    while steps and steps[0][2]:
        steps.pop(0)  # a hole outside every body is nothing to take away
    result = steps[0][1]
    i = 1
    while i < len(steps):
        cut = steps[i][2]
        tools = TopTools_ListOfShape()
        while i < len(steps) and steps[i][2] == cut:
            tools.Append(steps[i][1])
            i += 1
        args = TopTools_ListOfShape()
        args.Append(result)
        op = BRepAlgoAPI_Cut() if cut else BRepAlgoAPI_Fuse()
        op.SetArguments(args)
        op.SetTools(tools)
        op.Build()
        if not op.IsDone():
            return None
        result = op.Shape()
    # A boolean hands back a compound even when there is one solid in it, and
    # a compound is not what a part is: unwrap it so the caller is given the
    # SOLID it asked for.
    inside = []
    explorer = TopExp_Explorer(result, TopAbs_SOLID)
    while explorer.More():
        inside.append(TopoDS.Solid_s(explorer.Current()))
        explorer.Next()
    if len(inside) != 1:
        return None
    solid = inside[0]
    # Last of all, ask the kernel. A solid that does not pass is no use to a
    # boolean and no better than the shell the mesh import returns, so it is
    # dropped rather than served: this step can improve a part or leave it
    # alone, never make it worse.
    if not BRepCheck_Analyzer(solid).IsValid():
        return None
    return solid


def _write_binary_stl(tris, path):
    facets = []
    for a, b, c in tris:
        # _scaled() is a reflection, and a reflection turns every triangle it
        # passes through inside out, so two vertices are swapped back as it
        # happens.
        # Without this the winding _mesh() worked out means the opposite thing
        # in millimetres from what it meant in LDraw units, and the part comes
        # out with its whole surface facing inward and its volume negative.
        a, b, c = _scaled(a), _scaled(c), _scaled(b)
        n = _normal(a, b, c)
        if n is None:
            continue  # skip degenerate triangles
        facets.append((n, a, b, c))
    with open(path, "wb") as f:
        f.write(b"\0" * 80)
        f.write(struct.pack("<I", len(facets)))
        for n, a, b, c in facets:
            f.write(struct.pack("<12fH", *n, *a, *b, *c, 0))
    return len(facets)


def _resolve_dat(request):
    cfg = request.get("config") or {}
    params = request.get("parameters") or {}
    dat = cfg.get("dat") or params.get("file") or params.get("dat")
    if not dat:
        dat = request.get("orig_name") or ""
    if dat and not dat.lower().endswith(".dat"):
        dat += ".dat"
    return dat


def _drop_slivers(tris, tol, passes=8):
    """Take out triangles too thin to have a side, and mend the edge they leave.

    Splitting a T-junction makes them: where the vertex split at sits on the
    edge of the triangle beside it, that triangle's three corners are on one
    line. It has no area, so nothing built from the mesh keeps it, and the
    surface opens along it. Its middle corner lies on its longest edge;
    splitting the triangle across that edge at the same corner hands the two
    short edges to a triangle that can carry them, and the sliver can go.
    """
    removed = 0
    for _ in range(passes):
        walks = {}
        for i, t in enumerate(tris):
            for j in range(3):
                walks[(t[j], t[(j + 1) % 3])] = i
        drop, replace = set(), {}
        for i, t in enumerate(tris):
            if i in drop or i in replace:
                continue
            longest, j = max((math.dist(t[(j + 1) % 3], t[j]), j) for j in range(3))
            p, q, m = t[j], t[(j + 1) % 3], t[(j + 2) % 3]
            ux, uy, uz = q[0] - p[0], q[1] - p[1], q[2] - p[2]
            vx, vy, vz = m[0] - p[0], m[1] - p[1], m[2] - p[2]
            area2 = math.sqrt((uy * vz - uz * vy) ** 2 + (uz * vx - ux * vz) ** 2 + (ux * vy - uy * vx) ** 2)
            if longest == 0.0 or area2 / longest > tol:
                continue
            k = walks.get((q, p))
            if k is None or k == i or k in drop or k in replace:
                continue
            o = tris[k]
            a = o.index(q)
            if o[(a + 1) % 3] != p:
                continue
            r = o[(a + 2) % 3]
            replace[k] = [(q, m, r), (m, p, r)]
            drop.add(i)
        if not drop:
            break
        out = []
        for i, t in enumerate(tris):
            if i not in drop:
                out.extend(replace.get(i, [t]))
        tris[:] = out
        removed += len(drop)
    return removed


def _stitch(cap):
    """Triangles closing a long, thin loop: always across the shortest gap left.

    A seam is two runs of vertices lying side by side, and taking the ear whose
    new edge is shortest walks down it pairing each vertex with its neighbour
    across the gap - a zip - rather than reaching from one end to the other.
    """
    ring = list(cap)
    made = []
    while len(ring) > 3:
        n = len(ring)
        i = min(range(n), key=lambda k: math.dist(ring[k - 1], ring[(k + 1) % n]))
        made.append((ring[i - 1], ring[i], ring[(i + 1) % n]))
        del ring[i]
    made.append((ring[0], ring[1], ring[2]))
    return made


def _cap_shallow_loops(tris, tol, seam=0.0):
    """Close every hole still open that is all but flat, or no wider than 'seam'.

    Returns the holes left open. A fan from the centre of a loop within 'tol'
    of flat is the surface it is missing; a loop no wider than 'seam' (twice
    its area over its length) is a seam and is stitched. Anything else would
    be a guess, and is left for the caller to refuse.
    """
    left = 0
    for loop in _boundary_loops(tris):
        cap = list(reversed(loop))
        k = len(cap)
        centre = tuple(sum(p[i] for p in cap) / k for i in range(3))
        nx, ny, nz = _newell(cap)
        size = math.sqrt(nx * nx + ny * ny + nz * nz)
        if size < 1e-12:
            left += 1
            continue
        off = max(abs((p[0] - centre[0]) * nx + (p[1] - centre[1]) * ny + (p[2] - centre[2]) * nz) for p in cap) / size
        if off <= tol:
            tris.extend((cap[i], cap[(i + 1) % k], centre) for i in range(k))
            continue
        perimeter = sum(math.dist(cap[i], cap[(i + 1) % k]) for i in range(k))
        if perimeter > 0.0 and size / perimeter <= seam:  # size is twice the area
            tris.extend(_stitch(cap))
            continue
        left += 1
    return left


def _solid_problems(shape):
    """What keeps 'shape' from being a part. Empty when it is closed, valid solids and nothing else."""
    from OCP.BRep import BRep_Tool
    from OCP.BRepCheck import BRepCheck_Analyzer
    from OCP.BRepGProp import BRepGProp
    from OCP.GProp import GProp_GProps
    from OCP.TopAbs import TopAbs_EDGE, TopAbs_FACE, TopAbs_FORWARD, TopAbs_REVERSED, TopAbs_SOLID
    from OCP.TopExp import TopExp, TopExp_Explorer
    from OCP.TopoDS import TopoDS
    from OCP.TopTools import TopTools_IndexedDataMapOfShapeListOfShape

    if shape is None or shape.IsNull():
        return ["nothing was built"]
    found = []
    solids = []
    explorer = TopExp_Explorer(shape, TopAbs_SOLID)
    while explorer.More():
        solids.append(explorer.Current())
        explorer.Next()
    if not solids:
        found.append("no solid")
    if TopExp_Explorer(shape, TopAbs_FACE, TopAbs_SOLID).More():
        found.append("faces outside any solid")
    if not BRepCheck_Analyzer(shape).IsValid():
        found.append("not valid")
    for solid in solids:
        # An edge with one face on it is a hole in the surface - unless it is
        # one a face carries inside itself, which is what INTERNAL says.
        edges = TopTools_IndexedDataMapOfShapeListOfShape()
        TopExp.MapShapesAndAncestors_s(solid, TopAbs_EDGE, TopAbs_FACE, edges)
        for i in range(1, edges.Extent() + 1):
            edge = TopoDS.Edge_s(edges.FindKey(i))
            if (
                edges.FindFromIndex(i).Extent() == 1
                and not BRep_Tool.Degenerated_s(edge)
                and edge.Orientation() in (TopAbs_FORWARD, TopAbs_REVERSED)
            ):
                found.append("a solid that is open")
                break
        props = GProp_GProps()
        BRepGProp.VolumeProperties_s(solid, props)
        if props.Mass() <= 0.0:
            found.append("a solid with no volume inside it")
    return found


def _enclosed_volume(tris_mm):
    """The volume the surface encloses, by sampling, as (estimate, standard error).

    Asked independently of how the solid was built: a point is inside when the
    triangles wind around it, which a surface with gaps in it still answers
    for nearly every point. It is what a solid assembled from regions is held
    to, so that one that lost the body of the part - a servo reduced to its
    bosses - is refused instead of returned. The sample is seeded, so a part
    gets the same answer every time it is built.
    """
    import numpy as np

    T = np.array(tris_mm, dtype=float)
    lo, hi = T.reshape(-1, 3).min(axis=0), T.reshape(-1, 3).max(axis=0)
    box = float(np.prod(hi - lo))
    if box <= 0.0:
        return 0.0, 0.0
    points = lo + np.random.default_rng(0).random((_VOLUME_SAMPLES, 3)) * (hi - lo)
    winding = np.zeros(len(points))
    for start in range(0, len(points), 64):
        P = points[start : start + 64, None, :]
        A, B, C = T[None, :, 0] - P, T[None, :, 1] - P, T[None, :, 2] - P
        la, lb, lc = (np.linalg.norm(X, axis=2) for X in (A, B, C))
        det = np.einsum("pij,pij->pi", A, np.cross(B, C))
        dot = (
            la * lb * lc
            + np.einsum("pij,pij->pi", A, B) * lc
            + np.einsum("pij,pij->pi", B, C) * la
            + np.einsum("pij,pij->pi", C, A) * lb
        )
        winding[start : start + 64] = np.sum(2.0 * np.arctan2(det, dot), axis=1) / (4.0 * math.pi)
    inside = float(np.mean(winding > 0.5))
    return inside * box, box * math.sqrt(inside * (1.0 - inside) / len(points))


def _solid_from_regions(tris):
    """The solid an LDraw surface encloses, where sewing it into shells cannot.

    LDraw draws a part's surfaces, not its body, and freely lays one surface
    over another: a bush's end face on the face of the block it sits in, a
    primitive closing a face another one closes too. Sewn edge to edge, such a
    mesh has edges with three and four faces on them and no shell to take a
    solid from. So the faces are handed to OCCT whole instead: it cuts them
    against each other where they cross or coincide and returns every closed
    region they bound. Which regions are the part is read off the triangles
    themselves - each knows which of its sides is out - by asking, for each
    region, whether the faces around it face out the way the triangles they
    were cut from do. Area-weighted, so that a membrane drawn both ways round
    counts for nothing and one stray face cannot outvote a wall.

    Every face of the result lies on a triangle LDraw drew; nothing is moved,
    rounded or approximated.
    """
    from OCP.BOPAlgo import BOPAlgo_MakerVolume
    from OCP.BRep import BRep_Builder
    from OCP.BRepAdaptor import BRepAdaptor_Surface
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeFace, BRepBuilderAPI_MakePolygon
    from OCP.BRepGProp import BRepGProp
    from OCP.BRepLProp import BRepLProp_SLProps
    from OCP.BRepTools import BRepTools
    from OCP.GProp import GProp_GProps
    from OCP.gp import gp_Pnt
    from OCP.TopAbs import TopAbs_FACE, TopAbs_REVERSED, TopAbs_SOLID
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopoDS import TopoDS, TopoDS_Compound
    from OCP.TopTools import TopTools_IndexedMapOfShape, TopTools_ListOfShape

    faces, made, normals, mm = TopTools_ListOfShape(), [], [], []
    for a, b, c in tris:
        a, b, c = _scaled(a), _scaled(c), _scaled(b)  # the reflection swap, as in _solid_from_mesh
        n = _normal(a, b, c)
        if n is None:
            # No area, so no surface to lose: stitching a seam whose vertices
            # run in a straight line makes these. Were one ever the only thing
            # closing a gap, the region behind it would leak and the volume
            # check below would say so.
            continue
        face = BRepBuilderAPI_MakeFace(BRepBuilderAPI_MakePolygon(gp_Pnt(*a), gp_Pnt(*b), gp_Pnt(*c), True).Wire())
        if not face.IsDone():
            raise LDrawNotSolid("a triangle could not be made into a face")
        faces.Append(face.Face())
        made.append(face.Face())
        normals.append(n)
        mm.append((a, b, c))

    maker = BOPAlgo_MakerVolume()
    maker.SetArguments(faces)
    maker.Perform()
    if maker.HasErrors():
        raise LDrawNotSolid("its faces could not be split into regions")

    images, owners = TopTools_IndexedMapOfShape(), {}
    for i, face in enumerate(made):
        modified = list(maker.Modified(face))
        for image in modified if modified else ([] if maker.IsDeleted(face) else [face]):
            owners.setdefault(images.Add(image), []).append(i)

    def inside(shape):
        """The regions of 'shape' whose faces face out the way their triangles do."""
        kept = []
        regions = TopExp_Explorer(shape, TopAbs_SOLID)
        while regions.More():
            region = regions.Current()
            regions.Next()
            vote = 0.0
            around = TopExp_Explorer(region, TopAbs_FACE)
            while around.More():
                face = TopoDS.Face_s(around.Current())
                around.Next()
                index = images.FindIndex(face)
                if not index:
                    continue
                u1, u2, v1, v2 = BRepTools.UVBounds_s(face)
                local = BRepLProp_SLProps(BRepAdaptor_Surface(face), (u1 + u2) / 2, (v1 + v2) / 2, 1, 1e-6)
                if not local.IsNormalDefined():
                    continue
                out = local.Normal()
                if face.Orientation() == TopAbs_REVERSED:
                    out.Reverse()
                props = GProp_GProps()
                BRepGProp.SurfaceProperties_s(face, props)
                for i in owners[index]:
                    agrees = out.X() * normals[i][0] + out.Y() * normals[i][1] + out.Z() * normals[i][2] > 0.0
                    vote += props.Mass() if agrees else -props.Mass()
            if vote > 0.0:
                kept.append(region)
        return kept

    kept = inside(maker.Shape())
    if not kept:
        raise LDrawNotSolid("no region its faces bound is inside it")

    # The regions kept are cut from one arrangement of faces, so where two of
    # them meet they share the face between them exactly, and their union is
    # the solid bounded by the faces only one of them uses. That union is built
    # from those faces by the same maker with intersection turned off, which
    # has nothing to compute: every face is already split against every other.
    # A boolean fuse of the regions was used before, and it is not to be
    # trusted with them - on the Cone 4 x 4 x 2's 21 regions it came back
    # empty, or with a solid of no volume, varying from run to run. The joined
    # regions are put to the vote again, because faces that bound only kept
    # regions can also enclose a cavity that was never kept.
    if len(kept) > 1:
        uses, seen = {}, TopTools_IndexedMapOfShape()
        for region in kept:
            around = TopExp_Explorer(region, TopAbs_FACE)
            while around.More():
                index = seen.Add(around.Current())
                uses[index] = uses.get(index, 0) + 1
                around.Next()
        boundary = TopTools_ListOfShape()
        for index, n in uses.items():
            if n == 1:
                boundary.Append(seen.FindKey(index))
        joiner = BOPAlgo_MakerVolume()
        joiner.SetArguments(boundary)
        joiner.SetIntersect(False)
        joiner.Perform()
        kept = [] if joiner.HasErrors() else inside(joiner.Shape())
        if not kept:
            raise LDrawNotSolid("the regions inside it could not be joined")
    result = kept[0]
    if len(kept) > 1:
        result = TopoDS_Compound()
        builder = BRep_Builder()
        builder.MakeCompound(result)
        for region in kept:
            builder.Add(result, region)

    props = GProp_GProps()
    BRepGProp.VolumeProperties_s(result, props)
    expected, error = _enclosed_volume(mm)
    if abs(props.Mass() - expected) > max(3.0 * error, _VOLUME_SLACK * expected):
        raise LDrawNotSolid(
            "the solid built is %.0f mm^3 but its surface encloses about %.0f mm^3" % (props.Mass(), expected)
        )
    return result


def _single(shape):
    """A compound holding one solid is that solid; anything else is as it is."""
    from OCP.TopAbs import TopAbs_COMPOUND, TopAbs_SOLID
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopoDS import TopoDS

    if shape.ShapeType() != TopAbs_COMPOUND:
        return shape
    explorer = TopExp_Explorer(shape, TopAbs_SOLID)
    solids = []
    while explorer.More():
        solids.append(explorer.Current())
        explorer.Next()
    return TopoDS.Solid_s(solids[0]) if len(solids) == 1 else shape


def _build_shape(tris, uncertified=False):
    """The part's solid, or LDrawNotSolid saying why there is none. Never a shell.

    Two ways to it, the cheaper first. A mesh that closes edge to edge is sewn
    into shells and those into the solid they bound (_solid_from_mesh). One
    that does not - surfaces laid over each other, slivers, small holes left
    open - is mended as far as it can be without guessing and then handed to
    the region builder (_solid_from_regions). Whatever either returns is
    checked before it leaves: closed, valid solids and nothing else.

    There used to be a third way, an STL import of the raw mesh, and it is
    gone on purpose. What it returned was a shell, which renders like the part
    and makes every boolean taken against it meaningless.
    """
    # Pin pyexpat before importing OCP (see wrapper_import_mesh.py).
    import pyexpat  # noqa: F401

    if not tris:
        raise LDrawNotSolid("it has no surface")

    closed = _close_mesh(list(tris), orient=uncertified)
    if closed:
        try:
            solid = _solid_from_mesh(closed)
        except Exception:
            solid = None
        if solid is not None and not _solid_problems(solid):
            return solid

    mended = _weld(list(tris), _WELD_LDU)
    if uncertified:
        mended = _orient_consistently(mended)
    _split_t_junctions(mended, _TJUNCTION_LDU)
    if uncertified:
        mended = _orient_consistently(mended)
    _drop_slivers(mended, _WELD_LDU)
    _cap_planar_loops(mended, _PLANAR_LDU)
    # What is still open is not capped: anything put there would be a guess.
    # It is not refused here either. An open edge is not always a leak - the
    # missing face is often drawn, by a primitive that does not share its
    # edges - and the region builder cuts faces against each other wherever
    # they meet. A hole that does leak leaves the part's body without a closed
    # region, which the volume check below refuses.
    left = _cap_shallow_loops(mended, _SHALLOW_LDU, _SEAM_LDU)
    _drop_slivers(mended, _WELD_LDU)
    try:
        solid = _single(_solid_from_regions(mended))
    except LDrawNotSolid as e:
        if left:
            raise LDrawNotSolid("%s; %d holes in its surface were left open rather than guessed at" % (e, left))
        raise
    except Exception as e:
        raise LDrawNotSolid("building it failed: %s" % e)
    found = _solid_problems(solid)
    if found:
        raise LDrawNotSolid("what was built is %s" % ", ".join(found))
    return solid


if __name__ == "__partcad_part__":
    dat = _resolve_dat(request)  # noqa: F821 - injected by the sandbox
    if not dat:
        output = {"exception": "No LDraw part specified (expected config 'dat' or a part name)"}
    else:
        cache = _ldraw_cache_dir()
        text = _ldraw_fetch(dat, cache)
        if text is None:
            if _fetch_failure(dat, cache) == _MISSING:
                output = {"exception": "LDraw part not found in the library: %s" % dat}
            else:
                output = {
                    "exception": "LDraw part could not be fetched: %s "
                    "(the library did not answer; it may be throttling this run)" % dat
                }
        else:
            tris = []
            uncertified = []
            try:
                _mesh(text, _IDENT, (0, 0, 0), tris, cache, uncertified=uncertified)
            except LDrawSubfileMissing as e:
                tris = None
                output = {"exception": "%s (needed by %s)" % (e, dat)}
            if tris is not None:
                try:
                    output = {"shape": _build_shape(tris, bool(uncertified))}
                except LDrawNotSolid as e:
                    output = {"exception": "LDraw part is not a solid: %s (%s)" % (dat, e)}
