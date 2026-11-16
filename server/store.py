import threading

from common.protocol import MAX_KEYS, validate_key, validate_value, ERR_FULL


class Store:
    """In-memory key-value store, safe to share between threads.

    Each key maps to a (value, seq) pair. seq is a sequence number that grows
    with every successful write; replicas use it to tell which write is newer.
    """

    def __init__(self):
        self._data = {}                  # key -> (value, seq)
        self._lock = threading.Lock()    # protects _data and _seq
        self._max_keys = MAX_KEYS
        self._seq = 0                    # last sequence number handed out

    def set(self, key, value):
        """Store value under key and assign it a new sequence number.

        Returns (err, seq):
            (None, seq)     success; seq is the number assigned to this write
            (ERR_..., None) failure (ERR KEY, ERR VALUE or ERR FULL); nothing was written
        """
        # Validation does not touch shared state, so it runs outside the lock
        err = validate_key(key) or validate_value(value)
        if err:
            return err, None

        with self._lock:
            # The limit applies to NEW keys only; an existing key can be updated when full
            if key not in self._data and len(self._data) >= self._max_keys:
                return ERR_FULL, None
            # Check, numbering and write happen under one lock: no two writes share a seq
            self._seq += 1
            self._data[key] = (value, self._seq)
            return None, self._seq

    def apply_replicated(self, key, value, seq):
        """Apply a write that was already numbered by the primary (REPL or SYNC).

        Unlike set(), this does not generate a sequence number; it stores the
        one it is given. The write is applied only if seq is greater than the
        seq already stored for this key, so an old or duplicated message can
        never overwrite newer data.

        Returns:
            None      applied, or ignored because it was not newer
            ERR_FULL  the key is new and the store is full; nothing was written
        """
        with self._lock:
            entry = self._data.get(key)
            if entry is not None:
                _value, stored_seq = entry
                # >= (not >): the same message may arrive twice (REPL and SYNC,
                # or a retried REPL); applying it again must change nothing
                if stored_seq >= seq:
                    return None
            # The limit applies to NEW keys only, same rule as in set()
            if key not in self._data and len(self._data) >= self._max_keys:
                return ERR_FULL

            # Keep the primary's seq, do not touch self._seq: a backup never numbers writes
            self._data[key] = (value, seq)
            return None
            

    def get(self, key):
        """Return the value stored under key, or None if the key does not exist."""
        with self._lock:
            entry = self._data.get(key)
            if entry is None:
                return None
            value, _seq = entry
            return value

    def count(self):
        """Return the number of stored keys."""
        with self._lock:
            return len(self._data)

    def snapshot(self):
        """Return a copy of the store as a list of (seq, key, value), ordered by seq.

        A copy is returned so the caller can iterate without holding the lock
        (used by SYNC and, later, for saving to disk).
        """
        with self._lock:
            entries = [(seq, key, value) for key, (value, seq) in self._data.items()]
        entries.sort()
        return entries

