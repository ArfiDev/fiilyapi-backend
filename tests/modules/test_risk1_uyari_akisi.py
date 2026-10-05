"""RISK-1 — "Risk & Uyarilar" kartinin bekcileri.

NE CAKILIR
----------
Kart yer tutucu DEGIL: **siddet etiketli bir uyari akisi**. Uc AYRI kaynak, uc
AYRI izin kapisi, ve mockup'in ucuncu satiri bir risk degil IYI HABER.

🔴 "Bir satir cikti" iddiasi TEK BASINA hicbir seyi kanitlamaz (sahte-yesilin
8. hâli: agrega servisin arkasindaki KUME bekcisizdir). Bu yuzden her olumlu
kontrolun yaninda KARSIT KANIT durur ve `test_uyari_KUMESI_...` kumenin
TAMAMINI bagimsiz olarak yazilmis bir beklentiyle karsilastirir — sahte bir uye
eklemek "kac satir uretildi" testinden kacar, kume karsilastirmasindan KACAMAZ.

🔴 IZIN BEKCILERI CIFT YONLUDUR (K-IKIZ1): izni OLMAYAN gormemeli ama izni OLAN
GORMELI. Yalnizca olumsuz taraf cakilsaydi, her seyi gizleyen bozuk bir kart da
yesil gecerdi.
"""

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import event

from app.core.access import AccessLevel
from app.core.timezone import today
from app.modules.contracts.models import SubcontractorContract
from app.modules.dashboard.risks import (
    PROGRESS_PAYMENT_MODULE,
    SCHEDULE_MODULE,
    STOCK_MODULE,
)
from app.modules.dashboard.service import build_summary
from app.modules.inventory.models import (
    StockCategory,
    StockEntry,
    StockEntryLine,
    StockEntryType,
    StockItem,
    Warehouse,
)
from app.modules.invoicing.models import (
    Invoice,
    InvoiceDirection,
    InvoiceDocumentType,
    InvoiceStatus,
)
from app.modules.sites.models import Section, SectionStatus, Site
from app.modules.subcontractor_progress_payments.models import (
    SubcontractorPaymentStatus,
    SubcontractorProgressPayment,
)
from app.modules.users.models import ProjectMember
from tests.conftest import test_engine

from ._boq import _set_permission


@contextmanager
def _sorgu_sayaci() -> Iterator[list[str]]:
    """Surucuye giden HER ifadeyi sayar (`test_dashboard_pyt2_onay_sayaci.py` deseni)."""
    ifadeler: list[str] = []

    def kaydet(conn, cursor, statement, parameters, context, executemany) -> None:  # noqa: ANN001
        ifadeler.append(" ".join(statement.split()))

    event.listen(test_engine.sync_engine, "before_cursor_execute", kaydet)
    try:
        yield ifadeler
    finally:
        event.remove(test_engine.sync_engine, "before_cursor_execute", kaydet)


_KRITIK_BASLIK = "Stok kritik seviyede"
_ESIKSIZ_BASLIK = "Stok eşiği girilmemiş"
_GECIKME_BASLIK = "Hakediş gecikmiş"
_HEDEF_BASLIK = "Hedef aşıldı"


# --------------------------------------------------------------------------- #
# Kurulum yardimcilari — hepsi DOGRUDAN yazar (uclar bu dilimin konusu degil)
# --------------------------------------------------------------------------- #


async def _aktor(session, user_factory, email: str, role_key: str = "patron", *, projeler=None):
    """Rolu + proje kapsami AYRI eksenler: `projeler=None` ⇒ tum projeler."""
    user = await user_factory(email=email, password="parola1234", role_key=role_key)
    if projeler is None:
        user.all_projects = True
    else:
        for proje in projeler:
            session.add(ProjectMember(user_id=user.id, project_id=proje.id, role_id=user.role_id))
    await session.flush()
    return user


async def _merkez_depo(session, ad: str = "Merkez Depo") -> Warehouse:
    """`site_id IS NULL` — merkez depo her aktore gorunur (`visible_warehouse_ids`)."""
    depo = Warehouse(name=ad, site_id=None)
    session.add(depo)
    await session.flush()
    return depo


async def _kalem(
    session, kod: str, ad: str, *, min_stock: str | None, unit: str = "Ton"
) -> StockItem:
    item = StockItem(
        code=kod,
        name=ad,
        category=StockCategory.steel,
        unit=unit,
        min_stock=None if min_stock is None else Decimal(min_stock),
    )
    session.add(item)
    await session.flush()
    return item


async def _giris(session, depo: Warehouse, item: StockItem, miktar: str) -> None:
    entry = StockEntry(
        entry_type=StockEntryType.purchase,
        entry_date=date(2026, 8, 1),
        warehouse_id=depo.id,
    )
    session.add(entry)
    await session.flush()
    session.add(StockEntryLine(entry_id=entry.id, item_id=item.id, quantity=Decimal(miktar)))
    await session.flush()


async def _hakedis(
    session,
    project,
    yazan,
    *,
    onay_gunu: date | None,
    vade_gun: int = 30,
    taseron: str | None = "Çelik OSB",
    sequence_no: int = 1,
    durum: SubcontractorPaymentStatus = SubcontractorPaymentStatus.approved,
) -> SubcontractorProgressPayment:
    """Onayli taseron hakedisi. Vade = `approved_at` (TR gunu) + `payment_term_days`."""
    sozlesme = SubcontractorContract(
        project_id=project.id,
        subcontractor_name=taseron,
        payment_term_days=vade_gun,
        created_by=yazan.id,
    )
    session.add(sozlesme)
    await session.flush()
    hakedis = SubcontractorProgressPayment(
        contract_id=sozlesme.id,
        project_id=project.id,
        sequence_no=sequence_no,
        status=durum,
        vat_pct=Decimal("20"),
        advance_pct=Decimal("0"),
        retainage_pct=Decimal("0"),
        created_by=yazan.id,
        approved_at=None if onay_gunu is None else _ogle(onay_gunu),
    )
    session.add(hakedis)
    await session.flush()
    return hakedis


def _ogle(gun: date):
    """TR gununun ortasi — saat dilimi cevriminin gunu kaydirmadigi guvenli an."""
    from datetime import UTC, datetime, time

    return datetime.combine(gun, time(12, 0), tzinfo=UTC)


async def _faturala(session, hakedis, yazan, *, status=InvoiceStatus.approved) -> None:
    """CIFT SAYIM KAPISI kurulumu: hakedis faturalandiysa odenecek olan FATURADIR.

    🔴 HZ-CIFT: `status` PARAMETRIKTIR cunku kapinin sarti DURUM TASIR. Sabit
    `approved` birakilsaydi kusurun kendisi (onaylanmamis faturali borcun
    kartTAN da listeden de kaybolmasi) bu dosyada hic olculemezdi.
    """
    session.add(
        Invoice(
            direction=InvoiceDirection.incoming,
            invoice_no=f"RISK{uuid.uuid4().hex[:9].upper()}",
            document_type=InvoiceDocumentType.einvoice,
            status=status,
            issue_date=date(2026, 8, 1),
            party_name="Çelik OSB",
            subcontractor_progress_payment_id=hakedis.id,
            subtotal=Decimal("100.00"),
            tax_base=Decimal("100.00"),
            vat_amount=Decimal("20.00"),
            total=Decimal("120.00"),
            created_by_id=yazan.id,
        )
    )
    await session.flush()


async def _bolum(
    session, project, *, durum: SectionStatus, bitis: date | None, ad: str = "Kaba İnşaat"
) -> Section:
    site = Site(project_id=project.id, code=f"S-{ad[:3]}-{project.code}", name="A Blok")
    session.add(site)
    await session.flush()
    section = Section(site_id=site.id, name=ad, status=durum, end_date=bitis)
    session.add(section)
    await session.flush()
    return section


def _satirlar(kart, baslik: str) -> list[dict]:
    return [satir for satir in kart if satir["title"] == baslik]


async def _kart(session, user) -> dict:
    """Servis katmanindan kart — `model_dump` ile ekranin gordugu bicimde."""
    ozet = await build_summary(session, user)
    return ozet.risks.model_dump(mode="json")


# --------------------------------------------------------------------------- #
# 1) STOK — kritik satir + esiksiz kalem sayaci
# --------------------------------------------------------------------------- #


async def test_kritik_stok_kalemi_UYARI_satiri_basar(seeded_db, user_factory):
    """(1) OLUMLU KONTROL — kritik esigin altindaki kalem satir uretir.

    Aritmetik TESTTE ACIKCA yazilidir: `CRITICAL_RATIO = 0.5`, esik 10 ⇒ 5'in
    ALTI kritiktir; bakiye 2'dir. Yalnizca "bir satir var" cakilsaydi, esigi
    hic okumayan bir mutasyon da yesil gecerdi.
    """
    user = await _aktor(seeded_db, user_factory, "risk-stok1@d.co")
    depo = await _merkez_depo(seeded_db)
    demir = await _kalem(seeded_db, "RISK-D1", "Nervürlü Demir Ø12", min_stock="10")
    await _giris(seeded_db, depo, demir, "2")

    kart = await _kart(seeded_db, user)

    satirlar = _satirlar(kart["items"], _KRITIK_BASLIK)
    assert len(satirlar) == 1, kart["items"]
    assert satirlar[0]["severity"] == "warning", (
        "mockup `:378` sol seridi #f59e0b (kehribar) — stok satiri `danger` DEĞİLDİR"
    )
    assert satirlar[0]["module"] == STOCK_MODULE
    assert satirlar[0]["detail"] == "Nervürlü Demir Ø12 – kalan 2 Ton"


async def test_esigin_USTUNDEKI_kalem_satir_URETMEZ(seeded_db, user_factory):
    """(2) KARSIT KANIT — esik okunmadan her kalemi basan bir kart yakalanir."""
    user = await _aktor(seeded_db, user_factory, "risk-stok2@d.co")
    depo = await _merkez_depo(seeded_db)
    cimento = await _kalem(seeded_db, "RISK-C1", "Çimento", min_stock="10")
    await _giris(seeded_db, depo, cimento, "20")

    kart = await _kart(seeded_db, user)

    assert _satirlar(kart["items"], _KRITIK_BASLIK) == []
    assert kart["available"] is True, "kaynak okundu — kart KAPALI değil, uyarı YOK"


async def test_esigi_GIRILMEMIS_kalem_AYRI_bir_uyariyla_SAYILIR(seeded_db, user_factory):
    """(3) 🔴 "BİLİNMİYOR" HÂLİ SESSİZ KALAMAZ (K2).

    `min_stock` NULL iken `status_case` durumu `NULL` birakir — kalem HICBIR
    kovaya dusmez. Sayac olmasaydi bos liste hem "risk yok" hem "risk
    BILINMIYOR" demeye devam ederdi; bu turda tam bu siniftan IKI CANLI KUSUR
    cikti.

    Iki iddia birden: (a) esiksiz kalem KRITIK satiri URETMEZ, (b) AYRI bir
    `warning` satirinda SAYILIR.
    """
    user = await _aktor(seeded_db, user_factory, "risk-stok3@d.co")
    depo = await _merkez_depo(seeded_db)
    alci = await _kalem(seeded_db, "RISK-A1", "Alçı", min_stock=None, unit="Torba")
    await _giris(seeded_db, depo, alci, "1")

    kart = await _kart(seeded_db, user)

    assert _satirlar(kart["items"], _KRITIK_BASLIK) == [], "eşiksiz kalem KRİTİK sayılamaz"
    esiksiz = _satirlar(kart["items"], _ESIKSIZ_BASLIK)
    assert len(esiksiz) == 1, kart["items"]
    assert esiksiz[0]["detail"] == "1 kalemin asgari stoğu yok — durumu bilinmiyor"
    assert esiksiz[0]["severity"] == "warning"


async def test_esigi_OLAN_kalem_esiksiz_sayacina_GIRMEZ(seeded_db, user_factory):
    """(4) KARSIT KANIT — sayac `min_stock IS NULL` suzgecini GERCEKTEN uygular.

    Suzgeci silen mutasyon "1 kalemin" yerine "2 kalemin" yazardi.
    """
    user = await _aktor(seeded_db, user_factory, "risk-stok4@d.co")
    depo = await _merkez_depo(seeded_db)
    esiksiz = await _kalem(seeded_db, "RISK-E1", "Kum", min_stock=None, unit="m³")
    esikli = await _kalem(seeded_db, "RISK-E2", "Çakıl", min_stock="10", unit="m³")
    await _giris(seeded_db, depo, esiksiz, "1")
    await _giris(seeded_db, depo, esikli, "50")

    kart = await _kart(seeded_db, user)

    satirlar = _satirlar(kart["items"], _ESIKSIZ_BASLIK)
    assert len(satirlar) == 1
    assert satirlar[0]["detail"].startswith("1 kalemin"), satirlar[0]["detail"]


# --------------------------------------------------------------------------- #
# 2) HAKEDIS GECIKMESI — pencere, cift sayim, kapsam
# --------------------------------------------------------------------------- #


async def test_vadesi_GECMIS_hakedis_TEHLIKE_satiri_basar(seeded_db, user_factory, project_factory):
    """(5) OLUMLU KONTROL — mockup'in ikinci satiri BIREBIR.

    Vade = onay gunu + `payment_term_days`. 44 gun once onaylanmis, vadesi 30
    gun olan hakedis bugun **14 gun** gecikmistir — mockup `:386`nin sayisi.
    """
    yazan = await _aktor(seeded_db, user_factory, "risk-yz1@d.co", role_key="system_admin")
    user = await _aktor(seeded_db, user_factory, "risk-hk1@d.co")
    proje = await project_factory(code="RISK-HK1")
    await _hakedis(seeded_db, proje, yazan, onay_gunu=today() - timedelta(days=44), vade_gun=30)

    kart = await _kart(seeded_db, user)

    satirlar = _satirlar(kart["items"], _GECIKME_BASLIK)
    assert len(satirlar) == 1, kart["items"]
    assert satirlar[0]["severity"] == "danger", "mockup `:386` sol şeridi #ef4444 (kırmızı)"
    assert satirlar[0]["detail"] == "Çelik OSB – 14 gün gecikme"
    assert satirlar[0]["module"] == PROGRESS_PAYMENT_MODULE


async def test_vadesi_GELMEMIS_hakedis_satir_URETMEZ(seeded_db, user_factory, project_factory):
    """(6) KARSIT KANIT — pencere GECMISTIR. `vade < bugun` suzgecini silen
    mutasyon, vadesi HENUZ GELMEMIS bir borcu "gecikmis" gosterirdi."""
    yazan = await _aktor(seeded_db, user_factory, "risk-yz2@d.co", role_key="system_admin")
    user = await _aktor(seeded_db, user_factory, "risk-hk2@d.co")
    proje = await project_factory(code="RISK-HK2")
    await _hakedis(seeded_db, proje, yazan, onay_gunu=today() - timedelta(days=10), vade_gun=30)

    kart = await _kart(seeded_db, user)

    assert _satirlar(kart["items"], _GECIKME_BASLIK) == []


async def test_FATURALANMIS_hakedis_HALA_gecikmis_sayilir(seeded_db, user_factory, project_factory):
    """(7) 🔴 DENETIM BULGUSU 4 — DAVRANIS TERSINE CEVRILDI.

    Eskiden bu test "faturalanmis hakedis satir URETMEZ" diyordu ve gerekcesi
    `upcoming.py`den ODUNC ALINMISTI: *"ikisi de listelenseydi ayni gecikme iki
    satir uretirdi"*. O gerekce BURADA GECERSIZDIR cunku bu kartta bir FATURA
    DALI YOKTUR (olculdu: `dashboard` paketi `app.modules.invoicing`i hic import
    etmez, `command grep ... EXIT=1`). Dislama hicbir cift sayimi engellemiyor,
    yalnizca gecikmis borcu SESSIZCE siliyordu.

    Faturasi kesilmis olmak bir borcu gecikmis olmaktan CIKARMAZ. Uyariyi
    susturan tek sey borcun KAPANMASIDIR (`paid`) ve o damga ODM-2'den sonra
    ancak GERCEKLESMIS para ile basilabilir — karsit kanit:
    `test_ODENMIS_hakedis_satir_URETMEZ`."""
    yazan = await _aktor(seeded_db, user_factory, "risk-yz3@d.co", role_key="system_admin")
    user = await _aktor(seeded_db, user_factory, "risk-hk3@d.co")
    proje = await project_factory(code="RISK-HK3")
    hakedis = await _hakedis(
        seeded_db, proje, yazan, onay_gunu=today() - timedelta(days=44), vade_gun=30
    )
    await _faturala(seeded_db, hakedis, yazan)

    kart = await _kart(seeded_db, user)

    assert len(_satirlar(kart["items"], _GECIKME_BASLIK)) == 1


@pytest.mark.parametrize("durum", [InvoiceStatus.pending, InvoiceStatus.disputed])
async def test_ONAYLANMAMIS_faturali_gecikme_KARTTA_KALIR(
    seeded_db, user_factory, project_factory, durum
):
    """🔴 Faturanin DURUMU ne olursa olsun gecikme uyarisi DUSMEZ (bulgu 4).

    Bu kartta fatura dali olmadigi icin dislama tamamen kaldirildi; test, hangi
    durumda olursa olsun faturalanmis bir gecikmenin panelde KALDIGINI kilitler.
    """
    yazan = await _aktor(seeded_db, user_factory, "risk-yz3b@d.co", role_key="system_admin")
    user = await _aktor(seeded_db, user_factory, "risk-hk3b@d.co")
    proje = await project_factory(code="RISK-HK3B")
    hakedis = await _hakedis(
        seeded_db, proje, yazan, onay_gunu=today() - timedelta(days=44), vade_gun=30
    )
    await _faturala(seeded_db, hakedis, yazan, status=durum)

    kart = await _kart(seeded_db, user)

    assert len(_satirlar(kart["items"], _GECIKME_BASLIK)) == 1


async def test_GORUNMEYEN_projenin_gecikmesi_SIZMAZ(seeded_db, user_factory, project_factory):
    """(8) KARSIT KANIT — IDOR. Kapsam suzgeci dusseydi aktor, goremedigi
    projenin TASERON ADINI ve gecikmesini panelde okurdu.

    K-IKIZ1: olumsuz iddianin yaninda OLUMLU kontrol durur — ayni aktor GORDUGU
    projenin gecikmesini GORUR. Yoksa hicbir satir uretmeyen bir mutasyon da
    yesil gecerdi."""
    yazan = await _aktor(seeded_db, user_factory, "risk-yz4@d.co", role_key="system_admin")
    gorunur = await project_factory(code="RISK-GOR")
    gizli = await project_factory(code="RISK-GIZ")
    user = await _aktor(seeded_db, user_factory, "risk-hk4@d.co", projeler=[gorunur])
    await _hakedis(
        seeded_db,
        gorunur,
        yazan,
        onay_gunu=today() - timedelta(days=44),
        vade_gun=30,
        taseron="Görünür Ltd.",
    )
    await _hakedis(
        seeded_db,
        gizli,
        yazan,
        onay_gunu=today() - timedelta(days=44),
        vade_gun=30,
        taseron="Gizli Ltd.",
    )

    kart = await _kart(seeded_db, user)

    ayrintilar = [satir["detail"] for satir in _satirlar(kart["items"], _GECIKME_BASLIK)]
    assert ayrintilar == ["Görünür Ltd. – 14 gün gecikme"], ayrintilar


async def test_ODENMIS_hakedis_satir_URETMEZ(seeded_db, user_factory, project_factory):
    """(8b) KARSIT KANIT — `paid` hakedis BORC DEGILDIR.

    🔴 Bu bekci `status == approved` sartinin TEK gercek bekcisidir ve gerekcesi
    olculmustur: `draft`/`pending_approval` hakedisin `approved_at`i zaten
    NULL'dir, yani onlari eleyen sey durum suzgeci DEGIL vade ifadesidir
    (`NULL < bugun` -> NULL -> WHERE eler). YALNIZ `paid` hâli hem onay damgasi
    tasir hem borcu kapanmistir; durum suzgecini silen mutasyon ODENMIS bir
    hakedisi "gecikmis" gosterirdi ve BASKA HICBIR TEST bunu gormezdi."""
    yazan = await _aktor(seeded_db, user_factory, "risk-yz8@d.co", role_key="system_admin")
    user = await _aktor(seeded_db, user_factory, "risk-hk5@d.co")
    proje = await project_factory(code="RISK-HK5")
    await _hakedis(
        seeded_db,
        proje,
        yazan,
        onay_gunu=today() - timedelta(days=44),
        vade_gun=30,
        durum=SubcontractorPaymentStatus.paid,
    )

    kart = await _kart(seeded_db, user)

    assert _satirlar(kart["items"], _GECIKME_BASLIK) == []


async def test_gecikme_satirlari_EN_GEC_KALAN_ILK_SIRADA(seeded_db, user_factory, project_factory):
    """(8c) 🔴 GRUP ICI SIRA — `ORDER BY vade` NIHAI SIRA MIDIR?

    `_overdue_payment_alerts` docstring'i (`risks.py:217`) "En gec kalan ilk
    sirada (`ORDER BY vade`)" der. SQL bunu SAGLAR, ama `build_risks` sonundaki
    `alerts.sort(...)` onu EZIYORDU: anahtarda `alert.detail` vardi ve gecikme
    satirlarinin `title`i SABIT oldugu icin (`"Hakediş gecikmiş"`) ayni `danger`
    grubunun ICINDEKI tek ayirt edici anahtar `detail` STRINGIYDI. String
    siralamasinda once taseron ADI, sonra gun sayisi METIN olarak dizilir:
    `"90 gün"` metin olarak `"14 gün"`den BUYUKTUR ama `"Ada Beton"`
    `"Çelik OSB"`den ONCE gelir — yani EN AGIR gecikme listenin EN ALTINA
    duserdi.

    🔴 MEVCUT KUME BEKCISI KORDUR: `test_uyari_KUMESI_bagimsiz_beklentiyle_...`
    `danger` grubunda TEK satir kurar (tek `_hakedis` cagrisi), grup ICI sirayi
    olcmesi YAPISAL OLARAK imkansizdir.

    Kurulum sirasi BILEREK 5 -> 90 -> 14'tur: eklenme sirasinin tesadufen
    beklentiyi uretmesi engellenir, iddia SQL'in `vade` sirasina dayanir.
    """
    yazan = await _aktor(seeded_db, user_factory, "risk-yz11@d.co", role_key="system_admin")
    user = await _aktor(seeded_db, user_factory, "risk-sira@d.co")
    proje = await project_factory(code="RISK-SIRA")
    # Vade = onay gunu (TR) + `payment_term_days`; gecikme = bugun - vade.
    for gecikme, taseron in ((5, "Ada Beton"), (90, "Çelik OSB"), (14, "Çelik OSB")):
        await _hakedis(
            seeded_db,
            proje,
            yazan,
            onay_gunu=today() - timedelta(days=30 + gecikme),
            vade_gun=30,
            taseron=taseron,
        )

    kart = await _kart(seeded_db, user)

    ayrintilar = [satir["detail"] for satir in _satirlar(kart["items"], _GECIKME_BASLIK)]
    assert ayrintilar == [
        "Çelik OSB – 90 gün gecikme",
        "Çelik OSB – 14 gün gecikme",
        "Ada Beton – 5 gün gecikme",
    ], ayrintilar


# --------------------------------------------------------------------------- #
# 3) TAKVIM — "Hedef asildi" (mockup'in IYI HABERI)
# --------------------------------------------------------------------------- #


async def test_planlanan_bitisten_ONCE_tamamlanan_bolum_BASARI_satiri_basar(
    seeded_db, user_factory, project_factory
):
    """(9) OLUMLU KONTROL — `success` GERCEKTEN uretilir.

    Mockup'in ucuncu satiri bir risk DEGIL iyi haberdir; siddet uyesi
    suslemeden ibaret olsaydi bu bekci kirilirdi."""
    user = await _aktor(seeded_db, user_factory, "risk-tk1@d.co")
    proje = await project_factory(code="RISK-TK1", name="Belediye Yol")
    await _bolum(seeded_db, proje, durum=SectionStatus.completed, bitis=today() + timedelta(days=5))

    kart = await _kart(seeded_db, user)

    satirlar = _satirlar(kart["items"], _HEDEF_BASLIK)
    assert len(satirlar) == 1, kart["items"]
    assert satirlar[0]["severity"] == "success", "mockup `:394` sol şeridi #22c55e (yeşil)"
    assert satirlar[0]["module"] == SCHEDULE_MODULE
    assert satirlar[0]["detail"] == (
        "Belediye Yol – Kaba İnşaat: planlanan bitişe 5 gün kala tamamlandı"
    )


@pytest.mark.parametrize(
    ("durum", "bitis_farki", "gerekce"),
    [
        (SectionStatus.active, 5, "tamamlanmamış bölüm hedefi AŞMIŞ sayılamaz"),
        (SectionStatus.completed, -5, "planlanan bitiş GEÇMİŞSE erken tamamlanma İDDİA EDİLEMEZ"),
    ],
)
async def test_hedef_asildi_satiri_YALNIZ_iki_kosul_birlikteyken_dogar(
    seeded_db, user_factory, project_factory, durum, bitis_farki, gerekce
):
    """(10) KARSIT KANIT — IKI kosulun HER BIRI ayri bekcilenir.

    🔴 Katmanli savunmada her katmanin KENDI bagimsiz bekcisi olmali: tek bir
    kurulum iki sarti birden saglasaydi, birini silen mutasyon otekinin
    arkasinda maskelenirdi."""
    user = await _aktor(seeded_db, user_factory, f"risk-tk-{durum.value}{bitis_farki}@d.co")
    proje = await project_factory(code=f"RISK-TK{durum.value}{bitis_farki}")
    await _bolum(seeded_db, proje, durum=durum, bitis=today() + timedelta(days=bitis_farki))

    kart = await _kart(seeded_db, user)

    assert _satirlar(kart["items"], _HEDEF_BASLIK) == [], gerekce


# --------------------------------------------------------------------------- #
# 4) IZIN — kart KISMI dolar (ILR kanonu)
# --------------------------------------------------------------------------- #


async def test_muhasebe_STOGU_gormez_ama_GECIKMEYI_GORUR(
    client, seeded_db, user_factory, project_factory
):
    """(11) 🔴 ILR KANONU BIREBIR: izni olana veriyi ver, olmayana kaynagi sustur.

    Olculdu (`roles/seed_data.py` MATRIX): `accounting` icin
    `inventory = _N` ama `progress_payments = _APR` ve `sites = _FIN`. Yani
    muhasebeci stok satirlarini GORMEZ, hakedis gecikmesini GORUR.

    K-IKIZ1 — olumsuz iddianin karsiliginda IKI olumlu kontrol durur:
      (a) ayni kurulumda `patron` stok satirini GORUR (kart bozuk degil),
      (b) muhasebeci hakedis satirini GORUR (kart tamamen kapatilmamis).
    Iddia HTTP ucundan gecer: bekci KULLANICININ gordugunu olcmelidir.
    """
    yazan = await _aktor(seeded_db, user_factory, "risk-yz5@d.co", role_key="system_admin")
    proje = await project_factory(code="RISK-IZ1")
    depo = await _merkez_depo(seeded_db)
    demir = await _kalem(seeded_db, "RISK-IZ-D", "Nervürlü Demir Ø12", min_stock="10")
    await _giris(seeded_db, depo, demir, "2")
    await _hakedis(seeded_db, proje, yazan, onay_gunu=today() - timedelta(days=44), vade_gun=30)

    await _aktor(seeded_db, user_factory, "risk-muh@d.co", role_key="accounting")
    await _aktor(seeded_db, user_factory, "risk-ptr@d.co", role_key="patron")

    muhasebe = await _panel(client, "risk-muh@d.co")
    patron = await _panel(client, "risk-ptr@d.co")

    durumlar = {kaynak["module"]: kaynak["state"] for kaynak in muhasebe["sources"]}
    assert durumlar == {
        STOCK_MODULE: "restricted",
        PROGRESS_PAYMENT_MODULE: "ok",
        SCHEDULE_MODULE: "ok",
    }, durumlar
    assert _satirlar(muhasebe["items"], _KRITIK_BASLIK) == [], "stok izni YOK — satır sızmamalı"
    assert _satirlar(muhasebe["items"], _ESIKSIZ_BASLIK) == [], (
        "eşiksiz kalem SAYISI da stok verisidir — izinsiz aktöre sızmamalı"
    )
    assert len(_satirlar(muhasebe["items"], _GECIKME_BASLIK)) == 1, (
        "kart TAMAMEN kapatılmış — ILR kanonu kısmî dolum ister"
    )
    assert muhasebe["available"] is True

    # (a) POZITIF KONTROL: kurulum gercekten kritik bir kalem tasiyor.
    assert len(_satirlar(patron["items"], _KRITIK_BASLIK)) == 1
    assert len(_satirlar(patron["items"], _GECIKME_BASLIK)) == 1
    assert {kaynak["state"] for kaynak in patron["sources"]} == {"ok"}


async def _panel(client, email: str) -> dict:
    resp = await client.post("/auth/login", json={"email": email, "password": "parola1234"})
    assert resp.status_code == 200, resp.text
    headers = {"Authorization": f"Bearer {resp.json()['access_token']}"}
    ozet = await client.get("/dashboard/summary", headers=headers)
    assert ozet.status_code == 200, ozet.text
    return ozet.json()["risks"]


async def test_IK_iki_kaynakta_KISITLI_takvimi_GORUR(
    client, seeded_db, user_factory, project_factory
):
    """(12) IKINCI IZIN PROFILI — `hr_manager`: `inventory=_N`, `progress_payments=_N`,
    `sites=_LIM`. Kart iki kaynakta susar, UCUNCUSUNDE KONUSUR.

    Tek bir izin profili cakilsaydi "her seyi kapat" mutasyonu (ya da "hicbir
    seyi kapatma") biri tarafindan yakalanmayabilirdi."""
    yazan = await _aktor(seeded_db, user_factory, "risk-yz9@d.co", role_key="system_admin")
    proje = await project_factory(code="RISK-IZ2", name="Belediye Yol")
    await _bolum(seeded_db, proje, durum=SectionStatus.completed, bitis=today() + timedelta(days=5))
    # 🔴 SUSTURULACAK IKI KAYNAK GERCEKTEN VERI TASIR: kurulum bos olsaydi
    # kapiyi silen mutasyon da yesil gecerdi (olculdu: gecti).
    depo = await _merkez_depo(seeded_db)
    demir = await _kalem(seeded_db, "RISK-IZ2-D", "Nervürlü Demir Ø12", min_stock="10")
    await _giris(seeded_db, depo, demir, "2")
    await _hakedis(seeded_db, proje, yazan, onay_gunu=today() - timedelta(days=44), vade_gun=30)
    await _aktor(seeded_db, user_factory, "risk-ik@d.co", role_key="hr_manager")

    kart = await _panel(client, "risk-ik@d.co")

    durumlar = {kaynak["module"]: kaynak["state"] for kaynak in kart["sources"]}
    assert durumlar == {
        STOCK_MODULE: "restricted",
        PROGRESS_PAYMENT_MODULE: "restricted",
        SCHEDULE_MODULE: "ok",
    }, durumlar
    assert len(_satirlar(kart["items"], _HEDEF_BASLIK)) == 1, (
        "izni OLAN kaynak da susmuş — kapı gereğinden geniş"
    )
    assert _satirlar(kart["items"], _KRITIK_BASLIK) == [], "stok izni YOK"
    assert _satirlar(kart["items"], _GECIKME_BASLIK) == [], "hakediş izni YOK"


async def test_TAKVIM_kapisi_kapatilinca_YALNIZ_takvim_susar(
    client, seeded_db, user_factory, project_factory
):
    """(12b) 🔴 UCUNCU KAPININ KENDI BEKCISI.

    Kapi kapali kalsaydi eksik olurdu: tohumlanmis matriste paneli acabilen HER
    rol `sites`i en az `view` seviyesinde okur (`dashboard` satirinda `_N` olan
    tek rol `procurement`tir ve o paneli hic acamaz). Yani bu kapi CANLI
    matriste hicbir rolle tetiklenmez — bekcisi olmasaydi kapiyi silen mutasyon
    SAG KALIRDI (olculdu: sag kaldi).

    Bu yuzden izin hucresi testte KAPATILIR (`_set_permission` emsali,
    `test_ilr_ilerleme.py:489`). Iddia cift yonludur: takvim satiri DUSER, oteki
    iki kaynak KONUSMAYA DEVAM EDER — "her seyi kapat" mutasyonu da yakalanir.
    """
    yazan = await _aktor(seeded_db, user_factory, "risk-yz10@d.co", role_key="system_admin")
    proje = await project_factory(code="RISK-IZ3", name="Belediye Yol")
    await _bolum(seeded_db, proje, durum=SectionStatus.completed, bitis=today() + timedelta(days=5))
    depo = await _merkez_depo(seeded_db)
    demir = await _kalem(seeded_db, "RISK-IZ3-D", "Nervürlü Demir Ø12", min_stock="10")
    await _giris(seeded_db, depo, demir, "2")
    await _hakedis(seeded_db, proje, yazan, onay_gunu=today() - timedelta(days=44), vade_gun=30)
    await _aktor(seeded_db, user_factory, "risk-nosite@d.co", role_key="patron")
    await _set_permission(seeded_db, "patron", SCHEDULE_MODULE, AccessLevel.none)

    kart = await _panel(client, "risk-nosite@d.co")

    durumlar = {kaynak["module"]: kaynak["state"] for kaynak in kart["sources"]}
    assert durumlar[SCHEDULE_MODULE] == "restricted", durumlar
    assert _satirlar(kart["items"], _HEDEF_BASLIK) == [], "takvim izni YOK — satır sızmamalı"
    assert len(_satirlar(kart["items"], _KRITIK_BASLIK)) == 1, "stok kaynağı susturulmamalı"
    assert len(_satirlar(kart["items"], _GECIKME_BASLIK)) == 1, "hakediş kaynağı susturulmamalı"


# --------------------------------------------------------------------------- #
# 5) KUME — sahte bir uye eklemek "kac satir" testinden KACAR
# --------------------------------------------------------------------------- #


async def test_uyari_KUMESI_bagimsiz_beklentiyle_BIREBIR_esittir(
    client, seeded_db, user_factory, project_factory
):
    """(13) 🔴 KUME BEKCISI — evren TESTTE elle kuruldu, kart onunla karsilastirilir.

    "Kac uyari uretildi" testi kumeye SAHTE BIR UYE eklemeyi yakalamaz. Burada
    (siddet, baslik, ayrinti) UCLULERININ TAMAMI cakilir; fazladan tek bir satir
    da, eksik tek bir satir da testi kirar.

    Siralama da iddiadir: once `danger`, sonra `warning`, en son `success` —
    iyi haber kotu haberin ustune cikamaz.
    """
    yazan = await _aktor(seeded_db, user_factory, "risk-yz6@d.co", role_key="system_admin")
    proje = await project_factory(code="RISK-KUME", name="Belediye Yol")
    depo = await _merkez_depo(seeded_db)
    demir = await _kalem(seeded_db, "RISK-K-D", "Nervürlü Demir Ø12", min_stock="10")
    await _giris(seeded_db, depo, demir, "2")
    esiksiz = await _kalem(seeded_db, "RISK-K-A", "Alçı", min_stock=None, unit="Torba")
    await _giris(seeded_db, depo, esiksiz, "3")
    await _hakedis(seeded_db, proje, yazan, onay_gunu=today() - timedelta(days=44), vade_gun=30)
    await _bolum(seeded_db, proje, durum=SectionStatus.completed, bitis=today() + timedelta(days=5))
    await _aktor(seeded_db, user_factory, "risk-kume@d.co", role_key="patron")

    kart = await _panel(client, "risk-kume@d.co")

    assert [(s["severity"], s["title"], s["detail"]) for s in kart["items"]] == [
        ("danger", _GECIKME_BASLIK, "Çelik OSB – 14 gün gecikme"),
        ("warning", _ESIKSIZ_BASLIK, "1 kalemin asgari stoğu yok — durumu bilinmiyor"),
        ("warning", _KRITIK_BASLIK, "Nervürlü Demir Ø12 – kalan 2 Ton"),
        (
            "success",
            _HEDEF_BASLIK,
            "Belediye Yol – Kaba İnşaat: planlanan bitişe 5 gün kala tamamlandı",
        ),
    ]


async def test_hicbir_uyari_yokken_liste_BOS_ve_kart_ACIKTIR(seeded_db, user_factory):
    """(14) UCUNCU HAL — bos liste artik OTORITER bir "uyari yok"tur.

    Kurulumda esigi OLAN ve esigin USTUNDE bir kalem var: yani hem "kritik yok"
    hem "esiksiz kalem yok". Bu, `items == []`in mesru tek anlamidir."""
    user = await _aktor(seeded_db, user_factory, "risk-bos@d.co")
    depo = await _merkez_depo(seeded_db)
    cimento = await _kalem(seeded_db, "RISK-B1", "Çimento", min_stock="10")
    await _giris(seeded_db, depo, cimento, "42")

    kart = await _kart(seeded_db, user)

    assert kart["items"] == []
    assert kart["available"] is True
    assert {kaynak["state"] for kaynak in kart["sources"]} == {"ok"}


# --------------------------------------------------------------------------- #
# 6) N+1 — kart panelin SICAK yolundadir
# --------------------------------------------------------------------------- #


async def test_kart_sorgu_sayisi_SATIR_SAYISINDAN_BAGIMSIZ(
    seeded_db, user_factory, project_factory
):
    """(15) 🔴 N+1 YOK. Bir uyari ile ON uyari AYNI sorgu sayisini uretmeli.

    Esitlik iddiasi mutlak bir tavandan gucludur: tavan dilim buyudukce sessizce
    gevsetilebilir, esitlik satir basina ek sorgu eklendigi anda kirilir."""
    yazan = await _aktor(seeded_db, user_factory, "risk-yz7@d.co", role_key="system_admin")
    user = await _aktor(seeded_db, user_factory, "risk-n1@d.co")
    proje = await project_factory(code="RISK-N1")
    depo = await _merkez_depo(seeded_db)
    kalem = await _kalem(seeded_db, "RISK-N-0", "Kalem 0", min_stock="10")
    await _giris(seeded_db, depo, kalem, "1")

    with _sorgu_sayaci() as tek:
        bir = await _kart(seeded_db, user)
    assert _satirlar(bir["items"], _KRITIK_BASLIK)

    for n in range(1, 10):
        kalem = await _kalem(seeded_db, f"RISK-N-{n}", f"Kalem {n}", min_stock="10")
        await _giris(seeded_db, depo, kalem, "1")
        await _hakedis(
            seeded_db,
            proje,
            yazan,
            onay_gunu=today() - timedelta(days=44),
            vade_gun=30,
            sequence_no=n,
        )

    with _sorgu_sayaci() as on:
        onlu = await _kart(seeded_db, user)
    assert len(_satirlar(onlu["items"], _GECIKME_BASLIK)) == 3, (
        "kaynak başına TAVAN uygulanmalı (SQL LIMIT)"
    )

    assert len(on) == len(tek), (
        f"uyarı sayısı 1→10 olunca panelin sorgu sayısı {len(tek)}→{len(on)} oldu — N+1"
    )
