"""
Protokol katmanı: sabitler, doğrulama, satır okuma ve ayrıştırma.

Bu modül ağdan ve thread'lerden tamamen bağımsızdır (saf fonksiyonlar).
Sunucu, client ve testler aynı kuralları buradan kullanır; böylece bir kural
değişirse (ör. hoca "4080 bayt" derse) yalnızca bu dosya değişir.

Kurallar için bkz. docs/protokol.md (sürüm 1.1).
Sürüm 1.1 (Level 2): REPL, SYNC <port>, ENTRY/END ve ERR NOT_PRIMARY eklendi.
Sürüm 1.2 (Level 3): ERR WRONG_NODE <port> eklendi (sharding).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, TextIO

# ---------------------------------------------------------------------------
# Sabitler
# ---------------------------------------------------------------------------

MAX_KEYS = 256          # bir sunucudaki en fazla farklı key sayısı
MAX_KEY_LEN = 16        # varsayım: 1–16 karakter (hocaya sorulacak)
MAX_VALUE_LEN = 4080    # varsayım: karakter, bayt değil (hocaya sorulacak)
MAX_LINE_LEN = 4200     # satır sonu hariç; "SET " + 16 + " " + 4080 = 4101, üstüne pay
MAX_PORT = 65535        # bir TCP portunun alabileceği en büyük değer
ENCODING = "utf-8"      # her iki tarafta açıkça kullanılmalı (Windows varsayılanı cp1254)

# Komut adları
CMD_SET = "SET"
CMD_GET = "GET"
CMD_STATS = "STATS"
CMD_REPL = "REPL"       # Level 2: primary -> backup (kopyalama)
CMD_SYNC = "SYNC"       # Level 2: backup -> primary (tüm depoyu iste)

# Cevaplar
RESP_OK = "OK"
RESP_NOT_FOUND = "NOT_FOUND"
RESP_VALUE = "VALUE"
RESP_STATS = "STATS"
RESP_ENTRY = "ENTRY"    # Level 2: SYNC cevabındaki her kayıt satırı
RESP_END = "END"        # Level 2: SYNC cevabının son satırı

# Hata kodları (cevap satırında "ERR " öneki ile gönderilir)
ERR_KEY = "ERR KEY"
ERR_VALUE = "ERR VALUE"
ERR_FULL = "ERR FULL"
ERR_UNKNOWN = "ERR UNKNOWN"
ERR_NOT_PRIMARY = "ERR NOT_PRIMARY"   # Level 2: "ERR NOT_PRIMARY <port>" olarak gönderilir
ERR_WRONG_NODE = "ERR WRONG_NODE"     # Level 3: "ERR WRONG_NODE <port>" olarak gönderilir


class LineTooLongError(Exception):
    """Okunan satır MAX_LINE_LEN sınırını aştı. Sunucu ERR VALUE gönderip bağlantıyı kapatır."""


# ---------------------------------------------------------------------------
# Doğrulama
# ---------------------------------------------------------------------------

def validate_key(key: Optional[str]) -> Optional[str]:
    """Key geçerliyse None, değilse hata kodu (ERR_KEY) döndürür."""
    if not key:                          # None ya da boş string
        return ERR_KEY
    if len(key) > MAX_KEY_LEN:
        return ERR_KEY
    # isspace() boşluk, tab, \r, \n ve Unicode boşluk karakterlerini kapsar
    if any(ch.isspace() for ch in key):
        return ERR_KEY
    return None


def validate_value(value: Optional[str]) -> Optional[str]:
    """Value geçerliyse None, değilse hata kodu (ERR_VALUE) döndürür."""
    if not value:                        # None ya da boş string
        return ERR_VALUE
    if len(value) > MAX_VALUE_LEN:
        return ERR_VALUE
    if "\n" in value:                    # normalde mümkün değil (satırı bitirir), ama savunmacı kontrol
        return ERR_VALUE
    return None


def _parse_positive_int(text: str, upper: Optional[int] = None) -> Optional[int]:
    """Katı tam sayı ayrıştırıcı: seq ve port alanları için TEK yer (Level 2).

    Geçerli: yalnızca ASCII rakamlar, değer >= 1 (ve upper verilmişse <= upper).
    Geçersiz (hepsi None döndürür): boş metin, işaret ("+7", "-1"), boşluk (" 7"),
    alt çizgi ("1_000"), ondalık ("7.0"), ASCII olmayan rakamlar ("٧").
    Baştaki sıfırlar kabul edilir ("007" -> 7); bu bilinçli bir hoşgörüdür.

    Neden int() yetmez? int("+7"), int(" 7 "), int("1_000") ve int("٧") Python'da
    sorunsuz çalışır. Protokol sınırında (ağdan gelen veri) bu kadar gevşek
    olmak istemiyoruz: bir mesajın tek bir geçerli yazılışı olmalı.

    Neden isdigit() yetmez? "٧".isdigit() de True döner. İşe yarayan kontrol
    isascii() ve isdigit()'in birlikte kullanılmasıdır.
    """
    if not (text.isascii() and text.isdigit()):   # boş metin için isdigit() False döner
        return None
    number = int(text)                   # artık güvenli: yalnızca ASCII rakamlar var
    if number < 1:
        return None
    if upper is not None and number > upper:
        return None
    return number


# ---------------------------------------------------------------------------
# Ayrıştırma
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Request:
    """Ayrıştırılmış bir client isteği.

    error doluysa istek geçersizdir ve cevap olarak doğrudan error gönderilir;
    bu durumda diğer alanlar dikkate alınmaz.

    seq (REPL) ve port (SYNC) Level 2'de eklendi. Varsayılanları None olduğu
    için Level 1'deki Request(...) çağrıları değişmeden çalışır. Yeni alanlar
    en sona eklendi: konumsal (positional) kullanım bozulmasın.
    """
    cmd: Optional[str] = None
    key: Optional[str] = None
    value: Optional[str] = None
    error: Optional[str] = None
    seq: Optional[int] = None
    port: Optional[int] = None

    @property
    def ok(self) -> bool:
        return self.error is None


def parse_line(line: str) -> Request:
    """Tek bir protokol satırını ayrıştırır. İstisna fırlatmaz, her zaman Request döndürür.

    Kontrol sırası (docs/protokol.md, bölüm 3 ve 5.2.1):
        SET:  komut -> key -> value
        REPL: komut -> seq -> key -> value
        SYNC: komut -> port
    Satır uzunluğu kontrolü burada değil, read_line() içinde yapılır.
    Kapasite kontrolü (ERR FULL) Store'un işidir.
    """
    # Yalnızca satır sonu karakterleri atılır; value'nun boşlukları korunur
    line = line.rstrip("\r\n")
    if not line:
        return Request(error=ERR_UNKNOWN) #boş satır geçersiz

    # En fazla 3 parça: ilk ikisi tek kelime (yani komut ve key) geri kalan her şey value
    parts = line.split(" ", 2)
    cmd = parts[0].upper()

    if cmd == CMD_STATS:
        # STATS argüman almaz bu yüzden parçalara bölündüğünde tek bir parça olmalı; fazla argüman ERR UNKNOWN
        if len(parts) != 1:
            return Request(error=ERR_UNKNOWN)
        return Request(cmd=CMD_STATS)

    if cmd == CMD_GET:
        # "GET" -> key yok; "GET k1 fazla" -> key boşluk içeremez; ikisi de ERR KEY
        if len(parts) != 2:
            return Request(error=ERR_KEY)
        key = parts[1]
        err = validate_key(key)
        if err:
            return Request(error=err)
        return Request(cmd=CMD_GET, key=key)

    if cmd == CMD_SET:
        key = parts[1] if len(parts) >= 2 else None
        value = parts[2] if len(parts) == 3 else None
        # Önce key, sonra value kontrol edilir
        err = validate_key(key) or validate_value(value)
        if err:
            return Request(error=err)
        return Request(cmd=CMD_SET, key=key, value=value)

    if cmd == CMD_REPL:
        # "REPL <seq> <key> <value>": burada 4 parçaya bölünür (value boşluk içerebilir).
        # Yukarıdaki split(" ", 2) value'yu "seq key value"nun içinde bırakırdı, bu yüzden yeniden bölüyoruz.
        rparts = line.split(" ", 3)
        seq = _parse_positive_int(rparts[1]) if len(rparts) >= 2 else None
        if seq is None:
            # seq eksik ya da sayı değil: protokolde ERR UNKNOWN (docs/protokol.md 5.2.1)
            return Request(error=ERR_UNKNOWN)
        key = rparts[2] if len(rparts) >= 3 else None
        value = rparts[3] if len(rparts) == 4 else None
        # Ağdan gelen veri depoya girmeden önce doğrulanır: validate_* yeniden kullanılır, kural kopyalanmaz
        err = validate_key(key) or validate_value(value)
        if err:
            return Request(error=err)
        return Request(cmd=CMD_REPL, seq=seq, key=key, value=value)

    if cmd == CMD_SYNC:
        # Tam olarak "SYNC <port>": argümansız SYNC (sürüm 1.0) ve fazla argüman geçersizdir.
        # "SYNC 5001 " (sonda boşluk) da 3 parça verir ve reddedilir: bilinçli katılık.
        if len(parts) != 2:
            return Request(error=ERR_UNKNOWN)
        port = _parse_positive_int(parts[1], upper=MAX_PORT)
        if port is None:
            return Request(error=ERR_UNKNOWN)
        return Request(cmd=CMD_SYNC, port=port)

    return Request(error=ERR_UNKNOWN)


# ---------------------------------------------------------------------------
# Çerçeveleme (framing)
# ---------------------------------------------------------------------------

def read_line(stream: TextIO) -> Optional[str]:
    """Akıştan bir satır okur ve satır sonu karakterlerini atar.

    stream, soketten şöyle elde edilir:
        conn.makefile("r", encoding=ENCODING, newline="\\n")
    newline="\\n" sayesinde \\r çevrilmez, satırlar yalnızca \\n'de bölünür.

    Dönüş:
        str  -> okunan satır (sonunda \\r\\n yok)
        None -> karşı taraf bağlantıyı kapattı (EOF)
    Hata:
        LineTooLongError -> satır MAX_LINE_LEN sınırını aştı

    TCP mesaj değil bayt akışı taşır: tek bir recv() yarım satır ya da birden
    fazla satır döndürebilir. readline() tamponlayarak bu sorunu çözer.
    """
    # +2: satır sonundaki olası \r\n için pay bırak
    limit = MAX_LINE_LEN + 2
    raw = stream.readline(limit)
    if raw == "":
        return None                       # EOF

    if not raw.endswith("\n") and len(raw) >= limit:
        # Sınıra ulaşıldı ama satır bitmedi: satır çok uzun
        raise LineTooLongError()

    line = raw.rstrip("\r\n")
    if len(line) > MAX_LINE_LEN:
        raise LineTooLongError()
    # Not: \n olmadan biten son satır (karşı taraf \n göndermeden kapattı) kabul edilir
    return line


def encode_line(text: str) -> bytes:
    """Bir cevap/istek satırını gönderilecek baytlara çevirir (\\n eklenir).

    Gönderirken socket.send() değil socket.sendall() kullanılmalı.
    """
    return (text + "\n").encode(ENCODING)


# ---------------------------------------------------------------------------
# Cevap biçimlendirme (yardımcılar)
# ---------------------------------------------------------------------------

def format_value(value: str) -> str:
    """GET başarılı cevabı: 'VALUE <value>'."""
    return f"{RESP_VALUE} {value}"


def format_stats(count: int) -> str:
    """STATS cevabı: 'STATS <sayı>'."""
    return f"{RESP_STATS} {count}"


# ---------------------------------------------------------------------------
# Level 2: kopyalama ve SYNC mesajları (docs/protokol.md, bölüm 5)
# format_* ve parse_* çiftleri birbirinin tersidir; gidiş-dönüş testleri bunu doğrular.
# ---------------------------------------------------------------------------

def format_repl(seq: int, key: str, value: str) -> str:
    """Primary -> backup kopyalama satırı: 'REPL 7 k1 merhaba dünya'."""
    return f"{CMD_REPL} {seq} {key} {value}"


def format_sync(port: int) -> str:
    """Backup -> primary SYNC isteği: 'SYNC 5001' (port: isteği gönderen backup'ın dinlediği port)."""
    return f"{CMD_SYNC} {port}"


def format_entry(seq: int, key: str, value: str) -> str:
    """SYNC cevabındaki bir kayıt satırı: 'ENTRY 7 k1 merhaba dünya'."""
    return f"{RESP_ENTRY} {seq} {key} {value}"


def parse_entry(line: str) -> Optional[tuple]:
    """'ENTRY <seq> <key> <value>' satırını (seq, key, value) üçlüsüne çevirir.

    Dönüş:
        (seq, key, value)  geçerli bir ENTRY satırı
        None               satır "END" ise YA DA bozuksa

    DİKKAT: "END" ve "bozuk satır" aynı None ile döner, yani bu fonksiyon ikisini
    ayırt edemez. Çağıran (pull_sync) önce `line == RESP_END` kontrolünü yapmalı,
    sonra parse_entry'yi çağırıp None gelirse bunu "bozuk satır" saymalıdır.

    Ağdan gelen veri olduğu için key ve value yine validate_* ile doğrulanır:
    bozuk bir primary backup'ın deposuna geçersiz kayıt yazamasın.
    """
    parts = line.split(" ", 3)           # ENTRY, seq, key, value (value boşluk içerebilir)
    if len(parts) != 4 or parts[0] != RESP_ENTRY:
        return None
    seq = _parse_positive_int(parts[1])
    if seq is None:
        return None
    key, value = parts[2], parts[3]
    if validate_key(key) or validate_value(value):
        return None
    return seq, key, value


def format_not_primary(port: int) -> str:
    """Backup'ın client SET'ine cevabı: 'ERR NOT_PRIMARY 5000'."""
    return f"{ERR_NOT_PRIMARY} {port}"


def _parse_err_port(response: str, code: str) -> Optional[int]:
    """Shared by parse_not_primary and parse_wrong_node: '<code> <port>' -> port, else None.

    Both redirect messages have the same shape ("ERR <NAME> <port>"), so the strict rules
    live here, once. The match is exact: the code, ONE space, then only ASCII digits
    (_parse_positive_int), and the number must be a valid TCP port (1..MAX_PORT).
    Anything else returns None: a client must never be sent to a wrong or made-up port.
    """
    prefix = code + " "
    if not response.startswith(prefix):
        return None
    return _parse_positive_int(response[len(prefix):], upper=MAX_PORT)


def parse_not_primary(response: str) -> Optional[int]:
    """'ERR NOT_PRIMARY <port>' cevabından port'u çıkarır; başka bir cevap için None.

    "ERR NOT_PRIMARY" (port yok), "ERR NOT_PRIMARY abc" ve port'u 1..65535 dışında
    olan cevaplar da None döndürür: client yanlış bir porta yönlendirilmemeli.
    """
    return _parse_err_port(response, ERR_NOT_PRIMARY)


def format_wrong_node(port: int) -> str:
    """Level 3: a server's answer to a SET/GET for a key it does not own: 'ERR WRONG_NODE 5001'.

    <port> is the port of the server that DOES own the key (same host as the answering server).
    """
    return f"{ERR_WRONG_NODE} {port}"


def parse_wrong_node(response: str) -> Optional[int]:
    """'ERR WRONG_NODE <port>' -> the port (5001); None for any other response.

    Also None for a missing port, a non-numeric port, a port outside 1..65535 and extra text
    after the port. ERR NOT_PRIMARY is NOT a WRONG_NODE: the two redirects mean different
    things (Level 2 role vs Level 3 ownership) and must never be mixed up.
    """
    return _parse_err_port(response, ERR_WRONG_NODE)