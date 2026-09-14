"""Replays hand-written oplog entries; no mongod needed.

    python -m unittest test_timetravel
"""

import unittest
from datetime import datetime

import timetravel as tt


def entry(op, o, o2=None, wall=None):
    e = {"op": op, "ns": "db.c", "o": o, "wall": wall or datetime(2026, 1, 1)}
    if o2:
        e["o2"] = o2
    return e


class ReplayV2(unittest.TestCase):
    def test_set_insert_delete_fields(self):
        doc = {"_id": 1, "a": 1, "b": 2}
        tt.apply_v2_diff(doc, {"u": {"a": 9}, "i": {"c": 3}, "d": {"b": False}})
        self.assertEqual(doc, {"_id": 1, "a": 9, "c": 3})

    def test_nested_document(self):
        doc = {"_id": 1, "n": {"x": 1, "y": 2}}
        tt.apply_v2_diff(doc, {"sn": {"u": {"x": 5}, "d": {"y": False}}})
        self.assertEqual(doc["n"], {"x": 5})

    def test_array_push_and_element_update(self):
        doc = {"_id": 1, "items": [{"q": 1}]}
        tt.apply_v2_diff(doc, {"sitems": {"a": True, "u1": {"q": 2}}})
        self.assertEqual(doc["items"], [{"q": 1}, {"q": 2}])
        tt.apply_v2_diff(doc, {"sitems": {"a": True, "s0": {"u": {"q": 7}}}})
        self.assertEqual(doc["items"][0], {"q": 7})

    def test_array_truncate(self):
        doc = {"_id": 1, "items": [1, 2, 3]}
        tt.apply_v2_diff(doc, {"sitems": {"a": True, "l": 1}})
        self.assertEqual(doc["items"], [1])


class ReplayV1(unittest.TestCase):
    def test_set_and_unset_with_dotted_paths(self):
        doc = {"_id": 1, "n": {"x": 1}, "arr": [0, 1]}
        after = tt.apply(doc, entry("u", {"$v": 1, "$set": {"n.y": 2, "arr.1": 9},
                                          "$unset": {"n.x": 1}}, o2={"_id": 1}))
        self.assertEqual(after, {"_id": 1, "n": {"y": 2}, "arr": [0, 9]})

    def test_full_replacement_keeps_id(self):
        after = tt.apply({"_id": 1, "old": True}, entry("u", {"new": True}, o2={"_id": 1}))
        self.assertEqual(after, {"new": True, "_id": 1})


class States(unittest.TestCase):
    def test_earlier_states_are_not_mutated_by_later_updates(self):
        first = tt.apply(None, entry("i", {"_id": 1, "items": [{"q": 1}]}))
        second = tt.apply(first, entry("u", {"$v": 2, "diff": {"sitems": {"a": True, "u1": {"q": 2}}}},
                                       o2={"_id": 1}))
        self.assertEqual(len(first["items"]), 1)
        self.assertEqual(len(second["items"]), 2)

    def test_delete_then_reinsert(self):
        gone = tt.apply({"_id": 1}, entry("d", {"_id": 1}))
        self.assertIsNone(gone)
        back = tt.apply(gone, entry("i", {"_id": 1, "v": 2}))
        self.assertEqual(back["v"], 2)


class Diff(unittest.TestCase):
    def test_reports_added_removed_changed(self):
        lines = tt.diff({"_id": 1, "a": 1, "b": {"c": 2}}, {"_id": 1, "a": 5, "d": 0})
        self.assertIn("    a: 1 -> 5", lines)
        self.assertIn("  - b.c  (was 2)", lines)
        self.assertIn("  + d = 0", lines)

    def test_no_change(self):
        self.assertEqual(tt.diff({"_id": 1}, {"_id": 1}), ["  (no change)"])


class Record(unittest.TestCase):
    def test_json_line_for_update(self):
        e = entry("u", {"$v": 2, "diff": {"u": {"a": 5}}}, {"_id": 1})
        e["txnNumber"] = 3
        rec = tt.record(e, {"_id": 1, "a": 1, "b": 2}, {"_id": 1, "a": 5, "c": 0})
        self.assertEqual(rec["op"], "update")
        self.assertEqual(rec["txn"], 3)
        self.assertIsNone(rec["session"])
        self.assertEqual(rec["changes"], [
            {"path": "a", "old": 1, "new": 5},
            {"path": "b", "old": 2},
            {"path": "c", "new": 0},
        ])
        self.assertNotIn("deleted", rec)

    def test_json_line_for_delete_is_extended_json(self):
        rec = tt.record(entry("d", {"_id": 1}), {"_id": 1, "a": 1}, None)
        self.assertTrue(rec["deleted"])
        line = tt.show(rec)
        self.assertIn('"$date"', line)
        self.assertIn('"deleted": true', line)


class Ids(unittest.TestCase):
    def test_object_id_int_and_string(self):
        self.assertEqual(str(tt.parse_id("6aa5da2d7307e9debdcbc0ce")), "6aa5da2d7307e9debdcbc0ce")
        self.assertEqual(tt.parse_id("42"), 42)
        self.assertEqual(tt.parse_id("order-42"), "order-42")


if __name__ == "__main__":
    unittest.main()
