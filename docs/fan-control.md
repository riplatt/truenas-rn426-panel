# Fan control (IT8622 Super-I/O, LPC)

The chassis fan is not on the front board. It hangs off the mainboard's
IT8622 Super-I/O over LPC, a separate chip and bus from the LCD and the
MSP430. See
[`hardware.md`](hardware.md#related-but-separate-chassis-fan-control) for
where this sits relative to the rest of the front-panel hardware.

## Why it needs a module

TrueNAS SCALE does not load `it87` by default, and the chip cannot be
autoprobed. The driver has to poke LPC I/O ports directly to find it. Without
the module, there is no `fan*_input` anywhere under `/sys/class/hwmon`, which is
also why `rn426_panel.py`'s temp page shows **`Fan ?`**: `page_temp()` globs
every hwmon node for `fan*_input` and simply finds none.

## Loading it

```bash
modprobe it87 force_id=0x8622
```

Try plain `modprobe it87` first. `force_id=0x8622` is what's confirmed
working on this RN426. Do not cargo-cult it onto other hardware. Forcing the
wrong ID makes the driver talk to a chip that isn't actually there, which
gets you garbage reads/writes instead of a clean probe failure.

Verified result once loaded: an `it8622` hwmon node appears, exposing:

| Node | Observed | Notes |
|------|----------|-------|
| `fan2_input` | ~860 RPM at idle | chassis fan |
| `fan3_input` | `0` | unpopulated header, always reads 0 |
| `pwm2` / `pwm2_enable` | — | PWM duty + mode control for the chassis fan |

Once `fan2_input` exists, `rn426_panel.py` picks it up automatically,
`page_temp()` already globs every hwmon for `fan*_input`.

## ⚠️ Do not trust the chip's built-in automatic mode

On this chip, `temp1` and `temp3` read a permanent **`-128`C** (disconnected
sensors). The IT8622's own automatic fan mode chases whichever temp zone
`pwm2` is bound to, if that's one of the dead `-128`C inputs, the fan
**pulses/hunts** as the chip's control logic reacts to garbage. That's why
[`tools/it87-fancontrol.sh`](../tools/it87-fancontrol.sh) ignores auto mode
and drives `pwm2` in **manual mode** (`pwm2_enable=1`) from a
userspace curve based on real drive/CPU temps instead.

## ⚠️ PWM floor

`pwm2` values below **~12** stall the fan outright. `12` is the real floor
(~858 RPM), the fan-control script never commands lower than that.

## The fan-curve script

[`tools/it87-fancontrol.sh`](../tools/it87-fancontrol.sh) is the working
controller pulled off this NAS. Summary of what it does (see the script for
the exact tunables):

- Polls every 10 s.
- Takes the **hottest `drivetemp` sensor** across all disks, EMA-smoothed to
  avoid chasing single-sample noise, and maps it through a curve: floor below
  48C, full speed by 60C.
- Takes CPU package temp (`coretemp`) as a backstop: floor below 58C, full by
  78C.
- Commands `pwm2` to whichever of the two curves wants more speed.
- **Slew-limits** the PWM change per tick, up to +10/tick (~4 min floor to
  full) and down to -4/tick (~10 min full to floor), so the fan ramps
  smoothly instead of jumping.
- Logs to `$LOG` (default `/var/log/it87-fancontrol.log`, overridable via the
  `LOG` env var) whenever the commanded PWM moves by 4 or more. Writes are
  infrequent but unbounded over time. If long-term log size matters, add a
  logrotate entry for `$LOG` or point `LOG` at a tmpfs path.

> ⚠️ **`pwm2` holds its last value if this script dies.** There is no
> hardware fallback. See above. That means the script must run under
> something that restarts it, which is exactly what the `systemd-run
> --property=Restart=always` install recipe below is for; do not run it
> as a bare backgrounded process. As of this revision the script also
> installs a `trap ... EXIT INT TERM` that forces `pwm2` to `255` on any
> exit, so a crash makes the fan **loud**, not silent-and-hot, even in the
> gap before `Restart=always` brings it back.

## Persistence

Do not use `/etc/modules`, `/etc/modules-load.d`, or a plain systemd unit
for the `modprobe`. Like everything else on the root filesystem, changes
there don't survive a SCALE update (SCALE updates into a new boot
environment, so anything not registered in the config DB is gone). Register
the module load in the TrueNAS config DB instead:

**System Settings -> Advanced -> Init/Shutdown Scripts** -> add an entry:

- Type: **Command**
- Command: `modprobe it87 force_id=0x8622`
- When: **PREINIT**

**PREINIT** is just tidier, the timing isn't critical. `page_temp()` re-globs
hwmon every ~5 s and the fan script polls for the `it8622` node for up to
60 s, so a POSTINIT `modprobe` would also work.

### Installing the fan-curve script itself

Install `tools/it87-fancontrol.sh` as a **POSTINIT** init script the same way
`install.sh` registers the panel daemon, via `systemd-run`, so it survives
reboots without needing a unit file on the root filesystem (changes there
don't survive a SCALE update):

```bash
cp tools/it87-fancontrol.sh /mnt/<your-pool>/rn426-panel/it87-fancontrol.sh
chmod +x /mnt/<your-pool>/rn426-panel/it87-fancontrol.sh
systemd-run --unit=it87-fancontrol --property=Restart=always \
  --property=RestartSec=10 /bin/bash /mnt/<your-pool>/rn426-panel/it87-fancontrol.sh
```

Then add a second **System Settings -> Advanced -> Init/Shutdown Scripts**
entry (Type: Command, When: **POSTINIT**) with that same `systemd-run` command
so it re-registers on every boot, after the PREINIT `modprobe it87` above has
already run.
