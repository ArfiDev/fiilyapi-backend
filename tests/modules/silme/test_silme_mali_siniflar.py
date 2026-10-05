"""Mali sınıf kapsamı: satış/kapora, kısmi taksit, kök (`is_financial`) + puantaj uyarısı.

SIL-B2: mali kayıt silmeyi ENGELLEMEZ (`financial_pending` kalktı); sınıf yalnız önizlemede
vurgudur ve silme yine de 204 döner. Sınıflandırma `core/silme/etiketler.py` içindedir; tam liste
bekçisi `tests/core/test_silme_etiket_bekcisi.py`dir. Puantaj artık mali SAYILMAZ: kapanmış bordro
ayına düşen puantaj satırı yalnız kendisi silinir, bordro yerinde kalır, önizleme uyarır.
"""

from decimal import Decimal

import pytest

from app.core.db import Base
from app.core.silme.cozucu import SilmeAgaci, mali_sayisi
from app.modules.accounting.models import JournalEntry, JournalSourceType
from app.modules.payroll.models import PayrollPeriod, PayrollPeriodStatus
from app.modules.sales.models import SaleType, UnitSale, UnitSaleStatus
from app.modules.timesheet.models import TimesheetEntry
from app.modules.units.models import Unit
from tests._silme_yardimci import onizle, sil_aile, sil_genel, sisyon_girisi
from tests.modules.silme import _dunya as d
from tests.modules.silme import _mali_dunya as m


@pytest.fixture
async def sistem(client, user_factory):
    return await sisyon_girisi(client, user_factory)


@pytest.fixture
async def olusturan(user_factory):
    return await user_factory(
        email="olusturan@mali.co", password="parola1234", role_key="site_chief"
    )


async def _blok_unite(db_session, project_factory, kod: str):
    proje = await project_factory(kod)
    snt = await d.site(db_session, proje)
    blok = await d.block(db_session, proje, snt)
    birim = await d.unit(db_session, proje, blok, "1")
    return proje, snt, blok, birim


async def _mali_ama_silinir(client, sistem, kind, kayit_id) -> None:
    """Mali kayıt silmeyi ENGELLEMEZ: önizleme + silme 204."""
    yanit = await sil_genel(client, sistem, kind, kayit_id)
    assert yanit.status_code == 204, yanit.text


# --- Ünite satışı: durumdan BAĞIMSIZ kapora / tahsilat ---


async def test_kapora_alinmis_rezervasyon_mali_sayilir(
    client, db_session, sistem, project_factory, olusturan
) -> None:
    proje, _, _, birim = await _blok_unite(db_session, project_factory, "ML-1")
    await d.satis(db_session, proje, birim, olusturan, kapora=Decimal("50000.00"))

    onizleme = (await onizle(client, sistem, "unit", birim.id)).json()
    satislar = next(g for g in onizleme["groups"] if g["table"] == "unit_sales")
    assert satislar["is_financial"] is True  # önizlemede mali görünür
    await _mali_ama_silinir(client, sistem, "unit", birim.id)
    assert await d.sayim(db_session, UnitSale) == 0  # satış (mali) birlikte silindi


async def test_kaporasiz_tahsilatsiz_rezervasyon_silinir(
    client, db_session, sistem, project_factory, olusturan
) -> None:
    proje, _, _, birim = await _blok_unite(db_session, project_factory, "ML-2")
    await d.satis(db_session, proje, birim, olusturan)  # kapora yok, taksit yok

    onizleme = (await onizle(client, sistem, "unit", birim.id)).json()
    assert all(g["is_financial"] is False for g in onizleme["groups"])
    yanit = await sil_aile(client, sistem, "unit", birim.id)

    assert yanit.status_code == 204
    assert await d.sayim(db_session, UnitSale) == 0
    assert await d.sayim(db_session, Unit) == 0


async def test_sifir_kapora_mali_degildir(
    client, db_session, sistem, project_factory, olusturan
) -> None:
    proje, _, _, birim = await _blok_unite(db_session, project_factory, "ML-3")
    await d.satis(db_session, proje, birim, olusturan, kapora=Decimal("0.00"))

    assert (await sil_aile(client, sistem, "unit", birim.id)).status_code == 204


async def test_kismi_tahsilatli_taksit_mali_paid_at_bos_olsa_da(
    client, db_session, sistem, project_factory, olusturan
) -> None:
    """`paid_at` yalnız TAM ödemede dolar; kısmi tahsilat `paid_amount`ta kalır."""
    proje, _, _, birim = await _blok_unite(db_session, project_factory, "ML-4")
    kayit = await d.satis(db_session, proje, birim, olusturan)
    taksit = await d.taksit(db_session, kayit, odenen=Decimal("25000.00"), tam_odendi=False)
    assert taksit.paid_at is None  # eski kural (`paid_at IS NOT NULL`) bunu GÖRMEZDİ

    onizleme = (await onizle(client, sistem, "unit", birim.id)).json()
    gruplar = {g["table"]: g for g in onizleme["groups"]}
    assert gruplar["sale_installments"]["is_financial"] is True
    assert gruplar["unit_sales"]["is_financial"] is True  # tahsilatlı taksiti olan satış
    await _mali_ama_silinir(client, sistem, "unit", birim.id)


async def test_iptal_edilmis_ama_kaporali_satis_mali(
    client, db_session, sistem, project_factory, olusturan
) -> None:
    proje, _, _, birim = await _blok_unite(db_session, project_factory, "ML-5")
    await d.satis(
        db_session,
        proje,
        birim,
        olusturan,
        durum=UnitSaleStatus.cancelled,
        kapora=Decimal("10000.00"),
    )

    await _mali_ama_silinir(client, sistem, "unit", birim.id)


async def test_iptal_edilmis_kaporasiz_tahsilatsiz_satis_silinir(
    client, db_session, sistem, project_factory, olusturan
) -> None:
    proje, _, _, birim = await _blok_unite(db_session, project_factory, "ML-6")
    await d.satis(db_session, proje, birim, olusturan, durum=UnitSaleStatus.cancelled)

    assert (await sil_aile(client, sistem, "unit", birim.id)).status_code == 204


async def test_tahsilatsiz_taksit_ve_aktif_satis_durumu_ayrimi(
    client, db_session, sistem, project_factory, olusturan
) -> None:
    """Sözleşmeli (`active`) satış kaporasız/tahsilatsız bile mali: durumu belirler (eski kural)."""
    proje, _, _, birim = await _blok_unite(db_session, project_factory, "ML-7")
    await d.satis(
        db_session,
        proje,
        birim,
        olusturan,
        tur=SaleType.sale,
        durum=UnitSaleStatus.active,
    )

    await _mali_ama_silinir(client, sistem, "unit", birim.id)


async def test_tam_odenmis_taksit_mali(
    client, db_session, sistem, project_factory, olusturan
) -> None:
    proje, _, _, birim = await _blok_unite(db_session, project_factory, "ML-8")
    kayit = await d.satis(db_session, proje, birim, olusturan)
    await d.taksit(db_session, kayit, odenen=Decimal("100000.00"), tam_odendi=True)

    await _mali_ama_silinir(client, sistem, "unit", birim.id)


# --- Kök de mali kontrolün içinde ---


async def test_kok_mali_ise_mali_sayisi_koku_da_tarar_bagimlilar_disinda_kalmaz(
    db_session, project_factory, olusturan
) -> None:
    """`bagimlilar()` kökü HARİÇ tutar; mali kontrol KÖKÜ de taramalıdır (eski kusur)."""
    proje, _, _, birim = await _blok_unite(db_session, project_factory, "ML-9")
    kayit = await d.satis(db_session, proje, birim, olusturan, kapora=Decimal("1.00"))
    kok_mali = SilmeAgaci(
        kok_tablo="unit_sales", kok_pk=(kayit.id,), kayitlar={"unit_sales": {(kayit.id,)}}
    )

    assert kok_mali.bagimli_sayisi() == 0  # kök dışında bağımlı YOK
    assert await mali_sayisi(db_session, Base.metadata, "unit_sales", {(kayit.id,)}) == 1


# --- Puantaj: mali SAYILMAZ; kapanmış bordro ayına düşerse yalnız puantaj gider, bordro kalır ---


async def _puantajli_santiye(db_session, project_factory, olusturan, kod: str):
    proje = await project_factory(kod)
    snt = await d.site(db_session, proje)
    kisi = await d.personel(db_session, f"Personel {kod}")
    giris = await d.puantaj(db_session, proje, snt, kisi, olusturan)  # work_date = 2026-03-02
    return snt, giris


@pytest.mark.parametrize(
    "durum",
    [PayrollPeriodStatus.pending_approval, PayrollPeriodStatus.approved, PayrollPeriodStatus.paid],
)
async def test_kapali_bordro_ayina_dusen_puantaj_silinir_bordro_fisi_ve_mizan_kalir_uyari_yazar(
    client, db_session, sistem, project_factory, olusturan, durum
) -> None:
    snt, _ = await _puantajli_santiye(db_session, project_factory, olusturan, "PT-1")
    donem = await d.bordro_donemi(db_session, 2026, 3, durum)  # puantaj tarihi: 2026-03-02
    hs = await m.hesaplar(db_session)
    await m.fis(db_session, olusturan, hs, kaynak=(JournalSourceType.payroll_period, donem.id))
    donem_id = donem.id
    mizan_once = await m.mizan(db_session)

    onizleme = (await onizle(client, sistem, "site", snt.id)).json()
    puantaj = next(g for g in onizleme["groups"] if g["table"] == "timesheet_entries")
    assert puantaj["is_financial"] is False  # artık mali sayılmaz
    assert onizleme["closed_payroll_timesheet_count"] == 1
    assert onizleme["closed_payroll_periods"] == [{"year": 2026, "month": 3, "status": durum.value}]
    assert onizleme["closed_payroll_message"] == (
        "Bordrosu kapanmış ayda 1 puantaj satırı siliniyor; bordro değişmez"
    )
    assert "payroll_periods" not in {g["table"] for g in onizleme["groups"]}

    assert (await sil_genel(client, sistem, "site", snt.id)).status_code == 204

    assert await d.sayim(db_session, TimesheetEntry) == 0
    assert await d.sayim(db_session, PayrollPeriod, PayrollPeriod.id == donem_id) == 1
    assert await d.sayim(db_session, JournalEntry) == 1  # bordro fişi yerinde
    assert await m.mizan(db_session) == mizan_once  # mizan DEĞİŞMEDİ
    assert set((await m.tutarsizliklar(db_session)).values()) == {0}


async def test_acik_taslak_donemdeki_puantajda_uyari_yok(
    client, db_session, sistem, project_factory, olusturan
) -> None:
    snt, _ = await _puantajli_santiye(db_session, project_factory, olusturan, "PT-2")
    await d.bordro_donemi(db_session, 2026, 3, PayrollPeriodStatus.draft)

    onizleme = (await onizle(client, sistem, "site", snt.id)).json()
    assert onizleme["closed_payroll_timesheet_count"] == 0
    assert onizleme["closed_payroll_periods"] == [] and onizleme["closed_payroll_message"] is None
    assert (await sil_genel(client, sistem, "site", snt.id)).status_code == 204
    assert await d.sayim(db_session, TimesheetEntry) == 0


async def test_donemsiz_puantajda_sayi_sifir_liste_bos(
    client, db_session, sistem, project_factory, olusturan
) -> None:
    snt, _ = await _puantajli_santiye(db_session, project_factory, olusturan, "PT-3")

    onizleme = (await onizle(client, sistem, "site", snt.id)).json()

    assert onizleme["closed_payroll_timesheet_count"] == 0
    assert onizleme["closed_payroll_periods"] == []
    assert (await sil_genel(client, sistem, "site", snt.id)).status_code == 204
    assert await d.sayim(db_session, TimesheetEntry) == 0


async def test_baska_aydaki_kapali_donem_uyari_uretmez(
    client, db_session, sistem, project_factory, olusturan
) -> None:
    """Kapalı dönem YALNIZ kendi yıl+ayındaki girdiyi sayar (Şubat kapalı, puantaj Mart)."""
    snt, _ = await _puantajli_santiye(db_session, project_factory, olusturan, "PT-4")
    await d.bordro_donemi(db_session, 2026, 2, PayrollPeriodStatus.paid)
    await d.bordro_donemi(db_session, 2025, 3, PayrollPeriodStatus.paid)  # farklı yıl, aynı ay

    onizleme = (await onizle(client, sistem, "site", snt.id)).json()

    assert onizleme["closed_payroll_timesheet_count"] == 0
    assert (await sil_genel(client, sistem, "site", snt.id)).status_code == 204


async def test_iki_ayli_puantaj_donemleri_yil_ay_sirasiyla_ve_sayilariyla_bildirilir(
    client, db_session, sistem, project_factory, olusturan
) -> None:
    from datetime import date  # noqa: PLC0415

    snt, giris = await _puantajli_santiye(db_session, project_factory, olusturan, "PT-6")
    ikinci = TimesheetEntry(
        project_id=giris.project_id,
        site_id=snt.id,
        personnel_id=giris.personnel_id,
        work_date=date(2026, 4, 3),
        hours=giris.hours,
        created_by=olusturan.id,
    )
    db_session.add(ikinci)
    await db_session.flush()
    await d.bordro_donemi(db_session, 2026, 4, PayrollPeriodStatus.approved)
    await d.bordro_donemi(db_session, 2026, 3, PayrollPeriodStatus.paid)

    onizleme = (await onizle(client, sistem, "site", snt.id)).json()

    assert onizleme["closed_payroll_timesheet_count"] == 2
    assert [(x["year"], x["month"]) for x in onizleme["closed_payroll_periods"]] == [
        (2026, 3),
        (2026, 4),
    ]
