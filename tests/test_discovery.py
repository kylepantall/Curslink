import asyncio

import pytest

from curslink.pairing.discovery import SERVICE_TYPE, Advertiser, browse, found_from_info
from curslink.pairing.handshake import DeviceInfo


def info(props, port=24851, addresses=("192.168.1.20",)):
    from zeroconf import ServiceInfo

    return ServiceInfo(SERVICE_TYPE, f"x.{SERVICE_TYPE}", port=port, properties=props,
                       server="x.local.", parsed_addresses=list(addresses))


def test_found_from_txt_record():
    f = found_from_info(info({"id": "desk", "name": "Desk", "role": "host", "cport": "24850"}))
    assert (f.device_id, f.name, f.role, f.address, f.pairing_port, f.control_port) == (
        "desk", "Desk", "host", "192.168.1.20", 24851, 24850)


def test_unusable_adverts_are_ignored():
    assert found_from_info(info({"id": "desk", "role": "phone"})) is None
    assert found_from_info(info({"role": "host"})) is None
    assert found_from_info(info({"id": "d", "role": "agent"}, addresses=())) is None


async def test_advertise_and_browse():
    adverts = [
        Advertiser(DeviceInfo("pytest-desk", "Desk", "host"), 24990, 24991),
        Advertiser(DeviceInfo("pytest-laptop", "Laptop", "agent"), 24992),
    ]
    try:
        for a in adverts:
            await a.start()
    except OSError as exc:
        pytest.skip(f"no multicast here: {exc}")
    try:
        found = {f.device_id: f for f in await browse(2.0, exclude="pytest-laptop")}
    finally:
        for a in adverts:
            await a.close()
    if "pytest-desk" not in found:
        pytest.skip("mDNS answers did not arrive (multicast blocked on this machine)")
    assert "pytest-laptop" not in found
    assert found["pytest-desk"].control_port == 24991 and found["pytest-desk"].pairing_port == 24990
