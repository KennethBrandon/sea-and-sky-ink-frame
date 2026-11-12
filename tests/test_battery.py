"""hardware/battery.py: readings, the INA219 register protocol (against a fake bus), the
fake battery file, and `photopainter battery`."""

import json

import pytest

from photopainter import cli, state
from photopainter.hardware import battery as bat
from photopainter.hardware.battery import Battery, BatteryUnavailable, FakeBattery, Reading

# --- readings --------------------------------------------------------------------------


@pytest.mark.parametrize("volts, milliamps, state_", [
    (0.016, 0.0, "none"),  # no cell: what the board reads with nothing on the connector
    (2.4, -150.0, "discharging"),  # current flowing means a cell, whatever the voltage says
    (3.98, 312.0, "charging"),
    (3.72, -181.0, "discharging"),
    (4.19, 0.3, "idle"),
    (4.19, 9.9, "idle"),
    (4.19, -9.9, "idle"),
])
def test_state_from_voltage_and_current_sign(volts, milliamps, state_):
    r = Reading(volts, milliamps)
    assert r.state == state_
    assert r.on_battery is (state_ == "discharging")


def test_a_wandering_voltage_with_no_current_means_no_cell():
    assert Reading(3.75, 0.0, steady=False).state == "none"
    assert Reading(3.75, 0.0, steady=False).percent is None
    assert Reading(3.75, -180.0, steady=False).state == "discharging"  # current flowing: a cell, whatever the volts did


@pytest.mark.parametrize("volts, percent", [
    (0.02, None), (2.9, 0), (3.0, 0), (3.1, 1), (3.2, 2), (3.3, 4), (3.4, 8), (3.5, 15), (3.56, 21), (3.7, 32), (3.8, 48), (4.0, 88), (4.1, 100), (4.2, 100),
])
def test_percent_follows_the_curve(volts, percent):
    assert Reading(volts, 0.0).percent == percent


def test_reading_text_and_dict():
    r = Reading(3.72, -181.0)
    assert str(r) == "3.72 V  -181 mA  discharging  about 35 %"
    assert str(Reading(0.016, 0.0)) == "0.02 V  +0 mA  none"
    assert r.to_dict() == {"volts": 3.72, "milliamps": -181.0, "state": "discharging", "percent": 35}


# --- the chip --------------------------------------------------------------------------


class FakeBus:
    """An INA219 as /dev/i2c-1 shows it: a register pointer, 16-bit big-endian registers."""

    def __init__(self, registers: dict[int, int], opened: list):
        self.registers, self.pointer, self.log = dict(registers), None, []
        self.closed = False
        opened.append(self)

    def write(self, data: bytes) -> None:
        self.pointer = data[0]
        self.log.append(("write", data))
        if len(data) == 3:
            self.registers[data[0]] = (data[1] << 8) | data[2]

    def read(self, n: int) -> bytes:
        value = self.registers.get(self.pointer, 0)
        self.log.append(("read", self.pointer))
        return bytes([value >> 8, value & 0xFF])[:n]

    def close(self) -> None:
        self.closed = True


def chip(registers: dict[int, int]):
    opened: list = []
    battery = Battery(opener=lambda device, address: FakeBus(registers, opened), sleep=lambda s: None)
    return battery, opened


def test_read_configures_the_chip_then_reads_voltage_and_current():
    # 3.98 V: the bus register holds the voltage in 4 mV steps, shifted left 3.
    battery, opened = chip({bat.REG_BUS_VOLTAGE: 995 << 3, bat.REG_CURRENT: 2047})
    r = battery.read()
    assert r == Reading(3.98, 312.1)  # 2047 × 0.1524 mA
    bus = opened[0]
    assert bus.registers[bat.REG_CALIBRATION] == 26868
    assert bus.registers[bat.REG_CONFIG] == 0x0EEF
    assert bus.log[:2] == [("write", bytes([0x05, 0x68, 0xF4])), ("write", bytes([0x00, 0x0E, 0xEF]))]
    assert bus.closed


def test_negative_current_means_discharging():
    battery, _ = chip({bat.REG_BUS_VOLTAGE: 930 << 3, bat.REG_CURRENT: (-1188) & 0xFFFF})
    r = battery.read()
    assert (r.volts, r.milliamps, r.state) == (3.72, -181.1, "discharging")


def test_no_cell_reads_millivolts():
    battery, _ = chip({bat.REG_BUS_VOLTAGE: 4 << 3, bat.REG_CURRENT: 0})
    assert battery.read().state == "none"


def test_no_cell_caught_mid_pulse_is_still_none():
    """The floating line read 3.75 V, then 0.85 V 100 ms later (as measured with no cell)."""
    class PulsingBus(FakeBus):
        def read(self, n):
            value = super().read(n)
            self.registers[bat.REG_BUS_VOLTAGE] = 212 << 3  # the pulse is over: 0.85 V from now on
            return value

    opened: list = []
    battery = Battery(opener=lambda d, a: PulsingBus({bat.REG_BUS_VOLTAGE: 937 << 3, bat.REG_CURRENT: 0}, opened),
                      sleep=lambda s: None)
    r = battery.read()
    assert (r.volts, r.steady, r.state) == (0.848, False, "none")
    gaps = [args for name, args in opened[0].log if name == "read"]
    assert gaps == [bat.REG_BUS_VOLTAGE, bat.REG_CURRENT, bat.REG_BUS_VOLTAGE]


def test_no_i2c_device_is_unavailable(tmp_path):
    battery = Battery(device=str(tmp_path / "i2c-1"))
    with pytest.raises(BatteryUnavailable, match="can't open"):
        battery.read()


def test_a_chip_that_does_not_answer_is_unavailable_and_the_bus_is_closed():
    class DeadBus(FakeBus):
        def read(self, n):
            raise OSError(121, "Remote I/O error")

    opened: list = []
    battery = Battery(opener=lambda d, a: DeadBus({}, opened), sleep=lambda s: None)
    with pytest.raises(BatteryUnavailable, match="Remote I/O"):
        battery.read()
    assert opened[0].closed


# --- the fake ---------------------------------------------------------------------------


def test_fake_battery_reads_its_file(tmp_path):
    fake = FakeBattery(tmp_path)
    with pytest.raises(BatteryUnavailable, match="fake_battery.json"):
        fake.read()
    fake.set(3.7, -180)
    assert fake.read() == Reading(3.7, -180.0)
    (tmp_path / bat.FAKE_FILE).write_text("{}")
    with pytest.raises(BatteryUnavailable, match="bad"):
        fake.read()


def test_cli_battery_prints_a_reading(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv(state.STATE_DIR_ENV, str(tmp_path))
    FakeBattery(tmp_path).set(3.98, 312)
    assert cli.main(["battery", "--fake-panel"]) == 0
    assert capsys.readouterr().out.strip().endswith("3.98 V  +312 mA  charging  about 84 %")
    (tmp_path / bat.FAKE_FILE).unlink()
    assert cli.main(["battery", "--fake-panel"]) == 1  # unavailable: one error line, status 1


def test_fake_file_is_plain_json(tmp_path):
    FakeBattery(tmp_path).set(4.1, 0)
    assert json.loads((tmp_path / bat.FAKE_FILE).read_text()) == {"volts": 4.1, "milliamps": 0}
