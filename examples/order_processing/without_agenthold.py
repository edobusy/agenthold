"""
Without Agenthold — the silent overwrite problem.

Two agents process the same order at the same time. Both read the current
state, do their work, then write back. Because neither checks whether the
other has written in the meantime, the last writer silently wins and the
first writer's update is lost. No error is raised.
"""

import threading
import time

order: dict = {
    "status": "received",
    "total": 89.99,
    "reserved": False,
    "discount_applied": False,
}

log: list[str] = []


def inventory_agent() -> None:
    snapshot = dict(order)          # read current state
    time.sleep(0.05)                # check warehouse availability

    order.update({"reserved": True, "status": "processing"})
    log.append("[inventory] wrote: reserved=True, status='processing'")


def pricing_agent() -> None:
    snapshot = dict(order)          # read current state
    time.sleep(0.05)                # fetch pricing rules

    new_total = round(snapshot["total"] * 0.9, 2)
    order.update({"discount_applied": True, "total": new_total, "status": "awaiting_payment"})
    log.append(f"[pricing]   wrote: discount_applied=True, total={new_total}, status='awaiting_payment'")


def main() -> None:
    print("Order ORD-7829 received:")
    print(f"  status={order['status']!r}, total={order['total']}, "
          f"reserved={order['reserved']}, discount_applied={order['discount_applied']}")
    print()
    print("inventory-agent and pricing-agent start simultaneously...")

    t1 = threading.Thread(target=inventory_agent)
    t2 = threading.Thread(target=pricing_agent)
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    print()
    for entry in log:
        print(f"  {entry}")

    print()
    print("Final state:")
    print(f"  status={order['status']!r}, total={order['total']}, "
          f"reserved={order['reserved']}, discount_applied={order['discount_applied']}")
    print()

    if order["status"] == "awaiting_payment":
        lost = "inventory-agent set status='processing'"
    else:
        lost = "pricing-agent set status='awaiting_payment'"

    print(f"PROBLEM: Both agents completed without error, but {lost}")
    print(f"         was silently overwritten. No exception was raised.")
    print(f"         Run this again -- you may get a different result.")


if __name__ == "__main__":
    main()
