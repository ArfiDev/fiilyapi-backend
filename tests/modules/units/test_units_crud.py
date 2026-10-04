"""B6-B7 — ünite YAZMA + blok/ünite SİLME uçları (spec §7.5, §7.6, §7.9, §3.3).

`test_units_api.py`nin parçalarından biri (800 satır tavanı bölmesi); paylaşılan
yardımcılar `_units_api.py`dedir.

Silme uçlarında görünmeyen kayıt, var olmayandan AYIRT EDİLEMEZ olmalıdır ve
blok silme, altında ünite varken **409** ile reddedilir — hata mesajı ünite
sayısını SIZDIRMAZ.
"""

import uuid
from decimal import Decimal

from app.core.access import AccessLevel
from tests._silme_yardimci import sil_aile

from ._units_api import (
    _auth,
    _block,
    _block_exists,
    _count_units_in_block,
    _login,
    _login_with_access,
    _set_permission,
    _site,
    _unit,
)


async def test_create_unit_happy_path_201(client, db_session, user_factory, project_factory):
    project = await project_factory("B6-1", project_type="kendi_yatirim")
    site = await _site(db_session, project)
    block = await _block(db_session, project, site, name="A Blok")
    token = await _login(client, user_factory, "system_admin")

    resp = await client.post(
        f"/projects/{project.id}/units",
        json={
            "block_id": str(block.id),
            "unit_no": "12",
            "unit_kind": "apartment",
            "layout": "3+1",
            "gross_area_m2": "142.00",
            "net_area_m2": "120.00",
            "list_price": "1150000.00",
        },
        headers=_auth(token),
    )

    assert resp.status_code == 201
    body = resp.json()
    assert body["unit_no"] == "12"
    assert body["block_name"] == "A Blok"
    assert body["label"] == "A Blok · 12"
    assert body["unit_price_per_m2"] == "8098.59"
    assert body["owner_side"] is None
    assert body["is_landowner_share"] is False


async def test_create_unit_duplicate_no_in_block_returns_409(
    client, db_session, user_factory, project_factory
):
    project = await project_factory("B6-2")
    site = await _site(db_session, project)
    block = await _block(db_session, project, site)
    token = await _login(client, user_factory, "system_admin")
    payload = {"block_id": str(block.id), "unit_no": "1", "unit_kind": "apartment"}

    first = await client.post(f"/projects/{project.id}/units", json=payload, headers=_auth(token))
    second = await client.post(f"/projects/{project.id}/units", json=payload, headers=_auth(token))

    assert first.status_code == 201
    assert second.status_code == 409
    assert second.json()["detail"] == "Bu ünite numarası bu blokta zaten kullanılıyor"


async def test_create_unit_same_no_other_block_returns_201(
    client, db_session, user_factory, project_factory
):
    """SY 76/106: A Blok "1" ile B Blok "1" AYNI ANDA vardir."""
    project = await project_factory("B6-3")
    site = await _site(db_session, project)
    block_a = await _block(db_session, project, site, name="A Blok")
    block_b = await _block(db_session, project, site, name="B Blok")
    token = await _login(client, user_factory, "system_admin")

    first = await client.post(
        f"/projects/{project.id}/units",
        json={"block_id": str(block_a.id), "unit_no": "1", "unit_kind": "apartment"},
        headers=_auth(token),
    )
    second = await client.post(
        f"/projects/{project.id}/units",
        json={"block_id": str(block_b.id), "unit_no": "1", "unit_kind": "apartment"},
        headers=_auth(token),
    )

    assert first.status_code == 201
    assert second.status_code == 201


async def test_create_unit_foreign_block_returns_404(
    client, db_session, user_factory, project_factory
):
    """IDOR-9: govdedeki `block_id` baska projenin blogu olabilir."""
    project = await project_factory("B6-4A")
    await _site(db_session, project, code="S-OWN")
    other = await project_factory("B6-4B")
    other_site = await _site(db_session, other, code="S-FOREIGN")
    foreign_block = await _block(db_session, other, other_site, name="A Blok")
    token = await _login(client, user_factory, "system_admin")

    resp = await client.post(
        f"/projects/{project.id}/units",
        json={"block_id": str(foreign_block.id), "unit_no": "1", "unit_kind": "apartment"},
        headers=_auth(token),
    )

    assert resp.status_code == 404
    assert resp.json()["detail"] == "Blok bulunamadı"


async def test_create_unit_in_taahhut_project_returns_201(
    client, db_session, user_factory, project_factory
):
    """§3.3: `taahhut` projede unite tanimlamak SERBEST — kisit icat edilmedi."""
    project = await project_factory("B6-5", project_type="taahhut")
    site = await _site(db_session, project)
    block = await _block(db_session, project, site)
    token = await _login(client, user_factory, "system_admin")

    resp = await client.post(
        f"/projects/{project.id}/units",
        json={"block_id": str(block.id), "unit_no": "1", "unit_kind": "apartment"},
        headers=_auth(token),
    )

    assert resp.status_code == 201


async def test_create_unit_owner_side_in_kendi_yatirim_returns_422(
    client, db_session, user_factory, project_factory
):
    """§3.3: `owner_side` YALNIZ kat karsiligi projede dolu olabilir."""
    project = await project_factory("B6-6", project_type="kendi_yatirim")
    site = await _site(db_session, project)
    block = await _block(db_session, project, site)
    token = await _login(client, user_factory, "system_admin")

    resp = await client.post(
        f"/projects/{project.id}/units",
        json={
            "block_id": str(block.id),
            "unit_no": "1",
            "unit_kind": "apartment",
            "owner_side": "landowner",
        },
        headers=_auth(token),
    )

    assert resp.status_code == 422
    assert resp.json()["detail"] == "Ünite payı yalnızca kat karşılığı projelerde belirlenebilir"


async def test_create_unit_appraisal_value_in_kendi_yatirim_returns_201(
    client, db_session, user_factory, project_factory
):
    """§3.3/§4.4: iki fiyat sutunu da HER TIPTE kabul edilir — reddetmek mockup'ta
    olmayan bir kisit icat etmek olurdu."""
    project = await project_factory("B6-7", project_type="kendi_yatirim")
    site = await _site(db_session, project)
    block = await _block(db_session, project, site)
    token = await _login(client, user_factory, "system_admin")

    resp = await client.post(
        f"/projects/{project.id}/units",
        json={
            "block_id": str(block.id),
            "unit_no": "1",
            "unit_kind": "apartment",
            "appraisal_value": "900000.00",
        },
        headers=_auth(token),
    )

    assert resp.status_code == 201
    assert resp.json()["appraisal_value"] == "900000.00"


async def test_create_unit_net_greater_than_gross_returns_422(
    client, db_session, user_factory, project_factory
):
    """DB CHECK'e (ck_units_net_le_gross) DUSMEDEN servis Turkce mesaj verir."""
    project = await project_factory("B6-8")
    site = await _site(db_session, project)
    block = await _block(db_session, project, site)
    token = await _login(client, user_factory, "system_admin")

    resp = await client.post(
        f"/projects/{project.id}/units",
        json={
            "block_id": str(block.id),
            "unit_no": "1",
            "unit_kind": "apartment",
            "gross_area_m2": "100.00",
            "net_area_m2": "120.00",
        },
        headers=_auth(token),
    )

    assert resp.status_code == 422
    assert resp.json()["detail"] == "Net alan brüt alandan büyük olamaz"


async def test_create_unit_requires_full_permission(
    client, db_session, user_factory, project_factory
):
    project = await project_factory("B6-9")
    site = await _site(db_session, project)
    block = await _block(db_session, project, site)
    token = await _login_with_access(client, db_session, user_factory, "site_chief")

    resp = await client.post(
        f"/projects/{project.id}/units",
        json={"block_id": str(block.id), "unit_no": "1", "unit_kind": "apartment"},
        headers=_auth(token),
    )

    assert resp.status_code == 403


# --- B6: PATCH /units/{id} (spec §7.6) ---


async def test_patch_unit_partial_leaves_unsent_fields(
    client, db_session, user_factory, project_factory
):
    """ "Gonderilmedi" alani DEGISTIRMEZ (`model_fields_set` ayrimi)."""
    project = await project_factory("B6-10", project_type="kendi_yatirim")
    site = await _site(db_session, project)
    block = await _block(db_session, project, site)
    unit = await _unit(
        db_session, project, block, "1", layout="3+1", list_price=Decimal("1000000.00")
    )
    token = await _login(client, user_factory, "system_admin")

    resp = await client.patch(
        f"/units/{unit.id}", json={"list_price": "1200000.00"}, headers=_auth(token)
    )

    assert resp.status_code == 200
    assert resp.json()["list_price"] == "1200000.00"
    assert resp.json()["layout"] == "3+1"


async def test_patch_unit_null_clears_layout(client, db_session, user_factory, project_factory):
    """ "null yapildi" ile "gonderilmedi" AYNI SEY DEGILDIR."""
    project = await project_factory("B6-11")
    site = await _site(db_session, project)
    block = await _block(db_session, project, site)
    unit = await _unit(db_session, project, block, "1", layout="3+1")
    token = await _login(client, user_factory, "system_admin")

    resp = await client.patch(f"/units/{unit.id}", json={"layout": None}, headers=_auth(token))

    assert resp.status_code == 200
    assert resp.json()["layout"] is None


async def test_patch_unit_net_gt_gross_returns_422(
    client, db_session, user_factory, project_factory
):
    """Mevcut brut ile GONDERILEN net karsilastirilir — kismi gonderim tuzagi."""
    project = await project_factory("B6-12")
    site = await _site(db_session, project)
    block = await _block(db_session, project, site)
    unit = await _unit(db_session, project, block, "1", gross_area_m2=Decimal("100.00"))
    token = await _login(client, user_factory, "system_admin")

    resp = await client.patch(
        f"/units/{unit.id}", json={"net_area_m2": "120.00"}, headers=_auth(token)
    )

    assert resp.status_code == 422
    assert resp.json()["detail"] == "Net alan brüt alandan büyük olamaz"


async def test_patch_unit_moves_to_other_block(client, db_session, user_factory, project_factory):
    """Unite yanlis bloga girilmisse tasinabilir (spec §7.6)."""
    project = await project_factory("B6-13")
    site = await _site(db_session, project)
    block_a = await _block(db_session, project, site, name="A Blok")
    block_b = await _block(db_session, project, site, name="B Blok")
    unit = await _unit(db_session, project, block_a, "1")
    token = await _login(client, user_factory, "system_admin")

    resp = await client.patch(
        f"/units/{unit.id}", json={"block_id": str(block_b.id)}, headers=_auth(token)
    )

    assert resp.status_code == 200
    assert resp.json()["block_id"] == str(block_b.id)
    assert resp.json()["block_name"] == "B Blok"
    assert resp.json()["label"] == "B Blok · 1"


async def test_patch_unit_move_with_unit_no_conflict_returns_409(
    client, db_session, user_factory, project_factory
):
    """Hedef blokta ayni `unit_no` varsa tasima reddedilir."""
    project = await project_factory("B6-14")
    site = await _site(db_session, project)
    block_a = await _block(db_session, project, site, name="A Blok")
    block_b = await _block(db_session, project, site, name="B Blok")
    unit = await _unit(db_session, project, block_a, "1")
    await _unit(db_session, project, block_b, "1")
    token = await _login(client, user_factory, "system_admin")

    resp = await client.patch(
        f"/units/{unit.id}", json={"block_id": str(block_b.id)}, headers=_auth(token)
    )

    assert resp.status_code == 409
    assert resp.json()["detail"] == "Bu ünite numarası bu blokta zaten kullanılıyor"


async def test_patch_unit_move_to_foreign_block_returns_404(
    client, db_session, user_factory, project_factory
):
    """Hedef blok BASKA projede ise 404 — proje sinirini asan tasima yoktur."""
    project = await project_factory("B6-15A")
    site = await _site(db_session, project, code="S-OWN")
    block = await _block(db_session, project, site, name="A Blok")
    unit = await _unit(db_session, project, block, "1")
    other = await project_factory("B6-15B")
    other_site = await _site(db_session, other, code="S-FOREIGN")
    foreign_block = await _block(db_session, other, other_site, name="A Blok")
    token = await _login(client, user_factory, "system_admin")

    resp = await client.patch(
        f"/units/{unit.id}", json={"block_id": str(foreign_block.id)}, headers=_auth(token)
    )

    assert resp.status_code == 404
    assert resp.json()["detail"] == "Blok bulunamadı"


async def test_patch_unit_invisible_returns_404(client, db_session, user_factory, project_factory):
    """IDOR-4: gorunmeyen projenin unitesi 404 — mesaj UNITE icin ozeldir."""
    project = await project_factory("B6-16")
    site = await _site(db_session, project)
    block = await _block(db_session, project, site)
    unit = await _unit(db_session, project, block, "1")
    token = await _login(client, user_factory, "patron")

    resp = await client.patch(f"/units/{unit.id}", json={"layout": "2+1"}, headers=_auth(token))

    assert resp.status_code == 404
    assert resp.json()["detail"] == "Ünite bulunamadı"


async def test_patch_unit_unknown_uuid_returns_404_same_message(client, user_factory):
    """IDOR-7: var olmayan unite ile gorunmeyen unite AYIRT EDILEMEZ."""
    token = await _login(client, user_factory, "system_admin")

    resp = await client.patch(
        f"/units/{uuid.uuid4()}", json={"layout": "2+1"}, headers=_auth(token)
    )

    assert resp.status_code == 404
    assert resp.json()["detail"] == "Ünite bulunamadı"


async def test_patch_unit_owner_side_in_kendi_yatirim_returns_422(
    client, db_session, user_factory, project_factory
):
    """§3.3 korkulugu PATCH'te de gecerlidir — POST'tan kacan yol kapali."""
    project = await project_factory("B6-17", project_type="kendi_yatirim")
    site = await _site(db_session, project)
    block = await _block(db_session, project, site)
    unit = await _unit(db_session, project, block, "1")
    token = await _login(client, user_factory, "system_admin")

    resp = await client.patch(
        f"/units/{unit.id}", json={"owner_side": "contractor"}, headers=_auth(token)
    )

    assert resp.status_code == 422
    assert resp.json()["detail"] == "Ünite payı yalnızca kat karşılığı projelerde belirlenebilir"


# --- B7: DELETE /units/{id} (spec §7.9) ---
#
# VERI KAYBI SINIFI. Bu bolumun testleri UC DUZEYINDEN kosar (HTTP DELETE), yani
# dogruladiklari yol servis korkulugu → ORM `session.delete(...)` yoludur.
# DB duzeyindeki `ON DELETE RESTRICT` ayrica `test_units_models.py::
# test_block_delete_restricted_when_units_exist` ile ham ORM silme uzerinden
# dogrulanir — iki katman, iki ayri test. Modelde `relationship(cascade=...)`
# TANIMLI DEGILDIR, bu yuzden ORM'in kendiliginden unite silme yolu YOKTUR.


async def test_delete_unit_returns_204(client, db_session, user_factory, project_factory):
    project = await project_factory("B7-1")
    site = await _site(db_session, project)
    block = await _block(db_session, project, site)
    unit = await _unit(db_session, project, block, "1")
    token = await _login(client, user_factory, "system_admin")

    resp = await sil_aile(client, _auth(token), "unit", unit.id)

    assert resp.status_code == 204
    assert await _count_units_in_block(db_session, block.id) == 0


async def test_delete_unit_twice_returns_404(client, db_session, user_factory, project_factory):
    """Silinen unite ARTIK YOKTUR: ikinci istek 404, 204 degil (idempotent
    gorunumu kullaniciya "hâlâ duruyor mu?" sorusunu birakirdi)."""
    project = await project_factory("B7-2")
    site = await _site(db_session, project)
    block = await _block(db_session, project, site)
    unit = await _unit(db_session, project, block, "1")
    token = await _login(client, user_factory, "system_admin")

    first = await sil_aile(client, _auth(token), "unit", unit.id)
    second = await sil_aile(client, _auth(token), "unit", unit.id)

    assert first.status_code == 204
    assert second.status_code == 404
    assert second.json()["detail"] == "Ünite bulunamadı"


async def test_delete_unit_invisible_returns_403_indistinguishable_from_unknown(
    client, db_session, user_factory, project_factory
):
    """IDOR-6 (2026-07-30 karari sonrasi): silme `projects:admin` ister.

    `full` seviyeli `patron` artik YETKI KAPISINDA durur (403) ve gorunurluk
    suzgecine hic ulasmaz. Sizinti YOKTUR: ayni rol var olmayan bir UUID icin de
    birebir ayni 403'u alir, yani 403 kaydin varligi hakkinda hicbir sey soylemez.
    Gorunmeyen kaydin **404** dondugu davranis `guards.visible_unit` uzerinde
    aynen durur ve PATCH ucunda (hâlâ `full`) sinanir — bkz. `test_units_idor`.
    """
    project = await project_factory("B7-3")
    site = await _site(db_session, project)
    block = await _block(db_session, project, site)
    unit = await _unit(db_session, project, block, "1")
    token = await _login(client, user_factory, "patron")

    resp = await sil_aile(client, _auth(token), "unit", unit.id)
    unknown = await sil_aile(client, _auth(token), "unit", uuid.uuid4())

    assert resp.status_code == unknown.status_code == 403
    assert resp.json() == unknown.json()
    assert await _count_units_in_block(db_session, block.id) == 1


async def test_delete_unit_unknown_uuid_returns_404_same_message(client, user_factory):
    """IDOR-7: var olmayan unite ile gorunmeyen unite AYIRT EDILEMEZ."""
    token = await _login(client, user_factory, "system_admin")

    resp = await sil_aile(client, _auth(token), "unit", uuid.uuid4())

    assert resp.status_code == 404
    assert resp.json()["detail"] == "Ünite bulunamadı"


async def test_delete_unit_view_permission_forbidden(
    client, db_session, user_factory, project_factory
):
    """Spec §8: silme yazma iznine baglidir; `view` SILEMEZ (IDOR-13)."""
    project = await project_factory("B7-4")
    site = await _site(db_session, project)
    block = await _block(db_session, project, site)
    unit = await _unit(db_session, project, block, "1")
    token = await _login_with_access(client, db_session, user_factory, "site_chief")

    resp = await sil_aile(client, _auth(token), "unit", unit.id)

    assert resp.status_code == 403
    assert await _count_units_in_block(db_session, block.id) == 1


async def test_delete_unit_full_permission_forbidden(
    client, db_session, user_factory, project_factory
):
    """KULLANICI KARARI 2026-07-30: silme `projects:admin` ister, `full` YETMEZ.

    `full` seviyeli rol uniteyi PATCH ile duzenleyebilir ama silemez
    (`app/core/access.py`: "full silmeyi KAPSAMAZ — silme yalnizca admin").
    """
    project = await project_factory("B7-4B")
    site = await _site(db_session, project)
    block = await _block(db_session, project, site)
    unit = await _unit(db_session, project, block, "1")
    token = await _login_with_access(client, db_session, user_factory, "patron")

    patch = await client.patch(f"/units/{unit.id}", json={"unit_no": "9"}, headers=_auth(token))
    resp = await sil_aile(client, _auth(token), "unit", unit.id)

    assert patch.status_code == 200, "on kosul: bu rol uniteyi DUZENLEYEBILMELI"
    assert resp.status_code == 403
    assert await _count_units_in_block(db_session, block.id) == 1


async def test_delete_unit_admin_seviyesi_yetmez_yalniz_sistem_yoneticisi_siler(
    client, db_session, user_factory, project_factory
):
    """SIL-B1: `projects:admin` seviyesi silmeye YETMEZ — kapı rol ANAHTARI `system_admin`dir."""
    project = await project_factory("B7-4C")
    site = await _site(db_session, project)
    block = await _block(db_session, project, site)
    unit = await _unit(db_session, project, block, "1")
    await _set_permission(db_session, "project_manager", "projects", AccessLevel.admin)
    token = await _login_with_access(client, db_session, user_factory, "project_manager")

    resp = await sil_aile(client, _auth(token), "unit", unit.id)

    assert resp.status_code == 403
    assert await _count_units_in_block(db_session, block.id) == 1


# --- B7: DELETE /blocks/{id} (spec §7.9) ---


async def test_delete_block_with_units_deletes_units_too(
    client, db_session, user_factory, project_factory
):
    """SIL-B1: eski "ünitesi olan blok 409" kuralı Sistem Yöneticisi için BYPASS edilir; ünite
    (RESTRICT çocuk) önce, blok sonra silinir."""
    project = await project_factory("B7-5")
    site = await _site(db_session, project)
    block = await _block(db_session, project, site)
    await _unit(db_session, project, block, "1")
    token = await _login(client, user_factory, "system_admin")

    resp = await sil_aile(client, _auth(token), "block", block.id)

    assert resp.status_code == 204
    assert await _block_exists(db_session, block.id) is False
    assert await _count_units_in_block(db_session, block.id) == 0


async def test_delete_block_leaves_other_blocks_units_intact(
    client, db_session, user_factory, project_factory
):
    """KANIT TESTI: 24 daireli blok silinince YALNIZ onun üniteleri gider; yan bloğunki durur."""
    project = await project_factory("B7-6")
    site = await _site(db_session, project)
    block = await _block(db_session, project, site)
    other = await _block(db_session, project, site, "B Blok")
    for no in (str(n) for n in range(1, 25)):
        await _unit(db_session, project, block, no)
    await _unit(db_session, project, other, "1")
    token = await _login(client, user_factory, "system_admin")
    assert await _count_units_in_block(db_session, block.id) == 24

    resp = await sil_aile(client, _auth(token), "block", block.id)

    assert resp.status_code == 204
    assert await _count_units_in_block(db_session, block.id) == 0
    assert await _count_units_in_block(db_session, other.id) == 1
    assert await _block_exists(db_session, other.id) is True


async def test_delete_empty_block_returns_204(client, db_session, user_factory, project_factory):
    project = await project_factory("B7-8")
    site = await _site(db_session, project)
    block = await _block(db_session, project, site)
    token = await _login(client, user_factory, "system_admin")

    resp = await sil_aile(client, _auth(token), "block", block.id)

    assert resp.status_code == 204
    assert await _block_exists(db_session, block.id) is False


async def test_delete_block_invisible_returns_403_indistinguishable_from_unknown(
    client, db_session, user_factory, project_factory
):
    """IDOR-6 (2026-07-30 karari sonrasi): silme `projects:admin` ister.

    `full` seviyeli `patron` yetki kapisinda durur; 403 var olmayan UUID icin de
    birebir aynidir, dolayisiyla kaydin varligi sizmaz. Gorunurluk suzgecinin
    **404** davranisi degismedi — PATCH ucunda (hâlâ `full`) sinanir.
    """
    project = await project_factory("B7-10")
    site = await _site(db_session, project)
    block = await _block(db_session, project, site)
    token = await _login(client, user_factory, "patron")

    resp = await sil_aile(client, _auth(token), "block", block.id)
    unknown = await sil_aile(client, _auth(token), "block", uuid.uuid4())

    assert resp.status_code == unknown.status_code == 403
    assert resp.json() == unknown.json()
    assert await _block_exists(db_session, block.id) is True


async def test_delete_block_unknown_uuid_returns_404_same_message(client, user_factory):
    """IDOR-7: var olmayan blok ile gorunmeyen blok AYIRT EDILEMEZ."""
    token = await _login(client, user_factory, "system_admin")

    resp = await sil_aile(client, _auth(token), "block", uuid.uuid4())

    assert resp.status_code == 404
    assert resp.json()["detail"] == "Blok bulunamadı"


async def test_delete_block_view_permission_forbidden(
    client, db_session, user_factory, project_factory
):
    project = await project_factory("B7-11")
    site = await _site(db_session, project)
    block = await _block(db_session, project, site)
    token = await _login_with_access(client, db_session, user_factory, "site_chief")

    resp = await sil_aile(client, _auth(token), "block", block.id)

    assert resp.status_code == 403
    assert await _block_exists(db_session, block.id) is True


async def test_delete_block_full_permission_forbidden(
    client, db_session, user_factory, project_factory
):
    """KULLANICI KARARI 2026-07-30: silme `projects:admin` ister, `full` YETMEZ."""
    project = await project_factory("B7-11B")
    site = await _site(db_session, project)
    block = await _block(db_session, project, site)
    token = await _login_with_access(client, db_session, user_factory, "patron")

    patch = await client.patch(f"/blocks/{block.id}", json={"name": "Z Blok"}, headers=_auth(token))
    resp = await sil_aile(client, _auth(token), "block", block.id)

    assert patch.status_code == 200, "on kosul: bu rol blogu DUZENLEYEBILMELI"
    assert resp.status_code == 403
    assert await _block_exists(db_session, block.id) is True


async def test_delete_block_admin_seviyesi_yetmez_yalniz_sistem_yoneticisi_siler(
    client, db_session, user_factory, project_factory
):
    """SIL-B1: kapı seviyede DEĞİL, rol anahtarında: `projects:admin` olsa da 403."""
    project = await project_factory("B7-11C")
    site = await _site(db_session, project)
    block = await _block(db_session, project, site)
    await _set_permission(db_session, "project_manager", "projects", AccessLevel.admin)
    token = await _login_with_access(client, db_session, user_factory, "project_manager")

    resp = await sil_aile(client, _auth(token), "block", block.id)

    assert resp.status_code == 403
    assert await _block_exists(db_session, block.id) is True


# --- P3.1 §0.B: kodu NULL olan blokta ANLIK turetme (spec §3.2, karar 8) ---
