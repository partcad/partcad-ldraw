#
# partcad-ldraw, 2026
#
# Licensed under Apache License, Version 2.0.
#
"""Unit tests for the ':ldraw' partType wrapper's meshing (no network, no CAD).

An LDraw part is mostly references: a 2 x 2 round brick is a 707-byte stub whose
body is a subpart and a cylinder primitive. What happens when one of those
cannot be read is therefore the whole correctness question, and it is what these
tests are about.
"""

import importlib.util
import os

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
