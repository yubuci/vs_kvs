"""
TCP server: starts a separate thread for every client connection.

Flow:  read line -> parse (protocol.py) -> apply to Store -> send response line

Run (from the repository root):
    python -m server.server --port 5000                                   # standalone (Level 1)
    python -m server.server --port 5000 --role primary --peers 127.0.0.1:5001,127.0.0.1:5002
    python -m server.server --port 5001 --role backup --primary 127.0.0.1:5000
"""

import argparse
import logging
import socket
import threading

from common.protocol import (
    CMD_GET, CMD_SET, CMD_STATS, ENCODING, ERR_UNKNOWN, ERR_VALUE,
    RESP_NOT_FOUND, RESP_OK,
    LineTooLongError, encode_line, format_stats, format_value,
    parse_line, read_line,
)
from server.replication import Replicator
from server.store import Store

# server/sync.py belongs to another branch (level2-sync). Until it is merged the server
# starts without SYNC; once the file exists the calls below become active automatically.
# TODO: turn this into a plain import when sync.py is on main.
try:
    from server.sync import pull_sync
except ImportError:
    pull_sync = None

log = logging.getLogger("server")

ROLE_PRIMARY = "primary"
ROLE_BACKUP = "backup"


class Node:
    """Everything one server process knows about itself.

    role        None (standalone, Level 1 behaviour), "primary" or "backup"
    replicator  Replicator of a primary; None otherwise
    primary     (host, port) of the primary, known to a backup; None otherwise
    """

    def __init__(self, store, role=None, replicator=None, primary=None):
        self.store = store
        self.role = role
        self.replicator = replicator
        self.primary = primary


def parse_addr(text):
    """Turn "127.0.0.1:5001" into ("127.0.0.1", 5001). Raises ValueError if malformed."""
    host, port = text.rsplit(":", 1)        # split at the LAST colon
    return host, int(port)                  # port as int: the Replicator keys use ints


def parse_peers(text):
    """Turn "h1:p1,h2:p2" into [(h1, p1), (h2, p2)]. Empty text gives an empty list."""
    peers = []
    for part in text.split(","):
        if part.strip():                    # skip empty pieces, e.g. from a trailing comma
            peers.append(parse_addr(part.strip()))
    return peers


def execute(node, line):
    """Process a single request line and return the response line (without trailing \\n).

    Independent of the network, so it can be tested without a socket.
    """
    req = parse_line(line)
    if not req.ok:
        return req.error                 # ERR KEY / ERR VALUE / ERR UNKNOWN

    if req.cmd == CMD_SET:
        # TODO (B2.4): a backup must answer ERR NOT_PRIMARY <port> here (needs protocol.py 1.1)
        err, seq = node.store.set(req.key, req.value)
        if err:
            return err                   # nothing was written, so nothing is replicated
        if node.role == ROLE_PRIMARY:
            # The store lock is already released here: replicate() does network I/O.
            # Synchronous replication: the client gets OK only after the reachable backups answered.
            node.replicator.replicate(seq, req.key, req.value)
        return RESP_OK

    if req.cmd == CMD_GET:
        value = node.store.get(req.key)
        return RESP_NOT_FOUND if value is None else format_value(value)

    if req.cmd == CMD_STATS:
        return format_stats(node.store.count())

    return ERR_UNKNOWN                   # normally unreachable


def handle_client(conn, addr, node):
    """Serve one client connection. Runs in its own thread, one per connection."""
    log.info("baglandi: %s:%s", addr[0], addr[1])
    try:
        # Wrap the socket like a file: readline() extracts complete lines from the TCP byte stream (framing).
        # newline="\n": lines are split only at \n, \r is not translated.
        reader = conn.makefile("r", encoding=ENCODING, newline="\n")
        while True:
            try:
                line = read_line(reader)
            except LineTooLongError:
                # Protocol: on an over-long line send ERR VALUE and close the connection
                conn.sendall(encode_line(ERR_VALUE))
                break
            except UnicodeDecodeError:
                # Bytes that are not valid UTF-8: the stream can no longer be trusted, close it
                conn.sendall(encode_line(ERR_UNKNOWN))
                break

            if line is None:             # client closed the connection
                break

            try:
                response = execute(node, line)
            except Exception:            # an unexpected error must not bring the server down
                log.exception("istek islenirken hata")
                response = ERR_UNKNOWN

            # send() may transmit only part of the data; sendall() sends all of it
            conn.sendall(encode_line(response))
    except (ConnectionError, OSError) as e:
        # Client disconnected abruptly (e.g. Ctrl+C): only this thread ends, the server keeps running
        log.info("baglanti koptu: %s:%s (%s)", addr[0], addr[1], e)
    finally:
        conn.close()
        log.info("ayrildi: %s:%s", addr[0], addr[1])


def sync_from_primary(node, my_port):
    """Backup start-up: fetch the whole store from the primary. Runs in its own thread.

    Must run while the server is ALREADY accepting connections: the primary marks this
    backup alive at the start of the SYNC and may send REPL immediately. If the backup
    were not listening yet, the primary would time out and mark it behind again.
    """
    if pull_sync is None:
        log.warning("server/sync.py is not available yet: starting without SYNC")
        return
    host, port = node.primary
    try:
        count = pull_sync(host, port, my_port, node.store)
        log.info("SYNC finished: %s entries received from %s:%s", count, host, port)
    except Exception:                    # pull_sync should not raise; a failed SYNC must not stop the backup
        log.exception("SYNC with %s:%s failed", host, port)


def serve(host, port, node, on_ready=None, stop_event=None):
    """Open the listening socket and accept connections until Ctrl+C (or stop_event is set).

    on_ready    optional function, started in its own thread as soon as the socket is
                listening, so it runs while connections are already being accepted
                (a backup uses it for its start-up SYNC)
    stop_event  optional threading.Event; setting it ends the loop within about 1 s
                (used by tests; a normal server is stopped with Ctrl+C)
    """
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    # Avoid "Address already in use" when the server is restarted (must be set BEFORE bind)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((host, port))
    srv.listen(512)                      # backlog: lets 300 clients connect at the same time
    # accept() returns after 1 s at the latest; on Windows this is the only way Ctrl+C is noticed
    srv.settimeout(1.0)
    log.info("dinleniyor: %s:%s", host, port)

    if on_ready is not None:
        # From here on connections are queued by the OS and accepted by the loop below,
        # so on_ready may already cause other servers to connect to us
        threading.Thread(target=on_ready, daemon=True).start()

    try:
        while stop_event is None or not stop_event.is_set():
            try:
                conn, addr = srv.accept()
            except socket.timeout:
                continue                 # nobody connected, keep looping
            conn.settimeout(None)        # no timeout on the client socket (a shell may sit idle)
            # daemon=True: open connections do not keep the program alive at shutdown
            t = threading.Thread(target=handle_client, args=(conn, addr, node), daemon=True)
            t.start()
    except KeyboardInterrupt:
        log.info("kapatiliyor (Ctrl+C)")
    finally:
        srv.close()


def main():
    parser = argparse.ArgumentParser(description="Key-Value-Store sunucusu")
    parser.add_argument("--host", default="127.0.0.1", help="dinlenecek adres (varsayılan: 127.0.0.1)")
    parser.add_argument("--port", type=int, required=True, help="dinlenecek port")
    parser.add_argument("--role", choices=[ROLE_PRIMARY, ROLE_BACKUP], default=None,
                        help="replication role; omit for a standalone server (Level 1 behaviour)")
    parser.add_argument("--peers", default="",
                        help="primary only: backups as host:port,host:port")
    parser.add_argument("--primary", default=None,
                        help="backup only: address of the primary as host:port")
    args = parser.parse_args()

    # Turn the address texts into (host, port) tuples; reject bad input before opening any socket
    try:
        peers = parse_peers(args.peers)
        primary = parse_addr(args.primary) if args.primary else None
    except ValueError:
        parser.error("addresses must look like host:port, e.g. 127.0.0.1:5001")
    if args.role == ROLE_BACKUP and primary is None:
        parser.error("--role backup requires --primary host:port")
    if args.role != ROLE_BACKUP and primary is not None:
        parser.error("--primary is only valid with --role backup")
    if args.role != ROLE_PRIMARY and peers:
        parser.error("--peers is only valid with --role primary")

    # Each log line starts with the port so the output of several servers can be told apart
    logging.basicConfig(level=logging.INFO, format=f"%(asctime)s [{args.port}] %(message)s", datefmt="%H:%M:%S")

    # Only a primary replicates; a primary without --peers simply has no backups
    replicator = Replicator(peers) if args.role == ROLE_PRIMARY else None
    node = Node(Store(), role=args.role, replicator=replicator, primary=primary)
    if args.role:
        log.info("role: %s, peers: %s, primary: %s", args.role, peers, primary)

    # A backup fetches the store from the primary right after it started listening
    on_ready = None
    if args.role == ROLE_BACKUP:
        def on_ready():
            sync_from_primary(node, args.port)

    try:
        serve(args.host, args.port, node, on_ready=on_ready)
    except OSError as e:
        # E.g. the port is already in use by another program
        log.error("sunucu baslatilamadi: %s", e)  # TODO: translate log and help texts from Turkish
        raise SystemExit(1)
    finally:
        if replicator is not None:
            replicator.close()


if __name__ == "__main__":
    main()