"""DSC-B0 — `GET/PUT /users/{id}/disciplines` + `/auth/me.disciplines`.

Bu dilim HICBIR ucu suzmez; burada yalniz atama uclari sinanir: tam degistirme, bos =
kisitsiz, tekillestirme, atomiklik (bilinmeyen disiplin → 404 ve HICBIR satir degismez),
kullanici satiri kilidi (SQL-metin bekcisi), izin kapilari, denetim ve /auth/me.
"""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.audit.models import AuditAction, AuditLog
from app.modules.earned_value.models import EvDiscipline, UserDiscipline
from app.modules.users.models import User
from tests.conftest import test_engine

PASSWORD = "parola1234"


async def _headers(client: AsyncClient, user_factory, role_key: str) -> dict[str, str]:
    email = f"{role_key}.{uuid.uuid4().hex[:6]}@dsc-b0-api.co"
    await user_factory(email=email, password=PASSWORD, role_key=role_key)
    login = await client.post("/auth/login", json={"email": email, "password": PASSWORD})
    return {"Authorization": f"Bearer {login.json()['access_token']}"}


@pytest.fixture
async def admin(client: AsyncClient, user_factory) -> dict[str, str]:
    return await _headers(client, user_factory, "system_admin")


@pytest.fixture
async def hedef(seeded_db: AsyncSession, user_factory) -> User:
    return await user_factory(
        email=f"hedef.{uuid.uuid4().hex[:6]}@dsc-b0-api.co",
        password=PASSWORD,
        role_key="site_chief",
        full_name="Hedef Kişi",
    )


@pytest.fixture
async def civ(disiplin_fabrikasi) -> EvDiscipline:
    return await disiplin_fabrikasi("CIV", "İnşaat")


@pytest.fixture
async def elk(disiplin_fabrikasi) -> EvDiscipline:
    return await disiplin_fabrikasi("ELK", "Elektrik")


def _url(user_id: uuid.UUID) -> str:
    return f"/users/{user_id}/disciplines"


def _siralı(*disciplines: EvDiscipline) -> list[str]:
    return sorted(str(d.id) for d in disciplines)


async def _put(client, headers, user_id, ids) -> object:
    return await client.put(
        _url(user_id), json={"discipline_ids": [str(i) for i in ids]}, headers=headers
    )


async def _satirlar(session: AsyncSession, user_id: uuid.UUID) -> set[uuid.UUID]:
    rows = await session.execute(
        select(UserDiscipline.discipline_id).where(UserDiscipline.user_id == user_id)
    )
    return set(rows.scalars())


# --- GET / PUT --------------------------------------------------------------------------


async def test_atamasiz_kullanici_bos_liste_doner(client, admin, hedef) -> None:
    resp = await client.get(_url(hedef.id), headers=admin)
    assert resp.status_code == 200
    assert resp.json() == {"discipline_ids": []}


async def test_put_atar_get_okur_sirali(client, admin, hedef, civ, elk) -> None:
    resp = await _put(client, admin, hedef.id, [elk.id, civ.id])
    assert resp.status_code == 200
    assert resp.json() == {"discipline_ids": _siralı(civ, elk)}
    assert (await client.get(_url(hedef.id), headers=admin)).json() == {
        "discipline_ids": _siralı(civ, elk)
    }


async def test_put_tam_degistirir_fark_uygular(client, admin, hedef, civ, elk, seeded_db) -> None:
    await _put(client, admin, hedef.id, [civ.id])
    yeni = await _put(client, admin, hedef.id, [elk.id])
    assert yeni.json() == {"discipline_ids": _siralı(elk)}
    assert await _satirlar(seeded_db, hedef.id) == {elk.id}


async def test_bos_liste_tum_atamalari_siler_kisitsiz(client, admin, hedef, civ, elk, seeded_db):
    await _put(client, admin, hedef.id, [civ.id, elk.id])
    resp = await _put(client, admin, hedef.id, [])
    assert resp.status_code == 200
    assert resp.json() == {"discipline_ids": []}
    assert await _satirlar(seeded_db, hedef.id) == set()


async def test_yinelenen_idler_tekillesir(client, admin, hedef, civ, seeded_db) -> None:
    resp = await _put(client, admin, hedef.id, [civ.id, civ.id, civ.id])
    assert resp.status_code == 200
    assert resp.json() == {"discipline_ids": _siralı(civ)}
    assert await _satirlar(seeded_db, hedef.id) == {civ.id}


async def test_bilinmeyen_disiplin_404_ve_hicbir_satir_degismez(
    client, admin, hedef, civ, elk, seeded_db
) -> None:
    await _put(client, admin, hedef.id, [civ.id])
    yok = uuid.uuid4()

    resp = await _put(client, admin, hedef.id, [elk.id, yok])

    assert resp.status_code == 404
    assert str(yok) in resp.json()["detail"]
    assert await _satirlar(seeded_db, hedef.id) == {civ.id}  # ne silindi ne eklendi


async def test_olmayan_kullanici_404_get_ve_put(client, admin, civ) -> None:
    yok = uuid.uuid4()
    assert (await client.get(_url(yok), headers=admin)).status_code == 404
    resp = await _put(client, admin, yok, [civ.id])
    assert resp.status_code == 404
    assert resp.json()["detail"] == "Kullanıcı bulunamadı"


async def test_govde_dogrulamasi_extra_alan_ve_ust_sinir_422(client, admin, hedef) -> None:
    ekstra = await client.put(_url(hedef.id), json={"discipline_ids": [], "x": 1}, headers=admin)
    assert ekstra.status_code == 422
    cok = [str(uuid.uuid4()) for _ in range(101)]
    assert (
        await client.put(_url(hedef.id), json={"discipline_ids": cok}, headers=admin)
    ).status_code == 422
    assert (await client.put(_url(hedef.id), json={}, headers=admin)).status_code == 422


async def test_kendine_atama_engellenmez(client, seeded_db, user_factory, civ) -> None:
    """Ü10: admin kendine atayabilir (ekran uyarir, backend engellemez)."""
    email = f"kendi.{uuid.uuid4().hex[:6]}@dsc-b0-api.co"
    kendi = await user_factory(email=email, password=PASSWORD, role_key="system_admin")
    token = (await client.post("/auth/login", json={"email": email, "password": PASSWORD})).json()[
        "access_token"
    ]
    resp = await _put(client, {"Authorization": f"Bearer {token}"}, kendi.id, [civ.id])
    assert resp.status_code == 200


# --- izin -------------------------------------------------------------------------------


async def test_user_management_yokken_get_ve_put_403(client, user_factory, hedef, civ) -> None:
    """site_chief `user_management` = none: GET (view) da PUT (full) da 403."""
    headers = await _headers(client, user_factory, "site_chief")
    assert (await client.get(_url(hedef.id), headers=headers)).status_code == 403
    assert (await _put(client, headers, hedef.id, [civ.id])).status_code == 403


async def test_kimliksiz_401(client, hedef) -> None:
    assert (await client.get(_url(hedef.id))).status_code == 401


# --- denetim ----------------------------------------------------------------------------


async def test_put_denetim_satiri_yazar_hedef_adi_ve_kodlar(
    client, admin, hedef, civ, elk, seeded_db
) -> None:
    await _put(client, admin, hedef.id, [elk.id, civ.id])
    rows = list(
        (await seeded_db.execute(select(AuditLog).where(AuditLog.action == AuditAction.update)))
        .scalars()
        .all()
    )
    assert [r.detail for r in rows] == [
        "Kullanıcı disiplin ataması güncellendi: Hedef Kişi · CIV, ELK"
    ]


async def test_ayni_kume_ikinci_put_denetim_satiri_yazmaz(
    client, admin, hedef, civ, seeded_db
) -> None:
    async def guncellemeler() -> int:
        rows = await seeded_db.execute(
            select(AuditLog.id).where(AuditLog.action == AuditAction.update)
        )
        return len(list(rows.scalars()))

    await _put(client, admin, hedef.id, [civ.id])
    once = await guncellemeler()
    resp = await _put(client, admin, hedef.id, [civ.id, civ.id])
    assert resp.status_code == 200
    assert resp.json() == {"discipline_ids": _siralı(civ)}
    assert await guncellemeler() == once == 1


async def test_bos_atama_denetimi_kisitsiz_der_ve_get_denetim_yazmaz(
    client, admin, hedef, civ, seeded_db
) -> None:
    await client.get(_url(hedef.id), headers=admin)
    await _put(client, admin, hedef.id, [civ.id])
    await _put(client, admin, hedef.id, [])
    rows = list((await seeded_db.execute(select(AuditLog.detail, AuditLog.action))).all())
    updates = [d for d, a in rows if a == AuditAction.update]
    assert updates[-1] == "Kullanıcı disiplin ataması güncellendi: Hedef Kişi · kısıtsız"
    assert len(updates) == 2


# --- kilit ------------------------------------------------------------------------------


async def test_put_kullanici_satirini_for_update_ile_kilitler(client, admin, hedef, civ) -> None:
    """SQL-METIN bekcisi (`test_user_project_access_kilidi.py` emsali): davranis testi
    kilit kalksa da yesil kalabilir; kilidin KENDISINI yalniz ifade metni yakalar."""
    ifadeler: list[str] = []

    def kaydet(conn, cursor, statement, parameters, context, executemany) -> None:  # noqa: ANN001
        ifadeler.append(" ".join(statement.split()))

    event.listen(test_engine.sync_engine, "before_cursor_execute", kaydet)
    try:
        resp = await _put(client, admin, hedef.id, [civ.id])
    finally:
        event.remove(test_engine.sync_engine, "before_cursor_execute", kaydet)

    assert resp.status_code == 200
    kilitli = [i for i in ifadeler if "FOR UPDATE" in i and "FROM users" in i]
    assert kilitli, f"PUT kullanici satirini FOR UPDATE ile okumadi: {ifadeler}"


# --- /auth/me ---------------------------------------------------------------------------


async def test_me_disiplinleri_atamadan_once_bos_sonra_dolu(
    client, seeded_db, user_factory, admin, civ, elk
) -> None:
    email = f"me.{uuid.uuid4().hex[:6]}@dsc-b0-api.co"
    kisi = await user_factory(email=email, password=PASSWORD, role_key="site_chief")
    kisi_id, beklenen = kisi.id, _siralı(civ, elk)
    civ_id, elk_id = civ.id, elk.id
    token = (await client.post("/auth/login", json={"email": email, "password": PASSWORD})).json()[
        "access_token"
    ]
    kendi = {"Authorization": f"Bearer {token}"}
    seeded_db.expunge_all()  # ortak test oturumu; gercek istekte oturum ayridir

    once = await client.get("/auth/me", headers=kendi)
    assert once.status_code == 200
    assert once.json()["disciplines"] == []

    await _put(client, admin, kisi_id, [elk_id, civ_id])
    # Ortak test oturumu: PUT `User`i rolsuz yukledi; gercek istekte oturum ayridir.
    seeded_db.expunge_all()

    sonra = await client.get("/auth/me", headers=kendi)
    assert sonra.json()["disciplines"] == beklenen

    await _put(client, admin, kisi_id, [])
    seeded_db.expunge_all()
    assert (await client.get("/auth/me", headers=kendi)).json()["disciplines"] == []
