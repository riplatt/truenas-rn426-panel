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


# --------------------------------------------------------------------------
# Fake transport: stands in for the real i2c-dev/SMBus ioctl layer. Tests
# monkeypatch spm's three I/O primitives (_smbus_write_byte, _smbus_read_byte,
# _smbus_write_i2c_block) plus _funcs onto an instance of this, so the real
# policy code (_wr, _wr_block1, recover, read_block1, apply_capmode) runs
# unmodified against a fake chip -- no fcntl/ctypes calls happen at all,
# which is why this suite can pass on Windows.
# --------------------------------------------------------------------------
class FakeTransport(object):
    def __init__(self, block1, block_write_updates=True, supports_block_write=True,
                 read_byte_raises_at=None, write_byte_raises_at=None,
                 block_write_raises=False, ignore_spmcfg_writes=False,
                 signal_write_done=True):
        self.block1 = list(block1)
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
        self.block_write_raises = block_write_raises
        self.ignore_spmcfg_writes = ignore_spmcfg_writes
        self.signal_write_done = signal_write_done
        self.read_calls = 0
        self.write_calls = 0
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
        # for read at the block-1 base -- check that first.
        if self.window_open == "read" and self.window_base == spm.BLOCK1_BASE and 0 <= reg <= 7:
            return self.block1[reg]
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
        if self.block_write_raises:
            raise OSError("simulated i2c NAK on block write")
        self.log.append(("block", cmd, tuple(data)))
        if self.window_open == "write" and self.window_base == spm.BLOCK1_BASE:
            if self.block_write_updates:
                self.block1 = list(data)
            if self.signal_write_done:
                self.spm_write_done = True

    def funcs(self, fd):
        return spm.I2C_FUNC_SMBUS_WRITE_I2C_BLOCK if self.supports_block_write else 0

    # -- assertion helpers --
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
NETGEAR_BLOCK1 = (0x00, 0x01) + spm.CAPMODE + (0x77, 0x77, 0x77)
UNKNOWN_BLOCK1 = (0x00, 0x01, 0x12, 0x34, 0x56, 0x77, 0x77, 0x77)


# --------------------------------------------------------------------------
# Whitelists
# --------------------------------------------------------------------------
class TestWhitelists(unittest.TestCase):
    def test_write_whitelist_is_exactly_the_frozen_set(self):
        self.assertEqual(spm.WRITE_WHITELIST, frozenset({
            (0x0D, 0x00), (0x0D, 0x10), (0x0D, 0x18), (0x0E, 0x08), (0x09, 0x04),
        }))

    def test_dump_whitelist_contents(self):
        expected = {(0x0D, 0x00), (0x0D, 0x18)} | {(0x0E, b) for b in range(0x00, 0x80, 8)}
        self.assertEqual(spm.DUMP_WHITELIST, frozenset(expected))

    def test_wr_refuses_outside_whitelist(self):
        for reg, val in [(0x0E, 0xA5), (0x0E, 0x5A), (0x0E, 0x00), (0x0D, 0x30),
                          (0x0A, 0x0F), (0x0B, 0xFF), (0x0C, 0xF5), (0x00, 0x00)]:
            with self.assertRaises(ValueError):
                spm._wr(None, reg, val)

    def test_wr_accepts_every_whitelisted_pair(self):
        t = FakeTransport(block1=(0, 0, 0, 0, 0, 0, 0, 0))
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
# _wr_block1 (M1: now self-contained -- opens, sets base, reads back, writes,
# always closes)
# --------------------------------------------------------------------------
class TestWrBlock1(SpmTransportTestCase):
    SENTINEL = QSM_BLOCK1

    def _valid_buf(self):
        return self.SENTINEL[0:2] + spm.CAPMODE + self.SENTINEL[5:8]

    def test_refuses_wrong_length(self):
        buf = self.SENTINEL[0:2] + spm.CAPMODE + self.SENTINEL[5:7]   # only 7 bytes
        with self.assertRaises(ValueError):
            spm._wr_block1(None, buf, self.SENTINEL)

    def test_refuses_wrong_capmode(self):
        buf = self.SENTINEL[0:2] + (0xFF, 0xF5, 0x55) + self.SENTINEL[5:8]
        with self.assertRaises(ValueError):
            spm._wr_block1(None, buf, self.SENTINEL)

    def test_refuses_change_to_other_five_bytes(self):
        buf = (0x99, 0x01) + spm.CAPMODE + self.SENTINEL[5:8]   # byte 0 changed
        with self.assertRaises(ValueError):
            spm._wr_block1(None, buf, self.SENTINEL)

        buf2 = self.SENTINEL[0:2] + spm.CAPMODE + (0x00, 0x77, 0x77)   # byte 5 changed
        with self.assertRaises(ValueError):
            spm._wr_block1(None, buf2, self.SENTINEL)

    def test_refuses_without_block_write_function_support(self):
        t = self.install(FakeTransport(block1=list(self.SENTINEL), supports_block_write=False))
        with self.assertRaises(RuntimeError):
            spm._wr_block1(None, self._valid_buf(), self.SENTINEL)
        self.assertEqual(t.block_writes(), [])

    def test_refuses_when_readback_wrong(self):
        # The chip silently ignores the SpmCfg write (stuck at whatever it
        # was) -- _wr_block1 must catch this via its own readback, not trust
        # that the write succeeded just because it didn't raise.
        t = self.install(FakeTransport(block1=list(self.SENTINEL), ignore_spmcfg_writes=True))
        with self.assertRaises(RuntimeError):
            spm._wr_block1(None, self._valid_buf(), self.SENTINEL)
        self.assertEqual(t.block_writes(), [])
        # still attempts to close, even though the open didn't verify
        self.assertEqual(t.spmcfg_writes()[-1], spm.SPM_CLOSED)

    def test_accepts_valid_buffer_self_contained_open_base_close(self):
        t = self.install(FakeTransport(block1=list(self.SENTINEL)))
        buf = self._valid_buf()
        spm._wr_block1(None, buf, self.SENTINEL)
        self.assertEqual(len(t.block_writes()), 1)
        self.assertEqual(t.block_writes()[0][1], 0x00)
        self.assertEqual(t.block_writes()[0][2], buf)
        # _wr_block1 opened for write, set base 0x08, read both back, wrote,
        # and closed -- entirely on its own.
        self.assertEqual(t.spmcfg_writes(), [spm.SPM_WRITE_OPEN, spm.SPM_CLOSED])
        self.assertEqual(t.spmbase_writes(), [spm.BLOCK1_BASE])

    def test_closes_window_even_when_block_write_raises(self):
        t = self.install(FakeTransport(block1=list(self.SENTINEL), block_write_raises=True))
        with self.assertRaises(OSError):
            spm._wr_block1(None, self._valid_buf(), self.SENTINEL)
        self.assertEqual(t.spmcfg_writes(), [spm.SPM_WRITE_OPEN, spm.SPM_CLOSED])


# --------------------------------------------------------------------------
# layout_of
# --------------------------------------------------------------------------
class TestLayoutOf(unittest.TestCase):
    def test_netgear(self):
        self.assertEqual(spm.layout_of(NETGEAR_BLOCK1), "netgear")

    def test_qsm(self):
        self.assertEqual(spm.layout_of(QSM_BLOCK1), "qsm")

    def test_unknown(self):
        self.assertEqual(spm.layout_of(UNKNOWN_BLOCK1), "unknown")


# --------------------------------------------------------------------------
# read_block1 (M3: try opens immediately after the 0x0D write)
# --------------------------------------------------------------------------
class TestReadBlock1(SpmTransportTestCase):
    def test_normal_read_closes_window(self):
        t = self.install(FakeTransport(block1=(1, 2, 3, 4, 5, 6, 7, 8)))
        result = spm.read_block1(None)
        self.assertEqual(result, (1, 2, 3, 4, 5, 6, 7, 8))
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

    def test_spmbase_write_fails_during_block1_open(self):
        # Inside _wr_block1: 1st write_byte call = SpmCfg<-WRITE_OPEN
        # (succeeds), 2nd = the base write (fails).
        t = self.install(FakeTransport(block1=list(QSM_BLOCK1), write_byte_raises_at=2))
        buf = QSM_BLOCK1[0:2] + spm.CAPMODE + QSM_BLOCK1[5:8]
        with self.assertRaises(OSError):
            spm._wr_block1(None, buf, QSM_BLOCK1)
        self.assertEqual(t.spmcfg_writes()[-1], spm.SPM_CLOSED)
        self.assertEqual(t.block_writes(), [])

    def test_block_write_itself_raising_still_verifies(self):
        # S6: apply_capmode catches an OSError from the block write, still
        # waits, and still performs the verify read.
        t = self.install(FakeTransport(block1=list(QSM_BLOCK1), block_write_raises=True))
        result, b2 = spm.apply_capmode(None)
        self.assertEqual(result, "write-error")
        self.assertEqual(b2, QSM_BLOCK1)   # unchanged: block write never landed
        self.assertEqual(t.block_writes(), [])
        self.assertEqual(t.spmcfg_writes()[-1], spm.SPM_CLOSED)
        # the verify read is a full read_block1 sequence: at least one more
        # READ_OPEN/CLOSED pair happened after the failed write attempt.
        self.assertGreaterEqual(t.spmcfg_writes().count(spm.SPM_READ_OPEN), 1)


# --------------------------------------------------------------------------
# apply_capmode
# --------------------------------------------------------------------------
class TestApplyCapmode(SpmTransportTestCase):
    def test_already_netgear_zero_block_writes(self):
        t = self.install(FakeTransport(block1=NETGEAR_BLOCK1))
        result, b = spm.apply_capmode(None)
        self.assertEqual(result, "already")
        self.assertEqual(b, NETGEAR_BLOCK1)
        self.assertEqual(t.block_writes(), [])

    def test_unexpected_sentinel_zero_block_writes_zero_write_open(self):
        t = self.install(FakeTransport(block1=UNKNOWN_BLOCK1))
        result, b = spm.apply_capmode(None)
        self.assertEqual(result, "unexpected")
        self.assertEqual(b, UNKNOWN_BLOCK1)
        self.assertEqual(t.block_writes(), [])
        self.assertNotIn(spm.SPM_WRITE_OPEN, t.spmcfg_writes())

    def test_qsm_exactly_one_block_write_correct_bytes_window_closed(self):
        t = self.install(FakeTransport(block1=QSM_BLOCK1))
        result, b = spm.apply_capmode(None)
        self.assertEqual(result, "applied")
        expected_new = QSM_BLOCK1[0:2] + spm.CAPMODE + QSM_BLOCK1[5:8]
        self.assertEqual(b, expected_new)

        writes = t.block_writes()
        self.assertEqual(len(writes), 1)
        self.assertEqual(writes[0][1], 0x00)
        self.assertEqual(writes[0][2], expected_new)

        self.assertEqual(t.spmcfg_writes()[-1], spm.SPM_CLOSED)
        self.assertTrue(all(v == spm.BLOCK1_BASE for v in t.spmbase_writes()))

    def test_verify_mismatch_no_retry_no_further_writes(self):
        t = self.install(FakeTransport(block1=QSM_BLOCK1, block_write_updates=False))
        result, b = spm.apply_capmode(None)
        self.assertEqual(result, "verify-failed")
        self.assertEqual(b, QSM_BLOCK1)

        self.assertEqual(len(t.block_writes()), 1)
        self.assertNotIn(("wr", spm.REG_COMPOPMODE, spm.TRIGGER_COMPENSATION), t.log)
        self.assertEqual(t.spmcfg_writes()[-1], spm.SPM_CLOSED)

    def test_write_error_result_on_block_write_oserror(self):
        t = self.install(FakeTransport(block1=QSM_BLOCK1, block_write_raises=True))
        result, b = spm.apply_capmode(None)
        self.assertEqual(result, "write-error")
        self.assertEqual(b, QSM_BLOCK1)
        self.assertEqual(t.block_writes(), [])
        self.assertNotIn(("wr", spm.REG_COMPOPMODE, spm.TRIGGER_COMPENSATION), t.log)

    def test_timeout_on_wait_does_not_prevent_a_correct_result(self):
        # The chip's IRQ flag never fires (simulating a missed/late
        # interrupt), but the block write DID land -- apply_capmode must
        # still time out gracefully and let the verify read decide the
        # outcome, not treat the timeout itself as failure.
        t = self.install(FakeTransport(block1=QSM_BLOCK1, signal_write_done=False))
        result, b = spm.apply_capmode(None)
        expected_new = QSM_BLOCK1[0:2] + spm.CAPMODE + QSM_BLOCK1[5:8]
        self.assertEqual(result, "applied")
        self.assertEqual(b, expected_new)
        # the wait loop actually ran to its timeout (proof this exercised
        # the timeout path, not an immediate hit) -- using the fake clock,
        # so no real delay occurred.
        self.assertGreater(self.clock.sleep_calls, 0)

    def test_never_writes_outside_whitelist(self):
        t = self.install(FakeTransport(block1=QSM_BLOCK1))
        spm.apply_capmode(None)
        for e in t.log:
            if e[0] == "wr":
                self.assertIn((e[1], e[2]), spm.WRITE_WHITELIST)

    def test_golden_ordered_sequence_for_qsm_path(self):
        """The write, specified (Q1): assert the transport log is exactly
        this sequence, in order, for a clean QSM->netgear apply."""
        t = self.install(FakeTransport(block1=QSM_BLOCK1))
        result, b2 = spm.apply_capmode(None)
        self.assertEqual(result, "applied")
        new = QSM_BLOCK1[0:2] + spm.CAPMODE + QSM_BLOCK1[5:8]

        expected = (
            [("rd", 0x0D)]                                            # recover()
            + [("wr", 0x0D, 0x18), ("wr", 0x0E, 0x08)]                 # read_block1 open
            + [("rd", i) for i in range(8)]                            # read_block1 data
            + [("wr", 0x0D, 0x00)]                                     # read_block1 close
            + [("wr", 0x0D, 0x10), ("wr", 0x0E, 0x08),                 # _wr_block1 open+base
               ("rd", 0x0D), ("rd", 0x0E)]                              # _wr_block1 readback
            + [("block", 0x00, new)]                                   # the write
            + [("wr", 0x0D, 0x00)]                                     # _wr_block1 close
            + [("rd", 0x00)]                                           # wait for SPM write done
            + [("wr", 0x0D, 0x18), ("wr", 0x0E, 0x08)]                 # verify read_block1 open
            + [("rd", i) for i in range(8)]                            # verify data
            + [("wr", 0x0D, 0x00)]                                     # verify close
            + [("wr", 0x09, 0x04)]                                     # compensation trigger
            + [("rd", 0x00)]                                           # compensation poll
        )
        self.assertEqual(t.log, expected)


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

    def test_smbus_write_i2c_block_only_called_from_wr_block1(self):
        for name, node in self.functions.items():
            calls_it = self._calls_name(node, "_smbus_write_i2c_block")
            if name == "_wr_block1":
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

    def test_capmode_without_yes_writes_nothing(self):
        t = self.install(FakeTransport(block1=list(QSM_BLOCK1)))
        rc = self.tool.cmd_capmode(None, False)
        self.assertEqual(rc, 0)
        self.assertEqual(t.log, [])

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


if __name__ == "__main__":
    unittest.main()
