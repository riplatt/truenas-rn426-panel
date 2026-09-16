#!/usr/bin/env python3
"""
RN426/RN428 front-panel driver for TrueNAS SCALE on the NETGEAR ReadyNAS RN426
and RN428 (one board, two models -- the stock firmware drives both from the
same "rn426_8" config struct).

Also carries EXPERIMENTAL support for the ReadyNAS 528X/628X (C224 chipset,
`gpio_ich`). That path is stage-gated bring-up: it has not been confirmed on
real hardware, buttons and LCD both included. See docs/porting.md for what
each stage checks. Everything else in this docstring (the "only" language
below, the register details) describes the RN426/RN428 path, which is the
one this driver was originally verified against.

NOT for any other model. The two backends above write hard-coded SoC
register addresses (Denverton PADCFG_DW0 via /dev/mem, or ICH I/O ports via
/dev/port); on a different chipset either one pokes unrelated hardware (see
issue #4 and docs/porting.md). The driver refuses to start on unrecognized
hardware; RN_MODEL=rn426, RN_MODEL=rnx26 or RN_MODEL=rn316 overrides the
check if you know better.

It drives the 128x32 SSD1305 graphic LCD by bit-banging SPI over SoC GPIO,
and reads the 5-way navigation buttons from the front-board MSP430
microcontroller over the Intel i801 SMBus. No kernel module required.

See docs/ for the full reverse-engineering writeup and protocol details.

Usage:  rn426_panel.py [run|sleep|wake]
Env:    RN_SLEEP = idle seconds before the display sleeps (default 90; 0 = never)
        RN_MODEL = force model detection (rn426 / rnx26 / rn316)

Requires: python3-pil (Pillow) and the DejaVu fonts (both ship with TrueNAS SCALE),
          i2c-dev + i2c-i801 kernel modules, and root (for /dev/mem, /dev/port, i2c).
"""
import sys
try:
    from rnpanel.app import main
except ImportError as e:
    # Covers a missing or stale rnpanel/ and a genuine import error inside it;
    # the hint is true for all three and re-raising keeps the traceback.
    print("rn426_panel.py: cannot import its rnpanel/ package (%s). rnpanel/ must sit "
          "next to this file and match this version -- re-run install.sh, or copy "
          "rnpanel/ alongside this file." % e, file=sys.stderr)
    raise

if __name__ == "__main__":
    main()
