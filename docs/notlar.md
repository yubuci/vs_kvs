# Proje günlüğü: kararlar ve sorunlar

> Her kararı ve her hatayı tarihiyle buraya yazın. Raporun "Probleme" ve "Entwurf" bölümleri buradan çıkacak.

## 2026-11-13

- **Karar:** Protokol satır tabanlı; her mesaj `\n` ile biter. Satır en fazla 3 parçaya bölünür (`split(" ", 2)`), çünkü value boşluk içerebilir.
- **Karar:** Sınırlar ve doğrulama yalnızca `common/protocol.py` içinde. Hoca "bayt" ya da "tam 16 karakter" derse tek dosya değişir.
- **Karar:** `parse_line` istisna fırlatmaz, her zaman bir `Request` döndürür. Sunucu kodunda `try/except` gerekmez.
- **Karar:** Satır sonunda yalnızca `\r\n` atılır (`rstrip("\r\n")`, `strip()` değil). Value'nun başındaki ve sonundaki boşluklar kullanıcının verisidir.
- **Hata (belgede):** `protokol.md`'deki örnek oturumda `bu_key_17_karakt` için `ERR KEY` bekleniyordu, ama key 16 karakterdi, yani geçerliydi. Birim testi yakaladı. Ders: örnekleri de test et.

## 2026-11-14

- **Karar:** `Store` ağdan habersiz (yalnızca dict + `threading.Lock`). Ağ olmadan test edilebiliyor.
- **Karar:** "Key var mı → yer var mı → yaz" tek bir kilit bloğunda. Ayrı olsaydı iki thread aynı anda "255 kayıt var" görüp ikisi de ekleyebilirdi (257 kayıt).
- **Karar:** Doluyken var olan key güncellenebilir; sınır yalnızca yeni key için geçerli.
- **Hata:** `Store.__init__` içinde `self._max_keys` tanımlanmamıştı; `set` çağrılınca `AttributeError`. 300 thread'lik testte her thread aynı hatayı bastığı için çıktı 1000 satırı geçti. Ders: uzun hata çıktısında önce tekrar eden son satıra bak.
- **Gözlem:** Git, Linux'ta oluşturulan dosyalar için "LF will be replaced by CRLF" uyarısı verdi. Zararsız; gerekirse `.gitattributes` ile sabitlenir.
- **Karar:** Sunucuda bağlantı başına bir thread. Soket `makefile()` + `readline()` ile okunuyor: TCP mesaj değil bayt akışı taşır, tek `recv()` yarım satır ya da birden fazla satır döndürebilir.
- **Karar:** Dinleyen sokette `settimeout(1.0)`. Windows'ta bloklayan `accept()` Ctrl+C'yi yakalamıyor; timeout ile döngü saniyede bir uyanıyor.
- **Karar:** `SO_REUSEADDR` ve `listen(512)`; yeniden başlatmada "Address already in use" olmasın ve 300 eşzamanlı bağlantı kuyruğa sığsın.
- **Karar:** Client doğrulama yapmıyor, satırı olduğu gibi gönderiyor; kurallar tek yerde (sunucuda). Bedeli: hatalı komut için de bir ağ gidiş-dönüşü.
- **Karar:** Client kopan bağlantıda bir kez yeniden bağlanıp komutu tekrarlıyor. SET ve GET tekrarlanınca aynı sonucu verdiği için güvenli.
- **Gözlem:** Sunucu kapalıyken client: `WinError 10061` (bağlantı reddedildi), traceback yok, anlaşılır mesaj.
- **Gözlem:** Sunucu yeniden başlatılınca `STATS 0`: veri yalnızca RAM'de. Level 2'nin (replikasyon) gerekçesi.
- **Gözlem:** 5000 ve 5001 portlarındaki iki sunucu birbirinin verisini görmüyor. Level 2 ve 3'ün gerekçesi.
- **Ölçüm:** `tests/load_test.py`, Windows, 20 tekrar: Test A (300 client, yeni key) her seferinde 256 OK / 44 ERR FULL; Test B (50 client, aynı key) her seferinde tek tutarlı kayıt. 40/40 geçti.

## Açık konular

- Hocaya sorulacak: key tam 16 mı en fazla 16 mı; 4.080 karakter mi bayt mı; 3 kişilik grup olur mu.
- `protokol.md` ekipçe gözden geçirilecek.



## 2026-11-16 (Öğrenci1, Level 2 Aşama A)

- **Karar:** Store her key için (value, seq) saklıyor. seq sayacı kilit altında artıyor; "kontrol, numara ver, yaz" tek kilit bloğunda.
- **Karar:** Başarısız SET (ERR KEY, ERR VALUE, ERR FULL) seq harcamıyor.
- **Karar:** apply_replicated yalnızca gelen seq saklanandan büyükse yazıyor (>=: aynı mesaj iki kez gelebilir).
- **Karar:** apply_replicated deponun kendi sayacını değiştirmiyor; backup numara üretmez. Level 5'te bir backup primary olursa sayacın yükseltilmesi gerekecek.
- **Hata:** execute(), set()'in döndürdüğü çifti ayırmadan "hata var mı" diye sınıyordu; içi dolu bir çift her zaman doğru sayıldığı için her SET hatalı görünürdü.
- **Ders:** Etkileşimli Python oturumu dosyanın eski hâlini bellekte tutar; değişiklikten sonra yeniden başlatmak gerekir.


## 2026-11-16 (Öğrenci1, Level 2 Aşama B)

- **Karar:** Replicator her backup için ayrı bir KVClient ve ayrı bir kilit tutuyor. Ortak tek kilit olsaydı yavaş bir backup diğerine gönderimi de bekletirdi.
- **Karar:** Ulaşılamayan (1 sn timeout) ya da REPL'e OK dışında cevap veren backup "behind" oluyor ve mark_alive çağrılana kadar atlanıyor. Atlanmasaydı her SET ölü backup için 1 sn beklerdi.
- **Karar:** replicate() istisna fırlatmıyor; bir backup sorunu client'ın SET'ini bozmamalı.
- **Karar:** Primary, store.set döndükten (depo kilidi bırakıldıktan) sonra replicate çağırıyor; ağ işlemi kilit altında yapılmıyor. Bedeli: aynı key'e iki SET backup'a ters sırayla varabilir, apply_replicated'deki seq karşılaştırması bunu çözüyor.
- **Karar:** Senkron replikasyon: client'a OK, ulaşılabilen backup'lar cevap verdikten sonra dönüyor.
- **Gözlem:** İki backup kapalıyken ilk SET yaklaşık 2 sn sürdü (backup başına 1 sn), sonraki SET'ler anında döndü.
- **Hata:** Testlerde ResourceWarning (unclosed socket): Replicator bağlantıları kapatmıyordu. Replicator.close() eklendi.
- **Açık:** Backup rolü (ERR NOT_PRIMARY, REPL uygulama) protocol.py 1.1'i, SYNC çağrıları sync.py'yi bekliyor.


## 2026-11-17 (Öğrenci1, Level 2 Aşama C: race_demo)

- **Deney:** `python -m tests.race_demo --runs 500` (Windows, Python 3.11.9). 300 thread aynı anda 300 yeni key ekliyor, sınır 256, her varyant 500 tekrar.

| Varyant | 256'yı aşan çalıştırma | Oran | En az | En çok |
|---|---|---|---|---|
| Store (kilitli) | 0/500 | %0 | 256 | 256 |
| UnsafeStore (kilitsiz) | 0/500 | %0 | 256 | 256 |
| UnsafeStore (kilitsiz, pencere genişletilmiş) | 476/500 | %95,2 | 256 | 286 |

- **Gözlem:** Kilitsiz depo, pencere genişletilmeden 500 denemede hiç hata vermedi. Hata kodda var ama görünmüyor: Python thread'leri seyrek değiştirdiği için kontrol ile yazma arasına başka bir thread nadiren giriyor.
- **Gözlem:** Kontrol ile yazma arasına `time.sleep(0)` konunca (pencere bilerek genişletildi) 500 çalıştırmanın 476'sında sınır aşıldı, en fazla 286 kayıt oluştu (30 fazla). Kalan 24 çalıştırmada yarış yine oluşmadı.
- **Ders:** Yarış hatası testte görünmeyebilir; "500 kez denedim, çalıştı" kanıt değildir. Doğruluğu kilit garanti eder.

## 2026-11-18 (Öğrenci2, Level 2 Aşama A ve B: protokol, config, client)

- **Karar:** `seq` ve `port` için tek bir katı ayrıştırıcı (`_parse_positive_int`): yalnızca ASCII rakam, değer ≥ 1, port ≤ 65535. İşaret, boşluk, alt çizgi ve ASCII olmayan rakam reddedilir. Baştaki sıfır bilinçli olarak kabul edilir (`007` = 7).
- **Ders:** `int()` ağdan gelen veri için fazla hoşgörülü: `int("+7")`, `int(" 7 ")`, `int("1_000")` ve `int("٧")` çalışır. `"٧".isdigit()` de `True` döner; doğru kontrol `isascii()` ve `isdigit()` birlikte. Protokol sınırında bir mesajın tek bir geçerli yazılışı olmalı.
- **Karar:** `REPL` kontrol sırası komut → seq → key → value. Eksik seq `ERR UNKNOWN`, eksik key `ERR KEY`, eksik value `ERR VALUE` (SET ile aynı mantık).
- **Karar:** Argümansız `SYNC` artık `ERR UNKNOWN` (sürüm 1.0'dan kasıtlı kırılma; `SYNC <port>` şart). Sondaki boşluklu `SYNC 5001 ` da reddedilir.
- **Karar:** `parse_line` REPL için `validate_key` ve `validate_value` kullanıyor. Ağdan gelen veri depoya girmeden doğrulanır (`apply_replicated` kendisi doğrulamaz).
- **Gözlem:** `parse_entry` `END` ve bozuk satır için aynı `None` değerini döndürüyor; ikisini ayırt edemiyor. Çağıran (`pull_sync`) önce `line == RESP_END` kontrolünü yapmalı. Docstring'e ve protokol.md'ye yazıldı.
- **Hesap:** En uzun REPL satırı 5 + 19 + 1 + 16 + 1 + 4080 = 4122, ENTRY 4123; ikisi de `MAX_LINE_LEN` = 4200'ün altında (19 hane: seq ≤ 2^63−1 varsayımı). `TestLineLength` bunu test ediyor.
- **Karar:** `config.py`'de tüm doğrulama `load_servers` içinde (tek yer); `primary_of` ve `backups_of` basit kalıyor. Hatalar tek bir `ConfigError` ile bildirilir, client traceback yerine mesaj yazar.
- **Hata (yakalanan tuzak):** Python'da `bool`, `int`'in alt sınıfı; `"port": true` `isinstance(port, int)` kontrolünden geçerdi. Açıkça dışlandı ve test edildi.
- **Karar:** Yönlendirme kararı client'ta (`ClusterClient`). SET ise primary'ye, değilse ilk ulaşılabilir sunucuya. Sadece ilk kelimeye bakılır; key/value kuralları client'ta tekrarlanmaz.
- **Karar:** `ERR NOT_PRIMARY <port>` yalnızca BİR kez izlenir. İki sunucu birbirini gösterirse (yanlış yapılandırma) sonsuz döngü olmaz; ikinci NOT_PRIMARY olduğu gibi döndürülür.
- **Karar:** Primary'ye ulaşılamazsa SET hata verir, GET backup'tan gelir. Failover yok: yeni primary seçilmez (Level 5'in gerekçesi).
- **Hata/Düzeltme:** `KVClient.send_command` zaman aşımında da bir kez yeniden deniyordu. Takılı kalmış bir backup için `Replicator`'da bekleme 1 sn yerine 2 sn oluyordu. Çözüm: zaman aşımı için `KVTimeout` (KVError'un alt sınıfı); zaman aşımında yeniden deneme yok, kopan bağlantıda hâlâ bir kez deneniyor.
- **Yöntem:** Her test yazıldıktan sonra kodu bilerek bozup testin kırmızıya döndüğünü doğruladık (mutasyon denemesi): `isascii()` kaldırma, REPL'de kontrol sırasını değiştirme, port aralığını kaldırma, yönlendirmeyi döngüye çevirme, zaman aşımında yeniden denemeyi geri koyma, `bool` portu kabul etme, SET'i ilk sunucuya yollama. Hepsi en az bir testi kırdı.
- **Açık:** Failover demosu (primary'yi kapat → GET çalışıyor, SET ne dönüyor) gerçek üç sunucuyla yapılıp sonucu buraya yazılacak. Client tarafı testle doğrulandı (`test_primary_down_set_fails_but_get_works_from_backup`); gerçek küme Öğrenci1 (backup rolü) ve Öğrenci3 (sync) birleşince denenebilir.