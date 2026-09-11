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
import json
import math
import os
import re

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
    # An id the index cannot have: with no header and no index entry there is
    # nothing to say about the part but what it is built from.
    cfg = plugin._part_config("zzz-not-a-part", None)
    assert cfg == {
        "type": ":ldraw",
        "dat": "zzz-not-a-part.dat",
        "parameters": {"dat": {"type": "string", "default": "zzz-not-a-part.dat"}},
    }


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


# --- interfaces -------------------------------------------------------------
#
# The plugin never looks at a part's geometry: it reads the LDraw description
# and works the ports out from the LEGO grid. The coordinates asserted below
# were taken from the LDraw parts themselves (the peghole / beamhole / axlehole
# / connect / axle primitives of 3001, 3700, 32000, 6541, 32064a, 32316, 3705,
# 3673 and 32556), converted into the meshed part space the wrapper produces:
# (x, -y, z) * 0.4, so +Y is up and a brick's top plane is y = 0.

STUD = "//pub/universe/lego:stud"
ANTI = "//pub/universe/lego:anti-stud"
PIN = "//pub/universe/lego:technic-pin"
PIN_HOLE = "//pub/universe/lego:technic-pin-hole"
AXLE = "//pub/universe/lego:technic-axle"
AXLE_HOLE = "//pub/universe/lego:technic-axle-hole"


def test_brick_carries_a_stud_and_an_anti_stud_per_stud():
    implements = plugin._lego_implements("Brick  2 x  4")
    assert set(implements) == {STUD, ANTI}
    assert len(implements[STUD]) == 8 and len(implements[ANTI]) == 8
    # The 2 x 4 is 2 studs deep (Z) and 4 long (X), on an 8 mm grid.
    assert implements[STUD]["c0r0"] == [[-12.0, 0, -4.0], [1, 0, 0], 270]
    assert implements[STUD]["c3r1"] == [[12.0, 0, 4.0], [1, 0, 0], 270]
    # The anti-studs are the same grid on the part's own bottom plane.
    assert implements[ANTI]["c0r0"] == [[-12.0, -9.6, -4.0], [1, 1, -1], 120]


def test_tile_has_no_studs_and_a_plate_is_thinner():
    assert set(plugin._lego_implements("Tile  2 x  2")) == {ANTI}
    assert plugin._lego_implements("Plate  1 x  2")[ANTI]["c0r0"][0] == [-4.0, -3.2, 0.0]


def test_a_third_dimension_is_not_a_second_one():
    # A taller part's bottom plane is not where the "A x B" rule would put it.
    assert plugin._lego_implements("Brick  1 x  2 x  5") is None
    # ... and the regex must not reach that verdict by matching a shorter
    # number: "16 x 16 x 0.667" is not a 16 x 1 plate.
    assert plugin._lego_implements("Plate 16 x 16 x  0.667") is None
    assert plugin._lego_implements("Tile  8 x 20 x  0.667 with Curved Ends") is None


def test_technic_brick_with_holes_gets_studs_and_holes_between_them():
    implements = plugin._lego_implements("Technic Brick  1 x  4 with Holes")
    assert set(implements) == {STUD, ANTI, PIN_HOLE}
    assert len(implements[STUD]) == 4
    # Three holes, each between two studs: 4 mm below the top plane, through
    # the part's two 1-stud-wide faces.
    assert len(implements[PIN_HOLE]) == 6
    assert implements[PIN_HOLE]["h0-front"] == [[-8.0, -4.0, 4.0], [0, 0, 1], 0]
    assert implements[PIN_HOLE]["h0-back"] == [[-8.0, -4.0, -4.0], [1, 0, 0], 180]
    assert implements[PIN_HOLE]["h1-front"][0] == [0.0, -4.0, 4.0]
    assert implements[PIN_HOLE]["h2-front"][0] == [8.0, -4.0, 4.0]


def test_technic_brick_hole_shapes_of_the_short_bricks():
    # "with Hole" is one hole on the center line (3700 is a 1 x 2, 6541 a 1 x 1).
    for desc in ("Technic Brick  1 x  2 with Hole", "Technic Brick  1 x  1 with Hole"):
        holes = plugin._lego_implements(desc)[PIN_HOLE]
        assert len(holes) == 2 and holes["h0-front"][0] == [0.0, -4.0, 4.0]
    # The 1 x 2 "with Holes" (32000) is the exception: two holes, under the studs.
    holes = plugin._lego_implements("Technic Brick  1 x  2 with Holes")[PIN_HOLE]
    assert len(holes) == 4
    assert [holes["h0-front"][0], holes["h1-front"][0]] == [[-4.0, -4.0, 4.0], [4.0, -4.0, 4.0]]


def test_technic_brick_with_axlehole():
    # Every variant of the name puts the cross hole in the same place.
    for desc in (
        "Technic Brick  1 x  2 with Axlehole with Open Sides and Stud Blocker",
        "Technic Brick  1 x  2 with Reduced Axlehole",
        "Technic Brick  1 x  1 with Axlehole",
    ):
        implements = plugin._lego_implements(desc)
        assert set(implements) == {STUD, ANTI, AXLE_HOLE}
        assert implements[AXLE_HOLE]["axle-front"] == [[0.0, -4.0, 4.0], [0, 0, 1], 0]
        assert implements[AXLE_HOLE]["axle-back"] == [[0.0, -4.0, -4.0], [1, 0, 0], 180]


def test_technic_beam_holes_run_the_other_way_and_it_has_no_studs():
    implements = plugin._lego_implements("Technic Beam  5")
    assert set(implements) == {PIN_HOLE}
    assert len(implements[PIN_HOLE]) == 10
    # A beam lies along Z, is 8 mm thick along Y, and its holes go through that.
    assert implements[PIN_HOLE]["h0-top"] == [[0, 4.0, -16.0], [1, 0, 0], 270]
    assert implements[PIN_HOLE]["h0-bottom"] == [[0, -4.0, -16.0], [1, 0, 0], 90]
    assert implements[PIN_HOLE]["h4-top"][0] == [0.0, 4.0, 16.0]


def test_technic_axle_ends_point_at_each_other():
    implements = plugin._lego_implements("Technic Axle  4")
    assert set(implements) == {AXLE}
    # 4 modules = 32 mm along X, centered; each port's Z points down the shaft.
    assert implements[AXLE]["left"] == [[-16.0, 0, 0], [0, 1, 0], 90]
    assert implements[AXLE]["right"] == [[16.0, 0, 0], [0, 1, 0], 270]
    assert plugin._lego_implements("Technic Axle 32")[AXLE]["right"][0] == [128.0, 0, 0]


def test_technic_pins():
    # A 2-module pin: one collar, in the middle, with a pin either side of it.
    for desc in ("Technic Pin", "Technic Pin with Friction", "Technic Pin with Friction and Slots"):
        pins = plugin._lego_implements(desc)[PIN]
        assert pins["left"] == [[0, 0, 0], [0, 1, 0], 270]
        assert pins["right"] == [[0, 0, 0], [0, 1, 0], 90]
    # A 3-module pin has two collars, half a module either side of the middle.
    pins = plugin._lego_implements("Technic Pin Long")[PIN]
    assert [pins["left"][0], pins["right"][0]] == [[-4.0, 0, 0], [4.0, 0, 0]]
    # The 1/2 pin is a pin one way and a stud the other.
    half = plugin._lego_implements("Technic Pin  1/2")
    assert set(half) == {PIN, STUD}
    assert half[PIN]["left"] == [[0, 0, 0], [0, 1, 0], 270]
    assert half[STUD]["stud"] == [[0, 0, 0], [0, 1, 0], 90]


def test_a_name_that_says_more_than_the_rule_knows_gets_nothing():
    # These parts exist; their features are not where the plain name would be.
    for desc in (
        "Technic Brick  1 x  4 with Holes and Bumper Holder",
        "Technic Beam  3 x  5 Bent 90",
        "Technic Beam  2 Liftarm",
        "Technic Axle  4 with Stop",
        "Technic Axle  5.5 with Stop",
        "Technic Pin Long with Stop Bush",
        "Technic Plate  1 x  4 with Holes",
        # A full pin one way and a half one the other, which "left" and "right"
        # would not tell apart.
    ):
        assert plugin._lego_implements(desc) is None, desc


# --- the connections these ports produce ------------------------------------
#
# A rigid-transform algebra small enough to keep here, mirroring pc.Location and
# the placement 'assembly_factory_assy' computes for a connection:
#
#     location = <target port> * <the mate flip> * <freedom> * <source port>^-1
#
# The mate flip is a half turn about [1,1,0]; it faces the two ports at each
# other. What these tests check is that the parts then end up where the LEGO
# system says they should - which is what the choice of port coordinates and,
# just as much, the roll of each port decides.


def _rot(axis, angle_deg):
    import math

    x, y, z = axis
    norm = math.sqrt(x * x + y * y + z * z)
    x, y, z = x / norm, y / norm, z / norm
    a = math.radians(angle_deg)
    c, s, t = math.cos(a), math.sin(a), 1 - math.cos(a)
    return (
        (t * x * x + c, t * x * y - s * z, t * x * z + s * y),
        (t * x * y + s * z, t * y * y + c, t * y * z - s * x),
        (t * x * z - s * y, t * y * z + s * x, t * z * z + c),
    )


def _apply(m, v):
    return tuple(sum(m[i][k] * v[k] for k in range(3)) for i in range(3))


def _matmul(a, b):
    return tuple(tuple(sum(a[i][k] * b[k][j] for k in range(3)) for j in range(3)) for i in range(3))


class _Placement:
    """A rotation and a translation, composed the way pc.Location is."""

    def __init__(self, translation=(0, 0, 0), rotation=None):
        self.t = tuple(float(v) for v in translation)
        self.m = rotation if rotation is not None else _rot((0, 0, 1), 0)

    @staticmethod
    def of(port):
        """The placement a port's OCCT location describes."""
        return _Placement(port[0], _rot(port[1], port[2]))

    def __mul__(self, other):
        moved = _apply(self.m, other.t)
        return _Placement(tuple(moved[i] + self.t[i] for i in range(3)), _matmul(self.m, other.m))

    def inverse(self):
        transposed = tuple(tuple(self.m[j][i] for j in range(3)) for i in range(3))
        moved = _apply(transposed, self.t)
        return _Placement(tuple(-v for v in moved), transposed)

    def axis(self, which):
        return tuple(round(v, 6) + 0.0 for v in _apply(self.m, which))

    def at(self, point):
        """Where a point of the placed part ends up."""
        moved = _apply(self.m, point)
        return tuple(round(moved[i] + self.t[i], 6) + 0.0 for i in range(3))

    def position(self):
        return tuple(round(v, 6) + 0.0 for v in self.t)

    def is_upright(self):
        return (self.axis((1, 0, 0)), self.axis((0, 1, 0)), self.axis((0, 0, 1))) == (
            (1.0, 0.0, 0.0),
            (0.0, 1.0, 0.0),
            (0.0, 0.0, 1.0),
        )


_MATE_FLIP = _Placement((0, 0, 0), _rot((1, 1, 0), 180))


def _connect(target_placement, target_port, source_port, freedom=None):
    """Where the source part lands when its port is mated to the target's."""
    location = target_placement * _Placement.of(target_port) * _MATE_FLIP
    if freedom is not None:
        location = location * freedom
    return location * _Placement.of(source_port).inverse()


def test_a_brick_stacks_a_brick_height_above_another():
    brick = plugin._lego_implements("Brick  2 x  4")
    upper = _connect(_Placement(), brick[STUD]["c0r0"], brick[ANTI]["c0r0"])
    assert upper.is_upright()
    assert upper.position() == (0.0, 9.6, 0.0)
    # A different stud is the same connection, one grid step along X.
    offset = _connect(_Placement(), brick[STUD]["c1r0"], brick[ANTI]["c0r0"])
    assert offset.position() == (8.0, 9.6, 0.0)


def test_a_plate_seats_on_a_brick_and_not_inside_it():
    brick = plugin._lego_implements("Brick  2 x  4")
    plate = plugin._lego_implements("Plate  2 x  4")
    where = _connect(_Placement(), brick[STUD]["c0r0"], plate[ANTI]["c0r0"])
    # The plate's own bottom plane (y = -3.2) lands on the brick's top (y = 0).
    assert where.at((0, -3.2, 0)) == (0.0, 0.0, 0.0)


def test_a_pin_goes_into_a_beam_and_a_second_beam_onto_the_pin():
    beam = plugin._lego_implements("Technic Beam  5")
    pin = plugin._lego_implements("Technic Pin")[PIN]

    # The pin's right-hand half enters the middle hole through the beam's top
    # face: the collar lands on that face and the pin points into the material.
    placed_pin = _connect(_Placement(), beam[PIN_HOLE]["h2-top"], pin["right"])
    assert placed_pin.position() == (0.0, 4.0, 0.0)
    assert placed_pin.axis((1, 0, 0)) == (0.0, -1.0, 0.0)

    # A second beam hangs its own hole on the half of the pin still sticking
    # out: it ends up parallel to the first one, one beam thickness above it.
    second = _connect(placed_pin, pin["left"], beam[PIN_HOLE]["h2-bottom"])
    assert second.is_upright()
    assert second.position() == (0.0, 8.0, 0.0)


def test_a_pin_joins_two_technic_bricks_face_to_face():
    brick = plugin._lego_implements("Technic Brick  1 x  4 with Holes")
    pin = plugin._lego_implements("Technic Pin")[PIN]
    placed_pin = _connect(_Placement(), brick[PIN_HOLE]["h1-front"], pin["right"])
    second = _connect(placed_pin, pin["left"], brick[PIN_HOLE]["h1-back"])
    # Both bricks upright, touching along the faces the pin went through.
    assert second.is_upright()
    assert second.position() == (0.0, 0.0, 8.0)


def test_an_axle_lies_through_an_axle_hole_and_moveZ_pushes_it_further():
    brick = plugin._lego_implements("Technic Brick  1 x  2 with Reduced Axlehole")
    axle = plugin._lego_implements("Technic Axle  4")
    placed = _connect(_Placement(), brick[AXLE_HOLE]["axle-front"], axle[AXLE]["left"])
    # The axle's left end sits in the mouth of the hole (z = 4) and the shaft
    # runs from there through the brick, along -Z.
    assert placed.at((-16.0, 0, 0)) == (0.0, -4.0, 4.0)
    assert placed.at((16.0, 0, 0)) == (0.0, -4.0, -28.0)
    # 'moveZ' is the freedom the interface declares: it drives the axle in.
    deeper = _connect(_Placement(), brick[AXLE_HOLE]["axle-front"], axle[AXLE]["left"], _Placement((0, 0, 8.0)))
    assert deeper.at((-16.0, 0, 0)) == (0.0, -4.0, -4.0)


def test_a_half_pin_carries_a_brick_on_its_stud():
    half = plugin._lego_implements("Technic Pin  1/2")
    beam = plugin._lego_implements("Technic Beam  3")
    brick = plugin._lego_implements("Brick  2 x  4")
    # Its pin end goes into a beam...
    placed = _connect(_Placement(), beam[PIN_HOLE]["h1-top"], half[PIN]["left"])
    assert placed.position() == (0.0, 4.0, 0.0)
    # ... and its stud takes an ordinary brick, which is what the part is for.
    on_top = _connect(placed, half[STUD]["stud"], brick[ANTI]["c0r0"])
    # The brick's bottom plane ends up on the stud, which points straight up.
    assert on_top.at((-12.0, -9.6, -4.0)) == (0.0, 4.0, 0.0)


# --- the demo assemblies ----------------------------------------------------
#
# 'lego-demo' connects the LDraw parts by naming interface instances, which only
# exist because this plugin attaches them. Building those assemblies needs the
# network and a CAD kernel; checking that every name they use is a name the
# plugin produces needs neither, and is what breaks if the instances are ever
# renamed. The descriptions below are the LDraw part headers of the ids used.

_DEMO_PARTS = {
    "3001": "Brick  2 x  4",
    "3003": "Brick  2 x  2",
    "3010": "Brick  1 x  4",
    "3020": "Plate  2 x  4",
    "3068": "Tile  2 x  2",
    "3673": "Technic Pin",
    "3701": "Technic Brick  1 x  4 with Holes",
    "3705": "Technic Axle  4",
    "32064b": "Technic Brick  1 x  2 with Reduced Axlehole",
    "32316": "Technic Beam  5",
    "3647": "Technic Gear  8 Tooth",
    "3648b": "Technic Gear 24 Tooth with Single Axle Hole",
    "4019": "Technic Gear 16 Tooth",
    "3815b": "Minifig Hips",
    "973": "Minifig Torso",
    "3626b": "Minifig Head",
}


def _demo_assemblies():
    yaml = pytest.importorskip("yaml")
    demo = os.path.join(_here, "lego-demo")
    for name in sorted(os.listdir(demo)):
        if name.endswith(".assy"):
            with open(os.path.join(demo, name)) as f:
                yield name, yaml.safe_load(f)


def _interfaces_declared_here():
    """The interfaces this package declares, from its own partcad.yaml."""
    yaml = pytest.importorskip("yaml")
    with open(os.path.join(_here, "partcad.yaml")) as f:
        return yaml.safe_load(f)["interfaces"]


def test_the_demo_assemblies_name_instances_and_parameters_that_exist():
    declared = _interfaces_declared_here()
    checked = 0
    for assembly_name, assembly in _demo_assemblies():
        parts = {link.get("name", link["part"]): link["part"] for link in assembly["links"]}
        for link in assembly["links"]:
            connect = link.get("connect")
            if connect is None:
                continue
            for part, interface, instance, params in (
                (link["part"], connect.get("with"), connect.get("withInstance"), connect.get("withParams")),
                (parts[connect["name"]], connect.get("to"), connect.get("toInstance"), connect.get("toParams")),
            ):
                if interface is None:
                    continue  # left for PartCAD to work out; nothing named to check
                where = "%s: %s (%s)" % (assembly_name, part, interface)
                implements = plugin._lego_implements(_DEMO_PARTS[part.rsplit(":", 1)[1]])
                assert interface in implements, "%s is not implemented" % where
                if instance is not None:
                    assert instance in implements[interface], "%s has no instance %s" % (where, instance)
                for param in params or {}:
                    short = interface.rsplit(":", 1)[1]
                    if short not in declared:
                        continue  # another package declares it; not this one's to check
                    # Only 'parameters' is settable: PartCAD ignores everything in
                    # a 'mates' entry but the description and the port selectors.
                    assert param in declared[short].get("parameters", {}), "%s has no parameter %s" % (where, param)
                checked += 1
    assert checked > 0


# --- the families beyond Technic --------------------------------------------
#
# As above, every coordinate here was read off the LDraw parts: the gears'
# pitch circles and tooth phase from 3647/4019/69779/3649, the Duplo grid from
# 3011 and 3437, and the minifig offsets from LDraw's own assembled minifigs
# (979 and 980), which place the torso 32 LDU above the hips and the head 28
# above the torso.

DUPLO_STUD = "//pub/universe/lego:duplo-stud"
DUPLO_ANTI = "//pub/universe/lego:duplo-anti-stud"
NECK = "//pub/universe/lego:minifig-neck"
NECK_SOCKET = "//pub/universe/lego:minifig-neck-socket"
WAIST = "//pub/universe/lego:minifig-waist"
WAIST_SOCKET = "//pub/universe/lego:minifig-waist-socket"
GEAR_TOOTH = "//pub/universe/lego:gear-tooth"
GEAR_GAP = "//pub/universe/lego:gear-gap"
WHEEL = "//pub/universe/lego:wheel-rim"
TYRE = "//pub/universe/lego:tyre-bore"
RJ12_PLUG = "//pub/universe/lego:rj12-plug"
RJ12_SOCKET = "//pub/universe/lego:rj12-socket"


def test_duplo_is_the_stud_system_at_twice_the_size():
    implements = plugin._lego_implements("Duplo Brick  2 x  4")
    assert set(implements) == {DUPLO_STUD, DUPLO_ANTI}
    studs = {tuple(port[0]) for port in implements[DUPLO_STUD].values()}
    assert studs == {(x, 0.0, z) for x in (-24.0, -8.0, 8.0, 24.0) for z in (-8.0, 8.0)}
    # ... on a body 19.2 mm deep, both twice the system brick
    assert {port[0][1] for port in implements[DUPLO_ANTI].values()} == {-19.2}


def test_duplo_takes_only_the_plain_name():
    # A quarter of the suffixed names do not have the full A x B grid.
    for desc in ("Duplo Brick  2 x  4 with Holes", "Duplo Brick  2 x  2 Hinge Base"):
        assert plugin._lego_implements(desc) is None, desc


def test_minifig_parts_carry_the_joints_of_their_class():
    head = plugin._lego_implements("Minifig Head with Standard Grin Pattern")
    torso = plugin._lego_implements("Minifig Torso")
    hips = plugin._lego_implements("Minifig Hips")
    assert set(head) == {NECK_SOCKET, STUD}
    assert set(torso) == {NECK, WAIST_SOCKET}
    assert set(hips) == {WAIST}
    # The head's top is an ordinary system stud, so hats and hair need nothing new.
    assert head[STUD]["stud"] == [[0, 0, 0], [1, 0, 0], 270]


def test_a_sculpted_head_is_not_a_standard_one():
    for desc in ("Minifig Head Yoda with Curved Ears Type 2", "Minifig Torso Brick  2 x  3"):
        assert plugin._lego_implements(desc) is None, desc


def test_gear_teeth_and_gaps_sit_on_the_pitch_circle():
    implements = plugin._lego_implements("Technic Gear 24 Tooth with Single Axle Hole")
    assert set(implements) == {GEAR_TOOTH, GEAR_GAP}
    assert len(implements[GEAR_TOOTH]) == 24 and len(implements[GEAR_GAP]) == 24
    # One module: the pitch diameter in millimetres is the tooth count.
    for ports in (implements[GEAR_TOOTH], implements[GEAR_GAP]):
        for port in ports.values():
            x, y, z = port[0]
            assert abs(math.hypot(x, y) - 12.0) < 1e-3 and z == 0
    # Tooth 0 is on the +X axis and the gap that follows it half a pitch on.
    assert implements[GEAR_TOOTH]["t0"][0] == [12.0, 0.0, 0]
    assert implements[GEAR_GAP]["g0"][0] == pytest.approx([11.8973, 1.5663, 0], abs=1e-3)  # half a pitch on: 7.5 deg


def test_a_gear_the_system_does_not_cut_is_left_alone():
    # 641 is the vintage 14-tooth gear, cut to a different module.
    assert plugin._lego_implements("Technic Gear 14 Tooth") is None
    # A bevel gear meshes at a right angle, which these ports do not describe.
    assert plugin._lego_implements("Technic Gear 20 Tooth Bevel") is None


def test_wheels_and_tyres_name_the_size_they_fit():
    wheel = plugin._lego_implements("Wheel 30 x 64 with  7 Pin Holes")
    tyre = plugin._lego_implements("Tyre 20/ 48 x 30")
    assert list(wheel[WHEEL]) == ["d64"]
    assert list(tyre[TYRE]) == ["d30"]
    assert plugin._lego_implements("Tyre 11.2/ 28 x 17.6 Intermediate")[TYRE]
    assert list(plugin._lego_implements("Tyre 11.2/ 28 x 17.6 Intermediate")[TYRE]) == ["d17.6"]


# --- the connections these ports produce, for the new families ---------------


def test_duplo_bricks_stack_a_duplo_height_apart():
    brick = plugin._lego_implements("Duplo Brick  2 x  4")
    upper = _connect(_Placement(), brick[DUPLO_STUD]["c0r0"], brick[DUPLO_ANTI]["c0r0"])
    assert upper.is_upright()
    assert upper.position() == (0.0, 19.2, 0.0)


def test_a_minifig_stacks_the_way_ldraw_draws_one():
    head = plugin._lego_implements("Minifig Head")
    torso = plugin._lego_implements("Minifig Torso")
    hips = plugin._lego_implements("Minifig Hips")
    on_hips = _connect(_Placement(), hips[WAIST]["waist"], torso[WAIST_SOCKET]["waist"])
    assert on_hips.is_upright()
    assert on_hips.position() == (0.0, 12.8, 0.0)  # 32 LDU, as 979 and 980 place it
    on_torso = _connect(on_hips, torso[NECK]["neck"], head[NECK_SOCKET]["neck"])
    assert on_torso.is_upright()
    assert on_torso.position() == (0.0, 24.0, 0.0)  # a further 28 LDU


def test_a_tyre_fits_a_wheel_concentrically():
    wheel = plugin._lego_implements("Wheel 30 x 64 with  7 Pin Holes")
    tyre = plugin._lego_implements("Tyre 20/ 48 x 30")
    fitted = _connect(_Placement(), wheel[WHEEL]["d64"], tyre[TYRE]["d30"])
    # LDraw draws the pair sharing one origin (4266c01, 22253c01, 22969ac01).
    assert fitted.is_upright()
    assert fitted.position() == (0.0, 0.0, 0.0)


def test_meshing_gears_end_up_a_pitch_radius_apart_and_coplanar():
    big = plugin._lego_implements("Technic Gear 24 Tooth")
    small = plugin._lego_implements("Technic Gear  8 Tooth")
    placed = _connect(_Placement(), big[GEAR_TOOTH]["t0"], small[GEAR_GAP]["g0"])
    # (24 + 8) / 2 = 16 mm between the centres, which is two studs.
    assert math.hypot(*placed.position()[:2]) == pytest.approx(16.0, abs=1e-3)
    # And the small gear's axle stays parallel to the big one's: a mesh that
    # tipped the second gear would be no mesh at all.
    assert placed.axis((0, 0, 1)) == pytest.approx((0.0, 0.0, 1.0), abs=1e-6)


def test_gears_of_every_size_mesh_at_the_distance_they_are_cut_for():
    for first, second, distance in ((8, 24, 16.0), (16, 16, 16.0), (24, 40, 32.0), (12, 20, 16.0)):
        a = plugin._lego_implements("Technic Gear %d Tooth" % first)
        b = plugin._lego_implements("Technic Gear %d Tooth Double Bevel" % second) or plugin._lego_implements(
            "Technic Gear %d Tooth" % second
        )
        placed = _connect(_Placement(), a[GEAR_TOOTH]["t0"], b[GEAR_GAP]["g0"])
        assert math.hypot(*placed.position()[:2]) == pytest.approx(distance, abs=1e-3), (first, second)


# --- ports read from the geometry -------------------------------------------
#
# The walk itself, with the library replaced by a few lines of LDraw: what it
# fetches, what it refuses to fetch, and that it reads the same subpart twice
# when a part references it twice.

_FAKE_LIBRARY = {
    # a part that places one subpart twice, in two places, and one primitive
    "55804.dat": (
        "0 Electric Mindstorms NXT Cable 20 cm\n"
        "1 16 -35 0 0 1 0 0 0 1 0 0 0 1 933c01.dat\n"
        "1 16 35 0 0 -1 0 0 0 1 0 0 0 -1 933c01.dat\n"
        "1 16 0 0 0 1 0 0 0 1 0 0 0 1 4-4cyli.dat\n"
    ),
    "933c01.dat": "0 ~Plug\n1 16 0 0 0 1 0 0 0 1 0 0 0 1 933.dat\n",
    # a part whose hole is inside its own subpart
    "3700.dat": "0 Technic Brick\n1 16 0 0 0 1 0 0 0 1 0 0 0 1 s/3700s01.dat\n",
    # a beam-style through hole: two mouths, at the ends of its own Y
    "99999c.dat": ("0 Technic Beam Test\n1 16 0 0 0 1 0 0 0 1 0 0 0 1 beamhole.dat\n"),
    # a pin end and the middle section of a long pin at the same place: only the
    # end is a port
    "99999d.dat": (
        "0 Technic Pin Test\n"
        "1 16 -10 0 0 0 1 0 0 0 1 1 0 0 confric5.dat\n"
        "1 16 -10 0 0 0 -1 0 0 0 1 1 0 0 confric8.dat\n"
    ),
    # a Mindstorms part with a cross axle hole: the profile spans y in [0, 1]
    # and its matrix stretches it 20 LDU through the part
    "99999a.dat": ("0 Electric Mindstorms Test Motor\n" "1 16 0 -10 0 1 0 0 0 20 0 0 0 1 axlehole.dat\n"),
    # ...and one that draws the same hole out of faces, with only a perimeter
    "99999b.dat": (
        "0 Electric Mindstorms Test Sensor\n"
        "1 16 0 -10 0 1 0 0 0 20 0 0 0 1 axl2hol8.dat\n"
        "1 16 0 -10 0 1 0 0 0 20 0 0 0 1 axl2hol2.dat\n"
    ),
    "s/3700s01.dat": "0 ~subpart\n1 16 0 10 10 1 0 0 0 0 1 0 -1 0 peghole.dat\n",
    # a 2 x 2 brick drawn the way LDraw draws one: a group of studs on top, a
    # tube underneath, and a cylinder that is neither.
    "3003.dat": (
        "0 Brick  2 x  2\n"
        "1 16 0 0 0 1 0 0 0 1 0 0 0 1 stug-2x2.dat\n"
        "1 16 0 4 0 1 0 0 0 -5 0 0 0 1 stud4.dat\n"
        "1 16 0 0 0 1 0 0 0 1 0 0 0 1 4-4cyli.dat\n"
    ),
    # the corner brick: three studs, on an origin a rectangular part never uses
    "2357.dat": (
        "0 Brick  2 x  2 Corner\n"
        "1 16 0 0 0 1 0 0 0 1 0 0 0 1 stud.dat\n"
        "1 16 0 0 20 1 0 0 0 1 0 0 0 1 stud.dat\n"
        "1 16 20 0 0 1 0 0 0 1 0 0 0 1 stud.dat\n"
        "1 16 0 4 10 1 0 0 0 -5 0 0 0 1 stud3.dat\n"
        "1 16 10 4 0 1 0 0 0 -5 0 0 0 1 stud3.dat\n"
    ),
    # a stud that does not face up: the headlight brick's second stud
    "4070.dat": (
        "0 Brick  1 x  1 with Headlight\n"
        "1 16 0 0 0 1 0 0 0 1 0 0 0 1 stud.dat\n"
        "1 16 0 10 -6 1 0 0 0 0 -1 0 1 0 stud.dat\n"
    ),
    # a minifig head: one stud, which its name rule calls "stud" and not "c0r0"
    "3626b.dat": ("0 Minifig Head\n1 16 0 0 0 1 0 0 0 1 0 0 0 1 stud.dat\n"),
    # a 1 x 1: one stud and no tube, so nothing contradicts the name
    "3005.dat": ("0 Brick  1 x  1\n1 16 0 0 0 1 0 0 0 1 0 0 0 1 stud.dat\n"),
    # a 1 x 2 brick: two studs on top, one SOLID tube between them underneath
    "3004.dat": (
        "0 Brick  1 x  2\n"
        "1 16 -10 0 0 1 0 0 0 1 0 0 0 1 stud.dat\n"
        "1 16 10 0 0 1 0 0 0 1 0 0 0 1 stud.dat\n"
        "1 16 0 4 0 1 0 0 0 -5 0 0 0 1 stud3.dat\n"
    ),
    # a minifig hat: one OPEN tube, flipped, whose far end is the origin - the
    # socket itself, not the spacer an open tube means under a brick
    "30167.dat": ("0 Minifig Hat Wide Brim Flat\n1 16 0 -4 0 1 0 0 0 -1 0 0 0 1 stud4.dat\n"),
    # ...and one that is not a socket: the tube does not open at the origin
    "99999.dat": ("0 Minifig Hat Nonsense\n1 16 0 40 0 1 0 0 0 -1 0 0 0 1 stud4.dat\n"),
    # a part whose studs belong to another building system
    "3011.dat": (
        "0 Duplo Brick  2 x  4\n"
        "1 16 0 0 0 1 0 0 0 1 0 0 0 1 stug20-2x2.dat\n"
        "1 16 0 0 0 1 0 0 0 1 0 0 0 1 stud7.dat\n"
    ),
}


@pytest.fixture
def fake_library(monkeypatch):
    fetched = []

    def fetch(name):
        fetched.append(name)
        return _FAKE_LIBRARY.get(name)

    monkeypatch.setattr(plugin, "_fetch_ldraw_file", fetch)
    return fetched


def test_the_walk_reads_a_subpart_once_per_placement(fake_library):
    connectors = plugin._geometry_connectors("55804")
    plugs = [c for c in connectors if c[0] == RJ12_PLUG]
    # Two plugs, because the cable references the same subpart at both ends;
    # the second placement is mirrored, so its port faces the other way.
    assert len(plugs) == 2
    assert sorted(tuple(round(v) for v in c[1]) for c in plugs) == [(-35, 0, -18), (35, 0, 18)]


def test_the_walk_never_fetches_a_primitive(fake_library):
    plugin._geometry_connectors("55804")
    assert "4-4cyli.dat" not in fake_library
    # ... and fetches each file it does need only once
    assert sorted(fake_library) == ["55804.dat", "933c01.dat"]


def test_the_walk_descends_into_subparts(fake_library):
    connectors = plugin._geometry_connectors("3700")
    assert "s/3700s01.dat" in fake_library
    assert [(c[0], tuple(round(v) for v in c[1])) for c in connectors] == [(PIN_HOLE, (0, 10, 10))]


def test_a_part_ref_is_a_number_and_a_primitive_is_a_word():
    for name in ("3001.dat", "3626b.dat", "32064a.dat", "s/3700s01.dat"):
        assert plugin._PART_REF_RE.match(name), name
    for name in ("peghole.dat", "4-4cyli.dat", "stud2a.dat", "box5.dat", "connect.dat"):
        assert not plugin._PART_REF_RE.match(name), name


def test_a_u_prefixed_file_is_a_part_and_a_hyphenated_one_is_not():
    # 625 files under parts/ are named u<digits>; u9449 and u9450 are the RCX
    # modules, and each holds two pin holes.
    for name in ("u9449.dat", "u9208.dat", "s/u9013.dat"):
        assert plugin._PART_REF_RE.match(name), name
    # ...but a hyphen stays out, or every digit-initial primitive becomes
    # something the walk descends into and the file budget goes on geometry
    # that holds no connectors.
    for name in ("4-4cyli.dat", "1-4ndis.dat", "2-4disc.dat", "4-4edge.dat"):
        assert not plugin._PART_REF_RE.match(name), name


def test_geometry_ports_land_where_the_geometry_says(fake_library):
    implements = plugin._geometry_connector_implements("3700")
    # LDraw (0, 10, 10) is (0, -4, 4) once the wrapper has meshed it, and the
    # port faces out of the part, the way the name-derived holes do.
    port = implements[PIN_HOLE]["h0"]
    assert port[0] == [0.0, -4.0, 4.0]
    turned = _Placement.of(port)
    assert turned.axis((0, 0, 1)) == pytest.approx((0.0, 0.0, 1.0), abs=1e-6)


def test_an_orientation_survives_the_round_trip():
    for axis, angle in (((1, 0, 0), 90), ((0, 1, 0), 270), ((1, 1, -1), 120), ((0, 0, 1), 0)):
        rotation = _rot(axis, angle)
        again = plugin._orientation_of_matrix(rotation)
        assert _Placement((0, 0, 0), _rot(again[0], again[1])).axis((1, 2, 3)) == pytest.approx(
            _Placement((0, 0, 0), rotation).axis((1, 2, 3)), abs=1e-6
        )


def test_a_stud_primitive_says_what_it_is():
    # LDraw's own vocabulary: "Stud" is male, "Stud Tube ..." the socket under
    # it, "Stud Group A x B" a grid of either, and Duplo is another system.
    assert plugin._stud_primitive("stud.dat") == ("stud", plugin._ORIGIN_ONLY)
    assert plugin._stud_primitive("stud2a.dat") == ("stud", plugin._ORIGIN_ONLY)
    assert plugin._stud_primitive("stud4.dat") == ("tube", plugin._ORIGIN_ONLY)
    assert plugin._stud_primitive("stud3.dat") == ("tube", plugin._ORIGIN_ONLY)
    # the low-resolution spellings fold onto the ones they alias
    assert plugin._stud_primitive("stu24a.dat") == ("tube", plugin._ORIGIN_ONLY)
    assert plugin._stud_primitive("stu2.dat") == ("stud", plugin._ORIGIN_ONLY)
    # ...and anything that is not a stud at all is left for the walk
    assert plugin._stud_primitive("4-4cyli.dat") == (None, None)
    assert plugin._stud_primitive("peghole.dat") == (None, None)


def test_a_stud_group_is_expanded_and_not_fetched():
    kind, offsets = plugin._stud_primitive("stug-1x4.dat")
    assert kind == "stud"
    # A x B is A along Z by B along X, the same way a brick's name reads
    assert sorted(offsets) == [(-30.0, 0.0, 0.0), (-10.0, 0.0, 0.0), (10.0, 0.0, 0.0), (30.0, 0.0, 0.0)]
    assert sorted(plugin._stud_primitive("stug-4x1.dat")[1]) == [
        (0.0, 0.0, -30.0),
        (0.0, 0.0, -10.0),
        (0.0, 0.0, 10.0),
        (0.0, 0.0, 30.0),
    ]
    # a group is named after what it groups, so the tubes and the other
    # building systems classify themselves
    assert plugin._stud_primitive("stug4-2x2.dat")[0] == "tube"
    assert plugin._stud_primitive("stug10-2x2.dat")[0] == "stud"  # cut for a round 2 x 2
    assert plugin._stud_primitive("stug20-2x2.dat") == (None, None)  # Duplo
    assert plugin._stud_primitive("stug19-1x2.dat") == (None, None)  # Scala
    # "stug4.dat" is an alias for "stug-4x4", not a group of four
    assert len(plugin._stud_primitive("stug4.dat")[1]) == 16


def test_a_rectangular_part_keeps_the_studs_its_name_gave_it(fake_library):
    from_name = plugin._lego_implements("Brick  2 x  2")[STUD]
    from_geometry = plugin._lego_implements("Brick  2 x  2", "3003")[STUD]
    # identical, names and ports both: reading the geometry must not renumber
    # the grid that assemblies already refer to
    assert from_geometry == from_name
    assert sorted(from_geometry) == ["c0r0", "c0r1", "c1r0", "c1r1"]


def test_the_corner_brick_gets_the_studs_it_has(fake_library):
    implements = plugin._lego_implements("Brick  2 x  2 Corner", "2357")
    studs = implements[STUD]
    # three, not the four the name implies, and on the part's own origin
    assert len(studs) == 3
    assert sorted(p[0] for p in studs.values()) == [[0.0, 0.0, 0.0], [0.0, 0.0, 8.0], [8.0, 0.0, 0.0]]
    # and the underside matches: the two solid tubes put three anti-studs under
    # the three studs, where the name put four in a square
    assert len(implements[ANTI]) == 3


def test_a_stud_that_does_not_face_up_is_still_a_stud(fake_library):
    studs = plugin._lego_implements("Brick  1 x  1 with Headlight", "4070")[STUD]
    assert len(studs) == 2
    facings = sorted(tuple(port[1]) + (port[2],) for port in studs.values())
    # one up the way a name-derived stud faces, one out the front
    assert list(plugin._Z_TO_PLUS_Y[0]) + [plugin._Z_TO_PLUS_Y[1]] in [list(f[:3]) + [f[3]] for f in facings]
    assert len({f for f in facings}) == 2


def test_another_building_system_is_not_a_stud(fake_library):
    # Duplo studs are Duplo's; the system stud interface must not claim them
    implements = plugin._lego_implements("Duplo Brick  2 x  4", "3011")
    assert STUD not in (implements or {})


def test_the_name_rule_stands_when_the_walk_runs_out_of_budget(monkeypatch, fake_library):
    monkeypatch.setattr(plugin, "_GEOMETRY_FILES", 0)
    assert plugin._geometry_stud_implements("3003") is None
    # ...so the part keeps the studs its name gives it rather than losing them
    assert sorted(plugin._lego_implements("Brick  2 x  2", "3003")[STUD]) == ["c0r0", "c0r1", "c1r0", "c1r1"]


def test_the_name_rule_stands_when_the_walk_runs_out_of_time(monkeypatch, fake_library):
    """Off the network the files are the bound; on it the clock is.

    PartCAD stops asking this plugin anything for the rest of a command once a
    script blows its deadline, so a part outside the index must not spend the
    whole of it fetching geometry.
    """
    monkeypatch.setattr(plugin, "_GEOMETRY_SECONDS", -1.0)
    assert plugin._geometry_stud_implements("3003") is None
    assert sorted(plugin._lego_implements("Brick  2 x  2", "3003")[STUD]) == ["c0r0", "c0r1", "c1r0", "c1r1"]


def test_the_geometry_keeps_the_names_the_name_rule_gave(fake_library):
    # A minifig head's single stud is "stud", not the "c0r0" a grid would call
    # it, and an assembly hanging a hat on one already says so. When geometry
    # finds exactly the studs the name did, the naming has to survive.
    studs = plugin._lego_implements("Minifig Head", "3626b")[STUD]
    assert sorted(studs) == ["stud"]
    # ...while a part the name got wrong is renamed onto the grid, because its
    # studs are not the ones the name described
    assert sorted(plugin._lego_implements("Brick  2 x  2 Corner", "2357")[STUD]) == ["c0r0", "c0r1", "c1r0"]


def test_an_open_tube_is_four_anti_studs_and_a_solid_one_is_two(fake_library):
    # 2 x 2: one "Stud Tube Open" at the centre of the four
    square = plugin._geometry_anti_studs("3003")
    assert len(square) == 4
    assert sorted(p[0][:1] + p[0][2:] for p in square.values()) == [[-4.0, -4.0], [-4.0, 4.0], [4.0, -4.0], [4.0, 4.0]]
    # 1 x 2: one "Stud Tube Solid" between the two, and the studs say which axis
    strip = plugin._geometry_anti_studs("3004")
    assert sorted(p[0] for p in strip.values()) == [[-4.0, -9.6, 0.0], [4.0, -9.6, 0.0]]


def test_the_anti_studs_sit_on_the_plane_the_tube_reaches():
    # a tube spans y in [-4, 0] in its own frame, so its far end is the bottom
    # of the part: 24 LDU for a brick, which is -9.6 mm once meshed
    assert plugin._ANTI_PLANE_OFFSET == (0.0, -4.0, 0.0)


def test_the_corner_brick_gets_the_underside_it_has(fake_library):
    anti = plugin._lego_implements("Brick  2 x  2 Corner", "2357")[ANTI]
    # three, under its three studs, not the four the name implies
    assert len(anti) == 3
    assert sorted(p[0] for p in anti.values()) == [[0.0, -9.6, 0.0], [0.0, -9.6, 8.0], [8.0, -9.6, 0.0]]


def test_a_part_the_tubes_do_not_settle_keeps_the_name_grid(fake_library):
    # a 1 x 1 has no tube at all, so nothing contradicts the name
    assert plugin._geometry_anti_studs("3005") is None
    assert sorted(plugin._lego_implements("Brick  1 x  1", "3005")[ANTI]) == ["c0r0"]


def test_headgear_takes_its_socket_from_the_geometry(fake_library):
    # the same open tube that means "spacer" under a brick means "socket" here,
    # which is why the name picks the family and the geometry confirms it
    hat = plugin._lego_implements("Minifig Hat Wide Brim Flat", "30167")
    assert sorted(hat[ANTI]) == ["anti"]
    assert hat[ANTI]["anti"][0] == [0.0, 0.0, 0.0]
    # a tube that does not open at the origin is not a socket, and is left alone
    assert plugin._lego_implements("Minifig Hat Nonsense", "99999") is None


def test_the_three_quarter_pin_says_which_end_is_which():
    # LDraw places "connect" (a full pin) toward -X and "connect3" (a half one)
    # toward +X; the name says neither, which is why this needed the geometry
    pin = plugin._lego_implements("Technic Pin  3/4")[PIN]
    assert sorted(pin) == ["left", "rightHalf"]
    assert pin["left"][1:] == [list(plugin._Z_TO_MINUS_X[0]), plugin._Z_TO_MINUS_X[1]]
    assert pin["rightHalf"][1:] == [list(plugin._Z_TO_PLUS_X[0]), plugin._Z_TO_PLUS_X[1]]


def test_an_axle_hole_has_a_mouth_at_each_end_of_its_stretch(fake_library):
    holes = plugin._lego_implements("Electric Mindstorms Test Motor", "99999a")[AXLE_HOLE]
    # two mouths, at the two ends of the 20 LDU the matrix stretches it over
    assert len(holes) == 2
    assert sorted(p[0][1] for p in holes.values()) == [-4.0, 4.0]


def test_a_perimeter_marks_a_hole_a_whole_form_would_have_missed(fake_library):
    # 32064b, "Technic Brick 1 x 2 with Reduced Axlehole", draws its hole out of
    # faces alone; the "Perimeter" is the one that appears once per hole, while
    # "Side Edges" and the rest appear several times and must not count
    holes = plugin._lego_implements("Electric Mindstorms Test Sensor", "99999b")[AXLE_HOLE]
    assert len(holes) == 2


def test_a_through_hole_is_two_mouths_and_a_peg_hole_is_one(fake_library):
    beam = plugin._lego_implements("Technic Beam Test", "99999c")[PIN_HOLE]
    assert len(beam) == 2
    # +-10 LDU is +-4 mm once meshed, and each mouth faces out of the part
    assert sorted(p[0][1] for p in beam.values()) == [-4.0, 4.0]


def test_the_middle_of_a_long_pin_is_not_a_pin_end(fake_library):
    # 6558 places a "Middle Slotted" section at the same spot as its left end;
    # counting it would give the pin a third port on top of the two it has
    pins = plugin._lego_implements("Technic Pin Test", "99999d")[PIN]
    assert len(pins) == 1


def test_a_technic_part_keeps_the_port_names_its_rule_gave_it(fake_library):
    # The geometry reaches far more parts than the name rules, but where the two
    # describe the same ports the name's instance names stand, so an assembly
    # that says "left" or "h0-top" keeps working.
    found = plugin._geometry_connector_implements("99999c")[PIN_HOLE]
    named = {"h0-top": port for port in list(found.values())[:1]}
    named["h0-bottom"] = list(found.values())[1]
    merged = plugin._with_geometry_technic({PIN_HOLE: named}, "99999c")
    assert sorted(merged[PIN_HOLE]) == ["h0-bottom", "h0-top"]
    # ...but a part whose ports the name got wrong is renamed onto the geometry
    wrong = {"only": plugin._port((0.0, 0.0, 0.0), plugin._Z_TO_PLUS_Y)}
    replaced = plugin._with_geometry_technic({PIN_HOLE: wrong}, "99999c")
    assert sorted(replaced[PIN_HOLE]) == ["h0", "h1"]


# --- how the runtime actually runs this file --------------------------------
#
# Everything above imports the module. PartCAD does not: it runs it with
# runpy.run_path(run_name=request["api"]), top to bottom, so a helper defined
# below the dispatch does not exist by the time get() reaches it. An import
# cannot see that, which is why the whole plugin could fail in production with
# every test here passing.


def _run_plugin(key):
    """Answer one key the way partcad/wrappers/wrapper_plugin.py does."""
    import runpy

    result = runpy.run_path(
        os.path.join(_here, "ldraw_repo.py"),
        init_globals={"request": {"key": key, "api": "get"}},
        run_name="get",
    )
    return result["output"]["result"]


@pytest.mark.slow
def test_the_dispatch_runs_after_every_helper_it_needs():
    # The production failure, reproduced: resolving a real part reaches
    # _lego_implements -> _with_geometry_technic -> _geometry_connector_implements,
    # and with the dispatch in the middle of the file that last one is not
    # defined yet. Needs network; the offline guard is the AST test below.
    try:
        cfg = _run_plugin("Brick/objects/part/3001")
    except NameError as e:
        raise AssertionError(
            "the plugin ran into a name that does not exist yet: %s - the "
            "__name__ dispatch is executing before the rest of the file" % e
        )
    except Exception as e:
        pytest.skip("LDraw could not be reached: %s" % e)
    if cfg is None:
        pytest.skip("LDraw returned nothing for 3001 (offline?)")
    assert cfg["dat"] == "3001.dat"


def test_an_unknown_api_name_produces_an_empty_output():
    import runpy

    result = runpy.run_path(
        os.path.join(_here, "ldraw_repo.py"),
        init_globals={"request": {"key": "meta", "api": "nonesuch"}},
        run_name="nonesuch",
    )
    assert result["output"] == {}


def test_every_helper_is_defined_before_the_dispatch():
    # The failure mode in prose: no top-level statement that calls into the
    # plugin may appear before the last definition in the file.
    import ast

    tree = ast.parse(open(os.path.join(_here, "ldraw_repo.py")).read())
    last_def = max(
        node.lineno for node in tree.body if isinstance(node, (ast.FunctionDef, ast.ClassDef, ast.Assign))
    )
    dispatch = [node for node in tree.body if isinstance(node, ast.If)]
    assert dispatch, "the __name__ dispatch went missing"
    assert dispatch[-1].lineno > last_def, (
        "the __name__ dispatch must come after every definition: runpy.run_path "
        "executes this file top to bottom"
    )


# --- the part config carries what the shape cache keys on -------------------


def test_part_config_declares_the_dat_as_a_parameter():
    # PartCAD hashes only 'parameters', 'offset' and 'scale' out of a part's
    # config, so without this every part this repository serves shares one
    # shape-cache entry and they render as each other.
    a = plugin._part_config("3001", ("Brick  2 x  4", None, None))
    b = plugin._part_config("3003", ("Brick  2 x  2", None, None))
    assert a["parameters"]["dat"]["default"] == "3001.dat"
    assert b["parameters"]["dat"]["default"] == "3003.dat"
    assert a["parameters"] != b["parameters"]


# --- what happens when a fetch does not produce a body ----------------------
#
# library.ldraw.org rate-limits bursts, so this is an ordinary occurrence
# rather than an edge case, and three separate things used to go wrong when it
# happened: the failure was not remembered, a part that could not be described
# was reported as not existing, and a part served without its metadata lost its
# interfaces without saying so.


class _HTTPError(Exception):
    """Stands in for urllib.error.HTTPError, which needs a real response."""

    def __init__(self, code, headers=None):
        super().__init__(str(code))
        self.code = code
        self.headers = headers or {}


def test_a_404_is_an_answer_and_is_not_retried(monkeypatch):
    import urllib.error

    calls = []

    def _urlopen(req, timeout=None):
        calls.append(req.full_url)
        raise urllib.error.HTTPError(req.full_url, 404, "Not Found", {}, None)

    monkeypatch.setattr(plugin.urllib.request, "urlopen", _urlopen)
    monkeypatch.setattr(plugin.time, "sleep", lambda s: None)
    body, reason = plugin._http_get("https://example.invalid/x.dat")
    assert body is None
    assert reason == plugin._MISSING
    assert len(calls) == 1, "a 404 is conclusive; retrying it only costs time"


def test_a_server_that_says_nothing_is_retried_then_reported_unavailable(monkeypatch):
    calls = []

    def _urlopen(req, timeout=None):
        calls.append(req.full_url)
        raise OSError("connection reset")

    monkeypatch.setattr(plugin.urllib.request, "urlopen", _urlopen)
    monkeypatch.setattr(plugin.time, "sleep", lambda s: None)
    body, reason = plugin._http_get("https://example.invalid/x.dat")
    assert body is None
    assert reason == plugin._UNAVAILABLE
    assert len(calls) == plugin._HTTP_RETRIES


def test_a_throttled_fetch_waits_as_long_as_it_was_asked_to():
    assert plugin._retry_after(_HTTPError(429, {"Retry-After": "5"}), 0.5) == 5.0
    # No header, or one in the HTTP-date form, leaves the backoff as it was.
    assert plugin._retry_after(_HTTPError(429), 0.5) == 0.5
    assert plugin._retry_after(_HTTPError(429, {"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"}), 0.5) == 0.5
    # An absurd wait is capped rather than obeyed.
    assert plugin._retry_after(_HTTPError(429, {"Retry-After": "86400"}), 0.5) == 60.0


def test_a_failed_fetch_is_remembered_so_the_next_run_is_not_this_one(tmp_path, monkeypatch):
    monkeypatch.setenv("PARTCAD_LDRAW_CACHE", str(tmp_path))
    attempts = []

    def _get(url):
        attempts.append(url)
        return None, plugin._UNAVAILABLE

    monkeypatch.setattr(plugin, "_http_get", _get)
    assert plugin._cached("https://example.invalid/x.dat", "parts/x.dat") is None
    assert plugin._cached("https://example.invalid/x.dat", "parts/x.dat") is None
    assert len(attempts) == 1, "the second call should have read the negative entry"


def test_a_negative_entry_expires(tmp_path, monkeypatch):
    monkeypatch.setenv("PARTCAD_LDRAW_CACHE", str(tmp_path))
    plugin._remember_negative("parts/x.dat", plugin._UNAVAILABLE)
    assert plugin._negative_is_fresh("parts/x.dat")
    monkeypatch.setattr(plugin.time, "time", lambda: 1e12)  # long past the TTL
    assert not plugin._negative_is_fresh("parts/x.dat")


def test_the_library_not_having_a_file_is_remembered_for_longer_than_a_bad_day():
    assert plugin._NEGATIVE_TTL[plugin._MISSING] > plugin._NEGATIVE_TTL[plugin._UNAVAILABLE]


def test_a_fetch_is_not_attempted_again_until_the_entry_goes_stale(tmp_path, monkeypatch):
    monkeypatch.setenv("PARTCAD_LDRAW_CACHE", str(tmp_path))
    plugin._remember_negative("parts/x.dat", plugin._UNAVAILABLE)
    monkeypatch.setattr(plugin, "_http_get", lambda url: ("0 Brick  1 x  1\n", None))

    # While the entry is fresh the fetch does not happen at all, which is the
    # point of it: a throttled run must not be repeated immediately.
    assert plugin._cached("https://example.invalid/x.dat", "parts/x.dat") is None

    # Once it is stale the fetch happens, and succeeding clears the entry so a
    # later failure starts a fresh TTL rather than inheriting this one.
    real_time = plugin.time.time
    monkeypatch.setattr(plugin.time, "time", lambda: real_time() + 1e6)
    assert plugin._cached("https://example.invalid/x.dat", "parts/x.dat")
    assert not os.path.exists(plugin._negative_path("parts/x.dat"))


# --- a part the category lists is a part ------------------------------------


def test_a_listed_part_whose_header_cannot_be_read_is_still_served(monkeypatch):
    monkeypatch.setattr(plugin, "_categories", lambda: {"Cone": "Cone"})
    monkeypatch.setattr(plugin, "_dat_header", lambda pid, category=None: None)
    monkeypatch.setattr(plugin, "_part_ids", lambda category: ["3942a", "3942c"])
    cfg = plugin.get("Cone/objects/part/3942c")
    # 'pc list' showed this part; 'pc render' used to say it did not exist.
    assert cfg is not None
    assert cfg["dat"] == "3942c.dat"
    assert "desc" not in cfg


def test_an_id_the_category_does_not_have_is_still_not_found(monkeypatch):
    monkeypatch.setattr(plugin, "_categories", lambda: {"Cone": "Cone"})
    monkeypatch.setattr(plugin, "_dat_header", lambda pid, category=None: None)
    monkeypatch.setattr(plugin, "_part_ids", lambda category: ["3942a", "3942c"])
    assert plugin.get("Cone/objects/part/nosuchpart") is None


def test_the_catalog_and_a_single_lookup_agree_about_what_exists(monkeypatch):
    """The two used to disagree, which is the whole of this bug."""
    monkeypatch.setattr(plugin, "_categories", lambda: {"Cone": "Cone"})
    monkeypatch.setattr(plugin, "_dat_header", lambda pid, category=None: None)
    monkeypatch.setattr(plugin, "_part_ids", lambda category: ["3942a", "3942c"])
    catalog = plugin.get("Cone/objects/part")
    for pid in catalog:
        assert plugin.get("Cone/objects/part/%s" % pid) is not None


# --- a part served without its metadata says so -----------------------------


def test_a_part_without_metadata_says_why_it_has_no_interfaces(capsys):
    plugin._warned_metadata.discard("zzz-not-a-part")
    cfg = plugin._part_config("zzz-not-a-part", None)
    assert "implements" not in cfg
    said = capsys.readouterr().err
    assert "zzz-not-a-part" in said
    assert "interfaces" in said


def test_it_is_said_once_per_part_rather_than_once_per_lookup(capsys):
    plugin._warned_metadata.discard("zzz-not-a-part")
    plugin._part_config("zzz-not-a-part", None)
    capsys.readouterr()
    plugin._part_config("zzz-not-a-part", None)
    assert capsys.readouterr().err == ""


def test_an_indexed_part_keeps_its_interfaces_even_with_no_header(capsys):
    """The index covers for a header that could not be read.

    Interfaces used to be derived from the description alone, so an unreadable
    header cost a part all of them. For a part the index knows, it no longer
    does - and there is then nothing to warn about.
    """
    plugin._warned_metadata.discard("3941")
    cfg = plugin._part_config("3941", None)
    assert cfg["implements"]["//pub/universe/lego:anti-stud"]
    assert capsys.readouterr().err == ""


def test_a_part_with_metadata_says_nothing(capsys):
    plugin._part_config("3001", ("Brick  2 x  4", None, None))
    assert capsys.readouterr().err == ""


# --- the shipped index ------------------------------------------------------
#
# Answering "which parts are in this category" from the network meant one HTTP
# request per part, and PartCAD asks that in order to resolve any single part.
# The index makes it a file read. These tests hold it to that: the network is
# replaced with something that raises, so anything reaching for it fails here.


def _write_index(path, categories, strings=(), format=None):
    """Write an index zip in the shape the plugin reads."""
    import zipfile as _zipfile

    meta = {
        "format": plugin._INDEX_FORMAT if format is None else format,
        "strings": list(strings),
        "categories": {name: list(parts) for name, parts in categories.items()},
    }
    with _zipfile.ZipFile(path, "w", _zipfile.ZIP_DEFLATED) as z:
        z.writestr(plugin._INDEX_META, json.dumps(meta))
        for name, parts in categories.items():
            z.writestr(plugin.member_name(name), json.dumps(parts))


def _fake_index(categories, strings=(), tmp=None):
    """An in-memory _Index over 'categories', for the tests that need a small one."""
    import tempfile as _tempfile

    path = os.path.join(tmp or _tempfile.mkdtemp(prefix="ldraw-index-"), plugin._INDEX_FILE)
    _write_index(path, categories, strings)
    return plugin._Index(path)


@pytest.fixture
def _no_network(monkeypatch):
    def _forbidden(*args, **kwargs):
        raise AssertionError("the index should have answered this without fetching")

    monkeypatch.setattr(plugin, "_cached", _forbidden)
    monkeypatch.setattr(plugin, "_http_get", _forbidden)
    plugin._index_loaded = None
    yield
    plugin._index_loaded = None


def test_the_index_ships_with_the_package():
    assert os.path.exists(os.path.join(_here, plugin._INDEX_FILE))


def test_the_categories_come_from_the_index(_no_network):
    cats = plugin._categories()
    assert len(cats) > 50
    # The mapping is {sub_package_name: ldraw_category}, and a category whose
    # name has a space is a different string on each side of it.
    assert cats["Brick"] == "Brick"
    assert cats["Minifig-Accessory"] == "Minifig Accessory"


def test_a_whole_category_is_a_file_read(_no_network):
    ids = plugin._part_ids("Brick")
    assert len(ids) > 1000
    assert "3001" in ids and "3941" in ids


def test_a_part_is_described_without_being_fetched(_no_network):
    assert plugin._dat_header("3001")[0] == "Brick  2 x  4"
    assert plugin._dat_header("3941")[0].startswith("Brick  2 x  2 Round")


def test_the_catalog_of_a_category_needs_no_network_at_all(_no_network):
    catalog = plugin._catalog("Cone")
    assert catalog["3942b"]["desc"].startswith("Cone  2 x  2 x  2")
    # ...and the interfaces are worked out from those names, as always.
    assert "//pub/universe/lego:stud" in catalog["3942b"]["implements"]


def test_a_part_the_index_does_not_have_is_still_fetched(monkeypatch):
    plugin._index_loaded = None
    fetched = []

    def _fake_cached(url, rel):
        fetched.append(rel)
        return "0 Unofficial Thing\n0 Author: Someone\n"

    monkeypatch.setattr(plugin, "_cached", _fake_cached)
    try:
        assert plugin._dat_header("u9999zzz")[0] == "Unofficial Thing"
        assert fetched, "an id outside the index has to fall back to the network"
    finally:
        plugin._index_loaded = None


def test_the_index_can_be_ignored_on_purpose(monkeypatch):
    """PARTCAD_LDRAW_IGNORE_INDEX is how the index is checked against ldraw.org."""
    monkeypatch.setenv("PARTCAD_LDRAW_IGNORE_INDEX", "1")
    plugin._index_loaded = None
    try:
        assert plugin._index() is False
    finally:
        plugin._index_loaded = None


def test_an_index_this_plugin_cannot_read_is_not_guessed_at(tmp_path, monkeypatch):
    """A newer format falls back to the network rather than misreading it."""
    _write_index(tmp_path / plugin._INDEX_FILE, {}, format=plugin._INDEX_FORMAT + 1)
    monkeypatch.setattr(plugin.os.path, "dirname", lambda p: str(tmp_path))
    plugin._index_loaded = None
    try:
        assert plugin._load_index() is False
    finally:
        plugin._index_loaded = None


def test_an_index_that_is_not_a_zip_is_not_guessed_at(tmp_path, monkeypatch):
    (tmp_path / plugin._INDEX_FILE).write_bytes(b"not a zip")
    monkeypatch.setattr(plugin.os.path, "dirname", lambda p: str(tmp_path))
    plugin._index_loaded = None
    try:
        assert plugin._load_index() is False
    finally:
        plugin._index_loaded = None


def test_the_index_agrees_with_itself(_no_network):
    """Every part the categories list is one the header lookup can describe."""
    index = plugin._index()
    for category in list(index.categories)[:5]:
        for pid in index.part_ids(category)[:20]:
            assert index.entry(pid, category) is not None, "%s/%s" % (category, pid)


def test_a_key_that_names_no_category_reads_no_category(_no_network, monkeypatch):
    """What makes the index cheap enough to read once per key.

    PartCAD runs this script afresh for every key, so the metadata, the child
    list and the object kinds this repository does not serve must not pay for
    the twenty thousand parts they are not about.
    """
    index = plugin._index()
    read = []
    original = index.parts
    monkeypatch.setattr(index, "parts", lambda category: read.append(category) or original(category))
    assert plugin.get("deps")
    assert plugin.get("meta")
    assert plugin.get("Brick/meta")
    assert plugin.get("Brick/objects/sketch") == {}
    assert plugin.get("Brick/objects/partType")
    assert read == []
    # ...and a key that does name one reads that one and no other.
    plugin.get("Brick/objects/part")
    assert set(read) == {"Brick"}


def test_every_category_has_a_member_of_its_own(_no_network):
    """The builder refuses a library whose categories collide here."""
    index = plugin._index()
    members = {plugin.member_name(c) for c in index.categories}
    assert len(members) == len(index.categories)
    for category in index.categories:
        assert index.parts(category), category


def test_the_metadata_says_which_object_kinds_there_are(_no_network):
    """So PartCAD stops asking after the seven kinds no category has ever had."""
    assert plugin.get("meta")["objectKinds"] == ["partType"]
    assert plugin.get("Brick/objects/part")
    assert plugin.get("Brick/meta")["objectKinds"] == ["part", "partType"]
    for kind in plugin.get("Brick/meta")["objectKinds"]:
        assert plugin.get("Brick/objects/" + kind)


def test_the_interfaces_come_from_the_index_too(_no_network):
    """The expensive half. Deriving these reads the part's geometry, so doing
    it at run time means fetching every .dat in the category as well."""
    cfg = plugin._part_config("3001", plugin._dat_header("3001"))
    studs = cfg["implements"]["//pub/universe/lego:stud"]
    assert len(studs) == 8  # a 2 x 4 brick
    assert cfg["implements"]["//pub/universe/lego:anti-stud"]


def test_a_part_the_index_says_has_no_interfaces_is_not_re_derived(monkeypatch):
    """None and 'never heard of it' are different answers.

    Re-deriving on None would put the geometry walk back for every part that
    genuinely has no interfaces, which is most of the library.
    """
    plugin._index_loaded = None
    monkeypatch.setattr(plugin, "_index", lambda: _fake_index({"Misc": {"x1": ["Some Part", -1, -1, None]}}))
    monkeypatch.setattr(
        plugin, "_lego_implements", lambda *a: (_ for _ in ()).throw(AssertionError("re-derived"))
    )
    try:
        assert "implements" not in plugin._part_config("x1", ("Some Part", None, None))
    finally:
        plugin._index_loaded = None


def test_a_part_outside_the_index_still_has_its_interfaces_worked_out(monkeypatch):
    plugin._index_loaded = None
    monkeypatch.setattr(plugin, "_index", lambda: _fake_index({}))
    monkeypatch.setattr(plugin, "_lego_implements", lambda desc, pid: {"iface": {"a": 1}})
    try:
        cfg = plugin._part_config("u9999", ("Brick  1 x  1", None, None))
        assert cfg["implements"] == {"iface": {"a": 1}}
    finally:
        plugin._index_loaded = None


# --- the listing, when there is no index to read it from ---------------------
#
# What 'build_parts_index.py' and PARTCAD_LDRAW_IGNORE_INDEX=1 use. ldraw.org
# paginates at a fixed 25 rows; the first page is what says how many there are
# in all, and the rest are fetched together once that is known.


def _fake_listing(monkeypatch, per_page, total, pages=None, claims=None):
    """Serve a category listing out of memory, recording the pages asked for.

    'total' is how many parts the listing actually has; 'claims' is the number
    its "of N" summary states, which is not always the same thing - the count
    is scraped off the page and the page size is not promised anywhere.
    """
    asked = []
    stated = total if claims is None else claims

    def fake_cached(url, rel):
        page = int(re.search(r"page-(\d+)\.html", rel).group(1))
        asked.append(page)
        if pages is not None and page not in pages:
            return None
        ids = range((page - 1) * per_page, min(page * per_page, total))
        if not ids:
            return "of %d" % stated
        rows = "".join('<a href="/library/official/parts/p%04d.dat">' % i for i in ids)
        return ("of %d" % stated) + rows

    monkeypatch.setattr(plugin, "_index", lambda: False)
    monkeypatch.setattr(plugin, "_cached", fake_cached)
    return asked


def test_the_listing_is_read_in_page_order_however_it_arrives(monkeypatch):
    asked = _fake_listing(monkeypatch, plugin._PER_PAGE, 60)
    ids = plugin._part_ids("Brick")
    assert ids == ["p%04d" % i for i in range(60)]
    # Page 1 alone, because it is what carries the count; then the rest.
    assert asked[0] == 1
    assert sorted(asked) == [1, 2, 3]


def test_a_listing_that_states_no_total_is_walked_one_page_at_a_time(monkeypatch):
    def fake_cached(url, rel):
        page = int(re.search(r"page-(\d+)\.html", rel).group(1))
        if page > 2:
            return "<html>nothing</html>"  # past the end: no new ids
        ids = range((page - 1) * 25, page * 25)
        return "".join('<a href="/library/official/parts/p%04d.dat">' % i for i in ids)

    monkeypatch.setattr(plugin, "_index", lambda: False)
    monkeypatch.setattr(plugin, "_cached", fake_cached)
    assert plugin._part_ids("Brick") == ["p%04d" % i for i in range(50)]


def test_a_page_that_did_not_arrive_ends_the_listing_there(monkeypatch):
    """Better a short category than one with a hole in the middle of it."""
    _fake_listing(monkeypatch, plugin._PER_PAGE, 100, pages={1, 2, 4})
    assert plugin._part_ids("Brick") == ["p%04d" % i for i in range(2 * plugin._PER_PAGE)]


def test_a_listing_with_no_first_page_is_empty(monkeypatch):
    monkeypatch.setattr(plugin, "_index", lambda: False)
    monkeypatch.setattr(plugin, "_cached", lambda url, rel: None)
    assert plugin._part_ids("Brick") == []


# --- the builder and the reader are one format -------------------------------


def _builder():
    spec = importlib.util.spec_from_file_location(
        "build_parts_index", os.path.join(_here, "build_parts_index.py")
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_what_the_builder_writes_is_what_the_plugin_reads(tmp_path):
    builder = _builder()
    index = {
        "format": builder.FORMAT,
        "source": "test",
        "generated": "2026-01-01",
        "strings": ["James Jessiman", "CC BY 4.0"],
        "categories": {
            "Brick": {"3001": ["Brick  2 x  4", 0, 1, {"iface": {"a": 1}}]},
            "Minifig Accessory": {"3833": ["Minifig Helmet", 0, -1, None]},
        },
    }
    path = tmp_path / plugin._INDEX_FILE
    builder.write(index, str(path), plugin)

    read = plugin._Index(str(path))
    assert sorted(read.categories) == ["Brick", "Minifig Accessory"]
    assert read.part_ids("Brick") == ["3001"]
    assert read.header(read.entry("3001", "Brick")) == ("Brick  2 x  4", "James Jessiman", "CC BY 4.0")
    # A licence the entry does not carry stays None rather than becoming a string.
    assert read.header(read.entry("3833", "Minifig Accessory")) == ("Minifig Helmet", "James Jessiman", None)
    # ...and a part found without its category is found in the right one.
    assert read.category_of("3833") == "Minifig Accessory"
    assert read.entry("3001")[3] == {"iface": {"a": 1}}
    assert read.entry("nosuch") is None


def test_the_builder_refuses_categories_that_share_a_member(tmp_path):
    """The member name is the category's, so the categories have to stay distinct."""
    builder = _builder()
    index = {
        "format": builder.FORMAT,
        "source": "test",
        "generated": "2026-01-01",
        "strings": [],
        "categories": {"Sheet Fabric": {}, "Sheet  Fabric": {}},
    }
    with pytest.raises(SystemExit):
        builder.write(index, str(tmp_path / plugin._INDEX_FILE), plugin)


def test_a_page_size_smaller_than_assumed_does_not_truncate_a_category(monkeypatch):
    """The planned page count is a hint, not the stopping condition.

    The total and _PER_PAGE are both guesses about the site. If the real page
    size were smaller than _PER_PAGE, planning alone would fetch too few pages
    and silently cut the category short - and that would be baked into the
    shipped index, looking like parts the library does not have.
    """
    asked = _fake_listing(monkeypatch, per_page=10, total=100)
    assert plugin._part_ids("Brick") == ["p%04d" % i for i in range(100)]
    assert max(asked) >= 10  # it kept going past the 4 pages the count planned


def test_a_total_far_larger_than_the_listing_costs_one_empty_page(monkeypatch):
    """Guessing high stops on the page that adds nothing, and stays bounded."""
    asked = _fake_listing(monkeypatch, per_page=plugin._PER_PAGE, total=50, claims=10**6)
    assert plugin._part_ids("Brick") == ["p%04d" % i for i in range(50)]
    # Bounded: a claimed million parts must not put 2000 requests in flight.
    assert max(asked) <= plugin._LIST_BATCH_PAGES + 1


def test_a_total_smaller_than_the_listing_stops_where_it_always_did(monkeypatch):
    """A count scraped from unrelated page text: same answer as before."""
    _fake_listing(monkeypatch, per_page=plugin._PER_PAGE, total=100, claims=plugin._PER_PAGE)
    assert len(plugin._part_ids("Brick")) == plugin._PER_PAGE


# --- one deadline for the part, not one per walk -----------------------------


def test_every_walk_of_one_part_shares_one_deadline(monkeypatch, fake_library):
    """_lego_implements walks a part three times over (four for headgear).

    A deadline made inside each walk would bound one part at four times
    _GEOMETRY_SECONDS, which is past the PartCAD deadline it exists to stay
    inside.
    """
    seen = []
    original = plugin._walk_geometry
    monkeypatch.setattr(
        plugin,
        "_walk_geometry",
        lambda pid, visit, deadline=None: seen.append(deadline) or original(pid, visit, deadline),
    )
    plugin._lego_implements("Brick  2 x  2", "3003")
    assert len(seen) > 1, "expected more than one walk for a part"
    assert all(d is not None for d in seen)
    assert len(set(seen)) == 1, "each walk started a budget of its own"


def test_a_walk_reached_directly_still_gets_a_budget(fake_library):
    """Nothing hands a deadline to a helper called on its own; it makes one."""
    assert plugin._geometry_stud_implements("3003") is not None
