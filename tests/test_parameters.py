import unittest

from aetherpilot.parameters import resolve_parameter


class ParameterTests(unittest.TestCase):
    def test_cdf_instance_formula_retains_input_provenance(self):
        parameters = {"m": "2", "Fingers": "3", "simM": 'iPar("m")*iPar("Fingers")'}
        actual = resolve_parameter("simM", parameters)
        self.assertEqual(actual["value"], 6)
        self.assertEqual([item["parameter"] for item in actual["inputs"]], ["m", "Fingers"])

    def test_nested_engineering_and_parent_context(self):
        actual = resolve_parameter("area", {"w": "2u", "area": 'iPar("w")*pPar("length")'}, [{"length": "180n"}])
        self.assertAlmostEqual(actual["value"], 3.6e-13, places=25)
        self.assertEqual(actual["inputs"][1]["scope"], "parent")

    def test_unsafe_unknown_cyclic_and_unbounded_expressions(self):
        for expression in ('__import__("os").system("id")', 'open("file")', "[1][0]", 'iPar("missing")', 'iPar("value")', "1/0", "2**10000000", "1e300*1e300"):
            with self.subTest(expression=expression), self.assertRaises(ValueError):
                resolve_parameter("value", {"value": expression})
        with self.assertRaises(ValueError):
            resolve_parameter("value", {"value": 'pPar("size")'})

    def test_bounded_functions(self):
        self.assertEqual(resolve_parameter("value", {"value": "max(abs(-2), sqrt(9))+min(1, 5)"})["value"], 4)

    def test_alps_engineering_suffixes(self):
        from aetherpilot.contract import engineering

        self.assertAlmostEqual(engineering("251.1886431510x"), 251.1886431510e6)
        self.assertAlmostEqual(engineering("1.0000000000g"), 1e9)
        self.assertAlmostEqual(engineering("-163.2074478976n"), -163.2074478976e-9)
        self.assertAlmostEqual(engineering("198.4f"), 198.4e-15)


if __name__ == "__main__":
    unittest.main()
