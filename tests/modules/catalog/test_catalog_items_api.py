"""TKL-B2.2 — `GET/POST/PATCH /catalog/items` (cekirdek is kalemi katalogu).

Izin (GECICI): okuma `contracts:view`, yazma `contracts:full` + `RequireUnrestricted`.
`earned_value:view` olup `contracts=none` olan roller (site_chief, field_engineer) 403 alir.
`ref_price` para alani: kapsami `limited` olan rolde gizli; EV KAT yanitinda hic yok.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.core.access import AccessLevel
from app.core.sayfalar import HiddenCategory
from app.modules.audit.models import AuditLog
from app.modules.catalog.models import ContractorType, EvDiscipline
from app.modules.users.models import User
from tests._hassas_alan import rol_gizle
from tests._modul_duzeyi_yardimcisi import modul_duzeyi_yaz
from tests._proje_ekibi import baska_projede_disiplinli

from .._boq import _auth, _login_with_access

pytestmark = pytest.mark.asyncio

URL = "/catalog/items"
PASSWORD = "parola1234"


async def _disiplin(session, code: str, name: str = "Kaba İnşaat") -> EvDiscipline:
    row = EvDiscipline(
        code=code,
        name=name,
        color="#2563EB",
        default_contractor_type=ContractorType.OWN,
    )
    session.add(row)
    await session.flush()
    return row


def _govde(discipline: EvDiscipline, **over) -> dict:
    base = {
        "discipline_id": str(discipline.id),
        "name": "Kalıp",
        "uom": "m²",
        "standard_unit_mhr": "1.8000",
        "default_contractor_type": "own",
    }
    return {**base, **over}


async def _giris(client, db_session, user_factory, role_key: str) -> dict[str, str]:
    token = await _login_with_access(
        client, db_session, user_factory, role_key, f"{role_key}.{uuid.uuid4().hex[:6]}@tkl.co"
    )
    return _auth(token)


@pytest.fixture
async def kab(db_session, seeded_db) -> EvDiscipline:
    return await _disiplin(seeded_db, "KAB")


@pytest.fixture
async def admin(client, db_session, user_factory, seeded_db):
    return await _giris(client, db_session, user_factory, "system_admin")


# ---------------------------------------------------------------- mutlu yol


async def test_olustur_listele_guncelle_mutlu_yol(client, admin, kab) -> None:
    created = await client.post(URL, json=_govde(kab, description="Duvar kalıbı"), headers=admin)
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["poz_no"] == "KAB-0001"
    assert body["discipline"]["code"] == "KAB"
    assert body["default_contractor_type"] == "own"
    assert body["ref_price"] is None and body["price_updated_at"] is None
    assert body["description"] == "Duvar kalıbı"

    listed = await client.get(URL, headers=admin)
    assert listed.status_code == 200
    assert [i["id"] for i in listed.json()["items"]] == [body["id"]]

    patched = await client.patch(
        f"{URL}/{body['id']}", json={"name": "Kalıp (tünel)", "uom": "m²"}, headers=admin
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["name"] == "Kalıp (tünel)"
    assert patched.json()["poz_no"] == "KAB-0001"


async def test_poz_no_otomatik_ardisik_ve_liste_poz_no_sirali(client, admin, kab) -> None:
    # Ad sirasi poz no sirasinin TERSI (Ahsap < Demir < Zemin): `order_by(name)` mutasyonu
    # yakalanir.
    for ad in ("Zemin", "Demir", "Ahşap"):
        r = await client.post(URL, json=_govde(kab, name=ad), headers=admin)
        assert r.status_code == 201, r.text
    items = (await client.get(URL, headers=admin)).json()["items"]
    assert [i["poz_no"] for i in items] == ["KAB-0001", "KAB-0002", "KAB-0003"]
    assert [i["name"] for i in items] == ["Zemin", "Demir", "Ahşap"]


async def test_govdede_poz_no_422(client, admin, kab) -> None:
    resp = await client.post(URL, json=_govde(kab, poz_no="KAB-0099"), headers=admin)
    assert resp.status_code == 422
    created = await client.post(URL, json=_govde(kab), headers=admin)
    patch = await client.patch(
        f"{URL}/{created.json()['id']}", json={"poz_no": "KAB-0099"}, headers=admin
    )
    assert patch.status_code == 422


async def test_ref_price_yaz_oku_price_updated_at_dolar_ve_null_temizler(
    client, admin, kab
) -> None:
    created = await client.post(URL, json=_govde(kab, ref_price="1250.50"), headers=admin)
    assert created.status_code == 201, created.text
    assert Decimal(created.json()["ref_price"]) == Decimal("1250.50")
    first_stamp = created.json()["price_updated_at"]
    assert first_stamp is not None
    item_id = created.json()["id"]

    other = await client.patch(f"{URL}/{item_id}", json={"uom": "m2"}, headers=admin)
    assert other.json()["price_updated_at"] == first_stamp  # baska alan: damga degismez

    cleared = await client.patch(f"{URL}/{item_id}", json={"ref_price": None}, headers=admin)
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["ref_price"] is None
    assert cleared.json()["price_updated_at"] != first_stamp  # degisti → yenilendi


async def test_description_null_temizler_diger_alanlar_null_422(client, admin, kab) -> None:
    created = await client.post(URL, json=_govde(kab, description="x"), headers=admin)
    item_id = created.json()["id"]
    ok = await client.patch(f"{URL}/{item_id}", json={"description": None}, headers=admin)
    assert ok.status_code == 200 and ok.json()["description"] is None
    for field in ("name", "uom", "standard_unit_mhr", "default_contractor_type", "discipline_id"):
        resp = await client.patch(f"{URL}/{item_id}", json={field: None}, headers=admin)
        assert resp.status_code == 422, (field, resp.text)


@pytest.mark.parametrize(
    "over",
    [
        {"standard_unit_mhr": "0"},
        {"standard_unit_mhr": "1.00001"},
        {"ref_price": "-1"},
        {"ref_price": "1.001"},
        {"name": ""},
        {"name": "x" * 201},
        {"uom": "u" * 51},
        {"default_contractor_type": "other"},
    ],
)
async def test_alan_kisitlari_422(client, admin, kab, over) -> None:
    resp = await client.post(URL, json=_govde(kab, **over), headers=admin)
    assert resp.status_code == 422, resp.text


async def test_404_409_metinleri_EV_ile_ayni(client, admin, kab) -> None:
    await client.post(URL, json=_govde(kab), headers=admin)
    dup = await client.post(URL, json=_govde(kab, name="  KALIP ".replace("I", "ı")), headers=admin)
    assert dup.status_code == 409
    assert "zaten var" in dup.json()["detail"]
    ghost = await client.post(URL, json=_govde(kab, discipline_id=str(uuid.uuid4())), headers=admin)
    assert ghost.status_code == 404 and ghost.json()["detail"] == "Disiplin bulunamadı"
    missing = await client.patch(f"{URL}/{uuid.uuid4()}", json={"name": "a"}, headers=admin)
    assert missing.status_code == 404
    assert missing.json()["detail"] == "Katalog iş tipi bulunamadı"


async def test_arama_ve_disiplin_filtresi(client, admin, kab, db_session) -> None:
    elk = await _disiplin(db_session, "ELK", "Elektrik")
    await client.post(URL, json=_govde(kab, name="Kalıp"), headers=admin)
    await client.post(URL, json=_govde(elk, name="Kablo"), headers=admin)
    by_name = (await client.get(URL, params={"q": "kablo"}, headers=admin)).json()["items"]
    assert [i["name"] for i in by_name] == ["Kablo"]
    by_poz = (await client.get(URL, params={"q": "kab-0001"}, headers=admin)).json()["items"]
    assert [i["poz_no"] for i in by_poz] == ["KAB-0001"]
    by_disc = (await client.get(URL, params={"discipline_id": str(elk.id)}, headers=admin)).json()[
        "items"
    ]
    assert [i["poz_no"] for i in by_disc] == ["ELK-0001"]


async def test_arama_LIKE_jokerleri_kacirilir(client, admin, kab) -> None:
    for ad in ("Kalıp 100%", "Kalıp 100x", "Boru_a", "Boruxa"):
        r = await client.post(URL, json=_govde(kab, name=ad), headers=admin)
        assert r.status_code == 201, r.text

    async def _adlar(q: str) -> list[str]:
        items = (await client.get(URL, params={"q": q}, headers=admin)).json()["items"]
        return sorted(i["name"] for i in items)

    assert await _adlar("%") == ["Kalıp 100%"]  # kacissiz `%` her seyi eslerdi
    assert await _adlar("_") == ["Boru_a"]  # kacissiz `_` her tek karakteri eslerdi


async def test_yazma_denetim_satiri_yazar(client, admin, kab, db_session) -> None:
    created = await client.post(URL, json=_govde(kab), headers=admin)
    await client.patch(f"{URL}/{created.json()['id']}", json={"name": "Kalıp 2"}, headers=admin)
    details = list((await db_session.execute(select(AuditLog.detail))).scalars())
    assert any("KAB-0001" in d and "eklendi" in d for d in details)
    assert any("KAB-0001" in d and "güncellendi" in d for d in details)


async def _guncelleme_denetimleri(db_session) -> list[str]:
    rows = await db_session.execute(
        select(AuditLog.detail).where(
            AuditLog.detail.like("İş kalemi kataloğu kalemi güncellendi%")
        )
    )
    return sorted(rows.scalars())


async def test_fiyat_degisince_denetim_metni_eski_yeni_fiyati_icerir(
    client, admin, kab, db_session
) -> None:
    created = await client.post(URL, json=_govde(kab, ref_price="1250.50"), headers=admin)
    item_id = created.json()["id"]
    r = await client.patch(f"{URL}/{item_id}", json={"ref_price": "1300"}, headers=admin)
    assert r.status_code == 200, r.text
    assert await _guncelleme_denetimleri(db_session) == [
        "İş kalemi kataloğu kalemi güncellendi: KAB-0001 · Kalıp (m²)"
        " · referans fiyat 1,250.50 TL → 1,300.00 TL"
    ]
    await client.patch(f"{URL}/{item_id}", json={"ref_price": None}, headers=admin)
    assert any(
        d.endswith("referans fiyat 1,300.00 TL → —")
        for d in await _guncelleme_denetimleri(db_session)
    )


async def test_fiyat_yokken_atanirsa_eski_deger_tire(client, admin, kab, db_session) -> None:
    created = await client.post(URL, json=_govde(kab), headers=admin)
    await client.patch(f"{URL}/{created.json()['id']}", json={"ref_price": "99"}, headers=admin)
    assert await _guncelleme_denetimleri(db_session) == [
        "İş kalemi kataloğu kalemi güncellendi: KAB-0001 · Kalıp (m²) · referans fiyat — → 99.00 TL"
    ]


async def test_ayni_fiyat_farkli_yazimla_degisim_sayilmaz(client, admin, kab, db_session) -> None:
    created = await client.post(URL, json=_govde(kab, ref_price="1250.50"), headers=admin)
    item_id = created.json()["id"]
    # yalniz fiyat, farkli yazim → HICBIR alan degismedi → denetim satiri yok
    same = await client.patch(f"{URL}/{item_id}", json={"ref_price": "1250.5"}, headers=admin)
    assert same.status_code == 200 and Decimal(same.json()["ref_price"]) == Decimal("1250.50")
    assert await _guncelleme_denetimleri(db_session) == []
    # ad degisir, fiyat ayni (farkli yazim) → satir VAR ama fiyat ibaresi YOK
    await client.patch(
        f"{URL}/{item_id}", json={"name": "Kalıp 2", "ref_price": "1250.500"}, headers=admin
    )
    assert await _guncelleme_denetimleri(db_session) == [
        "İş kalemi kataloğu kalemi güncellendi: KAB-0001 · Kalıp 2 (m²)"
    ]


async def test_bos_ve_degisimsiz_patch_denetim_satiri_yazmaz(
    client, admin, kab, db_session
) -> None:
    created = await client.post(URL, json=_govde(kab), headers=admin)
    item_id = created.json()["id"]
    bos = await client.patch(f"{URL}/{item_id}", json={}, headers=admin)
    assert bos.status_code == 200 and bos.json()["id"] == item_id
    ayni = await client.patch(
        f"{URL}/{item_id}", json={"name": "Kalıp", "standard_unit_mhr": "1.80"}, headers=admin
    )
    assert ayni.status_code == 200
    assert await _guncelleme_denetimleri(db_session) == []
    # pozitif kontrol: gercek degisim satir yazar
    await client.patch(f"{URL}/{item_id}", json={"name": "Kalıp 3"}, headers=admin)
    assert len(await _guncelleme_denetimleri(db_session)) == 1


# ------------------------------------------------------------------- izin


async def test_contracts_view_okur_ama_yazamaz(
    client, admin, kab, db_session, user_factory
) -> None:
    created = await client.post(URL, json=_govde(kab), headers=admin)
    await modul_duzeyi_yaz(db_session, "accounting", "contracts", AccessLevel.view)
    muhasebe = await _giris(client, db_session, user_factory, "accounting")
    assert (await client.get(URL, headers=muhasebe)).status_code == 200
    assert (await client.post(URL, json=_govde(kab, name="Y"), headers=muhasebe)).status_code == 403
    patch = await client.patch(
        f"{URL}/{created.json()['id']}", json={"name": "Z"}, headers=muhasebe
    )
    assert patch.status_code == 403


@pytest.mark.parametrize("role_key", ["site_chief", "field_engineer"])
async def test_ev_view_olup_contracts_yok_roller_403(
    client, admin, kab, db_session, user_factory, role_key
) -> None:
    from app.modules.roles.seed_data import MATRIX, ROLE_ORDER

    sira = ROLE_ORDER.index(role_key)
    assert MATRIX["contracts"][sira][0] == AccessLevel.none  # on kosul: seed matrisi
    assert MATRIX["earned_value"][sira][0] != AccessLevel.none  # EV okuyabilen rol
    created = await client.post(URL, json=_govde(kab), headers=admin)
    kisi = await _giris(client, db_session, user_factory, role_key)

    assert (await client.get(URL, headers=kisi)).status_code == 403
    assert (await client.post(URL, json=_govde(kab, name="Y"), headers=kisi)).status_code == 403
    patch = await client.patch(f"{URL}/{created.json()['id']}", json={"name": "Z"}, headers=kisi)
    assert patch.status_code == 403
    # Pozitif kontrol: ayni rol EV KAT'i okuyabilir (403 kapi yuzunden, rol bozuk degil).
    assert (await client.get("/earned-value/catalog", headers=kisi)).status_code == 200


async def test_proje_basina_disiplinli_kullanici_katalogu_tam_gorur_ve_yazabilir(
    client, admin, kab, db_session, user_factory
) -> None:
    elk = await _disiplin(db_session, "ELK", "Elektrik")
    await client.post(URL, json=_govde(kab, name="Kalıp"), headers=admin)
    await client.post(URL, json=_govde(elk, name="Kablo"), headers=admin)
    token = await _login_with_access(
        client, db_session, user_factory, "project_manager", "pm.kisitli@tkl.co"
    )
    user_id = (
        await db_session.execute(select(User.id).where(User.email == "pm.kisitli@tkl.co"))
    ).scalar_one()
    await baska_projede_disiplinli(db_session, user_id, kab.id)
    kisitli = _auth(token)

    # IZN-B3: katalog sirket geneli — proje basina disiplin kisiti suzmez, yazmayi da engellemez.
    listed = (await client.get(URL, headers=kisitli)).json()["items"]
    assert sorted(i["poz_no"] for i in listed) == ["ELK-0001", "KAB-0001"]
    resp = await client.post(URL, json=_govde(kab, name="Yeni"), headers=kisitli)
    assert resp.status_code == 201, resp.text
    item_id = listed[0]["id"]
    assert (
        await client.patch(f"{URL}/{item_id}", json={"name": "Z"}, headers=kisitli)
    ).status_code == 200


# ----------------------------------------------------------- alan maskesi


async def test_limited_kapsamda_ref_price_gizli_all_kapsamda_gorunur(
    client, admin, kab, db_session, user_factory
) -> None:
    await client.post(URL, json=_govde(kab, ref_price="99.90"), headers=admin)
    # Izin satiri DOGRUDAN yazilir (`test_kapsam_yazma_kapisi` deseni): matriste atanabilir
    # limited+contracts hucresi yok; olculen sey maske ZINCIRI.
    await modul_duzeyi_yaz(db_session, "accounting", "contracts", AccessLevel.view)
    await rol_gizle(db_session, "accounting", HiddenCategory.sozlesme_fiyat)
    sinirli = await _giris(client, db_session, user_factory, "accounting")
    gizli = (await client.get(URL, headers=sinirli)).json()["items"][0]
    assert gizli["ref_price"] is None
    assert gizli["price_updated_at"] is None  # fiyatin zamani da fiyat bilgisidir
    assert gizli["standard_unit_mhr"] == "1.8000"  # para olmayan alan korunur
    assert gizli["poz_no"] == "KAB-0001"
    # Pozitif kontrol: kisitsiz kapsamda ayni kayit fiyatiyla doner.
    gorunur = (await client.get(URL, headers=admin)).json()["items"][0]
    assert Decimal(gorunur["ref_price"]) == Decimal("99.90")
    assert gorunur["price_updated_at"] is not None


async def test_fiyat_kategorisini_gizleyen_rol_olustururken_serbest_GUNCELLERKEN_YAZAMAZ(
    client, admin, kab, db_session, user_factory
) -> None:
    """IZN-B4a yazma kapısı: POST (oluşturma) gizli alanla SERBEST (yanıt maskeli); PATCH'te gizli
    kategorili alanı gönderen aktör 403, ilgisiz alanı güncelleyebilir."""
    await modul_duzeyi_yaz(db_session, "project_manager", "contracts", AccessLevel.full)
    await rol_gizle(db_session, "project_manager", HiddenCategory.sozlesme_fiyat)
    sinirli = await _giris(client, db_session, user_factory, "project_manager")
    resp = await client.post(URL, json=_govde(kab, ref_price="10.00"), headers=sinirli)
    assert resp.status_code == 201, resp.text
    assert resp.json()["ref_price"] is None  # yanıt maskeli
    kalem_id = resp.json()["id"]

    red = await client.patch(f"{URL}/{kalem_id}", json={"ref_price": "11.00"}, headers=sinirli)
    assert red.status_code == 403, red.text
    ok = await client.patch(f"{URL}/{kalem_id}", json={"uom": "m2"}, headers=sinirli)
    assert ok.status_code == 200, ok.text


# --------------------------------------------------------- EV sizinti bekcisi


async def test_EV_KAT_yanitinda_ref_price_ve_fiyat_yok(client, admin, kab) -> None:
    await client.post(URL, json=_govde(kab, ref_price="500.00"), headers=admin)
    rows = (await client.get("/earned-value/catalog", headers=admin)).json()
    assert len(rows) == 1
    assert "ref_price" not in rows[0]
    assert "price_updated_at" not in rows[0]
    assert "500.00" not in str(rows[0])
    ev_item = (
        await client.post(
            "/earned-value/catalog",
            json={
                "discipline_id": str(kab.id),
                "name": "Beton",
                "uom": "m³",
                "standard_unit_mhr": "2.0000",
                "default_contractor_type": "own",
            },
            headers=admin,
        )
    ).json()
    assert "ref_price" not in ev_item


# ------------------------------------------------------- /catalog/disciplines

DISC_URL = "/catalog/disciplines"


async def test_disiplin_listesi_admin_alanlar_ve_EV_ile_ayni_sira(
    client, admin, db_session
) -> None:
    await _disiplin(db_session, "ZZZ", "Son")
    await _disiplin(db_session, "AAA", "İlk")
    resp = await client.get(DISC_URL, headers=admin)
    assert resp.status_code == 200, resp.text
    items = resp.json()["items"]
    assert set(items[0]) == {
        "id",
        "code",
        "name",
        "color",
        "default_contractor_type",
        "sort_order",
    }
    assert items[0]["default_contractor_type"] == "own"
    ev = (await client.get("/earned-value/disciplines", headers=admin)).json()
    assert [i["id"] for i in items] == [d["id"] for d in ev]
    assert [i["code"] for i in items] == ["AAA", "ZZZ"]


@pytest.mark.parametrize("role_key", ["site_chief", "field_engineer"])
async def test_disiplin_listesi_contracts_yok_roller_403(
    client, db_session, seeded_db, user_factory, role_key
) -> None:
    kisi = await _giris(client, db_session, user_factory, role_key)
    assert (await client.get(DISC_URL, headers=kisi)).status_code == 403


async def test_disiplin_listesi_contracts_view_rolu_okur(
    client, db_session, seeded_db, user_factory
) -> None:
    await _disiplin(db_session, "KAB")
    await modul_duzeyi_yaz(db_session, "accounting", "contracts", AccessLevel.view)
    muhasebe = await _giris(client, db_session, user_factory, "accounting")
    resp = await client.get(DISC_URL, headers=muhasebe)
    assert resp.status_code == 200 and len(resp.json()["items"]) == 1


async def test_ozel_rol_contracts_view_EV_yok_cekirdek_200_EV_403(
    client, db_session, seeded_db, user_factory
) -> None:
    """Gerekce bekcisi: cekirdek uc EV izninden BAGIMSIZ; EV ucu hala kapali."""
    await _disiplin(db_session, "KAB")
    await modul_duzeyi_yaz(db_session, "procurement", "contracts", AccessLevel.view)
    await modul_duzeyi_yaz(db_session, "procurement", "earned_value", AccessLevel.none)
    await modul_duzeyi_yaz(db_session, "procurement", "user_management", AccessLevel.none)
    kisi = await _giris(client, db_session, user_factory, "procurement")
    assert (await client.get(DISC_URL, headers=kisi)).status_code == 200
    assert (await client.get("/earned-value/disciplines", headers=kisi)).status_code == 403


async def test_disiplin_listesi_proje_basina_disiplinli_kullaniciya_tam_gorunur(
    client, db_session, seeded_db, user_factory
) -> None:
    kab = await _disiplin(db_session, "KAB")
    await _disiplin(db_session, "ELK", "Elektrik")
    token = await _login_with_access(
        client, db_session, user_factory, "project_manager", "pm.disc@tkl.co"
    )
    user_id = (
        await db_session.execute(select(User.id).where(User.email == "pm.disc@tkl.co"))
    ).scalar_one()
    await baska_projede_disiplinli(db_session, user_id, kab.id)
    resp = await client.get(DISC_URL, headers=_auth(token))
    assert sorted(i["code"] for i in resp.json()["items"]) == ["ELK", "KAB"]
    ev = await client.get("/earned-value/disciplines", headers=_auth(token))
    assert sorted(i["code"] for i in ev.json()) == ["ELK", "KAB"]  # EV ile ayni davranis
