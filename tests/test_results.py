import unittest

from aetherpilot.errors import ValidationError
from aetherpilot.results import normalize_waveform, validate_waveform_result


class ResultTests(unittest.TestCase):
    def test_normalize_and_validate_waveform(self):
        result = normalize_waveform([[0, 1, 2], [1, 2, 3]], ["v(out)", "i(vdd)"], analysis="tran",
                                    source={"operation_id": "a" * 32}, fresh=True)
        self.assertEqual(result["axis"], {"name": "time", "unit": "s", "values": [0.0, 1.0]})
        self.assertEqual(validate_waveform_result(result), [])

    def test_rejects_nonmonotonic_axis(self):
        with self.assertRaises(ValidationError) as raised:
            normalize_waveform([[0, 1], [0, 2]], ["v(out)"], analysis="ac")
        self.assertEqual(raised.exception.code, "result_provenance")

    def test_detects_tampering(self):
        result = normalize_waveform([[0, 1], [1, 2]], ["v(out)"], analysis="dc")
        result["signals"][0]["values"][0] = 9.0
        self.assertTrue(validate_waveform_result(result))


if __name__ == "__main__":
    unittest.main()
