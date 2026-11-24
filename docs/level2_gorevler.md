# Level 2: Primary-Backup replikasyon – iş bölümü

> Kaynak: güncel plan (14.11.2026), bölüm 4. Bu dosya yalnızca Level 2'yi kapsar.
> Hedef: en geç **25 Kasım**, etiket `v2`.

## Durum (20.11.2026): Level 2 tamamlandı, etiket `v2`

| Rol | İsim | Ne yaptı |
|---|---|---|
| Öğrenci1 | Ceren | Store (seq), `Replicator`, sunucuda roller, `race_demo.py`; Replicator yarışının düzeltmesini inceledi |
| Öğrenci2 | (ekip yazsın) | `protocol.py` eklemeleri, `config.py` + `servers.json`, `ClusterClient` (yönlendirme, failover), `protokol.md` 1.1, README |
| Öğrenci3 | yubuci | `start_cluster.py`, `sync.py`, `test_sync.py`, `test_cluster.py`, `measure_latency.py`; Replicator'daki yarışın bulunması ve düzeltmesi |

- PR'lar: #2 (protokol, client), #3 (start_cluster, sync), #4 (entegrasyon testleri, yarış düzeltmesi), #5 (gecikme ölçümü). Öğrenci1'in commit'leri doğrudan `main`'e girdi.
- Bölüm 8'deki "bitti" listesi: rapor taslakları maddesi dışında hepsi tamam. **Rapor taslakları Level 3 sonrasına ertelendi (ekip kararı, 20.11).** `v2` bu madde olmadan atıldı.
- `v2`, PR #4'ü gösteriyor; gecikme ölçümü (PR #5) etiketten sonra geldi.
- Ayrıntılar ve bulunan hatalar: `docs/notlar.md`.

---

**Amaç:** Sunucu kapanınca veri kaybolmasın. 3 sunucu çalışır: 1 primary, 2 backup. Her `SET` primary'ye gider, primary backup'lara kopyalar. `GET` her sunucudan okunabilir.

| Rol | İsim | Dal | Ana konu |
|---|---|---|---|
| Öğrenci1 | ................ | `level2-replikasyon` | Sunucu çekirdeği: Store, Replicator, roller |
| Öğrenci2 | ................ | `level2-client` | Protokol, client yönlendirme, yapılandırma |
| Öğrenci3 | ................ | `level2-sync` | SYNC, küme betiği, entegrasyon testleri, ölçüm |

---

## 1. Paralel çalışmanın üç kuralı

1. **Her dosyanın tek sahibi var.** Kimse başkasının dosyasını değiştirmez; değişiklik gerekiyorsa sahibinden ister. Böylece merge çakışması olmaz.
2. **Arayüzler baştan sabit** (bölüm 2). Herkes karşı tarafın kodu hazır olmadan, yalnızca bu imzalara bakarak yazar. Arayüz değişecekse üç kişi onaylar ve bu dosya güncellenir.
3. **Küçük ve erken PR.** Başkalarının beklediği parçalar (Aşama A) ilk gün birleştirilir. Her PR'ı bölüm 7'deki kişi inceler.

### Dosya sahipliği

| Dosya | Sahibi | Durum |
|---|---|---|
| `server/store.py`, `tests/test_store.py` | Öğrenci1 | değişecek |
| `server/replication.py`, `tests/test_replication.py` | Öğrenci1 | yeni |
| `server/server.py` | Öğrenci1 | değişecek |
| `common/protocol.py`, `tests/test_protocol.py` | Öğrenci2 | değişecek |
| `common/config.py`, `servers.json`, `tests/test_config.py` | Öğrenci2 | yeni |
| `client/client.py`, `tests/test_client_routing.py` | Öğrenci2 | değişecek / yeni |
| `docs/protokol.md`, `README.md` | Öğrenci2 | güncellenecek |
| `server/sync.py`, `tests/test_sync.py` | Öğrenci3 | yeni |
| `scripts/start_cluster.py` | Öğrenci3 | yeni |
| `tests/test_cluster.py`, `tests/measure_latency.py` | Öğrenci3 | yeni |
| `tests/load_test.py` | Öğrenci3 | gerekirse güncellenir |
| `docs/notlar.md` | herkes | yalnızca sona ekleme; kendi tarih başlığının altına |

---

## 2. Sözleşme: sabit arayüzler

### 2.1 Sunucuyu başlatma

```
# Level 1 gibi, tek başına (hiçbir şey değişmez; --role verilmezse replikasyon yok)
python -m server.server --port 5000

# Primary
python -m server.server --port 5000 --role primary --peers 127.0.0.1:5001,127.0.0.1:5002

# Backup
python -m server.server --port 5001 --role backup --primary 127.0.0.1:5000
```

### 2.2 `servers.json` (depo kökünde)

```json
{
  "servers": [
    {"host": "127.0.0.1", "port": 5000, "role": "primary"},
    {"host": "127.0.0.1", "port": 5001, "role": "backup"},
    {"host": "127.0.0.1", "port": 5002, "role": "backup"}
  ]
}
```

`common/config.py` (Öğrenci2):

```python
def load_servers(path="servers.json") -> list[dict]   # [{"host":..., "port":..., "role":...}, ...]
def primary_of(servers) -> dict                       # rolü "primary" olan kayıt
def backups_of(servers) -> list[dict]
```

### 2.3 Mesajlar

| Mesaj | Kim → kime | Cevap |
|---|---|---|
| `REPL <seq> <key> <value>` | primary → backup | `OK` (seq eskiyse de `OK`), `ERR FULL` |
| `SYNC <port>` | backup → primary | `ENTRY <seq> <key> <value>` satırları, sonra `END` |
| client `SET` bir backup'a gelirse | | `ERR NOT_PRIMARY <primary_port>` |

> **Protokol değişikliği (sürüm 1.1):** `protokol.md`'de `SYNC` argümansızdı. Primary'nin hangi backup'ı yeniden "canlı" sayacağını bilmesi için `SYNC <port>` oldu; `<port>` isteği gönderen backup'ın dinlediği port. Öğrenci2 `protokol.md`'yi günceller.

`common/protocol.py` eklemeleri (Öğrenci2):

```python
CMD_REPL = "REPL";  CMD_SYNC = "SYNC"
RESP_ENTRY = "ENTRY";  RESP_END = "END"
ERR_NOT_PRIMARY = "ERR NOT_PRIMARY"

# Request'e yeni alanlar: seq (int | None), port (int | None)
# parse_line:  "REPL 7 k1 merhaba dünya" -> Request(cmd="REPL", seq=7, key="k1", value="merhaba dünya")
#              "SYNC 5001"               -> Request(cmd="SYNC", port=5001)
#              seq/port tam sayı değilse -> Request(error=ERR_UNKNOWN)

def format_repl(seq, key, value) -> str        # "REPL 7 k1 merhaba dünya"
def format_sync(port) -> str                   # "SYNC 5001"
def format_entry(seq, key, value) -> str       # "ENTRY 7 k1 merhaba dünya"
def parse_entry(line) -> tuple | None          # (seq, key, value); "END" ya da bozuk satır için None
def format_not_primary(port) -> str            # "ERR NOT_PRIMARY 5000"
def parse_not_primary(response) -> int | None  # 5000; başka bir cevap için None
```

### 2.4 `Store` (Öğrenci1)

```python
store.set(key, value)                  -> (err, seq)   # err None ise başarılı; seq yeni atanan sıra numarası
store.apply_replicated(key, value, seq) -> err         # yalnızca seq saklanandan büyükse yazar; None ya da ERR_FULL
store.get(key)                         -> value | None # değişmedi
store.count()                          -> int          # değişmedi
store.snapshot()                       -> list[(seq, key, value)]
```

- Sözlükte her key için `(value, seq)` tutulur. Seq sayacı kilit altında artar.
- `set` imzası değiştiği için `execute()` ve `tests/test_store.py` güncellenir (ikisi de Öğrenci1'in dosyası).

### 2.5 `Replicator` (Öğrenci1)

```python
rep = Replicator(peers)            # peers: [("127.0.0.1", 5001), ("127.0.0.1", 5002)]
rep.replicate(seq, key, value)     # canlı backup'lara REPL gönderir; KİLİT DIŞINDA çağrılır; istisna fırlatmaz
rep.mark_alive(host, port)         # backup'ı yeniden "canlı" yapar (SYNC başlarken çağrılır)
rep.status()                       # {("127.0.0.1", 5001): "alive" | "behind", ...}
```

- Ulaşılamayan (1 sn timeout) backup "behind" olur ve ona artık `REPL` gönderilmez.

### 2.6 `server/sync.py` (Öğrenci3)

```python
def serve_sync(conn, store, replicator, backup_host, backup_port)
    # Primary tarafı. SIRA ÖNEMLİ: 1) replicator.mark_alive(...)  2) store.snapshot()
    #                              3) her kayıt için ENTRY satırı  4) END

def pull_sync(primary_host, primary_port, my_port, store) -> int
    # Backup tarafı; açılışta bir kez çağrılır. SYNC <my_port> gönderir, END'e kadar okur,
    # her kaydı store.apply_replicated ile uygular. Dönüş: alınan kayıt sayısı.
    # Primary'ye ulaşılamazsa istisna fırlatmaz, uyarı loglar ve 0 döndürür.
```

`server.py` (Öğrenci1) bu iki fonksiyonu yalnızca çağırır: `SYNC` komutu gelince `serve_sync`, backup olarak açılırken `pull_sync`.

---

## 3. Sıra ve bağımlılıklar

```
Gün 0      Hep birlikte: Level 1 kodunu okuma, bu dosyayı onaylama, dalları açma
Aşama A    (1-2 gün) birbirinden bağımsız, başkalarının beklediği parçalar
             Ö1: Store (seq)          Ö2: protocol.py + config.py + servers.json      Ö3: start_cluster.py
             -> üç PR da birleşir
Aşama B    (2-3 gün) asıl mantık; A'daki arayüzleri kullanır
             Ö1: Replicator + roller  Ö2: client yönlendirme                           Ö3: sync.py
             -> üç PR da birleşir
Aşama C    (1-2 gün) entegrasyon
             Ö1: hata düzeltme, race_demo   Ö2: README, protokol.md, demo denemesi     Ö3: küme testleri, gecikme ölçümü
             -> v2
```

| Parça | Neye ihtiyaç duyar | Hazır olmadan nasıl ilerlenir |
|---|---|---|
| Ö1 Replicator + roller | Ö2'nin `format_repl`, `parse_line(REPL)` | Aşama A'da birleşmiş olur |
| Ö2 client yönlendirme | `ERR NOT_PRIMARY` döndüren bir sunucu | Testte 10 satırlık sahte sunucu (her `SET`'e `ERR NOT_PRIMARY 5000` döner) |
| Ö3 `sync.py` | Ö1'in `snapshot`, `apply_replicated`; Ö2'nin `format_entry`, `parse_entry` | Aşama A'da birleşmiş olur; `replicator` için testte `mark_alive`'ı olan sahte nesne |
| Ö3 küme testleri | Hepsi | Aşama C |

---

## 4. Öğrenci1: sunucu çekirdeği (`level2-replikasyon`)

**Aşama A**
- [ ] `Store`: değer `(value, seq)` çifti; `set` → `(err, seq)`; `apply_replicated`; `snapshot` → `(seq, key, value)` listesi
- [ ] `tests/test_store.py`: mevcut 9 testi yeni imzaya uyarla. Yeni testler: seq her `SET`'te artıyor; `apply_replicated` eski seq'i yok sayıyor, yeni seq'i uyguluyor; dolu depoda yeni key için `ERR_FULL`; 300 thread'lik testte tüm seq'ler farklı
- [ ] `server.py` içinde `execute()`'u yeni `set` imzasına uyarla (davranış aynı kalır; Level 1 testleri geçmeli)

**Aşama B**
- [ ] `server/replication.py`: `Replicator` (bölüm 2.5). Her backup için bir `KVClient` (timeout 1 sn) ve o bağlantıyı koruyan ayrı bir kilit
- [ ] `server.py`: `--role`, `--peers`, `--primary` parametreleri; `execute()` rol bilgisini alır
  - primary: `SET` → `store.set` → (kilit bırakıldıktan sonra) `replicator.replicate` → `OK`
  - backup: client `SET` → `ERR NOT_PRIMARY <port>`; `REPL` → `store.apply_replicated` → `OK`
  - rol yok: Level 1 davranışı
  - `SYNC` gelince `serve_sync(...)` çağrısı; backup açılırken `pull_sync(...)` çağrısı
- [ ] `tests/test_replication.py`: sahte backup'larla; ulaşılamayan backup "behind" oluyor ve tekrar denenmiyor; `mark_alive` sonrası yeniden `REPL` alıyor

**Aşama C**
- [ ] Entegrasyonda çıkan hatalar
- [ ] `tests/race_demo.py`: kilitsiz, bilerek bozuk Store ile 257+ kayıt çıktığını gösteren deney (rapor için)

**Rapor notu:** senkron ve asenkron replikasyon; neden ölü backup'ı atlıyoruz; seq ile versioning.
**Tahmini iş:** 11–13 saat.

---

## 5. Öğrenci2: protokol ve client (`level2-client`)

**Aşama A** (başkaları bunu bekliyor; ilk gün birleşmeli)
- [ ] `common/protocol.py`: bölüm 2.3'teki sabitler, `Request.seq` / `Request.port`, `parse_line` içinde `REPL` (`split(" ", 3)`) ve `SYNC`, yardımcı fonksiyonlar
- [ ] `tests/test_protocol.py`: `REPL` (boşluklu value, seq sayı değil, eksik alan), `SYNC` (port var / yok / sayı değil), `format_*` ↔ `parse_*` gidiş-dönüş testleri. Mevcut 20 test geçmeye devam etmeli
- [ ] `common/config.py` + `servers.json` + `tests/test_config.py` (dosya yok, bozuk JSON, primary yok, iki primary)

**Aşama B**
- [ ] `client/client.py`: `--servers servers.json` seçeneği (verilmezse `--port` ile Level 1 davranışı)
  - `SET` primary'ye gider
  - `GET` ve `STATS` listedeki ilk ulaşılabilen sunucuya gider
  - cevap `ERR NOT_PRIMARY <port>` ise o porta bağlanıp komutu bir kez tekrarlar
  - bağlantı hatasında listedeki sıradaki sunucuyu dener; hiçbiri yoksa anlaşılır hata
- [ ] `tests/test_client_routing.py`: sahte sunucularla; yönlendirme, failover, hepsi kapalıyken hata mesajı

**Aşama C**
- [ ] `docs/protokol.md`: sürüm 1.1 (`SYNC <port>`, değişiklik geçmişi)
- [ ] `README.md`: Level 2 bölümü (kümeyi başlatma, `--servers` ile client)
- [ ] Demo denemesi: primary'yi kapat → `GET` çalışıyor, `SET` ne dönüyor; sonucu `notlar.md`'ye yaz

**Rapor notu:** primary düşünce yazma durur (Level 5'in gerekçesi); yönlendirme kararının client'ta olması.
**Tahmini iş:** 11–13 saat.

---

## 6. Öğrenci3: SYNC, betikler, testler (`level2-sync`)

**Aşama A**
- [ ] `scripts/start_cluster.py`: `servers.json`'u okur, her sunucuyu `subprocess` ile bölüm 2.1'deki parametrelerle başlatır (önce primary), hepsi hazır olunca adresleri yazar, Ctrl+C ile hepsini kapatır. `tests/load_test.py`'deki `sunucu_baslat` örnek alınabilir
  - `--replication off`: herkesi rolsüz başlatır (Level 3 için şimdiden)
- [ ] `servers.json` henüz yoksa geçici olarak kendi kopyanla çalış; Ö2'nin PR'ı birleşince onunkini kullan

**Aşama B**
- [ ] `server/sync.py`: `serve_sync` ve `pull_sync` (bölüm 2.6). `serve_sync`'te sıra: önce `mark_alive`, sonra `snapshot`
- [ ] `tests/test_sync.py`: gerçek soket, sahte replicator; boş depo, 256 kayıtlı depo, boşluklu value; `pull_sync` primary yokken 0 döndürüyor

**Aşama C**
- [ ] `tests/test_cluster.py` (gerçek 3 sunucu, `subprocess`):
  - 100 `SET` → üç sunucuda da `STATS 100` ve rastgele 20 `GET` aynı
  - primary'yi kapat → `GET` backup'tan doğru
  - bir backup'ı kapat → 50 `SET` → backup'ı aç → `SYNC` sonrası `STATS` primary ile aynı
  - aynı senaryo, backup açılırken yazma sürerken (kayıp yok)
- [ ] `tests/measure_latency.py`: 1, 2 ve 3 kopyayla 1.000 `SET`'in ortalama ve medyan süresi (ms); tabloyu `notlar.md`'ye yaz
- [ ] `tests/load_test.py` hâlâ geçiyor mu (rolsüz tek sunucu)

**Rapor notu:** replikasyonun bedeli (gecikme tablosu); `SYNC`'te sıra neden önemli.
**Tahmini iş:** 11–13 saat.

---

## 7. İnceleme ve birleştirme

| PR sahibi | İnceleyen |
|---|---|
| Öğrenci1 | Öğrenci2 |
| Öğrenci2 | Öğrenci3 |
| Öğrenci3 | Öğrenci1 |

- PR açmadan önce: `python -m unittest discover -s tests -t .` geçiyor.
- PR küçük olsun: Aşama A için bir PR, Aşama B için bir PR.
- Birleştirmeden sonra herkes kendi dalında: `git checkout main`, `git pull`, `git checkout <dal>`, `git merge main`.
- Her gün 10 dakikalık durum konuşması: dün ne bitti, bugün ne var, kim neyi bekliyor.

## 8. Level 2 bitti sayılır

- [x] Primary kapatıldığında `OK` almış hiçbir veri kaybolmuyor; `GET` backup'tan doğru değeri döndürüyor
- [x] Kapatılıp açılan backup `SYNC`'ten sonra primary ile aynı (yazma sürerken de)
- [x] Backup'a gönderilen `SET` client tarafından primary'ye yönlendiriliyor
- [x] `--role` verilmeyen tek sunucu Level 1 gibi çalışıyor; Level 1 testleri ve `load_test` geçiyor
- [x] Gecikme tablosu `notlar.md`'de
- [x] `protokol.md` 1.1, `README.md` güncel
- [ ] Rapor taslakları: 2.1 (Öğrenci2), 2.2 ve 2.3 (Öğrenci1), 5'in Level 1–2 kısmı (Öğrenci3) – **Level 3 sonrasına ertelendi (20.11)**
- [x] Etiket `v2`

## 9. Bilinen sınırlar (bilerek çözülmüyor; rapora yazılır)

- Primary çökerse yazma durur; yeni primary seçilmez (Level 5).
- Primary yeniden başlarsa verisi boştur ve seq sayacı sıfırlanır; backup'lardaki veri eski seq'lerle kalır. Level 2'de primary'yi yeniden başlatmak "kümeyi yeniden kurmak" demektir.
- "Geride" kalan bir backup `SYNC` yapana kadar eski veri döndürebilir (`GET` backup'tan okunabildiği için).
- Kapasite hâlâ 256: üç sunucu da aynı veriyi tutar (Level 3).
