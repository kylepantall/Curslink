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
