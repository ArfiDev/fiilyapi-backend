"""OK-1C T4 — İKAME KAPISININ İKİNCİ DALININ KALINTISI: bir VARLIK KÂHİNİ.

Bu dosya bir ÖZELLİĞİ savunmaz; ÖLÇÜLMÜŞ bir kalıntıyı olduğu gibi çakar.
Davranışı değiştirmek yönetimin kararıdır — burası yalnız "bugün ne oluyor"
sorusunun tekrarlanabilir cevabıdır.

## Kalıntı nereden geliyor

`service.chain_step_substitutes_permission` İKİ dallıdır. İkincisi (T2'de
eklendi) şudur: **aktör ADAY İMZACI (en az bir onay rolü var) ve evrak HİÇ YOK
⇒ kapı açılır.** Gerekçesi bir sızıntıyı kapatmaktı: o dal olmasaydı GÖRÜNMEYEN
(kapsam dışı) bir kayıt 404, VAR OLMAYAN bir kimlik 403 verirdi ve elinde kimlik
olan bir aday imzacı kaydın VARLIĞINI öğrenirdi
(`test_ok1c_ikame.py::test_KAPSAMI_OLMAYAN_adim_sahibi_404_alir_...`).

Dal o sızıntıyı gerçekten kapattı — ama ayrımı YOK ETMEDİ, YERİNİ DEĞİŞTİRDİ.

## ÖLÇÜLEN ÜÇ HÂL (2026-08-22, taban `d5d0244`, gerçek HTTP istekleri)

Aktör: sistem rolü `hr_manager` — matriste `progress_payments = none` ve
`procurement = none`, yani ilgili modülde izni HİÇ YOKTUR.

| # | Hâl | Kod · `detail` |
|---|---|---|
| F1 | evrak HİÇ YOK (rastgele UUID) | **404** · `Hakediş bulunamadı` |
| F2 | (B3b: hâl YOK → F3) evrak VAR · kapsam DIŞI · sıradaki rol BENDE | 404 |
| F3 | evrak VAR · sıradaki adımın rolü BENDE DEĞİL | **403** · `Bu işlem için yetkiniz yok` |

F3 kapsam DIŞI ve kapsam İÇİ evrakta AYNIDIR (ikisi de 403) — kâhin kapsam
hakkında hiçbir şey söylemez, yalnız VARLIK hakkında konuşur.

## 🔴 CEVAP: F3, F1'DEN AYIRT EDİLEBİLİR — bu YENİ bir varlık kâhinidir

Bir ADAY İMZACI için:

* **403 ⇒ evrak KESİNLİKLE VARDIR.** (Var olmayan kimlik bu aktöre 403
  veremez: kapı ikinci dalla açılır ve uç 404'e düşer.)
* **404 ⇒ belirsiz** — ya evrak yoktur, ya vardır ama aktör onun sıradaki
  adımının rolünü taşır ve projeyi göremez.

OK-1C ÖNCESİ üç hâlin de cevabı 403'tü; yani kâhin YOKTU. Bugün vardır.

**Kâhin ZİNCİRSİZ evrakta da açıktır** (ölçüldü): zinciri hiç olmayan bir
hakediş de 403 verir, yani ailenin tablosundaki HERHANGİ bir kimlik
sınanabilir — yalnız zincire bağlı olanlar değil.

**`/reject` ucunda da AYNI kâhin vardır** (ölçüldü) — altı ucun hepsi AYNI
kapı nesnesini taşır. Dahası `/reject` GÖVDESİZ çağrıldığında ayrım 403 ↔ 422
olur: kapı FastAPI'nin gövde doğrulamasından ÖNCE koşar, yani var olmayan
kimlik 422'ye, var olan kimlik 403'e düşer. Kâhin gövdeyi hiç göndermeden de
işler.

## Neden bugün KABUL EDİLDİ (ve sınırı nedir)

* Kapı hiçbir YETKİ vermez. Açılması yalnızca isteğin uca ULAŞMASINI sağlar;
  uç kapsamı kendi sorar ve 404, otoriteyi `_assert_can_decide` kendi sorar ve
  403 verir. Ölçülen hiçbir hâlde veri dönmez — F2'nin gövdesi F1'inkiyle
  BİREBİR aynıdır (karşı taraf adı da tutar da sızmaz, o iddia kardeş dosyada).
* Kâhin **ADAY İMZACIYLA SINIRLIDIR**. Hiç onay rolü olmayan bir kullanıcı için
  üç hâl de 403'tür; ona kapalıdır ve bu dosyanın BEKÇİSİ tam olarak budur
  (`test_ADAY_IMZACI_OLMAYAN_kullanici_icin_kahin_KAPALIDIR`). Aday imzacı
  kümesi onay rolü atanmış kullanıcılardır — yani zaten onay kutusunun muhatabı
  olan, evrakların varlığını kutuda da görebilen dar bir kümedir.
* Sızan bilgi TEK BİTTİR: "bu UUID ailenin tablosunda var mı". Kimlikler
  tahmin edilemez (UUID4) — kâhin ancak elde HAZIR bir kimlik varken işe yarar.

🔴 Bekçilerin beklediği metinler ELLE YAZILMIŞTIR; koddan ithal edilmez.
"""

import uuid
from decimal import Decimal

from app.modules.approvals.models import ApprovalRole
from tests.modules.approvals.test_ok1c_ikame import _SATINALMA, _TASERON, _zincir

#: 🔴 ELLE YAZILDI (`core/permissions.py`ten ithal EDİLMEDİ).
_MODUL_KAPISI = "Bu işlem için yetkiniz yok"
#: 🔴 ELLE YAZILDI (`subcontractor_progress_payments/guards.py`ten ithal EDİLMEDİ).
_HAKEDIS_YOK = "Hakediş bulunamadı"
#: 🔴 ELLE YAZILDI (`procurement/guards.py`ten ithal EDİLMEDİ).
_TALEP_YOK = "Satın alma talebi bulunamadı"

_TASERON_YOL = "/subcontractor-progress-payments"
_SATINALMA_YOL = "/purchase-requests"

#: İlgili iki modülde de izni HİÇ OLMAYAN sistem rolü (matris: `progress_
#: payments = none`, `procurement = none`). "Seviyesi yetmiyor" değil, "izni
#: yok" hâli bilerek seçildi — kâhin en zayıf aktörde ölçülmelidir.
_IZINSIZ_ROL = "hr_manager"

#: `/reject` gövdesi ZORUNLUDUR (taşeron ailesi).
_GEREKCE = {"reason": "Metrajlar eksik, revize edin"}


# --------------------------------------------------------------------------- #
# F1 — evrak HİÇ YOK
# --------------------------------------------------------------------------- #


async def test_F1_ADAY_IMZACI_var_olmayan_evrakta_404_alir(
    client, aktor_fabrikasi, evrak_fabrikasi, giris
):
    """Kapının İKİNCİ dalı: aday imzacı + evrak yok ⇒ kapı açılır, uç 404 der.

    IZN-B3b: aday imzacı = bir projenin ekibinde adım rolü taşıyan kişi; bu yüzden önce bir
    proje (ve ekip satırı) vardır."""
    yaratan = await aktor_fabrikasi("kahin-f1-yaratan@ok1c.co")
    await evrak_fabrikasi(_TASERON, creator=yaratan)
    await aktor_fabrikasi(
        "kahin-f1@ok1c.co",
        role_key=_IZINSIZ_ROL,
        approval_roles=[ApprovalRole.site_chief],
        tum_projeler=True,
    )
    basliklar = await giris("kahin-f1@ok1c.co")

    yanit = await client.post(f"{_TASERON_YOL}/{uuid.uuid4()}/approve", headers=basliklar)

    assert yanit.status_code == 404, yanit.text
    assert yanit.json()["detail"] == _HAKEDIS_YOK


# --------------------------------------------------------------------------- #
# F2 — evrak VAR, kapsam DIŞI, aktör SIRADAKİ adımın rolünü TAŞIYOR
# --------------------------------------------------------------------------- #


async def test_F2_BASKA_PROJENIN_sahibi_F3_SINIFINA_duser_403_ve_GOVDE_SIZDIRMAZ(
    client, seeded_db, aktor_fabrikasi, evrak_fabrikasi, giris
):
    """IZN-B3b: eski F2 ("sıradaki adımın rolü bende ama proje kapsam dışı ⇒ 404") YOKTUR —
    adımın sahibi, tanım gereği belgenin projesinin ekibindedir. Başka projede şef olan
    aktör bu belgenin sahibi DEĞİLDİR ⇒ F3 sınıfı (403, modül kapısı), gövde sızdırmaz.
    """
    yaratan = await aktor_fabrikasi("kahin-f2-yaratan@ok1c.co")
    document_id, _proje = await evrak_fabrikasi(
        _TASERON, creator=yaratan, subcontractor_name="Gizli Taşeron A.Ş."
    )
    await _zincir(seeded_db, _TASERON, document_id, yaratan)
    _diger, baska_proje = await evrak_fabrikasi(_TASERON, creator=yaratan)
    await aktor_fabrikasi(
        "kahin-f2@ok1c.co",
        role_key=_IZINSIZ_ROL,
        approval_roles=[ApprovalRole.site_chief],
        projeler=[baska_proje],
    )
    basliklar = await giris("kahin-f2@ok1c.co")

    yanit = await client.post(f"{_TASERON_YOL}/{document_id}/approve", headers=basliklar)

    assert yanit.status_code == 403, yanit.text
    assert yanit.json()["detail"] == _MODUL_KAPISI
    assert "Gizli" not in yanit.text


# --------------------------------------------------------------------------- #
# F3 — evrak VAR, aktör SIRADAKİ adımın rolünü TAŞIMIYOR
# --------------------------------------------------------------------------- #


async def test_F3_SIRADAKI_ROLU_TASIMAYAN_aday_403_alir_KAPSAM_ICI_ve_DISI_AYNI(
    client, seeded_db, aktor_fabrikasi, evrak_fabrikasi, giris
):
    """Zincirin sıradaki adımı `site_chief`; aktörün onay rolü `accounting`.

    Kapı AÇILMAZ (birinci dal tutmaz, ikinci dal `document_exists` yüzünden
    tutmaz) ⇒ yakalanan modül kapısı 403'ü AYNEN fırlatılır.

    🔴 Kapsam İÇİ ve DIŞI aktör AYNI cevabı alır: kâhin kapsam hakkında hiçbir
    şey söylemez. Söyleseydi bu ikinci bir sızıntı olurdu.
    """
    yaratan = await aktor_fabrikasi("kahin-f3-yaratan@ok1c.co")
    await aktor_fabrikasi(
        "kahin-f3-dis@ok1c.co",
        role_key=_IZINSIZ_ROL,
        approval_roles=[ApprovalRole.accounting],
        tum_projeler=False,
    )
    await aktor_fabrikasi(
        "kahin-f3-ic@ok1c.co",
        role_key=_IZINSIZ_ROL,
        approval_roles=[ApprovalRole.accounting],
        tum_projeler=True,
    )
    disaridan = await giris("kahin-f3-dis@ok1c.co")
    iceriden = await giris("kahin-f3-ic@ok1c.co")
    document_id, _proje = await evrak_fabrikasi(_TASERON, creator=yaratan)
    await _zincir(seeded_db, _TASERON, document_id, yaratan)

    kapsam_disi = await client.post(f"{_TASERON_YOL}/{document_id}/approve", headers=disaridan)
    kapsam_ici = await client.post(f"{_TASERON_YOL}/{document_id}/approve", headers=iceriden)

    assert kapsam_disi.status_code == 403, kapsam_disi.text
    assert kapsam_disi.json()["detail"] == _MODUL_KAPISI
    assert kapsam_ici.status_code == 403, kapsam_ici.text
    assert kapsam_ici.json()["detail"] == _MODUL_KAPISI


# --------------------------------------------------------------------------- #
# 🔴 KÂHİNİN KENDİSİ — F3, F1'den AYIRT EDİLEBİLİR
# --------------------------------------------------------------------------- #


async def test_KAHIN_ACIK_aday_imzaci_icin_403_VARLIK_KANITIDIR(
    client, seeded_db, aktor_fabrikasi, evrak_fabrikasi, giris
):
    """🔴 BU DOSYANIN KALBİ. Aynı aktör, aynı oturum, tek fark evrağın VARLIĞI.

    Aktör aday imzacıdır ama evrağın sıradaki adımının rolünü TAŞIMAZ:
      * VAR OLAN kimlik  → 403
      * VAR OLMAYAN kimlik → 404

    OK-1C öncesi ikisi de 403'tü. Bugün 403, aday imzacı için bir VARLIK
    KANITIDIR. Bu davranış OLDUĞU GİBİ çakılıyor; değiştirmek yönetimin
    kararıdır.
    """
    yaratan = await aktor_fabrikasi("kahin-ana-yaratan@ok1c.co")
    await aktor_fabrikasi(
        "kahin-ana@ok1c.co",
        role_key=_IZINSIZ_ROL,
        approval_roles=[ApprovalRole.accounting],
        tum_projeler=True,
    )
    basliklar = await giris("kahin-ana@ok1c.co")
    document_id, _proje = await evrak_fabrikasi(_TASERON, creator=yaratan)
    await _zincir(seeded_db, _TASERON, document_id, yaratan)

    var_olan = await client.post(f"{_TASERON_YOL}/{document_id}/approve", headers=basliklar)
    olmayan = await client.post(f"{_TASERON_YOL}/{uuid.uuid4()}/approve", headers=basliklar)

    assert var_olan.status_code == 403, var_olan.text
    assert var_olan.json()["detail"] == _MODUL_KAPISI
    assert olmayan.status_code == 404, olmayan.text
    assert olmayan.json()["detail"] == _HAKEDIS_YOK
    assert var_olan.status_code != olmayan.status_code, (
        "kâhin KAPANMIŞ — bu bir düzeltmedir, dosyanın docstring'i ve OK-1C "
        "devir notu birlikte güncellenmelidir"
    )


async def test_KAHIN_ZINCIRSIZ_evrakta_da_ACIKTIR(client, aktor_fabrikasi, evrak_fabrikasi, giris):
    """Sınanabilen küme "zinciri olan evraklar" DEĞİL, ailenin TÜM tablosudur.

    Zinciri hiç olmayan bir hakediş de 403 verir (kapı `document_exists`
    yüzünden kapanır), var olmayan kimlik 404 verir.
    """
    yaratan = await aktor_fabrikasi("kahin-zsiz-yaratan@ok1c.co")
    await aktor_fabrikasi(
        "kahin-zsiz@ok1c.co",
        role_key=_IZINSIZ_ROL,
        approval_roles=[ApprovalRole.site_chief],
        tum_projeler=True,
    )
    basliklar = await giris("kahin-zsiz@ok1c.co")
    zincirsiz_id, _proje = await evrak_fabrikasi(_TASERON, creator=yaratan)

    var_olan = await client.post(f"{_TASERON_YOL}/{zincirsiz_id}/approve", headers=basliklar)
    olmayan = await client.post(f"{_TASERON_YOL}/{uuid.uuid4()}/approve", headers=basliklar)

    assert var_olan.status_code == 403, var_olan.text
    assert var_olan.json()["detail"] == _MODUL_KAPISI
    assert olmayan.status_code == 404, olmayan.text
    assert olmayan.json()["detail"] == _HAKEDIS_YOK


async def test_KAHIN_SATINALMA_ailesinde_de_ACIKTIR_404_METNI_AILEYE_OZGUDUR(
    client, seeded_db, aktor_fabrikasi, evrak_fabrikasi, giris
):
    """Aynı ayrım satınalmada da vardır; 404 metni ailenin KENDİ metnidir."""
    yaratan = await aktor_fabrikasi("kahin-sat-yaratan@ok1c.co")
    await aktor_fabrikasi(
        "kahin-sat@ok1c.co",
        role_key=_IZINSIZ_ROL,
        approval_roles=[ApprovalRole.accounting],
        tum_projeler=True,
    )
    basliklar = await giris("kahin-sat@ok1c.co")
    document_id, _proje = await evrak_fabrikasi(
        _SATINALMA, creator=yaratan, quantity=Decimal("10"), unit_price=Decimal("1000.00")
    )
    await _zincir(seeded_db, _SATINALMA, document_id, yaratan, Decimal("10000.00"))

    var_olan = await client.post(f"{_SATINALMA_YOL}/{document_id}/approve", headers=basliklar)
    olmayan = await client.post(f"{_SATINALMA_YOL}/{uuid.uuid4()}/approve", headers=basliklar)

    assert var_olan.status_code == 403, var_olan.text
    assert var_olan.json()["detail"] == _MODUL_KAPISI
    assert olmayan.status_code == 404, olmayan.text
    assert olmayan.json()["detail"] == _TALEP_YOK


# --------------------------------------------------------------------------- #
# 🔴 BEKÇİ — kâhin ADAY İMZACI OLMAYANA KAPALIDIR
# --------------------------------------------------------------------------- #


async def test_ADAY_IMZACI_OLMAYAN_kullanici_icin_kahin_KAPALIDIR(
    client, seeded_db, aktor_fabrikasi, evrak_fabrikasi, giris
):
    """🔴 KALINTININ SINIRI BUDUR ve bir BEKÇİDİR.

    Hiç onay rolü olmayan bir kullanıcı için kapı HİÇBİR dalla açılmaz: var
    olan evrak da var olmayan kimlik de AYNI 403'ü verir. İkinci dal
    "aday imzacı" koşulu olmadan yazılsaydı bu test kırmızıya döner ve bugün
    HİÇ OLMAYAN bir varlık keşif yüzeyi BÜTÜN kullanıcılara açılırdı.
    """
    yaratan = await aktor_fabrikasi("kahin-rolsuz-yaratan@ok1c.co")
    await aktor_fabrikasi("kahin-rolsuz@ok1c.co", role_key=_IZINSIZ_ROL, tum_projeler=True)
    basliklar = await giris("kahin-rolsuz@ok1c.co")
    document_id, _proje = await evrak_fabrikasi(_TASERON, creator=yaratan)
    await _zincir(seeded_db, _TASERON, document_id, yaratan)

    var_olan = await client.post(f"{_TASERON_YOL}/{document_id}/approve", headers=basliklar)
    olmayan = await client.post(f"{_TASERON_YOL}/{uuid.uuid4()}/approve", headers=basliklar)

    assert var_olan.status_code == 403, var_olan.text
    assert olmayan.status_code == 403, olmayan.text
    assert var_olan.json() == olmayan.json() == {"detail": _MODUL_KAPISI}


# --------------------------------------------------------------------------- #
# Aynı kâhin `/reject` ucunda da açıktır (altı uç AYNI kapı nesnesini taşır)
# --------------------------------------------------------------------------- #


async def test_KAHIN_REJECT_ucunda_da_ACIKTIR(
    client, seeded_db, aktor_fabrikasi, evrak_fabrikasi, giris
):
    """`/approve` ile `/reject` AYNI `_CHAIN_APPROVE` nesnesini taşır.

    Ayrım bu yüzden ikisinde de aynıdır; test iddiayı kod okumaya değil ÖLÇÜME
    dayandırır (ikisi ayrışırsa burası kırmızı olur).
    """
    yaratan = await aktor_fabrikasi("kahin-ret-yaratan@ok1c.co")
    await aktor_fabrikasi(
        "kahin-ret@ok1c.co",
        role_key=_IZINSIZ_ROL,
        approval_roles=[ApprovalRole.accounting],
        tum_projeler=True,
    )
    basliklar = await giris("kahin-ret@ok1c.co")
    document_id, _proje = await evrak_fabrikasi(_TASERON, creator=yaratan)
    await _zincir(seeded_db, _TASERON, document_id, yaratan)

    var_olan = await client.post(
        f"{_TASERON_YOL}/{document_id}/reject", json=_GEREKCE, headers=basliklar
    )
    olmayan = await client.post(
        f"{_TASERON_YOL}/{uuid.uuid4()}/reject", json=_GEREKCE, headers=basliklar
    )

    assert var_olan.status_code == 403, var_olan.text
    assert var_olan.json()["detail"] == _MODUL_KAPISI
    assert olmayan.status_code == 404, olmayan.text
    assert olmayan.json()["detail"] == _HAKEDIS_YOK


async def test_KAHIN_GOVDESIZ_REJECT_ile_de_isler_403_ve_422_AYRISIR(
    client, seeded_db, aktor_fabrikasi, evrak_fabrikasi, giris
):
    """🔴 Kapı, FastAPI'nin GÖVDE DOĞRULAMASINDAN ÖNCE koşar.

    Zorunlu gövde hiç gönderilmediğinde:
      * evrak VAR  → kapı kapanır, 403 (gövdeye hiç bakılmaz);
      * evrak YOK  → kapı açılır, istek gövde doğrulamasına ulaşır, 422.

    Yani kâhin, geçerli bir gerekçe metni bile yazmadan çalıştırılabilir.
    Aday imzacı OLMAYAN için ise iki hâl de 403'tür (kapı hiç açılmaz).
    """
    yaratan = await aktor_fabrikasi("kahin-govdesiz-yaratan@ok1c.co")
    await aktor_fabrikasi(
        "kahin-govdesiz@ok1c.co",
        role_key=_IZINSIZ_ROL,
        approval_roles=[ApprovalRole.accounting],
        tum_projeler=True,
    )
    await aktor_fabrikasi("kahin-govdesiz-rolsuz@ok1c.co", role_key=_IZINSIZ_ROL, tum_projeler=True)
    aday = await giris("kahin-govdesiz@ok1c.co")
    rolsuz = await giris("kahin-govdesiz-rolsuz@ok1c.co")
    document_id, _proje = await evrak_fabrikasi(_TASERON, creator=yaratan)
    await _zincir(seeded_db, _TASERON, document_id, yaratan)
    olmayan_id = uuid.uuid4()

    var_olan = await client.post(f"{_TASERON_YOL}/{document_id}/reject", headers=aday)
    olmayan = await client.post(f"{_TASERON_YOL}/{olmayan_id}/reject", headers=aday)

    assert var_olan.status_code == 403, var_olan.text
    assert var_olan.json()["detail"] == _MODUL_KAPISI
    assert olmayan.status_code == 422, olmayan.text

    # Aday imzacı OLMAYAN için ayrım YOKTUR.
    r_var = await client.post(f"{_TASERON_YOL}/{document_id}/reject", headers=rolsuz)
    r_yok = await client.post(f"{_TASERON_YOL}/{olmayan_id}/reject", headers=rolsuz)
    assert r_var.status_code == 403, r_var.text
    assert r_yok.status_code == 403, r_yok.text
    assert r_var.json() == r_yok.json() == {"detail": _MODUL_KAPISI}


# --------------------------------------------------------------------------- #
# Kapının FAIL-CLOSED dalı — bozuk kimlik
# --------------------------------------------------------------------------- #


async def test_BOZUK_KIMLIK_aday_imzaciya_da_403_verir_422_DEGIL(client, aktor_fabrikasi, giris):
    """`gate._zincir_adimi_ikame_ediyor` UUID'ye çevrilemeyen kimlikte `False` döner.

    Kapı FastAPI'nin 422'sinden ÖNCE koşar; bu yüzden cevap 403'tür ve bugünkü
    davranış bozuk kimlikte de birebir korunur.
    """
    await aktor_fabrikasi(
        "kahin-bozuk@ok1c.co",
        role_key=_IZINSIZ_ROL,
        approval_roles=[ApprovalRole.site_chief],
        tum_projeler=True,
    )
    basliklar = await giris("kahin-bozuk@ok1c.co")

    yanit = await client.post(f"{_TASERON_YOL}/bozuk-kimlik/approve", headers=basliklar)

    assert yanit.status_code == 403, yanit.text
    assert yanit.json()["detail"] == _MODUL_KAPISI
