"""
Key-Value-Store client'ı.

Üç mod:
    Tek komut:  python -m client.client --port 5000 SET k1 merhaba dünya
                python -m client.client --port 5000 GET k1
    Shell:      python -m client.client --port 5000        (komut verilmezse)
    Küme (L2):  python -m client.client --servers servers.json SET k1 merhaba

Client satırı olduğu gibi sunucuya gönderir; doğrulamada son söz sunucunundur.
Küme modunda yalnızca "komut SET mi?" sorusuna bakılır (yönlendirme için).
"""

import argparse
import socket
import sys

from common.config import ConfigError, load_servers, primary_of
from common.protocol import CMD_SET, ENCODING, encode_line, parse_not_primary, read_line

TIMEOUT = 3.0   # saniye; bağlanma ve cevap bekleme için (docs/protokol.md)


class KVError(Exception):
    """Sunucuya ulaşılamadı ya da bağlantı koptu."""


class KVTimeout(KVError):
    """Sunucu zamanında cevap vermedi (bağlantı açıktı ama cevap gelmedi).

    KVError'un alt sınıfı: `except KVError` yazan eski kodlar değişmeden çalışır.
    Ayrı bir sınıf olmasının nedeni send_command()'daki yeniden deneme kuralı (aşağıda).
    """


class KVClient:
    """Sunucuyla tek bir TCP bağlantısı üzerinden konuşur.

    Bağlantı ilk komutta açılır ve açık tutulur (shell modunda art arda komut).
    Yük testi ve Replicator bu sınıfı kullanır. Thread-safe DEĞİLDİR: aynı
    nesneyi birden fazla thread kullanacaksa kilit çağıranın işidir
    (Replicator bunu her backup için ayrı bir kilitle yapar).
    """

    def __init__(self, host, port, timeout=TIMEOUT):
        self.host = host
        self.port = port
        self.timeout = timeout
        self._sock = None
        self._reader = None

    def describe(self):
        """Kullanıcıya gösterilecek kısa ad: 'host:port'."""
        return f"{self.host}:{self.port}"

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

        ZAMAN AŞIMINDA YENİDEN DENEME YOK (KVTimeout): sunucu "kopmuş" değil,
        "cevap vermiyor". Tekrar denemek bekleme süresini ikiye katlardı
        (Replicator'da 1 sn yerine 2 sn) ve takılı kalmış bir sunucuya ikinci
        kez aynı komutu yollardı. Hızlı başarısız olmak daha iyidir.
        """
        yeniden_dene = self._sock is not None   # yalnızca eski bir bağlantı kopmuşsa
        try:
            return self._send_once(line)
        except KVTimeout:
            raise                                # zaman aşımı: tekrar deneme
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
            raise KVTimeout(f"Sunucu {self.timeout:g} sn içinde cevap vermedi") from e
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


class ClusterClient:
    """Level 2: servers.json'daki sunuculara komutları yönlendirir.

    Kurallar (Level 2 planı, bölüm 5):
      SET                       -> primary'ye
      GET, STATS (ve diğerleri) -> listedeki ilk ULAŞILABİLİR sunucuya
      ERR NOT_PRIMARY <port>    -> o porta (cevabı veren sunucuyla aynı host) bağlanıp
                                   komutu BİR KEZ tekrarlar
      bağlantı hatası (okuma)   -> listedeki sıradaki sunucu denenir
      hiçbiri ulaşılamıyor      -> KVError

    Yönlendirme kararı client'tadır (sunucular birbirini bilmeden de çalışır) ve
    yalnızca ilk kelimeye bakar: protokol kuralları (key/value doğrulaması) burada
    TEKRARLANMAZ, son söz yine sunucunundur.

    Primary'ye ulaşılamazsa SET başarısız olur: failover YOK, yeni primary seçilmez.
    Bu Level 2'nin bilinen sınırıdır (Level 5'in gerekçesi). GET ise backup'tan gelir.

    KVClient gibi thread-safe DEĞİLDİR.
    """

    def __init__(self, servers, timeout=TIMEOUT):
        # servers: load_servers() çıktısı. Sıra önemlidir (okuma bu sırayla denenir).
        self._addresses = [(s["host"], s["port"]) for s in servers]
        primary = primary_of(servers)                    # primary yoksa hemen ConfigError
        self._primary_addr = (primary["host"], primary["port"])
        self._timeout = timeout
        # Adres -> KVClient. Bağlantılar tembel açılır ve yeniden kullanılır.
        # Yönlendirme listede olmayan bir port gösterirse onun için de buraya eklenir.
        self._clients = {}

    def describe(self):
        """Kullanıcıya gösterilecek kısa ad: 'N sunucu (host:port, ...)'."""
        return f"{len(self._addresses)} sunucu ({self._format_addresses(self._addresses)})"

    def connect(self):
        """En az bir sunucuya bağlanılabildiğini doğrular (shell açılışındaki erken kontrol)."""
        for addr in self._addresses:
            try:
                self._client_for(addr).connect()
                return
            except KVError:
                continue
        raise KVError("Hiçbir sunucuya ulaşılamıyor: " + self._format_addresses(self._addresses))

    def send_command(self, line):
        """Komutu yönlendirir ve cevap satırını döndürür; ulaşılamazsa KVError fırlatır."""
        if self._is_write(line):
            response, answered_by = self._send_to_primary(line), self._primary_addr
        else:
            response, answered_by = self._send_to_first_reachable(line)

        # Cevap "ERR NOT_PRIMARY <port>" ise tam BİR kez o porta yönlendir.
        # Döngü yok: ikinci cevap yine NOT_PRIMARY ise olduğu gibi döndürülür. İki sunucu
        # birbirini gösterirse (yanlış yapılandırma) sonsuz gidip gelme olmaz.
        port = parse_not_primary(response)
        if port is not None:
            redirect_addr = (answered_by[0], port)
            response = self._ask(redirect_addr, line)
        return response

    def close(self):
        for client in self._clients.values():
            client.close()
        self._clients.clear()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # --- iç yardımcılar --------------------------------------------------------

    @staticmethod
    def _is_write(line):
        """Satır bir SET mi? Yalnızca ilk kelimeye bakar; sunucudaki ayrıştırmayla aynı kural
        (boşlukta böl, büyük/küçük harf fark etmez), ama key/value doğrulaması yapılmaz."""
        return line.split(" ", 1)[0].upper() == CMD_SET

    @staticmethod
    def _format_addresses(addresses):
        return ", ".join(f"{host}:{port}" for host, port in addresses)

    def _client_for(self, addr):
        if addr not in self._clients:
            self._clients[addr] = KVClient(addr[0], addr[1], timeout=self._timeout)
        return self._clients[addr]

    def _ask(self, addr, line):
        return self._client_for(addr).send_command(line)

    def _send_to_primary(self, line):
        try:
            return self._ask(self._primary_addr, line)
        except KVError as e:
            host, port = self._primary_addr
            raise KVError(f"Primary'ye ulaşılamıyor ({host}:{port}); yazma yapılamıyor: {e}") from e

    def _send_to_first_reachable(self, line):
        """Sırayla dener; ilk cevap veren sunucunun (cevap, adres) çiftini döndürür."""
        for addr in self._addresses:
            try:
                return self._ask(addr, line), addr
            except KVError:
                continue                  # bu sunucu cevap vermedi: sıradakine geç
        raise KVError("Hiçbir sunucuya ulaşılamıyor: " + self._format_addresses(self._addresses))


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
    print(f"{client.describe()} sunucusuna bağlanılıyor. Komutlar: SET <key> <value>, GET <key>, STATS, exit")
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
    parser.add_argument("--port", type=int, help="sunucu portu (tek sunucu modu)")
    parser.add_argument("--servers", help="servers.json yolu (küme modu; --port ile birlikte kullanılmaz)")
    parser.add_argument("command", nargs=argparse.REMAINDER,
                        help="tek komut, ör. SET k1 merhaba; verilmezse shell açılır")
    args = parser.parse_args()

    # Tam olarak biri gerekli: --port (Level 1 davranışı) ya da --servers (küme modu)
    if args.servers and args.port is not None:
        parser.error("--servers ve --port birlikte kullanılamaz")
    if not args.servers and args.port is None:
        parser.error("--port ya da --servers gerekli")

    if args.servers:
        try:
            client = ClusterClient(load_servers(args.servers))
        except ConfigError as e:
            print(f"Hata: {e}", file=sys.stderr)       # traceback değil, anlaşılır mesaj
            raise SystemExit(2)
    else:
        client = KVClient(args.host, args.port)

    with client:
        if args.command:
            # Value birden fazla argüman olarak gelir; tek boşlukla birleştirilir
            code = run_once(client, " ".join(args.command))
        else:
            code = run_shell(client)
    raise SystemExit(code)


if __name__ == "__main__":
    main()
