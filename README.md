# mongo-timetravel

Rebuild any MongoDB document as it was at any past instant. No audit log, no snapshot,
no extra collection: it reads the oplog your replica set already keeps.

```
timetravel.py URI DB.COLL ID history
timetravel.py URI DB.COLL ID at    "2026-09-12 14:32"
timetravel.py URI DB.COLL ID diff  "2026-09-12 10:00" "2026-09-12 11:00"
timetravel.py URI DB.COLL ID field price
```

## The problem

Someone asks: "why is this order marked refunded, and when did the total go to zero?"

MongoDB Community has no answer for that. There is no audit log, no `SELECT ... AS OF`,
and `$set` overwrites without keeping the old value. The usual options are a change
stream you should have started last month, a versioning scheme you should have
designed last year, or a backup you now have to restore somewhere and diff by hand.

But every replica set already records every write, in order, in `local.oplog.rs`. The
history is there. Nobody reads it because the format is a delta language, not documents.

## Four commands

Given one `_id`, the tool pulls every oplog entry that touched it, including writes
made inside transactions, and replays them from the insert forward. That gives one
full document per write. From there:

- `history` prints every write as a field-level diff, with time, session and
  transaction number.
- `at` prints the document exactly as it was at that instant.
- `diff` shows what changed between two instants.
- `field` follows one field (dotted paths work) and prints every value it ever had.

## Example

`demo.py` writes an order through a realistic life: cart, quantity change, second item
pushed, payment, refund inside a transaction, item pulled, delete. Then it asks.

```
$ python timetravel.py mongodb://127.0.0.1:27017/?directConnection=true shop.orders 6aa5da2d7307e9debdcbc0ce history

2026-09-12 20:03:09.629  insert  session 9a35abeb  txn 1
  + _id = {"$oid": "6aa5da2d7307e9debdcbc0ce"}
  + customer = "ana"
  + status = "cart"
  + items.0.sku = "A1"
  + items.0.qty = 1
  + items.0.price = 10.0
  + total = 10.0
  + notes.gift = false
2026-09-12 20:03:09.733  update  session 9a35abeb  txn 2
    items.0.qty: 1 -> 3
    total: 10.0 -> 30.0
2026-09-12 20:03:09.735  update  session 9a35abeb  txn 3
  + items.1.price = 5.5
  + items.1.qty = 1
  + items.1.sku = "B7"
    total: 30.0 -> 35.5
2026-09-12 20:03:09.841  update  session 9a35abeb  txn 4
    notes.gift: false -> true
  + paid_at = {"$date": "2026-09-12T14:32:00Z"}
    status: "cart" -> "paid"
2026-09-12 20:03:09.948  update  session 9a35abeb  txn 5
    status: "paid" -> "refunded"
2026-09-12 20:03:09.948  update  session 9a35abeb  txn 5
    total: 35.5 -> 0.0
2026-09-12 20:03:10.054  update  session 9a35abeb  txn 6
  - items.1.price  (was 5.5)
  - items.1.qty  (was 1)
  - items.1.sku  (was "B7")
2026-09-12 20:03:10.058  delete  session 9a35abeb  txn 7
  (deleted)
```

The two lines with `txn 5` and the same millisecond are the transaction: two updates,
one commit. The tool flattens `applyOps` so they show up as ordinary writes.

```
$ python timetravel.py ... shop.orders 6aa5da2d7307e9debdcbc0ce field status

2026-09-12 20:03:09.629  insert  session 9a35abeb  txn 1  status = "cart"
2026-09-12 20:03:09.841  update  session 9a35abeb  txn 4  status = "paid"
2026-09-12 20:03:09.948  update  session 9a35abeb  txn 5  status = "refunded"
2026-09-12 20:03:10.058  delete  session 9a35abeb  txn 7  status = null
```

## Oplog replay

1. Query `local.oplog.rs` for `ns` plus `o._id` or `o2._id` equal to the id, in natural
   order. Entries with `op: "c"` and an `applyOps` array are transactions; the matching
   inner ops are lifted out and inherit the transaction's `ts`, `wall`, `lsid` and
   `txnNumber`.
2. Replay. Inserts are full documents. Deletes make the document not exist. Updates come
   in three shapes and all three are handled:
   - `$v: 2` diffs, what MongoDB 5.0 and later write. Keys `i`, `u`, `d` for fields,
     `s<name>` for sub-documents, and inside arrays `a: true`, `l` for a new length,
     `u<index>` and `s<index>` for elements. `$push`, `$pull`, `$inc`, `$rename` and
     pipeline updates all arrive in this form.
   - `$v: 1` with `$set` and `$unset` and dotted paths, from older servers.
   - Full-document replacement, from `replaceOne`.
3. Keep one state per entry. `at` picks the last state whose `wall` is not after the
   instant. `diff` flattens two states into dotted paths and compares.

Every state is a deep copy, so replaying a `$push` never rewrites the state recorded
before it. That one was a real bug during development.

## Limits

- The oplog is a ring buffer. If the document's insert has already rotated out, the
  tool says so and reconstructs from the first write it can see; fields never touched
  since then are missing. Size the oplog for the window you care about
  (`replSetResizeOplog`).
- It needs a replica set. A single `mongod --replSet rs0` followed by
  `rs.initiate()` is enough for local use.
- The oplog records the session (`lsid`) and transaction number, not the user or the
  application. To get from a session to a person, correlate with your own logs or
  enable the server's audit log on the Enterprise build.
- `wall` is the server's clock. Times you type are read in your local zone unless they
  carry an offset or a `Z`.
- One `_id` at a time, by design. This is a forensic tool, not a replication tool.
- The oplog has no index on `_id`, so each question is a scan. Add `--since "2026-09-01"`
  to any command and the server seeks to that timestamp instead of reading from the
  start (explain shows `COLLSCAN` with a `minRecord` bound). If the cut lands after
  the insert, the tool says so and reconstructs from there.

## Requirements

Python 3.10 or newer and `pymongo`. Tested against MongoDB 8.2 with `pymongo` 4.18.

```
pip install pymongo
python -m unittest test_timetravel      # replays hand-written oplog entries, no server
python demo.py                          # needs a local replica set on 27017
```

## License

MIT.
