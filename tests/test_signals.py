import unittest

from aetherpilot.signals import bind_hierarchy, bit_connections, expand_signal, resolve_inherited


class SignalTests(unittest.TestCase):
    def test_scalar_bus_slice_concat_repeat_and_global(self):
        examples = {"gnd!": ["gnd!"], "a<3:1>": ["a<3>", "a<2>", "a<1>"], "a[1:3]": ["a<1>", "a<2>", "a<3>"],
                    "{a<3:1: -1>,b}": ["a<3>", "a<2>", "a<1>", "b"]}
        for expression, expected in list(examples.items())[:3]:
            self.assertEqual(expand_signal(expression), expected)
        self.assertEqual(expand_signal("a<4:0:-2>,b"), ["a<4>", "a<2>", "a<0>", "b"])
        self.assertEqual(expand_signal("<*2>a<1:0>"), ["a<1>", "a<0>", "a<1>", "a<0>"])
        self.assertEqual(expand_signal("{2{a[1:0],b}}"), ["a<1>", "a<0>", "b"] * 2)

    def test_invalid_or_excessive_expressions(self):
        for expression in ("a<1:4:-1>", "a<1:4:0>", "a<0:4:3>", "a<1]", "a,,b", "{a,b", "a<999999999999:0>", "<*999999>a"):
            with self.subTest(expression=expression), self.assertRaises(ValueError):
                expand_signal(expression)

    def test_bit_width_is_never_broadcast_implicitly(self):
        with self.assertRaises(ValueError):
            bit_connections({"IN<3:0>": "supply"})
        self.assertEqual(len(bit_connections({"IN<3:0>": "<*4>supply"})), 4)

    def test_instance_array_mapping(self):
        values = bit_connections({"IN<1:0>": "data<7:0>"}, instance_name="bank<3:0>")
        self.assertEqual(values[0], {"instance": "bank<3>", "terminal": "IN<1>", "net": "data<7>"})
        self.assertEqual(values[-1], {"instance": "bank<0>", "terminal": "IN<0>", "net": "data<0>"})

    def test_overlapping_terminals_are_rejected(self):
        with self.assertRaises(ValueError):
            bit_connections({"a<3:0>": "b<3:0>", "a<1>": "b<1>"})

    def test_hierarchy_mapping_is_positional_and_width_checked(self):
        values = bind_hierarchy({"port<1:0>": "parent<3:2>"}, {"port<1:0>": "local<0:1>"})
        self.assertEqual(values[0], {"port": "port<1>", "parent_net": "parent<3>", "child_net": "local<0>"})
        with self.assertRaises(ValueError):
            bind_hierarchy({"port<1:0>": "scalar"}, {"port<1:0>": "local<0:1>"})

    def test_nearest_inherited_override_and_default_provenance(self):
        value = resolve_inherited({"property": "supply", "default": "vdd!"}, [{"path": ["top", "child"], "overrides": {"supply": "local"}}, {"path": ["top"], "overrides": {"supply": "global"}}])
        self.assertEqual(value["effective"], "local")
        self.assertEqual(value["override_chain"][0]["source"], ["top", "child"])
        self.assertEqual(resolve_inherited({"property": "supply", "default": "vdd!"}, [])["effective"], "vdd!")

    def test_inherited_cycles_are_rejected(self):
        with self.assertRaises(ValueError):
            resolve_inherited({"property": "supply", "default": "vdd!"}, [{"overrides": {"supply": {"property": "supply", "default": "vdd!"}}}])


if __name__ == "__main__":
    unittest.main()
