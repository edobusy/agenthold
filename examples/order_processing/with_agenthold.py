"""
With Agenthold — conflict-safe shared state.

The same two agents process the same order at the same time. Each agent
passes expected_version when writing to a shared field. If another agent
has written since its read, a ConflictError is raised instead of silently
overwriting. The losing agent re-reads the current state and retries.

The final state is always correct regardless of which agent runs first.
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
    time.sleep(0.05)    # check warehouse availability

    retries = 0
    while True:
        status = store.get("ORD-7829", "status")
        try:
            store.set("ORD-7829", "reserved", True, updated_by="inventory-agent")
            store.set(
                "ORD-7829", "status", "processing",
                updated_by="inventory-agent",
                expected_version=status.version,    # reject write if status changed
            )
            suffix = f"  (after {retries} retries)" if retries else ""
            log.append(f"[inventory] wrote: reserved=True, status='processing'{suffix}")
            break
        except ConflictError as e:
            retries += 1
            log.append(
                f"[inventory] CONFLICT: status is now v{e.detail.actual_version}"
                f" (written by {e.detail.updated_by}) -- re-reading and retrying"
            )
            time.sleep(0.01)


def pricing_agent() -> None:
    time.sleep(0.05)    # fetch pricing rules

    retries = 0
    new_total: float | None = None
    while True:
        status = store.get("ORD-7829", "status")
        total = store.get("ORD-7829", "total")
        if new_total is None:
            # Compute the discount once from the original price.
            # Keeping new_total fixed across retries ensures the 10% is
            # applied exactly once even if the status write fails and retries.
            new_total = round(total.value * 0.9, 2)
        try:
            store.set("ORD-7829", "discount_applied", True, updated_by="pricing-agent")
            store.set("ORD-7829", "total", new_total, updated_by="pricing-agent")
            store.set(
                "ORD-7829", "status", "awaiting_payment",
                updated_by="pricing-agent",
                expected_version=status.version,    # reject write if status changed
            )
            suffix = f"  (after {retries} retries)" if retries else ""
            log.append(
                f"[pricing]   wrote: discount_applied=True,"
                f" total={new_total}, status='awaiting_payment'{suffix}"
            )
            break
        except ConflictError as e:
            retries += 1
            log.append(
                f"[pricing]   CONFLICT: status is now v{e.detail.actual_version}"
                f" (written by {e.detail.updated_by}) -- re-reading and retrying"
            )
            time.sleep(0.01)


def main() -> None:
    setup_order()

    print("Order ORD-7829 received (every value is versioned from first write):")
    for r in store.list_keys("ORD-7829"):
        print(f"  {r.key}={r.value!r}  (v{r.version})")
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
    print("Final state (each field records who last wrote it):")
    for r in store.list_keys("ORD-7829"):
        print(f"  {r.key}={r.value!r}  (v{r.version}, by {r.updated_by})")

    print()
    print("Audit trail for 'status' -- agenthold records every write:")
    for h in store.history("ORD-7829", "status"):
        print(f"  v{h.version}: {h.value!r}  written by {h.updated_by}")

    reserved = store.get("ORD-7829", "reserved")
    discount = store.get("ORD-7829", "discount_applied")
    total = store.get("ORD-7829", "total")

    assert reserved.value is True, "reserved should be True"
    assert discount.value is True, "discount_applied should be True"
    assert total.value == round(89.99 * 0.9, 2), "total should be discounted"

    print()
    print("RESULT: Both agents completed. No silent overwrites. State is always correct.")


if __name__ == "__main__":
    main()
