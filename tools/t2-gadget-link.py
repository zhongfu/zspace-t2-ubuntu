#!/usr/bin/env python3
"""Bring up the workstation's side of the T2 USB gadget link.

The board's `t2-usbgadget` presents **one** network function on the one USB-C
port: CDC-NCM (`cdc_ncm` on Linux).  NCM is served in-box on every current host
OS (Linux/BSD, macOS, Windows 10 1709+), which is why it replaced the old
ECM+RNDIS pair: that pair gave a Linux host *two* `enx…` interfaces, and the
RNDIS one took a DHCP lease from the board and then answered no ICMP/TCP at all
(measured 2026-10-02).  This script **prefers the `cdc_ncm` interface**, still
accepts `cdc_ether` from a legacy board, and refuses a `rndis_host`-only
interface (Linux cannot use it); it verifies the board actually answers before
declaring success.

The other thing that goes wrong without this: the gadget's host-side MAC is
random per boot (configfs default), so the interface is *renamed* every time
the board reboots (`enx3e8baa3d0a0d` -> `enxea479d34371e` -> …).  A
NetworkManager profile is keyed to that MAC, so the new interface comes up
**unmanaged-but-disconnected** (state 30) with no address: `ping 10.55.55.2`
fails and `ssh root@10.55.55.2` times out, while the cable, the gadget and the
board are all fine.  This script activates a profile for the interface that is
actually present - the documented, root-free way (NetworkManager's polkit
default allows the active user to activate a connection).  The profile reuses
one fixed UUID, so repeated runs update it instead of littering NM state.

    python3 tools/t2-gadget-link.py            # activate if needed, print address
    python3 tools/t2-gadget-link.py --status    # report only, change nothing

Manual fallback (no dbus-python), same effect - use the interface's own name:

    nmcli con add type ethernet ifname enx… con-name t2-cdc-ncm \\
        ipv4.method manual ipv4.addresses 10.55.55.10/24 ipv6.method disabled
    nmcli con up t2-cdc-ncm

This is harness tooling for the workstation, not part of any board image.
"""

import argparse
import subprocess
import sys
import time
import uuid

try:
    import dbus
except ImportError:  # --help still works without dbus-python
    dbus = None

NM = "org.freedesktop.NetworkManager"
NM_PATH = "/org/freedesktop/NetworkManager"
DEVICE = NM + ".Device"
PROPS = "org.freedesktop.DBus.Properties"
BOARD = "10.55.55.2"
# NCM is the function the board now exposes; cdc_ether is accepted from a
# legacy board, rndis_host never (Windows-only and inert on Linux).
PREFERRED_DRIVER = "cdc_ncm"
LEGACY_DRIVERS = ("cdc_ether",)
SUPPORTED_DRIVERS = (PREFERRED_DRIVER,) + LEGACY_DRIVERS
WINDOWS_ONLY_DRIVERS = ("rndis_host",)
# One fixed UUID: repeated runs update this profile instead of adding new ones.
PROFILE_UUID = str(uuid.uuid5(uuid.NAMESPACE_URL, "zspace-t2-gadget-link"))


def _bus():
    return dbus.SystemBus()


def gadget_devices(bus):
    """One dict per `enx*` ethernet interface.

    Keys: path, name, state (NM Device.State), managed, driver and addresses
    (empty list when the interface has no IPv4 address yet).
    """
    nm = bus.get_object(NM, NM_PATH)
    props = dbus.Interface(nm, PROPS)
    out = []
    for path in props.Get(NM, "Devices"):
        dev = dbus.Interface(bus.get_object(NM, path), PROPS)
        name = str(dev.Get(DEVICE, "Interface"))
        if not name.startswith("enx"):
            continue
        try:
            driver = str(dev.Get(DEVICE, "Driver"))
        except dbus.DBusException:
            driver = ""            # NetworkManager < 1.14 has no Driver property
        out.append({
            "path": path,
            "name": name,
            "state": int(dev.Get(DEVICE, "State")),
            "managed": bool(dev.Get(DEVICE, "Managed")),
            "driver": driver,
            "addresses": address(bus, path),
        })
    return out


def choose(devs):
    """Which interface to bring up: `(device, reason)`.

    The gadget exposes one CDC-NCM function (`cdc_ncm`); a legacy board still
    exposes ECM (`cdc_ether`), which is equally usable, so both are accepted
    with NCM preferred.  A `rndis_host`-only interface is refused: on Linux it
    is inert - it takes a DHCP lease and then passes no traffic (measured
    2026-10-02).
    """
    if not devs:
        return None, "no interface"
    pool = [d for d in devs if d["driver"] in SUPPORTED_DRIVERS]
    if not pool:
        return None, "no-ncm"
    # NCM first, legacy ECM after; the sort is stable, so discovery order is
    # kept within one driver.
    pool.sort(key=lambda d: SUPPORTED_DRIVERS.index(d["driver"]))
    idle = [d for d in pool if not d["addresses"]]
    if not idle:
        return None, "already up"
    return idle[0], "activate"


def reachable(iface, timeout=3):
    """True when the board answers ICMP on `iface` (link must be up first)."""
    try:
        return subprocess.run(
            ["ping", "-c", "1", "-W", str(timeout), "-I", iface, BOARD],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=timeout + 2).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def activate(bus, path):
    """Add and activate a DHCP ethernet profile on `path`.

    The profile UUID is fixed, so repeated runs update the one profile instead
    of minting a new "t2-gadget" per boot (the gadget's host-side MAC, and thus
    the interface name, changes on every board boot).
    """
    nm = dbus.Interface(bus.get_object(NM, NM_PATH), NM)
    settings = dbus.Dictionary({
        "connection": dbus.Dictionary(
            {"id": "t2-gadget", "uuid": PROFILE_UUID,
             "type": "802-3-ethernet", "autoconnect": True}, signature="sv"),
        "ipv4": dbus.Dictionary({"method": "auto"}, signature="sv"),
        "ipv6": dbus.Dictionary({"method": "ignore"}, signature="sv"),
    }, signature="sa{sv}")
    return nm.AddAndActivateConnection(settings, path, dbus.ObjectPath("/"))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--status", action="store_true",
                    help="only report; never activate a connection")
    ap.add_argument("--timeout", type=float, default=20.0,
                    help="seconds to wait for a DHCP address (default 20)")
    args = ap.parse_args(argv)

    if dbus is None:
        print("dbus-python is not installed: install python3-dbus to use this "
              "tool (it talks to NetworkManager over D-Bus)")
        return 1

    bus = _bus()
    devs = gadget_devices(bus)
    if not devs:
        print("no enx* interface: is the board's USB-C gadget cable plugged in "
              "and t2-usbgadget.service running?")
        return 1

    for d in devs:
        note = ("up" if d["addresses"] else
                "disconnected" if d["state"] == 30 else f"state {d['state']}")
        role = ("NCM, supported on this host"
                if d["driver"] == PREFERRED_DRIVER else
                "legacy ECM, supported on this host"
                if d["driver"] in LEGACY_DRIVERS else
                "RNDIS, Windows only - inert on Linux"
                if d["driver"] in WINDOWS_ONLY_DRIVERS else "unknown driver")
        print(f"{d['name']}: {d['addresses'] or '-'} ({note}"
              f"{'' if d['managed'] else ', unmanaged'}) [{d['driver'] or '?'}: "
              f"{role}]")

    if args.status:
        return 0

    dev, reason = choose(devs)
    if dev is None:
        if reason == "already up":
            print("already up")
            return 0
        if reason == "no-ncm":
            print("no NCM interface is present.  If the board shows an RNDIS "
                  "interface, this Linux host cannot use it (the RNDIS link "
                  "takes a lease and then passes no traffic).  Check on the "
                  "board that t2-usbgadget.service is active and that the NCM "
                  "function is bound - the board's `ip -brief addr` should list "
                  "usb0 with 10.55.55.2/24.")
            return 1
        print(f"nothing to do ({reason})")
        return 1

    name, path = dev["name"], dev["path"]
    print(f"activating DHCP profile on {name} ({dev['driver'] or '?'})")
    try:
        activate(bus, path)
    except dbus.DBusException as exc:
        print(f"  failed: {exc.get_dbus_name()}: {exc.get_dbus_message()}")
        return 1
    deadline = time.monotonic() + args.timeout
    while time.monotonic() < deadline:
        addrs = address(bus, path)
        if addrs:
            break
        time.sleep(0.5)
    else:
        print(f"  {name}: no address after {args.timeout:g}s "
              "(is the board's dnsmasq running?)")
        return 1
    print(f"  {name}: {addrs}")
    if reachable(name):
        print(f"board answering at {BOARD} (ssh root@{BOARD})")
        return 0
    print(f"  no ICMP reply from {BOARD} on {name}: the link is up but the "
          "board does not answer - check that it booted past the gadget start, "
          "and that the cable is on its USB-C data port")
    return 1


if __name__ == "__main__":
    sys.exit(main())
