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
import os
import struct
import tempfile
import time
import urllib.request

_LDRAW_BASE = "https://library.ldraw.org/library"
_LDRAW_SUBDIRS = ["official/parts", "official/p", "unofficial/parts", "unofficial/p"]
_LDRAW_UA = "Mozilla/5.0 (PartCAD ldraw partType)"
# 1 LDraw Unit = 0.4 mm; LDraw uses -Y as up, so flip Y for a Z-is-up render.
_LDU_MM = 0.4
# A dropped request costs geometry rather than time, so retry before giving up.
_FETCH_RETRIES = 4
_FETCH_BACKOFF = 0.5


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


def _ldraw_fetch(name, cache):
    """Return the text of an LDraw file, fetching+caching it on first use.

    Retried with a backoff, because a part is mostly references and a dropped
    request means a hole in the geometry rather than a slower render:
    library.ldraw.org rate-limits bursts, and a 2 x 2 round brick that loses
    4-4cyli.dat meshes into a flat disc.
    """
    key = name.replace("\\", "/").lower()
    cached = os.path.join(cache, key)
    if os.path.exists(cached):
        with open(cached, "r", encoding="latin-1") as f:
            return f.read()
    for attempt in range(_FETCH_RETRIES):
        for sub in _LDRAW_SUBDIRS:
            url = "%s/%s/%s" % (_LDRAW_BASE, sub, key)
            try:
                req = urllib.request.Request(url, headers={"User-Agent": _LDRAW_UA})
                with urllib.request.urlopen(req, timeout=60) as resp:
                    if resp.status == 200:
                        data = resp.read().decode("latin-1")
                        os.makedirs(os.path.dirname(cached), exist_ok=True)
                        with open(cached, "w", encoding="latin-1") as f:
                            f.write(data)
                        return data
            except Exception:
                continue
        time.sleep(_FETCH_BACKOFF * (attempt + 1))
    return None


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
    # Still LDraw units on LDraw's axes: _scaled() converts, at STL time.
    return (r[0] + t[0], r[1] + t[1], r[2] + t[2])


def _mesh(text, m, t, tris, cache, invert=False):
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
    # 26 of the 3041 files on hand declare. A file that certifies nothing has
    # no winding worth trusting and there is nothing to be done about that
    # here; it is read as CCW, which is what every file got before this, and
    # is right wherever its author happened to be consistent.
    winding_cw = False
    nocertify = False
    invertnext = False
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
            _mesh(subtext, nm, nt, tris, cache, invert ^ invertnext)
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


def _scaled(p):
    # 1 LDU is 0.4 mm, and LDraw's up is -Y, so negating Y here is what stands
    # the part up for a Z-is-up render. It is also a reflection - see the
    # vertex swap in _write_binary_stl(), which is there to answer for it.
    return (p[0] * _LDU_MM, -p[1] * _LDU_MM, p[2] * _LDU_MM)


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


def _write_binary_stl(tris, path):
    facets = []
    for a, b, c in tris:
        # _scaled() negates Y, and a reflection turns every triangle it passes
        # through inside out, so two vertices are swapped back as it happens.
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


def _build_shape(tris):
    # Pin pyexpat before importing build123d/OCP (see wrapper_import_mesh.py).
    import pyexpat  # noqa: F401
    import build123d as b3d

    stl_path = tempfile.mktemp(".stl")
    try:
        n = _write_binary_stl(tris, stl_path)
        if n == 0:
            return None
        try:
            return b3d.Mesher().read(stl_path)[0].wrapped
        except Exception:
            return b3d.import_stl(stl_path).wrapped
    finally:
        if os.path.exists(stl_path):
            os.unlink(stl_path)


if __name__ == "__partcad_part__":
    dat = _resolve_dat(request)  # noqa: F821 - injected by the sandbox
    if not dat:
        output = {"exception": "No LDraw part specified (expected config 'dat' or a part name)"}
    else:
        cache = _ldraw_cache_dir()
        text = _ldraw_fetch(dat, cache)
        if text is None:
            output = {"exception": "LDraw part not found in the library: %s" % dat}
        else:
            tris = []
            try:
                _mesh(text, _IDENT, (0, 0, 0), tris, cache)
            except LDrawSubfileMissing as e:
                tris = None
                output = {"exception": "%s (needed by %s)" % (e, dat)}
            shape = _build_shape(tris) if tris else None
            if shape is None:
                # 'output' is already set when a subfile went missing; only a
                # part that meshed to nothing needs the generic message.
                if tris is not None:
                    output = {"exception": "LDraw part produced no geometry: %s" % dat}
            else:
                output = {"shape": shape}
