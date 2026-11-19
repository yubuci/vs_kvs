"""
SYNC: geride kalan ya da yeniden başlayan bir backup'ın primary'den tüm depoyu çekmesi.

İki taraf:
    serve_sync()  primary tarafı; server.py, client'tan "SYNC <port>" gelince çağırır
    pull_sync()   backup tarafı; server.py, backup açılırken bir kez çağırır

Mesaj akışı (docs/protokol.md, bölüm 5):
    backup  -> primary : SYNC 5001
    primary -> backup  : ENTRY <seq> <key> <value>   (her kayıt için bir satır)
                         ...
                         END

Neden seq ile birlikte gönderiliyor: backup her kaydı store.apply_replicated() ile
uygular ve bu fonksiyon yalnızca daha yeni bir seq'i yazar. Böylece SYNC sırasında
aynı key için REPL ile gelmiş daha yeni bir değer, eski anlık görüntü tarafından ezilmez.
"""

import logging
import socket

from common.protocol import (
    ENCODING, RESP_END,
    LineTooLongError, encode_line, format_entry, format_sync, parse_entry, read_line,
)

log = logging.getLogger("sync")

PULL_TIMEOUT = 5.0   # saniye; bağlanma ve her satırı bekleme süresi (256 kayıt için bol bol yeter)


# ---------------------------------------------------------------------------
# Primary tarafı
# ---------------------------------------------------------------------------

def serve_sync(conn, store, replicator, backup_host, backup_port):
    """Bir backup'a deponun tamamını gönderir. Gönderilen kayıt sayısını döndürür.

    conn         backup'ın bağlantısı (handle_client'taki soket)
    store        primary'nin Store'u
    replicator   primary'nin Replicator'ı; rolsüz sunucuda None olabilir
    backup_host  backup'ın adresi (bağlantının geldiği adres, ör. "127.0.0.1")
    backup_port  backup'ın DİNLEDİĞİ port (SYNC satırındaki sayı; bağlantının kaynak portu değil)

    SIRA ÖNEMLİ:
      1) mark_alive  -> bu andan sonraki her SET backup'a REPL ile de gider
      2) snapshot    -> bu andan önceki her SET anlık görüntüde vardır
    Böylece hiçbir SET ikisinin arasından kaçamaz. Ters sırada yapılsaydı, snapshot ile
    mark_alive arasındaki SET'ler ne görüntüde olurdu ne de REPL ile giderdi.
    Bu sırayla bazı kayıtlar iki kez gelebilir (hem görüntüde hem REPL'de);
    apply_replicated aynı seq'i ikinci kez uygulamadığı için zararsızdır.

    Ağ hataları (backup bağlantıyı kopardı) yukarı iletilir; handle_client onları
    zaten yakalayıp bağlantıyı kapatıyor.
    """
    if replicator is not None:
        replicator.mark_alive(backup_host, backup_port)
        # mark_alive tanımadığı bir backup'ı sessizce yok sayar. O durumda backup hiç
        # REPL almaz ve fark edilmeden geride kalır; en azından logda görünsün.
        if (backup_host, backup_port) not in replicator.status():
            log.warning("SYNC: %s:%s --peers listesinde yok; REPL almayacak "
                        "(adres yazımı farklı olabilir, ör. localhost / 127.0.0.1)",
                        backup_host, backup_port)

    entries = store.snapshot()            # kopya: kilit tutulmadan gezilebilir

    # Bütün satırlar tek seferde gönderilir: 256 kayıt × en fazla ~4 KB ≈ 1 MB.
    # Satır satır sendall da olurdu; tek parça daha az sistem çağrısı demek.
    payload = b"".join(encode_line(format_entry(seq, key, value)) for seq, key, value in entries)
    payload += encode_line(RESP_END)
    conn.sendall(payload)

    log.info("SYNC: %s kayıt gönderildi -> %s:%s", len(entries), backup_host, backup_port)
    return len(entries)


# ---------------------------------------------------------------------------
# Backup tarafı
# ---------------------------------------------------------------------------

def pull_sync(primary_host, primary_port, my_port, store, timeout=PULL_TIMEOUT):
    """Primary'den deponun tamamını çeker ve store'a uygular. Alınan kayıt sayısını döndürür.

    my_port  bu backup'ın dinlediği port; primary onu bu portla tanıyıp yeniden "canlı" yapar

    İstisna fırlatmaz: primary'ye ulaşılamazsa, bağlantı yarıda koparsa ya da beklenmeyen
    bir cevap gelirse uyarı loglar ve o ana kadar uygulanan kayıt sayısını döndürür.
    Backup böyle bir durumda da açılmaya devam etmeli (server.py'deki sync_from_primary).

    Kayıtlar geldikçe uygulanır (hepsi beklenmez). Bağlantı yarıda koparsa uygulanmış
    kayıtlar kalır; bu güvenlidir, çünkü her biri primary'deki gerçek bir yazmadır ve
    bir sonraki SYNC eksikleri tamamlar.
    """
    applied = 0       # depoya yazılan ya da daha yeni olduğu için atlanan geçerli kayıt
    skipped = 0       # bozuk satır ya da ERR FULL yüzünden uygulanamayan kayıt

    try:
        sock = socket.create_connection((primary_host, primary_port), timeout=timeout)
    except OSError as e:
        log.warning("SYNC: primary %s:%s'e ulaşılamadı (%s); boş depoyla devam ediliyor",
                    primary_host, primary_port, e)
        return 0

    try:
        reader = sock.makefile("r", encoding=ENCODING, newline="\n")
        sock.sendall(encode_line(format_sync(my_port)))

        while True:
            line = read_line(reader)
            if line is None:
                log.warning("SYNC: primary END göndermeden bağlantıyı kapattı (%s kayıt alındı)", applied)
                break
            if line == RESP_END:
                break                                   # normal bitiş
            if line.startswith("ERR"):
                # Ör. "ERR UNKNOWN": karşıdaki sunucu SYNC'i tanımıyor ya da primary değil
                log.warning("SYNC: primary hata döndürdü: %r", line)
                break

            # DİKKAT: parse_entry hem "END" hem bozuk satır için None döndürür;
            # END yukarıda ayrıldığı için burada None yalnızca "bozuk satır" demek.
            entry = parse_entry(line)
            if entry is None:
                log.warning("SYNC: bozuk satır atlandı: %r", line[:80])
                skipped += 1
                continue

            seq, key, value = entry
            err = store.apply_replicated(key, value, seq)
            if err:
                # Yalnızca ERR FULL olabilir: backup'ta primary'de olmayan key'ler var
                log.warning("SYNC: %s uygulanamadı (%s)", key, err)
                skipped += 1
                continue
            applied += 1

    except socket.timeout:
        log.warning("SYNC: primary %g sn içinde cevap vermedi (%s kayıt alındı)", timeout, applied)
    except (OSError, LineTooLongError, UnicodeDecodeError) as e:
        log.warning("SYNC: bağlantı hatası (%s); %s kayıt alındı", e, applied)
    finally:
        sock.close()

    if skipped:
        log.warning("SYNC: %s kayıt uygulanamadı", skipped)
    return applied
