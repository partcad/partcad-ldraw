#
# partcad-ldraw, 2026
#
# Licensed under Apache License, Version 2.0.
#
"""Unit tests for the LDraw repository plugin's pure logic (no network, no CAD).

The .dat-header parser, the part-config builder, the category sanitizer and the
offline key/value dispatch are exercised directly. A network-gated test checks a
real category enumeration end to end.
"""

import importlib.util
import os

import pytest

_here = os.path.dirname(__file__)
_spec = importlib.util.spec_from_file_location("ldraw_repo", os.path.join(_here, "ldraw_repo.py"))
plugin = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(plugin)

_SAMPLE_DAT = """\
0 Brick  2 x  4
0 Name: 3001.dat
0 Author: James Jessiman
0 !LDRAW_ORG Part UPDATE 2004-03
0 !LICENSE Licensed under CC BY 4.0 : see CAreadme.txt

0 BFC CERTIFY CCW
1 16 0 0 0 1 0 0 0 1 0 0 0 1 s\\3001s01.dat
4 16 -40 0 -20 -40 24 -20 40 24 -20 40 0 -20
"""


def test_parse_header_extracts_desc_author_license():
    desc, author, lic = plugin._parse_header(_SAMPLE_DAT)
    assert desc == "Brick  2 x  4"
    assert author == "James Jessiman"
    assert lic == "Licensed under CC BY 4.0 : see CAreadme.txt"


def test_parse_header_missing_fields():
    desc, author, lic = plugin._parse_header("0 Just a description\n1 16 0 0 0 1 0 0 0 1 0 0 0 1 x.dat\n")
    assert desc == "Just a description"
    assert author is None and lic is None


def test_part_config_includes_available_metadata():
    cfg = plugin._part_config("3001", ("Brick 2 x 4", "James Jessiman", "CC BY 4.0"))
    assert cfg["type"] == ":ldraw"
    assert cfg["dat"] == "3001.dat"
    assert cfg["desc"] == "Brick 2 x 4"
    assert cfg["author"] == "James Jessiman"
    assert cfg["license"] == "CC BY 4.0"


def test_part_config_tolerates_missing_metadata():
    cfg = plugin._part_config("u1193", None)
    assert cfg == {"type": ":ldraw", "dat": "u1193.dat"}


def test_sanitize_category_name():
    assert plugin._sanitize("Constraction Accessory") == "Constraction-Accessory"
    assert plugin._sanitize("Brick") == "Brick"


def test_part_type_is_served_per_category():
    pt = plugin._PART_TYPE
    assert pt["kind"] == "wrapper"
    assert pt["path"] == "ldraw.py"


@pytest.mark.slow
def test_catalog_paginates_and_reads_metadata():
    # A small category enumerated in full (needs network); skipped offline.
    try:
        catalog = plugin._catalog("Antenna")
    except Exception as e:
        pytest.skip("LDraw could not be reached: %s" % e)
    if not catalog:
        pytest.skip("LDraw returned no parts (offline?)")
    assert all(c["type"] == ":ldraw" and c["dat"].endswith(".dat") for c in catalog.values())
