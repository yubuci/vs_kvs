# Protokol: Dağıtık Key-Value-Store (INF511, WS 2026)

> Bu belge client ile sunucu arasındaki ve sunucuların kendi arasındaki tüm mesajları tanımlar.
> Paralel çalışmanın tek şartı bu belgedir: client'ı yazan ile sunucuyu yazan kişi birbirini beklemeden, yalnızca buna bakarak çalışabilmelidir.
> Değişiklikler yalnızca Pull Request ile ve üç kişinin onayıyla yapılır. Her değişiklik en alttaki **Değişiklik geçmişi** bölümüne yazılır.

**Sürüm:** 1.0 (13.11.2026)

---

## 1. Genel kurallar

| Konu | Kural |
|---|---|
| Taşıma | TCP |
| Kodlama | **UTF-8**. Kodlama her iki tarafta da açıkça belirtilir (Windows'un varsayılan kodlaması cp1254'tür). |
| Çerçeveleme (framing) | Her mesaj tek bir satırdır ve `\n` ile biter. Okuyan taraf satır sonundaki `\r`'yi atar (telnet ve Windows `\r\n` gönderir). |
| Bağlantı | Aynı bağlantı üzerinden art arda birden fazla komut gönderilebilir. Her komuta **tam olarak bir** cevap satırı gelir. Tek istisna `SYNC` (bkz. 5.2). |
| Büyük/küçük harf | Komut adlarında büyük/küçük harf fark etmez (`set` = `SET`). Key ve value'da fark eder. |
| Ayrıştırma | Satır, tek boşluk karakterine göre **en fazla 3 parçaya** bölünür: `komut`, `key`, `value`. Üçüncü parçadan sonraki her şey (boşluklar dahil) value'ya aittir. Python'da: `line.split(" ", 2)`. |
| Maksimum satır uzunluğu | 4.200 karakter (SET + key + value + boşluklar ≈ 4.101, üstüne pay). Daha uzun bir satır gelirse sunucu `ERR VALUE` döner ve **bağlantıyı kapatır**. |
| Boşluk temizleme | Yalnızca satır sonu karakterleri atılır (`rstrip("\r\n")`). Value'nun başındaki ve sonundaki boşluklar **korunur**. |

### Satır ayrıştırma örnekleri

| Gelen satır | komut | key | value |
|---|---|---|---|
| `SET k1 merhaba dünya\n` | `SET` | `k1` | `merhaba dünya` |
| `set k1 merhaba\r\n` | `SET` | `k1` | `merhaba` |
| `SET k1  iki boşluk\n` | `SET` | `k1` | ` iki boşluk` (başında bir boşluk var) |
| `GET k1\n` | `GET` | `k1` | — |
| `STATS\n` | `STATS` | — | — |

---

## 2. Key ve value kuralları

| Alan | Kural | İhlal edilirse |
|---|---|---|
| Key | 1–16 karakter. Boşluk, tab, `\r` ya da `\n` içeremez. | `ERR KEY` |
| Value | 1–4.080 karakter. Boşluk içerebilir. `\n` içeremez (zaten satırı bitirir). | `ERR VALUE` |
| Kapasite | Bir sunucuda en fazla **256** farklı key bulunur. Var olan bir key'in güncellenmesi sınıra takılmaz. | `ERR FULL` |

> **Uygulama notu:** Bu sınırlar ve doğrulama fonksiyonları yalnızca `common/protocol.py` içinde tanımlanır. Sunucu, client ve testler aynı fonksiyonları kullanır. Hoca "bayt" ya da "tam 16 karakter" derse yalnızca orası değişir.

Sabitler:

```
MAX_KEYS      = 256
MAX_KEY_LEN   = 16
MAX_VALUE_LEN = 4080
MAX_LINE_LEN  = 4200
ENCODING      = "utf-8"
```

---

## 3. Client komutları (Seviye 1)

| İstek | Başarılı cevap | Olası hatalar | Açıklama |
|---|---|---|---|
| `SET <key> <value>` | `OK` | `ERR KEY`, `ERR VALUE`, `ERR FULL` | Yeni key ekler ya da var olanı günceller. |
| `GET <key>` | `VALUE <value>` | `NOT_FOUND`, `ERR KEY` | `NOT_FOUND` bir hata değil, normal bir cevaptır. |
| `STATS` | `STATS <kayıt sayısı>` | — | Test betikleri ve demo için. |
| bilinmeyen komut / boş satır | — | `ERR UNKNOWN` | Bağlantı açık kalır. |

### Eksik ve fazla argümanlar

| İstek | Cevap | Neden |
|---|---|---|
| `GET` | `ERR KEY` | Key yok. |
| `GET k1 fazla` | `ERR KEY` | Key boşluk içeremez. |
| `SET` | `ERR KEY` | Key yok. |
| `SET k1` | `ERR VALUE` | Value yok. |
| `STATS fazla` | `ERR UNKNOWN` | STATS argüman almaz. |

### Kontrol sırası (sunucu)

1. Satır uzunluğu → `ERR VALUE` + bağlantıyı kapat
2. Komut adı → `ERR UNKNOWN`
3. Key → `ERR KEY`
4. Value (yalnızca SET) → `ERR VALUE`
5. Kapasite (yalnızca SET ve yeni bir key ise) → `ERR FULL`

### Örnek oturum

```
> SET sehir1 Istanbul
OK
> SET sehir2 Bursa ve çevresi
OK
> GET sehir2
VALUE Bursa ve çevresi
> GET yok
NOT_FOUND
> SET bu_key_17_karakte x
ERR KEY
> STATS
STATS 2
> HELLO
ERR UNKNOWN
```

### Client komut satırı

```
# Tek komut modu: komutu çalıştırır, cevabı yazar, çıkar
python -m client.client --host 127.0.0.1 --port 5000 SET k1 merhaba dünya
python -m client.client --port 5000 GET k1

# Shell modu: komut verilmezse
python -m client.client --port 5000
kvs> SET k1 merhaba
OK
kvs> exit
```

- Bağlantı ve okuma için timeout 3 saniyedir.
- Sunucuya ulaşılamazsa client traceback değil, anlaşılır bir hata mesajı yazar ve 0'dan farklı bir çıkış koduyla biter.

---

## 4. Hata kodları (tam liste)

| Kod | Ne zaman | Seviye |
|---|---|---|
| `ERR KEY` | Key eksik, boş, 16 karakterden uzun ya da boşluk içeriyor | 1 |
| `ERR VALUE` | Value eksik, 4.080 karakterden uzun ya da satır çok uzun | 1 |
| `ERR FULL` | Sunucuda 256 key var ve gelen key yeni | 1 |
| `ERR UNKNOWN` | Bilinmeyen komut, boş satır ya da beklenmeyen sunucu hatası | 1 |
| `ERR NOT_PRIMARY <port>` | Backup'a client'tan `SET` geldi; `<port>` primary'nin portu | 2 |
| `ERR UNAVAILABLE` | Kopya sunucuya ulaşılamadı ya da kopya dolu; yazma geri alındı | 4 |

---

## 5. Sunucu → sunucu mesajları (Seviye 2+)

> Seviye 1'de uygulanmaz. Şimdiden tanımlandı ki Store ve sunucu buna göre tasarlansın.

### 5.1 Sıra numarası (seq)

- Primary (Seviye 4'te koordinatör) her başarılı `SET`'e artan bir tam sayı (`seq`) verir.
- Her sunucu her key için son uyguladığı `seq`'i saklar.
- Bir kopya, `REPL` ya da `SYNC` ile gelen bir değeri **yalnızca** `seq` o key için saklananından büyükse uygular. Değilse değeri yok sayar, cevap yine `OK` olur.
- Neden: `SYNC` ile gelen eski anlık görüntü, arada gelen daha yeni bir `REPL`'in üzerine yazmasın.

### 5.2 Mesajlar

| Mesaj | Cevap | Açıklama | Seviye |
|---|---|---|---|
| `REPL <seq> <key> <value>` | `OK` / `ERR FULL` | Primary'den backup'a kopyalama. Bu mesajda satır 4 parçaya bölünür: `split(" ", 3)`. | 2 |
| `SYNC` | `ENTRY <seq> <key> <value>` satırları, ardından `END` | Yeniden başlayan backup tüm depoyu çeker. | 2 |
| `PING` | `PONG` | Heartbeat (500 ms'de bir) | 5 |
| `VOTE <term> <aday_port>` | `GRANT` / `DENY` | Raft-lite leader seçimi | 5 |

### 5.3 Replikasyon kuralı (Seviye 2)

1. Primary `SET`'i doğrular, kilit altında uygular ve `seq` atar.
2. Primary kilidi **bırakır**, sonra ulaşılabilen tüm backup'lara `REPL` gönderir.
3. Ulaşılamayan bir backup atlanır ve "geride" olarak işaretlenir. Ona artık `REPL` gönderilmez.
4. Client'a `OK`, primary ve ulaşılabilen tüm backup'lar yazdıktan sonra döner.
5. Geride kalan ya da yeniden başlayan backup, tekrar `REPL` almadan önce `SYNC` yapmak zorundadır.

> Bu bilinçli bir tercih: tutarlılık yerine erişilebilirlik (CAP). Gerekçesi raporda, bölüm 2.3 ve 2.5'te anlatılır.

### 5.4 Yazma kuralı (Seviye 4, N=2)

1. Koordinatör, kilit altında key'i doğrular ve yer ayırır (rezervasyon: kayıt sayısı + bekleyen rezervasyonlar ≤ 256).
2. Kilidi bırakır ve kopya sunucuya `REPL` gönderir.
3. Kopya `OK` derse koordinatör kilit altında yazar ve client'a `OK` döner.
4. Kopya `ERR FULL` derse ya da ulaşılamazsa koordinatör rezervasyonu iptal eder ve client'a `ERR UNAVAILABLE` döner.
5. Kopya, ring'de koordinatörden sonra gelen ilk **farklı** fiziksel sunucudur.

---

## 6. Açık sorular (hocaya sorulacak)

| # | Soru | Şu anki varsayım | Cevap |
|---|---|---|---|
| 1 | Key tam 16 karakter mi, en fazla 16 mı? | 1–16 karakter, boşluksuz | _bekleniyor_ |
| 2 | 4.080 sınırı karakter mi, bayt mı? (16 + 4.080 = 4.096 = 4 KiB bayt sınırı düşündürüyor; UTF-8'de Türkçe karakterler 2 bayt.) | Karakter | _bekleniyor_ |
| 3 | Ödev 3 kişilik grupla yapılabilir mi? | Evet | _bekleniyor_ |

---

## 7. Değişiklik geçmişi

| Tarih | Sürüm | Değişiklik | Kim |
|---|---|---|---|
| 13.11.2026 | 1.0 | İlk sürüm | |
