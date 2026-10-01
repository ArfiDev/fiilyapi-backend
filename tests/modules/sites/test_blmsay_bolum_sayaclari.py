"""BLM-SAY — bölüm listesi sayaçlarının GERÇEĞE bağlanması.

Kullanıcı canlıda bölüm kartındaki dört alanın da boş olduğunu bildirdi.
Ölçüm üç ayrı kusur buldu ve bu dosya üçünü de çakar:

1. `budget_amount` / `planned_worker_count` **kayıtlıydı ama LİSTE ucu o
   alanları hiç döndürmüyordu** (BLF-B1: `budget_amount` yanıttan KALKTI —
   bedel yalnız türev `budget`; bu dosya eski kolonu DOLU kurar ve yanıta
   SIZMADIĞINI çakar) — ekran mecburen `budget` yer tutucusunu
   ("—") basıyor, planlanan işçiyi bilmediği için `0` yazıyordu.
2. `boq_item_count` hâlâ `pending_module:"boq"` yer tutucusuydu; oysa
   `boq_item_section_allocations` doldu ve sayı ARTIK ÖLÇÜLEBİLİR.
3. `budget` (BOQ türevi) aynı tahsislerden türetilebilir hâle geldi.

🔴 SAHTE-YEŞİL UYARISI: bir sayacın DOĞRU olduğunu "testler geçti" söylemez.
Bu yüzden her iddia **gerçek veriyle dolu bir kurulumdan** okunur ve kurulum
tahsisi OLAN ve OLMAYAN bölümü BİRLİKTE kurar (K-MKD3: "satır yok" ≠ "değer 0").

🔴 Zarflar GERÇEK okuma uçlarından okunur (`test_pyt2_yer_tutucu_denetimi.py`
deseni) — dönüştürücüler elle çağrılmaz: uydurma bir ORM nesnesi üzerinde
ölçülen zarf, ucun onu basmayı unutmasını göremezdi.
"""

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from decimal import Decimal

from sqlalchemy import UniqueConstraint, event

from app.core.discipline_scope import UNRESTRICTED
from app.modules.boq.models import BoqItemSectionAllocation
from app.modules.sites import service
from app.modules.sites.models import Section, Site
from app.modules.users.models import UserProjectAccess
from tests.conftest import test_engine
from tests.modules._boq import _group, _item

#: Canlıda ölçülen değerler (yönetim ölçümü, 2026-08-26): bu iki alan KAYITLI
#: ama liste ucunda YOKTU. Bekçi tam o iki sayıyı taşır.
_BEDEL = Decimal("4982030.00")
_PLANLANAN_ISCI = 12


async def _kurulum(session, user_factory, project_factory, kod: str, email: str):
    """Proje + şantiye + İKİ bölüm + BOQ pozları + tahsisler + aktör.

    * `dolu` bölüm: ÜÇ ayrı poza tahsisi var, `budget_amount`/
      `planned_worker_count` girilmiş. Miktarlar KASITLI olarak kuruş altına
      düşen çarpımlar üretir: kalem başına yuvarlama ile TOPLAMI bir kez
      yuvarlama ARASINDAKİ farkı (K3) görünür kılar.
    * `bos` bölüm: HİÇ tahsisi yok — "satır yok" hâlinin bekçisi.
    """
    project = await project_factory(kod)
    site = Site(project_id=project.id, code=f"{kod}-A", name="A-Blok")
    session.add(site)
    await session.flush()

    dolu = Section(
        site_id=site.id,
        name="Kat 6-10 Kaba İnşaat",
        sort_order=0,
        budget_amount=_BEDEL,
        planned_worker_count=_PLANLANAN_ISCI,
    )
    bos = Section(site_id=site.id, name="Çatı", sort_order=1)
    session.add_all([dolu, bos])
    await session.flush()

    group = await _group(session, site)
    # 400.000 × 280.00 = 112.000,00 (tam)
    poz1 = await _item(session, site, group, code="01.001")
    # 3.333 × 1500.50 = 5001,1665 → kuruşa yuvarlanır: 5001,17
    poz2 = await _item(
        session,
        site,
        group,
        code="01.002",
        quantity=Decimal("10.000"),
        unit_price=Decimal("1500.50"),
    )
    # 1.111 × 1500.50 = 1667,0555 → kuruşa yuvarlanır: 1667,06
    poz3 = await _item(
        session,
        site,
        group,
        code="01.003",
        quantity=Decimal("10.000"),
        unit_price=Decimal("1500.50"),
    )
    session.add_all(
        [
            BoqItemSectionAllocation(
                boq_item_id=poz1.id, section_id=dolu.id, quantity=Decimal("400.000")
            ),
            BoqItemSectionAllocation(
                boq_item_id=poz2.id, section_id=dolu.id, quantity=Decimal("3.333")
            ),
            BoqItemSectionAllocation(
                boq_item_id=poz3.id, section_id=dolu.id, quantity=Decimal("1.111")
            ),
        ]
    )
    await session.flush()

    user = await user_factory(email=email, password="parola1234", role_key="patron")
    session.add(UserProjectAccess(user_id=user.id, project_id=None, all_projects=True))
    await session.flush()
    return project, site, user, dolu, bos


#: `dolu` bölümün beklenen BOQ türevi bedeli — SATIR BAŞINA yuvarlanıp toplanır
#: (`boq.schemas.quantize_money`, TEK kopya):
#:   112.000,00 + 5.001,17 + 1.667,06
_TUREV_BEDEL = Decimal("118668.23")
#: 🔴 AYNI verinin ÖBÜR formülü: önce toplanıp SONRA bir kez yuvarlanmış hâli
#: (SQL'de `SUM(quantity * unit_price)` yazmanın sonucu) — 118.668,222 → ,22.
#: BİR KURUŞ farklıdır ve bekçi ikisini AYIRT EDER; ayırt etmeseydi K3
#: mutasyonu (formülü değiştirmek) sağ kalırdı.
_TEK_SEFER_YUVARLANMIS = Decimal("118668.22")


def _satirlar(liste) -> dict[uuid.UUID, object]:
    return {satir.id: satir for satir in liste}


# --------------------------------------------------------------------------- #
# İŞ 1 — kayıtlı kolonlar LİSTE yanıtında
# --------------------------------------------------------------------------- #


async def test_LISTE_ucu_planned_worker_count_BASAR_budget_amount_KALKTI(
    seeded_db, user_factory, project_factory
):
    """Kullanıcının bildirdiği kusurun ta kendisi: kayıtlı ama dönmeyen alan."""
    _p, site, user, dolu, bos = await _kurulum(
        seeded_db, user_factory, project_factory, "BS-1", "bs1@t.co"
    )

    satirlar = _satirlar(
        (await service.list_sections_for_site(seeded_db, user, site.id, scope=UNRESTRICTED)).items
    )

    # BLF-B1: eski kolon DOLU olsa da yanıtta alan YOKTUR.
    assert not hasattr(satirlar[dolu.id], "budget_amount")
    assert satirlar[dolu.id].planned_worker_count == _PLANLANAN_ISCI
    # Girilmemiş bölümde `None` KALIR — uydurma sıfır yasağı (K-MKD3).
    assert satirlar[bos.id].planned_worker_count is None


async def test_LISTE_ve_DETAY_ayni_TIPI_dondurur(seeded_db, user_factory, project_factory):
    """🔴 İki uç arasında ayrışan bir tip, aynı alanı bir yerde `Decimal` bir
    yerde `str` yapan sessiz bir tutarsızlıktır (`SectionCreate`/`SectionUpdate`
    için zaten uygulanan kural)."""
    _p, site, user, dolu, _bos = await _kurulum(
        seeded_db, user_factory, project_factory, "BS-2", "bs2@t.co"
    )

    satir = _satirlar(
        (await service.list_sections_for_site(seeded_db, user, site.id, scope=UNRESTRICTED)).items
    )[dolu.id]
    detay = await service.get_section_detail(seeded_db, user, dolu.id, scope=UNRESTRICTED)

    assert type(satir.planned_worker_count) is type(detay.planned_worker_count)
    assert satir.planned_worker_count == detay.planned_worker_count
    # Şema düzeyinde de TEK tanım: detay sınıfı alanı YENİDEN tanımlamaz.
    from app.modules.sites.schemas import SectionDetailResponse, SectionResponse

    for alan in ("planned_worker_count",):
        assert SectionResponse.model_fields[alan].annotation == (
            SectionDetailResponse.model_fields[alan].annotation
        ), alan
    assert "budget_amount" not in SectionResponse.model_fields


# --------------------------------------------------------------------------- #
# İŞ 2 — `boq_item_count` BAĞLANDI
# --------------------------------------------------------------------------- #


async def test_boq_item_count_BAGLI__tahsisi_olan_bolum(seeded_db, user_factory, project_factory):
    """Sayaç NEYİN kümesidir: **bölüme EN AZ BİR tahsis satırı bulunan FARKLI
    poz** sayısı. Şantiyenin tüm pozları DEĞİL, "tamamlanan" poz DEĞİL."""
    _p, site, user, dolu, _bos = await _kurulum(
        seeded_db, user_factory, project_factory, "BS-3", "bs3@t.co"
    )

    satir = _satirlar(
        (await service.list_sections_for_site(seeded_db, user, site.id, scope=UNRESTRICTED)).items
    )[dolu.id]

    assert satir.boq_item_count.available is True
    assert satir.boq_item_count.count == 3
    # Zarf KORUNDU: dolu `CountPlaceholder` da `pending_module` taşır
    # (`_worker_count` emsali) — şerit kaynağını oradan okur.
    assert satir.boq_item_count.pending_module == "boq"


async def test_boq_item_count__TAHSISI_OLMAYAN_bolum_SIFIR_dondurur(
    seeded_db, user_factory, project_factory
):
    """🔴 K-MKD3: "satır yok" ≠ "değer 0" ≠ "henüz bilinmiyor".

    Burada `0` BİLİNÇLİ bir ölçümdür, uydurma sıfır DEĞİL: tahsis satırının
    İKİ birleşim anahtarı da (`boq_item_id`, `section_id`) NOT NULL'dur, yani
    boş küme "bağlanmamış kayıt" ile karışamaz — `subcontractor_count`u yer
    tutucu bırakan K2 tuzağı burada YAPISAL OLARAK yoktur.
    """
    _p, site, user, _dolu, bos = await _kurulum(
        seeded_db, user_factory, project_factory, "BS-4", "bs4@t.co"
    )

    satir = _satirlar(
        (await service.list_sections_for_site(seeded_db, user, site.id, scope=UNRESTRICTED)).items
    )[bos.id]

    assert satir.boq_item_count.available is True, "ölçülebilen sayaç yer tutucuya DÜŞMEZ"
    assert satir.boq_item_count.count == 0


# --------------------------------------------------------------------------- #
# İŞ 3 — `budget` (BOQ türevi) BAĞLANDI
# --------------------------------------------------------------------------- #


async def test_budget_TAHSIS_EDILEN_miktarlardan_turer(seeded_db, user_factory, project_factory):
    """`budget` = Σ (bölüme tahsis edilen miktar × pozun birim fiyatı).

    Çarpım kalem başına `boq.schemas.quantize_money` ile yuvarlanır — o para
    formülünün TEK kopyasıdır (K3), ikinci bir kopya kuruş farklı bir "Bölüm
    Bedeli" üretirdi.
    """
    _p, site, user, dolu, _bos = await _kurulum(
        seeded_db, user_factory, project_factory, "BS-5", "bs5@t.co"
    )

    satir = _satirlar(
        (await service.list_sections_for_site(seeded_db, user, site.id, scope=UNRESTRICTED)).items
    )[dolu.id]

    assert satir.budget.available is True
    assert satir.budget.value == _TUREV_BEDEL
    # 🔴 K3 MUTASYON BEKÇİSİ: aynı veri SQL'de tek seferde yuvarlansaydı bir
    # kuruş eksik çıkardı. Bu satır olmadan "para formülünü değiştirmek"
    # mutasyonu SAĞ KALIRDI.
    assert satir.budget.value != _TEK_SEFER_YUVARLANMIS
    # Dolu `MetricPlaceholder` `pending_module` TAŞIMAZ (P10 T3 zarf kuralı).
    assert satir.budget.pending_module is None


async def test_budget__TUREVDIR_eski_budget_amount_kolonu_OKUNMAZ(
    seeded_db, user_factory, project_factory
):
    """🔴 BLF-B1: bedel TEK kaynaktır — BOQ türevi. Satırda eski elle `budget_amount`
    kolonu DOLU durur (`_BEDEL`) ama yanıt onu okumaz: `budget` türevi basar."""
    _p, site, user, dolu, bos = await _kurulum(
        seeded_db, user_factory, project_factory, "BS-6", "bs6@t.co"
    )

    satirlar = _satirlar(
        (await service.list_sections_for_site(seeded_db, user, site.id, scope=UNRESTRICTED)).items
    )

    assert satirlar[dolu.id].budget.value == _TUREV_BEDEL
    assert satirlar[dolu.id].budget.value != _BEDEL
    # Tahsisi olmayan bölüm: türev `0`.
    assert satirlar[bos.id].budget.available is True
    assert satirlar[bos.id].budget.value == Decimal("0.00")


async def test_progress_pct_ILR1DE_BAGLANDI__gunluk_YOKKEN_SIFIR(
    seeded_db, user_factory, project_factory
):
    """⚠️ **ILR-1'DE DEGISTI.** Eski adi `..._BU_DILIMDE_YER_TUTUCU_KALIR`di ve
    alanin BAGLANMAMIS kalmasini cakiyordu; kullanici karari (2026-08-27) o
    beklentiyi gecersiz kildi.

    Yeni iddia: TAHSISI OLAN bolumde payda VARDIR, dolayisiyla yuzde de vardir.
    Hic gonderilmis gunluk olmadigi icin deger GERCEKTEN `0.00`'dir —
    "bilinmiyor" degil. Zarf DOLUDUR, yani `pending_module` TASIMAZ.
    """
    _p, site, user, dolu, _bos = await _kurulum(
        seeded_db, user_factory, project_factory, "BS-7", "bs7@t.co"
    )

    satir = _satirlar(
        (await service.list_sections_for_site(seeded_db, user, site.id, scope=UNRESTRICTED)).items
    )[dolu.id]

    assert (satir.progress_pct.available, satir.progress_pct.pending_module) == (True, None)
    assert satir.progress_pct.value == Decimal("0.00"), (
        "tahsisi olan ama gunlugu olmayan bolum: OLCULMUS sifir (yer tutucu DEGIL)"
    )


# --------------------------------------------------------------------------- #
# ÜÇ YÜZEY — liste · şantiye detayı · bölüm detayı AYNI sayıyı basar
# --------------------------------------------------------------------------- #


async def test_UC_YUZEY_de_ayni_sayaci_basar(seeded_db, user_factory, project_factory):
    """Bölüm basan üç yüzey tek dönüştürücüden geçer; biri sayacı kaybederse
    ekranlar aynı bölüm için farklı sayı gösterirdi."""
    _p, site, user, dolu, _bos = await _kurulum(
        seeded_db, user_factory, project_factory, "BS-8", "bs8@t.co"
    )

    liste = _satirlar(
        (await service.list_sections_for_site(seeded_db, user, site.id, scope=UNRESTRICTED)).items
    )[dolu.id]
    santiye = _satirlar(
        (await service.get_site_detail(seeded_db, user, site.id, scope=UNRESTRICTED)).sections
    )[dolu.id]
    detay = await service.get_section_detail(seeded_db, user, dolu.id, scope=UNRESTRICTED)

    for yuzey in (liste, santiye, detay):
        assert yuzey.boq_item_count.count == 3
        assert yuzey.budget.value == _TUREV_BEDEL
        assert not hasattr(yuzey, "budget_amount")
        assert yuzey.planned_worker_count == _PLANLANAN_ISCI


# --------------------------------------------------------------------------- #
# N+1 — sorgu sayısı BÖLÜM SAYISINDAN bağımsızdır
# --------------------------------------------------------------------------- #


@contextmanager
def _sayac() -> Iterator[list[str]]:
    ifadeler: list[str] = []

    def kaydet(conn, cursor, statement, parameters, context, executemany) -> None:  # noqa: ANN001
        ifadeler.append(" ".join(statement.split()))

    event.listen(test_engine.sync_engine, "before_cursor_execute", kaydet)
    try:
        yield ifadeler
    finally:
        event.remove(test_engine.sync_engine, "before_cursor_execute", kaydet)


async def test_sorgu_sayisi_BOLUM_SAYISINDAN_bagimsizdir(seeded_db, user_factory, project_factory):
    """🔴 Bölüm başına sayım sorgusu AÇILMAZ (`timesheet/counts.py` deseni).

    İddia İKİ katlıdır (`tests/sales/test_sales_cost_binding.py` dersi):
    eşitlik N+1'i görür ama SABİT bir ek sorguyu göremez — mutlak tavan görür.
    """
    _p, site, user, _dolu, _bos = await _kurulum(
        seeded_db, user_factory, project_factory, "BS-9", "bs9@t.co"
    )

    with _sayac() as ifadeler:
        await service.list_sections_for_site(seeded_db, user, site.id, scope=UNRESTRICTED)
        iki_bolum = len(ifadeler)

    for sira in range(2, 8):
        seeded_db.add(Section(site_id=site.id, name=f"Ek {sira}", sort_order=sira))
    await seeded_db.flush()

    with _sayac() as ifadeler:
        liste = await service.list_sections_for_site(seeded_db, user, site.id, scope=UNRESTRICTED)
        sekiz_bolum = len(ifadeler)

    assert len(liste.items) == 8
    assert iki_bolum == sekiz_bolum, (iki_bolum, sekiz_bolum)
    # 🔴 MUTLAK TAVAN. Ölçüm: BLM-SAY ÖNCESİ 13 ifade, SONRASI 14 — yani bu
    # dilim sıcak yola TAM BİR sorgu ekledi (`boq/counts.py::by_section`) ve
    # bölüm sayısından bağımsız. Tavan 16'dır: sessiz bir kayma yakalansın diye
    # dar, kimlik/yetki katmanının bir ifade eklemesi türü boşuna kırmasın diye
    # de bir parmak paylı. Sayıyı DÜŞÜRMEK serbesttir; YÜKSELTMEK gerekçelidir.
    assert sekiz_bolum <= 16, (
        f"bölüm listesi sıcak yolu {sekiz_bolum} sorguya çıktı (tavan 16) — "
        "eşitlik iddiası SABİT bir artışı GÖREMEZ, tavan görür"
    )


# --------------------------------------------------------------------------- #
# 🔴 EŞDEĞER MUTANTIN AÇIKÇA YAZILMASI (dürüst rapor)
# --------------------------------------------------------------------------- #


def test_DISTINCT_sayim_bugun_ESDEGERDIR__cunku_UQ_var():
    """`by_section` pozları `set` ile sayar; SATIR sayması BUGÜN aynı sonucu verir.

    🔴 Mutasyon koşuldu (`set` → `list`) ve **SAĞ KALDI**. Bu bir bekçi eksiği
    DEĞİL, yapısal bir eşdeğerliktir: `boq_item_section_allocations` üzerinde
    `(boq_item_id, section_id)` UNIQUE'tir, yani bir bölüme aynı poz İKİNCİ KEZ
    tahsis edilemez — `COUNT(*)` ile `COUNT(DISTINCT boq_item_id)` ayrışamaz.

    O hâlde `set` neden duruyor? Çünkü eşdeğerlik **kısıta bağlıdır, koda
    değil**: UQ düşerse (ya da DEFERRABLE penceresinde aynı işlem içinde geçici
    olarak iki satır görünürse) satır sayımı sessizce şişerdi. Bu test o bağı
    ÇAKAR — kısıt kaybolursa sayacın gerekçesi de bayatlar ve haber verir.
    """
    kisitlar = {
        kisit.name: tuple(kolon.name for kolon in kisit.columns)
        for kisit in BoqItemSectionAllocation.__table__.constraints
        if isinstance(kisit, UniqueConstraint)
    }

    assert kisitlar.get("uq_boq_item_section_allocations_item_section") == (
        "boq_item_id",
        "section_id",
    ), f"tahsis tekilliği kısıtı değişti: {kisitlar} — DISTINCT sayımın eşdeğerliği ARTIK GEÇERSİZ"
