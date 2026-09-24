import tempfile
import unittest
from pathlib import Path

import aetherpilot
from aetherpilot.backend import backend_status
from aetherpilot.config import RuntimeConfig
from aetherpilot.harness import Harness
from aetherpilot.cli import parser


class OptionalBackendTests(unittest.TestCase):
    def test_import_and_offline_operations_do_not_need_native_runtime(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = RuntimeConfig(backend="offline", artifact_root=root / "artifacts", workspace=root / "workspace")
            self.assertFalse(backend_status(config)["available"])
            self.assertEqual(aetherpilot.backend_status(config)["code"], "backend_unavailable")
            harness = Harness(config)
            self.assertEqual(harness.backend.name, "stub")
            self.assertTrue(harness.diff_operation({"instances": []}, {"instances": []})["ok"])
            plan = harness.natural_language_plan("CMOS inverter", target={"library": "work", "cell": "inv"})
            self.assertTrue(plan["ok"])
            self.assertEqual(plan["readback"]["template"], "cmos_inverter")
            result = harness.libraries()
            self.assertFalse(result["ok"])
            self.assertEqual(result["errors"][0]["code"], "backend_unavailable")

    def test_harness_accepts_mapping_configuration_for_offline_use(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            harness = Harness({"backend": "offline", "artifact_root": root / "artifacts", "workspace": root / "workspace"})
            self.assertEqual(harness.backend_status()["reason"], "explicit_offline")

    def test_static_environment_probe_accepts_product_pyaether_tree(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            product = root / "product"
            (product / "bin").mkdir(parents=True)
            (product / "bin" / "pythone").touch()
            pyaether_tree = product / "tools" / "pyaether" / "2.6" / "linux" / "lib" / "pyAether"
            pyaether_tree.mkdir(parents=True)
            (pyaether_tree / "_pyaecore.so").touch()
            env_file = root / "backend.env"
            env_file.write_text(f"export AETHER_ROOT={product}\nexport PYAETHER_HOME=$AETHER_ROOT/tools/pyaether\n", encoding="utf-8")
            config = RuntimeConfig(backend="auto", env_file=env_file, workspace=root / "workspace", artifact_root=root / "artifacts")
            status = backend_status(config)
            self.assertTrue(status["available"])
            self.assertEqual(status["backend"], "native")

    def test_cli_keeps_explicit_backend_status_entrypoint(self):
        self.assertEqual(parser().parse_args(["backend-status"]).command, "backend-status")
        self.assertEqual(parser().parse_args(["backend"]).command, "backend")


if __name__ == "__main__":
    unittest.main()
