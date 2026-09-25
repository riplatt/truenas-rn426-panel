import ast
import importlib.util
import os
import re
import sys
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _REPO_ROOT)
sys.path.insert(0, _HERE)

import rnpanel.sx8635_spm as spm

_TOOL_PATH = os.path.join(_REPO_ROOT, "tools", "sx8635-spm.py")


def _load_tool():
    """sx8635-spm.py has a hyphen in its filename -- load it by path, exactly
    like tests/test_sx8635_watch.py does for the watcher. Its `from rnpanel
    import sx8635_spm as spm` binds the SAME module object this test file
    imports (Python caches modules by name), so monkeypatching spm's
    transport primitives here affects the tool's calls too."""
    _spec = importlib.util.spec_from_file_location("sx8635_spm_tool", _TOOL_PATH)
    m = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(m)
    return m


ZERO_BLOCK = (0, 0, 0, 0, 0, 0, 0, 0)


# --------------------------------------------------------------------------
# Fake transport: stands in for the real i2c-dev/SMBus ioctl layer. Tests
# monkeypatch spm's three I/O primitives (_smbus_write_byte, _smbus_read_byte,
# _smbus_write_i2c_block) plus _funcs onto an instance of this, so the real
# policy code (_wr, _wr_block, recover, read_block/read_block1,
# apply_capsense) runs unmodified against a fake chip -- no fcntl/ctypes
# calls happen at all, which is why this suite can pass on Windows.
#
# The fake chip carries THREE blocks now (bases 0x08/0x10/0x18), not one --
# apply_capsense reads and writes all three.
# --------------------------------------------------------------------------
class FakeTransport(object):
    def __init__(self, block1, block2=ZERO_BLOCK, block3=ZERO_BLOCK,
                 block_write_updates=True, supports_block_write=True,
                 read_byte_raises_at=None, write_byte_raises_at=None,
                 block_write_raises_at=None, ignore_spmcfg_writes=False,
                 signal_write_done=True):
        self.blocks = {0x08: list(block1), 0x10: list(block2), 0x18: list(block3)}
        self.spmcfg = 0
        self.spmbase = 0
        self.window_open = None      # None | "read" | "write" (derived from spmcfg)
        self.window_base = None
        self.spm_write_done = False
        self.comp_done = False
        self.block_write_updates = block_write_updates
        self.supports_block_write = supports_block_write
        self.read_byte_raises_at = read_byte_raises_at
        self.write_byte_raises_at = write_byte_raises_at
        self.block_write_raises_at = block_write_raises_at
        self.ignore_spmcfg_writes = ignore_spmcfg_writes
        self.signal_write_done = signal_write_done
        self.read_calls = 0
        self.write_calls = 0
        self.block_write_calls = 0
        self.log = []   # ("wr", reg, val) / ("rd", reg) / ("block", cmd, tuple(data))

    def write_byte(self, fd, reg, val):
        self.write_calls += 1
        if self.write_byte_raises_at is not None and self.write_calls == self.write_byte_raises_at:
            raise OSError("simulated i2c NAK on write")
        self.log.append(("wr", reg, val))
        if reg == spm.REG_SPMCFG:
            if not self.ignore_spmcfg_writes:
                self.spmcfg = val
            if val == spm.SPM_CLOSED:
                self.window_open = None
            elif val == spm.SPM_READ_OPEN:
                self.window_open = "read"
            elif val == spm.SPM_WRITE_OPEN:
                self.window_open = "write"
        elif reg == spm.REG_SPMBASE:
            self.window_base = val
            self.spmbase = val
        elif reg == spm.REG_COMPOPMODE and val == spm.TRIGGER_COMPENSATION:
            self.comp_done = True

    def read_byte(self, fd, reg):
        self.read_calls += 1
        if self.read_byte_raises_at is not None and self.read_calls == self.read_byte_raises_at:
            raise OSError("simulated i2c NAK on read")
        self.log.append(("rd", reg))
        # The SPM window remaps regs 0x00-0x07 to block data ONLY while open
        # for read at one of the known block bases -- check that first.
        if self.window_open == "read" and self.window_base in self.blocks and 0 <= reg <= 7:
            return self.blocks[self.window_base][reg]
        if reg == spm.REG_SPMCFG:
            return self.spmcfg
        if reg == spm.REG_SPMBASE:
            return self.spmbase
        if reg == spm.REG_IRQSRC:
            bits = 0
            if self.spm_write_done:
                bits |= spm.IRQ_SPMWRITE_BIT
                self.spm_write_done = False   # reading IrqSrc clears it
            if self.comp_done:
                bits |= spm.IRQ_COMP_BIT
                self.comp_done = False
            return bits
        return 0x00

    def write_i2c_block(self, fd, cmd, data):
        self.block_write_calls += 1
        if (self.block_write_raises_at is not None
                and self.block_write_calls == self.block_write_raises_at):
            raise OSError("simulated i2c NAK on block write")
        self.log.append(("block", cmd, tuple(data)))
        if self.window_open == "write" and self.window_base in self.blocks:
            if self.block_write_updates:
                self.blocks[self.window_base] = list(data)
            if self.signal_write_done:
                self.spm_write_done = True

    def funcs(self, fd):
        return spm.I2C_FUNC_SMBUS_WRITE_I2C_BLOCK if self.supports_block_write else 0

    # -- assertion helpers --
    def block(self, base):
        return tuple(self.blocks[base])

    def block_writes(self):
        return [e for e in self.log if e[0] == "block"]

    def spmcfg_writes(self):
        return [e[2] for e in self.log if e[0] == "wr" and e[1] == spm.REG_SPMCFG]

    def spmbase_writes(self):
        return [e[2] for e in self.log if e[0] == "wr" and e[1] == spm.REG_SPMBASE]


class FakeClock(object):
    """Stand-in for the `time` module used inside spm's poll loops. `sleep`
    advances the fake clock instead of actually blocking, so tests never
    incur a real delay even when a wait loop runs to its full timeout."""

    def __init__(self, step=0.005):
        self.t = 0.0
        self.step = step
        self.sleep_calls = 0

    def time(self):
        return self.t

    def sleep(self, secs):
        self.sleep_calls += 1
        self.t += secs


class SpmTransportTestCase(unittest.TestCase):
    """Installs a FakeTransport (and a FakeClock in place of spm.time) onto
    rnpanel.sx8635_spm for the duration of the test, and restores the real
    objects afterward."""

    def setUp(self):
        self._real_write_byte = spm._smbus_write_byte
        self._real_read_byte = spm._smbus_read_byte
        self._real_write_block = spm._smbus_write_i2c_block
        self._real_funcs = spm._funcs
        self._real_time = spm.time
        self.clock = FakeClock()
        spm.time = self.clock

    def tearDown(self):
        spm._smbus_write_byte = self._real_write_byte
        spm._smbus_read_byte = self._real_read_byte
        spm._smbus_write_i2c_block = self._real_write_block
        spm._funcs = self._real_funcs
        spm.time = self._real_time

    def install(self, transport):
        spm._smbus_write_byte = transport.write_byte
        spm._smbus_read_byte = transport.read_byte
        spm._smbus_write_i2c_block = transport.write_i2c_block
        spm._funcs = transport.funcs
        return transport


QSM_BLOCK1 = (0x00, 0x01, 0xFF, 0xF5, 0x55, 0x77, 0x77, 0x77)
UNKNOWN_BLOCK1 = (0x00, 0x01, 0x12, 0x34, 0x56, 0x77, 0x77, 0x77)
NETGEAR_ROW_08 = spm.NETGEAR_ROWS[0x08]
NETGEAR_ROW_10 = spm.NETGEAR_ROWS[0x10]
NETGEAR_ROW_18 = spm.NETGEAR_ROWS[0x18]

# M1: the 24 payload bytes, typed out here rather than read back from
# spm.NETGEAR_ROWS, so a transcription error in the module fails a test
# instead of being written to a tester's chip and then "verified" against
# the same wrong constant. From the stock sx8635.ko .data table
# (sx8635-re-report.md lines 146-149), independently confirmed against the
# tester's 2026-09-24 post-write dump of SPM rows 0x08-0x1F
# (rn316-postwrite-review.md section B.2).
NETGEAR_ROWS_LITERAL = {
    0x08: (0x00, 0x04, 0x0F, 0xFF, 0xF5, 0x75, 0x55, 0x55),
    0x10: (0x55, 0x55, 0x00, 0x80, 0xB0, 0x98, 0x98, 0x98),
    0x18: (0x98, 0x98, 0x98, 0x98, 0x98, 0x98, 0x98, 0x00),
}


class TestNetgearRowsLiteral(unittest.TestCase):
    def test_module_rows_match_the_literal_bytes_exactly(self):
        self.assertEqual(spm.NETGEAR_ROWS, NETGEAR_ROWS_LITERAL)

    def test_block1_row_carries_capmode_at_bytes_2_5(self):
        # The "already" check, layout_of() after an apply, and the daemon's
        # netgear pick all silently depend on this.
        self.assertEqual(NETGEAR_ROWS_LITERAL[0x08][2:5], spm.CAPMODE)

    def test_every_row_is_exactly_eight_bytes(self):
        for base, row in NETGEAR_ROWS_LITERAL.items():
            self.assertEqual(len(row), 8, "row at base 0x%02x is not 8 bytes" % base)


# --------------------------------------------------------------------------
# Whitelists
# --------------------------------------------------------------------------
class TestWhitelists(unittest.TestCase):
    def test_write_whitelist_is_exactly_the_frozen_set(self):
        self.assertEqual(spm.WRITE_WHITELIST, frozenset({
            (0x0D, 0x00), (0x0D, 0x10), (0x0D, 0x18),
            (0x0E, 0x08), (0x0E, 0x10), (0x0E, 0x18),
            (0x09, 0x04),
        }))

    def test_write_bases_is_exactly_netgear_rows_keys(self):
        self.assertEqual(spm.WRITE_BASES, frozenset({0x08, 0x10, 0x18}))
        self.assertNotIn(0x00, spm.WRITE_BASES)

    def test_dump_whitelist_contents(self):
        expected = {(0x0D, 0x00), (0x0D, 0x18)} | {(0x0E, b) for b in range(0x00, 0x80, 8)}
        self.assertEqual(spm.DUMP_WHITELIST, frozenset(expected))

    def test_wr_refuses_outside_whitelist(self):
        for reg, val in [(0x0E, 0xA5), (0x0E, 0x5A), (0x0E, 0x00), (0x0D, 0x30),
                          (0x0A, 0x0F), (0x0B, 0xFF), (0x0C, 0xF5), (0x00, 0x00)]:
            with self.assertRaises(ValueError):
                spm._wr(None, reg, val)

    def test_wr_accepts_every_whitelisted_pair(self):
        t = FakeTransport(block1=ZERO_BLOCK)
        real_write_byte = spm._smbus_write_byte
        spm._smbus_write_byte = t.write_byte
        try:
            for reg, val in spm.WRITE_WHITELIST:
                spm._wr(None, reg, val)   # must not raise
            self.assertEqual(len(t.log), len(spm.WRITE_WHITELIST))
        finally:
            spm._smbus_write_byte = real_write_byte

    def test_wr_dump_refuses_outside_dump_whitelist(self):
        for reg, val in [(0x0D, 0x10), (0x0E, 0xA5), (0x0E, 0x04), (0x09, 0x04)]:
            with self.assertRaises(ValueError):
                spm._wr_dump(None, reg, val)


# --------------------------------------------------------------------------
# _wr_block (payload is always NETGEAR_ROWS[base] -- no caller-supplied
# buffer any more)
# --------------------------------------------------------------------------
class TestWrBlock(SpmTransportTestCase):
    def test_refuses_base_zero(self):
        with self.assertRaises(ValueError):
            spm._wr_block(None, 0x00)

    def test_refuses_base_not_in_write_bases(self):
        for base in (0x00, 0x20, 0x78, 0x01):
            with self.assertRaises(ValueError):
                spm._wr_block(None, base)

    def test_refuses_without_block_write_function_support(self):
        t = self.install(FakeTransport(block1=list(QSM_BLOCK1), supports_block_write=False))
        with self.assertRaises(RuntimeError):
            spm._wr_block(None, 0x08)
        self.assertEqual(t.block_writes(), [])

    def test_refuses_when_readback_wrong(self):
        # The chip silently ignores the SpmCfg write (stuck at whatever it
        # was) -- _wr_block must catch this via its own readback, not trust
        # that the write succeeded just because it didn't raise.
        t = self.install(FakeTransport(block1=list(QSM_BLOCK1), ignore_spmcfg_writes=True))
        with self.assertRaises(RuntimeError):
            spm._wr_block(None, 0x08)
        self.assertEqual(t.block_writes(), [])
        # still attempts to close, even though the open didn't verify
        self.assertEqual(t.spmcfg_writes()[-1], spm.SPM_CLOSED)

    def test_accepts_valid_base_self_contained_open_base_close(self):
        for base, row in ((0x08, NETGEAR_ROW_08), (0x10, NETGEAR_ROW_10), (0x18, NETGEAR_ROW_18)):
            t = self.install(FakeTransport(block1=list(QSM_BLOCK1)))
            spm._wr_block(None, base)
            self.assertEqual(len(t.block_writes()), 1)
            self.assertEqual(t.block_writes()[0][1], 0x00)
            # the payload written equals NETGEAR_ROWS[base] exactly
            self.assertEqual(t.block_writes()[0][2], row)
            # _wr_block opened for write, set the base, read both back,
            # wrote, and closed -- entirely on its own.
            self.assertEqual(t.spmcfg_writes(), [spm.SPM_WRITE_OPEN, spm.SPM_CLOSED])
            self.assertEqual(t.spmbase_writes(), [base])

    def test_closes_window_even_when_block_write_raises(self):
        t = self.install(FakeTransport(block1=list(QSM_BLOCK1), block_write_raises_at=1))
        with self.assertRaises(OSError):
            spm._wr_block(None, 0x08)
        self.assertEqual(t.spmcfg_writes(), [spm.SPM_WRITE_OPEN, spm.SPM_CLOSED])

    def test_refuses_payload_not_eight_bytes(self):
        # M1: a transcription error that shortens/lengthens a row must fail
        # loudly, not get silently mis-packed into the 34-byte ioctl buffer.
        real_row = spm.NETGEAR_ROWS[0x08]
        spm.NETGEAR_ROWS[0x08] = real_row[:7]
        try:
            with self.assertRaises(ValueError):
                spm._wr_block(None, 0x08)
        finally:
            spm.NETGEAR_ROWS[0x08] = real_row


# --------------------------------------------------------------------------
# layout_of
# --------------------------------------------------------------------------
class TestLayoutOf(unittest.TestCase):
    def test_netgear(self):
        self.assertEqual(spm.layout_of(NETGEAR_ROW_08), "netgear")

    def test_qsm(self):
        self.assertEqual(spm.layout_of(QSM_BLOCK1), "qsm")

    def test_unknown(self):
        self.assertEqual(spm.layout_of(UNKNOWN_BLOCK1), "unknown")


# --------------------------------------------------------------------------
# read_block / read_block1 (M3: try opens immediately after the 0x0D write)
# --------------------------------------------------------------------------
class TestReadBlock(SpmTransportTestCase):
    def test_normal_read_closes_window(self):
        t = self.install(FakeTransport(block1=(1, 2, 3, 4, 5, 6, 7, 8)))
        result = spm.read_block1(None)
        self.assertEqual(result, (1, 2, 3, 4, 5, 6, 7, 8))
        self.assertEqual(t.spmcfg_writes()[-1], spm.SPM_CLOSED)

    def test_read_block_at_other_bases(self):
        t = self.install(FakeTransport(block1=QSM_BLOCK1, block2=NETGEAR_ROW_10, block3=NETGEAR_ROW_18))
        self.assertEqual(spm.read_block(None, 0x10), NETGEAR_ROW_10)
        self.assertEqual(spm.read_block(None, 0x18), NETGEAR_ROW_18)

    def test_read_block_refuses_base_zero_and_leaves_window_closed(self):
        # S5: base 0x00 is block 0 (I2CAddress) -- read_block() must refuse
        # it via _wr()'s whitelist, not silently open block 0's read window.
        t = self.install(FakeTransport(block1=list(QSM_BLOCK1)))
        with self.assertRaises(ValueError):
            spm.read_block(None, 0x00)
        self.assertEqual(t.spmcfg_writes()[-1], spm.SPM_CLOSED)

    def test_exception_mid_data_read_still_closes_window(self):
        # Raise on the 3rd transport read call (the 1st and 2nd real chip
        # reads inside read_block1's loop succeed, the 3rd raises).
        t = self.install(FakeTransport(block1=(1, 2, 3, 4, 5, 6, 7, 8),
                                        read_byte_raises_at=3))
        with self.assertRaises(OSError):
            spm.read_block1(None)
        self.assertEqual(t.spmcfg_writes()[-1], spm.SPM_CLOSED)
        self.assertEqual(t.spmcfg_writes(), [spm.SPM_READ_OPEN, spm.SPM_CLOSED])


# --------------------------------------------------------------------------
# S7 item 3: window-leak tests -- a write failing partway through must still
# close the window (and, for the block write, still verify).
# --------------------------------------------------------------------------
class TestWindowLeaksOnWriteFailure(SpmTransportTestCase):
    def test_spmbase_write_fails_during_read_block1(self):
        # 1st write_byte call = SpmCfg<-READ_OPEN (succeeds), 2nd = the base
        # write (fails).
        t = self.install(FakeTransport(block1=(1, 2, 3, 4, 5, 6, 7, 8),
                                        write_byte_raises_at=2))
        with self.assertRaises(OSError):
            spm.read_block1(None)
        self.assertEqual(t.spmcfg_writes()[-1], spm.SPM_CLOSED)

    def test_spmbase_write_fails_during_wr_block_open(self):
        # Inside _wr_block: 1st write_byte call = SpmCfg<-WRITE_OPEN
        # (succeeds), 2nd = the base write (fails).
        t = self.install(FakeTransport(block1=list(QSM_BLOCK1), write_byte_raises_at=2))
        with self.assertRaises(OSError):
            spm._wr_block(None, 0x08)
        self.assertEqual(t.spmcfg_writes()[-1], spm.SPM_CLOSED)
        self.assertEqual(t.block_writes(), [])

    def test_first_block_write_raising_still_verifies_and_reports_write_error(self):
        # S6: apply_capsense catches an OSError from a block write, still
        # waits, and still performs the verify re-read of all three blocks.
        t = self.install(FakeTransport(block1=list(QSM_BLOCK1), block_write_raises_at=1))
        result, rows = spm.apply_capsense(None)
        self.assertEqual(result, "write-error")
        # nothing landed: all three blocks unchanged from the fake's initial state
        self.assertEqual(rows, (tuple(QSM_BLOCK1), ZERO_BLOCK, ZERO_BLOCK))
        self.assertEqual(t.block_writes(), [])
        self.assertEqual(t.spmcfg_writes()[-1], spm.SPM_CLOSED)
        # the verify read is a full read_block sequence: at least one more
        # READ_OPEN/CLOSED pair happened after the failed write attempt.
        self.assertGreaterEqual(t.spmcfg_writes().count(spm.SPM_READ_OPEN), 1)

    def test_second_block_write_raising_stops_after_first_succeeds(self):
        # Write order is 0x18, 0x10, 0x08 -- a NAK on the SECOND block write
        # (0x10) must still verify and report write-error, without ever
        # attempting the third (CapMode, 0x08).
        t = self.install(FakeTransport(block1=list(QSM_BLOCK1), block_write_raises_at=2))
        result, rows = spm.apply_capsense(None)
        self.assertEqual(result, "write-error")
        self.assertEqual(len(t.block_writes()), 1)   # only 0x18 landed
        self.assertEqual(t.block_writes()[0][2], NETGEAR_ROW_18)
        b8, b10, b18 = rows
        self.assertEqual(b18, NETGEAR_ROW_18)        # 0x18 wrote through
        self.assertEqual(b10, ZERO_BLOCK)             # 0x10 never attempted
        self.assertEqual(b8, tuple(QSM_BLOCK1))       # CapMode (last) never attempted

    def test_third_block_write_raising_leaves_capmode_still_qsm(self):
        # S5: a NAK on the THIRD (CapMode, base 0x08) block write -- the
        # documented re-runnable state: 0x18 and 0x10 landed, CapMode was
        # never attempted, so layout_of still reads "qsm" and a re-run
        # rewrites all three cleanly.
        t = self.install(FakeTransport(block1=list(QSM_BLOCK1), block_write_raises_at=3))
        result, rows = spm.apply_capsense(None)
        self.assertEqual(result, "write-error")
        self.assertEqual(len(t.block_writes()), 2)   # 0x18 and 0x10 landed
        b8, b10, b18 = rows
        self.assertEqual(b18, NETGEAR_ROW_18)
        self.assertEqual(b10, NETGEAR_ROW_10)
        self.assertEqual(b8, tuple(QSM_BLOCK1))
        self.assertEqual(spm.layout_of(b8), "qsm")


# --------------------------------------------------------------------------
# apply_capsense
# --------------------------------------------------------------------------
class TestApplyCapsense(SpmTransportTestCase):
    def test_already_all_three_match_zero_block_writes(self):
        t = self.install(FakeTransport(block1=NETGEAR_ROW_08, block2=NETGEAR_ROW_10, block3=NETGEAR_ROW_18))
        result, rows = spm.apply_capsense(None)
        self.assertEqual(result, "already")
        self.assertEqual(rows, (NETGEAR_ROW_08, NETGEAR_ROW_10, NETGEAR_ROW_18))
        self.assertEqual(t.block_writes(), [])

    def test_capmode_only_writes_nothing(self):
        # CapMode (block 1) already reads netgear, but blocks 2/3 don't
        # match -- a hybrid state from some other write. Never "topped up".
        t = self.install(FakeTransport(block1=NETGEAR_ROW_08, block2=ZERO_BLOCK, block3=ZERO_BLOCK))
        result, rows = spm.apply_capsense(None)
        self.assertEqual(result, "capmode-only")
        self.assertEqual(rows, (NETGEAR_ROW_08, ZERO_BLOCK, ZERO_BLOCK))
        self.assertEqual(t.block_writes(), [])
        self.assertNotIn(spm.SPM_WRITE_OPEN, t.spmcfg_writes())

    def test_unexpected_sentinel_zero_block_writes_zero_write_open(self):
        t = self.install(FakeTransport(block1=UNKNOWN_BLOCK1))
        result, b = spm.apply_capsense(None)
        self.assertEqual(result, "unexpected")
        self.assertEqual(b, UNKNOWN_BLOCK1)
        self.assertEqual(t.block_writes(), [])
        self.assertNotIn(spm.SPM_WRITE_OPEN, t.spmcfg_writes())

    def test_qsm_three_block_writes_in_order_correct_bytes_window_closed(self):
        t = self.install(FakeTransport(block1=QSM_BLOCK1))
        result, rows = spm.apply_capsense(None)
        self.assertEqual(result, "applied")
        self.assertEqual(rows, (NETGEAR_ROW_08, NETGEAR_ROW_10, NETGEAR_ROW_18))

        writes = t.block_writes()
        self.assertEqual(len(writes), 3)
        self.assertTrue(all(w[1] == 0x00 for w in writes))
        # write order 0x18, 0x10, 0x08 -- CapMode (0x08) last
        self.assertEqual([w[2] for w in writes], [NETGEAR_ROW_18, NETGEAR_ROW_10, NETGEAR_ROW_08])
        # payload bytes written equal NETGEAR_ROWS exactly
        self.assertEqual(writes[0][2], spm.NETGEAR_ROWS[0x18])
        self.assertEqual(writes[1][2], spm.NETGEAR_ROWS[0x10])
        self.assertEqual(writes[2][2], spm.NETGEAR_ROWS[0x08])

        self.assertEqual(t.spmcfg_writes()[-1], spm.SPM_CLOSED)
        self.assertEqual(sorted(set(t.spmbase_writes())), [0x08, 0x10, 0x18])

    def test_verify_mismatch_no_retry_no_further_writes(self):
        t = self.install(FakeTransport(block1=QSM_BLOCK1, block_write_updates=False))
        result, rows = spm.apply_capsense(None)
        self.assertEqual(result, "verify-failed")
        self.assertEqual(rows, (tuple(QSM_BLOCK1), ZERO_BLOCK, ZERO_BLOCK))

        self.assertEqual(len(t.block_writes()), 3)   # all three attempted, none landed
        self.assertNotIn(("wr", spm.REG_COMPOPMODE, spm.TRIGGER_COMPENSATION), t.log)
        self.assertEqual(t.spmcfg_writes()[-1], spm.SPM_CLOSED)

    def test_timeout_on_wait_does_not_prevent_a_correct_result(self):
        # The chip's IRQ flag never fires (simulating a missed/late
        # interrupt), but the block writes DID land -- apply_capsense must
        # still time out gracefully and let the verify read decide the
        # outcome, not treat the timeout itself as failure.
        t = self.install(FakeTransport(block1=QSM_BLOCK1, signal_write_done=False))
        result, rows = spm.apply_capsense(None)
        self.assertEqual(result, "applied")
        self.assertEqual(rows, (NETGEAR_ROW_08, NETGEAR_ROW_10, NETGEAR_ROW_18))
        # the wait loop actually ran to its timeout (proof this exercised
        # the timeout path, not an immediate hit) -- using the fake clock,
        # so no real delay occurred.
        self.assertGreater(self.clock.sleep_calls, 0)

    def test_never_writes_outside_whitelist(self):
        t = self.install(FakeTransport(block1=QSM_BLOCK1))
        spm.apply_capsense(None)
        for e in t.log:
            if e[0] == "wr":
                self.assertIn((e[1], e[2]), spm.WRITE_WHITELIST)

    def test_golden_ordered_sequence_for_qsm_path(self):
        """The write, specified (Q1, updated for the three NETGEAR rows):
        assert the transport log is exactly this sequence, in order, for a
        clean QSM->netgear apply."""
        t = self.install(FakeTransport(block1=QSM_BLOCK1))
        result, rows = spm.apply_capsense(None)
        self.assertEqual(result, "applied")

        expected = (
            [("rd", 0x0D)]                                              # recover()
            + [("wr", 0x0D, 0x18), ("wr", 0x0E, 0x08)]                   # sentinel read_block(0x08) open
            + [("rd", i) for i in range(8)]                              # sentinel data
            + [("wr", 0x0D, 0x00)]                                       # sentinel close
        )
        for base in (0x18, 0x10, 0x08):                                  # write order
            expected += (
                [("wr", 0x0D, 0x10), ("wr", 0x0E, base),                 # _wr_block open+base
                 ("rd", 0x0D), ("rd", 0x0E)]                              # _wr_block readback
                + [("block", 0x00, NETGEAR_ROWS_LITERAL[base])]          # the write (literal, not spm.NETGEAR_ROWS)
                + [("wr", 0x0D, 0x00)]                                   # _wr_block close
                + [("rd", 0x00)]                                         # wait for SPM write done
            )
        for base in (0x08, 0x10, 0x18):                                  # verify, address order
            expected += (
                [("wr", 0x0D, 0x18), ("wr", 0x0E, base)]                 # verify read_block open
                + [("rd", i) for i in range(8)]                          # verify data
                + [("wr", 0x0D, 0x00)]                                   # verify close
            )
        expected += [("wr", 0x09, 0x04)]                                 # compensation trigger
        expected += [("rd", 0x00)]                                       # compensation poll

        self.assertEqual(t.log, expected)


# --------------------------------------------------------------------------
# M2: apply_capsense's optional `note` callback -- the per-block
# SPM-write-done outcome and the compensation-flag outcome, which the return
# value alone discards.
# --------------------------------------------------------------------------
class TestApplyCapsenseNote(SpmTransportTestCase):
    def test_note_receives_three_block_outcomes_and_compensation_in_order(self):
        self.install(FakeTransport(block1=QSM_BLOCK1))
        notes = []
        result, rows = spm.apply_capsense(None, note=notes.append)
        self.assertEqual(result, "applied")
        self.assertEqual(len(notes), 4)
        self.assertIn("0x18", notes[0])
        self.assertIn("0x10", notes[1])
        self.assertIn("0x08", notes[2])
        self.assertIn("compensation-done", notes[3])
        self.assertTrue(all("seen" in n and "not seen" not in n for n in notes))

    def test_note_reports_not_seen_on_write_done_timeout(self):
        # signal_write_done=False only withholds the SPM-write-done flag;
        # the block writes still land (block_write_updates defaults True),
        # so the compensation flag (set independently by the fake on the
        # 0x09<-0x04 write) is unaffected -- check the three block notes only.
        self.install(FakeTransport(block1=QSM_BLOCK1, signal_write_done=False))
        notes = []
        result, rows = spm.apply_capsense(None, note=notes.append)
        self.assertEqual(result, "applied")
        self.assertEqual(len(notes), 4)
        self.assertTrue(all("not seen in 300 ms" in n for n in notes[:3]))

    def test_note_called_on_write_error_too(self):
        # Write order 0x18,0x10,0x08 -- a NAK on the second block write still
        # gets its own wait-outcome note (the run stops right after).
        self.install(FakeTransport(block1=QSM_BLOCK1, block_write_raises_at=2))
        notes = []
        result, rows = spm.apply_capsense(None, note=notes.append)
        self.assertEqual(result, "write-error")
        self.assertEqual(len(notes), 2)
        self.assertIn("0x18", notes[0])
        self.assertIn("seen", notes[0])
        self.assertNotIn("not seen", notes[0])
        self.assertIn("0x10", notes[1])
        self.assertIn("not seen in 300 ms", notes[1])   # the failed block never set the flag

    def test_note_defaults_to_a_noop(self):
        self.install(FakeTransport(block1=QSM_BLOCK1))
        result, rows = spm.apply_capsense(None)   # no note= -- must not raise
        self.assertEqual(result, "applied")


# --------------------------------------------------------------------------
# S7 item 2: static (AST) choke-point proofs.
# --------------------------------------------------------------------------
class TestASTChokePoints(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(spm.__file__) as f:
            cls.src = f.read()
        cls.tree = ast.parse(cls.src)
        cls.functions = {n.name: n for n in cls.tree.body if isinstance(n, ast.FunctionDef)}

    @staticmethod
    def _calls_fcntl_ioctl(node):
        for n in ast.walk(node):
            if (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                    and n.func.attr == "ioctl" and isinstance(n.func.value, ast.Name)
                    and n.func.value.id == "fcntl"):
                return True
        return False

    @staticmethod
    def _calls_name(node, name):
        for n in ast.walk(node):
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == name:
                return True
        return False

    @staticmethod
    def _references_name(node, name):
        for n in ast.walk(node):
            if isinstance(n, ast.Name) and n.id == name:
                return True
        return False

    def test_fcntl_ioctl_only_inside_the_four_transport_primitives(self):
        allowed = {"_smbus_write_byte", "_smbus_read_byte", "_smbus_write_i2c_block", "_funcs"}
        for name, node in self.functions.items():
            calls_it = self._calls_fcntl_ioctl(node)
            if name in allowed:
                self.assertTrue(calls_it, "%s was expected to call fcntl.ioctl" % name)
            else:
                self.assertFalse(calls_it, "%s calls fcntl.ioctl directly" % name)

    def test_smbus_write_byte_only_called_from_wr_and_wr_dump(self):
        allowed = {"_wr", "_wr_dump"}
        for name, node in self.functions.items():
            calls_it = self._calls_name(node, "_smbus_write_byte")
            if name in allowed:
                self.assertTrue(calls_it, "%s was expected to call _smbus_write_byte" % name)
            else:
                self.assertFalse(calls_it, "%s calls _smbus_write_byte" % name)

    def test_smbus_write_i2c_block_only_called_from_wr_block(self):
        for name, node in self.functions.items():
            calls_it = self._calls_name(node, "_smbus_write_i2c_block")
            if name == "_wr_block":
                self.assertTrue(calls_it)
            else:
                self.assertFalse(calls_it, "%s calls _smbus_write_i2c_block" % name)

    def test_wr_dump_and_dump_whitelist_unreachable_from_other_functions(self):
        for name, node in self.functions.items():
            if name == "_wr_dump":
                continue
            self.assertFalse(self._references_name(node, "_wr_dump"),
                              "%s references _wr_dump" % name)
            self.assertFalse(self._references_name(node, "DUMP_WHITELIST"),
                              "%s references DUMP_WHITELIST" % name)


# --------------------------------------------------------------------------
# Source scans (S8: extended to the tool file too)
# --------------------------------------------------------------------------
class TestSourceScans(unittest.TestCase):
    _FORBIDDEN = re.compile(r'0x(ac|ad|b1|a5|5a|de)\b', re.IGNORECASE)

    def test_no_forbidden_hex_literals_in_spm_module(self):
        with open(spm.__file__) as f:
            src = f.read()
        match = self._FORBIDDEN.search(src)
        self.assertIsNone(match, "forbidden literal found: %r" % (match.group(0) if match else None))

    def test_no_forbidden_hex_literals_in_tool(self):
        with open(_TOOL_PATH) as f:
            src = f.read()
        match = self._FORBIDDEN.search(src)
        self.assertIsNone(match, "forbidden literal found: %r" % (match.group(0) if match else None))


# --------------------------------------------------------------------------
# S7 item 7: tool tests, loading tools/sx8635-spm.py by path.
# --------------------------------------------------------------------------
class TestToolCLI(SpmTransportTestCase):
    @classmethod
    def setUpClass(cls):
        cls.tool = _load_tool()

    def setUp(self):
        super(TestToolCLI, self).setUp()
        self.tool.OUT_LINES = []

    def test_capsense_without_yes_writes_nothing(self):
        t = self.install(FakeTransport(block1=list(QSM_BLOCK1)))
        rc = self.tool.cmd_capsense(None, False)
        self.assertEqual(rc, 0)
        self.assertEqual(t.log, [])

    def test_capsense_dry_run_prints_the_three_rows(self):
        t = self.install(FakeTransport(block1=list(QSM_BLOCK1)))
        self.tool.cmd_capsense(None, False)
        joined = "\n".join(self.tool.OUT_LINES)
        for row in (NETGEAR_ROW_08, NETGEAR_ROW_10, NETGEAR_ROW_18):
            self.assertIn(" ".join("%02x" % b for b in row), joined)

    def test_capsense_yes_applied_exits_zero(self):
        t = self.install(FakeTransport(block1=list(QSM_BLOCK1)))
        rc = self.tool.cmd_capsense(None, True)
        self.assertEqual(rc, 0)
        self.assertIn("result: applied", self.tool.OUT_LINES)

    def test_capsense_yes_write_error_exits_one(self):
        t = self.install(FakeTransport(block1=list(QSM_BLOCK1), block_write_raises_at=1))
        rc = self.tool.cmd_capsense(None, True)
        self.assertEqual(rc, 1)
        self.assertIn("result: write-error", self.tool.OUT_LINES)

    def test_capsense_yes_already_exits_zero(self):
        t = self.install(FakeTransport(block1=NETGEAR_ROW_08, block2=NETGEAR_ROW_10, block3=NETGEAR_ROW_18))
        rc = self.tool.cmd_capsense(None, True)
        self.assertEqual(rc, 0)
        self.assertIn("result: already", self.tool.OUT_LINES)
        joined = "\n".join(self.tool.OUT_LINES)
        self.assertIn("nothing was written", joined)

    def test_capsense_yes_capmode_only_exits_zero(self):
        t = self.install(FakeTransport(block1=NETGEAR_ROW_08, block2=ZERO_BLOCK, block3=ZERO_BLOCK))
        rc = self.tool.cmd_capsense(None, True)
        self.assertEqual(rc, 0)
        self.assertIn("result: capmode-only", self.tool.OUT_LINES)

    def test_capsense_yes_unexpected_exits_one_and_still_dumps(self):
        t = self.install(FakeTransport(block1=UNKNOWN_BLOCK1))
        rc = self.tool.cmd_capsense(None, True)
        self.assertEqual(rc, 1)
        self.assertIn("result: unexpected", self.tool.OUT_LINES)
        self.assertIn("\n=== full SPM dump after capsense ===", self.tool.OUT_LINES)

    def test_capsense_yes_verify_failed_exits_one(self):
        t = self.install(FakeTransport(block1=QSM_BLOCK1, block_write_updates=False))
        rc = self.tool.cmd_capsense(None, True)
        self.assertEqual(rc, 1)
        self.assertIn("result: verify-failed", self.tool.OUT_LINES)

    def test_capsense_yes_failure_next_steps_mention_the_issue(self):
        t = self.install(FakeTransport(block1=QSM_BLOCK1, block_write_updates=False))
        self.tool.cmd_capsense(None, True)
        joined = "\n".join(self.tool.OUT_LINES)
        self.assertIn("issue #2", joined)
        self.assertNotIn("--tap to confirm", joined)

    def test_read_all_spm_never_opens_write_direction(self):
        data = [i & 0xFF for i in range(128)]

        class DumpTransport(FakeTransport):
            def read_byte(self, fd, reg):
                self.read_calls += 1
                self.log.append(("rd", reg))
                if self.window_open == "read" and 0 <= reg <= 7:
                    return data[self.window_base + reg]
                return 0x00

        t = self.install(DumpTransport(block1=list(QSM_BLOCK1)))
        result = self.tool.read_all_spm(None)
        self.assertEqual(result, data)
        self.assertNotIn(spm.SPM_WRITE_OPEN, t.spmcfg_writes())
        self.assertEqual(t.spmcfg_writes()[-1], spm.SPM_CLOSED)

    def test_read_all_spm_closes_on_mid_read_exception(self):
        t = self.install(FakeTransport(block1=list(QSM_BLOCK1), read_byte_raises_at=5))
        with self.assertRaises(OSError):
            self.tool.read_all_spm(None)
        self.assertNotIn(spm.SPM_WRITE_OPEN, t.spmcfg_writes())
        self.assertEqual(t.spmcfg_writes()[-1], spm.SPM_CLOSED)

    def test_tool_source_has_no_smbus_identifier(self):
        with open(_TOOL_PATH) as f:
            src = f.read()
        self.assertNotIn("_smbus_", src)

    def test_tool_source_has_no_capmode_subcommand(self):
        with open(_TOOL_PATH) as f:
            src = f.read()
        self.assertNotIn('"capmode":', src)


if __name__ == "__main__":
    unittest.main()
