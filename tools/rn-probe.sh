#!/usr/bin/env bash
# rn-probe.sh -- read-only hardware survey for porting this driver to a
# DIFFERENT ReadyNAS model.
#
# You don't need to understand this script to use it. Run it, then paste the
# whole output (or attach the file it writes) into a GitHub issue. It collects
# everything the maintainer needs to judge whether/how this driver could be
# ported to your board: CPU family, i2c bus layout, hwmon sensors, PCI/GPIO
# bits the LCD side depends on.
#
# *** SAFETY: read this before running ***
# This driver's central hard-won lesson is that the front-board MSP430
# microcontroller (the chip that scans the buttons) deep-sleeps, and touching
# it wrong over i2c corrupts its button scanning -- see docs/buttons-protocol.md.
# A corrupted MCU cannot be fixed with a reboot: the front board runs on
# standby power even when the NAS is "off", so recovery needs a FULL AC
# power-off/on (unplug it, wait, plug back in).
#
# This script is strictly READ-ONLY: it never runs i2cset, never writes any
# sysfs file, /dev/mem, /dev/port, or PCI config, and never loads a kernel
# module (it only reports what's already loaded). The one i2c operation it
# performs is a single `i2cdetect -y -r` pass per adapter -- SMBus
# read-byte probing, the gentlest scan mode i2c-tools has. It deliberately
# avoids i2cdetect's default probe mode and its `-q` (quick-write) mode,
# because a quick-write to an unknown device is exactly the kind of unsolicited
# poke that can wedge a sleeping MCU. It also runs that scan exactly ONCE per
# bus -- no loops, no polling.
#
# On the RN426 family this is low-risk (this driver's own tooling does the
# same read-byte probe) but it is not proven zero-risk on an unknown board,
# which is the whole reason this script exists. If your buttons stop
# responding after running this, do one full AC power-off/on -- unplug the
# NAS, wait a few seconds, plug it back in. A warm reboot will NOT fix it.
#
# Usage:
#   bash tools/rn-probe.sh          # as root, or via sudo, for full detail
#
# Root is not required to run this, but /dev/mem-free items (i2c, hwmon, PCI
# IDs, GPIO chip labels) all still work unprivileged on most systems; running
# as root just means dmidecode and some /sys nodes are more likely to be
# readable.
set -u

OUT="${RN_PROBE_OUT:-/tmp/rn-probe-$(hostname 2>/dev/null || echo unknown).txt}"

have() { command -v "$1" >/dev/null 2>&1; }

hr() { printf '%s\n' "------------------------------------------------------------"; }
section() { printf '\n=== %s ===\n' "$1"; }

# Everything below is written to both stdout and $OUT via the pipeline at the
# very end of this script (a single `report | tee`), so functions just print.

report() {
hr
echo "rn-probe.sh report -- paste this whole thing into your GitHub issue"
echo "Generated on: $(hostname 2>/dev/null || echo unknown)"
hr

section "1. Identity"
if have dmidecode; then
    for key in system-product-name system-version baseboard-product-name; do
        val=$(dmidecode -s "$key" 2>/dev/null)
        printf '%-28s %s\n' "dmidecode $key:" "${val:-<unavailable>}"
    done
else
    echo "dmidecode: not installed, falling back to /sys/class/dmi/id/*"
fi
for f in product_name product_version board_name bios_version bios_date; do
    p="/sys/class/dmi/id/$f"
    if [ -r "$p" ]; then
        printf '%-28s %s\n' "$f:" "$(cat "$p" 2>/dev/null)"
    else
        printf '%-28s %s\n' "$f:" "<unreadable>"
    fi
done

section "2. CPU (most important field -- this driver's GPIO addresses are Intel Denverton/Atom C3000 specific)"
if have lscpu; then
    lscpu | grep -i '^Model name' || echo "lscpu ran but printed no 'Model name' line"
else
    echo "lscpu: not installed, falling back to /proc/cpuinfo"
    grep -m1 '^model name' /proc/cpuinfo 2>/dev/null || echo "  <could not read /proc/cpuinfo>"
fi

section "3. Kernel"
uname -a 2>/dev/null || echo "<uname failed>"

section "4. i2c"
echo "-- loaded i2c modules --"
if have lsmod; then
    lsmod | grep -i i2c || echo "  (none matched 'i2c')"
else
    echo "  lsmod: not installed"
fi

echo
echo "-- i2c-dev adapters --"
i801_buses=""
if [ -d /sys/class/i2c-dev ]; then
    for d in /sys/class/i2c-dev/i2c-*; do
        [ -e "$d" ] || continue
        bus=$(basename "$d" | sed 's/^i2c-//')
        name="<unreadable>"
        [ -r "$d/name" ] && name=$(cat "$d/name" 2>/dev/null)
        tag=""
        case "$name" in
            *I801*) tag=" <-- likely the i801 (RN426's MSP430 + RTC bus)"; i801_buses="$i801_buses $bus" ;;
        esac
        printf '  i2c-%s: %s%s\n' "$bus" "$name" "$tag"
    done
else
    echo "  /sys/class/i2c-dev not present"
fi
echo "  (NOTE: bus numbering is NOT stable across reboots -- identify by name, not number.)"

echo
echo "-- i2cdetect (ONE read-byte pass per adapter; 0x1c = expected MSP430 on RN426 family, 0x44 = RTC) --"
if have i2cdetect; then
    if [ -d /sys/class/i2c-dev ]; then
        for d in /sys/class/i2c-dev/i2c-*; do
            [ -e "$d" ] || continue
            bus=$(basename "$d" | sed 's/^i2c-//')
            echo "  bus $bus:"
            i2cdetect -y -r "$bus" 2>&1 | sed 's/^/    /'
        done
    else
        echo "  no i2c-dev adapters found to scan"
    fi
else
    echo "  i2cdetect: not installed (part of i2c-tools) -- skipping i2c bus scan"
fi

section "5. hwmon (fan control side)"
if [ -d /sys/class/hwmon ]; then
    for h in /sys/class/hwmon/hwmon*; do
        [ -e "$h" ] || continue
        name="<unreadable>"
        [ -r "$h/name" ] && name=$(cat "$h/name" 2>/dev/null)
        echo "  $h: $name"
        for f in "$h"/fan*_input "$h"/pwm* "$h"/temp*_input; do
            [ -e "$f" ] || continue
            [ -r "$f" ] || continue
            printf '    %-24s %s\n' "$(basename "$f"):" "$(cat "$f" 2>/dev/null)"
        done
    done
else
    echo "  /sys/class/hwmon not present"
fi

section "6. PCI (SMBus / LPC / ISA bridge; 00:1f.1 = P2SB the LCD driver needs to unhide)"
if have lspci; then
    lspci -nn 2>/dev/null | grep -iE 'SMBus|LPC|ISA bridge' || echo "  (no SMBus/LPC/ISA bridge lines matched)"
    echo
    if lspci -nn -s 00:1f.1 >/dev/null 2>&1 && [ -n "$(lspci -nn -s 00:1f.1 2>/dev/null)" ]; then
        echo "  00:1f.1 present: $(lspci -nn -s 00:1f.1 2>/dev/null)"
    else
        echo "  00:1f.1 (P2SB): not found at that address on this board"
    fi
else
    echo "  lspci: not installed -- skipping PCI scan"
fi

section "7. GPIO (read-only)"
if [ -d /sys/class/gpio ]; then
    for c in /sys/class/gpio/gpiochip*; do
        [ -e "$c" ] || continue
        if [ -r "$c/label" ]; then
            printf '  %s: %s\n' "$(basename "$c")" "$(cat "$c/label" 2>/dev/null)"
        else
            printf '  %s: <label unreadable>\n' "$(basename "$c")"
        fi
    done
    ls /sys/class/gpio 2>/dev/null | sed 's/^/  entry: /'
else
    echo "  /sys/class/gpio not present"
fi

section "8. Front panel state"
echo "  (This script cannot see the LCD. Please look at the unit right now and"
echo "   note in your GitHub issue which of these it's showing:)"
echo "     - stuck on \"Booting...\""
echo "     - blank / backlight off"
echo "     - showing real info (hostname, pool, etc. -- i.e. already working)"
echo "     - something else (describe it)"

hr
echo "ALSO PLEASE INCLUDE in your GitHub issue (this script cannot determine these):"
echo "  1. Your exact ReadyNAS model number (e.g. RN424, RN524X, RN214, ...)."
echo "  2. A PHOTO of the front board's silkscreen -- the small printed marking"
echo "     that identifies the board revision (e.g. this project's board reads"
echo "     \"RN526&RN626X Front Board V20\"). It's on the small board behind the"
echo "     front bezel, connected to the mainboard by a ribbon cable."
echo "  3. What the front-panel LCD is currently displaying (see section 8 above)."
hr
}

report | tee "$OUT"

echo
echo "Report written to: $OUT"
echo "Attach that file, or paste its contents, into your GitHub issue."
