# truenas-rn426-panel

A front-panel driver that gets the LCD and navigation buttons working
on a NETGEAR ReadyNAS RN426 (and the related RN526 / RN626X, which share the
same front board) running TrueNAS SCALE.

When you install TrueNAS on this hardware, the little front display stays frozen
on **`Booting...`** forever and the buttons do nothing. TrueNAS has no
driver for NETGEAR's front board. This project is that driver, reverse-engineered
from the stock ReadyNAS firmware and reimplemented from scratch in pure Python
(no kernel module, no compiled helper).

![status: working](https://img.shields.io/badge/status-working-brightgreen)

> Companion guide: for getting TrueNAS *installed* on the RN426 in the first
> place, see the community write-up
> [*TrueNAS installation on ReadyNAS 426 — a step-by-step*](https://www.reddit.com/r/truenas/comments/1ozklya/truenas_installation_on_readynas_426_a_stepbystep/).
> This project picks up where that leaves off and restores the front panel.

---

## What it does

- Drives the 128×32 SSD1305 graphic LCD (it's an LCD panel with an
  SSD1305-class controller; NETGEAR's code calls it "oled").
- Reads the 5-way navigation buttons (up / down / left / right / center).
- Shows rotating info pages you cycle with the buttons:
  1. Hostname + IP
  2. Pool name, health, capacity
  3. CPU temperature + chassis fan RPM (fan RPM requires the `it87` module.
     See [`docs/fan-control.md`](docs/fan-control.md).)
  4. Uptime + load average
- Auto-sleeps the display after a configurable idle timeout (default 90 s) to
  avoid image retention/burn-in, and wakes on any button press.
- Installs as a systemd service that survives reboots and TrueNAS updates
  (registered through TrueNAS's config-DB init scripts, just like a fan-control
  daemon would be).

## How it works (short version)

- The LCD is bit-banged SPI over the Intel Denverton SoC GPIO pads, written
  directly through `/dev/mem` (PADCFG_DW0 registers). The P2SB window that
  exposes those registers is un-hidden with a pure-Python PCI-config write via
  `/dev/port`, no helper binary.
- The buttons live on a front-board MSP430 microcontroller on the Intel
  i801 SMBus; the driver reads its `reg 0x04` button bitmap over `/dev/i2c`.
  It does not poll i2c (that corrupts the MCU's button scanning over time).
  Instead it watches the MCU's interrupt line, a Denverton SoC GPIO pad
  (`0xFDC50570`) read via `/dev/mem`, and only reads `reg 0x04` when a press is
  signalled. Interrupt-driven responsiveness from userspace, no kernel module.
  See [`docs/buttons-protocol.md`](docs/buttons-protocol.md).

Full details, register maps, and the reverse-engineering story are in [`docs/`](docs/).
Chassis fan control is a separate concern, a different chip, the IT8622
Super-I/O, not the front board.

## Requirements

- A ReadyNAS RN426 / RN526 / RN626X. This list assumes TrueNAS SCALE. If
  you're running plain Debian or OMV instead, see the "Running on plain
  Debian / OMV" section below.
- `python3` + Pillow (`python3-pil`) and the DejaVu fonts, both ship
  with TrueNAS SCALE.
- Kernel modules `i2c-dev` and `i2c-i801` (loaded automatically by the installer).
- A pool to install onto (the root filesystem is reset on updates, so the files
  live on a pool and the service is registered in the config DB).

## Install

```bash
git clone https://github.com/riplatt/truenas-rn426-panel.git
cd truenas-rn426-panel
sudo ./install.sh /mnt/<your-pool>/rn426-panel
```

`install.sh` copies the driver to the given directory, loads the i2c modules,
registers a **POSTINIT** init script in the TrueNAS config DB, and starts the
service. See the script for the exact, reversible steps.

To remove it: `sudo ./install.sh --uninstall /mnt/<your-pool>/rn426-panel`.

If you're running plain Debian or OpenMediaVault instead of TrueNAS SCALE,
don't run `install.sh`, it requires `midclt` and will refuse to run. See
[`docs/debian-omv.md`](docs/debian-omv.md) instead.

## Usage

```bash
# the service runs automatically; manual control:
sudo systemctl status rn426-panel
python3 rn426_panel.py sleep   # blank the display now
python3 rn426_panel.py wake    # re-init / wake it
python3 rn426_panel.py run     # run the loop in the foreground

RN_SLEEP=120 ...               # env var: idle seconds before sleep (0 = never)
```

Customize the pages by editing the `page_*()` functions and the `PAGES` list in
`rn426_panel.py`.

## Important gotchas / hardware warnings

- i2c bus numbers are not stable across reboots. The i801 and iSMT SMBus
  adapters can swap between `i2c-0` and `i2c-1`. The driver finds the i801 bus by
  name, so don't hard-code it.
- **Never write the MSP430's `reg 0x02`** (its LED/control register) and
  **never drive the LCD's `EN` or `RST` low.** All three are the same failure
  mode: `reg 0x02` also gates button scanning, and `EN`/`RST` are *shared*
  front-board lines, not LCD-private. Lowering either one resets the MSP430
  the same way writing `reg 0x02` does. Any of the three wedges the MCU out
  of button-reporting mode. This driver is read-only toward the MCU
  and never lowers `EN`/`RST` (sleep is pixels-off only via `0xAE`, wake is
  plain display-on via `0xAF`).
- **Recovery is the same for all three, and a warm reboot will not do it:**
  the front board runs on standby power, so only a **full AC power-off/on**
  (unplug, wait, plug back in) clears a wedged MCU. If your buttons are dead
  or the display won't come back, pull the power cord.

## Troubleshooting

- **Temp page shows `Fan ?`.** The `it87` kernel module isn't loaded (it
  doesn't autoprobe). See [`docs/fan-control.md`](docs/fan-control.md) for
  how to load it and make that survive reboots.

## Running on plain Debian / OMV

Hardware requirement is unchanged. This is still only for a ReadyNAS
RN426 / RN526 / RN626X, just running a different OS on the same front
board. Nothing in the driver is TrueNAS-specific (it talks to `/dev/mem`,
`/dev/port` and `/dev/i2c` directly); the only TrueNAS-specific piece is
`install.sh`. See [`docs/debian-omv.md`](docs/debian-omv.md) for the install
steps.

## Porting to other ReadyNAS models

The approach (RE the stock firmware's `oled_probe` / `spi_send` / `i2cfb_reporter`,
then drive the pads via `/dev/mem`) generalizes. The per-model GPIO pad map lives
in the firmware (look for the `rn426_8` / `rnx16` config structs). See
[`docs/porting.md`](docs/porting.md).

## License

MIT. See [LICENSE](LICENSE). Not affiliated with or endorsed by NETGEAR or
iXsystems. Use at your own risk; this pokes SoC registers directly.
