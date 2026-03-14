"""
Demonstrates coordinated multi-agent state using Agenthold.

The same two agents process the same order concurrently. When a conflict
is detected, the losing agent re-reads the current state and retries.
The final state is correct and deterministic regardless of scheduling.
"""

import threading
import time

from agenthold.exceptions import ConflictError
from agenthold.store import StateStore

store = StateStore(":memory:")
log: list[str] = []


def setup_order() -> None:
    store.set("ORD-7829", "status", "received", updated_by="system")
    store.set("ORD-7829", "total", 89.99, updated_by="system")
    store.set("ORD-7829", "reserved", False, updated_by="system")
    store.set("ORD-7829", "discount_applied", False, updated_by="system")


def inventory_agent() -> None:
    """Reserves stock using read-modify-write with conflict detection."""
    log.append("inventory-agent: reading current order state")
    time.sleep(0.05)

    retries = 0
    while True:
        status_record = store.get("ORD-7829", "status")
        try:
            store.set("ORD-7829", "reserved", True, updated_by="inventory-agent")
            store.set(
                "ORD-7829",
                "status",
                "processing",
                updated_by="inventory-agent",
                expected_version=status_record.version,
            )
            log.append(
                f"inventory-agent: wrote reserved=True, status=processing"
                f" (after {retries} retries)"
            )
            break
        except ConflictError as e:
            retries += 1
            log.append(
                f"inventory-agent: conflict on status"
                f" (expected v{e.detail.expected_version},"
                f" got v{e.detail.actual_version}"
                f" by {e.detail.updated_by}) — retrying"
            )
            time.sleep(0.01)


def pricing_agent() -> None:
    """Applies discount using read-modify-write with conflict detection."""
    log.append("pricing-agent: reading current order state")
    time.sleep(0.05)

    # Compute discount once from the original price before any retries.
    # total and discount_applied are only written by this agent, so those
    # writes are unconditional; only status races with inventory-agent.
    new_total = round(store.get("ORD-7829", "total").value * 0.9, 2)

    retries = 0
    while True:
        status_record = store.get("ORD-7829", "status")
        try:
            store.set(
                "ORD-7829", "discount_applied", True, updated_by="pricing-agent"
            )
            store.set(
                "ORD-7829", "total", new_total, updated_by="pricing-agent"
            )
            store.set(
                "ORD-7829",
                "status",
                "awaiting_payment",
                updated_by="pricing-agent",
                expected_version=status_record.version,
            )
            log.append(
                f"pricing-agent: wrote discount_applied=True,"
                f" total={new_total}, status=awaiting_payment"
                f" (after {retries} retries)"
            )
            break
        except ConflictError as e:
            retries += 1
            log.append(
                f"pricing-agent: conflict on {e.detail.key}"
                f" (expected v{e.detail.expected_version},"
                f" got v{e.detail.actual_version}"
                f" by {e.detail.updated_by}) — retrying"
            )
            time.sleep(0.01)


def main() -> None:
    setup_order()

    print("ORDER STATE AT START:")
    for record in store.list_keys("ORD-7829"):
        print(
            f"  {record.key}: {record.value}"
            f" (v{record.version})"
        )
    print()

    t1 = threading.Thread(target=inventory_agent)
    t2 = threading.Thread(target=pricing_agent)
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    print("EVENT LOG:")
    for entry in log:
        print(f"  {entry}")
    print()

    print("FINAL STATE:")
    for record in store.list_keys("ORD-7829"):
        print(
            f"  {record.key}: {record.value}"
            f" (v{record.version}, by {record.updated_by})"
        )
    print()

    print("VERSION HISTORY (status key):")
    for h in store.history("ORD-7829", "status"):
        print(f"  v{h.version}: {h.value} — written by {h.updated_by}")
    print()

    # Verify correctness
    status = store.get("ORD-7829", "status")
    reserved = store.get("ORD-7829", "reserved")
    discount = store.get("ORD-7829", "discount_applied")
    total = store.get("ORD-7829", "total")

    assert reserved.value is True, "reserved should be True"
    assert discount.value is True, "discount_applied should be True"
    assert total.value == round(89.99 * 0.9, 2), "total should be discounted"
    print("RESULT: All assertions passed. State is correct and deterministic.")


if __name__ == "__main__":
    main()
