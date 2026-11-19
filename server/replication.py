"""
Replication from the primary to its backups.

The primary calls Replicator.replicate() after every successful SET. Each
backup that is "alive" receives a REPL line; a backup that cannot be reached
becomes "behind" and is skipped until it asks for a SYNC (mark_alive).
"""

import logging
import threading

from client.client import KVClient, KVError
from common.protocol import RESP_OK, format_repl

log = logging.getLogger("replication")

TIMEOUT = 1.0   # seconds; a backup that does not answer within this time is marked behind


class Replicator:
    """Sends every write of the primary to its backups (REPL messages)."""

    def __init__(self, peers):
        # peers: list of (host, port) tuples, e.g. [("127.0.0.1", 5001), ("127.0.0.1", 5002)]
        self._status_lock = threading.Lock()     # protects the "status" fields below
        self._peers = {}                         # (host, port) -> {"client": ..., "lock": ..., "status": ...}

        for host, port in peers:
            self._peers[(host, port)] = {
                "client": KVClient(host, port, timeout=TIMEOUT),   # connects lazily, on the first send
                "lock": threading.Lock(),                          # one request/response at a time per backup
                "status": "alive",
            }

    def status(self):
        """Return {(host, port): "alive" | "behind"} for all backups."""
        with self._status_lock:
            result = {}
            for addr, peer in self._peers.items():
                result[addr] = peer["status"]
            return result

    def replicate(self, seq, key, value):
        """Send REPL to every backup that is alive. Never raises.

        Must be called AFTER the store lock was released: this does network I/O.
        """
        line = format_repl(seq, key, value)

        for addr, peer in self._peers.items():
            # Read the status under the status lock, then release it before any network I/O
            with self._status_lock:
                alive = peer["status"] == "alive"
            if not alive:
                continue                            # behind: skip, do not even try

            with peer["lock"]:                      # one request/response at a time on this connection
                try:
                    response = peer["client"].send_command(line)
                except KVError as e:
                    # Unreachable or no answer within the timeout
                    log.warning("backup %s:%s unreachable (%s): marked behind", addr[0], addr[1], e)
                    self._mark_behind(addr)
                    continue
                except Exception:
                    # replicate() must never raise: a backup problem must not break the client's SET
                    log.exception("unexpected error while replicating to %s:%s", addr[0], addr[1])
                    self._mark_behind(addr)
                    continue

            if response != RESP_OK:
                # The backup answered but did not apply the write (e.g. ERR FULL): it has diverged
                log.warning("backup %s:%s answered %r to REPL: marked behind", addr[0], addr[1], response)
                self._mark_behind(addr)

    def mark_alive(self, host, port):
        """Make a backup "alive" again so it receives REPL (called when it starts a SYNC)."""
        addr = (host, port)
        with self._status_lock:
            if addr not in self._peers:
                return                      # unknown backup: ignore instead of crashing
            self._peers[addr]["status"] = "alive"

    def close(self):
        """Close the connections to all backups (server shutdown, end of a test)."""
        for peer in self._peers.values():
            with peer["lock"]:              # wait for a REPL that is still in flight
                peer["client"].close()

    def _mark_behind(self, addr):
        """Set a backup to "behind"; it gets no more REPL until mark_alive() is called."""
        with self._status_lock:
            self._peers[addr]["status"] = "behind"