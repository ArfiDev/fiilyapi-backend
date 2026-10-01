# ruff: noqa: F811
"""GKS-B1 — `GET /sites/{id}/diary/skeleton` (kaydedilmemiş iskelet önizlemesi).

Kapı: `site_diary` görüntüleme (BOQ izni İSTENMEZ). Şantiye görünürlüğü 404, başka
şantiyenin/olmayan bölüm 422, `entry_date` zorunlu. Satır değerleri detay ucuyla AYNI hesaptan
gelir; o tarihte günlük varsa 409 değil `existing_entry_id`. DSC: kısıtlıya yalnız görünür kalem.
"""

import uuid
from datetime import date
from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.boq.models import BoqItemSectionAllocation
from app.modules.projects.models import Project
from app.modules.site_diary import repository, skeleton
from app.modules.site_diary.models import DiaryStatus
from app.modules.site_diary.schemas import SiteDiaryLineRead, SiteDiarySkeletonLine
from app.modules.sites.models import Section
from tests._disiplin_dunyasi import Dunya
from tests.discipline_scope.conftest import civil, dunya, elek  # noqa: F401
from tests.site_diary._gks_b1 import GUN, anahtarlar, beklenen, olustur, onizleme
from tests.site_diary.conftest import (
    KARISIK_BOLUMSUZ,
    KARISIK_S1,
    KARISIK_S2,
    KarisikSantiye,
)

pytestmark = pytest.mark.asyncio

YAZANLAR = True  # `dunya` fikstürü: yazan aktörleri de kurar


# --- Şekil ---


_SEMA_YOK_SAYILAN = {"description", "title"}


def _sema_alanlari(model) -> dict[str, dict]:  # noqa: ANN001
    """Alan → tip ayrıntısı (`description`/`title` hariç): tipe KÖR olmayan karşılaştırma."""
    return {
        ad: {k: v for k, v in alan.items() if k not in _SEMA_YOK_SAYILAN}
        for ad, alan in model.model_json_schema()["properties"].items()
    }


async def test_satir_semasi_detay_satirinin_kimliksiz_ikizidir() -> None:
    """Alan adı, TİP ve `required` kümesi AYNI: skeleton satırı = `SiteDiaryLineRead` − `id`
    (frontend aynı ağaç kodu)."""
    detay = _sema_alanlari(SiteDiaryLineRead)
    detay.pop("id")
    assert _sema_alanlari(SiteDiarySkeletonLine) == detay
    assert set(SiteDiarySkeletonLine.model_json_schema()["required"]) == set(
        SiteDiaryLineRead.model_json_schema()["required"]
    ) - {"id"}


async def test_saf_planli_yanittaki_planned_quantity_ile_ESIT(
    client: AsyncClient,
    admin_headers,
    karisik_santiye: KarisikSantiye,
    seeded_db: AsyncSession,
) -> None:
    """`SkeletonLine.planned` (saf kuralın hesabı) API'de kullanılmaz; yanıt `read.planned_quantity`
    değerini taşır. İkinci hesap sessizce ayrışmasın: karışık şantiyede satır satır eşit."""
    ks = karisik_santiye
    items = await repository.list_boq_items(seeded_db, ks.site.id)
    saf = skeleton.skeleton_keys(
        [skeleton.SkeletonItem(i.id, i.quantity) for i in items],
        skeleton.group_allocations(
            await repository.allocations_for_items(seeded_db, [i.id for i in items])
        ),
        None,
        await repository.section_order(seeded_db, ks.site.id),
    )
    yanit = (await onizleme(client, admin_headers, ks.site.id)).json()["lines"]
    assert saf and len(saf) == len(yanit)
    for anahtar, satir in zip(saf, yanit, strict=True):
        assert satir["boq_item_id"] == str(anahtar.item_id)
        assert satir["section_id"] == (str(anahtar.section_id) if anahtar.section_id else None)
        assert anahtar.planned == Decimal(satir["planned_quantity"])


async def test_kalem_sirasi_PG_harmanlamasindan_gelir_onizleme_esittir_detay(
    client: AsyncClient,
    admin_headers,
    seeded_db: AsyncSession,
    santiye_fabrikasi,
    proje: Project,
) -> None:
    """Kod-noktası ve PG harmanlaması ayrışan kodlar ("a.01" / "Ç.01" / "D.01", "01-002" /
    "01.001"): TAM tahsisli kalem bölüm satırlarına açılır; önizleme sırası == POST sonrası
    detay sırası (detay Python'da `code` ile yeniden sıralamaz)."""
    kodlar = ["a.01", "Ç.01", "D.01", "01-002", "01.001"]
    spec = [(k, Decimal("100.000"), Decimal("10.00")) for k in kodlar]
    site, _, items = await santiye_fabrikasi("HRM", project=proje, item_specs=spec)
    s1 = Section(
        id=uuid.UUID(int=0x6B52 << 100 | 2), site_id=site.id, code="S1", name="S1", sort_order=0
    )
    s2 = Section(
        id=uuid.UUID(int=0x6B52 << 100 | 1), site_id=site.id, code="S2", name="S2", sort_order=1
    )
    seeded_db.add_all([s1, s2])
    await seeded_db.flush()
    for item in items:
        if item.code in ("D.01", "01-002"):  # TAM tahsisli: iki bölüm satırı
            for bolum in (s1, s2):
                seeded_db.add(
                    BoqItemSectionAllocation(
                        boq_item_id=item.id, section_id=bolum.id, quantity=Decimal("50")
                    )
                )
    await seeded_db.flush()

    onceki = (await onizleme(client, admin_headers, site.id)).json()["lines"]
    kayit = await olustur(client, admin_headers, site.id)
    assert kayit.status_code == 201, kayit.text
    sonraki = [{k: v for k, v in s.items() if k != "id"} for s in kayit.json()["lines"]]

    assert len(onceki) == 7  # 3 bölümsüz + 2 × 2 bölüm satırı
    assert onceki == sonraki
    sirali_kodlar = list(dict.fromkeys(s["code"] for s in onceki))
    assert sirali_kodlar != sorted(kodlar), "dava harmanlama/kod-noktası ayrışmasını taşımalı"
    d01 = [s["section_name"] for s in onceki if s["code"] == "D.01"]
    assert d01 == ["S1", "S2"]


async def test_onizleme_degerleri_kayitli_detayla_AYNI_hesaptan_gelir(
    client: AsyncClient, admin_headers, karisik_santiye: KarisikSantiye
) -> None:
    """Önizleme satırı == aynı günün POST'unun detay satırı (id hariç, alan alan, sıra dahil)."""
    ks = karisik_santiye
    yanit = await onizleme(client, admin_headers, ks.site.id)
    assert yanit.status_code == 200, yanit.text
    onceki = yanit.json()["lines"]

    kayit = await olustur(client, admin_headers, ks.site.id)
    assert kayit.status_code == 201, kayit.text
    sonraki = [{k: v for k, v in satir.items() if k != "id"} for satir in kayit.json()["lines"]]

    assert onceki == sonraki
    assert yanit.json()["lines_total"] == kayit.json()["lines_total"]
    assert all(Decimal(s["quantity"]) == 0 for s in onceki)


async def test_kumulatif_kalan_gunun_oncesindeki_gonderilmislerden_gelir(
    client: AsyncClient, admin_headers, karisik_santiye: KarisikSantiye, gunluk_api
) -> None:
    """Önceki GÖNDERİLMİŞ gün 10 birim girdiyse, sonraki günün önizlemesi kümülatifi 10 taşır
    (detay ucuyla aynı `leaf_cumulative_before`); aynı/sonraki tarih etkilemez."""
    ks = karisik_santiye
    await gunluk_api(
        admin_headers, ks.site.id, date(2026, 7, 10), [_s(ks, "02", "10", "S1")], gonder=True
    )

    yanit = await onizleme(client, admin_headers, ks.site.id, bolum=ks.s1)

    satir = next(s for s in yanit.json()["lines"] if s["code"] == "02")
    assert Decimal(satir["leaf_cumulative_quantity"]) == Decimal("10")
    assert Decimal(satir["remaining_quantity"]) == Decimal("30")
    assert Decimal(satir["cumulative_quantity"]) == Decimal("10")
    erken = await onizleme(client, admin_headers, ks.site.id, date(2026, 7, 5), ks.s1)
    erken_satir = next(s for s in erken.json()["lines"] if s["code"] == "02")
    assert Decimal(erken_satir["leaf_cumulative_quantity"]) == 0


def _s(ks, kod, miktar, bolum):
    from tests.site_diary._gks_b1 import satir

    return satir(ks, kod, miktar, bolum)


# --- Kural (A) uç düzeyinde ---


async def test_bolum_secili_degil_bolumsuz_ve_tam_tahsisli_kalemler_bolumlu(
    client: AsyncClient, admin_headers, karisik_santiye: KarisikSantiye
) -> None:
    yanit = await onizleme(client, admin_headers, karisik_santiye.site.id)
    assert yanit.status_code == 200, yanit.text
    assert anahtarlar(yanit.json()["lines"]) == beklenen(KARISIK_BOLUMSUZ)
    assert yanit.json()["section_id"] is None


@pytest.mark.parametrize(("bolum", "beklenen_satirlar"), [("s1", KARISIK_S1), ("s2", KARISIK_S2)])
async def test_bolum_secili_yalniz_o_bolume_tahsisli_kalemler(
    client: AsyncClient, admin_headers, karisik_santiye: KarisikSantiye, bolum, beklenen_satirlar
) -> None:
    ks = karisik_santiye
    secili = getattr(ks, bolum)
    yanit = await onizleme(client, admin_headers, ks.site.id, bolum=secili)
    assert yanit.status_code == 200, yanit.text
    assert anahtarlar(yanit.json()["lines"]) == beklenen(beklenen_satirlar)
    assert yanit.json()["section_name"] == secili.name
    assert all(s["section_id"] == str(secili.id) for s in yanit.json()["lines"])


# --- İzin / kapsam ---


async def test_saha_muhendisi_boq_izni_olmadan_200(
    client: AsyncClient, saha_headers, karisik_santiye: KarisikSantiye
) -> None:
    """`field_engineer`: `site_diary` var, `boq` YOK. Kontrol: `GET /sites/{id}/boq` 403."""
    ks = karisik_santiye
    boq = await client.get(f"/sites/{ks.site.id}/boq", headers=saha_headers)
    assert boq.status_code == 403, boq.text

    yanit = await onizleme(client, saha_headers, ks.site.id)

    assert yanit.status_code == 200, yanit.text
    assert anahtarlar(yanit.json()["lines"]) == beklenen(KARISIK_BOLUMSUZ)


async def test_pm_view_200_ik_izinsiz_403(
    client: AsyncClient, pm_headers, hr_headers, karisik_santiye: KarisikSantiye
) -> None:
    ks = karisik_santiye
    assert (await onizleme(client, pm_headers, ks.site.id)).status_code == 200
    assert (await onizleme(client, hr_headers, ks.site.id)).status_code == 403


async def test_kimliksiz_istek_401(client: AsyncClient, karisik_santiye: KarisikSantiye) -> None:
    yanit = await onizleme(client, {}, karisik_santiye.site.id)
    assert yanit.status_code == 401


async def test_gorunmeyen_ve_olmayan_santiye_AYNI_404(
    client: AsyncClient, sef_headers, gorunmeyen_santiye
) -> None:
    gorunmeyen = await onizleme(client, sef_headers, gorunmeyen_santiye.id)
    olmayan = await onizleme(client, sef_headers, uuid.uuid4())
    assert gorunmeyen.status_code == 404
    assert (gorunmeyen.status_code, gorunmeyen.json()) == (olmayan.status_code, olmayan.json())


async def test_baska_santiyenin_ve_olmayan_bolum_AYNI_422(
    client: AsyncClient,
    admin_headers,
    karisik_santiye: KarisikSantiye,
    santiye_fabrikasi,
    seeded_db: AsyncSession,
) -> None:
    from app.modules.sites.models import Section

    ks = karisik_santiye
    diger, _, _ = await santiye_fabrikasi("GKS-D")
    yabanci = Section(site_id=diger.id, code="Y", name="Yabancı")
    seeded_db.add(yabanci)
    await seeded_db.flush()

    baska = await onizleme(client, admin_headers, ks.site.id, bolum=yabanci)
    olmayan = await client.get(
        f"/sites/{ks.site.id}/diary/skeleton",
        params={"entry_date": GUN.isoformat(), "section_id": str(uuid.uuid4())},
        headers=admin_headers,
    )

    assert baska.status_code == 422
    assert (baska.status_code, baska.json()) == (olmayan.status_code, olmayan.json())


async def test_entry_date_zorunlu_422(
    client: AsyncClient, admin_headers, karisik_santiye: KarisikSantiye
) -> None:
    yanit = await client.get(
        f"/sites/{karisik_santiye.site.id}/diary/skeleton", headers=admin_headers
    )
    assert yanit.status_code == 422


# --- Gün kilidi ---


async def test_gun_kilitliyse_locked_ve_rapor_tarihi_basilir(
    client: AsyncClient, admin_headers, karisik_santiye: KarisikSantiye, port
) -> None:
    ks = karisik_santiye
    port.kilitle(ks.site.id, GUN)

    kilitli = await onizleme(client, admin_headers, ks.site.id, GUN)
    serbest = await onizleme(client, admin_headers, ks.site.id, date(2026, 7, 21))

    assert kilitli.status_code == 200, kilitli.text  # önizleme yazmaz: kilit 409 DEĞİL bilgidir
    assert kilitli.json()["locked"] is True
    assert serbest.json()["locked"] is False and serbest.json()["lock_report_date"] is None


# --- Var olan günlük ---


async def test_o_tarihte_gunluk_varsa_409_degil_existing_entry_id_ve_ayni_iskelet(
    client: AsyncClient, admin_headers, karisik_santiye: KarisikSantiye
) -> None:
    ks = karisik_santiye
    bos = await onizleme(client, admin_headers, ks.site.id)
    assert bos.json()["existing_entry_id"] is None

    kayit = await olustur(client, admin_headers, ks.site.id)
    var = await onizleme(client, admin_headers, ks.site.id)

    assert var.status_code == 200, var.text
    assert var.json()["existing_entry_id"] == kayit.json()["id"]
    assert var.json()["lines"] == bos.json()["lines"]


async def test_gonderilmis_gunluk_de_existing_entry_id_dondurur(
    client: AsyncClient,
    admin_headers,
    karisik_santiye: KarisikSantiye,
    gunluk_fabrikasi,
    admin_kullanicisi,
) -> None:
    ks = karisik_santiye
    entry = await gunluk_fabrikasi(
        ks.site, admin_kullanicisi, entry_date=GUN, status=DiaryStatus.submitted
    )
    yanit = await onizleme(client, admin_headers, ks.site.id)
    assert yanit.json()["existing_entry_id"] == str(entry.id)


# --- DSC ---


async def test_kisitli_kullaniciya_yalniz_gorunur_kalemler(
    client: AsyncClient,
    dunya: Dunya,
    civil,
    elek,
) -> None:
    """Dünya: I1 (civil; S1 60 + S2 30 / 100 → KISMEN), I2 (elek; S1 50/50 → TAM), I3 (eşlemesiz;
    S2 20/40 → KISMEN). Atamasız üçünü görür; civil yalnız I1; elek yalnız I2; eşlemesiz I3
    kısıtlıya HİÇ görünmez (fail-closed)."""
    gun = date(2026, 5, 20)
    site = dunya.santiye
    atamasiz = dunya.baslik["atamasiz"]

    hepsi = await onizleme(client, atamasiz, site.id, gun)
    kodlar_civil = await onizleme(client, civil, site.id, gun)
    kodlar_elek = await onizleme(client, elek, site.id, gun)

    assert [(s["code"], s["section_id"]) for s in hepsi.json()["lines"]] == [
        ("01.001", None),
        ("02.001", str(dunya.s1.id)),  # TAM tahsisli: Bölümsüz yok, bölüm satırı
        ("03.001", None),
    ]
    assert [(s["code"], Decimal(s["planned_quantity"])) for s in kodlar_civil.json()["lines"]] == [
        ("01.001", Decimal(10))
    ]
    assert [(s["code"], s["section_id"]) for s in kodlar_elek.json()["lines"]] == [
        ("02.001", str(dunya.s1.id))
    ]
    assert Decimal(kodlar_civil.json()["lines_total"]) == 0
