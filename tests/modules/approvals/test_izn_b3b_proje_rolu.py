"""IZN-B3b (K1) — onay zinciri ADIM SAHİBİ = belgenin projesinde o role atanmış kişi.

Adım rolü rol ANAHTARIYLA kalır; adımı, belgenin PROJESİNDE `project_members` ile o role atanmış
kişi ya da "Tüm projeler" + ANA rolü o rol olan kişi onaylar (`approvals/step_owner.py`, tek
koşul: kapı · gelen kutusu · geçmiş · kilitli karar). Bu dosya SÖZLEŞMENİN davranış kanıtıdır:

* A'da PM olan onaylar, B'de olmayan onaylayamaz (kapı + kilitli karar);
* "Tüm projeler" + ana rol her projede onaylar;
* gelen kutusu proje başına: aynı kişi A'da şef, B'de saha mühendisi ise yalnız A'nın adımı düşer;
* rol değişimi bekleyen adımı götürür;
* BOŞ adım rolü gönderimi 409 ile ENGELLER: zincir açılmaz, evrak durumu değişmez; `all_projects`
  + ana rol sahibi varsa geçer; üç evrak ailesi için.
"""

from decimal import Decimal

import pytest
from sqlalchemy import func, select

from app.core.errors import ApprovalNotAllowedError, ConflictError
from app.modules.approvals import guards, service
from app.modules.approvals.models import ApprovalChain, ApprovalDocumentType
from app.modules.procurement.models import PurchaseRequest, PurchaseRequestStatus
from app.modules.progress_payments.models import ProgressPayment, ProgressPaymentStatus
from app.modules.subcontractor_progress_payments.models import (
    SubcontractorPaymentStatus,
    SubcontractorProgressPayment,
)
from tests.modules.approvals.conftest import adim_durumlari, proje_rolu_ver

_TASERON = ApprovalDocumentType.subcontractor_progress_payment
_ISVEREN = ApprovalDocumentType.progress_payment
_SATINALMA = ApprovalDocumentType.purchase_request

_TASERON_YOL = "/subcontractor-progress-payments"
_ISVEREN_YOL = "/progress-payments"
_SATINALMA_YOL = "/purchase-requests"

_MODUL_KAPISI = "Bu işlem için yetkiniz yok"


async def _zincir(seeded_db, tip, document_id, yaratan, amount=Decimal("100.00")):
    return await service.create_chain(
        seeded_db,
        document_type=tip,
        document_id=document_id,
        amount=amount,
        created_by_user_id=yaratan.id,
    )


# --------------------------------------------------------------------------- #
# 1. A'da şef olan onaylar, B'de olmayan onaylayamaz
# --------------------------------------------------------------------------- #


async def test_A_da_sef_olan_A_nin_adimini_onaylar_B_nin_adimini_ONAYLAYAMAZ(
    client, seeded_db, aktor_fabrikasi, evrak_fabrikasi, giris
):
    """Kule'de Şantiye Şefi olan kişi Kule hakedişinin şef adımını onaylar; Köprü'de başka rolde
    (ya da rolsüz) olduğu için Köprü'nünkini ONAYLAYAMAZ. Kapı (HTTP) ve kilitli karar (servis)
    ikisi de aynı sonucu verir; Köprü'nün adımı karara bağlanmaz."""
    yaratan = await aktor_fabrikasi("b3b-1-yaratan@b3b.co")
    kule_id, kule = await evrak_fabrikasi(_TASERON, creator=yaratan)
    kopru_id, kopru = await evrak_fabrikasi(_TASERON, creator=yaratan)
    kule_zincir = await _zincir(seeded_db, _TASERON, kule_id, yaratan)
    kopru_zincir = await _zincir(seeded_db, _TASERON, kopru_id, yaratan)
    kisi = await aktor_fabrikasi("b3b-1-sef@b3b.co", role_key="hr_manager", tum_projeler=False)
    await proje_rolu_ver(seeded_db, kisi, kule, "site_chief")
    await proje_rolu_ver(seeded_db, kisi, kopru, "field_engineer")
    basliklar = await giris("b3b-1-sef@b3b.co")

    kopru_yanit = await client.post(f"{_TASERON_YOL}/{kopru_id}/approve", headers=basliklar)
    assert kopru_yanit.status_code == 403, kopru_yanit.text
    assert kopru_yanit.json()["detail"] == _MODUL_KAPISI
    assert await adim_durumlari(seeded_db, kopru_zincir.id) == [False, False, False]
    with pytest.raises(ApprovalNotAllowedError) as hata:
        await service.approve_next_step(
            seeded_db, actor=kisi, document_type=_TASERON, document_id=kopru_id
        )
    assert str(hata.value) == guards.APPROVAL_ROLE_MISSING

    kule_yanit = await client.post(f"{_TASERON_YOL}/{kule_id}/approve", headers=basliklar)
    assert kule_yanit.status_code == 200, kule_yanit.text
    assert await adim_durumlari(seeded_db, kule_zincir.id) == [True, False, False]


# --------------------------------------------------------------------------- #
# 2. "Tüm projeler" + ana rol
# --------------------------------------------------------------------------- #


async def test_TUM_PROJELER_ana_rol_her_projede_kendi_adimini_onaylar(
    client, seeded_db, aktor_fabrikasi, evrak_fabrikasi, giris
):
    """Ekip satırı OLMAYAN ("Tüm projeler") kişi, ana rolü adım rolüyse HER projede onaylar.

    Mutasyon (a): `step_owner_clause` ana-rol dalı silinirse bu test kırmızıya döner."""
    yaratan = await aktor_fabrikasi("b3b-2-yaratan@b3b.co")
    kisi = await aktor_fabrikasi("b3b-2-sef@b3b.co", role_key="site_chief")  # all_projects=True
    assert kisi.all_projects is True
    basliklar = await giris("b3b-2-sef@b3b.co")
    belgeler = []
    for _ in range(2):
        document_id, _proje = await evrak_fabrikasi(_TASERON, creator=yaratan)
        belgeler.append((document_id, await _zincir(seeded_db, _TASERON, document_id, yaratan)))

    for document_id, zincir in belgeler:
        yanit = await client.post(f"{_TASERON_YOL}/{document_id}/approve", headers=basliklar)
        assert yanit.status_code == 200, yanit.text
        assert await adim_durumlari(seeded_db, zincir.id) == [True, False, False]


async def test_TUM_PROJELER_olmayan_ana_rol_adimi_onaylatmaz(
    client, seeded_db, aktor_fabrikasi, evrak_fabrikasi, giris
):
    """Ana rolü adım rolü olan ama "Tüm projeler" DEĞİL ve o projede ekipte olmayan kişi: 403
    (rol atama = proje rolü; ana rol YALNIZ "Tüm projeler"de sayılır)."""
    yaratan = await aktor_fabrikasi("b3b-2b-yaratan@b3b.co")
    await aktor_fabrikasi("b3b-2b-sef@b3b.co", role_key="site_chief", tum_projeler=False)
    basliklar = await giris("b3b-2b-sef@b3b.co")
    document_id, _proje = await evrak_fabrikasi(_TASERON, creator=yaratan)
    zincir = await _zincir(seeded_db, _TASERON, document_id, yaratan)

    yanit = await client.post(f"{_TASERON_YOL}/{document_id}/approve", headers=basliklar)

    assert yanit.status_code in (403, 404), yanit.text
    assert await adim_durumlari(seeded_db, zincir.id) == [False, False, False]


# --------------------------------------------------------------------------- #
# 3. Gelen kutusu proje başına + rol değişimi
# --------------------------------------------------------------------------- #


async def test_gelen_kutusu_PROJE_BASINA_AVM_de_saha_muhendisi_ise_adim_dusmez(
    client, seeded_db, aktor_fabrikasi, evrak_fabrikasi, giris
):
    yaratan = await aktor_fabrikasi("b3b-3-yaratan@b3b.co")
    kule_id, kule = await evrak_fabrikasi(_TASERON, creator=yaratan)
    avm_id, avm = await evrak_fabrikasi(_TASERON, creator=yaratan)
    await _zincir(seeded_db, _TASERON, kule_id, yaratan)
    await _zincir(seeded_db, _TASERON, avm_id, yaratan)
    kisi = await aktor_fabrikasi("b3b-3-sef@b3b.co", role_key="hr_manager", tum_projeler=False)
    await proje_rolu_ver(seeded_db, kisi, kule, "site_chief")
    await proje_rolu_ver(seeded_db, kisi, avm, "field_engineer")
    basliklar = await giris("b3b-3-sef@b3b.co")

    govde = (await client.get("/approvals", headers=basliklar)).json()

    assert [i["document_id"] for i in govde["items"]] == [str(kule_id)]
    assert govde["total"] == 1
    assert govde["items"][0]["can_decide"] is True


async def test_ROL_DEGISIMI_bekleyen_adimi_gotürür_ve_kutu_ile_gecmis_tutarlidir(
    client, seeded_db, aktor_fabrikasi, evrak_fabrikasi, giris
):
    """Ekipten rolü değişen kişi bekleyen adımını kaybeder (plan: beklenen davranış)."""
    yaratan = await aktor_fabrikasi("b3b-3b-yaratan@b3b.co")
    document_id, proje = await evrak_fabrikasi(_TASERON, creator=yaratan)
    await _zincir(seeded_db, _TASERON, document_id, yaratan)
    kisi = await aktor_fabrikasi("b3b-3b-sef@b3b.co", role_key="hr_manager", tum_projeler=False)
    await proje_rolu_ver(seeded_db, kisi, proje, "site_chief")
    basliklar = await giris("b3b-3b-sef@b3b.co")
    assert (await client.get("/approvals", headers=basliklar)).json()["total"] == 1

    await proje_rolu_ver(seeded_db, kisi, proje, "field_engineer")

    assert (await client.get("/approvals", headers=basliklar)).json()["total"] == 0
    yanit = await client.post(f"{_TASERON_YOL}/{document_id}/approve", headers=basliklar)
    assert yanit.status_code in (403, 404), yanit.text


async def test_gelen_kutusu_BASKA_PROJENIN_zincirini_gostermez_IDOR(
    client, seeded_db, aktor_fabrikasi, evrak_fabrikasi, giris
):
    """Köprü'de şef olan kişinin kutusunda Kule'nin zinciri YOK (items + total)."""
    yaratan = await aktor_fabrikasi("b3b-3c-yaratan@b3b.co")
    kule_id, _kule = await evrak_fabrikasi(_TASERON, creator=yaratan)
    _kopru_id, kopru = await evrak_fabrikasi(_TASERON, creator=yaratan)
    await _zincir(seeded_db, _TASERON, kule_id, yaratan)
    kisi = await aktor_fabrikasi("b3b-3c-sef@b3b.co", role_key="site_chief", tum_projeler=False)
    await proje_rolu_ver(seeded_db, kisi, kopru, "site_chief")
    basliklar = await giris("b3b-3c-sef@b3b.co")

    govde = (await client.get("/approvals", headers=basliklar)).json()

    assert govde["items"] == []
    assert govde["total"] == 0
    gecmis = (await client.get("/approvals/history", headers=basliklar)).json()
    assert gecmis["items"] == [] and gecmis["total"] == 0


# --------------------------------------------------------------------------- #
# 4. BOŞ adım rolü → 409; zincir AÇILMAZ, evrak durumu DEĞİŞMEZ
# --------------------------------------------------------------------------- #

_ATANMAMIS = "Bu projede {rol} atanmamış; önce Ayarlar > Kullanıcılar'dan atayın"


async def _taslak_yap(seeded_db, tip, document_id) -> None:
    """Fabrikanın `pending_approval` evrağını `draft`a çevirir (submit akışını sınamak için)."""
    if tip is _TASERON:
        belge = await seeded_db.get(SubcontractorProgressPayment, document_id)
        belge.status = SubcontractorPaymentStatus.draft
    elif tip is _ISVEREN:
        belge = await seeded_db.get(ProgressPayment, document_id)
        belge.status = ProgressPaymentStatus.draft
    else:
        belge = await seeded_db.get(PurchaseRequest, document_id)
        belge.status = PurchaseRequestStatus.draft
        from datetime import date

        belge.needed_by = date(2026, 12, 1)
    await seeded_db.flush()


async def _durum(seeded_db, tip, document_id):
    model = {
        _TASERON: SubcontractorProgressPayment,
        _ISVEREN: ProgressPayment,
        _SATINALMA: PurchaseRequest,
    }[tip]
    await seeded_db.refresh(await seeded_db.get(model, document_id))
    return (await seeded_db.get(model, document_id)).status


_AILELER = [
    (_TASERON, _TASERON_YOL, "Şantiye Şefi, Proje Müdürü, Muhasebe", {"period": (2026, 7)}),
    (_ISVEREN, _ISVEREN_YOL, "Muhasebe", {"period": (2026, 7)}),
    (_SATINALMA, _SATINALMA_YOL, "Satınalma, Proje Müdürü, Muhasebe", {}),
]


@pytest.mark.parametrize(("tip", "yol", "eksik_adlar", "ek"), _AILELER)
async def test_BOS_adim_rolu_submit_409_zincir_ACILMAZ_evrak_durumu_DEGISMEZ(
    client, seeded_db, aktor_fabrikasi, evrak_fabrikasi, giris, tip, yol, eksik_adlar, ek
):
    """Üç evrak ailesinde de: projede adım rollerinin sahibi YOKSA gönderim 409 + mesaj;
    zincir kurulmaz ve evrak `draft`ta kalır.

    Mutasyon (b): `create_chain`daki `_assert_step_roles_have_owners` kaldırılırsa kırmızı."""
    admin = await aktor_fabrikasi(
        "b3b-4-admin-" + tip.value[:4] + "@b3b.co", role_key="system_admin"
    )
    yaratan = await aktor_fabrikasi(
        "b3b-4-yaratan-" + tip.value[:4] + "@b3b.co", role_key="hr_manager"
    )
    document_id, _proje = await evrak_fabrikasi(tip, creator=yaratan, rol_sahipleri=False, **ek)
    await _taslak_yap(seeded_db, tip, document_id)
    basliklar = await giris(admin.email)

    yanit = await client.post(f"{yol}/{document_id}/submit", headers=basliklar)

    assert yanit.status_code == 409, yanit.text
    mesaj = yanit.json()["detail"]
    assert mesaj.startswith("Bu projede ") and mesaj.endswith(
        " atanmamış; önce Ayarlar > Kullanıcılar'dan atayın"
    ), mesaj
    for ad in eksik_adlar.split(", "):
        assert ad in mesaj, (ad, mesaj)
    assert (
        await seeded_db.scalar(
            select(func.count())
            .select_from(ApprovalChain)
            .where(ApprovalChain.document_type == tip, ApprovalChain.document_id == document_id)
        )
        == 0
    ), "zincir AÇILMAMALIYDI"
    assert (await _durum(seeded_db, tip, document_id)).value == "draft", "durum DEĞİŞMEMELİYDİ"


async def test_BOS_rol_mesaji_TEK_eksikte_plandaki_metinle_birebir(
    seeded_db, aktor_fabrikasi, evrak_fabrikasi
):
    """İşveren hakedişinin tek adımı Muhasebe: metin planla BİREBİR."""
    yaratan = await aktor_fabrikasi("b3b-4b-yaratan@b3b.co", role_key="hr_manager")
    document_id, _proje = await evrak_fabrikasi(_ISVEREN, creator=yaratan, rol_sahipleri=False)

    with pytest.raises(ConflictError) as hata:
        await _zincir(seeded_db, _ISVEREN, document_id, yaratan)

    assert (
        str(hata.value) == "Bu projede Muhasebe atanmamış; önce Ayarlar > Kullanıcılar'dan atayın"
    )


async def test_ALL_PROJECTS_ana_rol_sahibi_varsa_submit_GECER(
    client, seeded_db, aktor_fabrikasi, evrak_fabrikasi, giris
):
    """Projede ekip satırı yok ama "Tüm projeler" + ana rolü Muhasebe olan aktif kişi VAR: gönderim
    geçer ve zincir açılır."""
    admin = await aktor_fabrikasi("b3b-4c-admin@b3b.co", role_key="system_admin")
    yaratan = await aktor_fabrikasi("b3b-4c-yaratan@b3b.co", role_key="hr_manager")
    await aktor_fabrikasi("b3b-4c-muh@b3b.co", role_key="accounting")  # all_projects=True
    document_id, _proje = await evrak_fabrikasi(
        _ISVEREN, creator=yaratan, rol_sahipleri=False, period=(2026, 7)
    )
    await _taslak_yap(seeded_db, _ISVEREN, document_id)
    basliklar = await giris(admin.email)

    yanit = await client.post(f"{_ISVEREN_YOL}/{document_id}/submit", headers=basliklar)

    assert yanit.status_code == 200, yanit.text
    assert await service.open_chain(seeded_db, _ISVEREN, document_id) is not None


async def test_PASIF_kullanici_sahip_SAYILMAZ_zincir_acilmaz(
    seeded_db, aktor_fabrikasi, evrak_fabrikasi, user_factory
):
    """Pasif kullanıcı giriş yapamaz: tek sahip pasifse zincir TAKILIRDI → 409 (fail-closed)."""
    yaratan = await aktor_fabrikasi("b3b-4d-yaratan@b3b.co", role_key="hr_manager")
    pasif = await user_factory(
        email="b3b-4d-pasif@b3b.co", password="parola1234", role_key="accounting", status="passive"
    )
    pasif.all_projects = True
    await seeded_db.flush()
    document_id, _proje = await evrak_fabrikasi(_ISVEREN, creator=yaratan, rol_sahipleri=False)

    with pytest.raises(ConflictError):
        await _zincir(seeded_db, _ISVEREN, document_id, yaratan)


async def test_EKIP_uyesi_sahip_SAYILIR_baska_projenin_uyesi_SAYILMAZ(
    seeded_db, aktor_fabrikasi, evrak_fabrikasi
):
    yaratan = await aktor_fabrikasi("b3b-4e-yaratan@b3b.co", role_key="hr_manager")
    document_id, proje = await evrak_fabrikasi(_ISVEREN, creator=yaratan, rol_sahipleri=False)
    _diger_id, diger = await evrak_fabrikasi(_ISVEREN, creator=yaratan, rol_sahipleri=False)
    muh = await aktor_fabrikasi("b3b-4e-muh@b3b.co", role_key="hr_manager", tum_projeler=False)
    await proje_rolu_ver(seeded_db, muh, diger, "accounting")  # BAŞKA projede

    with pytest.raises(ConflictError):
        await _zincir(seeded_db, _ISVEREN, document_id, yaratan)

    await proje_rolu_ver(seeded_db, muh, proje, "accounting")  # şimdi bu projede
    zincir = await _zincir(seeded_db, _ISVEREN, document_id, yaratan)
    assert zincir.id is not None
