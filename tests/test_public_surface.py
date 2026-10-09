import unittest
from unittest.mock import Mock, patch

import aetherpilot
from aetherpilot.cli import parser


class PublicSurfaceTests(unittest.TestCase):
    def test_new_package_helpers_forward_semantics(self):
        target = {"library": "work", "cell": "dut"}
        attachment = object()
        native = Mock()
        with patch("aetherpilot.harness.Harness", return_value=native):
            aetherpilot.maestro_read(target, session_id="12", attached_api=attachment)
            native.maestro_read.assert_called_once_with(target, options=None, session_id="12", attached_api=attachment)
            aetherpilot.maestro_open_session(target, persistent=True, attached_api=attachment)
            native.maestro_open_session.assert_called_once_with(target, options=None, persistent=True, attached_api=attachment)
            aetherpilot.maestro_close_session(target, session_id="12", attached_api=attachment)
            native.maestro_close_session.assert_called_once_with(target, options=None, session_id="12", attached_api=attachment)
            aetherpilot.maestro_find_open_session(target, session_id="12", attached_api=attachment)
            native.maestro_find_open_session.assert_called_once_with(target, options=None, session_id="12", attached_api=attachment)
            aetherpilot.maestro_capabilities(attached_api=attachment)
            native.maestro_capabilities.assert_called_once_with(None, options=None, attached_api=attachment)
            aetherpilot.maestro_migrate_setup(target, {**target, "view": "copy"})
            native.maestro_migrate_setup.assert_called_once_with(target, {**target, "view": "copy"}, test="imported", options=None)
            aetherpilot.append_symbol({"shapes": []}, base_sha256="a" * 64)
            native.append_symbol.assert_called_once_with({"shapes": []}, base_sha256="a" * 64)
            aetherpilot.append_layout({"shapes": []}, base_sha256="a" * 64)
            native.append_layout.assert_called_once_with({"shapes": []}, base_sha256="a" * 64)
            aetherpilot.export_native_netlist(target, output_dir="private", netlist_format="hspice")
            native.export_native_netlist.assert_called_once_with(target, output_dir="private", model_libraries=None, temperature=27, netlist_format="hspice")
            aetherpilot.natural_language_apply("CMOS inverter", target=target, constraints=[])
            native.natural_language_apply.assert_called_once_with("CMOS inverter", target=target, grid=0.0625, constraints=[], routing=None, style=None)
            aetherpilot.natural_language_plan("CMOS inverter", target=target, constraints=[])
            native.natural_language_plan.assert_called_once_with("CMOS inverter", target=target, grid=0.0625, constraints=[], routing=None, style=None)
            manifest = {"schema_version": 1}
            aetherpilot.migrate_netlist("source.sp", target, manifest, approved=True, materialize=True)
            native.migrate_netlist.assert_called_once_with("source.sp", target, manifest, approved=True, materialize=True)

    def test_bridge_style_schematic_reader_forwards_to_guarded_readback(self):
        target = {"library": "work", "cell": "dut"}
        native = Mock()
        with patch("aetherpilot.harness.Harness", return_value=native):
            aetherpilot.read_schematic(target, recursive=False, max_depth=3, include_geometry=False)
            native.read_schematic.assert_called_once_with(
                target, recursive=False, max_depth=3, include_geometry=False,
                include_properties=True, configuration=None,
            )

    def test_cli_exposes_explicit_append_sort_and_native_format(self):
        cli = parser()
        for operation in ("layout-append", "symbol-append"):
            request = cli.parse_args([operation, "plan.json", "--base-sha256", "b" * 64])
            self.assertEqual(request.base_sha256, "b" * 64)
        request = cli.parse_args(["netlist-export-native", "--library", "work", "--cell", "dut"])
        self.assertEqual(request.format, "hspice")
        request = cli.parse_args(["symbol-generate", "--library", "work", "--cell", "dut", "--sort-pins", "geometric"])
        self.assertEqual(request.sort_pins, "geometric")
        self.assertEqual(cli.parse_args(["maestro-capabilities"]).command, "maestro-capabilities")
        self.assertEqual(cli.parse_args(["natural-apply", "CMOS inverter", "--library", "work", "--cell", "dut"]).command, "natural-apply")


if __name__ == "__main__":
    unittest.main()
