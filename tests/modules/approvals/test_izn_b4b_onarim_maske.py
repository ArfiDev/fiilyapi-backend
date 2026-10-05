"""IZN-B4b onarımı — onay kutusu / geçmişi tutarları HASSAS ALAN maskesinden geçer.

Bulgu: `gross_amount`/`net_amount`/`amount_snapshot` etiketsizdi; `tum_tutarlar`/`maliyet_kar`
gizli bir rol taşeron hakediş tutarını kutuda açık görüyordu.

Kural (`ApprovalInboxItem.KATEGORI_COZ`): işveren hakedişi `sozlesme_fiyat`, taşeron hakedişi ve
satınalma talebi `maliyet_kar`. Satır, evrakın PROJESİNDEKİ rolün bayraklarıyla maskelenir
(`project_id`); ekip rolü ana rolden farklı olabilir.
"""

from decimal import Decimal

from sqlalchemy import delete, select

from app.core.sayfalar import HiddenCategory
from app.modules.approvals import service
from app.modules.approvals.models import ApprovalDocumentType, ApprovalRole
from app.modules.roles.models import Role, RoleHiddenField
from tests.modules.approvals.conftest import proje_rolu_ver

_TASERON = ApprovalDocumentType.subcontractor_progress_payment
_ISVEREN = ApprovalDocumentType.progress_payment
_SATINALMA = ApprovalDocumentType.purchase_request


async def _bayrak(session, rol_anahtari: str, kategoriler: set[HiddenCategory]) -> None:
    rol_id = (await session.execute(select(Role.id).where(Role.key == rol_anahtari))).scalar_one()
    await session.execute(delete(RoleHiddenField).where(RoleHiddenField.role_id == rol_id))
    for kategori in kategoriler:
        session.add(RoleHiddenField(role_id=rol_id, category=kategori))
    await session.flush()


async def _zincir(session, tip, belge_id, yaratan):
    return await service.create_chain(
        session,
        document_type=tip,
        document_id=belge_id,
        amount=Decimal("100.00"),
        created_by_user_id=yaratan.id,
    )


async def _uc_tip(seeded_db, aktor_fabrikasi, evrak_fabrikasi, project_factory, rol: str):
    """Aynı aktör üç ayrı projede `rol` proje rolüyle; her projede bir evrak ailesi. Üç ailede de
    SON adım `accounting`tir → `accounting` proje rolü geçmişte üçünü de görür."""
    yaratan = await aktor_fabrikasi("b4b-yaratan@onay.co", full_name="Yaratan")
    projeler = {
        tip: await project_factory(code=f"B4B-{ad}", name=f"Proje {ad}")
        for tip, ad in ((_ISVEREN, "ISV"), (_TASERON, "TSR"), (_SATINALMA, "SAT"))
    }
    aktor = await aktor_fabrikasi(
        "b4b-aktor@onay.co", role_key=rol, projeler=list(projeler.values())
    )
    for tip, proje in projeler.items():
        belge_id, _ = await evrak_fabrikasi(tip, creator=yaratan, project=proje)
        await _zincir(seeded_db, tip, belge_id, yaratan)
    return aktor


async def _gecmis(client, giris, email):
    yanit = await client.get("/approvals/history", headers=await giris(email))
    assert yanit.status_code == 200, yanit.text
    return {s["document_type"]: s for s in yanit.json()["items"]}


async def test_gecmis_tutari_EVRAK_TIPINE_gore_gizlenir(
    client, seeded_db, aktor_fabrikasi, evrak_fabrikasi, project_factory, giris
):
    await _uc_tip(seeded_db, aktor_fabrikasi, evrak_fabrikasi, project_factory, "accounting")

    # İşveren hakedişi `sozlesme_fiyat`: yalnız o gizliyken işveren satırı `null`.
    await _bayrak(seeded_db, "accounting", {HiddenCategory.sozlesme_fiyat})
    satirlar = await _gecmis(client, giris, "b4b-aktor@onay.co")
    assert satirlar[_ISVEREN.value]["gross_amount"] is None
    assert satirlar[_ISVEREN.value]["net_amount"] is None
    assert satirlar[_ISVEREN.value]["amount_snapshot"] is None
    assert satirlar[_TASERON.value]["gross_amount"] is not None
    assert satirlar[_SATINALMA.value]["gross_amount"] is not None

    # Taşeron + satınalma `maliyet_kar`: yalnız onlar `null`, işveren AÇIK (pozitif kontrol).
    await _bayrak(seeded_db, "accounting", {HiddenCategory.maliyet_kar})
    satirlar = await _gecmis(client, giris, "b4b-aktor@onay.co")
    assert satirlar[_ISVEREN.value]["gross_amount"] is not None
    assert satirlar[_TASERON.value]["gross_amount"] is None
    assert satirlar[_TASERON.value]["net_amount"] is None
    assert satirlar[_SATINALMA.value]["gross_amount"] is None

    # `tum_tutarlar`: üç ailenin de tutarı gizli; başlık/alt başlık (metin) AÇIK.
    await _bayrak(seeded_db, "accounting", {HiddenCategory.tum_tutarlar})
    satirlar = await _gecmis(client, giris, "b4b-aktor@onay.co")
    assert all(s["gross_amount"] is None for s in satirlar.values())
    assert all(s["title"] for s in satirlar.values())

    # Bayraksız: üçü de açık.
    await _bayrak(seeded_db, "accounting", set())
    satirlar = await _gecmis(client, giris, "b4b-aktor@onay.co")
    assert all(s["gross_amount"] is not None for s in satirlar.values())


async def test_kutu_tasera_hakedis_tutari_maliyet_kar_gizliyken_NULL(
    client, seeded_db, aktor_fabrikasi, evrak_fabrikasi, giris
):
    """Bulgunun kendisi: `site_chief` taşeron hakediş tutarını kutuda açık görüyordu."""
    yaratan = await aktor_fabrikasi("b4b-k-yaratan@onay.co")
    await aktor_fabrikasi(
        "b4b-k-sef@onay.co", role_key="site_chief", approval_roles=[ApprovalRole.site_chief]
    )
    belge_id, _ = await evrak_fabrikasi(_TASERON, creator=yaratan)
    await _zincir(seeded_db, _TASERON, belge_id, yaratan)
    basliklar = await giris("b4b-k-sef@onay.co")

    await _bayrak(seeded_db, "site_chief", {HiddenCategory.maliyet_kar})
    (satir,) = (await client.get("/approvals", headers=basliklar)).json()["items"]
    assert satir["gross_amount"] is None and satir["net_amount"] is None
    assert satir["amount_snapshot"] is None
    assert satir["title"]  # metin gizlenmez

    # İlgisiz kategori gizliyken AÇIK.
    await _bayrak(seeded_db, "site_chief", {HiddenCategory.sozlesme_fiyat})
    (satir,) = (await client.get("/approvals", headers=basliklar)).json()["items"]
    assert satir["gross_amount"] == "100000.00"
    assert satir["net_amount"] == "115000.00"


async def test_gecmis_PROJE_matrisi_A_acik_B_gizli(
    client, seeded_db, aktor_fabrikasi, evrak_fabrikasi, project_factory, giris
):
    """Satır evrakın PROJESİNDEKİ rolle maskelenir: proje A'da `accounting` (açık), B'de
    `project_manager` (`maliyet_kar` gizli). Proje çözümü olmasa birleşim (gizli) A'yı da
    gizlerdi — `project_id` düşerse bu test KIRMIZI olmalıdır."""
    yaratan = await aktor_fabrikasi("b4b-m-yaratan@onay.co")
    proje_a = await project_factory(code="B4B-MA", name="Açık Proje")
    proje_b = await project_factory(code="B4B-MB", name="Gizli Proje")
    aktor = await aktor_fabrikasi("b4b-m-aktor@onay.co", role_key="accounting", projeler=[proje_a])
    await proje_rolu_ver(seeded_db, aktor, proje_b, "project_manager")
    a_id, _ = await evrak_fabrikasi(_TASERON, creator=yaratan, project=proje_a)
    b_id, _ = await evrak_fabrikasi(_TASERON, creator=yaratan, project=proje_b)
    await _zincir(seeded_db, _TASERON, a_id, yaratan)
    await _zincir(seeded_db, _TASERON, b_id, yaratan)
    await _bayrak(seeded_db, "accounting", set())
    await _bayrak(seeded_db, "project_manager", {HiddenCategory.maliyet_kar})

    yanit = await client.get("/approvals/history", headers=await giris("b4b-m-aktor@onay.co"))
    assert yanit.status_code == 200, yanit.text
    satirlar = {s["document_id"]: s for s in yanit.json()["items"]}
    assert satirlar[str(a_id)]["project_id"] == str(proje_a.id)
    assert satirlar[str(a_id)]["gross_amount"] == "100000.00"  # A: AÇIK
    assert satirlar[str(b_id)]["gross_amount"] is None  # B: gizli
