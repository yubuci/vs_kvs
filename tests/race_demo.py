"""
Race condition demo: why Store needs a lock (material for the report).

300 threads insert 300 different new keys at the same time into a store that
may hold at most 256 entries. Three variants are compared:

  1. Store                   the real store: check and write under one lock
  2. UnsafeStore             the same logic WITHOUT a lock
  3. UnsafeStore (widened)   no lock, and the thread gives up the CPU between
                             the check and the write; this makes the race
                             window wider so the error shows up reliably

Expected: variant 1 always ends with exactly 256 entries. Variants 2 and 3 can
end with more, because two threads may both see "255 entries, there is room"
before either of them has written.

This is a demonstration, not a unit test: its file name does not start with
"test_", so `unittest discover` does not run it.

Run (from the repository root):
    python -m tests.race_demo
    python -m tests.race_demo --runs 200 --threads 300
"""

import argparse
import threading
import time

from common.protocol import ERR_FULL, MAX_KEYS
from server.store import Store


class UnsafeStore:
    """Deliberately broken copy of Store.set(): the same steps, but no lock."""

    def __init__(self, widen=False):
        self._data = {}
        self._max_keys = MAX_KEYS
        self._widen = widen

    def set(self, key, value):
        # Step 1: check ("is there room?")
        if key not in self._data and len(self._data) >= self._max_keys:
            return ERR_FULL, None
        if self._widen:
            time.sleep(0)             # let another thread run between check and write
        # Step 2: write. Another thread may have filled the last slot in between.
        self._data[key] = value
        return None, None

    def count(self):
        return len(self._data)


def run_once(make_store, n_threads):
    """Let n_threads insert different new keys at the same moment; return the final entry count."""
    store = make_store()
    barrier = threading.Barrier(n_threads)        # release all threads together

    def work(i):
        barrier.wait()
        store.set(f"k{i}", "v")

    threads = [threading.Thread(target=work, args=(i,)) for i in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return store.count()


def measure(name, make_store, runs, n_threads):
    counts = [run_once(make_store, n_threads) for _ in range(runs)]
    over = sum(1 for c in counts if c > MAX_KEYS)
    print(f"{name:<24} {over:>4}/{runs:<5} {100 * over / runs:>5.1f} %   {min(counts):>4}   {max(counts):>4}")
    return over


def main():
    parser = argparse.ArgumentParser(description="Race condition demo: store with and without a lock")
    parser.add_argument("--runs", type=int, default=100, help="repetitions per variant (default: 100)")
    parser.add_argument("--threads", type=int, default=300, help="concurrent writers per run (default: 300)")
    args = parser.parse_args()

    print(f"{args.threads} threads insert {args.threads} new keys at once; limit = {MAX_KEYS} entries\n")
    print(f"{'variant':<24} {'runs > 256':>10} {'share':>8}   {'min':>4}   {'max':>4}")
    locked_over = measure("Store (with lock)", Store, args.runs, args.threads)
    measure("UnsafeStore", UnsafeStore, args.runs, args.threads)
    measure("UnsafeStore (widened)", lambda: UnsafeStore(widen=True), args.runs, args.threads)

    # The real store must never exceed the limit; a non-zero exit code makes a regression visible
    raise SystemExit(1 if locked_over else 0)


if __name__ == "__main__":
    main()