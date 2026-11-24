"""
TCP server: starts a separate thread for every client connection.

Flow:  read line -> parse (protocol.py) -> apply to Store -> send response line

Roles (Level 2):
    standalone  no --role: Level 1 behaviour
    primary     accepts SET, replicates every write (REPL), serves SYNC to backups
    backup      rejects client SET (ERR NOT_PRIMARY <port>), applies REPL, pulls SYNC at start-up

Sharding (Level 3, --cluster): every key belongs to exactly one server (common/ring.py).
A SET/GET for a key this server does not own is answered with ERR WRONG_NODE <port of the owner>.

Run (from the repository root):
    python -m server.server --port 5000                                   # standalone (Level 1)
    python -m server.server --port 5000 --role primary --peers 127.0.0.1:5001,127.0.0.1:5002
    python -m server.server --port 5001 --role backup --primary 127.0.0.1:5000
    python -m server.server --port 5000 --cluster servers_level3.json [--placement ring|modn] [--vnodes 50]
"""

import argparse
import json
import logging
import socket
import threading

from common.protocol import (
    CMD_GET, CMD_REPL, CMD_SET, CMD_STATS, CMD_SYNC, ENCODING, ERR_UNKNOWN, ERR_VALUE,
    RESP_NOT_FOUND, RESP_OK,
    LineTooLongError, encode_line, format_not_primary, format_stats, format_value,
    parse_line, read_line,
)
from common.config import ConfigError
from common.ring import DEFAULT_VNODES, make_placement, node_id
from server.replication import Replicator
from server.store import Store
from server.sync import pull_sync, serve_sync

# ---------------------------------------------------------------------------------------------
# TODO(Level 3): temporary stand-ins. Remove this whole block once Öğrenci2's changes
# (protocol 1.2: format_wrong_node; config: load_shard_config) are in this branch, and import
# the two functions directly. While the real functions exist, the "try" succeeds and the
# stand-ins below are never used. They follow the contract in level3_gorevler, sections 3.2-3.3.
# ---------------------------------------------------------------------------------------------
try:
    from common.protocol import format_wrong_node
except ImportError:
    def format_wrong_node(port):
        """Stand-in: "ERR WRONG_NODE 5001"."""
        return f"ERR WRONG_NODE {port}"

try:
    from common.config import load_shard_config
except ImportError:
    def load_shard_config(path="servers_level3.json"):
        """Stand-in: read servers_level3.json -> {"placement", "vnodes", "servers": [{"host", "port"}]}."""
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except FileNotFoundError:
            raise ConfigError(f"Config file not found: {path}") from None
        except (OSError, ValueError) as e:
            raise ConfigError(f"Cannot read config file {path}: {e}") from e
        if not isinstance(data, dict) or not isinstance(data.get("servers"), list) or not data["servers"]:
            raise ConfigError(f'Config file {path} must contain a non-empty "servers" list')
        placement = data.get("placement", "ring")
        vnodes = data.get("vnodes", DEFAULT_VNODES)
        if placement not in ("ring", "modn"):
            raise ConfigError(f'{path}: "placement" must be "ring" or "modn", got {placement!r}')
        if isinstance(vnodes, bool) or not isinstance(vnodes, int) or vnodes < 1:
            raise ConfigError(f'{path}: "vnodes" must be an integer >= 1, got {vnodes!r}')
        servers, seen = [], set()
        for i, entry in enumerate(data["servers"]):
            where = f"{path}, server #{i + 1}"
            if not isinstance(entry, dict):
                raise ConfigError(f"{where}: must be an object with host and port")
            if "role" in entry:
                raise ConfigError(f'{where}: has a "role" field; this looks like a Level 2 file (servers.json)')
            host, port = entry.get("host"), entry.get("port")
            if not isinstance(host, str) or not host:
                raise ConfigError(f'{where}: "host" must be a non-empty string')
            if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
                raise ConfigError(f'{where}: "port" must be an integer from 1 to 65535, got {port!r}')
            if (host, port) in seen:
                raise ConfigError(f"{where}: {host}:{port} is listed twice")
            seen.add((host, port))
            servers.append({"host": host, "port": port})
        return {"placement": placement, "vnodes": vnodes, "servers": servers}
# ------------------------------- end of temporary stand-ins ----------------------------------

log = logging.getLogger("server")

ROLE_PRIMARY = "primary"
ROLE_BACKUP = "backup"


class Node:
    """Everything one server process knows about itself.

    role        None (standalone, Level 1 behaviour), "primary" or "backup"
    replicator  Replicator of a primary; None otherwise
    primary     (host, port) of the primary, known to a backup; None otherwise
    placement   Level 3 only: function key -> node_id of the owner; None when not in a cluster
    self_id     Level 3 only: this server's node_id, e.g. "127.0.0.1:5000"
    ports       Level 3 only: node_id -> port, for the port in ERR WRONG_NODE
    """

    def __init__(self, store, role=None, replicator=None, primary=None,
                 placement=None, self_id=None, ports=None):
        self.store = store
        self.role = role
        self.replicator = replicator
        self.primary = primary
        self.placement = placement
        self.self_id = self_id
        self.ports = ports or {}


def cluster_settings(config, host, port, placement=None, vnodes=None):
    """Turn a loaded servers_level3.json into what a sharded Node needs.

    placement / vnodes override the values from the file (used by the measurement scripts).
    Returns (placement_function, self_id, ports, mode, vnodes).
    Raises ValueError if this server is not in the list: it could never own a key.
    """
    mode = placement or config["placement"]
    vnodes = config["vnodes"] if vnodes is None else vnodes
    nodes = [node_id(s["host"], s["port"]) for s in config["servers"]]
    self_id = node_id(host, port)
    if self_id not in nodes:
        raise ValueError(f"this server ({self_id}) is not in the cluster list: {', '.join(nodes)}")
    ports = {node_id(s["host"], s["port"]): s["port"] for s in config["servers"]}
    return make_placement(mode, nodes, vnodes), self_id, ports, mode, vnodes


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
    SYNC is not handled here: its answer has many lines, see handle_client().
    """
    return execute_request(node, parse_line(line))


def execute_request(node, req):
    """Process an already parsed request (see execute)."""
    if not req.ok:
        return req.error                 # ERR KEY / ERR VALUE / ERR UNKNOWN

    if node.placement is not None and req.cmd in (CMD_SET, CMD_GET):
        # Level 3: only the owner stores or reads a key; STATS is not checked (own count only)
        owner = node.placement(req.key)
        if owner != node.self_id:
            return format_wrong_node(node.ports[owner])

    if req.cmd == CMD_SET:
        if node.role == ROLE_BACKUP:
            # Only the primary accepts writes; the client follows this answer to the primary
            return format_not_primary(node.primary[1])
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

    if req.cmd == CMD_REPL:
        if node.role != ROLE_BACKUP:
            return ERR_UNKNOWN           # only a backup accepts REPL
        # Applied only if seq is newer than the stored one; an old or repeated REPL still gets OK
        err = node.store.apply_replicated(req.key, req.value, req.seq)
        return err if err else RESP_OK   # ERR FULL makes the primary mark this backup behind

    # SYNC reaches this point only on a server that is not the primary (see handle_client)
    return ERR_UNKNOWN


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

            req = parse_line(line)
            if req.ok and req.cmd == CMD_SYNC and node.role == ROLE_PRIMARY:
                # Multi-line answer (ENTRY ... END), written to the socket by serve_sync.
                # addr[0] is the backup's host; the port is the one it LISTENS on (from the request).
                serve_sync(conn, node.store, node.replicator, addr[0], req.port)
                continue

            try:
                response = execute_request(node, req)
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
    parser.add_argument("--cluster", default=None, metavar="FILE",
                        help="Level 3: cluster file (servers_level3.json); this server only accepts its own keys")
    parser.add_argument("--placement", choices=["ring", "modn"], default=None,
                        help="with --cluster: overrides the placement from the file")
    parser.add_argument("--vnodes", type=int, default=None,
                        help="with --cluster: overrides the number of virtual nodes from the file")
    args = parser.parse_args()

    # Level 3 options: sharding and replication are combined only in Level 4
    if args.cluster and args.role:
        parser.error("--cluster cannot be combined with --role (sharding + replication is Level 4)")
    if not args.cluster and (args.placement is not None or args.vnodes is not None):
        parser.error("--placement and --vnodes are only valid with --cluster")
    if args.vnodes is not None and args.vnodes < 1:
        parser.error("--vnodes must be at least 1")
    sharding = None
    if args.cluster:
        try:
            config = load_shard_config(args.cluster)
            sharding = cluster_settings(config, args.host, args.port, args.placement, args.vnodes)
        except (ConfigError, ValueError) as e:
            parser.error(str(e))

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
    if sharding:
        node.placement, node.self_id, node.ports, mode, vnodes = sharding
        log.info("cluster: %s, placement %s, vnodes %s, %d servers, this server %s",
                 args.cluster, mode, vnodes, len(node.ports), node.self_id)

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