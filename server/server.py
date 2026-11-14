"""
TCP sunucusu: her client bağlantısı için ayrı bir thread açar.

Akış:  satır oku -> ayrıştır (protocol.py) -> Store'a uygula -> cevap satırını gönder

Çalıştırma (depo kökünden):
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
    """Tek bir istek satırını işler ve cevap satırını (sonunda \\n olmadan) döndürür.

    Ağdan bağımsızdır; bu yüzden soket olmadan da test edilebilir.
    """
    req = parse_line(line)
    if not req.ok:
        return req.error                 # ERR KEY / ERR VALUE / ERR UNKNOWN

    if req.cmd == CMD_SET:
        err = store.set(req.key, req.value)
        return err if err else RESP_OK   # err burada yalnızca ERR FULL olabilir

    if req.cmd == CMD_GET:
        value = store.get(req.key)
        return RESP_NOT_FOUND if value is None else format_value(value)

    if req.cmd == CMD_STATS:
        return format_stats(store.count())

    return ERR_UNKNOWN                   # buraya normalde ulaşılmaz


def handle_client(conn, addr, store):
    """Bir client bağlantısına hizmet eder. Her bağlantı için ayrı bir thread'de çalışır."""
    log.info("baglandi: %s:%s", addr[0], addr[1])
    try:
        # Soketi dosya gibi sar: readline() TCP bayt akışından tam satırlar çıkarır (framing).
        # newline="\n": satırlar yalnızca \n'de bölünür, \r çevrilmez.
        reader = conn.makefile("r", encoding=ENCODING, newline="\n")
        while True:
            try:
                line = read_line(reader)
            except LineTooLongError:
                # Protokol: çok uzun satırda ERR VALUE gönder ve bağlantıyı kapat
                conn.sendall(encode_line(ERR_VALUE))
                break
            except UnicodeDecodeError:
                # Geçerli UTF-8 olmayan baytlar: akış artık güvenilir değil, kapat
                conn.sendall(encode_line(ERR_UNKNOWN))
                break

            if line is None:             # client bağlantıyı kapattı
                break

            try:
                response = execute(store, line)
            except Exception:            # beklenmeyen hata sunucuyu düşürmemeli
                log.exception("istek islenirken hata")
                response = ERR_UNKNOWN

            # send() verinin tamamını göndermeyebilir; sendall() hepsini gönderir
            conn.sendall(encode_line(response))
    except (ConnectionError, OSError) as e:
        # Client aniden koptu (ör. Ctrl+C): yalnızca bu thread biter, sunucu çalışmaya devam eder
        log.info("baglanti koptu: %s:%s (%s)", addr[0], addr[1], e)
    finally:
        conn.close()
        log.info("ayrildi: %s:%s", addr[0], addr[1])


def serve(host, port, store):
    """Dinleyen soketi açar ve Ctrl+C gelene kadar bağlantı kabul eder."""
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    # Sunucu yeniden başlatılınca "Address already in use" hatası almamak için (bind'dan ÖNCE)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((host, port))
    srv.listen(512)                      # bekleme kuyruğu: 300 client aynı anda bağlanabilsin
    # accept() en geç 1 sn'de bir döner; Windows'ta Ctrl+C ancak böyle yakalanır
    srv.settimeout(1.0)
    log.info("dinleniyor: %s:%s", host, port)

    try:
        while True:
            try:
                conn, addr = srv.accept()
            except socket.timeout:
                continue                 # kimse bağlanmadı, döngüye devam
            conn.settimeout(None)        # client soketinde timeout yok (shell boşta bekleyebilir)
            # daemon=True: ana program kapanınca açık bağlantılar programı bekletmez
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

    # Log satırlarının başına port yazılır; birden fazla sunucunun çıktısı ayırt edilebilsin
    logging.basicConfig(level=logging.INFO, format=f"%(asctime)s [{args.port}] %(message)s", datefmt="%H:%M:%S")

    try:
        serve(args.host, args.port, Store())
    except OSError as e:
        # Örn. port başka bir program tarafından kullanılıyor
        log.error("sunucu baslatilamadi: %s", e) #T0DO türkçeden çevrilecek
        raise SystemExit(1)


if __name__ == "__main__":
    main()
