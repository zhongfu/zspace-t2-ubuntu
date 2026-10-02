#!/usr/bin/env python3
"""Drive one GPIO line from the T2 without libgpiod or the sysfs interface.

The bench kernel has CONFIG_GPIO_CDEV=y but no CONFIG_GPIO_SYSFS and no
libgpiod in the rootfs, so `/sys/class/gpio` does not exist and `gpioset` is not
available - yet `/dev/gpiochipN` does.  This speaks the GPIO character-device
v2 ioctl ABI (the only one, CONFIG_GPIO_CDEV_V1 is off) with ctypes.

    t2-gpio.py CHIP OFFSET VALUE [--hold SECS] [--name NAME]

VALUE is the *physical* line level (0/1): no GPIO_ACTIVE_LOW flag is applied,
which is the point - it makes a pin whose polarity is unknown (e.g. the USB data
mux) directly testable in both states.

The line stays claimed for --hold seconds (default 3600) because releasing it
lets the pinctrl core put the pin back to its default function, which would
undo the experiment.  Kill the process to release it.

Written for the USB data mux (`gpio4 PD2` = chip 4, offset 26; the vendor's
`switch-gpios = <&gpio4 26 GPIO_ACTIVE_LOW>`, see notes/bsp-port.md).
"""
import argparse
import ctypes
import ctypes.util
import fcntl
import os
import struct
import sys
import time

GPIO_V2_LINES_MAX = 64
GPIO_MAX_NAME_SIZE = 32
GPIO_V2_LINE_NUM_ATTRS_MAX = 10

# uapi: _BITULL(2) / _BITULL(3) - NOT the v1 handle flags (1/2), which is what
# makes a request silently come back as an input line and set-values fail EPERM.
GPIO_V2_LINE_FLAG_INPUT = 1 << 2
GPIO_V2_LINE_FLAG_OUTPUT = 1 << 3

_IOC_WRITE = 1   # asm-generic/ioctl.h: _IOC_WRITE is 1, _IOC_READ is 2, so
_IOC_READ = 2    # _IOWR encodes dir = 3 (not 6 - that sets a second dir bit
                 # and yields an unknown command, i.e. EINVAL from the kernel).


class LineAttribute(ctypes.Structure):
    _fields_ = [("id", ctypes.c_uint32),
                ("padding", ctypes.c_uint32),
                ("value", ctypes.c_uint64)]


class LineConfigAttribute(ctypes.Structure):
    _fields_ = [("attr", LineAttribute),
                ("mask", ctypes.c_uint64)]


class LineConfig(ctypes.Structure):
    _fields_ = [("flags", ctypes.c_uint64),
                ("num_attrs", ctypes.c_uint32),
                ("padding", ctypes.c_uint32 * 5),
                ("attrs", LineConfigAttribute * GPIO_V2_LINE_NUM_ATTRS_MAX)]


class LineRequest(ctypes.Structure):
    _fields_ = [("offsets", ctypes.c_uint32 * GPIO_V2_LINES_MAX),
                ("consumer", ctypes.c_char * GPIO_MAX_NAME_SIZE),
                ("config", LineConfig),
                ("num_lines", ctypes.c_uint32),
                ("event_buffer_size", ctypes.c_uint32),
                ("padding", ctypes.c_uint32 * 5),
                ("fd", ctypes.c_int32)]


class LineValues(ctypes.Structure):
    _fields_ = [("bits", ctypes.c_uint64), ("mask", ctypes.c_uint64)]


def ioc(dir_, type_, nr, size):
    return (dir_ << 30) | (size << 16) | (type_ << 8) | nr


GET_LINE_IOCTL = ioc(_IOC_READ | _IOC_WRITE, 0xB4, 0x07,
                     ctypes.sizeof(LineRequest))
SET_VALUES_IOCTL = ioc(_IOC_READ | _IOC_WRITE, 0xB4, 0x0F,
                       ctypes.sizeof(LineValues))


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("chip", type=int, help="gpiochip number, e.g. 4")
    ap.add_argument("offset", type=int, help="line offset within the chip")
    ap.add_argument("value", type=int, choices=(0, 1),
                    help="physical line level to drive")
    ap.add_argument("--hold", type=float, default=3600.0,
                    help="seconds to keep the line claimed (default 3600)")
    ap.add_argument("--name", default="t2-gpio", help="consumer name")
    args = ap.parse_args()

    path = "/dev/gpiochip%d" % args.chip
    fd = os.open(path, os.O_RDWR | os.O_CLOEXEC)

    req = LineRequest()
    req.offsets[0] = args.offset
    req.consumer = args.name.encode()[:GPIO_MAX_NAME_SIZE - 1]
    req.config.flags = GPIO_V2_LINE_FLAG_OUTPUT
    req.num_lines = 1
    req.fd = -1

    fcntl.ioctl(fd, GET_LINE_IOCTL, req, True)
    if req.fd < 0:
        print("%s offset %d: request failed" % (path, args.offset),
              file=sys.stderr)
        return 1

    values = LineValues(bits=args.value, mask=1)
    fcntl.ioctl(req.fd, SET_VALUES_IOCTL, values, True)

    print("%s offset %d = %d (held for %.0fs; kill to release)"
          % (path, args.offset, args.value, args.hold), flush=True)
    try:
        time.sleep(args.hold)
    except KeyboardInterrupt:
        pass
    os.close(req.fd)
    os.close(fd)
    return 0


if __name__ == "__main__":
    sys.exit(main())
