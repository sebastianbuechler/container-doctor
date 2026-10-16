"""Reads have connections of their own, so the dashboard never queues behind
a write: a slow commit or a prune held every request on the page."""

import os
import sqlite3
import tempfile
import threading
import time
import unittest

from app.store import Store


class ReadsDoNotWaitForWrites(unittest.TestCase):
    def setUp(self):
        self.store = Store(os.path.join(tempfile.mkdtemp(), "doctor.db"))
        self.addCleanup(self.store.close)
        self.store.record_llm_call({"purpose": "diagnosis"})

    def test_a_read_goes_ahead_while_a_write_holds_the_lock(self):
        held, release = threading.Event(), threading.Event()

        def slow_write():
            with self.store._lock:
                held.set()
                release.wait(5)

        writer = threading.Thread(target=slow_write)
        writer.start()
        self.addCleanup(writer.join)
        self.addCleanup(release.set)
        self.assertTrue(held.wait(5))
        started = time.monotonic()
        self.assertEqual(self.store.count_llm_calls(), 1)
        self.assertLess(time.monotonic() - started, 1.0)

    def test_a_write_is_seen_by_a_read_on_another_thread_at_once(self):
        self.assertEqual(self.store.count_llm_calls(), 1)  # this thread's reader is open
        seen = []
        other = threading.Thread(target=lambda: seen.append(self.store.count_llm_calls()))
        other.start()
        other.join()
        self.store.record_llm_call({"purpose": "classifier_signal"})
        self.assertEqual(self.store.count_llm_calls(), 2)
        self.assertEqual(self.store.count_llm_calls("classifier"), 1)
        self.assertEqual(seen, [1])

    def test_read_connections_cannot_write(self):
        with self.store._read() as conn:
            with self.assertRaises(sqlite3.OperationalError):
                conn.execute("DELETE FROM llm_calls")

    def test_closing_the_store_closes_every_read_connection(self):
        threads = [threading.Thread(target=self.store.count_llm_calls) for _ in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        readers = list(self.store._readers)
        self.assertEqual(len(readers), 3)
        self.store.close()
        for conn in readers:
            with self.assertRaises(sqlite3.ProgrammingError):
                conn.execute("SELECT 1")


class InMemory(unittest.TestCase):
    def test_reads_share_the_one_connection_an_in_memory_database_has(self):
        store = Store(":memory:")
        self.addCleanup(store.close)
        store.record_llm_call({"purpose": "diagnosis"})
        self.assertEqual(store.count_llm_calls(), 1)
        self.assertEqual(store._readers, [])


if __name__ == "__main__":
    unittest.main()
