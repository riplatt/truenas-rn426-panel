"""
rnpanel/sx8635_spm.py -- the ONLY module in this codebase permitted to write
to the RN316's SX8635 capacitive touch-wheel controller (i2c address 0x2b on
the i801 SMBus). rnpanel/sx8635.py (the event-reading class the daemon runs
every tick) stays completely write-free; this is a separate module, used
only by tools/sx8635-spm.py, the one-shot tool a person runs knowingly.

Why this exists: the factory CapMode configuration (loaded into the chip's
volatile SPM working RAM at power-up from a fixed on-chip pattern) splits
the RN316's touch ring in half -- CAP2-5 report as four separate buttons,
CAP6-9 as a different, separate 6-segment wheel -- even though the ring is
one physical set of electrodes. Reprogramming three SPM bytes (the CapMode
block, SPM offsets 0x0A-0x0C) turns the whole ring into a single 8-segment
wheel. Nothing else on the chip is touched: the other five bytes in the
same 8-byte block are read back and written back unchanged.

What this module CAN write. Exactly two things:
  1. Five specific single-register (register, value) pairs, used only to
     open and close the SPM read/write window and to trigger post-write
     sensor compensation. WRITE_WHITELIST below is the complete list, and
     _wr() raises for anything not in it -- there is no other single-
     register write path anywhere in this file.
  2. One 8-byte block, at a fixed base offset (SPM block 1, covering SPM
     0x08-0x0F). _wr_block1() is the only function that builds a
     write-direction block transfer, and it is entirely self-contained: it
     opens the SPM window for write, sets the base to block 1, reads both
     registers back to confirm that took effect, and only then issues the
     block write, closing the window again in its own `finally` regardless
     of outcome. It also asserts the three CapMode bytes are exactly the
     target value and that the other five bytes are byte-for-byte what was
     just read from the chip -- so even a caller mistake cannot smuggle an
     unexpected byte into the chip's configuration RAM.

What this module can NEVER write, by construction, not by a runtime check:
  - The chip's I2C slave address (SPM offset 0x04, "I2CAddress"), which lives
    in SPM block 0. The one function that builds a write-direction block
    transfer, _wr_block1(), never trusts a base some earlier call set: it
    opens the window and writes the block-1 base itself, immediately before
    writing, and reads both SpmCfg and SpmBaseAddr back to confirm the
    window is actually open for write at that base before it ever calls the
    block-write primitive. So no sequence of calls -- including one that
    first ran _wr_dump()'s read-only walk, which does visit base 0x00 (block
    0) -- can leave a pending block write aimed anywhere but block 1;
    _wr_block1() re-asserts its own addressing every single time it runs.
  - The chip's NVM (its permanent, limited-write-count storage). Burning it
    requires writing an unlock key pair to two specific registers, followed
    by a specific two-value pulse written to the SPM window's base
    register. Neither unlock register appears in WRITE_WHITELIST or
    DUMP_WHITELIST, and the only value either whitelist ever allows into
    the base register is the block-1 base -- so the pulse this module could
    ever issue there cannot be the burn pulse, key or no key.
  - A software reset of the chip. The datasheet's soft-reset register wipes
    the whole SPM back to its power-up state, discarding not just this
    module's own CapMode write but every setting the board's own firmware
    put there before the OS ever ran (proximity sensing, wheel rotation
    threshold, button configuration, LED autolight). This file contains no
    path to that register, under any value whatsoever. On a failed
    post-write verification (or an I/O error during the write itself),
    apply_capmode() stops and reports failure -- it never resets the chip
    and never retries the write. See
    .claude/advice/rn316-plan-review.md item 3 ("never soft-reset").
  - An unrecognised chip state does not get written over either:
    apply_capmode() only ever writes when the sentinel read decodes as the
    known factory ("qsm") layout; anything else (including read garbage from
    a chip that never actually opened its window) is reported and left
    alone.

Volatility is the safety net for everything this module IS allowed to
touch: SPM is working RAM, lost and reloaded from scratch on every
power-down or reset event (datasheet SS3.10, SS3.14). The worst this module
can do is undone by a cold power cycle -- exactly like the CapMode rewrite
NETGEAR's own stock firmware performs on every boot of every RN316 ever
shipped, just with a far smaller diff.
"""
import ctypes
import struct
import time

from rnpanel.i2c import fcntl, I2C_SLAVE, I2C_SMBUS, _smbus_ioctl, find_i801_bus

# --------------------------------------------------------------------------
# Register/value constants
# --------------------------------------------------------------------------
CAPMODE = (0x0F, 0xFF, 0xF5)      # target CapMode bytes: SPM 0x0A, 0x0B, 0x0C
QSM_CAPMODE = (0xFF, 0xF5, 0x55)  # factory QSM default CapMode bytes
BLOCK1_BASE = 0x08                # SPM block 1 base offset (covers SPM 0x08-0x0F)

REG_IRQSRC = 0x00
REG_SPMSTAT = 0x08        # read outside the window: SpmStat (bit3 NvmValid, bits2:0 NvmCount)
REG_COMPOPMODE = 0x09
REG_SPMCFG = 0x0D
REG_SPMBASE = 0x0E

SPM_CLOSED = 0x00          # SpmCfg: mode off (window closed)
SPM_WRITE_OPEN = 0x10      # SpmCfg: mode on, direction write
SPM_READ_OPEN = 0x18       # SpmCfg: mode on, direction read
SPM_MODE_MASK = 0x30       # SpmCfg bits 5:4 -- nonzero means the window is open
SPM_CFG_DIR_MASK = 0x38    # SpmCfg bits 5:3 -- mode + direction, used for _wr_block1's readback check

TRIGGER_COMPENSATION = 0x04   # CompOpMode bit 2, write direction: trigger compensation

IRQ_SPMWRITE_BIT = 0x20   # IrqSrc bit 5: SPM write done (reading IrqSrc clears it)
IRQ_COMP_BIT = 0x02       # IrqSrc bit 1: compensation done
SPM_WRITE_WAIT_TIMEOUT = 0.3   # seconds
POLL_SLEEP = 0.005             # seconds between IrqSrc polls

# The complete set of single-register writes apply_capmode()'s path may
# ever issue. _wr() raises for anything outside this frozenset -- see the
# module docstring for why that makes the burn/reset/address paths
# unreachable regardless of any bug elsewhere in this file.
WRITE_WHITELIST = frozenset({
    (REG_SPMCFG, SPM_CLOSED),                  # (0x0D, 0x00) close the SPM window
    (REG_SPMCFG, SPM_WRITE_OPEN),              # (0x0D, 0x10) open for write
    (REG_SPMCFG, SPM_READ_OPEN),               # (0x0D, 0x18) open for read
    (REG_SPMBASE, BLOCK1_BASE),                # (0x0E, 0x08) window base = block 1
    (REG_COMPOPMODE, TRIGGER_COMPENSATION),    # (0x09, 0x04) trigger compensation
})

# dump()'s own, SEPARATE whitelist: read-only window walks over all 16 SPM
# blocks (bases 0x00, 0x08, 0x10, .. 0x78), used only by tools/sx8635-spm.py's
# `dump` command via _wr_dump() below. Nothing on apply_capmode()'s call path
# calls _wr_dump or references DUMP_WHITELIST -- see the AST choke-point test
# in tests/test_sx8635_spm.py.
DUMP_WHITELIST = frozenset(
    {(REG_SPMCFG, SPM_CLOSED), (REG_SPMCFG, SPM_READ_OPEN)}
    | {(REG_SPMBASE, base) for base in range(0x00, 0x80, 8)}
)

I2C_FUNCS = 0x0705                          # ioctl request
I2C_FUNC_SMBUS_WRITE_I2C_BLOCK = 0x08000000
I2C_SMBUS_BYTE_DATA = 2
I2C_SMBUS_I2C_BLOCK_DATA = 8
SMBUS_WRITE, SMBUS_READ = 0, 1


# --------------------------------------------------------------------------
# Low-level transport primitives. Nothing above this line does I/O; nothing
# below this line applies policy (whitelists, block-content assertions).
# Tests replace these three functions and _funcs() with a fake in-memory
# transport, so the policy functions above run as real production code
# without touching fcntl/ctypes (which don't work on Windows anyway).
# --------------------------------------------------------------------------
def _smbus_write_byte(fd, reg, val):
    buf = (ctypes.c_ubyte * 34)()
    buf[0] = val
    xfer = _smbus_ioctl(SMBUS_WRITE, reg, I2C_SMBUS_BYTE_DATA, ctypes.cast(buf, ctypes.c_void_p))
    fcntl.ioctl(fd, I2C_SMBUS, xfer)


def _smbus_read_byte(fd, reg):
    buf = (ctypes.c_ubyte * 34)()
    xfer = _smbus_ioctl(SMBUS_READ, reg, I2C_SMBUS_BYTE_DATA, ctypes.cast(buf, ctypes.c_void_p))
    fcntl.ioctl(fd, I2C_SMBUS, xfer)
    return buf[0]


def _smbus_write_i2c_block(fd, cmd, data):
    """Issue one I2C_SMBUS_I2C_BLOCK_DATA transfer: block[0] = length,
    block[1:1+length] = data bytes. `data` must be exactly 8 bytes for the
    block-1 write this module performs (checked by the caller, _wr_block1)."""
    n = len(data)
    buf = (ctypes.c_ubyte * 34)()
    buf[0] = n
    for i, b in enumerate(data):
        buf[1 + i] = b
    xfer = _smbus_ioctl(SMBUS_WRITE, cmd, I2C_SMBUS_I2C_BLOCK_DATA, ctypes.cast(buf, ctypes.c_void_p))
    fcntl.ioctl(fd, I2C_SMBUS, xfer)


def _funcs(fd):
    """I2C_FUNCS ioctl: returns the adapter's supported-functions bitmask."""
    buf = struct.pack("L", 0)
    buf = fcntl.ioctl(fd, I2C_FUNCS, buf)
    return struct.unpack("L", buf)[0]


def _require_i2c_block_write(fd):
    if not (_funcs(fd) & I2C_FUNC_SMBUS_WRITE_I2C_BLOCK):
        raise RuntimeError(
            "i801 adapter does not report I2C_FUNC_SMBUS_WRITE_I2C_BLOCK -- "
            "refusing to write the SX8635 block. This module never falls back "
            "to single-register writes for the CapMode block.")


# --------------------------------------------------------------------------
# Policy: the only write choke points in this file.
# --------------------------------------------------------------------------
def _wr(fd, reg, val):
    """The only single-register write path used by the CapMode-write side of
    this module. Raises ValueError for any (reg, val) not in
    WRITE_WHITELIST."""
    if (reg, val) not in WRITE_WHITELIST:
        raise ValueError(
            "refusing to write SX8635 reg 0x%02x = 0x%02x: not in WRITE_WHITELIST" % (reg, val))
    _smbus_write_byte(fd, reg, val)


def _wr_dump(fd, reg, val):
    """dump()'s own single-register write path: opens/closes the SPM read
    window and walks its base across all 16 blocks. Raises ValueError for
    anything not in DUMP_WHITELIST. Used only by tools/sx8635-spm.py's
    `dump` command -- apply_capmode() and everything it calls uses _wr(),
    never this function."""
    if (reg, val) not in DUMP_WHITELIST:
        raise ValueError(
            "refusing to write SX8635 reg 0x%02x = 0x%02x: not in DUMP_WHITELIST" % (reg, val))
    _smbus_write_byte(fd, reg, val)


def _wr_block1(fd, buf, sentinel_buf):
    """The only function in this file that builds a write-direction block
    transfer. Entirely self-contained: it opens the SPM window for write,
    sets the base to block 1 itself, reads both registers back to confirm
    that took effect, issues the block write, and closes the window again in
    its own `finally` -- regardless of outcome. It never trusts a base or
    direction some other call set earlier.

    Payload safety, checked before anything is opened:
      - exactly 8 bytes
      - bytes 2:5 (the CapMode block) equal CAPMODE, the one value this
        module ever writes there
      - every other byte equals what was just read (sentinel_buf), so the
        five untouched fields (Reserved, CapModeMisc, CapSensitivity) go
        back to the chip byte-for-byte unchanged
    """
    buf = tuple(buf)
    sentinel_buf = tuple(sentinel_buf)
    if len(buf) != 8:
        raise ValueError("block1 write must be exactly 8 bytes, got %d" % len(buf))
    if buf[2:5] != CAPMODE:
        raise ValueError("block1 write must set CapMode to %r, got %r" % (CAPMODE, buf[2:5]))
    if buf[0:2] != sentinel_buf[0:2] or buf[5:8] != sentinel_buf[5:8]:
        raise ValueError(
            "block1 write must leave the other five bytes unchanged from the read "
            "that produced sentinel_buf")
    _require_i2c_block_write(fd)
    _wr(fd, REG_SPMCFG, SPM_WRITE_OPEN)
    try:
        _wr(fd, REG_SPMBASE, BLOCK1_BASE)
        cfg, base = _rd(fd, REG_SPMCFG), _rd(fd, REG_SPMBASE)
        if (cfg & SPM_CFG_DIR_MASK) != SPM_WRITE_OPEN or base != BLOCK1_BASE:
            raise RuntimeError(
                "SPM window not open for write at block 1 (SpmCfg=0x%02x SpmBaseAddr=0x%02x) "
                "-- refusing to write" % (cfg, base))
        _smbus_write_i2c_block(fd, 0x00, buf)
    finally:
        _wr(fd, REG_SPMCFG, SPM_CLOSED)


# --------------------------------------------------------------------------
# Reads (regs 0x00-0x0F). No whitelist: reads are unrestricted.
# --------------------------------------------------------------------------
def _rd(fd, reg):
    return _smbus_read_byte(fd, reg)


# --------------------------------------------------------------------------
# Higher-level operations.
# --------------------------------------------------------------------------
def recover(fd):
    """If a crashed previous session left the SPM window open (SpmCfg mode
    bits nonzero), close it. A no-op, and harmless, on a clean chip."""
    if _rd(fd, REG_SPMCFG) & SPM_MODE_MASK:
        _wr(fd, REG_SPMCFG, SPM_CLOSED)


def read_block1(fd):
    """Open the SPM read window at block 1 (covering SPM 0x08-0x0F), read
    all eight bytes, and ALWAYS close the window again in finally -- even if
    the base write or a data read raises partway through (the try starts
    immediately after the register that opens the window). Returns an
    8-tuple of ints."""
    _wr(fd, REG_SPMCFG, SPM_READ_OPEN)
    try:
        _wr(fd, REG_SPMBASE, BLOCK1_BASE)
        return tuple(_rd(fd, i) for i in range(8))
    finally:
        _wr(fd, REG_SPMCFG, SPM_CLOSED)


def layout_of(block1):
    """Pure classification of a block-1 read by its CapMode bytes (indices
    2:5, i.e. SPM 0x0A-0x0C). No I/O."""
    cm = tuple(block1[2:5])
    if cm == CAPMODE:
        return "netgear"
    if cm == QSM_CAPMODE:
        return "qsm"
    return "unknown"


def _poll_irq_bit(fd, bit, timeout):
    """Poll IrqSrc for `bit` until it's seen or `timeout` elapses, sleeping
    POLL_SLEEP between reads. Reading IrqSrc clears it, so a plain poll loop
    is correct here -- each read either finds the bit or consumes a stale
    one. OSError is tolerated on the read (the chip can NAK briefly while
    busy applying a write or compensating) but never suppresses a write
    error anywhere else. Returns whether the bit was seen; a timeout is not
    itself a failure -- callers always verify with a real re-read afterward."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if _rd(fd, REG_IRQSRC) & bit:
                return True
        except OSError:
            pass
        time.sleep(POLL_SLEEP)
    return False


def _wait_for_spm_write_done(fd, timeout=SPM_WRITE_WAIT_TIMEOUT):
    return _poll_irq_bit(fd, IRQ_SPMWRITE_BIT, timeout)


def _trigger_compensation(fd, timeout=SPM_WRITE_WAIT_TIMEOUT):
    """Write the one whitelisted CompOpMode value that triggers sensor
    compensation, then poll for IrqSrc's compensation-done bit (datasheet
    Table 30's footnote: wait for INTB or 300 ms before any further I2C read
    after an operating-mode-affecting write). Returns whether the flag was
    seen; either way the caller proceeds -- this is post-write housekeeping,
    not something CapMode's own success depends on."""
    _wr(fd, REG_COMPOPMODE, TRIGGER_COMPENSATION)
    return _poll_irq_bit(fd, IRQ_COMP_BIT, timeout)


def apply_capmode(fd):
    """recover(); read block 1 (the sentinel). Then:
      ("already", block)        -- sentinel already netgear; nothing written
      ("unexpected", block)     -- sentinel is neither qsm nor netgear;
                                    refuses to write over a chip state this
                                    module doesn't recognise
      ("applied", block)        -- write verified, compensation triggered
      ("verify-failed", block)  -- the re-read didn't match what was written
      ("write-error", block)    -- the block write itself raised OSError;
                                    block is a fresh re-read taken afterward,
                                    not a retried write
    NEVER retried, NEVER reset on any failure path -- see the module
    docstring's "never soft-reset" rule.
    """
    recover(fd)
    b = read_block1(fd)
    layout = layout_of(b)
    if layout == "netgear":
        return ("already", b)
    if layout != "qsm":
        return ("unexpected", b)
    new = b[0:2] + CAPMODE + b[5:8]
    try:
        _wr_block1(fd, new, b)
    except OSError:
        _wait_for_spm_write_done(fd)
        b2 = read_block1(fd)
        return ("write-error", b2)
    _wait_for_spm_write_done(fd)
    b2 = read_block1(fd)
    if b2 != new:
        return ("verify-failed", b2)
    _trigger_compensation(fd)
    return ("applied", b2)
