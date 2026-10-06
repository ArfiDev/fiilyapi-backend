"""Satinalma korkuluklari ve Turkce hata metinleri (SA spec §2, §3, §4).

`inventory/guards.py` deseninin kardesi: hata SINIFLARI `app/core/errors.py`de,
METINLER burada TEK kopya sabit olarak durur; router'a ya da servise gomulu
string YAZILMAZ.

## Hangi kural hangi koda duser (ST §4b kanonu)

| Durum | Kod | Sinif |
|---|---|---|
| Gorunmeyen ya da var olmayan tedarikci/talep | 404 | `NotFoundError` |
| Govdedeki proje/santiye/bolum/malzeme referansi gorunmuyor ya da yok | 404 | `NotFoundError` |
| Bicim/kural ihlali (XOR, miktar, uzunluk, `limit` tavani) | 422 | Pydantic |
| Taslak DISINDA duzenleme/silme | 409 | `ConflictError` |
| Baskasinin taslagini silme (`can_delete` reddi) | 403 | `DeleteNotAllowedError` |

**Kanon tek cumledir (ST T4-artcisi, 2026-08-11 kullanici karari):**
*gorunmez/yok VARLIK referansi = 404 · bicim/kural ihlali = 422.* Bu modulun
UCLARI o kanona ST ile BIREBIR uyar — emsal ayrismasi birakilmaz.

**Durum cakismasi 409'dur, 403 DEGIL** (`progress_payments.INVALID_STATUS_
TRANSITION` deseni): kullanicinin yetkisi VARDIR, engelleyen sey kaydin
DURUMUDUR. Silme reddi ise 403'tur cunku orada engel YETKIDIR.
"""

PERMISSION_MODULE = "procurement"
"""Spec §2: izin anahtari seed'de ZATEN vardi ("Satinalma & Teklif", 10. modul,
grup STOK_SATINALMA) — yeni izin modulu ACILMAZ, izin migration'i YOKTUR.

Kapilar (`roles/seed_data.py` matrisi: sef/saha `request`, PM `approve`,
satinalma/patron `full`, sysadmin `admin`):
* okuma                → `view`
* TALEP yazimi         → `request`  (talebi sahadan acan sef ve saha muhendisidir)
* TEDARIKCI/TEKLIF/SIPARIS yazimi → `full` (katalog ve pazarlik satinalmanin isi)
* ONAY/RET             → `approve`  (+ ₺500K ustu `full`, T3 esigi)

Sabit `service.py`de DEGIL burada durur: `repository.actor_level` de ona
ihtiyac duyar ve `repository → service` ithalati donguye girerdi. `service.
PERMISSION_MODULE` geriye donuk takma addir.
"""

__all__ = [
    "APPROVAL_THRESHOLD_EXCEEDED",
    "PERMISSION_MODULE",
    "INVALID_ORDER_TRANSITION",
    "INVALID_REQUEST_TRANSITION",
    "ORDER_MISSING",
    "QUOTE_MISSING",
    "QUOTE_SHIPPING_CONFLICT",
    "REQUEST_LINE_DUPLICATE",
    "REQUEST_LINE_INVALID",
    "REQUEST_LINE_SOURCE_CONFLICT",
    "REQUEST_MISSING",
    "REQUEST_NOT_DRAFT",
    "REQUEST_NOT_QUOTE_WAIT",
    "REQUEST_PROJECT_INVALID",
    "REQUEST_SECTION_INVALID",
    "REQUEST_SITE_INVALID",
    "REQUEST_STOCK_ITEM_INVALID",
    "SUPPLIER_MISSING",
]

# 404 — tedarikci katalogunda KAPSAM SUZGECI YOKTUR (tabloda `project_id` kolonu
# bile yoktur, spec §2: ayni "Demirsan A.S." her projede kullanilir). Bu yuzden
# "gorunmeyen tedarikci" diye bir durum da yoktur; tek 404 sebebi kaydin
# gercekten olmamasidir. IDOR unutulmus DEGILDIR — sonraki okuyucu buraya proje
# suzgeci EKLEMESIN. (Kartin PARA turevi ise kapsamlidir; bkz. `repository`.)
SUPPLIER_MISSING = "Tedarikçi bulunamadı"

# 404 — gorunmeyen talep ile var olmayan talep AYNI govdeyi alir. 403 verilseydi
# elinde kimlik olan kullanici kaydin var oldugunu ogrenirdi.
REQUEST_MISSING = "Satın alma talebi bulunamadı"

# 404 — govdedeki `project_id` gorunmuyor ya da hic yok. Iki durum AYNI cumleyi
# alir; ayri cumleler kimligin varligini ele verirdi.
REQUEST_PROJECT_INVALID = "Seçilen proje bulunamadı"

# 404 — `site_id` yok, gorunmuyor YA DA talebin projesine ait degil. Ucuncu dal
# da ayni cumleyi alir: baska projenin santiyesi bu talep icin "yok"tur ve
# ayri bir mesaj hangi santiyenin nerede oldugunu ele verirdi.
REQUEST_SITE_INVALID = "Seçilen şantiye bulunamadı"

# 404 — `section_id` yok ya da SECILEN SANTIYEYE ait degil. Bolum santiyenin ic
# kirilimidir (P2 karari): santiyesiz bolum secimi de bu cumleyi alir, cunku
# bolum ancak santiyesiyle birlikte anlamlidir.
REQUEST_SECTION_INVALID = "Seçilen bölüm bulunamadı"

# 404 — kalemdeki `stock_item_id` katalogda YOK (ST'nin `ENTRY_ITEM_INVALID`
# kardesi). Metin KAC satirin ya da HANGI kimligin hatali oldugunu SOYLEMEZ:
# kimlik sizdirmaz ve eyleme donuktur (`SITE_HAS_BLOCKS` dersi).
REQUEST_STOCK_ITEM_INVALID = "Seçilen malzeme kartı bulunamadı"

# 404 — PATCH kalemindeki `id` bu talebin satirlariyla eslesmiyor (yok ya da baska talebin).
REQUEST_LINE_INVALID = "Seçilen talep kalemi bulunamadı"

# 422 — ayni satir `id`si govdede birden cok kez.
REQUEST_LINE_DUPLICATE = "Aynı talep kalemi birden fazla kez gönderildi"

# 422 — birlestirilmis kalem stok kartina VE serbest tanima ayni anda bagli ya da ikisi de bos.
REQUEST_LINE_SOURCE_CONFLICT = (
    "Kalem ya stok kartından seçilir ya da serbest tanımlanır (ad ve birim); ikisi birden olmaz"
)

# 409 — taslak disinda duzenleme/silme. Onaya gonderilmis bir talebin kalemi
# degistirilebilseydi ONAY EDILEN SEY ile SIPARIS EDILEN SEY ayrisirdi (esik
# atlatmanin da en kisa yolu budur: dusuk tutarla onaylat, sonra sisir).
REQUEST_NOT_DRAFT = "Yalnızca taslak talep düzenlenebilir veya silinebilir"

# --- T3: onay akisi, teklif ve siparis ---

# 409 — talebin gecis matrisinde (`transitions.REQUEST_TRANSITIONS`) OLMAYAN her
# cift. Metin HANGI durumda oldugunu SOYLEMEZ: kapsam suzgecini gecmis bir
# kullaniciya bile gereksiz ayrinti verilmez ve tek cumle dort islemin hepsini
# karsilar (`progress_payments.INVALID_STATUS_TRANSITION` deseni).
INVALID_REQUEST_TRANSITION = "Satın alma talebinin durumu bu işleme uygun değil"

# 409 — siparisin gecis matrisinde OLMAYAN her cift. `in_transit → delivered`
# DAHILDIR: teslim damgasini stok girisi atar (§7 S4, T4). Elle yapilabilseydi
# hic mal girmemis bir siparis teslim gorunur, stok bakiyesiyle satinalma kaydi
# sessizce ayrisirdi.
INVALID_ORDER_TRANSITION = "Siparişin durumu bu işleme uygun değil"

# 403 — ₺500K ve ustu talep `full` seviyesi ister (FST 166 "Patron").
# Kullanici hangi kapinin kapali oldugunu bilmeli: "yetkiniz yok" cumlesi onu izin
# ekranina degil bosluga gonderirdi.
APPROVAL_THRESHOLD_EXCEEDED = "Bu tutardaki bir talebi onaylamak için üst seviye yetki gerekir"

# 409 — teklif YAZMA yalniz `quote_wait`te acik (OKUMA her durumda serbesttir:
# siparise donmus talebin karsilastirma gecmisi silinmez). Ayri metin: kullanici
# "yanlis durum" ile "yanlis islem"i ayirt edebilsin.
REQUEST_NOT_QUOTE_WAIT = "Teklifler yalnızca teklif bekleyen talebe eklenebilir"

# 404 — teklif yok, gorunmuyor YA DA BASKA TALEBIN altinda (yol caprazi).
# Ucuncu dal da ayni cumleyi alir: baska talebin teklifi bu talep icin YOKTUR
# ve ayri bir mesaj teklifin nerede oldugunu ele verirdi (ST §4b kanonu).
QUOTE_MISSING = "Teklif bulunamadı"

# 404 — siparis yok ya da projesi gorunmuyor.
ORDER_MISSING = "Sipariş bulunamadı"

# 422 — TEK 90 iki hali ayirir: "Dahil" ya da "Hariç (+₺8.000)". Ikisi birden
# gonderilirse hangisinin gecerli oldugu belirsizdir. Kural BIRLESIK degerler
# uzerinde kosar (PATCH kismi govde gonderir) — bu yuzden semada degil serviste.
QUOTE_SHIPPING_CONFLICT = "Nakliye dahilse ayrıca nakliye tutarı girilemez"
