#
# PartCAD, 2026
#
# Licensed under Apache License, Version 2.0.
#
"""What 'build_colors.py' writes for LEGO's materials: the analysis values with their sources, and the ids."""

import os
import sys

import yaml

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import build_colors  # noqa: E402


def _materials():
    block = build_colors.yaml_block({})
    return yaml.safe_load(block.replace(build_colors.BEGIN, "").replace(build_colors.END, ""))["materials"]


def test_every_material_keeps_the_ldraw_id_it_is_converted_by():
    """The 'ldraw' convention finds a material by this id; a material that loses it is never used."""
    materials = _materials()
    for key, material in build_colors.MATERIALS.items():
        assert materials[material["name"]]["ids"] == {"ldraw": key}


def test_abs_and_polycarbonate_state_what_an_analysis_is_solved_with():
    materials = _materials()
    for name in ("ABS", "Polycarbonate"):
        for field in ("youngsModulus", "poissonsRatio", "yieldStrength", "thermalConductivity"):
            assert isinstance(materials[name][field], (int, float)), (name, field)
    # Rubber and fabric state none: nobody publishes LEGO's compounds.
    assert "youngsModulus" not in materials["Rubber"] and "youngsModulus" not in materials["Fabric"]


def test_each_value_is_written_with_its_source_beside_it():
    block = build_colors.yaml_block({})
    for material in build_colors.MATERIALS.values():
        for field, value, source in material.get("analysis", ()):
            assert "    %s: %s # %s" % (field, value, source) in block.split("\n")
            # YAML 1.1 reads an exponent as a number only with a '.' and a sign.
            if "e" in value:
                assert "." in value and ("e+" in value or "e-" in value), value
