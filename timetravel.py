#!/usr/bin/env python3
"""Rebuild any MongoDB document as it was at any past instant, from the oplog.

Community Edition has no audit log and no point-in-time read. But every replica
set already keeps the oplog: an ordered list of every write that happened. This
tool walks it for a single _id and replays the writes, so you can ask

    what did this document look like at 14:32?
    which operation changed the "price" field, and from what to what?
    what is the difference between the document at 10:00 and at 11:00?

Usage:
    timetravel.py URI DB.COLL ID history
    timetravel.py URI DB.COLL ID at    "2026-09-12 14:32"
    timetravel.py URI DB.COLL ID diff  "2026-09-12 10:00" "2026-09-12 11:00"
    timetravel.py URI DB.COLL ID field price

Add --since "2026-09-01" to any command to skip the part of the oplog older than
that. The oplog has no index on _id, so without it every question scans the whole
oplog; with it the server seeks straight to that timestamp.

Add --json to `history` or `field` to get one Extended JSON object per line instead
of the text layout, so the output can go through jq or into another collection.

Times are read in your local time zone unless they carry an explicit offset or
a trailing Z. Only pymongo is required.
"""

import copy
import sys
from datetime import datetime, timezone

from bson import ObjectId, Timestamp, json_util
from pymongo import MongoClient

# ---------------------------------------------------------------- oplog reading

def oplog_entries(client, ns, doc_id, since=None):
    """Every oplog entry that touched (ns, doc_id), oldest first, transactions flattened.

    `since` is a UTC datetime. A `ts` lower bound lets the server seek into the
    oplog instead of scanning it from the start.
    """
    oplog = client["local"]["oplog.rs"]
    touches = {"$or": [{"o._id": doc_id}, {"o2._id": doc_id}]}
    query = {"$or": [
        {"ns": ns, **touches},
        {"op": "c", "o.applyOps": {"$elemMatch": {"ns": ns, **touches}}},
    ]}
    if since is not None:
        query["ts"] = {"$gte": Timestamp(int(since.replace(tzinfo=timezone.utc).timestamp()), 0)}
    for entry in oplog.find(query).sort("$natural", 1):
        if entry["op"] == "c":
            for inner in entry["o"]["applyOps"]:
                if inner.get("ns") == ns and _touches(inner, doc_id):
                    yield _with_context(inner, entry)
        else:
            yield entry


def _touches(inner, doc_id):
    return inner.get("o", {}).get("_id") == doc_id or inner.get("o2", {}).get("_id") == doc_id


def _with_context(inner, outer):
    """An op inside a transaction inherits the transaction's time and session."""
    merged = dict(inner)
    for key in ("ts", "wall", "lsid", "txnNumber"):
        if key in outer:
            merged[key] = outer[key]
    return merged


def oldest_oplog_time(client):
    first = client["local"]["oplog.rs"].find_one(sort=[("$natural", 1)])
    return first["wall"] if first else None

# ---------------------------------------------------------------- replaying writes

def apply(doc, entry):
    """Return the document after this oplog entry. None means it does not exist."""
    op = entry["op"]
    if op == "i":
        return copy.deepcopy(entry["o"])
    if op == "d":
        return None
    if op != "u":
        return doc
    change = entry["o"]
    base = copy.deepcopy(doc) if doc is not None else {"_id": entry["o2"]["_id"]}
    if change.get("$v") == 2 and "diff" in change:
        apply_v2_diff(base, change["diff"])
        return base
    if any(key.startswith("$") for key in change if key != "$v"):
        for path, value in change.get("$set", {}).items():
            set_path(base, path, value)
        for path in change.get("$unset", {}):
            unset_path(base, path)
        return base
    replacement = dict(change)               # full-document replacement
    replacement.setdefault("_id", base["_id"])
    return replacement


def apply_v2_diff(target, diff):
    """The $v:2 delta format MongoDB 5.0+ writes to the oplog.

    Document diff keys: i (insert fields), u (update fields), d (delete fields),
    s<name> (sub-diff of a field). Array diff keys: a:true, l (new length),
    u<index> (set element), s<index> (sub-diff of an element).
    """
    if diff.get("a"):
        if "l" in diff:
            del target[diff["l"]:]
            target.extend([None] * (diff["l"] - len(target)))
        for key, value in diff.items():
            if key in ("a", "l"):
                continue
            index = int(key[1:])
            while len(target) <= index:
                target.append(None)
            if key[0] == "u":
                target[index] = value
            elif key[0] == "s":
                apply_v2_diff(target[index], value)
        return
    for field, value in diff.get("i", {}).items():
        target[field] = value
    for field, value in diff.get("u", {}).items():
        target[field] = value
    for field in diff.get("d", {}):
        target.pop(field, None)
    for key, value in diff.items():
        if key[0] == "s" and len(key) > 1:
            apply_v2_diff(target[key[1:]], value)


def set_path(target, path, value):
    parts = path.split(".")
    for part in parts[:-1]:
        target = _step(target, part, create=True)
    last = parts[-1]
    if isinstance(target, list):
        index = int(last)
        while len(target) <= index:
            target.append(None)
        target[index] = value
    else:
        target[last] = value


def unset_path(target, path):
    parts = path.split(".")
    for part in parts[:-1]:
        target = _step(target, part, create=False)
        if target is None:
            return
    last = parts[-1]
    if isinstance(target, list):
        index = int(last)
        if index < len(target):
            target[index] = None
    else:
        target.pop(last, None)


def _step(target, part, create):
    if isinstance(target, list):
        index = int(part)
        while create and len(target) <= index:
            target.append({})
        return target[index] if index < len(target) else None
    if create:
        return target.setdefault(part, {})
    return target.get(part)

# ---------------------------------------------------------------- timeline

class Timeline:
    """The document's states, one per oplog entry, oldest first."""

    def __init__(self, client, ns, doc_id, since=None):
        self.entries = list(oplog_entries(client, ns, doc_id, since))
        self.states = []
        doc = None
        self.partial = bool(self.entries) and self.entries[0]["op"] != "i"
        for entry in self.entries:
            doc = apply(doc, entry)
            self.states.append(doc)
        self.oplog_start = oldest_oplog_time(client)

    def at(self, when):
        """Document as it was at `when`; None if it did not exist then."""
        doc = None
        for entry, state in zip(self.entries, self.states):
            if entry["wall"] > when:
                break
            doc = state
        return doc

    def changes(self):
        """(entry, before, after) for every write."""
        before = None
        for entry, after in zip(self.entries, self.states):
            yield entry, before, after
            before = after

# ---------------------------------------------------------------- diffing documents

def flatten(doc, prefix=""):
    """{"a.b": value} for every leaf, so two documents can be compared path by path."""
    if doc is None:
        return {}
    out = {}
    items = doc.items() if isinstance(doc, dict) else enumerate(doc)
    for key, value in items:
        path = f"{prefix}{key}"
        if isinstance(value, (dict, list)) and value:
            out.update(flatten(value, path + "."))
        else:
            out[path] = value
    return out


def diff(before, after):
    """Lines describing how `before` became `after`."""
    if before is None and after is None:
        return ["  (does not exist)"]
    if before is None:
        return [f"  + {path} = {show(value)}" for path, value in flatten(after).items()]
    if after is None:
        return ["  (deleted)"]
    old, new = flatten(before), flatten(after)
    lines = []
    for path in sorted(set(old) | set(new)):
        if path not in old:
            lines.append(f"  + {path} = {show(new[path])}")
        elif path not in new:
            lines.append(f"  - {path}  (was {show(old[path])})")
        elif old[path] != new[path]:
            lines.append(f"    {path}: {show(old[path])} -> {show(new[path])}")
    return lines or ["  (no change)"]


def show(value):
    return json_util.dumps(value)


def pretty(doc):
    return json_util.dumps(doc, indent=2) if doc is not None else "(does not exist)"

# ---------------------------------------------------------------- CLI

def parse_id(text):
    if ObjectId.is_valid(text):
        return ObjectId(text)
    for convert in (int, float):
        try:
            return convert(text)
        except ValueError:
            pass
    try:
        return json_util.loads(text)
    except Exception:
        return text


def parse_time(text):
    when = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if when.tzinfo is None:
        when = when.astimezone()               # naive input means local time
    return when.astimezone(timezone.utc).replace(tzinfo=None)


def local(when):
    return when.replace(tzinfo=timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


def record(entry, before, after):
    """One write as a plain dict, for --json: who wrote, when, and every changed path."""
    session = entry.get("lsid", {}).get("id")
    out = {
        "wall": entry["wall"],
        "op": {"i": "insert", "u": "update", "d": "delete"}.get(entry["op"], entry["op"]),
        "session": session.hex()[:8] if session is not None else None,
        "txn": entry.get("txnNumber"),
        "changes": [],
    }
    old, new = flatten(before), flatten(after)
    for path in sorted(set(old) | set(new)):
        if path not in old:
            out["changes"].append({"path": path, "new": new[path]})
        elif path not in new:
            out["changes"].append({"path": path, "old": old[path]})
        elif old[path] != new[path]:
            out["changes"].append({"path": path, "old": old[path], "new": new[path]})
    if after is None and before is not None:
        out["deleted"] = True
    return out


def who(entry):
    session = entry.get("lsid", {}).get("id")
    parts = [{"i": "insert", "u": "update", "d": "delete"}.get(entry["op"], entry["op"])]
    if session is not None:
        parts.append(f"session {session.hex()[:8]}")
    if "txnNumber" in entry:
        parts.append(f"txn {entry['txnNumber']}")
    return "  ".join(parts)


def main(argv):
    if len(argv) < 4:
        print(__doc__)
        return 2
    since = None
    if "--since" in argv:
        at = argv.index("--since")
        since = parse_time(argv[at + 1])
        argv = argv[:at] + argv[at + 2:]
    as_json = "--json" in argv
    argv = [a for a in argv if a != "--json"]
    uri, ns, doc_id, command = argv[0], argv[1], parse_id(argv[2]), argv[3]
    args = argv[4:]
    client = MongoClient(uri)
    timeline = Timeline(client, ns, doc_id, since)

    if not timeline.entries:
        print(f"no write to {ns} {show(doc_id)} in the oplog", end="")
        if timeline.oplog_start:
            print(f" (oplog starts {local(timeline.oplog_start)})", end="")
        print()
        return 1
    if timeline.partial:
        reason = (f"--since cuts the history at {local(since)}" if since
                  else f"the oplog only goes back to {local(timeline.oplog_start)}")
        print(f"warning: first oplog entry for this document is not its insert; {reason}. "
              f"Fields never written since then are missing from the reconstruction.\n")

    if command == "history":
        for entry, before, after in timeline.changes():
            if as_json:
                print(show(record(entry, before, after)))
                continue
            print(f"{local(entry['wall'])}  {who(entry)}")
            print("\n".join(diff(before, after)))
    elif command == "at":
        print(pretty(timeline.at(parse_time(args[0]))))
    elif command == "diff":
        start, end = parse_time(args[0]), parse_time(args[1])
        print("\n".join(diff(timeline.at(start), timeline.at(end))))
    elif command == "field":
        path = args[0]
        previous = None
        for entry, before, after in timeline.changes():
            value = flatten(after).get(path) if after is not None else None
            if value != previous or entry["op"] in ("i", "d"):
                if as_json:
                    line = record(entry, None, None)
                    del line["changes"]
                    line.update({"path": path, "value": value})
                    print(show(line))
                else:
                    print(f"{local(entry['wall'])}  {who(entry)}  {path} = {show(value)}")
            previous = value
    else:
        print(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
