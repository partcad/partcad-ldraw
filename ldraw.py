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
import os
import struct
import tempfile
import urllib.request

_LDRAW_BASE = "https://library.ldraw.org/library"
_LDRAW_SUBDIRS = ["official/parts", "official/p", "unofficial/parts", "unofficial/p"]
_LDRAW_UA = "Mozilla/5.0 (PartCAD ldraw partType)"
# 1 LDraw Unit = 0.4 mm; LDraw uses -Y as up, so flip Y for a Z-is-up render.
_LDU_MM = 0.4


def _ldraw_cache_dir():
    base = os.environ.get("PARTCAD_LDRAW_CACHE")
    if not base:
        xdg = os.environ.get("XDG_CACHE_HOME") or os.path.join(os.path.expanduser("~"), ".cache")
        base = os.path.join(xdg, "partcad-ldraw")
    return base


def _ldraw_fetch(name, cache):
    """Return the text of an LDraw file, fetching+caching it on first use."""
    key = name.replace("\\", "/").lower()
    cached = os.path.join(cache, key)
    if os.path.exists(cached):
        with open(cached, "r", encoding="latin-1") as f:
            return f.read()
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
    return None


_IDENT = ((1, 0, 0), (0, 1, 0), (0, 0, 1))


def _mat_mul(a, b):
    return tuple(tuple(sum(a[i][k] * b[k][j] for k in range(3)) for j in range(3)) for i in range(3))


def _mat_vec(a, v):
    return tuple(sum(a[i][k] * v[k] for k in range(3)) for i in range(3))


def _compose(pm, pt, cm, ct):
    m = _mat_mul(pm, cm)
    mv = _mat_vec(pm, ct)
    return m, (mv[0] + pt[0], mv[1] + pt[1], mv[2] + pt[2])


def _xform(m, t, p):
    r = _mat_vec(m, p)
    # scale to mm and flip Y (LDraw -Y is up) as we bake world coordinates
    return (r[0] + t[0], r[1] + t[1], r[2] + t[2])


def _mesh(text, m, t, tris, cache):
    for line in text.splitlines():
        f = line.split()
        if not f:
            continue
        code = f[0]
        if code == "1" and len(f) >= 15:
            v = list(map(float, f[2:14]))
            cm = (tuple(v[3:6]), tuple(v[6:9]), tuple(v[9:12]))
            ct = (v[0], v[1], v[2])
            nm, nt = _compose(m, t, cm, ct)
            subtext = _ldraw_fetch(" ".join(f[14:]), cache)
            if subtext is not None:
                _mesh(subtext, nm, nt, tris, cache)
        elif code == "3" and len(f) >= 11:
            v = list(map(float, f[2:11]))
            tris.append((_xform(m, t, v[0:3]), _xform(m, t, v[3:6]), _xform(m, t, v[6:9])))
        elif code == "4" and len(f) >= 14:
            v = list(map(float, f[2:14]))
            p1, p2, p3, p4 = v[0:3], v[3:6], v[6:9], v[9:12]
            tris.append((_xform(m, t, p1), _xform(m, t, p2), _xform(m, t, p3)))
            tris.append((_xform(m, t, p1), _xform(m, t, p3), _xform(m, t, p4)))


def _scaled(p):
    return (p[0] * _LDU_MM, -p[1] * _LDU_MM, p[2] * _LDU_MM)


def _normal(a, b, c):
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
        a, b, c = _scaled(a), _scaled(b), _scaled(c)
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
            _mesh(text, _IDENT, (0, 0, 0), tris, cache)
            shape = _build_shape(tris) if tris else None
            if shape is None:
                output = {"exception": "LDraw part produced no geometry: %s" % dat}
            else:
                output = {"shape": shape}
