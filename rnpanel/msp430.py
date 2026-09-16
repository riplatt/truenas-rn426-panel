import os, time, ctypes
from rnpanel.i2c import fcntl, I2C_SLAVE, I2C_SMBUS, _smbus_ioctl, find_i801_bus

class Msp430Buttons:
    """RN426/RNx26 front-board TI MSP430 over the i801 SMBus. See the
    module-level Buttons comment above for the reg 0x02 write hazard."""
    LEFT, RIGHT, UP, DOWN, CENTER = 0x01, 0x02, 0x04, 0x08, 0x10
    ADDR, REG = 0x1C, 0x04

    def __init__(self, gpio):
        self.gpio = gpio
        self.fd = os.open("/dev/i2c-%d" % find_i801_bus(), os.O_RDWR)
        fcntl.ioctl(self.fd, I2C_SLAVE, self.ADDR)
        self.prev = 0
        # Debounce state for events(): one reg 0x04 read per interrupt-pad
        # assertion, re-armed only once the pad has been continuously
        # inactive for >= 50 ms (see docs/buttons-protocol.md).
        self._armed = True
        self._high_since = time.time()

    def _read(self, reg):
        buf = (ctypes.c_ubyte * 34)()
        a = _smbus_ioctl(1, reg, 2, ctypes.cast(buf, ctypes.c_void_p))
        fcntl.ioctl(self.fd, I2C_SMBUS, a)
        return buf[0]

    def pressed(self):
        """Bitmap of buttons newly pressed since the last call (rising
        edge). Kept for tools/find-int-pad.py compatibility; events() below
        is what run() uses."""
        try:
            v = self._read(self.REG)
        except OSError:
            return 0
        new = v & ~self.prev
        self.prev = v
        return new

    def events(self, now):
        """Watch the MCU interrupt pad via the Gpio backend -- a cheap read
        that does NOT touch i2c. We read the MCU (reg 0x04) ONLY when this
        pad signals a press, i.e. only when the MCU is awake, and only once
        per assertion (armed/re-armed below). We never poll a sleeping MCU,
        so its button reporting is never corrupted (the old i2c-poll loop
        did that). UP->PREV, DOWN->NEXT, CENTER->OK. LEFT/RIGHT emit their
        own "LEFT"/"RIGHT" actions: run() treats those (and any action it
        doesn't recognize) as activity-only -- they reset the idle timer
        and wake a sleeping display like any other action, but never move
        a page, matching the RN426's original behaviour (any nonzero
        bitmap counted as activity). Never writes reg 0x02 (see class
        comment)."""
        actions = []
        if self.gpio.int_active():
            self._high_since = None
            if self._armed:                     # one event per physical press
                self._armed = False
                try:
                    v = self._read(self.REG)
                except OSError:
                    v = 0
                if v:
                    if v & self.LEFT:   actions.append("LEFT")
                    if v & self.RIGHT:  actions.append("RIGHT")
                    if v & self.UP:     actions.append("PREV")
                    if v & self.DOWN:   actions.append("NEXT")
                    if v & self.CENTER: actions.append("OK")
                    if not actions:     actions.append("ACTIVITY")   # unknown bits still counted as activity, as before
        else:
            # pad idle (high). Re-arm once it has been stable-high briefly,
            # to debounce the MCU's pulse-train (one physical press -> one event).
            if self._high_since is None:
                self._high_since = now
            elif now - self._high_since >= 0.05:
                self._armed = True
        return actions

Buttons = Msp430Buttons   # alias kept for external users/backward compatibility
                           # (tools/find-int-pad.py does not import this -- it has its own reg04())
