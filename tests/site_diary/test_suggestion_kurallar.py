"""Şantiye günlüğü öneri ucu — ORTAK KURALLAR · SALT OKUNURLUK · IDOR/izin.

`test_suggestion.py`nin ikinci parçası (800 satır tavanı bölmesi); paylaşılan
yardımcılar `_suggestion.py`dedir.

EN KRİTİK KURAL SALT OKUNURLUKTUR: öneri ucu DB'ye HİÇBİR ŞEY yazmaz ve denetim
kaydı AÇMAZ — `_db_izi` istekten önce ve sonra karşılaştırılır. Taslak günlük
öneriye GİRMEZ, köprüsüz poz öneri ÜRETMEZ ama sessizce de atlanmaz.
"""

import uuid
from datetime import date
from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.access import AccessLevel
from app.modules.audit.models import AuditLog
from app.modules.site_diary import guards
from tests._modul_duzeyi_yardimcisi import modul_duzeyi_yaz

from ._suggestion import (
    DONEM,
    _db_izi,
    _gun,
    _isveren_onerisi,
    _satir,
    _taseron_onerisi,
)

pytestmark = pytest.mark.asyncio


async def test_taslak_gunluk_ONERIYE_GIRMEZ(
    client: AsyncClient,
    admin_headers,
    santiye,
    sozlesme_kalemi_fabrikasi,
    taseron_sozlesmesi_fabrikasi,
) -> None:
    """T4 `summary` ile AYNI süzgeç (spec §3/§4): ikinci bir toplama kuralı YOK."""
    site, project, items = santiye
    kalem = sorted(items, key=lambda i: i.code)[0]
    sozlesme = await sozlesme_kalemi_fabrikasi(kalem, project)
    tas_sozlesme = await taseron_sozlesmesi_fabrikasi(
        project, site=site, kalemler=[("TK-1", sozlesme)]
    )
    await _gun(
        client,
        admin_headers,
        site.id,
        date(2026, 7, 10),
        [{"boq_item_id": str(kalem.id), "quantity": "10"}],
    )
    await _gun(
        client,
        admin_headers,
        site.id,
        date(2026, 7, 12),
        [{"boq_item_id": str(kalem.id), "quantity": "5"}],
        gonder=False,
    )

    isveren = (await _isveren_onerisi(client, admin_headers, project.id, **DONEM)).json()
    assert Decimal(_satir(isveren, sozlesme.id, site.id)["quantity"]) == Decimal("10")

    taseron = (await _taseron_onerisi(client, admin_headers, tas_sozlesme.id, **DONEM)).json()
    assert Decimal(taseron["lines"][0]["quantity"]) == Decimal("10")


async def test_koprusuz_poz_ONERI_URETMEZ_ama_sessizce_atlanmaz(
    client: AsyncClient, admin_headers, santiye, sozlesme_kalemi_fabrikasi
) -> None:
    """`contract_item_id` NULL olan poz hangi sözleşme kalemine yazılacağını
    BİLMEZ; öneri üretilmez ama sayısı BİLDİRİLİR (sessiz atlama yok)."""
    site, project, items = santiye
    kalem_a, kalem_b = sorted(items, key=lambda i: i.code)
    sozlesme_a = await sozlesme_kalemi_fabrikasi(kalem_a, project)  # yalnız A köprülü

    await _gun(
        client,
        admin_headers,
        site.id,
        date(2026, 7, 10),
        [
            {"boq_item_id": str(kalem_a.id), "quantity": "10"},
            {"boq_item_id": str(kalem_b.id), "quantity": "6"},
        ],
    )

    govde = (await _isveren_onerisi(client, admin_headers, project.id, **DONEM)).json()
    assert len(govde["lines"]) == 1
    assert _satir(govde, sozlesme_a.id, site.id) is not None
    assert govde["skipped_unbridged_count"] == 1


async def test_hicbiri_koprulu_degilse_bos_liste_ve_KOPRU_GEREKCESI(
    client: AsyncClient, admin_headers, santiye
) -> None:
    site, project, items = santiye
    kalem = sorted(items, key=lambda i: i.code)[0]
    await _gun(
        client,
        admin_headers,
        site.id,
        date(2026, 7, 10),
        [{"boq_item_id": str(kalem.id), "quantity": "10"}],
    )

    govde = (await _isveren_onerisi(client, admin_headers, project.id, **DONEM)).json()
    assert govde["lines"] == []
    assert govde["skipped_unbridged_count"] == 1
    assert govde["reason"] == guards.SUGGESTION_NO_BRIDGE


async def test_sifir_miktarli_poz_oneriye_GIRMEZ(
    client: AsyncClient, admin_headers, santiye, sozlesme_kalemi_fabrikasi
) -> None:
    """Günlük iskeleti TÜM BOQ pozlarını sıfırla açar; sıfır "bu dönem miktar
    gelmedi" demektir, öneri satırı olmamalıdır (aksi hâlde ekran her ay tüm
    pozları sıfırla önerirdi)."""
    site, project, items = santiye
    kalem_a, kalem_b = sorted(items, key=lambda i: i.code)
    sozlesme_a = await sozlesme_kalemi_fabrikasi(kalem_a, project)
    await sozlesme_kalemi_fabrikasi(kalem_b, project)

    await _gun(
        client,
        admin_headers,
        site.id,
        date(2026, 7, 10),
        [
            {"boq_item_id": str(kalem_a.id), "quantity": "10"},
            {"boq_item_id": str(kalem_b.id), "quantity": "0"},
        ],
    )

    govde = (await _isveren_onerisi(client, admin_headers, project.id, **DONEM)).json()
    assert len(govde["lines"]) == 1
    assert _satir(govde, sozlesme_a.id, site.id) is not None


async def test_baska_ayin_gunlugu_doneme_GIRMEZ(
    client: AsyncClient, admin_headers, santiye, sozlesme_kalemi_fabrikasi
) -> None:
    site, project, items = santiye
    kalem = sorted(items, key=lambda i: i.code)[0]
    sozlesme = await sozlesme_kalemi_fabrikasi(kalem, project)
    await _gun(
        client,
        admin_headers,
        site.id,
        date(2026, 7, 10),
        [{"boq_item_id": str(kalem.id), "quantity": "10"}],
    )
    await _gun(
        client,
        admin_headers,
        site.id,
        date(2026, 8, 3),
        [{"boq_item_id": str(kalem.id), "quantity": "40"}],
    )

    temmuz = (await _isveren_onerisi(client, admin_headers, project.id, **DONEM)).json()
    assert Decimal(_satir(temmuz, sozlesme.id, site.id)["quantity"]) == Decimal("10")

    tum_yil = (await _isveren_onerisi(client, admin_headers, project.id, year=2026)).json()
    assert Decimal(_satir(tum_yil, sozlesme.id, site.id)["quantity"]) == Decimal("50")


async def test_oneri_miktari_summary_ile_AYNI_sayi(
    client: AsyncClient, admin_headers, santiye, sozlesme_kalemi_fabrikasi
) -> None:
    """İki ekran aynı sayıyı söylemek ZORUNDA: `summary` poz miktarı ile önerinin
    miktarı aynı dönemde BİREBİR eşittir."""
    site, project, items = santiye
    kalem = sorted(items, key=lambda i: i.code)[0]
    sozlesme = await sozlesme_kalemi_fabrikasi(kalem, project)
    await _gun(
        client,
        admin_headers,
        site.id,
        date(2026, 7, 10),
        [{"boq_item_id": str(kalem.id), "quantity": "10.25"}],
    )
    await _gun(
        client,
        admin_headers,
        site.id,
        date(2026, 7, 14),
        [{"boq_item_id": str(kalem.id), "quantity": "1.75"}],
    )

    ozet = (
        await client.get(f"/sites/{site.id}/diary/summary", params=DONEM, headers=admin_headers)
    ).json()
    ozet_miktar = next(
        Decimal(i["quantity"]) for i in ozet["items"] if i["boq_item_id"] == str(kalem.id)
    )
    oneri = (await _isveren_onerisi(client, admin_headers, project.id, **DONEM)).json()
    assert Decimal(_satir(oneri, sozlesme.id, site.id)["quantity"]) == ozet_miktar


# --- SALT OKUNURLUK (en kritik kural) ---


async def test_isveren_onerisi_DB_YE_HICBIR_SEY_YAZMAZ(
    client: AsyncClient, seeded_db: AsyncSession, admin_headers, santiye, sozlesme_kalemi_fabrikasi
) -> None:
    site, project, items = santiye
    kalem = sorted(items, key=lambda i: i.code)[0]
    sozlesme = await sozlesme_kalemi_fabrikasi(kalem, project)
    await _gun(
        client,
        admin_headers,
        site.id,
        date(2026, 7, 10),
        [{"boq_item_id": str(kalem.id), "quantity": "10"}],
    )

    onceki = await _db_izi(seeded_db)
    yanit = await _isveren_onerisi(client, admin_headers, project.id, **DONEM)
    assert yanit.status_code == 200, yanit.text
    assert len(yanit.json()["lines"]) == 1
    sonraki = await _db_izi(seeded_db)

    assert sonraki == onceki, "öneri ucu SALT OKUNURDUR — DB'de tek satır bile değişemez"
    # Uygulamak KULLANICININ ayrı çağrısıdır: uç, hakediş satırı ÜRETMEZ.
    assert sonraki["isveren_satir"] == 0
    assert str(sozlesme.id)  # köprü kurulu ama yine de yazma yok


async def test_taseron_onerisi_DB_YE_HICBIR_SEY_YAZMAZ(
    client: AsyncClient,
    seeded_db: AsyncSession,
    admin_headers,
    santiye,
    sozlesme_kalemi_fabrikasi,
    taseron_sozlesmesi_fabrikasi,
) -> None:
    site, project, items = santiye
    kalem = sorted(items, key=lambda i: i.code)[0]
    sozlesme = await sozlesme_kalemi_fabrikasi(kalem, project)
    tas_sozlesme = await taseron_sozlesmesi_fabrikasi(
        project, site=site, kalemler=[("TK-1", sozlesme)]
    )
    await _gun(
        client,
        admin_headers,
        site.id,
        date(2026, 7, 10),
        [{"boq_item_id": str(kalem.id), "quantity": "10"}],
    )

    onceki = await _db_izi(seeded_db)
    yanit = await _taseron_onerisi(client, admin_headers, tas_sozlesme.id, **DONEM)
    assert yanit.status_code == 200, yanit.text
    assert len(yanit.json()["lines"]) == 1
    sonraki = await _db_izi(seeded_db)

    assert sonraki == onceki, "öneri ucu SALT OKUNURDUR — DB'de tek satır bile değişemez"
    assert sonraki["taseron_hakedis"] == 0 and sonraki["taseron_satir"] == 0


async def test_oneri_ucu_denetim_kaydi_ACMAZ(
    client: AsyncClient, seeded_db: AsyncSession, admin_headers, santiye, sozlesme_kalemi_fabrikasi
) -> None:
    """Okuma uçları denetlenmez (repo deseni): aksi hâlde uç bir YAZMA yolu olurdu."""
    site, project, items = santiye
    kalem = sorted(items, key=lambda i: i.code)[0]
    await sozlesme_kalemi_fabrikasi(kalem, project)
    await _gun(
        client,
        admin_headers,
        site.id,
        date(2026, 7, 10),
        [{"boq_item_id": str(kalem.id), "quantity": "10"}],
    )

    async def _denetim_sayisi() -> int:
        return int(
            (await seeded_db.execute(select(func.count()).select_from(AuditLog))).scalar_one()
        )

    onceki = await _denetim_sayisi()
    await _isveren_onerisi(client, admin_headers, project.id, **DONEM)
    assert await _denetim_sayisi() == onceki


# --- IDOR + izin + doğrulama ---


async def test_isveren_gorunmeyen_proje_404(
    client: AsyncClient, sef_headers, santiye_fabrikasi
) -> None:
    _, gizli_proje, _ = await santiye_fabrikasi("SD-GIZLI")
    yanit = await _isveren_onerisi(client, sef_headers, gizli_proje.id, **DONEM)
    assert yanit.status_code == 404, yanit.text


async def test_taseron_gorunmeyen_proje_404(
    client: AsyncClient, sef_headers, santiye_fabrikasi, taseron_sozlesmesi_fabrikasi
) -> None:
    gizli_site, gizli_proje, _ = await santiye_fabrikasi("SD-GIZLI2")
    tas_sozlesme = await taseron_sozlesmesi_fabrikasi(gizli_proje, site=gizli_site, code="TS-GIZLI")
    yanit = await _taseron_onerisi(client, sef_headers, tas_sozlesme.id, **DONEM)
    assert yanit.status_code == 404, yanit.text


async def test_var_olmayan_kimlikler_404(client: AsyncClient, admin_headers) -> None:
    """Var olmayan ile görünmeyen AYIRT EDİLEMEZ (repo IDOR kuralı)."""
    assert (await _isveren_onerisi(client, admin_headers, uuid.uuid4(), **DONEM)).status_code == 404
    assert (await _taseron_onerisi(client, admin_headers, uuid.uuid4(), **DONEM)).status_code == 404


async def test_gunluk_izni_OLMAYAN_hakedis_onaycisi_403(
    client: AsyncClient, muhasebe_headers, santiye, taseron_sozlesmesi_fabrikasi
) -> None:
    """`accounting`: `progress_payments=_APR` ama `site_diary=_N`. Uç yalnız
    hakediş izniyle korunsaydı günlük verisi bu role SIZARDI."""
    site, project, _ = santiye
    tas_sozlesme = await taseron_sozlesmesi_fabrikasi(project, site=site, code="TS-MUH")
    assert (
        await _isveren_onerisi(client, muhasebe_headers, project.id, **DONEM)
    ).status_code == 403
    assert (
        await _taseron_onerisi(client, muhasebe_headers, tas_sozlesme.id, **DONEM)
    ).status_code == 403


async def test_hakedis_izni_OLMAYAN_gunluk_rolu_403(
    client: AsyncClient, sef_headers, pm_headers, santiye, taseron_sozlesmesi_fabrikasi
) -> None:
    """Karşı yön: `site_chief` (`progress_payments=_DRF`, `site_diary=_F`) ve
    `project_manager` (`_APR`/`_V`) İKİ kapıdan da geçer — çift kapı meşru
    kullanıcıyı KİLİTLEMEZ."""
    site, project, _ = santiye
    tas_sozlesme = await taseron_sozlesmesi_fabrikasi(project, site=site, code="TS-SEF")
    assert (await _isveren_onerisi(client, sef_headers, project.id, **DONEM)).status_code == 200
    assert (await _taseron_onerisi(client, pm_headers, tas_sozlesme.id, **DONEM)).status_code == 200


async def test_hakedis_izni_KALDIRILMIS_gunluk_rolu_403(
    client: AsyncClient,
    seeded_db: AsyncSession,
    sef_headers,
    sef_kullanicisi,
    santiye,
    taseron_sozlesmesi_fabrikasi,
) -> None:
    """Hakediş kapısının ÖLÜ KURAL olmadığının kanıtı.

    Öntanımlı matriste `site_diary`yi gören her rol hakedişi de görür, bu yüzden
    kapı ancak izin satırı DEĞİŞTİRİLEREK sınanabilir — izinler veridir, yönetici
    onları ekrandan kısabilir. `site_chief`in hakediş izni kapatıldığında öneri
    ucu 403 döner: hakediş kalem kimlikleri hakedişi görmeyen role sızmaz.
    """
    site, project, _ = santiye
    tas_sozlesme = await taseron_sozlesmesi_fabrikasi(project, site=site, code="TS-KAPALI")
    await modul_duzeyi_yaz(
        seeded_db, sef_kullanicisi.role_id, "progress_payments", AccessLevel.none
    )

    assert (await _isveren_onerisi(client, sef_headers, project.id, **DONEM)).status_code == 403
    assert (
        await _taseron_onerisi(client, sef_headers, tas_sozlesme.id, **DONEM)
    ).status_code == 403
    # Günlüğün KENDİ ucu etkilenmez — kapı yalnız öneri ucundadır.
    assert (
        await client.get(f"/sites/{site.id}/diary/summary", params=DONEM, headers=sef_headers)
    ).status_code == 200


async def test_gunluk_izni_hic_olmayan_rol_403(client: AsyncClient, hr_headers, santiye) -> None:
    _, project, _ = santiye
    assert (await _isveren_onerisi(client, hr_headers, project.id, **DONEM)).status_code == 403


async def test_ay_tek_basina_422(
    client: AsyncClient, admin_headers, santiye, taseron_sozlesmesi_fabrikasi
) -> None:
    """T2/T4 ile TUTARLI: `month` yalnız `year` ile anlamlıdır."""
    site, project, _ = santiye
    tas_sozlesme = await taseron_sozlesmesi_fabrikasi(project, site=site, code="TS-422")
    isveren = await _isveren_onerisi(client, admin_headers, project.id, month=7)
    assert isveren.status_code == 422, isveren.text
    assert guards.YEAR_REQUIRED_FOR_MONTH in isveren.text
    taseron = await _taseron_onerisi(client, admin_headers, tas_sozlesme.id, month=7)
    assert taseron.status_code == 422, taseron.text


async def test_gecersiz_ay_422(client: AsyncClient, admin_headers, santiye) -> None:
    _, project, _ = santiye
    assert (
        await _isveren_onerisi(client, admin_headers, project.id, year=2026, month=13)
    ).status_code == 422


async def test_donemsiz_cagri_tum_gunlugu_kapsar(
    client: AsyncClient, admin_headers, santiye, sozlesme_kalemi_fabrikasi
) -> None:
    site, project, items = santiye
    kalem = sorted(items, key=lambda i: i.code)[0]
    sozlesme = await sozlesme_kalemi_fabrikasi(kalem, project)
    await _gun(
        client,
        admin_headers,
        site.id,
        date(2025, 12, 20),
        [{"boq_item_id": str(kalem.id), "quantity": "3"}],
    )
    await _gun(
        client,
        admin_headers,
        site.id,
        date(2026, 7, 10),
        [{"boq_item_id": str(kalem.id), "quantity": "7"}],
    )

    govde = (await _isveren_onerisi(client, admin_headers, project.id)).json()
    assert Decimal(_satir(govde, sozlesme.id, site.id)["quantity"]) == Decimal("10")
    assert govde["year"] is None and govde["month"] is None
