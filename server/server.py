"""
TCP server: starts a separate thread for every client connection.

Flow:  read line -> parse (protocol.py) -> apply to Store -> send response line

Run (from the repository root):
    python -m server.server --port 5000
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
from server.store import Store

log = logging.getLogger("server")


def execute(store, line):
    """Process a single request line and return the response line (without trailing \\n).

    Independent of the network, so it can be tested without a socket.
    """
    req = parse_line(line)
    if not req.ok:
        return req.error                 # ERR KEY / ERR VALUE / ERR UNKNOWN

    if req.cmd == CMD_SET:
        # set() returns (err, seq); seq is used for replication from Level 2 on
        err, seq = store.set(req.key, req.value)
        return err if err else RESP_OK

    if req.cmd == CMD_GET:
        value = store.get(req.key)
        return RESP_NOT_FOUND if value is None else format_value(value)

    if req.cmd == CMD_STATS:
        return format_stats(store.count())

    return ERR_UNKNOWN                   # normally unreachable


def handle_client(conn, addr, store):
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
                response = execute(store, line)
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


def serve(host, port, store):
    """Open the listening socket and accept connections until Ctrl+C."""
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    # Avoid "Address already in use" when the server is restarted (must be set BEFORE bind)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((host, port))
    srv.listen(512)                      # backlog: lets 300 clients connect at the same time
    # accept() returns after 1 s at the latest; on Windows this is the only way Ctrl+C is noticed
    srv.settimeout(1.0)
    log.info("dinleniyor: %s:%s", host, port)

    try:
        while True:
            try:
                conn, addr = srv.accept()
            except socket.timeout:
                continue                 # nobody connected, keep looping
            conn.settimeout(None)        # no timeout on the client socket (a shell may sit idle)
            # daemon=True: open connections do not keep the program alive at shutdown
            t = threading.Thread(target=handle_client, args=(conn, addr, store), daemon=True)
            t.start()
    except KeyboardInterrupt:
        log.info("kapatiliyor (Ctrl+C)")
    finally:
        srv.close()


def main():
    parser = argparse.ArgumentParser(description="Key-Value-Store sunucusu")
    parser.add_argument("--host", default="127.0.0.1", help="dinlenecek adres (varsayılan: 127.0.0.1)")
    parser.add_argument("--port", type=int, required=True, help="dinlenecek port")
    args = parser.parse_args()

    # Each log line starts with the port so the output of several servers can be told apart
    logging.basicConfig(level=logging.INFO, format=f"%(asctime)s [{args.port}] %(message)s", datefmt="%H:%M:%S")

    try:
        serve(args.host, args.port, Store())
    except OSError as e:
        # E.g. the port is already in use by another program
        log.error("sunucu baslatilamadi: %s", e)  # TODO: translate log and help texts from Turkish
        raise SystemExit(1)


if __name__ == "__main__":
    main()