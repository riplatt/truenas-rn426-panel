#!/usr/bin/env bash
# it87 chassis-fan controller - ReadyNAS 426 / TrueNAS SCALE
# Drives pwm2 on the it8622 from the hottest drive + CPU package temp.
# v2 (2026-05-27): EMA temp smoothing + asymmetric PWM slew limiting +
#   raised knee. Stops the fan hunting/jumping between floor and ramp.
#
# Requires the `it87` module loaded first (it does not autoprobe -- see
# docs/fan-control.md), e.g. `modprobe it87 force_id=0x8622`. Must run as root
# (writes /sys/class/hwmon/*/pwm2).
set -u
INTERVAL=10            # poll period (s); short so slew ramps stay smooth
PWM_MIN=12             # floor (~858 RPM; hardware cannot go lower)
PWM_MAX=255
SLEW_UP=10             # max PWM increase per tick (~4 min floor->full)
SLEW_DN=4              # max PWM decrease per tick (~10 min full->floor)
D_LOW=48; D_HIGH=60    # drive curve: floor until 48C, full by 60C
C_LOW=58; C_HIGH=78    # cpu backstop: floor until 58C, full by 78C
LOG=${LOG:-/var/log/it87-fancontrol.log}

ts(){ date '+%F %T'; }
logmsg(){ echo "$(ts) $*" >> "$LOG"; }
find_hwmon(){ local h; for h in /sys/class/hwmon/hwmon*; do [ -r "$h/name" ] || continue; [ "$(cat "$h/name")" = "$1" ] && echo "$h"; done; }
curve(){ local t=$1 lo=$2 hi=$3
  if   [ "$t" -le "$lo" ]; then echo "$PWM_MIN"
  elif [ "$t" -ge "$hi" ]; then echo "$PWM_MAX"
  else echo $(( PWM_MIN + (t-lo)*(PWM_MAX-PWM_MIN)/(hi-lo) )); fi; }

IT=""
for i in $(seq 1 30); do IT="$(find_hwmon it8622 | head -n1)"; [ -n "$IT" ] && break; sleep 2; done
[ -z "$IT" ] && { logmsg "FATAL it8622 not found"; exit 1; }
logmsg "start v2: $IT/pwm2 min=$PWM_MIN up=$SLEW_UP dn=$SLEW_DN drive=${D_LOW}-${D_HIGH} cpu=${C_LOW}-${C_HIGH} int=${INTERVAL}s"

# Fail-safe: if this script dies for any reason (killed, OOM, unhandled
# error), pwm2 freezes at whatever it last commanded -- possibly the floor
# of 12 (~858 RPM) -- and stays there. The IT8622's own auto mode is not a
# safe fallback here: it chases temp1/temp3, which read a dead -128C (see
# docs/fan-control.md), so it just hunts garbage. Full speed on exit is the
# only failure mode that cannot cook the drives; a loud fan beats a silent,
# stuck-slow one.
trap 'echo 255 > "$IT/pwm2" 2>/dev/null' EXIT INT TERM

ema=0                  # smoothed drive temp, x10 fixed point
cur=$PWM_MIN           # pwm we are currently commanding
last_log=-1
echo 1 > "$IT/pwm2_enable" 2>/dev/null
while true; do
  dmax=0
  for h in $(find_hwmon drivetemp); do v=$(cat "$h/temp1_input" 2>/dev/null || echo 0); v=$((v/1000)); [ "$v" -gt "$dmax" ] && dmax=$v; done
  ct=0; ch="$(find_hwmon coretemp | head -n1)"; [ -n "$ch" ] && [ -r "$ch/temp1_input" ] && ct=$(( $(cat "$ch/temp1_input")/1000 ))

  s=$((dmax*10))
  if [ "$ema" -eq 0 ]; then ema=$s; else ema=$(( (ema*3 + s) / 4 )); fi
  dt=$(( ema / 10 ))

  dp=$(curve "$dt" "$D_LOW" "$D_HIGH")
  cpv=$(curve "$ct" "$C_LOW" "$C_HIGH")
  target=$dp; [ "$cpv" -gt "$target" ] && target=$cpv

  if   [ "$target" -gt "$cur" ]; then cur=$(( cur + SLEW_UP )); [ "$cur" -gt "$target" ] && cur=$target
  elif [ "$target" -lt "$cur" ]; then cur=$(( cur - SLEW_DN )); [ "$cur" -lt "$target" ] && cur=$target; fi
  [ "$cur" -lt "$PWM_MIN" ] && cur=$PWM_MIN
  [ "$cur" -gt "$PWM_MAX" ] && cur=$PWM_MAX

  echo "$cur" > "$IT/pwm2" 2>/dev/null
  if [ "$last_log" -lt 0 ] || [ $(( cur>last_log ? cur-last_log : last_log-cur )) -ge 4 ]; then
    logmsg "drive=${dmax}C(ema${dt}) cpu=${ct}C target=$target -> pwm2=$cur rpm=$(cat "$IT/fan2_input" 2>/dev/null)"
    last_log=$cur
  fi
  sleep "$INTERVAL"
done
