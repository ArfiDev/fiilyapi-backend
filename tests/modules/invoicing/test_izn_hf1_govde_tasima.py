"""IZN-HF1 — fatura PATCH'inde `project_id` gövdesi ile bağlam TAŞINAMAZ (uçtan uca).

`InvoiceUpdate.project_id` bildirilir ve `/invoices/{invoice_id}` yol çözücüsü vardır. Şirket geneli
fatura (`project_id IS NULL`) yol çözücüsünden `None` döner; eskiden bu durumda gövde devreye girip
bağlamı gövdedeki projeye ÇEVİRİYORDU: kişi B'deki rolüyle (kapı + maske) şirket faturasını okuyup
yazabiliyordu. Taşıma kuralıyla bağlam `None`: kapı ANA role, maske ana ∪ ekip rolleri birleşimine
bakar.
"""

from __future__ import annotations

from sqlalchemy import select

from app.core.sayfalar import PageLevel
from app.modules.invoicing.models import Invoice
from tests._hassas_alan import rol_gizli
from tests._proje_ekibi import ekibe_ekle
from tests.modules.invoicing.conftest import _auth

_PAROLA = "parola1234"


async def _giris(client, email):
    resp = await client.post("/auth/login", json={"email": email, "password": _PAROLA})
    assert resp.status_code == 200, resp.text
    return _auth(resp.json()["access_token"])


async def _fatura(session, kimlik):
    session.expire_all()
    return (await session.execute(select(Invoice).where(Invoice.id == kimlik))).scalar_one()


async def test_sirket_faturasi_govde_project_id_ile_B_rolune_TASINAMAZ(
    client, seeded_db, db_session, user_factory, project_factory, fatura_fabrikasi
):
    """Ana rol Hiçbir şey (kapı kapalı), B'de Düzenler (açık): şirket faturasına `project_id: B`
    ile PATCH 403, fatura DEĞİŞMEZ (not + proje aynı)."""
    proje_b = await project_factory("HF1-B", name="Proje B")
    fatura = await fatura_fabrikasi(project=None)
    fatura_id, b_id, ilk_not = fatura.id, str(proje_b.id), fatura.note
    r_ana = await rol_gizli(seeded_db, "hf1_ana", set(), level=PageLevel.none)
    r_b = await rol_gizli(seeded_db, "hf1_b", set())
    kisi = await user_factory(email="tasi@hf1inv.co", password=_PAROLA, role_key=r_ana.key)
    await ekibe_ekle(seeded_db, kisi, proje_b.id, r_b.id)
    basliklar = await _giris(client, "tasi@hf1inv.co")

    yanit = await client.patch(
        f"/invoices/{fatura_id}",
        json={"project_id": b_id, "note": "ele geçirildi"},
        headers=basliklar,
    )

    assert yanit.status_code == 403, yanit.text
    sonra = await _fatura(db_session, fatura_id)
    assert sonra.note == ilk_not and sonra.project_id is None

    govdesiz = await client.patch(f"/invoices/{fatura_id}", json={"note": "x"}, headers=basliklar)
    assert govdesiz.status_code == 403, (
        govdesiz.text
    )  # ikiz: gövdesiz de 403 (bağlam yok → ana rol)


async def test_proje_faturasi_kendi_projesini_yazan_govde_yol_baglaminda_calisir(
    client, seeded_db, user_factory, project_factory, fatura_fabrikasi
):
    """POZİTİF KONTROL (P == Q): B'deki fatura, B'de Düzenler, gövdede B → kapıyı geçer."""
    proje_b = await project_factory("HF1-P", name="Proje P")
    fatura = await fatura_fabrikasi(project=proje_b)
    fatura_id, b_id = fatura.id, str(proje_b.id)
    r_ana = await rol_gizli(seeded_db, "hf1p_ana", set(), level=PageLevel.none)
    r_b = await rol_gizli(seeded_db, "hf1p_b", set())
    kisi = await user_factory(email="kendi@hf1inv.co", password=_PAROLA, role_key=r_ana.key)
    await ekibe_ekle(seeded_db, kisi, proje_b.id, r_b.id)
    basliklar = await _giris(client, "kendi@hf1inv.co")

    yanit = await client.patch(
        f"/invoices/{fatura_id}", json={"project_id": b_id, "note": "ok"}, headers=basliklar
    )

    assert yanit.status_code == 200, yanit.text
