"""Araç kataloğu — **TEK KAYNAK** (spec §5.2, AI-0b dilimi).

Dört araç üç kapı sınıfını da sınar, artı `navigate_to` ve `yetkilerim`.

🔴 **Dinamik/gecikmeli araç yükleme REDDEDİLDİ.** Canlı kusurun kök sebebi tam
olarak "prompt araçları eksik listeliyordu"ydu (S9). Katalog her turda TAM
listelenir; prompt uzunluğu sorun olursa çözüm **açıklamaları kısaltmaktır,
listeyi değil**.

🔴 **`PROPOSE_TOOLS` boş ama dallanma ŞİMDİ yazılır** (spec T5). Boş bir liste
"kapı çalışıyor"u kanıtlamaz; bu yüzden testler **sahte** bir propose aracıyla
kapıyı ölçer (`patron` = matriste `_V`, `_F` bile olsa göremez).

## Yönetişim denylist'i (S16/S17, bekçi B26)

`{user_management, settings, approvals, roles}` modüllerine **dokunan** hiçbir
araç kaydedilemez... **bir istisnayla**: `onay_kutum` `approvals` modülünün
`GET /approvals` ucunu sarar ama o uç **kapısızdır** (`kapilar == ∅`) ve
denylist kapı modülleri üzerinden çalışır. Yani denylist "yazma yüzeyine dokunma"
kuralıdır ve `onay_kutum` hiçbir kapı beyan etmediği için ona takılmaz. Bu
inceliğin kendisi bekçilenir: `onay_kutum`a `("approvals", view)` kapısı
eklenirse B26 kırmızı olur.
"""

from __future__ import annotations

import uuid
from typing import Final

from app.core.access import AccessLevel
from app.modules.ai.registry import ToolKapsami, ToolKumesi, ToolRegistry, ToolSpec
from app.modules.ai.tools import schemas
from app.modules.ai.tools.reads import ai2bd, handlers

#: 🔴 Bu modüllerin **kapısını** taşıyan hiçbir araç kaydedilemez (B26).
#: Gerekçe S17: "yalnız sysadmin yazar" cümlesi, AI'ın izin matrisini yeniden
#: yazabilmesi demek DEĞİLDİR. Tek kabul edilen öneri kalıcıdır — eşik
#: 500.000 → 999.999.999 yapılırsa Patron adımı bir daha asla eklenmez ve ekran
#: hiçbir anda "bozuldu" demez.
YONETISIM_DENYLIST: Final[frozenset[str]] = frozenset(
    {"user_management", "settings", "approvals", "roles"}
)


PROJELERI_LISTELE = ToolSpec(
    ad="projeleri_listele",
    aciklama=(
        "NE ZAMAN: kullanıcı projelerini, proje kodunu/adını ya da kaç projesi "
        "olduğunu sorduğunda. NE SORMAZ: bütçe/ilerleme detayı, maliyet, kâr. "
        "Dönen liste kullanıcının GÖRÜNÜR projeleridir; boş küme 'proje yok' "
        "DEĞİL 'senin kapsamında yok' anlamına gelebilir."
    ),
    kapsam=ToolKapsami.MODUL_KAPISI,
    kume=ToolKumesi.PROJE_KAPSAMLI,
    kapilar=frozenset({("projects", AccessLevel.view)}),
    ucler=("/projects",),
    #: `ProjectListItem` alanları — kişisel veri YOK, hepsi `projects`.
    veri_modulleri=frozenset({"projects"}),
    yol_parametreleri={},
    girdi=schemas.BosGirdi,
    yanit_modeli=schemas.AiProjeListesi,
    calistir=handlers.projeleri_listele,
)

ONAY_KUTUM = ToolSpec(
    ad="onay_kutum",
    aciklama=(
        "NE ZAMAN: kullanıcı 'onayımda ne var', 'imzam bekleyen' diye sorduğunda. "
        "NE SORMAZ: onaylama/reddetme — bu araç YALNIZ OKUR. Dönen küme zaten "
        "'bu adım SANA düştü' olgusuyla sınırlıdır; boş olması yetki reddi DEĞİLDİR."
    ),
    # 🔴 Kapının MEKANİK TÜRETİLEMEYECEĞİNİN canlı vakası. Router: "Ayri bir
    # yetki kapisi YOKTUR ve olmamalidir".
    kapsam=ToolKapsami.KENDI_KUMESI,
    kume=ToolKumesi.PROJE_KAPSAMLI,
    kapilar=frozenset(),
    ucler=("/approvals",),
    #: 🔴 `kapilar` BOŞ ama veri BOŞ DEĞİL. Kutu üç evrak ailesinin künyesini
    #: taşır (`progress_payments` + `procurement`) ve evrağı YARATANIN adını
    #: (`created_by_name`) basar. `kapilar`dan türeten bir sistem burada
    #: "hiçbir modülün verisi" derdi — ölçülmüş deliğin ta kendisi.
    veri_modulleri=frozenset({"approvals", "progress_payments", "procurement"}),
    yol_parametreleri={},
    girdi=schemas.BosGirdi,
    yanit_modeli=schemas.AiOnayKutusu,
    calistir=handlers.onay_kutum,
)

PUANTAJ_HAFTASI = ToolSpec(
    ad="puantaj_haftasi",
    aciklama=(
        "NE ZAMAN: belirli bir şantiyenin belirli bir ISO haftasındaki puantaj "
        "özeti sorulduğunda. NE SORMAZ: kişi bazlı gün kodları, ücret, bordro. "
        "Şantiyeye erişimin yoksa 'kayıt yok' cevabı gelir — bu, şantiyenin var "
        "olmadığı anlamına GELMEZ."
    ),
    kapsam=ToolKapsami.MODUL_KAPISI,
    kume=ToolKumesi.PROJE_KAPSAMLI,
    kapilar=frozenset({("timesheet", AccessLevel.view)}),
    ucler=("/sites/{site_id}/timesheet/week",),
    #: Haftalık özet şantiye + proje adını da basar; `personnel` **taşımaz**
    #: (kişi bazlı gün kodu araç şemasında YOK — `aciklama` bunu söyler).
    veri_modulleri=frozenset({"timesheet", "sites", "projects"}),
    yol_parametreleri={"site_id": uuid.UUID},
    girdi=schemas.PuantajHaftasiGirdi,
    yanit_modeli=schemas.AiPuantajHaftasi,
    calistir=handlers.puantaj_haftasi,
)

GOSTERGE_OZETI = ToolSpec(
    ad="gosterge_ozeti",
    aciklama=(
        "NE ZAMAN: 'genel durum', 'özet', 'panelde ne var' sorulduğunda. "
        "NE SORMAZ: tek bir projenin detayı. ⚠️ Bu yanıttaki proje sayıları ÜÇ "
        "AYRI ŞEYİ sayar ve birbirinden TÜRETİLEMEZ; risk kartı sessizce kırpılır "
        "ve toplam sayı BİLDİRMEZ."
    ),
    kapsam=ToolKapsami.MODUL_KAPISI,
    # 🔴 `SIRKET_GENELI`nin İLK GERÇEK KULLANIMI — ve bir DÜZELTMEdir.
    #
    # Bu araç `PROJE_KAPSAMLI` beyan ediyordu; ölçüm bunu çürüttü. Zincir:
    # `dashboard/service.py::_risks` → `dashboard/risks.py::build_risks` →
    # `_stock_alerts` → `inventory/repository.py::visible_warehouse_ids` →
    # `_warehouse_scope`, ve o süzgeç **İKİ DALLI ve OR**'ludur:
    #
    #     Warehouse.site_id.is_(None) | Warehouse.site_id.in_(gorunen_santiyeler)
    #
    # Yani MERKEZ DEPO (`site_id IS NULL`) kapsam süzgecine **TABİ DEĞİLDİR** —
    # `_warehouse_scope` docstring'i bunu bilinçli bir karar olarak yazar
    # ("Merkez dalı OR'dan çıkarılsaydı şirketin ana ambarı hiç kimseye
    # görünmezdi"). Sonuç: `risk_notu`daki sayı, aktörün HİÇBİR projesine bağlı
    # olmayan satırları içerebilir. `equipment/repository.py::scope` de birebir
    # aynı OR'u taşır — desen tek değil, ikizdir.
    #
    # Kartın öbür yarısı (`portfoy`, `gorunur_proje_sayisi`) gerçekten proje
    # kapsamlıdır; yani araç KARIŞIKTIR. Fail-closed okuma `SIRKET_GENELI`dir:
    # kapsam notunun işi modelin "senin kapsamında" demesini engellemektir ve
    # yanıtın **bir bölümü** bile kapsam dışıysa o cümle yalan olur.
    #
    # ⚠️ Ve not TERSİNE ÇEVRİLEMEZ: dolu bir küme kapsam iznini KANITLAMAZ.
    # `guards.KAPSAM_NOTLARI["sirket_geneli"]` bunu açıkça söyler.
    kume=ToolKumesi.SIRKET_GENELI,
    kapilar=frozenset({("dashboard", AccessLevel.view)}),
    ucler=("/dashboard/summary",),
    #: 🔴 TEK KAPI, BEŞ MODÜL — kapıdan türetmenin çürütüldüğü vaka.
    #: `_portfolio` → `progress_payments`; `_risks` → `inventory` + `sites` +
    #: `progress_payments`; `projects` dizisi ve iki sayaç → `projects`.
    veri_modulleri=frozenset({"dashboard", "projects", "progress_payments", "inventory", "sites"}),
    yol_parametreleri={},
    girdi=schemas.BosGirdi,
    yanit_modeli=schemas.AiGostergeOzeti,
    calistir=handlers.gosterge_ozeti,
)

YETKILERIM = ToolSpec(
    ad="yetkilerim",
    aciklama=(
        "NE ZAMAN: bir sorunun cevabını verebilmek için önce kullanıcının neyi "
        "görebildiğini bilmen gerektiğinde, ya da kullanıcı yetkisini sorduğunda. "
        "NE SORMAZ: başka bir kullanıcının yetkisi. Listede olmayan bir modül "
        "'yok' DEMEK DEĞİLDİR — izin satırı olmayan modül burada hiç görünmez."
    ),
    kapsam=ToolKapsami.KENDI_KUMESI,
    kume=ToolKumesi.KAPSAMSIZ,
    kapilar=frozenset(),
    ucler=("/auth/me",),
    #: Yalnız aktörün KENDİ izin haritası. `users` bir izin modülü değildir;
    #: taşıdığı tek şey `ai` modülünün kendi meta verisidir.
    veri_modulleri=frozenset({"ai"}),
    yol_parametreleri={},
    girdi=schemas.BosGirdi,
    yanit_modeli=schemas.AiYetkilerim,
    calistir=handlers.yetkilerim,
)

NAVIGATE_TO = ToolSpec(
    ad="navigate_to",
    aciklama=(
        "NE ZAMAN: kullanıcının istediği şey bir ekranda yapılıyorsa ve elinde "
        "uygun bir veri aracı YOKSA. NE SORMAZ: veri — bu araç hiçbir kayıt "
        "okumaz, yalnız bir ekran önerir. Eşleşen bir veri aracı yoksa "
        "çağırabileceğin TEK araç budur."
    ),
    kapsam=ToolKapsami.KENDI_KUMESI,
    kume=ToolKumesi.KAPSAMSIZ,
    kapilar=frozenset(),
    #: Hiçbir uca gitmez. Boş demet, `ReadOnlyTransport`un HER yolu reddetmesi
    #: demektir — yani bu araç bir GET denese fail-closed patlar.
    ucler=(),
    #: Hiçbir kayıt okumaz — beyan da boştur ve `dogrula_spec` bunu yalnız
    #: `ucler` boş olduğu için kabul eder.
    veri_modulleri=frozenset(),
    yol_parametreleri={},
    girdi=schemas.YonlendirGirdi,
    yanit_modeli=schemas.AiYonlendirme,
    calistir=handlers.navigate_to,
)


AI0B_TOOLS: Final[tuple[ToolSpec, ...]] = (
    PROJELERI_LISTELE,
    ONAY_KUTUM,
    PUANTAJ_HAFTASI,
    GOSTERGE_OZETI,
    YETKILERIM,
    NAVIGATE_TO,
)

# =========================================================================== #
# AI-2b — proje · şantiye · poz · arsa payı · hakediş · sözleşme · saha
# =========================================================================== #
#
# 🔴 `kapilar` HER ARAÇTA ucun GERÇEK kapısına ölçümle eşitlendi (rota
# tablosundan, `require_permission` kapanışının serbest değişkenlerinden). Üç
# uç yanıltıcıdır ve üçü de burada ADIYLA geçer:
#
#   * `…/plan/day-summary` → izin modülü **`site_diary`** (`site_planning` bir
#     izin modülü DEĞİLDİR),
#   * `…/land-share/summary` → **`projects`**,
#   * `/subcontractor-progress-payments` → **`progress_payments`**.
#
# 🔴 `kume` beyanı da ölçümle verildi. `SIRKET_GENELI` beyan eden BEŞ araç var
# ve beşinin de gerekçesi AYNI ölçülmüş desendir: kapsam süzgeci **iki dallı ve
# OR'ludur**, `site_id IS NULL` dalı kapsama TABİ DEĞİLDİR
# (`equipment/repository.py::scope` · `work_log_scope` · `fuel_log_scope` ·
# `rental_repository.py::invoice_scope`). `taseronlar` ise daha da nettir: uç
# `user` parametresi bile ALMAZ.


PROJE_DETAYI = ToolSpec(
    ad="proje_detayi",
    aciklama=(
        "NE ZAMAN: tek bir projenin künyesi, bütçesi, sözleşme tutarı ya da "
        "ilerlemesi sorulduğunda. NE SORMAZ: işverenin vergi/iletişim bilgisi, "
        "maliyet kırılımı, ünite satışı. `progress_pct` MALİ ilerlemedir, "
        "fiziksel DEĞİL. Görünmeyen proje ile var olmayan proje AYNI cevabı verir."
    ),
    kapsam=ToolKapsami.MODUL_KAPISI,
    kume=ToolKumesi.PROJE_KAPSAMLI,
    kapilar=frozenset({("projects", AccessLevel.view)}),
    ucler=("/projects/{project_id}",),
    #: Yalnız `projects`: `employer` nesnesi (ve `tax_number`) OKUNMAZ.
    veri_modulleri=frozenset({"projects"}),
    yol_parametreleri={"project_id": uuid.UUID},
    girdi=schemas.ProjeKimligiGirdi,
    yanit_modeli=schemas.AiProjeDetayi,
    calistir=ai2bd.proje_detayi,
)

SANTIYELERI_LISTELE = ToolSpec(
    ad="santiyeleri_listele",
    aciklama=(
        "NE ZAMAN: 'hangi şantiyeler var', 'şantiye listesi' sorulduğunda ya da "
        "başka bir aracın istediği `site_id`yi bulman gerektiğinde. NE SORMAZ: "
        "şantiye detayı, işçi sayısı, ilerleme. Boş küme 'şantiye yok' DEĞİL "
        "'senin kapsamında yok' anlamına gelebilir."
    ),
    kapsam=ToolKapsami.MODUL_KAPISI,
    kume=ToolKumesi.PROJE_KAPSAMLI,
    kapilar=frozenset({("sites", AccessLevel.view)}),
    ucler=("/sites",),
    #: Satır şantiye + PROJE adını taşır.
    veri_modulleri=frozenset({"sites", "projects"}),
    yol_parametreleri={},
    girdi=schemas.BosGirdi,
    yanit_modeli=schemas.AiSantiyeListesi,
    calistir=ai2bd.santiyeleri_listele,
)

SANTIYE_DETAYI = ToolSpec(
    ad="santiye_detayi",
    aciklama=(
        "NE ZAMAN: tek bir şantiyenin künyesi, şefi, tarihleri ya da bölüm "
        "sayısı sorulduğunda. NE SORMAZ: adres, puantaj, poz cetveli, günlük "
        "kayıt — bunların ayrı araçları var. Erişemediğin şantiye 'kayıt yok' "
        "cevabı alır; bu şantiyenin var OLMADIĞI anlamına gelmez."
    ),
    kapsam=ToolKapsami.MODUL_KAPISI,
    kume=ToolKumesi.PROJE_KAPSAMLI,
    kapilar=frozenset({("sites", AccessLevel.view)}),
    ucler=("/sites/{site_id}",),
    veri_modulleri=frozenset({"sites", "projects"}),
    yol_parametreleri={"site_id": uuid.UUID},
    girdi=schemas.SantiyeKimligiGirdi,
    yanit_modeli=schemas.AiSantiyeDetayi,
    calistir=ai2bd.santiye_detayi,
)

IS_KALEMLERI = ToolSpec(
    ad="is_kalemleri",
    aciklama=(
        "NE ZAMAN: bir şantiyenin poz cetveli / iş kalemleri / metraj sorulduğunda. "
        "NE SORMAZ: hakediş, gerçekleşen imalat miktarı, bölüm bazlı tahsis. "
        "Üç toplam alanı bir SAYI olmayabilir: 'Bu değeri görme yetkiniz yok.' "
        "cümlesi geldiyse o toplam SIFIR DEĞİL, GÖRÜNMEZDİR."
    ),
    kapsam=ToolKapsami.MODUL_KAPISI,
    kume=ToolKumesi.PROJE_KAPSAMLI,
    kapilar=frozenset({("boq", AccessLevel.view)}),
    ucler=("/sites/{site_id}/boq",),
    #: Gövde yalnız poz cetvelidir; şantiye/proje ADI basılmaz.
    veri_modulleri=frozenset({"boq"}),
    yol_parametreleri={"site_id": uuid.UUID},
    girdi=schemas.SantiyeKimligiGirdi,
    yanit_modeli=schemas.AiIsKalemleri,
    calistir=ai2bd.is_kalemleri,
)

ARSA_PAYI = ToolSpec(
    ad="arsa_payi",
    aciklama=(
        "NE ZAMAN: kat karşılığı paylaşımı, arsa sahibi payı, hissedar dağılımı "
        "ya da paylaşım dengesi sorulduğunda. NE SORMAZ: ALICI kimliği ve ünite "
        "satış detayı — bu araç onları GETİRMEZ. Kat karşılığı olmayan proje "
        "'kayıt yok' alır; bu 'yetkin yok' DEMEK DEĞİLDİR. Adet dengesi ile "
        "değer dengesi AYRI iki karardır, birinden ötekini çıkarma."
    ),
    kapsam=ToolKapsami.MODUL_KAPISI,
    kume=ToolKumesi.PROJE_KAPSAMLI,
    kapilar=frozenset({("projects", AccessLevel.view)}),
    #: 🔴 TEK UÇ ve bilinçli seçim: `…/land-share/units` gövdesi `buyer_name`
    #: taşır (`Customer.name`, kapısı `sales` = KAPALI). Özet ucunda o alan HİÇ
    #: YOKTUR — düşürülmüş alan ile var olmayan alan aynı güvence değildir.
    ucler=("/projects/{project_id}/land-share/summary",),
    #: 🔴 YALNIZ `projects` (yönetim kararı). Ölçüldü: `landowner_name`
    #: `ProjectLandShare`ten, hissedar adları `LandShareShareholder`dan gelir;
    #: ikisi de `app/modules/projects/models.py`dedir. `sales` zinciri bu ucun
    #: gövdesine HİÇ girmez.
    veri_modulleri=frozenset({"projects"}),
    yol_parametreleri={"project_id": uuid.UUID},
    girdi=schemas.ProjeKimligiGirdi,
    yanit_modeli=schemas.AiArsaPayi,
    calistir=ai2bd.arsa_payi,
)

ISVEREN_HAKEDISLERI = ToolSpec(
    ad="isveren_hakedisleri",
    aciklama=(
        "NE ZAMAN: işverene kesilen hakedişler, hakediş sırası, brüt/net tutar "
        "sorulduğunda. NE SORMAZ: taşeron hakedişi (ayrı araç), hakediş "
        "satırları, onay zinciri. Sonuç KIRPILMIŞ gelebilir; kırpıldıysa "
        "toplam/oran HESAPLAMA."
    ),
    kapsam=ToolKapsami.MODUL_KAPISI,
    kume=ToolKumesi.PROJE_KAPSAMLI,
    kapilar=frozenset({("progress_payments", AccessLevel.view)}),
    ucler=("/progress-payments",),
    veri_modulleri=frozenset({"progress_payments", "projects"}),
    yol_parametreleri={},
    girdi=schemas.BosGirdi,
    yanit_modeli=schemas.AiIsverenHakedisListesi,
    calistir=ai2bd.isveren_hakedisleri,
    disiplin_kisitliya_kapali=True,  # Ü2: `/progress-payments` RequireUnrestricted
)

TASERON_HAKEDISLERI = ToolSpec(
    ad="taseron_hakedisleri",
    aciklama=(
        "NE ZAMAN: taşeronlara kesilen hakedişler, dönemi, taşeron adı ya da "
        "brüt/net tutarı sorulduğunda. NE SORMAZ: işveren hakedişi (ayrı araç), "
        "hakediş satırları, teminat/fiyat farkı kırılımı."
    ),
    kapsam=ToolKapsami.MODUL_KAPISI,
    kume=ToolKumesi.PROJE_KAPSAMLI,
    #: 🔴 İzin modülü `subcontractor_progress_payments` DEĞİL
    #: `progress_payments`tır (router'dan ölçüldü) — B10 küme eşitliği ister.
    kapilar=frozenset({("progress_payments", AccessLevel.view)}),
    ucler=("/subcontractor-progress-payments",),
    #: `subcontractor_name` + `contract_no` `SubcontractorContract`tan gelir.
    veri_modulleri=frozenset({"progress_payments", "projects", "contracts"}),
    yol_parametreleri={},
    girdi=schemas.BosGirdi,
    yanit_modeli=schemas.AiTaseronHakedisListesi,
    calistir=ai2bd.taseron_hakedisleri,
    disiplin_kisitliya_kapali=True,  # Ü2: `/subcontractor-progress-payments` RequireUnrestricted
)

SOZLESMELER = ToolSpec(
    ad="sozlesmeler",
    aciklama=(
        "NE ZAMAN: sözleşme listesi, sözleşme tutarı ya da süresi dolan "
        "sözleşmeler sorulduğunda. 🔴 `contract_type` ZORUNLUDUR ve yalnız "
        "'employer' (işveren) ya da 'subcontractor' (taşeron) olabilir; ikisi "
        "AYRI kümedir ve tek çağrıda birleşmez. NE SORMAZ: poz kalemleri, "
        "hakediş satırları, taşeron kartoteksi (ayrı araç)."
    ),
    kapsam=ToolKapsami.MODUL_KAPISI,
    kume=ToolKumesi.PROJE_KAPSAMLI,
    kapilar=frozenset({("contracts", AccessLevel.view)}),
    ucler=("/contracts",),
    #: `summary.progress_payment_total` hakediş modülünden türer; başlık ve
    #: karşı taraf adı projeden/sözleşmeden gelir.
    veri_modulleri=frozenset({"contracts", "projects", "progress_payments"}),
    yol_parametreleri={},
    girdi=schemas.SozlesmelerGirdi,
    yanit_modeli=schemas.AiSozlesmeListesi,
    calistir=ai2bd.sozlesmeler,
)

TASERONLAR = ToolSpec(
    ad="taseronlar",
    aciklama=(
        "NE ZAMAN: taşeron firma kartoteksi, taşeronun ilgili kişisi ya da "
        "kategorisi sorulduğunda. NE SORMAZ: vergi numarası, telefon, e-posta "
        "— bu araç onları GETİRMEZ. Bu liste PROJE KAPSAMLI DEĞİLDİR: şirketin "
        "tüm taşeronlarını içerir, bu yüzden buradan 'benim projemin taşeronu' "
        "SONUCU ÇIKARMA."
    ),
    kapsam=ToolKapsami.MODUL_KAPISI,
    # 🔴 `SIRKET_GENELI` — ve bu, iki dallı OR'dan DEĞİL, süzgecin HİÇ
    # OLMAMASINDAN gelir. Uç imzası `user` parametresi bile almaz;
    # `contracts/router.py` yorumu birebir: *"`visible_projects` süzgeci
    # BİLİNÇLİ OLARAK yok: kartoteks proje-bağımsızdır"*. `PROJE_KAPSAMLI`
    # yazmak kapsam notunu YALANA çevirirdi.
    kume=ToolKumesi.SIRKET_GENELI,
    kapilar=frozenset({("contracts", AccessLevel.view)}),
    ucler=("/subcontractors",),
    veri_modulleri=frozenset({"contracts"}),
    yol_parametreleri={},
    girdi=schemas.BosGirdi,
    yanit_modeli=schemas.AiTaseronListesi,
    calistir=ai2bd.taseronlar,
)

PUANTAJ = ToolSpec(
    ad="puantaj",
    aciklama=(
        "NE ZAMAN: bir şantiyenin AYLIK puantaj özeti (işçi sayısı, toplam "
        "saat, adam-gün, gün gün toplamlar) sorulduğunda. Haftalık özet için "
        "`puantaj_haftasi` var. 🔴 NE SORMAZ: KİŞİ BAZLI satır, isim, gün kodu, "
        "ücret — bu araç kişi satırı TAŞIMAZ ve taşıyamaz."
    ),
    kapsam=ToolKapsami.MODUL_KAPISI,
    kume=ToolKumesi.PROJE_KAPSAMLI,
    kapilar=frozenset({("timesheet", AccessLevel.view)}),
    ucler=("/sites/{site_id}/timesheet",),
    #: 🔴 `personnel` BEYAN EDİLMEZ — `puantaj_haftasi` emsali. Kişi satırı
    #: basılmadığı için beyan da edilmez; beyan edilseydi (AGREGA) `full_name`
    #: olmasa bile araç KAYDEDİLİRDİ ama beyan gerçeği aşardı.
    veri_modulleri=frozenset({"timesheet", "sites", "projects"}),
    yol_parametreleri={"site_id": uuid.UUID},
    girdi=schemas.PuantajAyiGirdi,
    yanit_modeli=schemas.AiPuantajAyi,
    calistir=ai2bd.puantaj,
)

GUNLUK_KAYIT = ToolSpec(
    ad="gunluk_kayit",
    aciklama=(
        "NE ZAMAN: bir şantiyenin günlük kayıtları, kayıt durumu, olay olup "
        "olmadığı ya da günlük işçi toplamı sorulduğunda. NE SORMAZ: kaydın "
        "SERBEST METNİ, imalat satırları, fotoğraf. Boş liste 'kayıt "
        "girilmemiş' olabilir; erişemediğin şantiye ise 'kayıt yok' alır."
    ),
    kapsam=ToolKapsami.MODUL_KAPISI,
    kume=ToolKumesi.PROJE_KAPSAMLI,
    kapilar=frozenset({("site_diary", AccessLevel.view)}),
    ucler=("/sites/{site_id}/diary",),
    veri_modulleri=frozenset({"site_diary"}),
    yol_parametreleri={"site_id": uuid.UUID},
    girdi=schemas.SantiyeKimligiGirdi,
    yanit_modeli=schemas.AiGunlukKayitListesi,
    calistir=ai2bd.gunluk_kayit,
)

GUN_PLANI = ToolSpec(
    ad="gun_plani",
    aciklama=(
        "NE ZAMAN: bir şantiyenin önümüzdeki günlerdeki iş planı, planlanan "
        "işçi sayısı ya da hangi bölümde çalışılacağı sorulduğunda. `start` "
        "ZORUNLUDUR ve HERHANGİ bir gün olabilir. NE SORMAZ: gerçekleşen iş "
        "(o `gunluk_kayit`tır), plan YAZMA. `has_plan=false` olan gün "
        "'plan girilmemiş'tir, 'iş yok' DEĞİL."
    ),
    kapsam=ToolKapsami.MODUL_KAPISI,
    kume=ToolKumesi.PROJE_KAPSAMLI,
    #: 🔴 İZİN MODÜLÜ TUZAĞI: uç `site_planning` paketindedir ama kapısı
    #: `site_diary`dir (`site_planning` bir izin modülü DEĞİLDİR). B10 küme
    #: eşitliği ister; `site_planning` yazmak turu kaybettirirdi.
    kapilar=frozenset({("site_diary", AccessLevel.view)}),
    ucler=("/sites/{site_id}/plan/day-summary",),
    veri_modulleri=frozenset({"site_diary", "sites", "projects"}),
    yol_parametreleri={"site_id": uuid.UUID},
    girdi=schemas.GunPlaniGirdi,
    yanit_modeli=schemas.AiGunPlani,
    calistir=ai2bd.gun_plani,
)


# =========================================================================== #
# AI-2d — makine
# =========================================================================== #

MAKINE_LISTESI = ToolSpec(
    ad="makine_listesi",
    aciklama=(
        "NE ZAMAN: makine/ekipman filosu, bir makinenin durumu, plakası ya da "
        "hangi şantiyede olduğu sorulduğunda. NE SORMAZ: çalışma saati, yakıt, "
        "kira bedeli — üçünün de ayrı aracı var. `site_id` boş olan makine "
        "DEPODADIR ve hiçbir projeye bağlı değildir."
    ),
    kapsam=ToolKapsami.MODUL_KAPISI,
    kume=ToolKumesi.SIRKET_GENELI,
    kapilar=frozenset({("equipment", AccessLevel.view)}),
    ucler=("/equipment",),
    veri_modulleri=frozenset({"equipment"}),
    yol_parametreleri={},
    girdi=schemas.BosGirdi,
    yanit_modeli=schemas.AiMakineListesi,
    calistir=ai2bd.makine_listesi,
)

MAKINE_CALISMA = ToolSpec(
    ad="makine_calisma",
    aciklama=(
        "NE ZAMAN: bir AYIN makine çalışma özeti — saat, arıza saati, kullanım "
        "yüzdesi, maliyet — sorulduğunda. `year` ve `month` ZORUNLUDUR. "
        "NE SORMAZ: tek tek çalışma kayıtları, operatör. 🔴 Bedeli bilinmeyen "
        "makine toplama 0 ile GİRMEZ; `total_cost` yalnız BİLİNENLERİN "
        "toplamıdır ve bunu not açıkça söyler."
    ),
    kapsam=ToolKapsami.MODUL_KAPISI,
    kume=ToolKumesi.SIRKET_GENELI,
    kapilar=frozenset({("equipment", AccessLevel.view)}),
    ucler=("/equipment/work-summary",),
    veri_modulleri=frozenset({"equipment"}),
    yol_parametreleri={},
    girdi=schemas.MakineDonemiGirdi,
    yanit_modeli=schemas.AiMakineCalismasi,
    calistir=ai2bd.makine_calisma,
)

MAKINE_YAKIT = ToolSpec(
    ad="makine_yakit",
    aciklama=(
        "NE ZAMAN: bir AYIN yakıt özeti — litre, tutar, norm sapması, anormal "
        "tüketim sayısı — sorulduğunda. `year` ve `month` ZORUNLUDUR. "
        "NE SORMAZ: tek tek yakıt fişleri. `lt_per_hour_avg` boşsa o dönemde "
        "ÇALIŞMA KAYDI yoktur; bu 'tüketim sıfır' DEMEK DEĞİLDİR."
    ),
    kapsam=ToolKapsami.MODUL_KAPISI,
    kume=ToolKumesi.SIRKET_GENELI,
    kapilar=frozenset({("equipment", AccessLevel.view)}),
    ucler=("/equipment/fuel-summary",),
    veri_modulleri=frozenset({"equipment"}),
    yol_parametreleri={},
    girdi=schemas.MakineDonemiGirdi,
    yanit_modeli=schemas.AiMakineYakiti,
    calistir=ai2bd.makine_yakit,
)

MAKINE_KIRA = ToolSpec(
    ad="makine_kira",
    aciklama=(
        "NE ZAMAN: kiralık makine hakediş/faturaları, tedarikçisi, dönemi ya da "
        "ödenecek tutarı sorulduğunda. NE SORMAZ: fatura SATIRLARI, ödeme "
        "emri, tedarikçinin vergi/iletişim bilgisi. `site_name` boş olan fatura "
        "'Tüm Projeler' faturasıdır ve tek bir şantiyeye ait değildir."
    ),
    kapsam=ToolKapsami.MODUL_KAPISI,
    kume=ToolKumesi.SIRKET_GENELI,
    kapilar=frozenset({("equipment", AccessLevel.view)}),
    ucler=("/equipment/rental-invoices",),
    #: `supplier_name` `procurement.Supplier`dan, `site_name` `sites`ten gelir.
    veri_modulleri=frozenset({"equipment", "procurement", "sites"}),
    yol_parametreleri={},
    girdi=schemas.BosGirdi,
    yanit_modeli=schemas.AiMakineKirasi,
    calistir=ai2bd.makine_kira,
)


#: 🔴 AI-2b + AI-2d'nin on altı aracı. Ayrı bir demet olarak durur ki
#: `READ_TOOLS`un hangi dilimden geldiği okunabilsin; `CATALOG` yine TEK
#: kaynaktır.
AI2BD_TOOLS: Final[tuple[ToolSpec, ...]] = (
    PROJE_DETAYI,
    SANTIYELERI_LISTELE,
    SANTIYE_DETAYI,
    IS_KALEMLERI,
    ARSA_PAYI,
    ISVEREN_HAKEDISLERI,
    TASERON_HAKEDISLERI,
    SOZLESMELER,
    TASERONLAR,
    PUANTAJ,
    GUNLUK_KAYIT,
    GUN_PLANI,
    MAKINE_LISTESI,
    MAKINE_CALISMA,
    MAKINE_YAKIT,
    MAKINE_KIRA,
)


#: 🔴 `READ_TOOLS` iki dilim demetinin BİRLEŞİMİDİR ve bu, bir bekçinin
#: dayanağıdır: parametrize testlerin bir kısmı `AI2BD_TOOLS` üzerinden koşar.
#: Bir araç `READ_TOOLS`a elle eklenip demete yazılmazsa o bekçiler onu
#: **sessizce atlar**; küme eşitliği `test_ai2bd_araclar.py::test_KAT_*`ta.
READ_TOOLS: Final[tuple[ToolSpec, ...]] = (*AI0B_TOOLS, *AI2BD_TOOLS)

#: AI-3'e kadar BOŞ. Dallanma ve testi ŞİMDİ yazılır (spec T5).
PROPOSE_TOOLS: Final[tuple[ToolSpec, ...]] = ()

CATALOG: Final[tuple[ToolSpec, ...]] = (*READ_TOOLS, *PROPOSE_TOOLS)

REGISTRY: Final[ToolRegistry] = ToolRegistry(READ_TOOLS, PROPOSE_TOOLS)
