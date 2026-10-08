# Kısayol düzenleme test senaryoları

Bu matris odak, açık pencere, seçim ve atama durumu arasındaki farkları kontrol eder. Otomatik senaryolar gerçek uygulama penceresi veya klavye kancası açmadan, üretimdeki işleyicileri kayıt yapan nesnelere bağlar. Gerçek tuş yakalama, NVDA'nın konuşması ve Windows'un global kısayolu kabul etmesi ayrıca canlı kabul testi gerektirir.

| Senaryo | Beklenen sonuç | Otomatik kapsam |
| --- | --- | --- |
| Katalogdaki her komut yeni bir birleşime taşınır | Yeni birleşim yalnızca tanımlı bağlamda özgün komuta dönüşür; eski birleşim komutu çalıştırmaz | Her bağlam kaydı için parametrik test |
| Aynı tuş ilgisiz pencerede basılır | Başka pencerenin komutuna dönüşmez | Her bağlam kaydı için parametrik test |
| Mesaj listesinde Boşluk, seçim yok | Odaklanan mesajın oynatma işleyicisi çalışır | Özgün ve değiştirilmiş tuş |
| Mesaj listesinde Boşluk, seçim modu açık ve seçili mesaj var | Oynatma yerine seçim işleyicisi çalışır | Özgün ve değiştirilmiş tuş |
| Mesaj listesinde seçim var ama seçim modu kapalı | Oynatma davranışı korunur | Özgün ve değiştirilmiş tuş |
| Yazma alanında Boşluk | Normal metin tuşu olarak bırakılır; mesaj/medya oynatılmaz | Üç farklı kısayol yapılandırması |
| Medya görüntüleyicide cevap, metin veya kaydırıcı odakta | Oynatma tuşu bu kontrolün girişini çalmaz | Dört kontrol × özgün/değiştirilmiş atama |
| Medya görüntüleyicide oynatma kontrolü odakta | Yeni oynatma tuşu çalışır; taşınan Boşluk oynatmaz | Gerçek medya tuş işleyicisi |
| Gezinme listesi açma tuşu değişir | Yalnızca odaklanan gezinme satırı açılır | Gerçek gezinme tuş işleyicisi |
| Sohbet araması Ctrl+F'den taşınır; Ctrl+F çoklu seçime verilir | Yeni seçim, ana ve arşiv listelerinde çalışır | Gerçek iki liste işleyicisi |
| Üst kapsamın yeni tuşu eski bir alt kapsam tuşuna dönüşür | Alt kapsam mantıksal tuşu fiziksel giriş sanıp yeniden yönlendirmez | Ardışık kapsam dönüşümü |
| Alt kapsamın eski tuşu ana pencere komutuna atanır | Eski alt komut çalışmaz; gerçek üst tabloya geçiş izinlidir | Odak kontrolünün gerçek üst zinciri |
| Eski tuş kaldırılır ve başka komuta verilmez | Eski komut veya yerel Enter gönderme yolu çalışmaz | Kaldırılmış atama senaryoları |
| Bahsetme önerisi açıkken Ekle/Kapat değiştirilir veya kaldırılır | Erken CHAR_HOOK da yeni ayarı okur; eski Enter/Escape çalışmaz | Her iki komut × taşıma/kaldırma |
| Gelen aramayı yanıtlama tuşu taşınır veya kaldırılır | Eski Alt birleşimi tablodan çıkar; yeni birleşim doğru düğmeye gider; düz harf cevaplamaz | Gerçek tablo kurucusu ve düğme yönlendirmesi |
| Durum cevap gönderme tuşu değişir | Yeni tuş bir kez gönderir; eski Enter göndermez; Boşluk normaldir | Ortak Enter işleyicisi |
| Mesaj gönderme Enter'dan taşınır | Yeni tuş gönderir; Enter satır ekler; Shift+Enter satır eklemeyi korur | Gerçek yazma alanı işleyicisi |
| Boşalan Enter/Ctrl+Tab ana pencere komutuna atanır | Yazma ve Aramalar kancaları yeni üst komutu engellemez | Gerçek yazma ve Aramalar işleyicileri |
| Cevap, grup izni veya kişi adı içeren alan varken ayar uygulanır | Alanın anlamsal adı korunur; yalnızca taşınan mnemonik kaldırılır | Bağlamsal ad ve varsayılana dönüş testleri |
| Sohbet açılır/yeniden adlandırılır veya cevap modu biter | Eski yerel odak mnemonikleri kendiliğinden geri gelmez | Ortak alan adı güncellemesi ve mevcut yeniden adlandırma testleri |
| Türkçe Ayarlar gezinme kısayolu değişir | Eski ctrl+virgül ipucu kaldırılır, yeni birleşim gösterilir | Yerelleştirilmiş ipucu testi |
| Cevap gönderme tuşu medya veya klasik durum komutuyla aynı olur | İki pencerenin kapsamları da çakışma kontrolüne katılır | Ortak komut kimliğinin tüm kapsamları |
| Mesaj yer imi ana pencere tuşuna atanır | Mesaj satırından düzenlense bile ana pencere çakışması bulunur | Ortak yer imi kapsamı |
| Düzenleme yapılır ama Ayarlar uygulanmaz | Kaydedilmiş ayarlar değişmez | Geçici/kalıcı sözlük ayrımı |
| Uygula sonrası geçici liste değiştirilir | Kaydedilmiş atamalar sonradan sessizce değişmez | Derin kopya kontrolü |
| Kısayol kaldırılır, global kısayol da boş | İki boş atama çakışma sayılmaz | Kayıt öncesi doğrulama |
| Genel sekmesinin eski global yakalama alanı çakışan tuşa ayarlanır | Uygula reddedilir; ayarlar yazılmaz | Kayıt öncesi tekrar kontrol |
| Tümünü varsayılana döndür | Yerel geçersiz kılmalar silinir, isteğe bağlı global tuş temizlenir | Ayar işleyicisi |
| Gizli kasa vardır | Kilitli liste/gezinti/acil kilitleme satırları listeden bilgi sızdırmaz | Envanter görünürlüğü |
| Sayısal tuş takımındaki eski rakam taşınmıştır | Ana sıradaki rakam gibi devre dışı kalır | Tuş normalleştirme |
| Windows değiştiricisi uygulama atamasına eklenir | Uygulama komutuna eşleşmez | Değiştirici kontrolü |
| Bozuk/eksik/yanlış türde içe aktarılan atama vardır | Bilinen ve geçerli kayıtlar alınır; açık null korunur; bozuk kayıt hazır atamaya döner | Saf ayar ve settings-transfer testleri |
| Ana pencere/alt tablo aynı hazır tuşu kullanır | Mevcut öncelik korunur; yeni çakışma reddedilir | Çakışma regresyonları |
| Hazır dil mnemonikleri veya çeviriler değişir | Tüm kayıtlı dillerin metinleri/placeholder'ları ve gettext derlemesi tutarlıdır | Çeviri kontrolü ve mevcut dil testleri |

Ek regresyon grupları: kayıt ve ileri/geri sarma, çoklu sohbet/mesaj işlemleri, Shift+Enter, F3 arama, global yer imi sıfır kancası, hesap geçişinde odak, kilitli kasa ayarları, erişilebilirlik kısayol bilgileri, menü ve yardım metni, ayar aktarımı. Depodaki varsayılan test paketi gerçek diyalog gösteren `wxgui` senaryolarını bilinçli olarak atlar.
