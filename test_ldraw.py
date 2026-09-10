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
