import pytest

from curslink.layout import Device, Layout, Screen


def test_overlapping_screens_rejected():
    with pytest.raises(ValueError, match="overlap"):
        Layout.from_devices(
            [
                Device("a", "A", (Screen("s", "a", 0, 0, 100, 100),)),
                Device("b", "B", (Screen("s", "b", 50, 50, 100, 100),)),
            ]
        )


def test_os_mapping(layout):
    right = layout.screen("desktop", "right")
    assert right.to_os(1920, 10) == (1920, 10)
    laptop = layout.screen("laptop", "main")
    assert laptop.to_os(3840 + 5, 200 + 7) == (5, 7)
    assert layout.screen_at_os("laptop", 5, 7) == laptop
    assert laptop.from_os(5, 7) == (3845, 207)


def test_moving_between_own_monitors_is_not_a_crossing(layout):
    assert layout.find_crossing("desktop", 1919, 500, 5, 0) is None


def test_right_edge_crosses_to_laptop(layout):
    hit = layout.find_crossing("desktop", 3839, 500, 1, 0)
    assert hit is not None
    assert hit.screen.device_id == "laptop"
    # pushed a couple of pixels inside so it doesn't immediately bounce back
    assert 3840 < hit.x < 3850 and hit.y == 500


def test_no_crossing_where_screens_dont_overlap_vertically(layout):
    # laptop spans y 200..1000; at y=100 there is nothing to the right
    assert layout.find_crossing("desktop", 3839, 100, 1, 0) is None


def test_gap_tolerance_bridges_small_gaps():
    lay = Layout.from_devices(
        [
            Device("a", "A", (Screen("s", "a", 0, 0, 100, 100),)),
            Device("b", "B", (Screen("s", "b", 130, 0, 100, 100),)),
        ]
    )
    assert lay.find_crossing("a", 99, 50, 1, 0, gap_tolerance=0) is None
    hit = lay.find_crossing("a", 99, 50, 1, 0, gap_tolerance=40)
    assert hit and hit.screen.device_id == "b"


def test_edge_pushes(layout):
    # right edge of the right monitor is an outer edge
    assert (1, 0) in layout.edge_pushes("desktop", 3839, 500)
    # the seam between the two desktop monitors is not
    assert layout.edge_pushes("desktop", 1919, 500) == []
    # top-left corner of the left monitor pushes both up and left
    assert set(layout.edge_pushes("desktop", 0, 0)) == {(-1, 0), (0, -1)}


def test_clamp_to_device_slides_along_edge(layout):
    screen, x, y = layout.clamp_to_device("laptop", 6000, 500)
    assert screen.device_id == "laptop" and x == 3840 + 1280 - 1 and y == 500


def _overlapping(allow=True, laptop_z=1):
    # A 1000x600 monitor, with the laptop standing in front of its bottom-right corner.
    return Layout.from_devices(
        [
            Device("pc", "PC", (Screen("main", "pc", 0, 0, 1000, 600),)),
            Device("lap", "Laptop", (Screen("main", "lap", 800, 400, 500, 300, z=laptop_z),)),
        ],
        allow_overlap=allow,
    )


def test_overlap_allowed_when_flag_on():
    lay = _overlapping()
    (behind, front), = lay.overlaps()
    assert (behind.device_id, front.device_id) == ("pc", "lap")
    assert behind.overlap(front) == (800, 400, 200, 200)


def test_overlap_still_refused_without_flag():
    with pytest.raises(ValueError, match="pc/main and lap/main overlap"):
        _overlapping(allow=False)


def test_screen_hidden_completely_is_refused():
    with pytest.raises(ValueError, match="completely hidden"):
        Layout.from_devices(
            [
                Device("a", "A", (Screen("s", "a", 10, 10, 50, 50),)),
                Device("b", "B", (Screen("s", "b", 0, 0, 100, 100, z=1),)),
            ],
            allow_overlap=True,
        )


def test_front_screen_owns_the_shared_area():
    lay = _overlapping()
    assert lay.screen_at(900, 500).device_id == "lap"
    assert lay.screen_at(900, 500, "pc") is None  # blocked out
    assert lay.screen_at(100, 100, "pc").device_id == "pc"
    assert lay.covered_by("pc", 900, 500).device_id == "lap"
    assert lay.covered_by("pc", 100, 100) is None
    # with the monitor in front instead, the laptop's corner is the one hidden
    lay = _overlapping(laptop_z=-1)
    assert lay.screen_at(900, 500).device_id == "pc"
    assert lay.covered_by("lap", 900, 500).device_id == "pc"


def test_pushing_into_the_blocked_corner_crosses():
    lay = _overlapping()
    # just above the laptop, moving down onto it
    hit = lay.find_crossing("pc", 900, 399, 0, 1)
    assert hit and hit.screen.device_id == "lap"
    # from the laptop, up past its top edge, onto the visible monitor
    hit = lay.find_crossing("lap", 900, 400, 0, -1)
    assert hit and hit.screen.device_id == "pc" and hit.y < 400

