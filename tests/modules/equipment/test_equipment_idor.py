"""MK-1 T3 — 🔴 K20 görünürlüğü: `visible_projects` süzgeci + depo istisnası.

Spec §3 K20: ekipman bir şantiyeye atanır ve maliyeti bir projeye yansır,
dolayısıyla süzgeç UYGULANIR (`personnel`/`payroll`ün şirket-geneli istisnası
BURADA GEÇERSİZ). `site_id IS NULL` (depodaki) ekipman HERKESE görünür.

Süzgeç HER uçta ayrı ayrı kilitlenir: liste · detay · PATCH · summary. Bir ucun
atlanması yeter, çünkü sızıntı tek uçtan olur.
"""

import uuid

import pytest
from sqlalchemy import select

from app.core.access import AccessLevel, Scope
from app.modules.roles.models import Module, Role, RolePermission
from tests._legacy_permission_yardimcisi import sync_page_cells


async def _set_permission(
    session, role_key: str, module_key: str, level: AccessLevel, scope: Scope = Scope.all
) -> None:
    """İzin kapısını seed matrisinden BAĞIMSIZ kılar (ST IDOR deseni)."""
    role_id = (await session.execute(select(Role.id).where(Role.key == role_key))).scalar_one()
    module_id = (
        await session.execute(select(Module.id).where(Module.key == module_key))
    ).scalar_one()
    permission = (
        await session.execute(
            select(RolePermission).where(
                RolePermission.role_id == role_id, RolePermission.module_id == module_id
            )
        )
    ).scalar_one()
    permission.access_level = level
    permission.scope = scope
    await session.flush()
    await sync_page_cells(session, permission.role_id)


@pytest.mark.asyncio
async def test_gorunmeyen_projenin_ekipmani_listede_yok(
    client, admin_headers, sef_headers, ekipman_fabrikasi, gorunen_santiye, gorunmeyen_santiye
):
    gorunen = await ekipman_fabrikasi("Tower Crane TC-48", site=gorunen_santiye)
    await ekipman_fabrikasi("Gizli Ekskavatör", site=gorunmeyen_santiye)

    yanit = await client.get("/equipment", headers=sef_headers)
    assert yanit.status_code == 200, yanit.text
    govde = yanit.json()
    assert [e["id"] for e in govde["items"]] == [str(gorunen.id)]
    assert govde["total"] == 1, "`total` de süzgeçten geçer, tabloyu saymaz"

    hepsi = await client.get("/equipment", headers=admin_headers)
    assert hepsi.json()["total"] == 2


@pytest.mark.asyncio
async def test_depodaki_ekipman_izni_olan_herkese_gorunur(
    client, sef_headers, muhendis_headers, ekipman_fabrikasi
):
    """🔴 K20 istisnası: `site_id IS NULL` hiçbir projeye ait değildir; proje
    kapsamına tabi tutulsaydı depodaki makineyi HİÇ KİMSE göremezdi."""
    depodaki = await ekipman_fabrikasi("Kompresör SC-200")

    for basliklar in (sef_headers, muhendis_headers):
        yanit = await client.get("/equipment", headers=basliklar)
        assert yanit.status_code == 200, yanit.text
        assert [e["id"] for e in yanit.json()["items"]] == [str(depodaki.id)]

    detay = await client.get(f"/equipment/{depodaki.id}", headers=sef_headers)
    assert detay.status_code == 200, detay.text


@pytest.mark.asyncio
async def test_gorunmeyen_ekipman_detayda_404_ve_ayirt_edilemez(
    client, sef_headers, ekipman_fabrikasi, gorunmeyen_santiye
):
    """403 DEĞİL 404: elinde kimlik olan kullanıcı kaydın VAR OLDUĞUNU
    öğrenmemelidir; gövde var olmayan kimliğinkiyle BİREBİR aynıdır."""
    gizli = await ekipman_fabrikasi("Gizli Ekskavatör", site=gorunmeyen_santiye)

    gorunmez = await client.get(f"/equipment/{gizli.id}", headers=sef_headers)
    olmayan = await client.get(f"/equipment/{uuid.uuid4()}", headers=sef_headers)
    assert gorunmez.status_code == 404, gorunmez.text
    assert olmayan.status_code == 404
    assert gorunmez.json() == olmayan.json()


@pytest.mark.asyncio
async def test_gorunmeyen_ekipman_patchte_404(
    client, sef_headers, ekipman_fabrikasi, gorunmeyen_santiye
):
    gizli = await ekipman_fabrikasi("Gizli Ekskavatör", site=gorunmeyen_santiye)

    gorunmez = await client.patch(
        f"/equipment/{gizli.id}", json={"brand": "X"}, headers=sef_headers
    )
    olmayan = await client.patch(
        f"/equipment/{uuid.uuid4()}", json={"brand": "X"}, headers=sef_headers
    )
    assert gorunmez.status_code == 404, gorunmez.text
    assert olmayan.status_code == 404
    assert gorunmez.json() == olmayan.json()


@pytest.mark.asyncio
async def test_yetki_gorunurlugun_onune_gecmez(
    client, seeded_db, sef_headers, ekipman_fabrikasi, gorunmeyen_santiye
):
    """`equipment:admin` TAŞIYAN ama projeyi görmeyen kullanıcı da 404 alır —
    "yetkiliyse söyleyebiliriz" kestirmesi yetkili hesabı keşif aracına
    çevirirdi (ST IDOR dersi)."""
    gizli = await ekipman_fabrikasi("Gizli Ekskavatör", site=gorunmeyen_santiye)
    await _set_permission(seeded_db, "site_chief", "equipment", AccessLevel.admin)

    yanit = await client.get(f"/equipment/{gizli.id}", headers=sef_headers)
    assert yanit.status_code == 404, yanit.text


@pytest.mark.asyncio
async def test_ozette_gorunmeyen_ekipman_sayilmaz(
    client, sef_headers, ekipman_fabrikasi, gorunen_santiye, gorunmeyen_santiye
):
    """🔴 Süzgeç summary ucunda da koşar: sayaç sızıntısı da bir sızıntıdır
    (görünmeyen projenin filo büyüklüğü ele verilir)."""
    from app.modules.equipment.models import EquipmentStatus

    await ekipman_fabrikasi("Görünen", site=gorunen_santiye, status=EquipmentStatus.working)
    await ekipman_fabrikasi("Gizli-1", site=gorunmeyen_santiye, status=EquipmentStatus.working)
    await ekipman_fabrikasi("Gizli-2", site=gorunmeyen_santiye, status=EquipmentStatus.broken)

    yanit = await client.get("/equipment/summary", headers=sef_headers)
    assert yanit.status_code == 200, yanit.text
    govde = yanit.json()
    assert govde["working"] == 1
    assert govde["broken"] == 0


@pytest.mark.asyncio
async def test_gorunmeyen_makinenin_calisma_kaydi_gorunmez(
    client, sef_headers, admin_headers, ekipman_fabrikasi, gorunen_santiye, gorunmeyen_santiye
):
    """🔴 K20 · T4: çalışma kaydı KENDİ `site_id`siyle süzülür (K9) ve
    görünmeyen kayıt listede YOKTUR."""
    gorunen = await ekipman_fabrikasi("Görünen Vinç", site=gorunen_santiye)
    gizli = await ekipman_fabrikasi("Gizli Vinç", site=gorunmeyen_santiye)

    for makine in (gorunen, gizli):
        yanit = await client.post(
            "/equipment/work-logs",
            json={
                "equipment_id": str(makine.id),
                "work_date": "2026-07-17",
                "site_id": str(makine.site_id),
                "hours": "8",
            },
            headers=admin_headers,
        )
        assert yanit.status_code == 201, yanit.text

    liste = await client.get("/equipment/work-logs", headers=sef_headers)
    assert liste.status_code == 200, liste.text
    govde = liste.json()
    assert govde["total"] == 1
    assert govde["items"][0]["equipment_id"] == str(gorunen.id)


@pytest.mark.asyncio
async def test_gorunmeyen_calisma_kaydinin_detayi_ve_yazmasi_404(
    client, sef_headers, admin_headers, ekipman_fabrikasi, gorunmeyen_santiye
):
    """Detay · PATCH · DELETE üçü de AYNI 404'ü döner; sızıntı tek uçtan olur."""
    gizli = await ekipman_fabrikasi("Gizli Vinç", site=gorunmeyen_santiye)
    kayit = await client.post(
        "/equipment/work-logs",
        json={
            "equipment_id": str(gizli.id),
            "work_date": "2026-07-17",
            "site_id": str(gizli.site_id),
            "hours": "8",
        },
        headers=admin_headers,
    )
    kayit_id = kayit.json()["id"]

    olmayan = await client.get(f"/equipment/work-logs/{uuid.uuid4()}", headers=sef_headers)
    gorunmez = await client.get(f"/equipment/work-logs/{kayit_id}", headers=sef_headers)
    assert gorunmez.status_code == 404, gorunmez.text
    assert gorunmez.json() == olmayan.json()

    duzeltme = await client.patch(
        f"/equipment/work-logs/{kayit_id}", json={"hours": "5"}, headers=sef_headers
    )
    silme = await client.delete(f"/equipment/work-logs/{kayit_id}", headers=sef_headers)
    assert duzeltme.status_code == 404, duzeltme.text
    assert silme.status_code == 403, silme.text  # SIL-B1: kapı önce; varlık sızmaz


@pytest.mark.asyncio
async def test_gorunmeyen_makineye_calisma_kaydi_acilamaz_404(
    client, sef_headers, ekipman_fabrikasi, gorunen_santiye, gorunmeyen_santiye
):
    """Gövdedeki `equipment_id` ve `site_id` ikisi de süzgeçten geçer."""
    gizli = await ekipman_fabrikasi("Gizli Vinç", site=gorunmeyen_santiye)
    gorunen = await ekipman_fabrikasi("Görünen Vinç", site=gorunen_santiye)

    gizli_makineye = await client.post(
        "/equipment/work-logs",
        json={"equipment_id": str(gizli.id), "work_date": "2026-07-17", "hours": "8"},
        headers=sef_headers,
    )
    assert gizli_makineye.status_code == 404, gizli_makineye.text

    gizli_santiyeye = await client.post(
        "/equipment/work-logs",
        json={
            "equipment_id": str(gorunen.id),
            "work_date": "2026-07-17",
            "site_id": str(gorunmeyen_santiye.id),
            "hours": "8",
        },
        headers=sef_headers,
    )
    assert gizli_santiyeye.status_code == 404, gizli_santiyeye.text


@pytest.mark.asyncio
async def test_depodaki_makinenin_kaydi_herkese_gorunur(
    client, sef_headers, muhendis_headers, ekipman_fabrikasi
):
    """K20 depo istisnası çalışma kaydında da geçerlidir: `site_id IS NULL`
    kayıt hiçbir projeye ait değildir."""
    depodaki = await ekipman_fabrikasi("Kompresör SC-200")
    kayit = await client.post(
        "/equipment/work-logs",
        json={"equipment_id": str(depodaki.id), "work_date": "2026-07-17", "hours": "8"},
        headers=sef_headers,
    )
    assert kayit.status_code == 201, kayit.text

    for basliklar in (sef_headers, muhendis_headers):
        liste = await client.get("/equipment/work-logs", headers=basliklar)
        assert [k["id"] for k in liste.json()["items"]] == [kayit.json()["id"]]


@pytest.mark.asyncio
async def test_gorunmeyen_santiyeye_atama_404(
    client, sef_headers, ekipman_fabrikasi, gorunen_santiye, gorunmeyen_santiye
):
    """Gövdedeki `site_id` de süzgeçten geçer: geçmeseydi kullanıcı kendi
    makinesini görmediği bir projeye taşıyıp kaydı kendinden gizleyebilirdi."""
    yanit = await client.post(
        "/equipment",
        json={"name": "Yeni", "category": "crane", "site_id": str(gorunmeyen_santiye.id)},
        headers=sef_headers,
    )
    assert yanit.status_code == 404, yanit.text

    makine = await ekipman_fabrikasi("Mevcut", site=gorunen_santiye)
    tasima = await client.patch(
        f"/equipment/{makine.id}",
        json={"site_id": str(gorunmeyen_santiye.id)},
        headers=sef_headers,
    )
    assert tasima.status_code == 404, tasima.text
