"""Writes one order through a realistic life cycle, then asks timetravel what happened.

    python demo.py mongodb://127.0.0.1:27017/?directConnection=true
"""

import subprocess
import sys
import time
from datetime import datetime

from pymongo import MongoClient

uri = sys.argv[1] if len(sys.argv) > 1 else "mongodb://127.0.0.1:27017/?directConnection=true"
client = MongoClient(uri)
orders = client["shop"]["orders"]
orders.delete_many({})

marks = {}

def mark(name):
    time.sleep(0.05)
    marks[name] = datetime.now()
    time.sleep(0.05)

order_id = orders.insert_one({
    "customer": "ana",
    "status": "cart",
    "items": [{"sku": "A1", "qty": 1, "price": 10.0}],
    "total": 10.0,
    "notes": {"gift": False},
}).inserted_id
mark("created")

orders.update_one({"_id": order_id}, {"$set": {"items.0.qty": 3, "total": 30.0}})
orders.update_one({"_id": order_id}, {"$push": {"items": {"sku": "B7", "qty": 1, "price": 5.5}},
                                     "$inc": {"total": 5.5}})
mark("filled")

orders.update_one({"_id": order_id}, {"$set": {"status": "paid", "notes.gift": True,
                                               "paid_at": datetime(2026, 9, 12, 14, 32)},
                                     "$unset": {"notes.gift_message": ""}})
mark("paid")

with client.start_session() as session:
    with session.start_transaction():
        orders.update_one({"_id": order_id}, {"$set": {"status": "refunded"}}, session=session)
        orders.update_one({"_id": order_id}, {"$inc": {"total": -35.5}}, session=session)
mark("refunded")

orders.update_one({"_id": order_id}, {"$pull": {"items": {"sku": "B7"}}})
orders.delete_one({"_id": order_id})
mark("deleted")

def run(*args):
    print(f"\n$ timetravel {' '.join(args)}\n", flush=True)
    subprocess.run([sys.executable, "timetravel.py", uri, "shop.orders", str(order_id), *args])

stamp = lambda name: marks[name].strftime("%Y-%m-%d %H:%M:%S.%f")

run("history")
run("at", stamp("paid"))
run("diff", stamp("filled"), stamp("refunded"))
run("field", "status")
run("field", "items.1.sku")
