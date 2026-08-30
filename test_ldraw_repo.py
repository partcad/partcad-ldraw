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
        "Technic Pin  3/4",
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
