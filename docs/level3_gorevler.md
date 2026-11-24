# Level 3: Sharding + Consistent Hashing – iş bölümü

> Kaynak: güncel plan (14.11.2026), bölüm 5. Bu dosya yalnızca Level 3'ü kapsar.
> Hedef: **27 Kasım** (plandaki son tarih 1 Aralık; 4 gün tampon). Etiket `v3`.
> Level 3 bitince proje **teslim edilebilir** durumda olur.

**Amaç:** Level 2'de üç sunucu da aynı veriyi tutuyor, kapasite hâlâ 256. Level 3'te key'ler sunuculara **bölünür** (sharding): her key yalnızca bir sunucuda durur, 3 sunucu birlikte 256'dan fazla kayıt saklar. Hangi key'in hangi sunucuya gideceğine **consistent hashing** karar verir. Replikasyon bu level'da kapalıdır (kod silinmez, Level 4'te birleşir).

**Raporun asıl ölçümü bu level'da:** sunucu sayısı 3'ten 4'e çıkınca key'lerin ne kadarı yer değiştiriyor? Beklenen: `hash mod N` ile yaklaşık %75, consistent hashing ile yaklaşık %25.

| Rol | İsim | Dal | Ana konu | Tahmini iş |
|---|---|---|---|---|
| Öğrenci1 | ................ | `level3-ring` | Hash ring (`ring.py`), sunucuda sahiplik kontrolü | 10–12 saat |
| Öğrenci2 | ................ | `level3-client` | Protokol 1.2, küme yapılandırması, sharding client'ı, demo | 10–12 saat |
| Öğrenci3 | ................ | `level3_measure` | Küme betiği, ölçümler (araştırma sorusu), entegrasyon testleri | 10–12 saat |

Rollerin kime verileceğine ekip karar verir; isimleri tabloya siz yazın. İş paketleri, yük yaklaşık eşit olacak şekilde ayrıldı. Saatler tahmindir. Biri erken biterse bölüm 9'daki ortak işlerden alır.

---

## 1. Level 2'den çıkan dersler (bu level'ın kuralları)

1. **Herkes PR ile çalışır, `main`'e doğrudan push yok.** Level 2'de Öğrenci1'in commit'leri PR'sız girdi; Level 3'te üç kişi aynı dosyaları kullanacağı için bu kural daha önemli.
2. **Arayüz önce, kod sonra.** Bölüm 3'teki imzalar sabittir. Değişecekse üç kişi onaylar ve bu dosya güncellenir.
3. **Darboğaz parçalar ilk gün birleşir.** Level 2'de `protocol.py`, Level 3'te `ring.py`. Aşama A'nın PR'ları en geç 2. gün `main`'de olmalı.
4. **Düğüm kimliği her yerde aynı yazılır:** `"127.0.0.1:5000"` (host + `:` + port). `localhost` ile `127.0.0.1` karışırsa iki farklı düğüm sanılır; Level 2'deki `mark_alive` tuzağının aynısı.
5. **Testler Windows'ta da çalıştırılır.** Level 2'deki iki hata yalnızca Windows'ta göründü. PR açan, testleri Windows'ta geçtiğini PR açıklamasına yazar.
6. **Önemli testler bilerek bozulmuş kodla denenir** (mutasyon denemesi): test kırmızıya dönmüyorsa hatayı yakalamıyordur.
7. **Kod yorumlarının dili** (Türkçe mi İngilizce mi) hâlâ karışık. Level 3'e başlamadan karar verin ve buraya yazın: ............

---

## 2. Dosya sahipliği

| Dosya | Sahibi | Durum |
|---|---|---|
| `common/ring.py`, `tests/test_ring.py` | Öğrenci1 | yeni |
| `server/server.py`, `tests/test_server.py` | Öğrenci1 | değişecek (sahiplik kontrolü) |
| `common/protocol.py`, `tests/test_protocol.py` | Öğrenci2 | değişecek (`ERR WRONG_NODE`) |
| `common/config.py`, `tests/test_config.py` | Öğrenci2 | değişecek (`load_shard_config`) |
| `servers_level3.json` | Öğrenci2 | yeni |
| `client/client.py`, `tests/test_client_routing.py` | Öğrenci2 | değişecek (`ShardedClient`) |
| `scripts/demo_level3.py` | Öğrenci2 | yeni |
| `docs/protokol.md`, `README.md` | Öğrenci2 | güncellenecek |
| `scripts/start_cluster.py` | Öğrenci3 | değişecek (sharding modu) |
| `tests/measure_rebalance.py` | Öğrenci3 | yeni (çevrimdışı ölçüm) |
| `tests/measure_first_full.py` | Öğrenci3 | yeni (gerçek küme ölçümü) |
| `tests/test_sharding_cluster.py` | Öğrenci3 | yeni (entegrasyon) |
| `docs/notlar.md` | herkes | yalnızca sona, kendi tarih başlığının altına |
| `docs/level3_gorevler.md` | herkes | yalnızca kendi kutucuklarını işaretler |

---

## 3. Sözleşme: sabit arayüzler

### 3.1 `common/ring.py` (Öğrenci1)

```python
DEFAULT_VNODES = 50

def node_id(host, port) -> str
    # "127.0.0.1:5000". Düğümün tek geçerli yazımı; herkes bunu kullanır.

def stable_hash(text: str) -> int
    # md5(text.encode("utf-8")) özetinin ilk 8 baytı, big-endian, işaretsiz 64-bit tam sayı.
    # Python'un hash()'i KULLANILMAZ: her çalıştırmada farklı sonuç verir.

class HashRing:
    def __init__(self, nodes, vnodes=DEFAULT_VNODES)
        # nodes: node_id listesi. Her düğüm için vnodes sanal nokta: stable_hash(f"{node}#{i}"), i = 0..vnodes-1
        # Boş liste ya da vnodes < 1 -> ValueError
    nodes                       # (özellik) düğüm listesi, verilen sırayla
    def node_for(self, key) -> str
        # stable_hash(key)'den saat yönünde ilk sanal noktanın düğümü (bisect; sondan başa sarar)
    def nodes_for(self, key, n) -> list[str]
        # saat yönünde ilk n FARKLI fiziksel düğüm (Level 4 için şimdiden); n > düğüm sayısı -> hepsi

def modn_node_for(key, nodes) -> str
    # nodes[stable_hash(key) % len(nodes)]  (karşılaştırma için)

def make_placement(mode, nodes, vnodes=DEFAULT_VNODES)
    # mode "ring" ya da "modn"; key -> node_id döndüren bir fonksiyon verir.
    # Client, sunucu ve ölçüm betikleri yerleştirmeyi YALNIZCA bununla yapar.
```

### 3.2 Protokol 1.2 (Öğrenci2)

| Mesaj | Ne zaman | Seviye |
|---|---|---|
| `ERR WRONG_NODE <port>` | Sunucuya kendisine ait olmayan bir key için `SET`/`GET` geldi; `<port>` doğru sunucunun portu | 3 |

```python
ERR_WRONG_NODE = "ERR WRONG_NODE"
def format_wrong_node(port) -> str            # "ERR WRONG_NODE 5001"
def parse_wrong_node(response) -> int | None  # 5001; başka bir cevap için None
```

`STATS` sahiplik kontrolüne girmez: her sunucu kendi kayıt sayısını döndürür.

### 3.3 Küme yapılandırması (Öğrenci2)

`servers_level3.json` (depo kökünde; `servers.json` Level 2 için olduğu gibi kalır):

```json
{
  "placement": "ring",
  "vnodes": 50,
  "servers": [
    {"host": "127.0.0.1", "port": 5000},
    {"host": "127.0.0.1", "port": 5001},
    {"host": "127.0.0.1", "port": 5002}
  ]
}
```

```python
def load_shard_config(path="servers_level3.json") -> dict
    # {"placement": "ring" | "modn", "vnodes": int, "servers": [{"host", "port"}, ...]}
    # placement yoksa "ring", vnodes yoksa 50. "role" alanı varsa ConfigError (Level 2 dosyası verilmiş).
    # Hatalar load_servers ile aynı tarzda ConfigError.
```

### 3.4 Sunucu (Öğrenci1)

```
# Level 1 gibi (değişmez)
python -m server.server --port 5000

# Level 3: kümenin bir parçası; kendine ait olmayan key'leri reddeder
python -m server.server --port 5000 --cluster servers_level3.json [--placement ring|modn] [--vnodes 50]
```

- `--placement` / `--vnodes` verilirse dosyadakini geçersiz kılar (ölçüm betikleri için).
- Sunucu kendi kimliğini `node_id(host, port)` ile hesaplar; listede yoksa açılmaz (anlaşılır hata).
- `SET`/`GET` için `make_placement(...)(key)` kendisi değilse `ERR WRONG_NODE <doğru port>` döner.
- `--cluster` ile `--role` birlikte verilirse hata (ikisinin birleşimi Level 4).

### 3.5 Client (Öğrenci2)

```
python -m client.client --cluster servers_level3.json SET k1 merhaba
python -m client.client --cluster servers_level3.json            # shell
python -m client.client --cluster servers_level3.json --placement modn GET k1
```

```python
class ShardedClient:
    def __init__(self, servers, placement="ring", vnodes=50, timeout=TIMEOUT)
    def send_command(self, line) -> str
        # SET/GET: key'in sahibine gider. ERR WRONG_NODE gelirse o porta bir kez yönlenir.
        # Sahip sunucuya ulaşılamazsa KVError (Level 3'te failover YOK; Level 4'ün gerekçesi).
        # STATS: "STATS <toplam>" döndürür.
    def stats_per_server(self) -> dict[str, int | None]
        # {"127.0.0.1:5000": 87, ...}; ulaşılamayan sunucu için None
    def close(); __enter__ / __exit__
```

Shell'de `STATS` hem toplamı hem sunucu başına sayıları göstersin (demo için).

### 3.6 Küme betiği (Öğrenci3)

```
python -m scripts.start_cluster --config servers_level3.json          # sharding modu (dosyada role yoksa)
python -m scripts.start_cluster --config servers_level3.json --vnodes 10
```

```python
start_cluster(servers, replication=True, log_dir=None, quiet=False, cluster_file=None,
              placement=None, vnodes=None)
    # cluster_file verilirse sunucular "--cluster cluster_file [--placement] [--vnodes]" ile başlar
```

Ölçüm ve test betikleri geçici yapılandırma dosyası yazıp (`tempfile`) bunu kullanır.

---

## 4. Sıra ve bağımlılıklar

```
Gün 0    Hep birlikte (30 dk): bu dosyayı onaylama, yorum dili kararı, dalları açma
Aşama A  (1-2 gün; başkalarının beklediği parçalar)
           Ö1: ring.py + test_ring.py         <- DARBOĞAZ, ilk birleşen bu olmalı
           Ö2: protocol 1.2 + load_shard_config + servers_level3.json
           Ö3: start_cluster sharding modu; measure_rebalance.py (ring.py'nin imzasına göre)
           -> üç PR birleşir
Aşama B  (2-3 gün)
           Ö1: sunucuda sahiplik kontrolü
           Ö2: ShardedClient + CLI
           Ö3: measure_first_full.py, test_sharding_cluster.py
           -> üç PR birleşir
Aşama C  (1-2 gün)
           Ö1: Windows'ta tam test turu, mutasyon denemesi (ring)
           Ö2: demo_level3.py, protokol.md 1.2, README
           Ö3: ölçümleri Windows'ta çalıştırma, tablolar notlar.md'ye
           -> v3
```

| Parça | Neye ihtiyaç duyar | Beklerken |
|---|---|---|
| Ö3 `measure_rebalance.py` | `ring.py` | İmza sabit; birleşene kadar 20 satırlık geçici bir kopyayla yaz, PR'dan önce gerçeğine geç |
| Ö2 `ShardedClient` | `make_placement` | Aynı: imzaya göre yaz; testte sahte yerleştirme fonksiyonu |
| Ö1 sahiplik kontrolü | `load_shard_config`, `format_wrong_node` | Aşama A'da birleşmiş olur |
| Ö3 `measure_first_full.py`, `test_sharding_cluster.py` | sunucu + client | Aşama B'nin sonunda; o zamana kadar betik iskeleti |


### 4.1 Dosya dosya bağımlılık sırası

Bir dalgadaki iş, ancak "Neye bağlı" sütunundaki işler `main`'e girince **bitirilebilir**. Başlamak için beklemek gerekmez: imzalar bölüm 3'te sabit, beklerken son sütundaki yöntemle yazılır.

| Dalga | Dosya (sahibi) | Neye bağlı | Kimleri bekletiyor | Beklerken |
|---|-------------|--------|----------|--------|
| **0** | `common/ring.py` (Ö1) | hiçbir şey | **6 iş:** sunucu kontrolü (Ö1), ShardedClient (Ö2), demo (Ö2), measure_rebalance, measure_first_full, test_sharding_cluster (Ö3) | – |
| **0** | `common/config.py` + `servers_level3.json` (Ö2) | hiçbir şey | **3 iş:** sunucu (Ö1), client (Ö2), start_cluster (Ö3) | – |
| **0** | `common/protocol.py`, ERR WRONG_NODE (Ö2) | hiçbir şey | **2 iş:** sunucu (Ö1), client (Ö2) | – |
| **1** | `tests/measure_rebalance.py` (Ö3) | ring.py | – | geçici ring kopyasıyla yazılır; PR'dan önce gerçeğine geçilir |
| **1** | `scripts/start_cluster.py`, sharding modu (Ö3) | config.py; çalıştırmak için sunucunun --cluster'ı | 3 iş: measure_first_full, test_sharding_cluster, demo | komut satırını üreten kod yazılır; deneme dalga 2'de |
| **1** | `server/server.py`, sahiplik kontrolü (Ö1) | ring.py, config.py, protocol.py | 3 iş: measure_first_full, test_sharding_cluster, demo | – |
| **1** | `client/client.py`, ShardedClient (Ö2) | ring.py, config.py, protocol.py | 3 iş: measure_first_full, test_sharding_cluster, demo | sahte yerleştirme fonksiyonu ve sahte sunucularla test |
| **2** | `tests/test_sharding_cluster.py` (Ö3) | sunucu, client, start_cluster | – | senaryolar yazılır, çalıştırma en son |
| **2** | `tests/measure_first_full.py` (Ö3) | sunucu, client, start_cluster | – | çevrimdışı tahmin kısmı yalnızca ring.py ile hemen yazılır |
| **2** | `scripts/demo_level3.py` (Ö2) | sunucu, client, start_cluster | – | senaryo metni |
| **3** | Windows ölçümleri, notlar.md, protokol.md 1.2, README.md, `v3` (herkes) | hepsi | – | – |

**Kritik yol** (en uzun bekleme zinciri; burada gecikme bütün level'ı geciktirir):

```text
ring.py ──┐
config.py ┼──> server.py (sahiplik) ──┐
protocol ─┘                           ├──> start_cluster denenir ──> test_sharding_cluster
          └──> ShardedClient ─────────┘                          └──> measure_first_full ──> v3
```

**Kim neyle başlamalı:**

1. **İlk başlayan kişi `ring.py` ile başlamalı.** En çok işi o bekletiyor (6) ve dalga 1'deki üç işin üçü de ona bağlı. Öğrenci1 o gün çalışamıyorsa, ilk başlayan yazar ve Öğrenci1 PR'ı inceler; bu durumda sahiplik yer değiştirir ve bu dosyaya not düşülür.
2. **İkinci olarak `config.py` + `servers_level3.json`**, sonra `protocol.py`. İkisi de küçük (1–2 saat); ikisi bitmeden sunucu ve client tamamlanamaz.
3. **Öğrenci3 dalga 0'ı beklemez:** `measure_rebalance.py` yalnızca `ring.py`'nin imzasına bağlı ve geçici bir kopyayla hemen yazılabilir; `ring.py` gelince tek satır değişir.
4. **Dalga 0'ın üç PR'ı en geç 2. gün `main`'de olmalı.** Gecikirse dalga 1'deki herkes beklemeye başlar.

---

## 5. Öğrenci1: hash ring ve sunucu (`level3-ring`)

**Aşama A**

- [ ] `common/ring.py`: bölüm 3.1'in tamamı
- [ ] `tests/test_ring.py`:
  - [ ] `stable_hash` her çalıştırmada aynı (sabit bir değerle karşılaştır)
  - [ ] aynı key her zaman aynı düğüm; düğüm sırası değişse de aynı sonuç
  - [ ] tek düğümlü ring her şeyi ona verir; boş liste / `vnodes=0` -> `ValueError`
  - [ ] sardırma: en büyük sanal noktadan sonraki hash ilk noktaya düşer
  - [ ] **consistent hashing özelliği:** 4. düğüm eklenince yer değiştiren her key **yeni düğüme** gider; düğüm çıkarılınca yalnızca onun key'leri yer değiştirir
  - [ ] `nodes_for(key, 2)` iki farklı fiziksel düğüm; `nodes_for(key, 5)` 3 düğümde 3 tane
  - [ ] `modn_node_for` ve `make_placement("modn" / "ring" / bilinmeyen)`

**Aşama B**

- [ ] `server/server.py`: `--cluster`, `--placement`, `--vnodes` (bölüm 3.4); `SET`/`GET`'te sahiplik kontrolü
- [ ] `tests/test_server.py`: doğru key kabul, yanlış key `ERR WRONG_NODE <doğru port>`, `STATS` kontrolsüz, `--cluster` ile `--role` birlikte hata, kendi adresi listede yoksa açılmıyor

**Aşama C**

- [ ] Mutasyon denemesi: `node_for`'da bisect sonucu bilerek bir kaydırılınca `test_ring` kırmızı mı?
- [ ] Windows'ta tam test turu

**Rapor notu:** consistent hashing ve sanal düğümler (2.4'e malzeme); yönlendirme hatasını sunucunun yakalaması (Redis Cluster'daki `MOVED` yönlendirmesine benzer).

---

## 6. Öğrenci2: protokol, yapılandırma, client (`level3-client`)

**Aşama A** (Öğrenci1'in Aşama B'si bunu bekliyor)

- [ ] `common/protocol.py`: `ERR_WRONG_NODE`, `format_wrong_node`, `parse_wrong_node`; testler
- [ ] `common/config.py`: `load_shard_config` (bölüm 3.3); `tests/test_config.py`: varsayılanlar, bilinmeyen placement, `vnodes` 0 / negatif / bool, `role` alanı varsa hata, tekrar eden adres
- [ ] `servers_level3.json`

**Aşama B**

- [ ] `client/client.py`: `ShardedClient` (bölüm 3.5); CLI'da `--cluster` ve `--placement` (`--port` ve `--servers` ile birlikte verilemez)
- [ ] `tests/test_client_routing.py`: sahte sunucularla doğru sahibe gitme, `ERR WRONG_NODE` ile tek yönlenme (sonsuz döngü yok: ikinci `WRONG_NODE`'da hata), sahip kapalıyken `KVError`, `STATS` toplamı, `stats_per_server`'da kapalı sunucu `None`

**Aşama C**

- [ ] `scripts/demo_level3.py`: 3 sunucu (vnode 50; vnode 1'de 600 key sığmaz, bkz. bölüm 7), iki client'tan 600 key, sunucu başına `STATS`; bir sunucuyu kapat -> o sunucunun key'leri okunamıyor (Level 4'ün gerekçesi)
- [ ] `docs/protokol.md` 1.2, `README.md` Level 3 bölümü

**Rapor notu:** yerleştirme kararının client'ta olmasının artı ve eksileri; sunucunun `WRONG_NODE` ile düzeltmesi; Level 3'te failover olmaması.

---

## 7. Öğrenci3: betikler, ölçüm, entegrasyon (`level3_measure`)

**Aşama A**

- [ ] `scripts/start_cluster.py`: sharding modu (bölüm 3.6); `role` alanı olmayan dosya -> sharding; `--placement`, `--vnodes`
- [ ] `tests/measure_rebalance.py` (**çevrimdışı, sunucu gerekmez**; yalnızca `ring.py`):
  - [ ] 10.000 key, düzenler: `modn`, `ring` vnode = 1, 10, 50, 200
  - [ ] **yer değiştiren key oranı:** 3 -> 4 ve 4 -> 3 sunucu
  - [ ] **dağılım:** sunucu başına key sayısı, en büyük / en küçük, standart sapma
  - [ ] Markdown tablosu + `--csv`

**Aşama B**

- [ ] `tests/measure_first_full.py` (gerçek küme): 3 sunucu, benzersiz key'ler tek tek yazılır, **ilk `ERR FULL` kaçıncı key'de**; ayrıca 600 key yazınca kaçı `ERR FULL`. Düzenler: `modn`, vnode 1, 10, 50, 200; her biri 5 farklı key kümesiyle, medyan
- [ ] Aynı sayıların ring'den çevrimdışı tahmini ile karşılaştırılması (ölçüm ve tahmin örtüşmeli; örtüşmüyorsa bir yerde hata var)
- [ ] `tests/test_sharding_cluster.py` (gerçek 3 sunucu, kendi boş portlarıyla):
  - [ ] 600 key yaz -> hepsi okunuyor, `STATS` toplamı 600, her sunucu 256 veya altında
  - [ ] her key yalnızca sahibinde (diğer iki sunucuya doğrudan `GET` -> `ERR WRONG_NODE`)
  - [ ] bir sunucu kapalı -> onun key'leri okunamıyor, diğerlerininki okunuyor
  - [ ] `modn` ile de aynı testler

**Aşama C**

- [ ] Ölçümleri **Windows'ta** çalıştır; tablolar ve yorumlar `notlar.md`'ye
- [ ] Mutasyon denemesi: `test_sharding_cluster` yanlış yerleştirmeyi yakalıyor mu?

**Beklenen aralıklar** (14 satırlık bir prototiple, Linux'ta, 10.000 key; ölçümleriniz bunlara yakın çıkmalı, çok farklıysa bir yerde hata var):

| Düzen | 3 -> 4'te yer değiştiren | Sunucu başına key (3 sunucu) | İlk `ERR FULL` (5 key kümesi, medyan) |
|---|---|---|---|
| `modn` | ~%75 | çok dengeli (~3.300 ± 60) | ~735 |
| ring, vnode 1 | ~%25 | çok dengesiz (800 – 4.900) | **~535** (600 key sığmaz) |
| ring, vnode 10 | ~%27 | dengesiz | ~610 |
| ring, vnode 50 | ~%20–25 | dengeli | ~690–735 |
| ring, vnode 200 | ~%25 | dengeli | ~700 |

Buradan çıkan iki sonuç rapor için önemli: (1) **`modn` en dengeli dağıtımı veriyor ama sunucu eklenince key'lerin dörtte üçünü yerinden oynatıyor**; consistent hashing tersini yapıyor, dengeyi ancak yeterince sanal düğümle sağlıyor. (2) **600 key demosu ancak yeterli vnode ile çalışır**; vnode 1'de yaklaşık 535. key'de bir sunucu dolar.

**Rapor notu:** raporun ana Evaluation bölümü (5) ve Fragestellung'a cevap; "768 kayıt" iddiasının neden yalnızca teorik üst sınır olduğu.

---

## 8. İnceleme

| PR sahibi | İnceleyen |
|---|---|
| Öğrenci1 | Öğrenci2 |
| Öğrenci2 | Öğrenci3 |
| Öğrenci3 | Öğrenci1 |

- PR açmadan önce `python -m unittest` geçiyor (açıklamaya: "Windows'ta geçti").
- Aşama başına bir PR. Birleşince herkes: `git checkout <dal>`, `git pull origin main`.

## 9. Ortak işler (erken biten alır)

- [ ] `tests/race_demo.py` gibi, Level 3 için kısa bir "neden vnode?" görselleştirmesi (ring üzerindeki noktaların dağılımı, metin ya da CSV)
- [ ] `docs/notlar.md`'deki Level 1–2 maddelerinden rapor bölüm 6 (Probleme) için madde listesi
- [ ] Hocaya sorulacak üç soru (key uzunluğu, karakter/bayt, grup) soruldu mu? Cevaplar `protokol.md`'ye

## 10. Level 3 bitti sayılır

- [ ] 3 sunucuda toplam 256'dan fazla kayıt (600 key demosu)
- [ ] Bir key yalnızca sahibinde; yanlış sunucuya giden istek `ERR WRONG_NODE` ile düzeltiliyor
- [ ] Yer değiştiren key tablosu (3 -> 4, 4 -> 3; modn ve vnode 1/10/50/200), Windows'ta
- [ ] İlk `ERR FULL` tablosu (ölçüm ve çevrimdışı tahmin), Windows'ta
- [ ] Level 1–2 testleri hâlâ geçiyor (`servers.json` ile Level 2 kümesi değişmeden çalışıyor)
- [ ] `protokol.md` 1.2, `README.md` güncel
- [ ] Etiket `v3`
- [ ] Rapor: Level 3 bitince taslaklar başlar (ekip kararı, 20.11)

## 11. Bilinen sınırlar (bilerek çözülmüyor; rapora yazılır)

- Bir sunucu çökerse onun key'leri okunamaz ve yazılamaz (replikasyon kapalı; Level 4).
- Kapasite eşit dağılmaz: ilk dolan sunucu sınırı belirler; 768 yalnızca teorik üst sınırdır (vnode 50 ile ilk `ERR FULL` yaklaşık 700. key'de).
- Sunucu sayısı değişince key'ler **taşınmaz**: yeni yapılandırmayla açılan küme boş başlar. Yer değiştirme oranı bu yüzden çevrimdışı ölçülür; gerçek taşıma (rebalancing) Ausblick.
- Yapılandırma her sunucuda ve client'ta aynı dosyadan okunur; farklı dosyalar farklı ring demektir (sunucunun `WRONG_NODE` kontrolü bunu fark eder ama düzeltemez).
