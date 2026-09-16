import subprocess, socket, glob

# --------------------------------------------------------------------------
# Info pages -- each returns (line1, line2)
# --------------------------------------------------------------------------
def _sh(cmd):
    try:
        return subprocess.check_output(cmd, shell=True, text=True, timeout=4).strip()
    except Exception:
        return ""

def _ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.connect(("1.1.1.1", 80))
        ip = s.getsockname()[0]; s.close(); return ip
    except Exception:
        return "no-ip"

def page_host():
    return (socket.gethostname()[:12], _ip())

def page_pool():
    o = _sh("zpool list -H -o name,health,cap")
    if o:
        p = o.splitlines()[0].split()
        return ("Pool " + p[0][:7], "%s %s" % (p[1][:7], p[2]))
    return ("Pool", "n/a")

def page_temp():
    t = fan = ""
    for hw in sorted(glob.glob("/sys/class/hwmon/hwmon*")):
        try:
            if open(hw + "/name").read().strip() == "coretemp":
                t = "CPU %dC" % max(int(open(f).read()) // 1000 for f in glob.glob(hw + "/temp*_input"))
        except Exception:
            pass
    for hw in sorted(glob.glob("/sys/class/hwmon/hwmon*")):
        try:
            for f in sorted(glob.glob(hw + "/fan*_input")):
                r = int(open(f).read())
                if r > 0:
                    fan = "Fan %drpm" % r; break
            if fan:
                break
        except Exception:
            pass
    return (t or "CPU ?", fan or "Fan ?")

def page_uptime():
    up = float(open("/proc/uptime").read().split()[0])
    d, h, m = int(up // 86400), int((up % 86400) // 3600), int((up % 3600) // 60)
    la = open("/proc/loadavg").read().split()[0]
    return ("Up %dd %dh" % (d, h) if d else "Up %dh %dm" % (h, m), "Load " + la)

PAGES = [page_host, page_pool, page_temp, page_uptime]
