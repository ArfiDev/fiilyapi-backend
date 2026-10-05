"""IZN-HF1 — çek/senet PATCH'inde `project_id` gövdesi ile bağlam TAŞINAMAZ (uçtan uca).

`FinancialInstrumentUpdate.project_id` bildirilir; `/financial-instruments/{instrument_id}` yol
çözücüsü (`_instrument_project`) HF1 çürütmesinde eklendi (eskiden çözücüsüzdü: gövdedeki Q bağlamı
Q'ya çeviriyordu). Şimdi P'deki araç Q'ya taşınamaz.
"""

from __future__ import annotations

from sqlalchemy import select

from app.core.sayfalar import PageLevel
from app.modules.treasury.models import FinancialInstrument
from tests._hassas_alan import rol_gizli
from tests._proje_ekibi import ekibe_ekle
from tests.modules.treasury.conftest import _auth

_PAROLA = "parola1234"


async def _giris(client, email):
    resp = await client.post("/auth/login", json={"email": email, "password": _PAROLA})
    assert resp.status_code == 200, resp.text
    return _auth(resp.json()["access_token"])


async def _evrak(session, kimlik):
    session.expire_all()
    return (
        await session.execute(select(FinancialInstrument).where(FinancialInstrument.id == kimlik))
    ).scalar_one()


async def test_arac_P_projesinden_Q_projesine_govdeyle_TASINAMAZ(
    client, seeded_db, db_session, user_factory, project_factory, cek_fabrikasi
):
    """Ana rol Hiçbir şey, P'de Düzenler (ve Q'da da Düzenler): P çekini `project_id: Q` ile
    taşımak 403, kayıt DEĞİŞMEZ. POZİTİF: P == Q yazımı kapıyı geçer. `null` ile şirket geneline
    taşıma da 403."""
    proje_p = await project_factory("HF1C-P", name="Proje P")
    proje_q = await project_factory("HF1C-Q", name="Proje Q")
    cek = await cek_fabrikasi(project=proje_p)
    cek_id, p_id, q_id = cek.id, proje_p.id, str(proje_q.id)
    r_ana = await rol_gizli(seeded_db, "hf1c_ana", set(), level=PageLevel.none)
    r_ekip = await rol_gizli(seeded_db, "hf1c_ekip", set())
    kisi = await user_factory(email="cek@hf1c.co", password=_PAROLA, role_key=r_ana.key)
    await ekibe_ekle(seeded_db, kisi, proje_p.id, r_ekip.id)
    await ekibe_ekle(seeded_db, kisi, proje_q.id, r_ekip.id)
    basliklar = await _giris(client, "cek@hf1c.co")
    yol = f"/financial-instruments/{cek_id}"

    tasima = await client.patch(yol, json={"project_id": q_id}, headers=basliklar)
    assert tasima.status_code == 403, tasima.text
    assert (await _evrak(db_session, cek_id)).project_id == p_id

    genelleme = await client.patch(yol, json={"project_id": None}, headers=basliklar)
    assert genelleme.status_code == 403, genelleme.text
    assert (await _evrak(db_session, cek_id)).project_id == p_id

    kendi = await client.patch(yol, json={"project_id": str(p_id)}, headers=basliklar)
    assert kendi.status_code != 403, kendi.text  # P == Q: yol bağlamı (P'de Düzenler)
