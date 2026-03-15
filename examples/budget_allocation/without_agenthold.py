"""
Without Agenthold -- the silent overcommit problem.

Two AI agents draw from the same $10,000 marketing budget simultaneously.
Both read the balance before doing their work, then each writes back its
own allocation. Neither sees the other's write. The budget is silently
overcommitted -- no error, no warning.
"""

import threading
import time

TOTAL_BUDGET = 10_000.0
SOCIAL_GOAL  =  8_000.0   # social-agent wants to spend this much
SEARCH_GOAL  =  7_000.0   # search-agent wants to spend this much

budget: dict = {
    "balance":          TOTAL_BUDGET,
    "social_allocated": 0.0,
    "search_allocated": 0.0,
}
log: list[str] = []


def header(title: str) -> None:
    width = 62
    print(f"\n{'-' * width}")
    print(f"  {title}")
    print(f"{'-' * width}")


def social_agent() -> None:
    balance = budget["balance"]          # snapshot before doing work
    time.sleep(0.05)                     # analyse social media metrics

    allocation = min(SOCIAL_GOAL, balance)
    budget["balance"] = balance - allocation    # write back based on stale snapshot
    budget["social_allocated"] = allocation
    log.append(
        f"  social-agent  read ${balance:>9,.2f}  =>  allocated ${allocation:>9,.2f}"
    )


def search_agent() -> None:
    balance = budget["balance"]          # snapshot before doing work
    time.sleep(0.05)                     # analyse search campaign data

    allocation = min(SEARCH_GOAL, balance)
    budget["balance"] = balance - allocation    # write back based on stale snapshot
    budget["search_allocated"] = allocation
    log.append(
        f"  search-agent  read ${balance:>9,.2f}  =>  allocated ${allocation:>9,.2f}"
    )


def main() -> None:
    header("CAMPAIGN BUDGET ALLOCATION  (without Agenthold)")
    print(f"  Total budget          ${TOTAL_BUDGET:>9,.2f}")
    print(f"  social-agent goal     ${SOCIAL_GOAL:>9,.2f}")
    print(f"  search-agent goal     ${SEARCH_GOAL:>9,.2f}")
    print(f"  Combined ask          ${SOCIAL_GOAL + SEARCH_GOAL:>9,.2f}  (already over budget!)")

    header("AGENTS START SIMULTANEOUSLY")
    print("  Both agents snapshot the balance before sleeping to do their work.")
    print("  Neither knows the other is running.\n")

    t1 = threading.Thread(target=social_agent)
    t2 = threading.Thread(target=search_agent)
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    for entry in log:
        print(entry)

    header("BUDGET REPORT")
    committed = budget["social_allocated"] + budget["search_allocated"]
    print(f"  social-agent allocated    ${budget['social_allocated']:>9,.2f}")
    print(f"  search-agent allocated    ${budget['search_allocated']:>9,.2f}")
    print(f"                            {'=' * 13}")
    print(f"  Total committed           ${committed:>9,.2f}")
    print(f"  Actual budget             ${TOTAL_BUDGET:>9,.2f}")
    print(f"  Balance (dict says)       ${budget['balance']:>9,.2f}")

    header("PROBLEM")
    overcommit = committed - TOTAL_BUDGET
    print(f"  Both agents finished without raising any error.")
    print(f"  Total committed:  ${committed:>9,.2f}")
    print(f"  Actual budget:    ${TOTAL_BUDGET:>9,.2f}")
    print(f"  Overcommit:       ${overcommit:>9,.2f}  <-- silent data corruption")
    print()
    print(f"  The dict balance is wrong -- whoever wrote last won.")
    print(f"  Run this again -- you may see a different overcommit amount.")
    print()


if __name__ == "__main__":
    main()
