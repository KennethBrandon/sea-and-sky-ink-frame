"""The buttons on the back: two switches from P1's RXD and TXD pins to its GND pin.

P1 is the board's 3-pin header by the USB-C socket (docs/hardware.md "Buttons"). With the
serial port off, as it is on the Pi, RXD and TXD are two free GPIOs that need no soldering.
A button connects its pin to ground, so a pin reads 0 while its button is down, with the
Pi's own pull-up holding it at 1 otherwise. No resistor, and never 3.3 V or 5 V.

`lgpio` is imported here and only when the buttons are opened, so this imports on the Mac.
`FakeButtons` stands in for them in tests.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator

FORWARD_PIN = 15  # BCM, P1's RXD
BACK_PIN = 14  # BCM, P1's TXD
GPIO_CHIP = 0


class ButtonsUnavailable(RuntimeError):
    """The buttons' pins can't be opened: not on the Pi, or they're in use."""


class GpioButtons:
    """The real buttons. `read()` gives (forward is down, back is down)."""

    def __init__(self, forward_pin: int = FORWARD_PIN, back_pin: int = BACK_PIN, chip: int = GPIO_CHIP):
        try:
            import lgpio
        except ImportError as exc:
            raise ButtonsUnavailable("lgpio isn't installed (the buttons only work on the Pi)") from exc
        self._lgpio = lgpio
        self._pins = (forward_pin, back_pin)
        self._handle = None
        try:
            self._handle = lgpio.gpiochip_open(chip)
            for pin in self._pins:
                lgpio.gpio_claim_input(self._handle, pin, lgpio.SET_PULL_UP)
        except lgpio.error as exc:
            self.close()
            raise ButtonsUnavailable(f"can't open GPIO {self._pins}: {exc}") from exc

    def read(self) -> tuple[bool, bool]:
        forward, back = self._pins
        return (self._lgpio.gpio_read(self._handle, forward) == 0,
                self._lgpio.gpio_read(self._handle, back) == 0)

    def close(self) -> None:
        if self._handle is not None:
            self._lgpio.gpiochip_close(self._handle)
            self._handle = None


class FakeButtons:
    """Plays back (forward, back) samples; after the last, both are up."""

    def __init__(self, samples: Iterable[tuple[bool, bool]] = ()):
        self._samples: Iterator[tuple[bool, bool]] = iter(samples)
        self.closed = False

    def read(self) -> tuple[bool, bool]:
        return next(self._samples, (False, False))

    def close(self) -> None:
        self.closed = True
