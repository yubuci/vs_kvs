"""
Key-Value-Store client'ı.

İki mod:
    Tek komut:  python -m client.client --port 5000 SET k1 merhaba dünya
                python -m client.client --port 5000 GET k1
    Shell:      python -m client.client --port 5000        (komut verilmezse)

Client satırı olduğu gibi sunucuya gönderir; doğrulamada son söz sunucunundur.
"""

import argparse
import socket
import sys

from common.protocol import ENCODING, encode_line, read_line

TIMEOUT = 3.0   # saniye; bağlanma ve cevap bekleme için (docs/protokol.md)


class KVError(Exception):
    """Sunucuya ulaşılamadı ya da bağlantı koptu."""


class KVClient:
    """Sunucuyla tek bir TCP bağlantısı üzerinden konuşur.

    Bağlantı ilk komutta açılır ve açık tutulur (shell modunda art arda komut).
    Yük testi ve Seviye 2'deki yönlendirme de bu sınıfı kullanır.
    """

    def __init__(self, host, port, timeout=TIMEOUT):
        self.host = host
        self.port = port
        self.timeout = timeout
        self._sock = None
        self._reader = None

    def connect(self):
        try:
            self._sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        except OSError as e:
            self._sock = None
            raise KVError(f"Sunucuya ulaşılamıyor ({self.host}:{self.port}): {e}") from e
        # Sunucudaki ile aynı çerçeveleme: satırlar yalnızca \n'de bölünür
        self._reader = self._sock.makefile("r", encoding=ENCODING, newline="\n")

    def close(self):
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
        self._sock = None
        self._reader = None

    def send_command(self, line):
        """Bir komut satırı gönderir ve sunucunun cevap satırını döndürür.

        Açık bağlantı kopmuşsa (ör. sunucu yeniden başlatıldı) bir kez yeniden
        bağlanıp komutu tekrar gönderir. SET ve GET tekrarlanınca aynı sonucu
        verdiği için bu güvenlidir. Yine başarısız olursa KVError fırlatır.
        """
        yeniden_dene = self._sock is not None   # yalnızca eski bir bağlantı kopmuşsa
        try:
            return self._send_once(line)
        except KVError:
            if not yeniden_dene:
                raise
        return self._send_once(line)

    def _send_once(self, line):
        if self._sock is None:
            self.connect()
        try:
            self._sock.sendall(encode_line(line))
            response = read_line(self._reader)
        except socket.timeout as e:
            self.close()
            raise KVError(f"Sunucu {self.timeout:g} sn içinde cevap vermedi") from e
        except (OSError, ValueError) as e:
            self.close()
            raise KVError(f"Bağlantı koptu: {e}") from e

        if response is None:              # sunucu bağlantıyı kapattı
            self.close()
            raise KVError("Sunucu bağlantıyı kapattı")
        return response

    # with KVClient(...) as c:  kullanımı için
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def run_once(client, line):
    """Tek komut modu. Çıkış kodu: 0 başarılı, 1 sunucu ERR döndü, 2 bağlantı hatası."""
    try:
        response = client.send_command(line)
    except KVError as e:
        print(f"Hata: {e}", file=sys.stderr)
        return 2
    print(response)
    return 1 if response.startswith("ERR") else 0


def run_shell(client):
    """Etkileşimli mod: 'exit' ya da 'quit' ile çıkılır."""
    print(f"{client.host}:{client.port} sunucusuna bağlanılıyor. Komutlar: SET <key> <value>, GET <key>, STATS, exit")
    try:
        client.connect()
    except KVError as e:
        print(f"Hata: {e}", file=sys.stderr)
        return 2

    while True:
        try:
            line = input("kvs> ")
        except (EOFError, KeyboardInterrupt):   # Ctrl+Z / Ctrl+D / Ctrl+C
            print()
            break

        if line.strip().lower() in ("exit", "quit"):
            break
        if not line.strip():                    # boş satırı göndermeye gerek yok
            continue

        try:
            print(client.send_command(line))
        except KVError as e:
            # Bağlantı kapandı; bir sonraki komutta yeniden bağlanmayı deneyecek
            print(f"Hata: {e}", file=sys.stderr)
    return 0


def main():
    parser = argparse.ArgumentParser(description="Key-Value-Store client'ı")
    parser.add_argument("--host", default="127.0.0.1", help="sunucu adresi (varsayılan: 127.0.0.1)")
    parser.add_argument("--port", type=int, required=True, help="sunucu portu")
    parser.add_argument("command", nargs=argparse.REMAINDER,
                        help="tek komut, ör. SET k1 merhaba; verilmezse shell açılır")
    args = parser.parse_args()

    with KVClient(args.host, args.port) as client:
        if args.command:
            # Value birden fazla argüman olarak gelir; tek boşlukla birleştirilir
            code = run_once(client, " ".join(args.command))
        else:
            code = run_shell(client)
    raise SystemExit(code)


if __name__ == "__main__":
    main()
