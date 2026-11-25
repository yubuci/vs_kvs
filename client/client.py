"""
Key-Value-Store client'ı.

Dört mod:
    Tek komut:  python -m client.client --port 5000 SET k1 merhaba dünya
                python -m client.client --port 5000 GET k1
    Shell:      python -m client.client --port 5000        (komut verilmezse)
    Küme (L2):  python -m client.client --servers servers.json SET k1 merhaba
    Shard (L3): python -m client.client --cluster servers_level3.json SET k1 merhaba
                python -m client.client --cluster servers_level3.json --placement modn GET k1

Client satırı olduğu gibi sunucuya gönderir; doğrulamada son söz sunucunundur.
Küme modunda yalnızca "komut SET mi?" sorusuna bakılır (yönlendirme için).
"""

import argparse
import socket
import sys

from common.config import VALID_PLACEMENTS, ConfigError, load_servers, load_shard_config, primary_of
from common.protocol import (
    CMD_GET, CMD_SET, CMD_STATS, ENCODING, RESP_STATS, encode_line, format_stats,
    parse_not_primary, parse_wrong_node, read_line,
)
from common.ring import DEFAULT_VNODES, make_placement, node_id

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
        # The reader made by makefile() holds its OWN reference to the socket: socket.close()
        # alone only marks the socket closed, the file descriptor stays open until the reader is
        # closed too (or garbage collected, which raises a ResourceWarning). So close both.
        for resource in (self._reader, self._sock):
            if resource is not None:
                try:
                    resource.close()
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


class ShardedClient:
    """Level 3: sends every command to the server that OWNS the key.

    Rules:
      SET / GET <key>      -> the owner, chosen by make_placement(...)(key)
      ERR WRONG_NODE <port>-> the same host, that port; the command is repeated ONCE
      STATS                -> asked from EVERY server; the answer is "STATS <total>"
      anything else        -> the FIRST server in the list (no key, nothing to route by:
                              the server's own validation gives the ERR answer)

    Placement is decided here, in the client (like Level 2's routing). A server that does not
    own a key answers WRONG_NODE, so a client with an old or different config is corrected
    instead of silently writing to the wrong place.

    There is NO failover: if the owner is down, the command fails with KVError. That is the
    known limit of Level 3 (the reason for Level 4's replication).

    The routing code never validates keys or values: the last word belongs to the server.
    Like KVClient, this class is NOT thread-safe.
    """

    def __init__(self, servers, placement="ring", vnodes=DEFAULT_VNODES, timeout=TIMEOUT):
        # servers: the "servers" list of load_shard_config(); the ORDER is kept.
        # node_id() is the only place that spells a node ("127.0.0.1:5000"): ring and client agree.
        self._addresses = {}                              # node id -> (host, port), in config order
        for server in servers:
            self._addresses[node_id(server["host"], server["port"])] = (server["host"], server["port"])
        if len(self._addresses) != len(servers):
            raise ValueError("the same server is listed twice")
        # An empty list, an unknown mode or bad vnodes make make_placement raise ValueError
        self._place = make_placement(placement, list(self._addresses), vnodes)
        self._placement = placement
        self._timeout = timeout
        self._clients = {}                                # (host, port) -> KVClient, opened lazily

    def describe(self):
        """Short name for the user: 'N sunucu, ring (host:port, ...)'."""
        return f"{len(self._addresses)} sunucu, {self._placement} ({', '.join(self._addresses)})"

    def connect(self):
        """Check that at least one server is reachable (the early check when the shell opens)."""
        for addr in self._addresses.values():
            try:
                self._client_for(addr).connect()
                return
            except KVError:
                continue
        raise KVError("Hiçbir sunucuya ulaşılamıyor: " + ", ".join(self._addresses))

    def send_command(self, line):
        """Route the command and return the answer line; raise KVError if it cannot be done."""
        parts = line.split(" ", 2)                        # same split as common.protocol.parse_line
        cmd = parts[0].upper()

        if cmd == CMD_STATS and len(parts) == 1:
            return self._total_stats()

        if cmd in (CMD_SET, CMD_GET) and len(parts) >= 2 and parts[1]:
            owner = self._addresses[self._place(parts[1])]
            return self._send_to_owner(owner, line)

        # No key to route by (or a command that is not routed): any server gives the right ERR
        first = next(iter(self._addresses.values()))
        return self._ask(first, line)

    def stats_per_server(self):
        """{"127.0.0.1:5000": 87, ...} in config order; None for a server with no usable answer."""
        counts = {}
        for nid, addr in self._addresses.items():
            try:
                counts[nid] = self._parse_stats(self._ask(addr, CMD_STATS))
            except KVError:
                counts[nid] = None
        return counts

    def close(self):
        for client in self._clients.values():
            client.close()
        self._clients.clear()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # --- internal helpers ----------------------------------------------------------

    def _client_for(self, addr):
        if addr not in self._clients:
            self._clients[addr] = KVClient(addr[0], addr[1], timeout=self._timeout)
        return self._clients[addr]

    def _ask(self, addr, line):
        return self._client_for(addr).send_command(line)

    def _send_to_owner(self, owner, line):
        try:
            response = self._ask(owner, line)
        except KVError as e:
            raise KVError(f"Key'in sahibi {node_id(*owner)} sunucusuna ulaşılamıyor "
                          f"(failover yok): {e}") from e

        port = parse_wrong_node(response)
        if port is None:
            return response                               # the normal case: no redirect

        # The server says another port owns the key: ask THAT one, exactly once.
        redirect = (owner[0], port)
        try:
            response = self._ask(redirect, line)
        except KVError as e:
            raise KVError(f"{node_id(*owner)} key'in {node_id(*redirect)} sunucusunda "
                          f"olduğunu söyledi ama oraya ulaşılamıyor: {e}") from e
        if parse_wrong_node(response) is not None:
            # No loop: two servers that point at each other would bounce the command forever.
            # It also means the servers and this client use DIFFERENT configs (a different ring).
            raise KVError(f"Sunucular key'in sahibi konusunda anlaşamıyor ({node_id(*owner)} ve "
                          f"{node_id(*redirect)} birbirini gösteriyor): yapılandırma dosyaları farklı olabilir")
        return response

    @staticmethod
    def _parse_stats(response):
        """'STATS <n>' -> n (0 is valid!); anything else -> None."""
        word, _, number = response.partition(" ")
        if word == RESP_STATS and number.isascii() and number.isdigit():
            return int(number)
        return None

    def _total_stats(self):
        counts = self.stats_per_server()
        missing = [nid for nid, count in counts.items() if count is None]
        if missing:
            # A total that silently skips a server would look like data was lost.
            raise KVError("STATS eksik, şu sunuculardan sayı alınamadı: " + ", ".join(missing))
        return format_stats(sum(counts.values()))


def format_stats_report(counts):
    """Text for the shell's STATS: the total, then one line per server (a dict from stats_per_server)."""
    known = [count for count in counts.values() if count is not None]
    missing = len(counts) - len(known)
    total = f"STATS {sum(known)}"
    if missing:
        total += f" (EKSİK: {missing} sunucuya ulaşılamadı)"
    lines = [total]
    for nid, count in counts.items():
        lines.append(f"  {nid}: {'ulaşılamıyor' if count is None else count}")
    return "\n".join(lines)


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
            if line.strip().upper() == CMD_STATS and hasattr(client, "stats_per_server"):
                print(format_stats_report(client.stats_per_server()))   # sharded: total + per server
            else:
                print(client.send_command(line))
        except KVError as e:
            # Bağlantı kapandı; bir sonraki komutta yeniden bağlanmayı deneyecek
            print(f"Hata: {e}", file=sys.stderr)
    return 0


def build_client(args):
    """Create the client the command line asks for. Raises ConfigError for a bad config file."""
    if args.cluster:
        config = load_shard_config(args.cluster)
        # --placement overrides the file (the measurement scripts compare both modes)
        return ShardedClient(config["servers"], placement=args.placement or config["placement"],
                             vnodes=config["vnodes"])
    if args.servers:
        return ClusterClient(load_servers(args.servers))
    return KVClient(args.host, args.port)


def main():
    parser = argparse.ArgumentParser(description="Key-Value-Store client'ı")
    parser.add_argument("--host", default="127.0.0.1", help="sunucu adresi (varsayılan: 127.0.0.1)")
    parser.add_argument("--port", type=int, help="sunucu portu (tek sunucu modu)")
    parser.add_argument("--servers", help="servers.json yolu (Level 2 küme modu; --port ile birlikte kullanılmaz)")
    parser.add_argument("--cluster", help="servers_level3.json yolu (Level 3 sharding; --port ve --servers ile "
                                          "birlikte kullanılmaz)")
    parser.add_argument("--placement", choices=VALID_PLACEMENTS,
                        help="dosyadaki yerleştirmeyi geçersiz kıl (yalnızca --cluster ile)")
    parser.add_argument("command", nargs=argparse.REMAINDER,
                        help="tek komut, ör. SET k1 merhaba; verilmezse shell açılır")
    args = parser.parse_args()

    # Tam olarak biri gerekli: --port (Level 1), --servers (Level 2) ya da --cluster (Level 3)
    chosen = [name for name, given in (("--port", args.port is not None),
                                       ("--servers", bool(args.servers)),
                                       ("--cluster", bool(args.cluster))) if given]
    if len(chosen) > 1:
        parser.error(" ve ".join(chosen) + " birlikte kullanılamaz")
    if not chosen:
        parser.error("--port, --servers ya da --cluster gerekli")
    if args.placement and not args.cluster:
        parser.error("--placement yalnızca --cluster ile kullanılabilir")

    try:
        client = build_client(args)
    except ConfigError as e:
        print(f"Hata: {e}", file=sys.stderr)           # traceback değil, anlaşılır mesaj
        raise SystemExit(2)

    with client:
        if args.command:
            # Value birden fazla argüman olarak gelir; tek boşlukla birleştirilir
            code = run_once(client, " ".join(args.command))
        else:
            code = run_shell(client)
    raise SystemExit(code)


if __name__ == "__main__":
    main()