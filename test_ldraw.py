#
# partcad-ldraw, 2026
#
# Licensed under Apache License, Version 2.0.
#
"""Unit tests for the ':ldraw' partType wrapper's meshing.

An LDraw part is mostly references: a 2 x 2 round brick is a 707-byte stub whose
body is a subpart and a cylinder primitive. What happens when one of those
cannot be read is one whole correctness question, and which way round the
triangles that come back are wound is the other.

Everything here runs with no network and no CAD kernel except the two at the
end, which say so and skip.
"""

import importlib.util
import os
import struct
import tempfile

import pytest

_here = os.path.dirname(__file__)
_spec = importlib.util.spec_from_file_location("ldraw", os.path.join(_here, "ldraw.py"))
ldraw = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ldraw)

# A parent that draws one triangle of its own and defers the rest to a subfile,
# which is the shape of every real part in the library.
_PARENT = """\
0 Test Part
1 16 0 0 0 1 0 0 0 1 0 0 0 1 s\\tests01.dat
3 16 0 0 0 10 0 0 0 10 0
"""
_SUBFILE = "3 16 0 0 0 -10 0 0 0 -10 0\n"


def _mesh(text, fetch):
    tris = []
    ldraw._ldraw_fetch = fetch
    ldraw._mesh(text, ldraw._IDENT, (0, 0, 0), tris, "/nonexistent")
    return tris


def test_a_subfile_that_reads_is_meshed_into_the_parent():
    tris = _mesh(_PARENT, lambda name, cache: _SUBFILE)
    # One triangle from the parent, one from the subfile.
    assert len(tris) == 2


def test_a_missing_subfile_is_an_error_rather_than_a_hole(monkeypatch):
    # The regression this guards: skipping an unreadable subfile returns a part
    # with a piece missing and says nothing, so a 2 x 2 round brick that loses
    # 4-4cyli.dat meshes into a flat disc - and PartCAD caches that shape.
    with pytest.raises(ldraw.LDrawSubfileMissing) as excinfo:
        _mesh(_PARENT, lambda name, cache: None)
    assert "tests01.dat" in str(excinfo.value)


def test_the_error_names_the_subfile_that_could_not_be_read():
    err = ldraw.LDrawSubfileMissing("4-4cyli.dat")
    assert err.name == "4-4cyli.dat"
    assert "4-4cyli.dat" in str(err)


def test_a_fetch_is_retried_before_it_is_given_up_on():
    # A dropped request costs geometry, not just time: library.ldraw.org
    # rate-limits bursts, so one attempt is not enough.
    assert ldraw._FETCH_RETRIES > 1


def test_the_part_entry_point_is_not_run_on_import():
    # The module guards its work behind '__partcad_part__', so importing it for
    # these tests must not try to resolve or fetch anything.
    assert "output" not in vars(ldraw)


# --- BFC winding ------------------------------------------------------------
#
# LDraw records no normals. Which side of a surface is its outside is given by
# the order its vertices are written in, read together with the BFC meta
# statements that say what that order means, so a mesh built without reading
# them is not a solid however right it looks. Measured over the 363 parts that
# mesh out of a warm cache, 300 of them came back with triangles that disagreed
# with their own neighbours about which way is out.
#
# Three things reverse a file's winding and they compound, which is the part
# that is easy to get half right, so each has a test and so does the pairing.

_REAL_FETCH = ldraw._ldraw_fetch

# One triangle whose three vertices are told apart by which axis they are on,
# so a reversal is visible in the result rather than inferred from it.
_TRI = "3 16 1 0 0 0 1 0 0 0 1\n"
_A, _B, _C = (1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0)


def _bfc(text, subfiles=None):
    files = subfiles or {}
    ldraw._ldraw_fetch = lambda name, cache: files.get(name.replace("\\", "/").lower())
    tris = []
    ldraw._mesh(text, ldraw._IDENT, (0, 0, 0), tris, "/nonexistent")
    return tris


def _directed_edges(tris):
    edges = []
    for a, b, c in tris:
        edges += [(a, b), (b, c), (c, a)]
    return edges


def test_a_ccw_file_is_read_in_the_order_it_is_written():
    assert _bfc("0 BFC CERTIFY CCW\n" + _TRI) == [(_A, _B, _C)]


def test_a_cw_file_is_read_the_other_way_round():
    # The whole official library bar a few dozen files is CCW, so a CW one is
    # exactly the case that goes unnoticed: 30274.dat, Brick 2 x 3 x 3 with
    # Lion's Head Carving, is one of them.
    assert _bfc("0 BFC CERTIFY CW\n" + _TRI) == [(_A, _C, _B)]


def test_a_bare_cw_turns_the_rest_of_the_file_and_nothing_before_it():
    tris = _bfc("0 BFC CERTIFY CCW\n" + _TRI + "0 BFC CW\n" + _TRI)
    assert tris == [(_A, _B, _C), (_A, _C, _B)]


def test_nocertify_stops_the_file_s_later_bfc_statements_being_read():
    # 'Any other BFC meta-statements in the file will be ignored' - so the CW
    # here is not an instruction, and reading it as one would turn the file
    # over on the strength of a line the spec says to throw away.
    assert _bfc("0 BFC NOCERTIFY\n0 BFC CW\n" + _TRI) == [(_A, _B, _C)]


def test_invertnext_turns_over_the_subfile_it_precedes():
    tris = _bfc(
        "0 BFC CERTIFY CCW\n0 BFC INVERTNEXT\n"
        "1 16 0 0 0 1 0 0 0 1 0 0 0 1 s\\one.dat\n",
        {"s/one.dat": "0 BFC CERTIFY CCW\n" + _TRI},
    )
    assert tris == [(_A, _C, _B)]


def test_invertnext_reaches_one_reference_and_no_further():
    ref = "1 16 0 0 0 1 0 0 0 1 0 0 0 1 s\\one.dat\n"
    tris = _bfc(
        "0 BFC CERTIFY CCW\n0 BFC INVERTNEXT\n" + ref + ref,
        {"s/one.dat": "0 BFC CERTIFY CCW\n" + _TRI},
    )
    assert tris == [(_A, _C, _B), (_A, _B, _C)]


def test_an_inversion_carries_on_down_the_reference_branch():
    # The spec has inversion accumulate: a subfile of an inverted file is
    # itself inverted. Stopping at the first level leaves everything a stud
    # or a tube is made of wound against the part it belongs to.
    tris = _bfc(
        "0 BFC CERTIFY CCW\n0 BFC INVERTNEXT\n"
        "1 16 0 0 0 1 0 0 0 1 0 0 0 1 s\\mid.dat\n",
        {
            "s/mid.dat": "0 BFC CERTIFY CCW\n1 16 0 0 0 1 0 0 0 1 0 0 0 1 s\\leaf.dat\n",
            "s/leaf.dat": "0 BFC CERTIFY CCW\n" + _TRI,
        },
    )
    assert tris == [(_A, _C, _B)]


def test_a_mirrored_placement_is_read_the_other_way_round():
    # LDraw makes a mirrored copy by mirroring the matrix rather than by
    # writing a mirrored file, and it does so constantly - 8 of the 12
    # references in Brick 2 x 2 Corner are mirrored.
    tris = _bfc(
        "0 BFC CERTIFY CCW\n1 16 0 0 0 -1 0 0 0 1 0 0 0 1 s\\one.dat\n",
        {"s/one.dat": "0 BFC CERTIFY CCW\n" + _TRI},
    )
    assert tris == [((-1.0, 0.0, 0.0), _C, _B)]


def test_a_mirror_and_an_invertnext_cancel_each_other_out():
    # They compound rather than override, so what matters is their parity.
    # Applying whichever is noticed first and stopping gets this one wrong.
    tris = _bfc(
        "0 BFC CERTIFY CCW\n0 BFC INVERTNEXT\n"
        "1 16 0 0 0 -1 0 0 0 1 0 0 0 1 s\\one.dat\n",
        {"s/one.dat": "0 BFC CERTIFY CCW\n" + _TRI},
    )
    assert tris == [((-1.0, 0.0, 0.0), _B, _C)]


def test_two_halves_of_a_mirrored_pair_meet_along_their_shared_edge():
    # What a reversal costs, in the smallest form it takes: the same triangle
    # placed twice, once mirrored, is two halves of one surface, and they are
    # only a surface if they run along the edge they share in opposite
    # directions. Read without the mirror they run along it the same way, and
    # every boolean taken from the result afterwards is meaningless.
    half = "0 BFC CERTIFY CCW\n3 16 0 0 0 1 0 0 0 0 1\n"
    tris = _bfc(
        "0 BFC CERTIFY CCW\n"
        "1 16 0 0 0 1 0 0 0 1 0 0 0 1 s\\half.dat\n"
        "1 16 0 0 0 -1 0 0 0 1 0 0 0 1 s\\half.dat\n",
        {"s/half.dat": half},
    )
    edges = _directed_edges(tris)
    assert len(edges) == len(set(edges)), "a directed edge is used twice"
    origin, up = (0.0, 0.0, 0.0), (0.0, 0.0, 1.0)
    assert (up, origin) in edges and (origin, up) in edges


def test_a_quad_is_turned_over_whole_rather_than_a_half_at_a_time():
    # A quad becomes two triangles about the p1-p3 diagonal, and they have to
    # go on agreeing about it. Reversing the two independently leaves them
    # both traversing that diagonal the same way.
    quad = "4 16 0 0 0 1 0 0 1 1 0 0 1 0\n"
    p1, p2, p3, p4 = (0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (1.0, 1.0, 0.0), (0.0, 1.0, 0.0)
    assert _bfc("0 BFC CERTIFY CCW\n" + quad) == [(p1, p2, p3), (p1, p3, p4)]
    flipped = _bfc("0 BFC CERTIFY CW\n" + quad)
    assert flipped == [(p1, p4, p3), (p1, p3, p2)]
    edges = _directed_edges(flipped)
    assert (p3, p1) in edges and (p1, p3) in edges


# --- the millimetre frame ---------------------------------------------------
#
# _scaled() negates Y to stand a part up, which is a reflection, and a
# reflection turns over every triangle it passes through. The winding _mesh()
# works out therefore has to be reversed again on the way into the STL, or the
# whole part arrives with its surface facing inward.


def _facets(tris):
    path = tempfile.mktemp(".stl")
    try:
        written = ldraw._write_binary_stl(tris, path)
        with open(path, "rb") as f:
            f.read(80)
            (count,) = struct.unpack("<I", f.read(4))
            out = [struct.unpack("<12fH", f.read(50)) for _ in range(count)]
    finally:
        if os.path.exists(path):
            os.unlink(path)
    assert written == len(out)
    return out


def test_a_surface_facing_ldraw_s_up_still_faces_up_in_millimetres():
    # This triangle's outward side is -Y, which is up in LDraw. Y is negated
    # on the way to millimetres, so up is +Y there, and that is where the
    # normal has to end up. Without the reversal it comes out as -Y: the part
    # is inside out, and its volume is negative.
    (facet,) = _facets([((0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (0.0, 0.0, 1.0))])
    assert facet[0:3] == pytest.approx((0.0, 1.0, 0.0), abs=1e-6)


def test_the_facet_normal_says_the_same_thing_as_the_vertices_beside_it():
    # The normal is computed from the vertices rather than carried alongside
    # them, so the two cannot drift apart. Worth holding to, because importers
    # differ on which of the two they believe.
    (facet,) = _facets([((0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (0.0, 0.0, 1.0))])
    n, a, b, c = facet[0:3], facet[3:6], facet[6:9], facet[9:12]
    u = [b[i] - a[i] for i in range(3)]
    v = [c[i] - a[i] for i in range(3)]
    cross = (u[1] * v[2] - u[2] * v[1], u[2] * v[0] - u[0] * v[2], u[0] * v[1] - u[1] * v[0])
    length = sum(x * x for x in cross) ** 0.5
    assert n == pytest.approx(tuple(x / length for x in cross), abs=1e-6)


# --- against the real library -----------------------------------------------
#
# Closing the surface: welding, mending seams, and filling the holes LDraw
# leaves where it stands a stud on a face it never cut.


def _box(lo, hi, outward=True, omit=()):
    """The six faces of an axis-aligned box, as triangles facing out (or in)."""
    (x0, y0, z0), (x1, y1, z1) = lo, hi
    centre = ((x0 + x1) / 2.0, (y0 + y1) / 2.0, (z0 + z1) / 2.0)
    quads = {
        "y-": [(x0, y0, z0), (x1, y0, z0), (x1, y0, z1), (x0, y0, z1)],
        "y+": [(x0, y1, z0), (x1, y1, z0), (x1, y1, z1), (x0, y1, z1)],
        "x-": [(x0, y0, z0), (x0, y1, z0), (x0, y1, z1), (x0, y0, z1)],
        "x+": [(x1, y0, z0), (x1, y1, z0), (x1, y1, z1), (x1, y0, z1)],
        "z-": [(x0, y0, z0), (x1, y0, z0), (x1, y1, z0), (x0, y1, z0)],
        "z+": [(x0, y0, z1), (x1, y0, z1), (x1, y1, z1), (x0, y1, z1)],
    }
    tris = []
    for name, q in quads.items():
        if name in omit:
            continue
        n = ldraw._normal(q[0], q[1], q[2])
        mid = [sum(p[k] for p in q) / 4.0 for k in range(3)]
        away = sum(n[k] * (mid[k] - centre[k]) for k in range(3)) > 0.0
        if away != outward:
            q = list(reversed(q))
        tris.append((q[0], q[1], q[2]))
        tris.append((q[0], q[2], q[3]))
    return tris


def _signed_volume(tris):
    total = 0.0
    for a, b, c in tris:
        total += (
            a[0] * (b[1] * c[2] - b[2] * c[1])
            - a[1] * (b[0] * c[2] - b[2] * c[0])
            + a[2] * (b[0] * c[1] - b[1] * c[0])
        ) / 6.0
    return total


def test_a_flat_hole_is_filled_and_the_part_measures_what_it_should():
    # The shape of every stud in the library: a box with a face left off.
    tris = ldraw._close_mesh(_box((0, 0, 0), (10, 10, 10), omit=("y-",)))
    assert tris is not None
    assert ldraw._boundary_loops(tris) == []
    assert _signed_volume(tris) == pytest.approx(1000.0)


def test_the_cap_is_wound_to_agree_with_the_surface_it_closes():
    tris = ldraw._close_mesh(_box((0, 0, 0), (10, 10, 10), omit=("y-",)))
    seen = {}
    for edge in _directed_edges(tris):
        seen[edge] = seen.get(edge, 0) + 1
    assert all(n == 1 for n in seen.values())
    assert all(seen.get((b, a)) == 1 for a, b in seen)


def test_two_spellings_of_one_corner_become_one_corner():
    # LDraw writes to four or five decimals, so the same point reached through
    # two primitives differs in the last digit and nothing sews.
    tris = _box((0, 0, 0), (10, 10, 10))
    drifted = [tuple(tuple(c + 1e-4 if c else c for c in p) for p in t) for t in tris[:2]]
    welded = ldraw._weld(tris[2:] + drifted, ldraw._WELD_LDU)
    assert len({p for t in welded for p in t}) == 8


def test_a_vertex_sitting_on_an_edge_splits_it():
    # A 16-sided wall meeting a 48-sided floor: the floor's extra vertices land
    # in the middle of the wall's edges rather than at their ends.
    tris = [
        ((0.0, 0.0, 0.0), (10.0, 0.0, 0.0), (0.0, 10.0, 0.0)),
        ((0.0, 0.0, 0.0), (5.0, 0.0, 0.0), (0.0, 0.0, -10.0)),
    ]
    assert ldraw._split_t_junctions(tris, ldraw._TJUNCTION_LDU) == 1
    assert ((0.0, 0.0, 0.0), (5.0, 0.0, 0.0)) in ldraw._half_edges(tris)
    assert ((5.0, 0.0, 0.0), (10.0, 0.0, 0.0)) in ldraw._half_edges(tris)


def test_a_ring_shaped_hole_is_filled_as_a_ring_and_not_as_a_disc():
    # An underside: a box open at the bottom with a cavity that is open there
    # too. The hole in that plane is bounded by two loops, and filling each of
    # them on its own lays one disc over another and measures the cavity as
    # solid.
    outer = _box((0, 0, 0), (10, 10, 10), omit=("y-",))
    cavity = _box((2, 0, 2), (8, 5, 8), outward=False, omit=("y-",))
    tris = ldraw._close_mesh(outer + cavity)
    assert tris is not None
    assert ldraw._boundary_loops(tris) == []
    assert _signed_volume(tris) == pytest.approx(1000.0 - 6.0 * 5.0 * 6.0)


def test_a_cavity_drawn_facing_inward_is_taken_out_rather_than_added():
    # LDraw marks the inside of a hole by turning the winding round, which is
    # what '0 BFC INVERTNEXT' is mostly for. Fusing such a shell in rather than
    # cutting it out fills the hole with material and the part weighs too much.
    pytest.importorskip("build123d")
    try:
        from OCP.BRepGProp import BRepGProp
        from OCP.GProp import GProp_GProps
    except ImportError as e:
        pytest.skip("no CAD kernel: %s" % e)
    tris = _box((0, 0, 0), (10, 10, 10)) + _box((3, 3, 3), (6, 6, 6), outward=False)
    solid = ldraw._solid_from_mesh(tris)
    assert solid is not None
    props = GProp_GProps()
    BRepGProp.VolumeProperties_s(solid, props)
    # Millimetres, so 1 LDU of each side is 0.4 mm: 4^3 - 1.2^3.
    assert props.Mass() == pytest.approx((10 * ldraw._LDU_MM) ** 3 - (3 * ldraw._LDU_MM) ** 3)


def test_a_hole_that_is_not_flat_is_left_open_rather_than_guessed_at():
    tris = _box((0, 0, 0), (10, 10, 10), omit=("y-",))
    # Pull one corner of the opening out of its plane.
    moved = []
    for t in tris:
        moved.append(tuple((p[0], p[1], p[2]) if p != (0.0, 0.0, 0.0) else (0.0, -6.0, -6.0) for p in t))
    assert ldraw._close_mesh(moved) is None


#
# The two below reach ldraw.org, and the second wants a CAD kernel as well.
# Both skip rather than fail where they cannot have what they need, because
# the rest of this file is meant to run anywhere.


@pytest.mark.slow
def test_a_real_part_meshes_into_a_surface_that_agrees_with_itself(monkeypatch):
    # Brick 2 x 4, the part the defect was found on. Every directed edge that
    # two triangles share has to be traversed once each way; before the BFC
    # read, 454 of its 700 triangles came back reversed against the other 246.
    # The few edges left over are the open ends LDraw draws a stud and a stud
    # tube as, which is a separate matter and not this one.
    monkeypatch.setattr(ldraw, "_ldraw_fetch", _REAL_FETCH)
    cache = ldraw._ldraw_cache_dir()
    text = ldraw._ldraw_fetch("3001.dat", cache)
    if text is None:
        pytest.skip("LDraw could not be reached (offline?)")
    tris = []
    try:
        ldraw._mesh(text, ldraw._IDENT, (0, 0, 0), tris, cache)
    except ldraw.LDrawSubfileMissing as e:
        pytest.skip("LDraw could not be reached: %s" % e)
    seen = {}
    for edge in _directed_edges(tris):
        seen[edge] = seen.get(edge, 0) + 1
    doubled = [e for e, n in seen.items() if n > 1]
    lopsided = [e for e, n in seen.items() if seen.get((e[1], e[0]), 0) not in (0, n)]
    assert not doubled, "%d directed edges are used twice" % len(doubled)
    assert not lopsided, "%d edges are shared unevenly" % len(lopsided)


@pytest.mark.slow
def test_a_real_round_part_comes_back_as_a_solid(monkeypatch):
    # The defect: Brick 2 x 2 Round came back as a SHELL with no solid in it,
    # so every boolean taken against it returned nothing and its mass, its
    # interference and its FEA were all meaningless. The rectangular bricks
    # did return a solid, but one BRepCheck_Analyzer rejected.
    pytest.importorskip("build123d")
    try:
        from OCP.BRepCheck import BRepCheck_Analyzer
        from OCP.BRepGProp import BRepGProp
        from OCP.GProp import GProp_GProps
        from OCP.TopAbs import TopAbs_ShapeEnum
    except ImportError as e:
        pytest.skip("no CAD kernel: %s" % e)

    monkeypatch.setattr(ldraw, "_ldraw_fetch", _REAL_FETCH)
    cache = ldraw._ldraw_cache_dir()
    for name in ("3941.dat", "4589.dat", "3005.dat"):
        text = ldraw._ldraw_fetch(name, cache)
        if text is None:
            pytest.skip("LDraw could not be reached (offline?)")
        tris = []
        try:
            ldraw._mesh(text, ldraw._IDENT, (0, 0, 0), tris, cache)
        except ldraw.LDrawSubfileMissing as e:
            pytest.skip("LDraw could not be reached: %s" % e)
        shape = ldraw._build_shape(tris)
        assert shape is not None
        assert shape.ShapeType() == TopAbs_ShapeEnum.TopAbs_SOLID, name
        assert BRepCheck_Analyzer(shape).IsValid(), name
        props = GProp_GProps()
        BRepGProp.VolumeProperties_s(shape, props)
        assert props.Mass() > 0.0, name


@pytest.mark.slow
def test_a_real_part_occupies_positive_space_and_not_its_neighbour_s(monkeypatch):
    # The defect as it was reported. Brick 2 x 4 had a volume of -1939.6 mm^3,
    # and shared -2282.9 mm^3 with a copy of itself standing 100 mm away -
    # which is the measurement that says it matters, since two solids that far
    # apart share exactly none of it.
    pytest.importorskip("build123d")
    try:
        from OCP.BRepAlgoAPI import BRepAlgoAPI_Common
        from OCP.BRepBuilderAPI import BRepBuilderAPI_Transform
        from OCP.BRepGProp import BRepGProp
        from OCP.gp import gp_Trsf, gp_Vec
        from OCP.GProp import GProp_GProps
    except ImportError as e:
        pytest.skip("no CAD kernel: %s" % e)

    monkeypatch.setattr(ldraw, "_ldraw_fetch", _REAL_FETCH)
    cache = ldraw._ldraw_cache_dir()
    text = ldraw._ldraw_fetch("3001.dat", cache)
    if text is None:
        pytest.skip("LDraw could not be reached (offline?)")
    tris = []
    try:
        ldraw._mesh(text, ldraw._IDENT, (0, 0, 0), tris, cache)
    except ldraw.LDrawSubfileMissing as e:
        pytest.skip("LDraw could not be reached: %s" % e)
    shape = ldraw._build_shape(tris)
    assert shape is not None

    def volume(s):
        props = GProp_GProps()
        BRepGProp.VolumeProperties_s(s, props)
        return props.Mass()

    moved = gp_Trsf()
    moved.SetTranslation(gp_Vec(100.0, 0.0, 0.0))
    away = BRepBuilderAPI_Transform(shape, moved, True).Shape()
    assert volume(shape) > 0.0
    assert volume(BRepAlgoAPI_Common(shape, away).Shape()) == pytest.approx(0.0, abs=1e-6)


# --- a file that never certified its winding ------------------------------
#
# '0 BFC NOCERTIFY', or no BFC line at all, is the author declining to promise
# that the order the vertices are written in means anything. Such a file may
# wind one triangle one way and the next the other, which reads as a surface
# that is not orientable: it sews into nothing and its volume means nothing.
# The mesh settles what the metadata would not.


def _uncertified(text, subfiles=None):
    """Mesh 'text' and report whether anything in it left its winding open."""
    files = subfiles or {}
    ldraw._ldraw_fetch = lambda name, cache: files.get(name.replace("\\", "/").lower())
    tris, flag = [], []
    ldraw._mesh(text, ldraw._IDENT, (0, 0, 0), tris, "/nonexistent", uncertified=flag)
    return tris, bool(flag)


def _scramble(tris, every=2):
    """Turn every n-th triangle over, as an uncertified author might have."""
    return [(a, c, b) if i % every == 0 else (a, b, c) for i, (a, b, c) in enumerate(tris)]


def test_a_certified_file_is_not_reported_as_uncertified():
    _, flag = _uncertified("0 BFC CERTIFY CCW\n" + _TRI)
    assert flag is False


def test_a_nocertify_file_is_reported():
    _, flag = _uncertified("0 BFC NOCERTIFY\n" + _TRI)
    assert flag is True


def test_a_file_with_no_bfc_line_at_all_is_reported():
    # The spec's default: a file that says nothing has certified nothing.
    _, flag = _uncertified(_TRI)
    assert flag is True


def test_a_certify_after_the_first_polygon_still_counts():
    # The flag is decided once the whole file has been read, so a CERTIFY that
    # arrives late is not mistaken for a file that never certified at all.
    _, flag = _uncertified(_TRI + "0 BFC CERTIFY CCW\n")
    assert flag is False


def test_an_uncertified_subfile_is_reported_through_its_parent():
    parent = "0 BFC CERTIFY CCW\n0 BFC INVERTNEXT\n1 16 0 0 0 1 0 0 0 1 0 0 0 1 sub.dat\n"
    _, flag = _uncertified(parent, {"sub.dat": _TRI})
    assert flag is True


def test_scrambled_winding_is_settled_from_the_mesh():
    """Triangles that disagree with their neighbours are turned to agree"""
    tris = _scramble(_box((0, 0, 0), (10, 10, 10)))
    # As read, the surface is not orientable: some edge is traversed the same
    # way by both triangles that share it.
    edges = {}
    for edge in _directed_edges(tris):
        edges[edge] = edges.get(edge, 0) + 1
    assert any(n > 1 for n in edges.values())

    fixed = ldraw._orient_consistently(tris)

    edges = {}
    for edge in _directed_edges(fixed):
        edges[edge] = edges.get(edge, 0) + 1
    assert all(n == 1 for n in edges.values())
    assert all(edges.get((b, a)) == 1 for a, b in edges)
    assert abs(_signed_volume(fixed)) == pytest.approx(1000.0)


def test_a_consistent_mesh_is_left_exactly_as_it_was():
    """Nothing is done to a file whose author was consistent after all"""
    tris = _box((0, 0, 0), (10, 10, 10))
    assert ldraw._orient_consistently(tris) == tris


def test_orienting_is_only_done_when_it_is_asked_for():
    """A certified file keeps the winding it declared, whatever the mesh says

    A part may legitimately be wound against its own outside - LDraw draws a
    cavity that way - so a file that certified its winding is taken at its
    word.
    """
    scrambled = _scramble(_box((0, 0, 0), (10, 10, 10)))
    left = ldraw._close_mesh(list(scrambled))
    settled = ldraw._close_mesh(list(scrambled), orient=True)
    assert abs(_signed_volume(settled)) == pytest.approx(1000.0)
    assert left is None or abs(_signed_volume(left)) != pytest.approx(1000.0)


def test_each_piece_of_a_surface_is_settled_on_its_own():
    """A part arrives as a body and its studs, which touch nothing"""
    tris = _scramble(_box((0, 0, 0), (10, 10, 10)) + _box((50, 50, 50), (60, 60, 60)))

    fixed = ldraw._orient_consistently(tris)

    edges = {}
    for edge in _directed_edges(fixed):
        edges[edge] = edges.get(edge, 0) + 1
    assert all(n == 1 for n in edges.values())
    assert abs(_signed_volume(fixed)) == pytest.approx(2000.0)
