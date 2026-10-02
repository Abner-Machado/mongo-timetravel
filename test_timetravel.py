"""Replays hand-written oplog entries; no mongod needed.

    python -m unittest test_timetravel
"""

import contextlib
import io
import unittest
from datetime import datetime
from types import SimpleNamespace

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

    def test_subdiff_of_unknown_field_is_skipped(self):
        # Partial history: the insert fell off the oplog, so the first entry is an
        # update whose sub-diff touches a field we never saw. It must not crash.
        after = tt.apply(None, entry("u", {"$v": 2, "diff": {"saddr": {"u": {"city": "SP"}}}},
                                     o2={"_id": 1}))
        self.assertEqual(after, {"_id": 1})

    def test_subdiff_of_unknown_array_element_is_skipped(self):
        # Array grows to reach the index, so the element is a padding None whose
        # prior value is unknown. A sub-diff of it must be skipped, not crash.
        doc = {"_id": 1, "items": [{"q": 1}]}
        tt.apply_v2_diff(doc, {"sitems": {"a": True, "s2": {"u": {"q": 9}}}})
        self.assertEqual(doc["items"], [{"q": 1}, None, None])


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


class Usage(unittest.TestCase):
    """Bad command lines must fail before any connection attempt."""

    def run_main(self, *args):
        # Any attempt to connect would call MongoClient; make that loud.
        real = tt.MongoClient
        tt.MongoClient = lambda *a, **k: self.fail("connected with a bad command line")
        out = io.StringIO()
        try:
            with contextlib.redirect_stdout(out):
                code = tt.main(["mongodb://x", "db.c", "1", *args])
        finally:
            tt.MongoClient = real
        return code, out.getvalue()

    def test_missing_time_for_at(self):
        code, out = self.run_main("at")
        self.assertEqual(code, 2)
        self.assertIn("at takes TIME", out)

    def test_diff_with_one_time(self):
        code, out = self.run_main("diff", "2026-01-01")
        self.assertEqual(code, 2)
        self.assertIn("diff takes START END", out)

    def test_field_without_path(self):
        code, _ = self.run_main("field")
        self.assertEqual(code, 2)

    def test_since_without_value(self):
        code, out = self.run_main("history", "--since")
        self.assertEqual(code, 2)
        self.assertIn("--since needs a time", out)

    def test_unknown_command(self):
        code, out = self.run_main("undo")
        self.assertEqual(code, 2)
        self.assertIn("Usage:", out)


class FakeOplog:
    """Stands in for local.oplog.rs: keeps the entries and the last query."""

    def __init__(self, entries):
        self.entries = entries
        self.query = None

    def find(self, query):
        self.query = query
        return self

    def sort(self, *_):
        return iter(self.entries)

    def find_one(self, sort=None):
        return self.entries[0] if self.entries else None


class FakeClient(dict):
    def __init__(self, entries):
        self.oplog = FakeOplog(entries)
        super().__init__({"local": {"oplog.rs": self.oplog}})


class Timelines(unittest.TestCase):
    def timeline(self, entries, since=None):
        return tt.Timeline(FakeClient(entries), "db.c", 1, since)

    def test_at_is_inclusive_of_the_write_instant(self):
        t1, t2 = datetime(2026, 1, 1, 10), datetime(2026, 1, 1, 11)
        line = self.timeline([
            entry("i", {"_id": 1, "v": "a"}, wall=t1),
            entry("u", {"$v": 2, "diff": {"u": {"v": "b"}}}, {"_id": 1}, wall=t2),
        ])
        self.assertIsNone(line.at(datetime(2026, 1, 1, 9)))
        self.assertEqual(line.at(t1)["v"], "a")
        self.assertEqual(line.at(datetime(2026, 1, 1, 10, 30))["v"], "a")
        self.assertEqual(line.at(t2)["v"], "b")
        self.assertFalse(line.partial)

    def test_partial_when_the_insert_fell_off_the_oplog(self):
        line = self.timeline([
            entry("u", {"$v": 2, "diff": {"u": {"v": "b"}}}, {"_id": 1}),
        ])
        self.assertTrue(line.partial)
        self.assertEqual(line.states[0], {"_id": 1, "v": "b"})

    def test_changes_pairs_each_write_with_its_before_state(self):
        line = self.timeline([
            entry("i", {"_id": 1, "v": "a"}),
            entry("d", {"_id": 1}),
        ])
        pairs = [(before, after) for _, before, after in line.changes()]
        self.assertEqual(pairs, [(None, {"_id": 1, "v": "a"}), ({"_id": 1, "v": "a"}, None)])

    def test_transaction_ops_are_flattened_and_inherit_the_outer_time(self):
        when = datetime(2026, 1, 1, 12)
        outer = {"op": "c", "ns": "admin.$cmd", "wall": when, "ts": "outer-ts", "lsid": "s1",
                 "o": {"applyOps": [
                     {"op": "i", "ns": "db.c", "o": {"_id": 1, "v": "a"}},
                     {"op": "i", "ns": "db.other", "o": {"_id": 1, "v": "x"}},   # other collection
                     {"op": "u", "ns": "db.c", "o": {"$v": 2, "diff": {"u": {"v": "b"}}}, "o2": {"_id": 2}},  # other doc
                 ]}}
        line = self.timeline([outer])
        self.assertEqual(len(line.entries), 1)
        self.assertEqual(line.entries[0]["wall"], when)
        self.assertEqual(line.entries[0]["ts"], "outer-ts")
        self.assertEqual(line.entries[0]["lsid"], "s1")
        self.assertEqual(line.at(when), {"_id": 1, "v": "a"})

    def test_since_adds_a_ts_lower_bound_to_the_query(self):
        client = FakeClient([])
        tt.Timeline(client, "db.c", 1, since=datetime(2026, 1, 1, 0, 0, 0))
        bound = client.oplog.query["ts"]["$gte"]
        self.assertEqual(bound, tt.Timestamp(1767225600, 0))

    def test_without_since_the_query_has_no_ts(self):
        client = FakeClient([])
        tt.Timeline(client, "db.c", 1)
        self.assertNotIn("ts", client.oplog.query)


class FlattenTests(unittest.TestCase):
    def test_empty_doc_and_none(self):
        self.assertEqual(tt.flatten({}), {})
        self.assertEqual(tt.flatten(None), {})

    def test_nested_and_lists_use_dotted_paths(self):
        flat = tt.flatten({"a": {"b": 1}, "xs": [10, 20]})
        self.assertEqual(flat["a.b"], 1)
        self.assertEqual(flat["xs.0"], 10)
        self.assertEqual(flat["xs.1"], 20)

    def test_empty_container_is_kept_as_a_leaf(self):
        # An empty dict/list is falsy, so it is recorded as a value, not recursed.
        self.assertEqual(tt.flatten({"tags": [], "meta": {}}), {"tags": [], "meta": {}})


class DiffTests(unittest.TestCase):
    def test_both_absent(self):
        self.assertEqual(tt.diff(None, None), ["  (does not exist)"])

    def test_creation_lists_every_leaf_as_added(self):
        lines = tt.diff(None, {"a": 1, "b": 2})
        self.assertIn("  + a = 1", lines)
        self.assertIn("  + b = 2", lines)

    def test_deletion(self):
        self.assertEqual(tt.diff({"a": 1}, None), ["  (deleted)"])

    def test_no_change(self):
        self.assertEqual(tt.diff({"a": 1}, {"a": 1}), ["  (no change)"])

    def test_added_removed_and_changed(self):
        lines = tt.diff({"a": 1, "b": 2}, {"a": 9, "c": 3})
        self.assertIn("    a: 1 -> 9", lines)
        self.assertIn("  - b  (was 2)", lines)
        self.assertIn("  + c = 3", lines)


class ParseTimeTests(unittest.TestCase):
    def test_z_suffix_is_utc(self):
        self.assertEqual(tt.parse_time("2026-01-02T03:04:05Z"), datetime(2026, 1, 2, 3, 4, 5))

    def test_explicit_offset_converts_to_utc(self):
        # 03:00-03:00 is 06:00 UTC; the result is tz-naive UTC.
        got = tt.parse_time("2026-01-02T03:00:00-03:00")
        self.assertEqual(got, datetime(2026, 1, 2, 6, 0, 0))
        self.assertIsNone(got.tzinfo)


class WhoTests(unittest.TestCase):
    def test_bare_op_name(self):
        self.assertEqual(tt.who({"op": "u"}), "update")

    def test_session_is_truncated_to_eight_hex(self):
        sid = SimpleNamespace(hex=lambda: "abcdef1234567890")
        self.assertEqual(tt.who({"op": "i", "lsid": {"id": sid}}), "insert  session abcdef12")

    def test_txn_number_appended(self):
        line = tt.who({"op": "d", "txnNumber": 7})
        self.assertEqual(line, "delete  txn 7")


if __name__ == "__main__":
    unittest.main()
