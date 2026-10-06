"""IZN-B5f — sayfasız ZİNCİR ADIMI SAHİBİNİN approve/reject yanıtı gizli kategorileri maskeler.

Onay uçları (`approvals/gate.py` yedek dalı) sayfa Onaylar biti olmayan adım sahibine açıktır ve
yanıt belgenin TAM detayıdır. Bu dosya yanıt gövdesinin kişinin etkin rolünün gizli
kategorileriyle maskelendiğini kanıtlar:

* A: "Tüm projeler" + ana rol = adım rolü, onay sayfalarında Onaylar biti kapalı;
* B: ana rol başka, projede EKİP rolü = adım rolü, o rolün HİÇ sayfa hücresi yok.

Ölçüt: yanıt gövdesi, beklenen kümeyle ikinci kez maskelendiğinde DEĞİŞMEZ (zaten maskeli) ve
somut tutar alanı `None`dır. Pozitif kontrol: gizli kümesi BOŞ aynı kişi aynı alanı DOLU görür ve
o gövde maskelenince DEĞİŞİR (ölçütün dişi var).
"""

from decimal import Decimal

import pytest
from sqlalchemy import delete, select, update

from app.core.field_mask import MaskeKumeleri, maskele
from app.core.page_gate import pages_ok
from app.core.sayfalar import HiddenCategory as H
from app.modules.approvals import service
from app.modules.approvals.models import ApprovalDocumentType as T
from app.modules.procurement.schemas import PurchaseRequestResponse
from app.modules.progress_payments.schemas import ProgressPaymentDetail
from app.modules.roles.models import Role, RolePagePermission
from app.modules.subcontractor_progress_payments.schemas import SubcontractorProgressPaymentDetail
from tests._hassas_alan import gizli_alanlar_ayarla
from tests.modules.approvals.conftest import proje_rolu_ver, rol_id
from tests.progress_payments.conftest import _mu3d_esleme  # noqa: F401 — MU-3D eşleme (autouse)

_SAT_GIZLI = frozenset({H.sozlesme_fiyat, H.maliyet_kar})
#: İşveren hakediş tutarları `sozlesme_fiyat`, taşeron hakediş tutarları `maliyet_kar` etiketlidir.
_ISV_GIZLI = frozenset({H.sozlesme_fiyat})
_TAS_GIZLI = frozenset({H.maliyet_kar})

# (tip, yol, ilk adım rolü, onay sayfaları, yanıt şeması, gizli küme, somut tutar alanının yolu)
DURUMLAR = [
    pytest.param(
        T.purchase_request,
        "/purchase-requests",
        "procurement",
        ("stok.satinalma_talepleri",),
        PurchaseRequestResponse,
        _SAT_GIZLI,
        ("estimated_total",),
        id="satinalma",
    ),
    pytest.param(
        T.subcontractor_progress_payment,
        "/subcontractor-progress-payments",
        "site_chief",
        ("mali.hakedis_taseron", "proje.taseron_hakedis"),
        SubcontractorProgressPaymentDetail,
        _TAS_GIZLI,
        ("calculation", "net"),
        id="taseron",
    ),
    pytest.param(
        T.progress_payment,
        "/progress-payments",
        "accounting",
        ("mali.hakedis_isveren", "proje.isveren_hakedis", "santiye.hakedisler"),
        ProgressPaymentDetail,
        _ISV_GIZLI,
        ("calculation", "net"),
        id="isveren",
    ),
]


def _al(govde: dict, yol: tuple[str, ...]):
    for parca in yol:
        govde = govde[parca]
    return govde


async def _rol(db, key: str) -> Role:
    return (await db.execute(select(Role).where(Role.key == key))).scalar_one()


def _yeniden_maskeli(sema, govde: dict, gizli: frozenset) -> dict:
    kumeler = MaskeKumeleri(varsayilan=gizli, proje_basina={}, ana=gizli)
    return maskele(sema.model_validate(govde), kumeler).model_dump(mode="json")


async def _kur(db, fab, evrak, tip, adim_rolu, sayfalar, senaryo, etiket):
    yaratan = await fab(f"b5f-mask-{etiket}-y@o.co")
    doc_id, proje = await evrak(tip, creator=yaratan)
    await service.create_chain(
        db,
        document_type=tip,
        document_id=doc_id,
        amount=Decimal("100.00"),
        created_by_user_id=yaratan.id,
    )
    email = f"b5f-mask-{etiket}-k@o.co"
    adim = await rol_id(db, adim_rolu)
    if senaryo == "A":
        kisi = await fab(email, role_key=adim_rolu)  # "Tüm projeler" + ana rol
        await db.execute(
            update(RolePagePermission)
            .where(RolePagePermission.role_id == adim, RolePagePermission.page_key.in_(sayfalar))
            .values(can_approve=False)
        )
    else:
        kisi = await fab(email, role_key="hr_manager", tum_projeler=False)
        await proje_rolu_ver(db, kisi, proje, adim_rolu)
        await db.execute(delete(RolePagePermission).where(RolePagePermission.role_id == adim))
    await db.flush()
    assert not await pages_ok(db, kisi, sayfalar, "approve", project_id=proje.id, record=False)
    return doc_id, email


async def _istek(client, giris_, yol, doc_id, eylem, email):
    kw = {"json": {"reason": "maske olcumu"}} if eylem == "reject" else {}
    yanit = await client.post(f"{yol}/{doc_id}/{eylem}", headers=await giris_(email), **kw)
    assert yanit.status_code == 200, yanit.text
    return yanit.json()


@pytest.mark.parametrize(
    ("tip", "yol", "adim_rolu", "sayfalar", "sema", "gizli", "tutar_alani"), DURUMLAR
)
@pytest.mark.parametrize("senaryo", ["A", "B"])
@pytest.mark.parametrize("eylem", ["approve", "reject"])
async def test_sayfasiz_adim_sahibinin_onay_yaniti_GIZLI_KATEGORIYI_MASKELER(
    client,
    seeded_db,
    aktor_fabrikasi,
    evrak_fabrikasi,
    giris,
    tip,
    yol,
    adim_rolu,
    sayfalar,
    sema,
    gizli,
    tutar_alani,
    senaryo,
    eylem,
):
    etiket = f"{tip.value}-{senaryo}-{eylem}"
    await gizli_alanlar_ayarla(seeded_db, await _rol(seeded_db, adim_rolu), gizli)
    await gizli_alanlar_ayarla(seeded_db, await _rol(seeded_db, "hr_manager"), gizli)
    doc_id, email = await _kur(
        seeded_db, aktor_fabrikasi, evrak_fabrikasi, tip, adim_rolu, sayfalar, senaryo, etiket
    )

    govde = await _istek(client, giris, yol, doc_id, eylem, email)

    assert govde == _yeniden_maskeli(sema, govde, gizli)
    assert _al(govde, tutar_alani) is None


@pytest.mark.parametrize(
    ("tip", "yol", "adim_rolu", "sayfalar", "sema", "gizli", "tutar_alani"), DURUMLAR
)
@pytest.mark.parametrize("senaryo", ["A", "B"])
async def test_KONTROL_gizli_kumesi_BOS_adim_sahibi_tutari_GORUR(
    client,
    seeded_db,
    aktor_fabrikasi,
    evrak_fabrikasi,
    giris,
    tip,
    yol,
    adim_rolu,
    sayfalar,
    sema,
    gizli,
    tutar_alani,
    senaryo,
):
    """Ölçütün dişi: maskesiz gövde, beklenen kümeyle maskelenince DEĞİŞİR."""
    etiket = f"{tip.value}-{senaryo}-kontrol"
    await gizli_alanlar_ayarla(seeded_db, await _rol(seeded_db, adim_rolu), ())
    await gizli_alanlar_ayarla(seeded_db, await _rol(seeded_db, "hr_manager"), ())
    doc_id, email = await _kur(
        seeded_db, aktor_fabrikasi, evrak_fabrikasi, tip, adim_rolu, sayfalar, senaryo, etiket
    )

    govde = await _istek(client, giris, yol, doc_id, "approve", email)

    assert govde != _yeniden_maskeli(sema, govde, gizli)
    assert _al(govde, tutar_alani) is not None
