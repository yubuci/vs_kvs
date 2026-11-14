"""
Protokol katmanı: sabitler, doğrulama, satır okuma ve ayrıştırma.

Bu modül ağdan ve thread'lerden tamamen bağımsızdır (saf fonksiyonlar).
Sunucu, client ve testler aynı kuralları buradan kullanır; böylece bir kural
değişirse (ör. hoca "4080 bayt" derse) yalnızca bu dosya değişir.

Kurallar için bkz. docs/protokol.md (sürüm 1.0).
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
ENCODING = "utf-8"      # her iki tarafta açıkça kullanılmalı (Windows varsayılanı cp1254)

# Komut adları
CMD_SET = "SET"
CMD_GET = "GET"
CMD_STATS = "STATS"

# Cevaplar
RESP_OK = "OK"
RESP_NOT_FOUND = "NOT_FOUND"
RESP_VALUE = "VALUE"
RESP_STATS = "STATS"

# Hata kodları (cevap satırında "ERR " öneki ile gönderilir)
ERR_KEY = "ERR KEY"
ERR_VALUE = "ERR VALUE"
ERR_FULL = "ERR FULL"
ERR_UNKNOWN = "ERR UNKNOWN"


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


# ---------------------------------------------------------------------------
# Ayrıştırma
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Request:
    """Ayrıştırılmış bir client isteği.

    error doluysa istek geçersizdir ve cevap olarak doğrudan error gönderilir;
    bu durumda diğer alanlar dikkate alınmaz.
    """
    cmd: Optional[str] = None
    key: Optional[str] = None
    value: Optional[str] = None
    error: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.error is None


def parse_line(line: str) -> Request:
    """Tek bir protokol satırını ayrıştırır. İstisna fırlatmaz, her zaman Request döndürür.

    Kontrol sırası (docs/protokol.md, bölüm 3): komut -> key -> value.
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
