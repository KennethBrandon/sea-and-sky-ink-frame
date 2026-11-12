"""Hardware access: the e-paper panel (and later battery and RTC).

This is the only package allowed to import spidev / gpiozero / smbus, and it must do so
lazily (inside functions), so the rest of the project imports cleanly on a Mac.
"""
