# WinZapp kısayol düzenleme envanteri

Bu belge uygulamanın merkezi komut kataloğunu açıklar. Katalogda 247 ayrı düzenlenebilir yerel atama ve bunların 265 bağlam kaydı bulunur. Sistem genelinde WinZapp'ı gösterme ataması bunlara ek olarak listelenir. Aynı komutun ana pencere ve alt panellerdeki kayıtları tek ayarı paylaşır. Rakamlı ailelerin her rakamı ve alternatif tuş birleşimleri ayrı düzenlenebilir.

## Kullanım

Ayarlar → Kısayollar sekmesinde satırı seçin. Tab ile Değiştir düğmesine geçin, tuş birleşimine basın, Tab ile Tamam'a geçip onaylayın. Örneğin sohbet listesine geçişi Alt+1 yerine Kontrol+1 yapınca satır yeni birleşimi gösterir. Ayarlar'daki Uygula veya Tamam değişikliği etkinleştirir; İptal uygulanmamış değişiklikleri bırakır. Kısayolu kaldır yalnızca o atamayı devre dışı bırakır. Varsayılana dön o komutu, Tümünü varsayılana döndür bütün yerel atamaları ve isteğe bağlı sistem kısayolunu sıfırlar.

## Bağlam ve çakışma mantığı

* Ana pencere komutları sohbet, arşiv, kilitli sohbet, mesaj, yazma, arama, durum ve arama geçmişi panelleriyle aynı anda etkin olabilir. Yeni atamaları bu bağlamlarda aynı tuşa vermek reddedilir.
* Sohbet panelinin tablosu mesaj alanının üst tablosudur. Mesaj listesi, yazma alanı, arama alanı, bağlantı listesi ve bahsetme listesi kendi odak bağlamını kullanır. Bahsetme önerileri yazma alanının alt bağlamıdır.
* Durum tablosu durum listesi ve cevap alanına uygulanır. Çoklu sohbet seçimi normal, arşivlenmiş ve kilitli sohbet listelerinde ve yönlendirme penceresinde aynı ayarları kullanır.
* Medya görüntüleyici, etkin arama, gelen arama, emoji, yapay zekâ sonucu ve ses aygıtı pencereleri bağımsız bağlamlardır. Birbirlerinde aynı tuşu kullanabilirler.
* Sistem genelinde WinZapp'ı gösterme tuşu bütün bağlamlarla çakışır. Eski Genel sekmesindeki yakalama alanı aynı ayarı düzenler; kayıt öncesinde yeniden kontrol edilir.
* Hazır atamalardaki mevcut üst/alt tablo öncelikleri korunur. Örneğin F5 ana pencerede eşitleme, durum ve arama geçmişinde yenileme yapar. Delete odaklanan sohbeti/mesajı, Shift+Delete seçilen satırları etkiler. Yeni bir çakışma bu istisnadan yararlanamaz.
* Ctrl+P sohbet sabitleme veya duraklatılmış ses kaydını dinleme işini mevcut kayıt durumuna göre yapar. Boşluk oynatma veya etkin çoklu seçim modunda seçme işini mevcut seçim/oynatma durumuna göre yapar. Bu durum denetimleri tuş değişince de korunur.
* Tab/Shift+Tab odak hareketi, düz metin yazma, yerel metin seçimi/kopyalama, oklarla imleç hareketi, kaydırıcı yönleri ve işletim sistemi/ekran okuyucu tuşları komut kataloğunun dışındadır. Kontrolsüz harf/rakam ataması yazma alanını bozacağı için yeni yakalamada kabul edilmez; hazır rakamlı aygıt seçimleri korunur. Alt+Tab, Alt+F4, Ctrl+Escape, Ctrl+Shift+Escape ve Ctrl+Alt+Delete yeni atama olarak reddedilir.
* Tuş kodu, değiştirici bitleri ve sabit komut kimliği saklanır. Çevrilmiş görünen ad saklanmaz. Atama yoksa geçerli dilin hazır kısayolu izlenir; açık null değeri devre dışı anlamındadır. Mnemonikler ve OEM harfleri Windows'un etkin klavye düzeninden çözümlenir.
* Kilitli kasa gezinmeden gizlenmişse ilgili komutlar bu listede de gizlenir. Hesap geçişi satırları hesap kayıt yöneticisi bulunan pencerelerde görünür; sistem kısayolu hesabın mevcut kayıt mekanizmasını kullanır.

## Üç kontrolün kapsamı

1. Dokuz hızlandırıcı tablosu, menü atamaları, rakamlı aileler ve erişilebilirlik kısayol bilgileri kaynak üzerinden eşleştirildi. Eski tabloların uygulamaya özgü 175 girdisi katalogla bağlandı; eşdeğer büyük/küçük harf tekrarları tek atamaya indirildi.
2. Doğrudan KEY_DOWN/CHAR_HOOK yolları ayrıca tarandı: oynatma/çoklu seçim, arama, Enter ile gönderme, satır ekleme, hesap ve yer imi kancaları, gelen arama, bahsetme, medya ve ses aygıtı seçimleri dahil edildi. Yerel kontrol hareketleri yukarıdaki sınırla ayrıldı.
3. Kayıtlı tuşların çalışan tablolara geçmesi, eski tuşun devreden çıkması, ortak komutların aynı ayarı okuması, çakışma ve varsayılana dönme davranışları penceresiz testlerle kontrol edilir. Mevcut kayıt, seçim, arama, hesap, kasa ve çeviri regresyon testleri de çalıştırılır. Canlı NVDA ve gerçek tuş yakalama denemesi otomatik penceresiz testin yerine geçmez; bu bilgisayarda uygulama penceresi açılmadan doğrulama yapılır.

## Tam hazır atama listesi

Aşağıdaki değerler Türkçe arayüz içindir. Çevirinin mnemonik harfi değişirse yalnızca varsayılan atama onu izler; kullanıcının ataması sabit kalır. Menü/yardım satırları etkin atamayı gösterir.

| Bağlam | İşlev | Hazır tuş | Kalıcı kimlik |
| --- | --- | --- | --- |
| Sistem genelinde | WinZapp'ı göster | Atanmamış | general.global_hotkey |
| Ana gezinti | Aç | Boşluk | navigation.activate |
| Ana gezinti | sohbetlere git | Alt+1 | main.ID_ALT_1 |
| Ana gezinti, Sohbetler, Sohbet içi | son mesaja git | Alt+2 | main.ID_ALT_2 |
| Ana gezinti, Sohbetler, Sohbet içi | okunmamış mesajlara git | Alt+3 | main.ID_ALT_3 |
| Ana gezinti | arşivlenmiş sohbetlere git | Alt+4 | main.ID_ALT_4 |
| Ana gezinti | duruma git | Alt+5 | main.ID_ALT_5 |
| Ana gezinti | aramalara git | Alt+6 | main.ID_ALT_6 |
| Ana gezinti | kilitli sohbetleri aç | Alt+7 | main.ID_ALT_7 |
| Ana gezinti | ana gezintiye odaklan | Alt+A | main.ID_ALT_NAV |
| Ana gezinti | ayarlar | Kontrol+, | main.ID_CTRL_COMMA |
| Ana gezinti | klavye kısayollarını göster | F1 | main.ID_F1 |
| Ana gezinti | sohbet durumunu duyur (son görülme, çevrimiçi, yazıyor) | Alt+T | main.ID_ALT_T |
| Ana gezinti | herhangi bir panelden o anda çalmakta olan sesi duraklat/sürdür | Kontrol+Alt+Shift+P | main.ID_CTRL_ALT_SHIFT_P |
| Ana gezinti | WinZapp kapanana kadar tüm açık hesaplarda oynatma cihazını değiştir (ardından 1 ile 0) | Kontrol+Alt+Shift+H | main.ID_CTRL_ALT_SHIFT_H |
| Ana gezinti | WinZapp kapanana kadar tüm açık hesaplarda kayıt cihazını değiştir (ardından 1 ile 0) | Kontrol+Alt+Shift+G | main.ID_CTRL_ALT_SHIFT_G |
| Sohbetler, Sohbet içi, Arşivlenmiş sohbetler, Kilitli sohbetler | Mesajlar | Alt+M | navigation.messages |
| Sohbetler | Sohbetlerde ara | Kontrol+F | chats.ID_CTRL_F |
| Sohbetler | yeni sohbet | Kontrol+N | chats.ID_CTRL_N |
| Sohbetler | Sohbeti sil | Sil | chats.ID_DELETE_CONV |
| Sohbetler | telefon numarasını kopyala | Alt+Shift+C | chats.ID_ALT_SHIFT_C_LIST |
| Sohbetler | Sohbet bilgisi | Kontrol+Shift+D | chats.ID_CONV_DATA_LIST |
| Sohbetler | okundu/okunmadı olarak işaretle | Kontrol+Shift+M | chats.ID_TOGGLE_READ_LIST |
| Sohbetler | Sessize al | Alt+Shift+S | chats.ID_MUTE_LIST |
| Sohbetler | Engelle | Kontrol+Shift+B | chats.ID_BLOCK_LIST |
| Sohbetler | Sohbeti temizle | Kontrol+Shift+L | chats.ID_CLEAR_LIST |
| Sohbetler | aramayı sonlandır | Kontrol+Shift+Q | chats.ID_ARCHIVE_LIST |
| Sohbetler | Sohbeti kilitle | Kontrol+Shift+T | chats.ID_LOCK_LIST |
| Sohbetler | Sohbeti sabitle veya sabitlemeyi kaldır; kayıt duraklatıldığında önizlemeyi oynat veya durdur | Kontrol+P | chats.ID_PIN_LIST |
| Sohbetler | sohbeti kapat. Mesajlar seçiliyken, Ayarlar > Kullanıcı Arayüzü bölümünde seçenek etkinse ilk Esc yalnızca tüm seçimleri kaldırır ve yalnızca ikincisi sohbeti kapatır | Kontrol+W | chats.ID_CLOSE_CONV_LIST |
| Sohbetler | Seçili sohbetleri temizle | Kontrol+Alt+Shift+L | chats.ID_BULK_CLEAR_CHATS |
| Sohbetler | Seçili sohbetleri sil | Kontrol+Shift+Sil | chats.ID_BULK_DELETE_CHATS |
| Sohbetler | Seçili sohbetleri arşivle | Kontrol+Alt+Shift+A | chats.ID_BULK_ARCHIVE_CHATS |
| Sohbetler | seçili sohbetleri okundu olarak işaretle | Kontrol+Alt+Shift+R | chats.ID_BULK_READ_CHATS |
| Sohbetler | seçili sohbetleri okunmadı olarak işaretle | Kontrol+Alt+Shift+U | chats.ID_BULK_UNREAD_CHATS |
| Sohbet içi | odaktaki mesajın dosyasını çevrimiçi yapay zekâ ile yazıya dök, betimle veya dönüştür | Kontrol+Shift+I | messages.ID_AI_ACTION |
| Sohbet içi | Mesaj yaz: | Alt+Z | messages.ID_ALT_FOCUS_FIELD |
| Sohbet içi | sesli mesaj kaydet | Kontrol+R | messages.ID_CTRL_R |
| Sohbet içi | sohbeti kapat. Mesajlar seçiliyken, Ayarlar > Kullanıcı Arayüzü bölümünde seçenek etkinse ilk Esc yalnızca tüm seçimleri kaldırır ve yalnızca ikincisi sohbeti kapatır | Escape | messages.ID_ESC |
| Sohbet içi | sohbeti kapat. Mesajlar seçiliyken, Ayarlar > Kullanıcı Arayüzü bölümünde seçenek etkinse ilk Esc yalnızca tüm seçimleri kaldırır ve yalnızca ikincisi sohbeti kapatır | Kontrol+W | messages.CTRL_W |
| Sohbet içi | sohbet bilgisi / kaydı iptal et | Kontrol+Shift+D | messages.ID_CTRL_SHIFT_D |
| Sohbet içi | ek ekle | Kontrol+Shift+A | messages.ID_CTRL_SHIFT_A |
| Sohbet içi | kişiyi engelle | Kontrol+Shift+B | messages.ID_CTRL_SHIFT_B |
| Sohbet içi | mesajı yanıtla | Alt+R | messages.ID_ALT_R |
| Sohbet içi | mesaj bilgisi | Alt+Shift+D | messages.ID_ALT_SHIFT_D |
| Sohbet içi | mesajı ilet | Kontrol+Shift+E | messages.ID_CTRL_SHIFT_E |
| Sohbet içi | kaydı duraklat/sürdür (kayıt yapılmıyorken seçili mesajı sabitle/sabitlemeyi kaldır) | Kontrol+Shift+P | messages.ID_CTRL_SHIFT_P |
| Sohbet içi | cevapsız bir aramayı geri ara | Kontrol+Shift+R | messages.ID_CTRL_SHIFT_R |
| Sohbet içi | sesli mesajı diğer modda kaydet (varsayılan mono ise stereo, stereo ise mono) | Kontrol+Shift+G | messages.ID_CTRL_SHIFT_G |
| Sohbet içi | mikrofon ve bilgisayar sesini kaydet | Kontrol+Shift+H | messages.ID_CTRL_SHIFT_H |
| Sohbet içi | mesajı veya sohbeti sil (seçim varsa seçili olanların tümünü) | Sil | messages.ID_DELETE_MSG |
| Sohbet içi | arama ayarları | Kontrol+C | messages.ID_CTRL_C |
| Sohbet içi | açıklamayı kopyala (fotoğraf/video/belge) | Kontrol+Shift+C | messages.ID_CTRL_SHIFT_C |
| Sohbet içi | mesaj metnini açılır pencerede göster | Alt+C | messages.ID_ALT_C |
| Sohbet içi | mesajı düzenle | Alt+E | messages.ID_ALT_E |
| Sohbet içi | listede kısaltılmış bir metin mesajının geri kalanını oku | Alt+L | messages.ID_ALT_L |
| Sohbet içi | odaklanılan mesajın durumunu duyur | Alt+Shift+L | messages.ID_ALT_SHIFT_L |
| Sohbet içi | odaklanılan mesajın tarih ve saatini duyur | Alt+Shift+K | messages.ID_ALT_SHIFT_K |
| Sohbet içi | medyayı kaydet/indir | Kontrol+Shift+S | messages.ID_CTRL_SHIFT_S |
| Sohbet içi | okundu/okunmadı olarak işaretle | Kontrol+Shift+M | messages.ID_CTRL_SHIFT_M |
| Sohbet içi | sohbeti temizle | Kontrol+Shift+L | messages.ID_CTRL_SHIFT_L |
| Sohbet içi | sohbette ara | Kontrol+Shift+F | messages.ID_CTRL_SHIFT_F |
| Sohbet içi | sonraki arama sonucu, açık sohbetin her yerinde | F3 | messages.ID_F3 |
| Sohbet içi | önceki arama sonucu, açık sohbetin her yerinde | Shift+F3 | messages.ID_SHIFT_F3 |
| Sohbet içi | okunmamış mesajlara git | Alt+U | messages.ID_ALT_U |
| Sohbet içi | okunmamış mesajlara git | Kontrol+L | messages.ID_ALT_U.1 |
| Sohbet içi | özel olarak yanıtla (gruplar) | Alt+Shift+R | messages.ID_ALT_SHIFT_R |
| Sohbet içi | Son tepkiler: | Alt+Shift+E | messages.ID_ALT_SHIFT_E |
| Sohbet içi | sizden bahseden önceki mesaja git | Alt+Shift+M | messages.ID_ALT_SHIFT_M |
| Sohbet içi | size yanıt veren önceki mesaja git | Alt+Shift+P | messages.ID_ALT_SHIFT_P |
| Sohbet içi | telefon numarasını kopyala | Alt+Shift+C | messages.ID_ALT_SHIFT_C |
| Sohbet içi | katılımcıyla sohbet et (gruplar) | Alt+Shift+V | messages.ID_ALT_SHIFT_V |
| Sohbet içi | Sesli ara | Kontrol+Shift+V | messages.ID_CTRL_SHIFT_V |
| Sohbet içi | Görüntülü ara | Kontrol+Alt+Shift+V | messages.ID_CTRL_ALT_SHIFT_V |
| Sohbet içi | alıntılanan mesaja git | Alt+Shift+Q | messages.ID_ALT_SHIFT_Q |
| Sohbet içi | sohbeti sessize alma menüsünü aç / sesi aç | Alt+Shift+S | messages.ID_ALT_SHIFT_S |
| Sohbet içi | sesli mesajı veya ses kaydını Whisper ile bu bilgisayarda yazıya dök ya da kayıtlı dökümü aç | Alt+Shift+T | messages.ID_ALT_SHIFT_T |
| Sohbet içi | mesajı yıldızla | Kontrol+Shift+O | messages.ID_CTRL_SHIFT_O |
| Sohbet içi | oynatma hızını azalt | Alt+, | messages.ID_ALT_COMMA |
| Sohbet içi | oynatma hızını artır | Alt+. | messages.ID_ALT_PERIOD |
| Sohbet içi | Emoji seç | Kontrol+. | messages.ID_CTRL_PERIOD |
| Sohbet içi | seçili mesajları kopyala | Kontrol+Alt+Shift+C | messages.ID_BULK_COPY |
| Sohbet içi | seçili mesajları ilet | Kontrol+Alt+Shift+E | messages.ID_BULK_FORWARD |
| Sohbet içi | seçili mesajları yıldızla | Kontrol+Alt+Shift+F | messages.ID_BULK_STAR |
| Sohbet içi | seçili mesajları sabitle | Kontrol+Alt+Shift+X | messages.ID_BULK_PIN |
| Sohbet içi | seçili mesajları kaydet | Kontrol+Alt+Shift+S | messages.ID_BULK_SAVE |
| Sohbet içi | seçili sohbetleri sil | Kontrol+Shift+Sil | messages.ID_BULK_DELETE |
| Sohbet içi, Ana gezinti | odaklanılan mesaja yer işareti koy veya mevcut yer işaretine git (0) | Kontrol+0 | messages.ID_BOOKMARK[0] |
| Sohbet içi, Ana gezinti | odaklanılan mesaja yer işareti koy veya mevcut yer işaretine git (1) | Kontrol+1 | messages.ID_BOOKMARK[1] |
| Sohbet içi, Ana gezinti | odaklanılan mesaja yer işareti koy veya mevcut yer işaretine git (2) | Kontrol+2 | messages.ID_BOOKMARK[2] |
| Sohbet içi, Ana gezinti | odaklanılan mesaja yer işareti koy veya mevcut yer işaretine git (3) | Kontrol+3 | messages.ID_BOOKMARK[3] |
| Sohbet içi, Ana gezinti | odaklanılan mesaja yer işareti koy veya mevcut yer işaretine git (4) | Kontrol+4 | messages.ID_BOOKMARK[4] |
| Sohbet içi, Ana gezinti | odaklanılan mesaja yer işareti koy veya mevcut yer işaretine git (5) | Kontrol+5 | messages.ID_BOOKMARK[5] |
| Sohbet içi, Ana gezinti | odaklanılan mesaja yer işareti koy veya mevcut yer işaretine git (6) | Kontrol+6 | messages.ID_BOOKMARK[6] |
| Sohbet içi, Ana gezinti | odaklanılan mesaja yer işareti koy veya mevcut yer işaretine git (7) | Kontrol+7 | messages.ID_BOOKMARK[7] |
| Sohbet içi, Ana gezinti | odaklanılan mesaja yer işareti koy veya mevcut yer işaretine git (8) | Kontrol+8 | messages.ID_BOOKMARK[8] |
| Sohbet içi, Ana gezinti | odaklanılan mesaja yer işareti koy veya mevcut yer işaretine git (9) | Kontrol+9 | messages.ID_BOOKMARK[9] |
| Sohbet içi | yer işaretini kaldır (0) | Kontrol+Shift+0 | messages.ID_BOOKMARK_REMOVE[0] |
| Sohbet içi | yer işaretini kaldır (1) | Kontrol+Shift+1 | messages.ID_BOOKMARK_REMOVE[1] |
| Sohbet içi | yer işaretini kaldır (2) | Kontrol+Shift+2 | messages.ID_BOOKMARK_REMOVE[2] |
| Sohbet içi | yer işaretini kaldır (3) | Kontrol+Shift+3 | messages.ID_BOOKMARK_REMOVE[3] |
| Sohbet içi | yer işaretini kaldır (4) | Kontrol+Shift+4 | messages.ID_BOOKMARK_REMOVE[4] |
| Sohbet içi | yer işaretini kaldır (5) | Kontrol+Shift+5 | messages.ID_BOOKMARK_REMOVE[5] |
| Sohbet içi | yer işaretini kaldır (6) | Kontrol+Shift+6 | messages.ID_BOOKMARK_REMOVE[6] |
| Sohbet içi | yer işaretini kaldır (7) | Kontrol+Shift+7 | messages.ID_BOOKMARK_REMOVE[7] |
| Sohbet içi | yer işaretini kaldır (8) | Kontrol+Shift+8 | messages.ID_BOOKMARK_REMOVE[8] |
| Sohbet içi | yer işaretini kaldır (9) | Kontrol+Shift+9 | messages.ID_BOOKMARK_REMOVE[9] |
| Sohbet içi | odaklanılan mesaja geçici yer işareti koy veya mevcut geçici yer işaretine git (sohbetten ayrıldığınızda temizlenir) (0) | Alt+Shift+0 | messages.ID_TEMP_BOOKMARK[0] |
| Sohbet içi | odaklanılan mesaja geçici yer işareti koy veya mevcut geçici yer işaretine git (sohbetten ayrıldığınızda temizlenir) (1) | Alt+Shift+1 | messages.ID_TEMP_BOOKMARK[1] |
| Sohbet içi | odaklanılan mesaja geçici yer işareti koy veya mevcut geçici yer işaretine git (sohbetten ayrıldığınızda temizlenir) (2) | Alt+Shift+2 | messages.ID_TEMP_BOOKMARK[2] |
| Sohbet içi | odaklanılan mesaja geçici yer işareti koy veya mevcut geçici yer işaretine git (sohbetten ayrıldığınızda temizlenir) (3) | Alt+Shift+3 | messages.ID_TEMP_BOOKMARK[3] |
| Sohbet içi | odaklanılan mesaja geçici yer işareti koy veya mevcut geçici yer işaretine git (sohbetten ayrıldığınızda temizlenir) (4) | Alt+Shift+4 | messages.ID_TEMP_BOOKMARK[4] |
| Sohbet içi | odaklanılan mesaja geçici yer işareti koy veya mevcut geçici yer işaretine git (sohbetten ayrıldığınızda temizlenir) (5) | Alt+Shift+5 | messages.ID_TEMP_BOOKMARK[5] |
| Sohbet içi | odaklanılan mesaja geçici yer işareti koy veya mevcut geçici yer işaretine git (sohbetten ayrıldığınızda temizlenir) (6) | Alt+Shift+6 | messages.ID_TEMP_BOOKMARK[6] |
| Sohbet içi | odaklanılan mesaja geçici yer işareti koy veya mevcut geçici yer işaretine git (sohbetten ayrıldığınızda temizlenir) (7) | Alt+Shift+7 | messages.ID_TEMP_BOOKMARK[7] |
| Sohbet içi | odaklanılan mesaja geçici yer işareti koy veya mevcut geçici yer işaretine git (sohbetten ayrıldığınızda temizlenir) (8) | Alt+Shift+8 | messages.ID_TEMP_BOOKMARK[8] |
| Sohbet içi | odaklanılan mesaja geçici yer işareti koy veya mevcut geçici yer işaretine git (sohbetten ayrıldığınızda temizlenir) (9) | Alt+Shift+9 | messages.ID_TEMP_BOOKMARK[9] |
| Sohbet içi | geçici yer işaretini kaldır (0) | Kontrol+Alt+Shift+0 | messages.ID_TEMP_BOOKMARK_REMOVE[0] |
| Sohbet içi | geçici yer işaretini kaldır (1) | Kontrol+Alt+Shift+1 | messages.ID_TEMP_BOOKMARK_REMOVE[1] |
| Sohbet içi | geçici yer işaretini kaldır (2) | Kontrol+Alt+Shift+2 | messages.ID_TEMP_BOOKMARK_REMOVE[2] |
| Sohbet içi | geçici yer işaretini kaldır (3) | Kontrol+Alt+Shift+3 | messages.ID_TEMP_BOOKMARK_REMOVE[3] |
| Sohbet içi | geçici yer işaretini kaldır (4) | Kontrol+Alt+Shift+4 | messages.ID_TEMP_BOOKMARK_REMOVE[4] |
| Sohbet içi | geçici yer işaretini kaldır (5) | Kontrol+Alt+Shift+5 | messages.ID_TEMP_BOOKMARK_REMOVE[5] |
| Sohbet içi | geçici yer işaretini kaldır (6) | Kontrol+Alt+Shift+6 | messages.ID_TEMP_BOOKMARK_REMOVE[6] |
| Sohbet içi | geçici yer işaretini kaldır (7) | Kontrol+Alt+Shift+7 | messages.ID_TEMP_BOOKMARK_REMOVE[7] |
| Sohbet içi | geçici yer işaretini kaldır (8) | Kontrol+Alt+Shift+8 | messages.ID_TEMP_BOOKMARK_REMOVE[8] |
| Sohbet içi | geçici yer işaretini kaldır (9) | Kontrol+Alt+Shift+9 | messages.ID_TEMP_BOOKMARK_REMOVE[9] |
| Arşivlenmiş sohbetler | Sohbetlerde ara | Kontrol+F | archived.ID_CTRL_F |
| Arşivlenmiş sohbetler | Sohbeti sil | Sil | archived.ID_DELETE_CONV |
| Arşivlenmiş sohbetler | telefon numarasını kopyala | Alt+Shift+C | archived.ID_ALT_SHIFT_C_LIST |
| Arşivlenmiş sohbetler | Sohbet bilgisi | Kontrol+Shift+D | archived.ID_CONV_DATA_LIST |
| Arşivlenmiş sohbetler | okundu/okunmadı olarak işaretle | Kontrol+Shift+M | archived.ID_TOGGLE_READ_LIST |
| Arşivlenmiş sohbetler | Sessize al | Alt+Shift+S | archived.ID_MUTE_LIST |
| Arşivlenmiş sohbetler | Engelle | Kontrol+Shift+B | archived.ID_BLOCK_LIST |
| Arşivlenmiş sohbetler | Sohbeti temizle | Kontrol+Shift+L | archived.ID_CLEAR_LIST |
| Arşivlenmiş sohbetler | Sohbeti arşivden çıkar | Kontrol+Shift+Q | archived.ID_UNARCHIVE_LIST |
| Arşivlenmiş sohbetler | Sohbeti kilitle | Kontrol+Shift+T | archived.ID_LOCK_LIST |
| Arşivlenmiş sohbetler | Sohbeti sabitle | Kontrol+P | archived.ID_PIN_LIST |
| Arşivlenmiş sohbetler | Seçili sohbetleri temizle | Kontrol+Alt+Shift+L | archived.ID_BULK_CLEAR_CHATS |
| Arşivlenmiş sohbetler | Seçili sohbetleri sil | Kontrol+Shift+Sil | archived.ID_BULK_DELETE_CHATS |
| Arşivlenmiş sohbetler | Seçili sohbetleri arşivden çıkar | Kontrol+Alt+Shift+A | archived.ID_BULK_UNARCHIVE_CHATS |
| Arşivlenmiş sohbetler | seçili sohbetleri okundu olarak işaretle | Kontrol+Alt+Shift+R | archived.ID_BULK_READ_CHATS |
| Arşivlenmiş sohbetler | seçili sohbetleri okunmadı olarak işaretle | Kontrol+Alt+Shift+U | archived.ID_BULK_UNREAD_CHATS |
| Kilitli sohbetler | Seçili sohbetlerin kilidini kaldır | Kontrol+Alt+Shift+T | locked.ID_BULK_UNLOCK_CHATS |
| Kilitli sohbetler | seçili sohbetleri okundu olarak işaretle | Kontrol+Alt+Shift+R | locked.ID_BULK_READ_CHATS |
| Kilitli sohbetler | seçili sohbetleri okunmadı olarak işaretle | Kontrol+Alt+Shift+U | locked.ID_BULK_UNREAD_CHATS |
| Durum paneli | önceki durum | Kontrol+Sol | status.ID_CTRL_LEFT |
| Durum paneli | sonraki durum | Kontrol+Sağ | status.ID_CTRL_RIGHT |
| Durum paneli | sohbeti kapat. Mesajlar seçiliyken, Ayarlar > Kullanıcı Arayüzü bölümünde seçenek etkinse ilk Esc yalnızca tüm seçimleri kaldırır ve yalnızca ikincisi sohbeti kapatır | Escape | status.ID_ESCAPE |
| Durum paneli | Durumu yenile | F5 | status.ID_F5 |
| Durum paneli | Durum metnini kopyala | Kontrol+C | status.ID_CTRL_C |
| Durum paneli | sesli mesaj kaydet | Kontrol+R | status.ID_CTRL_R |
| Durum paneli | Kaydedilen sesi dinle | Kontrol+P | status.ID_CTRL_P |
| Durum paneli | Kaydı duraklat | Kontrol+Shift+P | status.ID_CTRL_SHIFT_P |
| Durum paneli | Sesli mesajı sil | Kontrol+Shift+D | status.ID_CTRL_SHIFT_D |
| Durum paneli | Emoji seç | Kontrol+. | status.ID_CTRL_PERIOD |
| Durum paneli | medyayı kaydet/indir | Kontrol+Shift+S | status.ID_CTRL_SHIFT_S |
| Aramalar paneli | arama listesine git | Alt+L | calls.ID_ALT_L |
| Aramalar paneli | cevapsız bir aramayı geri ara | Kontrol+Shift+R | calls.ID_CTRL_SHIFT_R |
| Aramalar paneli | arama listesini yenile | F5 | calls.ID_F5 |
| Medya görüntüleyici | önceki durum | Kontrol+Sol | media.ID_CTRL_LEFT |
| Medya görüntüleyici | sonraki durum | Kontrol+Sağ | media.ID_CTRL_RIGHT |
| Medya görüntüleyici | medyayı kaydet/indir | Kontrol+Shift+S | media.ID_CTRL_SHIFT_S |
| Medya görüntüleyici | 10 saniye geri | Alt+V | media.ID_ALT_V |
| Medya görüntüleyici | 10 saniye ileri | Alt+A | media.ID_ALT_A |
| Arama penceresi | aramayı sonlandır | Kontrol+Shift+Q | call.ID_CALL_END |
| Arama penceresi | mikrofonu kapat veya aç | Kontrol+M | call.ID_CALL_MUTE |
| Arama penceresi | arama ayarları | Kontrol+C | call.ID_CALL_SETTINGS |
| Arama penceresi | görüntülü aramada videoyu aç veya kapat | Kontrol+V | call.ID_CALL_VIDEO |
| Arama penceresi | sesli aramayı görüntülü aramaya geçir | Kontrol+P | call.ID_CALL_PROMOTE |
| Ana gezinti | Tüm sohbetleri okundu olarak işaretle | Kontrol+Alt+Shift+M | main.mark_all_read |
| Ana gezinti | Bağlantıyı kes | Kontrol+Alt+Shift+D | main.disconnect |
| Ana gezinti | Çıkış | Kontrol+Alt+Shift+Q | main.exit |
| Ana gezinti | Tüm sohbetleri yeniden eşitle | F5 | main.resync_all |
| Ana gezinti | Bu sohbeti yeniden eşitle | Shift+F5 | main.resync_conversation |
| Ana gezinti | Medyayı indir | Kontrol+Alt+Shift+B | main.sync_media |
| Ana gezinti | Çevrimdışı mod | Kontrol+Alt+Shift+O | main.offline |
| Ana gezinti | kilitli sohbetler kasasını kapat ve gizle | Kontrol+Shift+K | main.lock_vault |
| Mesajlar | Klasörde göster | Kontrol+Enter | message_list.show_folder |
| Mesajlar | Mesaj listesinden yazma alanına yapıştır | Kontrol+V | message_list.paste |
| Mesajlar | ses/videoyu 5 saniye geri sar | Shift+Sol | message_list.seek_back |
| Mesajlar | ses/videoyu 5 saniye ileri sar | Shift+Sağ | message_list.seek_forward |
| Mesajlar | ses/videoyu 1 dakika geri sar | Shift+Sayfa yukarı | message_list.seek_back_minute |
| Mesajlar | ses/videoyu 1 dakika ileri sar | Shift+Sayfa aşağı | message_list.seek_forward_minute |
| Mesajlar | oynatılan ses/videonun başına git veya hiçbir şey çalmıyorsa yukarıdaki her mesajı/sohbeti seçip ilkine git | Shift+Başlangıç | message_list.start |
| Mesajlar | oynatılan ses/videonun sonuna git veya hiçbir şey çalmıyorsa aşağıdaki her mesajı/sohbeti seçip sonuncusuna git | Shift+Son | message_list.end |
| Mesajlar | sonraki sohbeti veya mesajı seç ve odağı ona taşı | Shift+Aşağı | message_list.select_next |
| Mesajlar | Seçimi önceki satıra genişlet | Shift+Yukarı | message_list.select_previous |
| Mesajlar | tüm sohbetleri veya mesajları seç ya da hepsi zaten seçiliyse tümünün seçimini kaldır | Kontrol+Shift+Boşluk | message_list.select_all |
| Mesajlar | odaklanılan ses veya videoyu oynat/duraklat. Ayarlar > Kullanıcı Arayüzü bölümünde seçenek etkinse, önceden bir seçim varsa bunun yerine odaklanılan sohbeti veya mesajı seçer/seçimini kaldırır | Boşluk | message_list.play |
| Mesajlar | odaklanılan sohbeti veya mesajı seç/seçimini kaldır | Kontrol+Boşluk | message_list.select |
| Toplu seçim (sohbetler ve mesajlar) | Seçimi ilk satıra genişlet | Shift+Başlangıç | chat_selection.start |
| Toplu seçim (sohbetler ve mesajlar) | Seçimi son satıra genişlet | Shift+Son | chat_selection.end |
| Toplu seçim (sohbetler ve mesajlar) | sonraki sohbeti veya mesajı seç ve odağı ona taşı | Shift+Aşağı | chat_selection.select_next |
| Toplu seçim (sohbetler ve mesajlar) | Seçimi önceki satıra genişlet | Shift+Yukarı | chat_selection.select_previous |
| Toplu seçim (sohbetler ve mesajlar) | tüm sohbetleri veya mesajları seç ya da hepsi zaten seçiliyse tümünün seçimini kaldır | Kontrol+Shift+Boşluk | chat_selection.select_all |
| Toplu seçim (sohbetler ve mesajlar) | Seçim modu açıkken odaklanan satırı seç veya bırak | Boşluk | chat_selection.select_mode |
| Toplu seçim (sohbetler ve mesajlar) | odaklanılan sohbeti veya mesajı seç/seçimini kaldır | Kontrol+Boşluk | chat_selection.select |
| Sohbet araması | sonraki arama sonucu (alternatif, yalnızca arama alanında çalışır) | Enter | search.next |
| Sohbet araması | önceki arama sonucu (alternatif, yalnızca arama alanında çalışır) | Shift+Enter | search.previous |
| Mesaj yaz: | Mesaj gönder | Enter | composer.send |
| Bahsetmeler | Seçilen bahsetmeyi ekle | Enter | autocomplete.insert |
| Bahsetmeler | Kapat | Escape | autocomplete.close |
| Mesaj yaz: | Yeni satır ekle | Shift+Enter | composer.newline |
| Medya görüntüleyici | Kapat | Escape | media.close |
| Medya görüntüleyici | odaklanılan ses veya videoyu oynat/duraklat. Ayarlar > Kullanıcı Arayüzü bölümünde seçenek etkinse, önceden bir seçim varsa bunun yerine odaklanılan sohbeti veya mesajı seçer/seçimini kaldırır | Boşluk | media.play |
| Çevrimiçi Yapay Zekâ: Yazıya Dökme ve Betimleme | Sor | Kontrol+Enter | ai_result.ask |
| Çevrimiçi Yapay Zekâ: Yazıya Dökme ve Betimleme | Kapat | Escape | ai_result.close |
| Emoji seç | Ekle | Kontrol+Enter | emoji.queue |
| Emoji seç | Önceki emoji ten rengi | Sol | emoji.previous_tone |
| Emoji seç | Sonraki emoji ten rengi | Sağ | emoji.next_tone |
| Bağlantılar | Aç | Enter | links.open |
| Bağlantılar | Aç | Boşluk | links.open_space |
| Bağlantılar | Bağlantıyı kopyala | Kontrol+C | links.copy |
| Bahsetmeler | Aç | Enter | mention.open |
| Bahsetmeler | Aç | Boşluk | mention.open_space |
| Aramalar paneli | tümü, cevapsız, reddedilen ve cevaplanan sekmeleri arasında geçiş yap | Kontrol+Tab | calls.next_filter |
| Aramalar paneli | Önceki arama listesi filtresi | Kontrol+Shift+Tab | calls.previous_filter |
| Aramalar paneli | aramanın sohbetini aç | Enter | calls.open_chat |
| Durum paneli | Odaklanan durumu aç veya oynat | Boşluk | status_list.open |
| Yanıtı gönder, Medya görüntüleyici | Yanıtı gönder | Enter | status_reply.send |
| Ses Cihazları | Numaralı ses aygıtını seç (0) | 0 | device.pick[0] |
| Ses Cihazları | Seçilen ses aygıtını kullan | Enter | device.pick_selected |
| Ses Cihazları | Numaralı ses aygıtını seç (1) | 1 | device.pick[1] |
| Ses Cihazları | Numaralı ses aygıtını seç (2) | 2 | device.pick[2] |
| Ses Cihazları | Numaralı ses aygıtını seç (3) | 3 | device.pick[3] |
| Ses Cihazları | Numaralı ses aygıtını seç (4) | 4 | device.pick[4] |
| Ses Cihazları | Numaralı ses aygıtını seç (5) | 5 | device.pick[5] |
| Ses Cihazları | Numaralı ses aygıtını seç (6) | 6 | device.pick[6] |
| Ses Cihazları | Numaralı ses aygıtını seç (7) | 7 | device.pick[7] |
| Ses Cihazları | Numaralı ses aygıtını seç (8) | 8 | device.pick[8] |
| Ses Cihazları | Numaralı ses aygıtını seç (9) | 9 | device.pick[9] |
| Ana gezinti | eşleşen eşlenmiş hesaba geç (1) | Kontrol+Alt+1 | main.account[1] |
| Ana gezinti | eşleşen eşlenmiş hesaba geç (2) | Kontrol+Alt+2 | main.account[2] |
| Ana gezinti | eşleşen eşlenmiş hesaba geç (3) | Kontrol+Alt+3 | main.account[3] |
| Ana gezinti | eşleşen eşlenmiş hesaba geç (4) | Kontrol+Alt+4 | main.account[4] |
| Ana gezinti | eşleşen eşlenmiş hesaba geç (5) | Kontrol+Alt+5 | main.account[5] |
| Ana gezinti | eşleşen eşlenmiş hesaba geç (6) | Kontrol+Alt+6 | main.account[6] |
| Ana gezinti | eşleşen eşlenmiş hesaba geç (7) | Kontrol+Alt+7 | main.account[7] |
| Ana gezinti | eşleşen eşlenmiş hesaba geç (8) | Kontrol+Alt+8 | main.account[8] |
| Ana gezinti | eşleşen eşlenmiş hesaba geç (9) | Kontrol+Alt+9 | main.account[9] |
| Ana gezinti | diğerlerini açık bırakarak geçerli hesabı kapat | Kontrol+F4 | main.close_account |
| Gelen arama | Cevapla | Alt+C | incoming.incoming_call_answer_button |
| Gelen arama | Görüntülü cevapla | Alt+C | incoming.incoming_call_answer_with_video_button |
| Gelen arama | Görüntüsüz cevapla | Alt+Z | incoming.incoming_call_answer_without_video_button |
| Gelen arama | Reddet | Alt+R | incoming.incoming_call_reject_button |
| Gelen arama | Sessize al | Alt+S | incoming.incoming_call_silence_button |
| Gelen arama | Pencereyi kapat | Alt+K | incoming.incoming_call_close_button |
