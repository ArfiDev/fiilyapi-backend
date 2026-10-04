"""BOQ — SİLME uçları: DELETE kalem (BE-B) ve DELETE grup (TB3-C).

`test_boq_api.py`nin parçalarından biri (800 satır tavanı bölmesi); paylaşılan
yardımcılar `_boq.py`dedir.

Grup silme, `delete_boq_item_*` ailesinin birebir emsalidir (kapı `_ADMIN`,
IDOR 404, denetim kaydı). TEK FARK: grup BOŞ değilse 409 `RelatedRecordsExistError`
döner. `BoqGroup`ta `parent_id` YOKTUR — hiyerarşi bulunmadığı için "boş" =
kalemi yok demektir.
"""

import uuid
from datetime import date
from decimal import Decimal

from app.core.access import AccessLevel
from app.modules.audit.models import AuditAction
from app.modules.boq.models import BoqGroup, BoqItem
from app.modules.site_diary.models import DiaryStatus, SiteDiaryEntry, SiteDiaryLine

from ._boq import (
    _audit_details,
    _auth,
    _group,
    _item,
    _login,
    _login_with_access,
    _set_permission,
    _site,
)


async def test_delete_boq_item_happy_path(client, db_session, user_factory, project_factory):
    project = await project_factory("BOQ-API-39")
    site = await _site(db_session, project)
    group = await _group(db_session, site)
    item = await _item(db_session, site, group, code="01.001")
    token = await _login(client, user_factory, "system_admin")

    resp = await client.delete(f"/boq/items/{item.id}", headers=_auth(token))

    assert resp.status_code == 204
    assert await db_session.get(BoqItem, item.id) is None


async def test_delete_boq_item_keeps_group_and_totals_consistent(
    client, db_session, user_factory, project_factory
):
    """Silinen kalemin grubu ayakta kalir, `grand_total` kalan kalemlere gore duser."""
    project = await project_factory("BOQ-API-40")
    site = await _site(db_session, project)
    group = await _group(db_session, site)
    kept = await _item(
        db_session,
        site,
        group,
        code="01.001",
        quantity=Decimal("2.000"),
        unit_price=Decimal("100.00"),
    )
    doomed = await _item(
        db_session,
        site,
        group,
        code="01.002",
        quantity=Decimal("3.000"),
        unit_price=Decimal("100.00"),
    )
    token = await _login(client, user_factory, "system_admin")

    before = await client.get(f"/sites/{site.id}/boq", headers=_auth(token))
    assert before.json()["totals"]["grand_total"] == "500.00"

    resp = await client.delete(f"/boq/items/{doomed.id}", headers=_auth(token))
    assert resp.status_code == 204

    after = await client.get(f"/sites/{site.id}/boq", headers=_auth(token))
    body = after.json()
    assert body["totals"]["grand_total"] == "200.00"
    assert len(body["groups"]) == 1
    assert body["groups"][0]["id"] == str(group.id)
    assert body["groups"][0]["group_total"] == "200.00"
    assert [i["id"] for i in body["groups"][0]["items"]] == [str(kept.id)]
    assert await db_session.get(BoqGroup, group.id) is not None


async def test_delete_boq_item_unauthenticated(client, db_session, project_factory):
    project = await project_factory("BOQ-API-41")
    site = await _site(db_session, project)
    group = await _group(db_session, site)
    item = await _item(db_session, site, group, code="01.001")

    resp = await client.delete(f"/boq/items/{item.id}")

    assert resp.status_code == 401


async def test_delete_boq_item_view_only_role_forbidden(
    client, db_session, user_factory, project_factory
):
    """site_chief: boq=view — silme yazma iznine baglidir (PATCH ile ayni kapi)."""
    project = await project_factory("BOQ-API-42")
    site = await _site(db_session, project)
    group = await _group(db_session, site)
    item = await _item(db_session, site, group, code="01.001")
    token = await _login_with_access(
        client, db_session, user_factory, "site_chief", "sc@boq-api-42.co"
    )

    resp = await client.delete(f"/boq/items/{item.id}", headers=_auth(token))

    assert resp.status_code == 403
    assert await db_session.get(BoqItem, item.id) is not None


async def test_delete_boq_item_full_level_role_forbidden(
    client, db_session, user_factory, project_factory
):
    """KULLANICI KARARI 2026-07-30: silme `boq:admin` ister, `full` YETMEZ.

    `full` seviyeli rol projeyi GORUR ve PATCH ile kalemi duzenleyebilir; buna
    karsin silemez (`app/core/access.py`: "full silmeyi KAPSAMAZ"). Kayit
    yerinde durur — 403 sonrasi hicbir yan etki olmamalidir.
    """
    project = await project_factory("BOQ-API-42B")
    site = await _site(db_session, project)
    group = await _group(db_session, site)
    item = await _item(db_session, site, group, code="01.001")
    await _set_permission(db_session, "patron", "boq", AccessLevel.full)
    token = await _login_with_access(
        client, db_session, user_factory, "patron", "pat@boq-api-42b.co"
    )

    patch = await client.patch(
        f"/boq/items/{item.id}", json={"description": "Yeni tarif"}, headers=_auth(token)
    )
    resp = await client.delete(f"/boq/items/{item.id}", headers=_auth(token))

    assert patch.status_code == 200, "on kosul: bu rol kalemi DUZENLEYEBILMELI"
    assert resp.status_code == 403
    assert await db_session.get(BoqItem, item.id) is not None


async def test_delete_boq_item_admin_seviyesi_yetmez_yalniz_sistem_yoneticisi(
    client, db_session, user_factory, project_factory
):
    """SIL-B1: `boq:admin` seviyesi silmeye YETMEZ — kapi rol ANAHTARI `system_admin`dir."""
    project = await project_factory("BOQ-API-42C")
    site = await _site(db_session, project)
    group = await _group(db_session, site)
    item = await _item(db_session, site, group, code="01.001")
    await _set_permission(db_session, "project_manager", "boq", AccessLevel.admin)
    token = await _login_with_access(
        client, db_session, user_factory, "project_manager", "pm@boq-api-42c.co"
    )

    resp = await client.delete(f"/boq/items/{item.id}", headers=_auth(token))

    assert resp.status_code == 403
    assert await db_session.get(BoqItem, item.id) is not None


async def test_delete_boq_item_invisible_returns_403_not_404(
    client, db_session, user_factory, project_factory
):
    """IDOR (spec §5.5, SIL-B1 sonrasi): kapi (Sistem Yoneticisi) handler'dan ONCE kosar; gorunmeyen
    kayit 403 alir (eskiden 404) ve kayit SILINMEZ. Var olmayan kimlik ile AYNI yanit.

    Aktorun `boq` izni testte acikca `admin`e cekilir (2026-07-30 karari): aksi
    hâlde 403 yetki kapisindan doner ve bu test gorunurluk suzgecini HIC sinamaz.
    Gorunurluk `projects` izninden gelir, `boq`dan degil — project_manager'in
    `projects` seviyesi `full` kaldigi icin erisim satiri olmadan projeyi goremez.
    """
    project = await project_factory("BOQ-API-43")
    site = await _site(db_session, project)
    group = await _group(db_session, site)
    item = await _item(db_session, site, group, code="01.001")
    await _set_permission(db_session, "project_manager", "boq", AccessLevel.admin)
    token = await _login(client, user_factory, "project_manager", "pm@boq-api-43.co")

    resp = await client.delete(f"/boq/items/{item.id}", headers=_auth(token))

    assert resp.status_code == 403
    assert resp.json()["detail"] == "Bu işlemi yalnızca Sistem Yöneticisi yapabilir"
    assert await db_session.get(BoqItem, item.id) is not None


async def test_delete_boq_item_missing_is_indistinguishable_from_invisible(
    client, db_session, user_factory, project_factory
):
    """Var olmayan UUID ile gorunmeyen kayit AYNI yaniti verir (varlik sizdirilmaz)."""
    project = await project_factory("BOQ-API-44")
    site = await _site(db_session, project)
    group = await _group(db_session, site)
    item = await _item(db_session, site, group, code="01.001")
    await _set_permission(db_session, "project_manager", "boq", AccessLevel.admin)
    token = await _login(client, user_factory, "project_manager", "pm@boq-api-44.co")

    invisible = await client.delete(f"/boq/items/{item.id}", headers=_auth(token))
    missing = await client.delete(f"/boq/items/{uuid.uuid4()}", headers=_auth(token))

    assert invisible.status_code == missing.status_code == 403
    assert invisible.json() == missing.json()


async def test_delete_boq_item_records_audit(client, db_session, user_factory, project_factory):
    project = await project_factory("BOQ-API-45")
    site = await _site(db_session, project)
    group = await _group(db_session, site)
    item = await _item(db_session, site, group, code="01.020", description="Silinecek tarif")
    token = await _login(client, user_factory, "system_admin")

    resp = await client.delete(f"/boq/items/{item.id}", headers=_auth(token))

    assert resp.status_code == 204
    details = await _audit_details(db_session, AuditAction.delete)
    assert any("01.020" in d and "Silinecek tarif" in d for d in details)


# --- TB3-C — DELETE /boq/groups/{group_id} (bos grup silme) ---
#
# `delete_boq_item_*` ailesinin birebir emsali (kapi `_ADMIN`, IDOR 404,
# denetim kaydi). TEK FARK: grup BOS degilse 409 `RelatedRecordsExistError`
# doner (`contracts.service.delete_employer_group` deseni). BoqGroup'ta
# `parent_id` YOKTUR — hiyerarsi bulunmadigi icin "bos" = kalemi yok demektir.


async def test_delete_boq_group_happy_path(client, db_session, user_factory, project_factory):
    project = await project_factory("BOQ-API-46")
    site = await _site(db_session, project)
    group = await _group(db_session, site, name="SILINECEK GRUP")
    token = await _login(client, user_factory, "system_admin")

    resp = await client.delete(f"/boq/groups/{group.id}", headers=_auth(token))

    assert resp.status_code == 204
    assert await db_session.get(BoqGroup, group.id) is None


async def test_delete_boq_group_with_items_returns_409(
    client, db_session, user_factory, project_factory
):
    """Kalemi olan grup SILINMEZ: cascade birakilirsa kalemler sessizce yok olur."""
    project = await project_factory("BOQ-API-47")
    site = await _site(db_session, project)
    group = await _group(db_session, site)
    item = await _item(db_session, site, group, code="01.001")
    token = await _login(client, user_factory, "system_admin")

    resp = await client.delete(f"/boq/groups/{group.id}", headers=_auth(token))

    assert resp.status_code == 409
    assert resp.json()["detail"] == "Bu grupta iş kalemi var, önce kalemleri silin"
    assert await db_session.get(BoqGroup, group.id) is not None
    assert await db_session.get(BoqItem, item.id) is not None


async def test_delete_boq_group_unauthenticated(client, db_session, project_factory):
    project = await project_factory("BOQ-API-48")
    site = await _site(db_session, project)
    group = await _group(db_session, site)

    resp = await client.delete(f"/boq/groups/{group.id}")

    assert resp.status_code == 401


async def test_delete_boq_group_full_level_role_forbidden(
    client, db_session, user_factory, project_factory
):
    """`full` silmeyi KAPSAMAZ (2026-07-30 karari) — PATCH gecer, DELETE 403."""
    project = await project_factory("BOQ-API-49")
    site = await _site(db_session, project)
    group = await _group(db_session, site, name="ESKI AD")
    await _set_permission(db_session, "patron", "boq", AccessLevel.full)
    token = await _login_with_access(
        client, db_session, user_factory, "patron", "pat@boq-api-49.co"
    )

    patch = await client.patch(
        f"/boq/groups/{group.id}", json={"name": "YENI AD"}, headers=_auth(token)
    )
    resp = await client.delete(f"/boq/groups/{group.id}", headers=_auth(token))

    assert patch.status_code == 200, "on kosul: bu rol grubu DUZENLEYEBILMELI"
    assert resp.status_code == 403
    assert await db_session.get(BoqGroup, group.id) is not None


async def test_delete_boq_group_admin_seviyesi_yetmez_yalniz_sistem_yoneticisi(
    client, db_session, user_factory, project_factory
):
    project = await project_factory("BOQ-API-50")
    site = await _site(db_session, project)
    group = await _group(db_session, site)
    await _set_permission(db_session, "project_manager", "boq", AccessLevel.admin)
    token = await _login_with_access(
        client, db_session, user_factory, "project_manager", "pm@boq-api-50.co"
    )

    resp = await client.delete(f"/boq/groups/{group.id}", headers=_auth(token))

    assert resp.status_code == 403
    assert await db_session.get(BoqGroup, group.id) is not None


async def test_delete_boq_group_invisible_returns_403_not_404(
    client, db_session, user_factory, project_factory
):
    """IDOR (SIL-B1): kapi once kosar; gorunmeyen grup 403 alir (eskiden 404)."""
    project = await project_factory("BOQ-API-51")
    site = await _site(db_session, project)
    group = await _group(db_session, site)
    await _set_permission(db_session, "project_manager", "boq", AccessLevel.admin)
    token = await _login(client, user_factory, "project_manager", "pm@boq-api-51.co")

    resp = await client.delete(f"/boq/groups/{group.id}", headers=_auth(token))

    assert resp.status_code == 403
    assert resp.json()["detail"] == "Bu işlemi yalnızca Sistem Yöneticisi yapabilir"
    assert await db_session.get(BoqGroup, group.id) is not None


async def test_delete_boq_group_missing_is_indistinguishable_from_invisible(
    client, db_session, user_factory, project_factory
):
    project = await project_factory("BOQ-API-52")
    site = await _site(db_session, project)
    group = await _group(db_session, site)
    await _set_permission(db_session, "project_manager", "boq", AccessLevel.admin)
    token = await _login(client, user_factory, "project_manager", "pm@boq-api-52.co")

    invisible = await client.delete(f"/boq/groups/{group.id}", headers=_auth(token))
    missing = await client.delete(f"/boq/groups/{uuid.uuid4()}", headers=_auth(token))

    assert invisible.status_code == missing.status_code == 403
    assert invisible.json() == missing.json()


async def test_delete_boq_group_records_audit(client, db_session, user_factory, project_factory):
    project = await project_factory("BOQ-API-53")
    site = await _site(db_session, project)
    group = await _group(db_session, site, name="KALDIRILAN GRUP")
    token = await _login(client, user_factory, "system_admin")

    resp = await client.delete(f"/boq/groups/{group.id}", headers=_auth(token))

    assert resp.status_code == 204
    details = await _audit_details(db_session, AuditAction.delete)
    assert any("KALDIRILAN GRUP" in d for d in details)


async def test_delete_boq_group_leaves_sibling_groups_intact(
    client, db_session, user_factory, project_factory
):
    """Silinen grup BOQ listesinden duser, kardes grup ve toplamlar bozulmaz."""
    project = await project_factory("BOQ-API-54")
    site = await _site(db_session, project)
    doomed = await _group(db_session, site, name="BOS GRUP", sort_order=1)
    kept = await _group(db_session, site, name="DOLU GRUP", sort_order=2)
    await _item(
        db_session,
        site,
        kept,
        code="01.001",
        quantity=Decimal("2.000"),
        unit_price=Decimal("100.00"),
    )
    token = await _login(client, user_factory, "system_admin")

    resp = await client.delete(f"/boq/groups/{doomed.id}", headers=_auth(token))
    assert resp.status_code == 204

    after = await client.get(f"/sites/{site.id}/boq", headers=_auth(token))
    body = after.json()
    assert [g["id"] for g in body["groups"]] == [str(kept.id)]
    assert body["totals"]["grand_total"] == "200.00"


# --- BOQ kalemi <-> gunluk satiri butunlugu (borc #53) ---------------------
#
# `site_diary_lines.boq_item_id` FK'si `ondelete="SET NULL"`dir
# (site_diary/models.py:209-215): korkuluk olmadan tek bir DELETE, o kaleme
# yazilmis GECMIS gunlerin satirlarini "bagi kopmus"a cevirir. O satirlar
# `site_diary/repository.py:249` ve `:279`daki INNER `join(BoqItem, ...)`
# okumalarindan DUSER — Hakedis Ozeti ve "gunlukten doldur" onerisi o ₺'yi
# artik saymaz — ama gunun DETAY ekrani snapshot kolonlariyla (code/description/
# unit/unit_price/quantity) satiri gostermeye devam eder. Iki ekran kalici
# olarak farkli ₺ soyler ve GONDERILMIS gunde duzeltme yolu yoktur:
# `apply_lines` yalniz `draft`ta kosar (site_diary/service.py) ve bagi kopmus
# satir govdeden ADRESLENEMEZ.


async def _diary_line(session, site, project, actor, item, quantity: Decimal = Decimal("12.000")):
    """Gonderilmis bir gune, verilen BOQ kalemine bagli tek satir yazar."""
    entry = SiteDiaryEntry(
        site_id=site.id,
        project_id=project.id,
        entry_date=date(2026, 3, 1),
        status=DiaryStatus.submitted,
        created_by=actor.id,
    )
    session.add(entry)
    await session.flush()
    line = SiteDiaryLine(
        entry_id=entry.id,
        boq_item_id=item.id,
        code=item.code,
        description=item.description,
        unit=item.unit,
        unit_price=item.unit_price,
        quantity=quantity,
    )
    session.add(line)
    await session.flush()
    return line


async def test_gunluk_satiri_olan_kalem_silinemez_409(
    client, db_session, user_factory, project_factory
):
    """Kaleme yazilmis gunluk satiri varsa DELETE 409 doner, bag KOPMAZ."""
    project = await project_factory("BOQ-API-53")
    site = await _site(db_session, project)
    group = await _group(db_session, site)
    item = await _item(db_session, site, group, code="01.001")
    actor = await user_factory(
        email="gunlukcu@boq-api.co", password="parola1234", role_key="system_admin"
    )
    line = await _diary_line(db_session, site, project, actor, item)
    token = await _login(client, user_factory, "system_admin")

    resp = await client.delete(f"/boq/items/{item.id}", headers=_auth(token))

    assert resp.status_code == 409
    assert await db_session.get(BoqItem, item.id) is not None
    await db_session.refresh(line)
    assert line.boq_item_id == item.id


async def test_gunluk_satiri_olmayan_kalem_hala_silinir_204(
    client, db_session, user_factory, project_factory
):
    """Regresyon: gunluk satiri BASKA kaleme yazilmissa hedef kalem silinebilir."""
    project = await project_factory("BOQ-API-54")
    site = await _site(db_session, project)
    group = await _group(db_session, site)
    used = await _item(db_session, site, group, code="01.001")
    doomed = await _item(db_session, site, group, code="01.002")
    actor = await user_factory(
        email="gunlukcu2@boq-api.co", password="parola1234", role_key="system_admin"
    )
    await _diary_line(db_session, site, project, actor, used)
    token = await _login(client, user_factory, "system_admin")

    resp = await client.delete(f"/boq/items/{doomed.id}", headers=_auth(token))

    assert resp.status_code == 204
    assert await db_session.get(BoqItem, doomed.id) is None
    assert await db_session.get(BoqItem, used.id) is not None
