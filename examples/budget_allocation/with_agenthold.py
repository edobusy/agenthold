"""
With Agenthold -- conflict-safe budget allocation.

The same two agents draw from the same $10,000 budget. Each reads the
balance (with its version number) before sleeping. When writing, it passes
expected_version so agenthold can detect if another agent wrote first.

The losing agent receives a ConflictError, re-reads the real remaining
balance, and adjusts its allocation to fit. Total committed always equals
the budget exactly -- no overcommit, no silent data loss.
"""

import threading
import time

from agenthold.exceptions import ConflictError
from agenthold.store import StateStore

TOTAL_BUDGET = 10_000.0
SOCIAL_GOAL  =  8_000.0
SEARCH_GOAL  =  7_000.0

store = StateStore(":memory:")
log: list[str] = []


def header(title: str) -> None:
    width = 62
    print(f"\n{'-' * width}")
    print(f"  {title}")
    print(f"{'-' * width}")


def setup() -> None:
    store.set("campaign", "balance",          TOTAL_BUDGET, updated_by="system")
    store.set("campaign", "social_allocated", 0.0,          updated_by="system")
    store.set("campaign", "search_allocated", 0.0,          updated_by="system")


def social_agent() -> None:
    # Read BEFORE sleeping -- both agents will hold v1 when they wake up
    record = store.get("campaign", "balance")
    time.sleep(0.05)    # analyse social media metrics

    retries = 0
    while True:
        allocation = min(SOCIAL_GOAL, record.value)
        try:
            store.set("campaign", "social_allocated", allocation, updated_by="social-agent")
            store.set(
                "campaign", "balance",
                record.value - allocation,
                updated_by="social-agent",
                expected_version=record.version,
            )
            suffix = f"  (after {retries} retries)" if retries else ""
            log.append(
                f"  social-agent  v{record.version} ${record.value:>9,.2f}"
                f"  =>  allocated ${allocation:>9,.2f}{suffix}"
            )
            break
        except ConflictError as e:
            retries += 1
            log.append(
                f"  social-agent  CONFLICT on v{e.detail.expected_version}"
                f" -- now v{e.detail.actual_version}"
                f" (written by {e.detail.updated_by}) -- re-reading"
            )
            time.sleep(0.01)
            record = store.get("campaign", "balance")


def search_agent() -> None:
    # Read BEFORE sleeping -- both agents will hold v1 when they wake up
    record = store.get("campaign", "balance")
    time.sleep(0.05)    # analyse search campaign data

    retries = 0
    while True:
        allocation = min(SEARCH_GOAL, record.value)
        try:
            store.set("campaign", "search_allocated", allocation, updated_by="search-agent")
            store.set(
                "campaign", "balance",
                record.value - allocation,
                updated_by="search-agent",
                expected_version=record.version,
            )
            suffix = f"  (after {retries} retries)" if retries else ""
            log.append(
                f"  search-agent  v{record.version} ${record.value:>9,.2f}"
                f"  =>  allocated ${allocation:>9,.2f}{suffix}"
            )
            break
        except ConflictError as e:
            retries += 1
            log.append(
                f"  search-agent  CONFLICT on v{e.detail.expected_version}"
                f" -- now v{e.detail.actual_version}"
                f" (written by {e.detail.updated_by}) -- re-reading"
            )
            time.sleep(0.01)
            record = store.get("campaign", "balance")


def main() -> None:
    setup()

    header("CAMPAIGN BUDGET ALLOCATION  (with Agenthold)")
    print(f"  Total budget          ${TOTAL_BUDGET:>9,.2f}")
    print(f"  social-agent goal     ${SOCIAL_GOAL:>9,.2f}")
    print(f"  search-agent goal     ${SEARCH_GOAL:>9,.2f}")
    print(f"  Combined ask          ${SOCIAL_GOAL + SEARCH_GOAL:>9,.2f}  (exceeds budget)")
    balance_r = store.get("campaign", "balance")
    print(f"  Balance stored as v{balance_r.version} in agenthold")

    header("AGENTS START SIMULTANEOUSLY")
    print("  Both agents read balance v1 before sleeping to do their work.")
    print("  Agenthold rejects any write that does not match the current version.\n")

    t1 = threading.Thread(target=social_agent)
    t2 = threading.Thread(target=search_agent)
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    for entry in log:
        print(entry)

    header("BUDGET REPORT")
    social_r  = store.get("campaign", "social_allocated")
    search_r  = store.get("campaign", "search_allocated")
    balance_r = store.get("campaign", "balance")
    committed = social_r.value + search_r.value
    print(f"  social-agent allocated    ${social_r.value:>9,.2f}  (v{social_r.version})")
    print(f"  search-agent allocated    ${search_r.value:>9,.2f}  (v{search_r.version})")
    print(f"                            {'=' * 13}")
    print(f"  Total committed           ${committed:>9,.2f}")
    print(f"  Actual budget             ${TOTAL_BUDGET:>9,.2f}")
    print(f"  Remaining balance         ${balance_r.value:>9,.2f}  (v{balance_r.version})")

    header("ALLOCATION HISTORY  (agenthold audit trail)")
    for h in store.history("campaign", "balance"):
        if h.event_type == "delete":
            print(f"  v{h.version}  [deleted]              by {h.updated_by}")
        else:
            print(f"  v{h.version}  ${h.value:>9,.2f}  written by {h.updated_by}")

    header("RESULT")
    assert committed <= TOTAL_BUDGET, "BUG: budget overcommitted"
    assert abs(balance_r.value + committed - TOTAL_BUDGET) < 0.01, "BUG: balance mismatch"
    print(f"  Both agents completed successfully.")
    print(f"  Total committed:  ${committed:>9,.2f}  of  ${TOTAL_BUDGET:,.2f}")
    print(f"  Remaining:        ${balance_r.value:>9,.2f}")
    print()
    print(f"  The losing agent detected the conflict, re-read the real balance,")
    print(f"  and adjusted its allocation to fit. No overcommit. Every write tracked.")
    print()


if __name__ == "__main__":
    main()
