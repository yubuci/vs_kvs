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

## 2026-11-19 (Öğrenci3, Level 2 Aşama A ve B: start_cluster, sync)

- **Karar:** `scripts/start_cluster.py` önce primary'yi, sonra backup'ları başlatır. Backup açılırken primary'den SYNC çektiği için primary'nin o sırada dinliyor olması gerekir.
- **Karar:** `start_cluster()` ve `stop_cluster()` ayrı fonksiyonlar; Aşama C'deki `test_cluster.py` kümeyi bunlarla kuracak. `servers.json` yoksa betik varsayılan kümeyle (5000/5001/5002) çalışır.
- **Hata:** İlk sürüm, port başka bir sunucu tarafından tutulurken "küme hazır" dedi. Hazır olma kontrolü "bu porta bağlanıp STATS sor" idi ve portu tutan yabancı sunucu cevap verdi; bizim sunucu ise hemen kapanmıştı. Çözüm: başlatmadan önce portun boş olduğu kontrol ediliyor (`port_in_use`).
- **Ders:** "Bir şey cevap veriyor" ile "benim başlattığım şey cevap veriyor" aynı değil. Hazır olma kontrolü yanlış süreci de onaylayabilir.
- **Karar:** `serve_sync` önce `mark_alive`, sonra `snapshot` çağırır. Ters sırada, ikisinin arasındaki SET'ler ne görüntüde olurdu ne de REPL ile giderdi. Bu sırayla bazı kayıtlar iki kez gelebilir; `apply_replicated` aynı seq'i ikinci kez uygulamadığı için zararsız. Sıra `test_sira_once_mark_alive_sonra_snapshot` ile test ediliyor.
- **Karar:** `mark_alive` tanımadığı bir backup'ı sessizce yok sayıyor (ör. `--peers`'te `localhost`, bağlantı `127.0.0.1`'den). O backup hiç REPL almaz ve fark edilmeden geride kalır. `serve_sync` bu durumda uyarı logluyor.
- **Karar:** `pull_sync` hiçbir durumda istisna fırlatmaz (primary kapalı, cevap yok, `ERR ...`, bağlantı yarıda koptu, bozuk satır). Uyarı loglar ve uygulanan kayıt sayısını döndürür; backup açılmaya devam eder.
- **Karar:** Kayıtlar geldikçe uygulanır. Bağlantı yarıda koparsa uygulanmış kayıtlar kalır; her biri primary'deki gerçek bir yazma olduğu için güvenli, bir sonraki SYNC eksikleri tamamlar.
- **Karar:** `parse_entry` `END` ve bozuk satır için aynı `None`'u döndürdüğünden (Öğrenci2'nin notu) `pull_sync` önce `line == RESP_END` kontrol ediyor; sonra gelen `None` "bozuk satır" sayılıp atlanıyor.
- **Hata (platform farkı):** `test_primary_kapali` Linux'ta geçti, Windows'ta başarısız oldu (2,03 sn, sınır 2,0 sn). Linux kimsenin dinlemediği bir porta bağlantıyı anında reddediyor; Windows birkaç kez yeniden deneyip "reddedildi" hatasını ancak ~2 sn sonra veriyor. Test "hemen döner" yerine "timeout'u aşmadan, istisnasız 0 döner" kontrol edecek şekilde düzeltildi (`pull_sync(..., timeout=1.0)`).
- **Ders:** Zamana bağlı testleri tek bir işletim sisteminde çalıştırmak platforma özgü varsayımları gizler. Öğrenci1'in "iki backup kapalıyken ilk SET ~2 sn sürdü" gözlemi de bununla ilgili: Windows'ta ölü bir backup'ı fark etmek timeout'a (1 sn) kadar sürüyor. Gecikme ölçümlerinde işletim sistemi raporda belirtilmeli.
- **Durum:** Gerçek kümede backup'lar açılışta SYNC deniyor; primary SYNC'i henüz `serve_sync`'e yönlendirmediği için `ERR UNKNOWN` dönüyor, backup uyarı loglayıp devam ediyor. Bekleyen: `server.py`'de `handle_client` içinde SYNC → `serve_sync` bağlantısı (Öğrenci1).




## 2026-11-19 (Öğrenci1, Level 2: backup rolü ve SYNC bağlantısı)

- **Karar:** Backup'a gelen client `SET`'i `ERR NOT_PRIMARY <primary portu>` alıyor; `REPL` → `apply_replicated` → `OK` (depo doluysa `ERR FULL`).
- **Karar:** Primary'de `SYNC <port>`, `handle_client` içinde `serve_sync`'e veriliyor (çok satırlı cevap); backup host'u bağlantının adresinden, portu istekten alınıyor.
- **Karar:** Yanlış role gelen istek (primary'ye ya da rolsüz sunucuya `REPL`, backup'a ya da rolsüz sunucuya `SYNC`) `ERR UNKNOWN` alıyor. Yeni bir kural değil: protokol.md bölüm 3'teki "bilinmeyen komut → ERR UNKNOWN" kuralı uygulanıyor.
- **Gözlem (gerçek küme, 1 primary + 2 backup):** 100 SET sonrası üç sunucuda da STATS 100; backup kapatılıp 50 SET yapıldı, yeniden açılınca SYNC sonrası üçü de eşit; yazma sürerken yeniden başlatılan backup da primary ile aynı; primary kapalıyken GET backup'tan çalıştı, SET hata verdi.

## 2026-11-20 (Öğrenci3, Level 2 Aşama C: test_cluster)

- **Karar:** `tests/test_cluster.py` gerçek 3 sunucuyla (ayrı süreçler, `start_cluster()`) çalışıyor; hiçbir şey taklit edilmiyor. Her test kendi boş portlarını işletim sisteminden alıyor, açık bir kümeyle çakışmıyor.
- **Kapsam (8 test):** 100 SET üç sunucuda aynı; güncelleme kopyalanıyor; backup client SET'ini `ERR NOT_PRIMARY` ile reddediyor; `ClusterClient` backup'tan primary'ye yönleniyor; primary kapalıyken GET backup'tan, SET hata (failover yok); backup kapat → yaz → aç → SYNC ile yetişiyor ve REPL yeniden geliyor; yazma sürerken SYNC'te kayıp yok; iki backup kapalıyken primary yazmaya devam ediyor ve ölü backup'ları tekrar beklemiyor.
- **Karar:** Backup yeniden açılınca `wait_ready` yalnızca "dinliyor mu" diye bakıyor, SYNC'in bitmesini değil. Testler bu yüzden backup'ın primary'ye yetişmesini en fazla 5 sn bekliyor (`wait_until`), sabit `sleep` kullanmıyor.
- **Hata (testin kendisinde):** "Yazma sürerken SYNC" testinin ilk sürümü, `serve_sync`'teki sıra bilerek bozulunca (önce snapshot, sonra `mark_alive`, araya 0,3 sn) da **geçti**. Neden: yazıcı 150 key'i döngüyle tekrar yazıyordu; pencerede kaybolan bir yazma, aynı key birkaç ms sonra yeniden yazılınca REPL ile telafi ediliyor ve iz bırakmıyordu. Çözüm: her yazma benzersiz bir key kullanıyor (en fazla 220, 256 sınırının altında).
- **Ölçüm (mutasyon denemesi, Linux):** Düzeltilmiş testle doğru kod 3/3 geçti; bozuk sırayla 3/3 başarısız oldu, backup her seferinde 29 key eksik kaldı (ör. `w099`: primary'de `VALUE v99`, backup'ta `NOT_FOUND`).
- **Ders:** Bir testin geçmesi, beklenen hatayı yakalayabildiğini göstermez. Testi önce bilerek bozulmuş kodla çalıştırıp kırmızıya döndüğünü görmek gerekir. Sürekli üzerine yazılan veri, kayıpları gizler (raporda 2.3 ve 5 için).
- **Hata (Windows'ta test_cluster ile bulundu, Replicator'da yarış):** `test_sync_yazma_surerken_kayip_yok` Windows'ta başarısız oldu: yeniden açılan backup 44 kayıtta kaldı, primary'de 145 vardı; SYNC'ten sonra hiç REPL gelmedi. Sıra: backup kapatılınca ona giden bir REPL ölü porta bağlanmaya çalışırken asılı kalıyor (Windows reddi ~2 sn geç bildiriyor, REPL 1 sn timeout'a kadar bekliyor). Bu sırada backup yeniden açılıp SYNC yapıyor ve `mark_alive` onu "alive" yapıyor. Ardından eski REPL timeout'la bitiyor ve `_mark_behind` onu yeniden "behind" yapıyor: eski bir başarısızlık, kendisinden sonra gelen `mark_alive`'ı eziyor. Linux'ta ret anında geldiği için eski REPL, backup açılmadan çok önce bitiyor ve yarış görünmüyor.
- **Tekrar üretme:** Cevap vermeyen sahte backup ile deterministik test (`test_eski_basarisizlik_yeni_mark_alive_i_ezmez`): REPL asılıyken `mark_alive` çağrılıyor, REPL timeout'la bitince durum `behind` (beklenen `alive`). Linux'ta da her seferinde başarısız.
- **Düzeltme önerisi (Öğrenci1'in onayıyla):** Her backup için bir nesil sayacı (`generation`). `mark_alive` sayacı artırır; REPL başlarken o anki nesli okur; başarısız olursa backup'ı yalnızca nesil değişmemişse "behind" yapar. Arada `mark_alive` olduysa başarısızlık yok sayılır. Güvenli, çünkü o REPL'in taşıdığı yazma `mark_alive`'dan sonra alınan SYNC snapshot'ında zaten var.
- **Ders:** Durum değiştiren eşzamanlı işlemlerde "son yazan kazanır" yetmez; eski bir sonucun yeni bir kararın üzerine yazmaması için sürüm/nesil bilgisi gerekir (seq ile aynı fikir, bu kez Replicator'ın durum tablosunda). Ayrıca: yarış, yalnızca zamanlaması farklı bir platformda (Windows) ortaya çıktı.
