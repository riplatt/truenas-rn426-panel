import os
import sys
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))
sys.path.insert(0, _HERE)

from rnpanel.models import detect_model


class TestDetectModel(unittest.TestCase):
    def setUp(self):
        self._old_env = os.environ.get("RN_MODEL")

    def tearDown(self):
        if self._old_env is None:
            os.environ.pop("RN_MODEL", None)
        else:
            os.environ["RN_MODEL"] = self._old_env

    def test_env_override_rn426(self):
        os.environ["RN_MODEL"] = "rn426"
        self.assertEqual(detect_model(), "rn426")

    def test_env_override_rnx26(self):
        os.environ["RN_MODEL"] = "rnx26"
        self.assertEqual(detect_model(), "rnx26")

    def test_env_override_rn316(self):
        os.environ["RN_MODEL"] = "rn316"
        self.assertEqual(detect_model(), "rn316")

    def test_env_override_unknown_exits(self):
        os.environ["RN_MODEL"] = "bogus"
        with self.assertRaises(SystemExit):
            detect_model()

    def test_cpuinfo_denverton_selects_rn426(self):
        os.environ.pop("RN_MODEL", None)
        cpuinfo = lambda: "model name\t: Intel(R) Atom(TM) CPU C3538 @ 2.10GHz\n"
        self.assertEqual(detect_model(cpuinfo_reader=cpuinfo), "rn426")

    def test_dmi_528x_selects_rnx26(self):
        os.environ.pop("RN_MODEL", None)
        cpuinfo = lambda: (_ for _ in ()).throw(OSError())
        dmi = lambda: "ReadyNAS 528X\n"
        self.assertEqual(detect_model(cpuinfo_reader=cpuinfo, dmi_reader=dmi), "rnx26")

    def test_dmi_628x_selects_rnx26(self):
        os.environ.pop("RN_MODEL", None)
        cpuinfo = lambda: (_ for _ in ()).throw(OSError())
        dmi = lambda: "ReadyNAS 628X\n"
        self.assertEqual(detect_model(cpuinfo_reader=cpuinfo, dmi_reader=dmi), "rnx26")

    def test_dmi_316_selects_rn316(self):
        os.environ.pop("RN_MODEL", None)
        cpuinfo = lambda: (_ for _ in ()).throw(OSError())
        dmi = lambda: "ReadyNAS 316\n"
        self.assertEqual(detect_model(cpuinfo_reader=cpuinfo, dmi_reader=dmi), "rn316")

    def test_unknown_hardware_exits(self):
        os.environ.pop("RN_MODEL", None)
        cpuinfo = lambda: "model name\t: Some Other CPU\n"
        dmi = lambda: "Some Other Box\n"
        with self.assertRaises(SystemExit):
            detect_model(cpuinfo_reader=cpuinfo, dmi_reader=dmi)

    def test_missing_files_exit_not_crash(self):
        os.environ.pop("RN_MODEL", None)
        cpuinfo = lambda: (_ for _ in ()).throw(OSError())
        dmi = lambda: (_ for _ in ()).throw(OSError())
        with self.assertRaises(SystemExit):
            detect_model(cpuinfo_reader=cpuinfo, dmi_reader=dmi)


if __name__ == "__main__":
    unittest.main()
