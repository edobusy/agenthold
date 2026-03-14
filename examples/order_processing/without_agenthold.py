"""
Demonstrates the state coordination problem in multi-agent systems.

Two agents process the same order concurrently. Without a coordination
layer, they overwrite each other's state updates silently. No error is
raised — the data is simply wrong.
"""

import threading
import time

# Shared state — a plain dict, like most teams use in early multi-agent code
order_state: dict = {
    "order_id": "ORD-7829",
    "status": "received",
    "total": 89.99,
    "reserved": False,
    "discount_applied": False,
}

write_log: list[str] = []


def inventory_agent() -> None:
    """Reads current state, reserves stock, updates status."""
    # Simulate reading state at this moment
    time.sleep(0.05)  # simulate processing time

    # Simulate writing back
    order_state.update({
        "reserved": True,
        "status": "processing",  # ← inventory agent sets status
    })
    write_log.append(
        "inventory-agent: wrote reserved=True, status=processing"
    )


def pricing_agent() -> None:
    """Reads current state, applies discount, updates status."""
    # Simulate reading state at the same moment
    time.sleep(0.05)  # same processing time

    # Simulate writing back — but this overwrites inventory agent's status
    order_state.update({
        "discount_applied": True,
        "total": round(order_state["total"] * 0.9, 2),
        "status": "awaiting_payment",  # ← pricing agent sets its own status
    })
    write_log.append(
        "pricing-agent: wrote discount_applied=True, status=awaiting_payment"
    )


def main() -> None:
    print("ORDER STATE AT START:")
    print(f"  status: {order_state['status']}")
    print(f"  total:  {order_state['total']}")
    print()

    t1 = threading.Thread(target=inventory_agent)
    t2 = threading.Thread(target=pricing_agent)

    t1.start()
    t2.start()
    t1.join()
    t2.join()

    print("WRITE LOG (order of operations):")
    for entry in write_log:
        print(f"  {entry}")
    print()

    print("FINAL STATE:")
    print(f"  status:           {order_state['status']}")
    print(f"  total:            {order_state['total']}")
    print(f"  reserved:         {order_state['reserved']}")
    print(f"  discount_applied: {order_state['discount_applied']}")
    print()

    # The problem: both agents completed, no error was raised,
    # but the result depends on which thread ran last.
    # inventory-agent's status update may have been silently overwritten.
    print("PROBLEM:")
    print("  Both agents ran without error.")
    print("  But depending on thread scheduling, inventory-agent's")
    print("  status='processing' may have been silently overwritten.")
    print("  The final state is non-deterministic and potentially wrong.")
    print("  This is a silent data corruption bug.")


if __name__ == "__main__":
    main()
