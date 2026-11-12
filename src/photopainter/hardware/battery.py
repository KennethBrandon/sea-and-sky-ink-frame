"""The battery: a reading from the board's INA219, and a fake for the Mac and tests.

The PhotoPainter board's INA219 (I2C bus 1, address 0x43) sits on the battery line
between the charger and the cell, across a 0.01 Ω shunt (the board schematic; see
docs/hardware.md "Power"). So its bus voltage is the battery's, and its current is the
battery's: positive while the cell charges, negative while the frame runs on it, about
zero on USB power with a full cell. With no cell fitted the line floats: mostly near 0 V,
with the charger's detection pulses reaching about 3.8 V for 50 ms at a time, so a
reading is two voltages 100 ms apart, and a pair that disagrees means there's no cell.

`Battery.read()` talks to the chip through `/dev/i2c-1` with the standard library alone
(an `I2C_SLAVE` ioctl, then a pointer write and a two-byte read per register), so it
needs no smbus package. It raises `BatteryUnavailable` on a Mac or when the chip doesn't
answer, and the tick then records no reading. `FakeBattery` reads `fake_battery.json` in
the state dir instead (`{"volts": 3.7, "milliamps": -180}`), for `--fake-panel` runs and
tests; without the file it's unavailable, like a Mac.

The percentage is an estimate from the voltage along the discharge curve we measured:
the chip is no fuel gauge, and the voltage sags under load and rises while charging.
The current, and so the state, is what to trust.
"""

from __future__ import annotations

import fcntl
import json
import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

I2C_DEVICE = "/dev/i2c-1"
ADDRESS = 0x43  # Waveshare's board; the chip's own default is 0x40
I2C_SLAVE = 0x0703  # ioctl: talk to this address

REG_CONFIG = 0x00
REG_BUS_VOLTAGE = 0x02
REG_CURRENT = 0x04
REG_CALIBRATION = 0x05

SHUNT_OHMS = 0.01
# Waveshare's calibration (INA219.py in their demo): 16 V bus range, ±80 mV across the
# shunt (gain /2, so up to 8 A), 12-bit conversions averaged over 32 samples (17 ms each),
# both channels converted continuously.
CALIBRATION = 26868
CURRENT_LSB_MA = 0.04096 / (CALIBRATION * SHUNT_OHMS) * 1000  # 0.1524 mA per bit
BUS_VOLTAGE_LSB_V = 0.004
CONFIG = (0 << 13) | (1 << 11) | (0x0D << 7) | (0x0D << 3) | 0x07
CONVERSION_S = 0.05  # a fresh pair of conversions after the config write (2 × 17 ms)

NO_CELL_VOLTS = 2.5  # below this nothing usable is connected
FLOW_MA = 10.0  # less than this either way counts as no current
# With no cell the line floats, and the charger's detection pulses push it up to about
# 3.8 V for 50 ms at a time (measured 2026-11-04). A cell holds its voltage, so two
# readings this far apart that disagree by more than STEADY_VOLTS mean there isn't one.
STEADY_GAP_S = 0.1
STEADY_VOLTS = 0.1
# Charge left against the voltage while the frame runs on the cell (about 0.2 A), from the
# first full run-down of the 2000 mAh pack (2026-11-07, docs/hardware.md): full read 4.06 V,
# and the power cut out at 3.00 V. A full cell resting on USB reads about 4.13 V.
CURVE = ((3.0, 0), (3.1, 1), (3.2, 2), (3.3, 4), (3.4, 8), (3.5, 15), (3.6, 25), (3.7, 32), (3.8, 48), (3.9, 66),
         (4.0, 88), (4.05, 99), (4.1, 100))
STATES = ("none", "charging", "discharging", "idle")
FAKE_FILE = "fake_battery.json"


class BatteryUnavailable(Exception):
    """No reading: no I2C device here, or the chip didn't answer."""


@dataclass(frozen=True)
class Reading:
    volts: float
    milliamps: float  # positive = charging, negative = the frame runs on the battery
    steady: bool = True  # the voltage held still between two readings (see STEADY_GAP_S)

    @property
    def state(self) -> str:
        """"none" (no cell), "charging", "discharging" (on battery) or "idle" (on USB,
        the cell full or the charger resting)."""
        if self.milliamps > FLOW_MA:
            return "charging"
        if self.milliamps < -FLOW_MA:
            return "discharging"
        if self.volts < NO_CELL_VOLTS or not self.steady:
            return "none"
        return "idle"

    @property
    def on_battery(self) -> bool:
        return self.state == "discharging"

    @property
    def percent(self) -> int | None:
        """Charge left, estimated from the voltage (None with no cell)."""
        if self.state == "none":
            return None
        v = self.volts
        if v <= CURVE[0][0]:
            return 0
        for (v0, p0), (v1, p1) in zip(CURVE, CURVE[1:]):
            if v <= v1:
                return round(p0 + (p1 - p0) * (v - v0) / (v1 - v0))
        return 100

    def to_dict(self) -> dict:
        return {"volts": self.volts, "milliamps": self.milliamps, "state": self.state, "percent": self.percent}

    def __str__(self) -> str:
        text = f"{self.volts:.2f} V  {self.milliamps:+.0f} mA  {self.state}"
        return text if self.percent is None else f"{text}  about {self.percent} %"


class BatteryReader(Protocol):
    def read(self) -> Reading: ...


class I2CDevice(Protocol):
    """What `Battery` needs from the bus: a register-pointer write and a short read."""

    def write(self, data: bytes) -> None: ...
    def read(self, n: int) -> bytes: ...
    def close(self) -> None: ...


class _LinuxI2C:
    def __init__(self, device: str, address: int):
        self.fd = os.open(device, os.O_RDWR)
        try:
            fcntl.ioctl(self.fd, I2C_SLAVE, address)
        except OSError:
            os.close(self.fd)
            raise

    def write(self, data: bytes) -> None:
        os.write(self.fd, data)

    def read(self, n: int) -> bytes:
        return os.read(self.fd, n)

    def close(self) -> None:
        os.close(self.fd)


class Battery:
    """The INA219 on the board. `read()` opens the bus, reads, and closes it again."""

    def __init__(self, device: str = I2C_DEVICE, address: int = ADDRESS,
                 opener: Callable[[str, int], I2CDevice] | None = None,
                 sleep: Callable[[float], None] = time.sleep):
        self.device, self.address = device, address
        self._open = opener or _LinuxI2C
        self._sleep = sleep

    def read(self) -> Reading:
        try:
            dev = self._open(self.device, self.address)
        except OSError as exc:
            raise BatteryUnavailable(f"can't open {self.device} at 0x{self.address:02x}: {exc}") from exc
        try:
            _write16(dev, REG_CALIBRATION, CALIBRATION)  # lost if the chip was power-cycled
            _write16(dev, REG_CONFIG, CONFIG)
            self._sleep(CONVERSION_S)
            first = _read16(dev, REG_BUS_VOLTAGE)
            self._sleep(STEADY_GAP_S)
            current = _read16(dev, REG_CURRENT)
            bus = _read16(dev, REG_BUS_VOLTAGE)
        except OSError as exc:
            raise BatteryUnavailable(f"INA219 at 0x{self.address:02x} on {self.device}: {exc}") from exc
        finally:
            dev.close()
        if current >= 0x8000:
            current -= 0x10000
        volts, earlier = (bus >> 3) * BUS_VOLTAGE_LSB_V, (first >> 3) * BUS_VOLTAGE_LSB_V
        return Reading(round(volts, 3), round(current * CURRENT_LSB_MA, 1), abs(volts - earlier) <= STEADY_VOLTS)


def _write16(dev: I2CDevice, register: int, value: int) -> None:
    dev.write(bytes([register, (value >> 8) & 0xFF, value & 0xFF]))


def _read16(dev: I2CDevice, register: int) -> int:
    dev.write(bytes([register]))
    data = dev.read(2)
    if len(data) != 2:
        raise OSError(f"short read from register 0x{register:02x}")
    return (data[0] << 8) | data[1]


class FakeBattery:
    """Readings from `fake_battery.json` in the state dir; unavailable without it."""

    def __init__(self, state_dir: Path):
        self.path = Path(state_dir) / FAKE_FILE

    def read(self) -> Reading:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return Reading(float(data["volts"]), float(data["milliamps"]))
        except FileNotFoundError as exc:
            raise BatteryUnavailable(f"no {self.path.name} (fake battery)") from exc
        except (OSError, ValueError, TypeError, KeyError) as exc:
            raise BatteryUnavailable(f"bad {self.path.name}: {exc}") from exc

    def set(self, volts: float, milliamps: float) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps({"volts": volts, "milliamps": milliamps}), encoding="utf-8")
