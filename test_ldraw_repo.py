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
import math
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
    # a tile: an underside of two open tubes and no studs of its own to say
    # where the lattice is, which is what most of the library's undersides are
    "99999e.dat": (
        "0 Tile Test  2 x  3\n"
        "1 16 -10 4 0 1 0 0 0 -1 0 0 0 1 stud4.dat\n"
        "1 16 10 4 0 1 0 0 0 -1 0 0 0 1 stud4.dat\n"
    ),
    # a studless underside of solid tubes, which no stud and no open tube says
    # the axis of: the shape of a 1-wide tile, and 436 parts in the library
    "99999f.dat": (
        "0 Tile Test  1 x  3\n"
        "1 16 -10 4 0 1 0 0 0 -1 0 0 0 1 stud3.dat\n"
        "1 16 10 4 0 1 0 0 0 -1 0 0 0 1 stud3.dat\n"
    ),
    # a one-sided Technic hole, whose mouth LDraw draws as a nested peghole
    "99999h.dat": ("0 Technic One Sided Test\n1 16 0 0 0 1 0 0 0 1 0 0 0 1 connhol3.dat\n"),
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
    # and what tells them apart is where its far end lands: on the part's own
    # origin, so the tube stands above the plane the part mates on
    hat = plugin._lego_implements("Minifig Hat Wide Brim Flat", "30167")
    assert sorted(hat[ANTI]) == ["anti"]
    assert hat[ANTI]["anti"][0] == [0.0, 0.0, 0.0]
    # the same primitive 40 LDU down is an underside, not a socket: its far end
    # is nowhere near the origin, so it separates four anti-studs as usual
    other = plugin._lego_implements("Minifig Hat Nonsense", "99999")
    assert len(other[ANTI]) == 4
    assert {p[0][1] for p in other[ANTI].values()} == {-17.6}


def test_a_part_with_no_studs_of_its_own_still_gets_its_underside(fake_library):
    # a tile has an underside but nothing on top, which used to mean its
    # anti-studs came from its name - and a name does not say which way round
    # the part is drawn
    anti = plugin._geometry_anti_studs("99999e")
    assert len(anti) == 6
    assert sorted(p[0] for p in anti.values()) == [
        [-8.0, -3.2, -4.0],
        [-8.0, -3.2, 4.0],
        [0.0, -3.2, -4.0],
        [0.0, -3.2, 4.0],
        [8.0, -3.2, -4.0],
        [8.0, -3.2, 4.0],
    ]


def test_a_studless_underside_of_solid_tubes_is_left_to_the_name(fake_library):
    # a solid tube does not say which two anti-studs it separates, and on a part
    # with no studs there is nothing to ask, so the name's grid stands
    assert plugin._geometry_anti_studs("99999f") is None
    assert sorted(plugin._lego_implements("Tile  1 x  3", "99999f")[ANTI]) == ["c0r0", "c1r0", "c2r0"]


def test_the_one_sided_hole_takes_the_mouth_ldraw_draws_inside_it(fake_library):
    # connhol3 is blind at one end, and says which by drawing a peghole at the
    # other. The walk never fetches a primitive, so that is transcribed.
    holes = plugin._lego_implements("Technic One Sided Test", "99999h")[PIN_HOLE]
    assert len(holes) == 1
    assert list(holes.values())[0][0] == [0.0, 4.0, 0.0]
    assert "connhol3.dat" not in fake_library


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
