#
# PartCAD, 2026
#
# Licensed under Apache License, Version 2.0.
#
"""What 'build_colors.py' writes for LEGO's materials: the standard plastics under LDraw's ids, and the ids."""

import os
import sys

import yaml

_here = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _here)
import build_colors  # noqa: E402

# What a material says about the substance, which an enrich takes from its
# source rather than stating a second time.
_SUBSTANCE = ("density", "youngsModulus", "poissonsRatio", "yieldStrength", "thermalConductivity", "mu", "color")


def _materials():
    block = build_colors.yaml_block({})
    return yaml.safe_load(block.replace(build_colors.BEGIN, "").replace(build_colors.END, ""))["materials"]


def test_every_material_keeps_the_ldraw_id_it_is_converted_by():
    """The 'ldraw' convention finds a material by this id; a material that loses it is never used."""
    materials = _materials()
    for key, material in build_colors.MATERIALS.items():
        assert materials[material["name"]]["ids"] == {"ldraw": key}


def test_abs_and_polycarbonate_are_the_standard_plastics_under_ldraw_s_names():
    """An enrich, not an alias: an alias's ids do not count, and the 'ldraw' convention would find neither."""
    materials = _materials()
    for name, source in (
        ("ABS", "//pub/std/manufacturing/material/plastic:abs"),
        ("Polycarbonate", "//pub/std/manufacturing/material/plastic:pc"),
    ):
        assert materials[name]["type"] == "enrich", name
        assert materials[name]["source"] == source, name
        assert materials[name]["desc"], name


def test_abs_and_polycarbonate_state_nothing_their_source_states():
    """Their numbers are the standard catalogue's, with its sources: stating one here would be a second copy."""
    materials = _materials()
    for name in ("ABS", "Polycarbonate"):
        assert set(materials[name]) == {"type", "source", "desc", "ids"}, name
        for field in _SUBSTANCE:
            assert field not in materials[name], (name, field)


def test_rubber_and_fabric_are_declared_here():
    """The standard catalogue has no equivalent of either, and nobody publishes LEGO's compounds."""
    materials = _materials()
    assert "type" not in materials["Rubber"] and "type" not in materials["Fabric"]
    assert materials["Rubber"]["density"] == 1150
    assert "density" not in materials["Fabric"]
    assert "youngsModulus" not in materials["Rubber"] and "youngsModulus" not in materials["Fabric"]


def test_the_materials_in_partcad_yaml_are_the_ones_the_script_writes():
    """The block is generated: an edit by hand is undone by the next run, so it is caught here instead."""
    with open(os.path.join(_here, "partcad.yaml"), encoding="utf-8") as stream:
        config = stream.read()
    checked_in = config[config.index(build_colors.BEGIN) :]
    checked_in = checked_in[: checked_in.index(build_colors.END) + len(build_colors.END)]
    written = build_colors.yaml_block({})
    # The colours come from LDraw's LDConfig.ldr, which is not here; the
    # materials and the finishes after them come from this script alone.
    assert checked_in[checked_in.index("\nmaterials:\n") :] == written[written.index("\nmaterials:\n") :].rstrip("\n")
