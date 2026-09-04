#!/usr/bin/env python3
import sys
sys.path.insert(0, "/home/daniel/stock-data-node/src")

from models import DownloadPriority, DownloadRequest, IBKRContract
from priority_queue import DownloadQueue

def make_req(ticker: str, tf: str = "1D", prio: DownloadPriority = DownloadPriority.STALENESS) -> DownloadRequest:
    contract = IBKRContract(symbol=ticker, exchange="SMART", currency="USD", sec_type="STK")
    return DownloadRequest(ticker=ticker, timeframe=tf, priority=prio, contract=contract)

def run_tests():
    q = DownloadQueue()

    # 1. Deduplication
    q.enqueue(make_req("AAPL", "1D", DownloadPriority.STALENESS))
    q.enqueue(make_req("AAPL", "1D", DownloadPriority.STALENESS))
    assert q.size() == 1, f"Expected size 1, got {q.size()}"
    item = q.dequeue()
    assert item.ticker == "AAPL"
    assert q.size() == 0, f"Expected size 0, got {q.size()}"
    assert q.dequeue() is None
    print("Test 1 (Deduplication) PASSED")

    # 2. Priority Upgrade
    q.enqueue(make_req("AAPL", "1D", DownloadPriority.STALENESS))
    assert q.size() == 1
    # Upgrade to API (prio 1)
    q.enqueue(make_req("AAPL", "1D", DownloadPriority.API))
    assert q.size() == 1, f"Expected size 1 after upgrade, got {q.size()}"
    item = q.dequeue()
    assert item.priority == DownloadPriority.API, f"Expected priority API, got {item.priority}"
    assert q.size() == 0
    assert q.dequeue() is None, "Expected no duplicate lower-priority item"
    print("Test 2 (Priority Upgrade) PASSED")

    # 3. Lower priority ignored when higher exists
    q.enqueue(make_req("AAPL", "1D", DownloadPriority.API))
    q.enqueue(make_req("AAPL", "1D", DownloadPriority.STALENESS))
    assert q.size() == 1
    item = q.dequeue()
    assert item.priority == DownloadPriority.API
    assert q.dequeue() is None
    print("Test 3 (Lower priority ignored) PASSED")

    # 4. Different timeframes kept
    q.enqueue(make_req("AAPL", "1D", DownloadPriority.STALENESS))
    q.enqueue(make_req("AAPL", "1W", DownloadPriority.STALENESS))
    assert q.size() == 2
    q.dequeue()
    q.dequeue()
    assert q.size() == 0
    print("Test 4 (Different timeframes) PASSED")

    # 5. has_higher_priority_waiting
    q.enqueue(make_req("AAPL", "1D", DownloadPriority.STALENESS))
    assert not q.has_higher_priority_waiting(DownloadPriority.STALENESS)
    q.enqueue(make_req("MSFT", "1D", DownloadPriority.API))
    assert q.has_higher_priority_waiting(DownloadPriority.STALENESS)
    assert not q.has_higher_priority_waiting(DownloadPriority.API)
    item = q.dequeue()
    assert item.ticker == "MSFT"
    assert not q.has_higher_priority_waiting(DownloadPriority.STALENESS)
    print("Test 5 (has_higher_priority_waiting) PASSED")

    print("\nALL 5 TESTS PASSED SUCCESSFULLY!")

if __name__ == "__main__":
    run_tests()
