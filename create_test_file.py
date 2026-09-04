#!/usr/bin/env python3
TEST_CONTENT = """import unittest
from models import DownloadPriority, DownloadRequest, IBKRContract
from priority_queue import DownloadQueue


class TestPriorityQueueDeduplication(unittest.TestCase):
    def setUp(self):
        self.queue = DownloadQueue()

    def _make_req(self, ticker: str, tf: str = "1D", prio: DownloadPriority = DownloadPriority.STALENESS) -> DownloadRequest:
        contract = IBKRContract(symbol=ticker, exchange="SMART", currency="USD", sec_type="STK")
        return DownloadRequest(ticker=ticker, timeframe=tf, priority=prio, contract=contract)

    def test_deduplicate_identical_requests(self):
        req1 = self._make_req("AAPL", "1D", DownloadPriority.STALENESS)
        req2 = self._make_req("AAPL", "1D", DownloadPriority.STALENESS)
        self.queue.enqueue(req1)
        self.queue.enqueue(req2)

        self.assertEqual(self.queue.size(), 1)
        item = self.queue.dequeue()
        self.assertIsNotNone(item)
        self.assertEqual(item.ticker, "AAPL")
        self.assertIsNone(self.queue.dequeue())
        self.assertEqual(self.queue.size(), 0)

    def test_priority_upgrade(self):
        req_low = self._make_req("AAPL", "1D", DownloadPriority.STALENESS)
        req_high = self._make_req("AAPL", "1D", DownloadPriority.API)

        self.queue.enqueue(req_low)
        self.assertEqual(self.queue.size(), 1)

        self.queue.enqueue(req_high)
        self.assertEqual(self.queue.size(), 1)

        item = self.queue.dequeue()
        self.assertIsNotNone(item)
        self.assertEqual(item.priority, DownloadPriority.API)

        self.assertIsNone(self.queue.dequeue())
        self.assertEqual(self.queue.size(), 0)

    def test_ignore_lower_priority_if_higher_exists(self):
        req_high = self._make_req("AAPL", "1D", DownloadPriority.API)
        req_low = self._make_req("AAPL", "1D", DownloadPriority.STALENESS)

        self.queue.enqueue(req_high)
        self.queue.enqueue(req_low)

        self.assertEqual(self.queue.size(), 1)
        item = self.queue.dequeue()
        self.assertIsNotNone(item)
        self.assertEqual(item.priority, DownloadPriority.API)
        self.assertIsNone(self.queue.dequeue())

    def test_different_timeframes_kept(self):
        req_1d = self._make_req("AAPL", "1D", DownloadPriority.STALENESS)
        req_1w = self._make_req("AAPL", "1W", DownloadPriority.STALENESS)

        self.queue.enqueue(req_1d)
        self.queue.enqueue(req_1w)

        self.assertEqual(self.queue.size(), 2)

    def test_has_higher_priority_waiting(self):
        req_staleness = self._make_req("AAPL", "1D", DownloadPriority.STALENESS)
        req_api = self._make_req("MSFT", "1D", DownloadPriority.API)

        self.queue.enqueue(req_staleness)
        self.assertFalse(self.queue.has_higher_priority_waiting(DownloadPriority.STALENESS))

        self.queue.enqueue(req_api)
        self.assertTrue(self.queue.has_higher_priority_waiting(DownloadPriority.STALENESS))
        self.assertFalse(self.queue.has_higher_priority_waiting(DownloadPriority.API))

        item = self.queue.dequeue()
        self.assertEqual(item.ticker, "MSFT")
        self.assertFalse(self.queue.has_higher_priority_waiting(DownloadPriority.STALENESS))
"""

with open("/home/daniel/stock-data-node/tests/unit/test_priority_queue_dedup.py", "w") as f:
    f.write(TEST_CONTENT)
print("Created test file.")
