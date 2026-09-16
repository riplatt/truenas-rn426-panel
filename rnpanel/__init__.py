"""
rnpanel -- RN426/RN428 (and experimental RN316, 528X/628X) front-panel
driver package. See rn426_panel.py (the entrypoint next to this directory)
for the usage docstring; this package only holds the implementation.

Layer map (leaves first; nothing here imports rnpanel.app):
    gpio.py     Gpio backends (Dnv MMIO, ICH I/O-port) + the P2SB unhide
                helper the Dnv backend needs. Leaf module.
    lcd.py      INIT_SEQ + the SSD1305 LCD driving logic. Leaf module.
    i2c.py      fcntl import guard, I2C_SLAVE/I2C_SMBUS ioctl constants,
                _smbus_ioctl, find_i801_bus. Leaf module.
    msp430.py   Msp430Buttons (RN426/RNx26 front-board MCU over SMBus).
                Imports i2c.
    sx8635.py   Sx8635Buttons (RN316 touch-wheel controller over SMBus) +
                its pure wheel/button math. Imports i2c.
    pages.py    Info page functions + PAGES. Leaf module.
    models.py   MODELS table + detect_model. Imports gpio, lcd.
    app.py      _build/_build_buttons, BUTTON_CLASSES, run(), main().
                Imports models, lcd, msp430, sx8635, pages.

This module deliberately has NO imports of its own, so importing any single
leaf module (e.g. from tools/sx8635-watch.py) never pulls in the rest of
the package, and no import cycle can be introduced here.

Adding a model:
    1. Add a row to MODELS in models.py (backend, geometry, init_seq, pins,
       buttons, ...).
    2. If it needs a new GPIO backend, add the class to gpio.py (or, for a
       new button protocol, its own module alongside msp430.py/sx8635.py).
    3. If it uses a new button protocol, add its class to app.BUTTON_CLASSES.
"""
