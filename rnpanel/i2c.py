import os, glob, ctypes
try:
    import fcntl                        # POSIX only; needed by Buttons (real i2c hardware)
except ImportError:
    fcntl = None                        # lets this module import (e.g. for tests) on Windows

# --------------------------------------------------------------------------
# Buttons: two independent front-panel protocols, picked by
# MODELS[...]["buttons"] (never derived from BTN_INT/has_int_line -- see
# _button_class). Both classes take the model's Gpio in __init__ and call
# gpio.int_active() themselves, and both expose events(now) -> list of
# action strings drawn from {"NEXT", "PREV", "OK"}, so run() is backend-
# agnostic: it does not know or care which protocol produced an action.
#
#   "msp430" -- Msp430Buttons: RN426/RNx26 front-board TI MSP430 over the
#     i801 SMBus. reg 0x04 = active-high button bitmap. READ ONLY.
#     IMPORTANT: do NOT write the MCU's reg 0x02 (LED/control). On this
#     firmware it also gates button scanning, and once disabled it only
#     recovers on a full power-cycle (a warm reboot is not enough). This
#     driver never writes the MCU.
#   "sx8635" -- Sx8635Buttons: RN316 front-board Semtech SX8635 touch-wheel
#     controller over the i801 SMBus. See that class's docstring.
# --------------------------------------------------------------------------
I2C_SLAVE, I2C_SMBUS = 0x0703, 0x0720
class _smbus_ioctl(ctypes.Structure):
    _fields_ = [("rw", ctypes.c_ubyte), ("cmd", ctypes.c_ubyte),
                ("size", ctypes.c_uint), ("data", ctypes.c_void_p)]

def find_i801_bus():
    """Find the i801 SMBus adapter by name -- the /dev/i2c-N number is NOT
    stable across reboots (iSMT and i801 can swap)."""
    for d in sorted(glob.glob("/sys/class/i2c-dev/i2c-*")):
        try:
            if "I801" in open(os.path.join(d, "name")).read():
                return int(d.rsplit("-", 1)[1])
        except OSError:
            pass
    return 1
