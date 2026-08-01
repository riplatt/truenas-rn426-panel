# Running on plain Debian / OMV

**Hardware requirement is unchanged** — this is still only for a ReadyNAS
RN426 / RN526 / RN626X. This page is about running a different **OS** on
that same front board, not different hardware; for different hardware see
[`porting.md`](porting.md).

## Nothing here is TrueNAS-specific

Nothing in the driver is TrueNAS-specific: it talks to `/dev/mem`, `/dev/port`
and `/dev/i2c` directly, plus Pillow for rendering. The only TrueNAS-specific
piece is `install.sh`, which registers the daemon in TrueNAS's config DB
because SCALE resets the root filesystem on every update. Plain Debian and
OpenMediaVault don't do that, so skip `install.sh` entirely and install the
unit already shipped in [`systemd/rn426-panel.service`](../systemd/rn426-panel.service):

```bash
sudo apt install python3-pil fonts-dejavu-core
sudo mkdir -p /opt/rn426-panel
sudo cp rn426_panel.py /opt/rn426-panel/
sudo cp systemd/rn426-panel.service /etc/systemd/system/
# edit ExecStart if you put it somewhere other than /opt/rn426-panel
sudo systemctl enable --now rn426-panel
```

The unit already covers loading the i2c modules (`ExecStartPre` runs
`modprobe i2c-dev` and `i2c-i801`) and sets the idle-sleep timeout
(`RN_SLEEP=90`).

## Gotchas

- The DejaVu font paths in `LCD.__init__` are hard-coded to
  `/usr/share/fonts/truetype/dejavu/`. `fonts-dejavu-core` installs them
  there on Debian — just confirm the files exist if you're on a derivative
  that packages fonts differently.
- It must run as root — `/dev/mem`, `/dev/port` and `/dev/i2c` all require
  it. The unit runs as root by default; don't add a `User=` line.
