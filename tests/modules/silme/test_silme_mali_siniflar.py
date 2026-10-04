"""SIL-B1 onarımı — mali sınıf delikleri: satış/kapora, kısmi taksit, kök, puantaj (bordro dönemi).

Kural (CEO eki): SIL-B2'ye kadar ağaçta MALİ satır varsa silme 409 `financial_pending` ve HİÇBİR
ŞEY silinmez. Sınıflandırma `core/silme/etiketler.py` içindedir; tam liste bekçisi
`tests/core/test_silme_etiket_bekcisi.py`dir.
"""

from decimal import Decimal

import pytest
from sqlalchemy import select

from app.core.silme.cozucu import SilmeAgaci
from app.modules.audit.models import AuditAction, AuditLog
from app.modules.payroll.models import PayrollPeriodStatus
from app.modules.sales.models import SaleType, UnitSale, UnitSaleStatus
from app.modules.silme import service
from app.modules.timesheet.models import TimesheetEntry
from app.modules.units.models import Unit
from tests._silme_yardimci import onizle, sil_aile, sil_genel, sisyon_girisi
from tests.modules.silme import _dunya as d

FINANCIAL_PENDING = (
    "Bu kaydın bağlı mali kayıtları var; mali kayıt silme bir sonraki sürümde açılacak"
)


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


async def _silme_engellendi(client, sistem, kind, kayit_id) -> None:
    """409 `financial_pending` döner ve HİÇBİR ŞEY silinmez."""
    onizleme = (await onizle(client, sistem, kind, kayit_id)).json()
    yanit = await client.delete(
        f"/admin/silme/{kind}/{kayit_id}",
        params={"preview_token": onizleme["preview_token"]},
        headers=sistem,
    )
    assert yanit.status_code == 409, yanit.text
    assert yanit.json() == {"detail": FINANCIAL_PENDING, "code": "financial_pending"}


# --- Ünite satışı: durumdan BAĞIMSIZ kapora / tahsilat ---


async def test_kapora_alinmis_rezervasyon_mali_sayilir(
    client, db_session, sistem, project_factory, olusturan
) -> None:
    proje, _, _, birim = await _blok_unite(db_session, project_factory, "ML-1")
    await d.satis(db_session, proje, birim, olusturan, kapora=Decimal("50000.00"))

    onizleme = (await onizle(client, sistem, "unit", birim.id)).json()
    satislar = next(g for g in onizleme["groups"] if g["table"] == "unit_sales")
    assert satislar["is_financial"] is True  # önizlemede mali görünür
    await _silme_engellendi(client, sistem, "unit", birim.id)
    assert await d.sayim(db_session, UnitSale) == 1  # DB değişmedi


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
    await _silme_engellendi(client, sistem, "unit", birim.id)


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

    await _silme_engellendi(client, sistem, "unit", birim.id)


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

    await _silme_engellendi(client, sistem, "unit", birim.id)


async def test_tam_odenmis_taksit_mali(
    client, db_session, sistem, project_factory, olusturan
) -> None:
    proje, _, _, birim = await _blok_unite(db_session, project_factory, "ML-8")
    kayit = await d.satis(db_session, proje, birim, olusturan)
    await d.taksit(db_session, kayit, odenen=Decimal("100000.00"), tam_odendi=True)

    await _silme_engellendi(client, sistem, "unit", birim.id)


# --- Kök de mali kontrolün içinde ---


async def test_kok_mali_ise_de_financial_pending_kok_bagimlilar_disinda_kalmaz(
    db_session, project_factory, olusturan
) -> None:
    """`bagimlilar()` kökü HARİÇ tutar; mali kontrol KÖKÜ de taramalıdır (eski kusur)."""
    proje, _, _, birim = await _blok_unite(db_session, project_factory, "ML-9")
    kayit = await d.satis(db_session, proje, birim, olusturan, kapora=Decimal("1.00"))
    kok_mali = SilmeAgaci(
        kok_tablo="unit_sales", kok_pk=(kayit.id,), kayitlar={"unit_sales": {(kayit.id,)}}
    )

    assert kok_mali.bagimli_sayisi() == 0  # kök dışında bağımlı YOK
    assert await service._mali_var_mi(db_session, kok_mali) is True


# --- Puantaj: kapalı bordro dönemine düşen girdi mali ---


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
async def test_kapali_bordro_donemine_dusen_puantaj_financial_pending(
    client, db_session, sistem, project_factory, olusturan, durum
) -> None:
    snt, _ = await _puantajli_santiye(db_session, project_factory, olusturan, "PT-1")
    await d.bordro_donemi(db_session, 2026, 3, durum)  # puantaj tarihi: 2026-03-02

    onizleme = (await onizle(client, sistem, "site", snt.id)).json()
    puantaj = next(g for g in onizleme["groups"] if g["table"] == "timesheet_entries")
    assert puantaj["is_financial"] is True
    await _silme_engellendi(client, sistem, "site", snt.id)
    assert await d.sayim(db_session, TimesheetEntry) == 1


async def test_acik_taslak_donemdeki_puantaj_silinir(
    client, db_session, sistem, project_factory, olusturan
) -> None:
    snt, _ = await _puantajli_santiye(db_session, project_factory, olusturan, "PT-2")
    await d.bordro_donemi(db_session, 2026, 3, PayrollPeriodStatus.draft)

    onizleme = (await onizle(client, sistem, "site", snt.id)).json()
    puantaj = next(g for g in onizleme["groups"] if g["table"] == "timesheet_entries")
    assert puantaj["is_financial"] is False
    assert (await sil_genel(client, sistem, "site", snt.id)).status_code == 204
    assert await d.sayim(db_session, TimesheetEntry) == 0


async def test_donemsiz_puantaj_silinir(
    client, db_session, sistem, project_factory, olusturan
) -> None:
    snt, _ = await _puantajli_santiye(db_session, project_factory, olusturan, "PT-3")

    assert (await sil_genel(client, sistem, "site", snt.id)).status_code == 204
    assert await d.sayim(db_session, TimesheetEntry) == 0


async def test_baska_aydaki_kapali_donem_puantaji_etkilemez(
    client, db_session, sistem, project_factory, olusturan
) -> None:
    """Kapalı dönem YALNIZ kendi yıl+ayındaki girdiyi mali yapar (Şubat kapalı, puantaj Mart)."""
    snt, _ = await _puantajli_santiye(db_session, project_factory, olusturan, "PT-4")
    await d.bordro_donemi(db_session, 2026, 2, PayrollPeriodStatus.paid)
    await d.bordro_donemi(db_session, 2025, 3, PayrollPeriodStatus.paid)  # farklı yıl, aynı ay

    assert (await sil_genel(client, sistem, "site", snt.id)).status_code == 204


async def test_engellenen_silme_denetime_satir_yazmaz(
    client, db_session, sistem, project_factory, olusturan
) -> None:
    snt, _ = await _puantajli_santiye(db_session, project_factory, olusturan, "PT-5")
    await d.bordro_donemi(db_session, 2026, 3, PayrollPeriodStatus.approved)

    await _silme_engellendi(client, sistem, "site", snt.id)

    satirlar = (
        (await db_session.execute(select(AuditLog).where(AuditLog.action == AuditAction.delete)))
        .scalars()
        .all()
    )
    assert satirlar == []
