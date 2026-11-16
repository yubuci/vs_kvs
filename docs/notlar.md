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