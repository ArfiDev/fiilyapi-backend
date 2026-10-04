"""T10/T11 — `DELETE /sites/{site_id}` ve `DELETE /sections/{section_id}` (spec §7.1, §12.3).

## Bu dosya neden VERI KAYBI SINIFI bir test setidir

`sites.id`'yi hedefleyen **dort FK'nin de `ON DELETE CASCADE`** oldugu koddan
dogrulandi (`sections`, `boq_groups`, `boq_items`, `blocks`). Yani DB
KENDILIGINDEN KORUMAZ: korkuluksuz tek bir `DELETE /sites/{id}` cagrisi
bolumleri, poz gruplarini, poz kalemlerini ve bloklari SESSIZCE yok eder ve bu
GERI ALINAMAZ.

Bu yuzden **409 donmesi tek basina kanit DEGILDIR**: 409 doner ama silme yine de
gerceklesirse (ornegin korkuluk `session.delete`ten SONRA kosarsa) hata gorunur,
veri gitmistir. Kanit, engellenen denemeden SONRA **dort tablonun da sayiminin
degismemis olmasidir** — asagida `_counts` yardimcisi tam olarak bunu olcer ve
engelleme testlerinin hepsi oncesi/sonrasi esitligini dogrular.
"""

import uuid
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from app.core.access import AccessLevel
from app.modules.audit.models import AuditAction, AuditLog
from app.modules.boq.models import BoqGroup, BoqItem
from app.modules.documents.models.core import Document, DocumentFolder
from app.modules.personnel.models import Personnel
from app.modules.roles.models import Module, Role, RolePermission
from app.modules.site_diary.models import SiteDiaryEntry, WorkerSource
from app.modules.site_planning.models import (
    PlanGoalStatus,
    PlanResourceKind,
    SitePlanGoal,
    SitePlanRow,
    SitePlanSprint,
)
from app.modules.sites.models import Section, Site
from app.modules.timesheet.models import TimesheetEntry
from app.modules.units.models import Block, Unit, UnitKind
from tests._legacy_permission_yardimcisi import sync_page_cells

# Spec §7.2 — silme korkuluklarinin Turkce metinleri (testte BIREBIR beklenir).
SECTION_BLOCKER = "Bu şantiyede bölüm var, önce bölümleri silin"
BOQ_BLOCKER = "Bu şantiyede iş kalemi var, önce iş kalemlerini silin"
BLOCK_BLOCKER = "Bu şantiyede blok var, önce blokları silin"
# 59/60 — CASCADE'li ama KORKULUKSUZ yedi FK icin eklenen dort yeni metin.
TIMESHEET_BLOCKER = "Bu şantiyede puantaj kaydı var, önce puantaj kayıtlarını silin"
DIARY_BLOCKER = "Bu şantiyede şantiye günlüğü var, önce günlükleri silin"
DOCUMENT_BLOCKER = "Bu şantiyede belge arşivi kaydı var, önce belge ve klasörleri silin"
PLAN_BLOCKER = "Bu şantiyede plan ızgarası var, önce planı temizleyin"
SITE_MISSING = "Şantiye bulunamadı"
SECTION_MISSING = "Bölüm bulunamadı"


async def _login(client, user_factory, role_key: str = "system_admin") -> str:
    """Silme kapisi `sites:admin` — seed matrisinde YALNIZ `system_admin`'de var."""
    address = f"{role_key}-del@t.co"
    await user_factory(email=address, password="parola1234", role_key=role_key)
    resp = await client.post("/auth/login", json={"email": address, "password": "parola1234"})
    return resp.json()["access_token"]


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _site(session, project, code: str = "A-BLOK", **kwargs) -> Site:
    site = Site(project_id=project.id, code=code, name=kwargs.pop("name", "A-Blok Şantiyesi"))
    for field, value in kwargs.items():
        setattr(site, field, value)
    session.add(site)
    await session.flush()
    return site


async def _section(session, site, name: str = "Kaba İnşaat", **kwargs) -> Section:
    section = Section(site_id=site.id, name=name, **kwargs)
    session.add(section)
    await session.flush()
    return section


async def _group(session, site, name: str = "TOPRAK VE TEMEL İŞLERİ") -> BoqGroup:
    group = BoqGroup(site_id=site.id, name=name)
    session.add(group)
    await session.flush()
    return group


async def _item(session, site, group, code: str = "01.001") -> BoqItem:
    item = BoqItem(
        site_id=site.id,
        group_id=group.id,
        code=code,
        description="Kazı (Makine ile)",
        unit="m³",
        quantity=Decimal("1240.000"),
        unit_price=Decimal("280.00"),
    )
    session.add(item)
    await session.flush()
    return item


async def _block(session, project, site, name: str = "A Blok") -> Block:
    block = Block(project_id=project.id, site_id=site.id, name=name)
    session.add(block)
    await session.flush()
    return block


async def _unit(session, project, block, unit_no: str = "1") -> Unit:
    unit = Unit(
        project_id=project.id,
        block_id=block.id,
        unit_no=unit_no,
        unit_kind=UnitKind.apartment,
    )
    session.add(unit)
    await session.flush()
    return unit


async def _counts(session, site_id: uuid.UUID) -> dict[str, int]:
    """CASCADE'in tetiklenmedigini kanitlayan olcum (§0.1).

    `sites.id`'ye CASCADE ile bagli TUM tablolarin SANTIYE KAPSAMINDA sayimi +
    santiyenin kendisi. `units` ayrica sayilir: `units.block_id` RESTRICT oldugu
    icin blok cascade'i patlar, ama bu KAZA sonucu bir korumadir — olculmeden
    guvenilmez.

    🔴 59/60 dersi: bu yardimci UZUN SURE yalniz sections/boq/blocks/units
    sayiyordu ve tam da bu yuzden puantaj/gunluk/belge/plan bacaginda YAPISAL
    OLARAK KORDU — o tablolar sayilmadigi icin "cascade tetiklenmedi" iddiasi o
    bacaklarda hicbir sey bekciliyemiyordu.
    """

    async def _count(model, column, value) -> int:
        stmt = select(func.count()).select_from(model).where(column == value)
        return int((await session.execute(stmt)).scalar_one())

    block_ids = (
        (await session.execute(select(Block.id).where(Block.site_id == site_id))).scalars().all()
    )
    unit_count = 0
    if block_ids:
        stmt = select(func.count()).select_from(Unit).where(Unit.block_id.in_(block_ids))
        unit_count = int((await session.execute(stmt)).scalar_one())
    return {
        "sites": await _count(Site, Site.id, site_id),
        "sections": await _count(Section, Section.site_id, site_id),
        "boq_groups": await _count(BoqGroup, BoqGroup.site_id, site_id),
        "boq_items": await _count(BoqItem, BoqItem.site_id, site_id),
        "blocks": await _count(Block, Block.site_id, site_id),
        "units": unit_count,
        "timesheet_entries": await _count(TimesheetEntry, TimesheetEntry.site_id, site_id),
        "diary_entries": await _count(SiteDiaryEntry, SiteDiaryEntry.site_id, site_id),
        "plan_rows": await _count(SitePlanRow, SitePlanRow.site_id, site_id),
        "plan_goals": await _count(SitePlanGoal, SitePlanGoal.site_id, site_id),
        "plan_sprints": await _count(SitePlanSprint, SitePlanSprint.site_id, site_id),
        "documents": await _count(Document, Document.site_id, site_id),
        "document_folders": await _count(DocumentFolder, DocumentFolder.site_id, site_id),
    }


# --- 59/60 fabrikalari: BOLUMSUZ santiyeye bagli CASCADE satirlari ---
#
# Hepsi `section_id` VERMEDEN yazilir (NULLABLE, SET NULL): kaydin tam da
# iddiasi "bolumu olmayan santiye ilk korkuluga takilmaz" oldugu icin bu
# satirlarin bolumsuz kurulabilmesi testin ON KOSULUDUR.


async def _personnel(session, full_name: str = "Ahmet Yılmaz") -> Personnel:
    personnel = Personnel(full_name=full_name, source=WorkerSource.company)
    session.add(personnel)
    await session.flush()
    return personnel


async def _timesheet(session, project, site, personnel, user) -> TimesheetEntry:
    entry = TimesheetEntry(
        personnel_id=personnel.id,
        site_id=site.id,
        project_id=project.id,
        work_date=date(2026, 3, 2),
        hours=Decimal("8.0"),
        created_by=user.id,
    )
    session.add(entry)
    await session.flush()
    return entry


async def _diary(session, project, site, user) -> SiteDiaryEntry:
    entry = SiteDiaryEntry(
        site_id=site.id,
        project_id=project.id,
        entry_date=date(2026, 3, 2),
        created_by=user.id,
    )
    session.add(entry)
    await session.flush()
    return entry


async def _document(session, project, site) -> Document:
    document = Document(
        project_id=project.id,
        site_id=site.id,
        filename="isg-tutanak.pdf",
        mime_type="application/pdf",
        size_bytes=1024,
    )
    session.add(document)
    await session.flush()
    return document


async def _folder(session, project, site, name: str = "İSG") -> DocumentFolder:
    folder = DocumentFolder(project_id=project.id, site_id=site.id, name=name)
    session.add(folder)
    await session.flush()
    return folder


async def _plan_row(session, project, site) -> SitePlanRow:
    """EKIPMAN satiri: `section_id` bu turde ZATEN NULL'dur (models.py:120-125)."""
    row = SitePlanRow(
        site_id=site.id,
        project_id=project.id,
        kind=PlanResourceKind.equipment,
        label="Tower Crane",
    )
    session.add(row)
    await session.flush()
    return row


async def _plan_goal(session, project, site) -> SitePlanGoal:
    goal = SitePlanGoal(
        site_id=site.id,
        project_id=project.id,
        week_start=date(2026, 3, 2),
        title="Temel betonu",
        status=PlanGoalStatus.in_progress,
    )
    session.add(goal)
    await session.flush()
    return goal


async def _plan_sprint(session, site) -> SitePlanSprint:
    sprint = SitePlanSprint(site_id=site.id, name="Mart Sprinti")
    session.add(sprint)
    await session.flush()
    return sprint


# --- S1: bos santiye silinir ---


async def test_delete_empty_site_returns_204(client, db_session, user_factory, project_factory):
    project = await project_factory("D-1")
    site = await _site(db_session, project)
    token = await _login(client, user_factory)

    resp = await client.delete(f"/sites/{site.id}", headers=_auth(token))

    assert resp.status_code == 204
    follow_up = await client.get(f"/sites/{site.id}", headers=_auth(token))
    assert follow_up.status_code == 404
    assert follow_up.json()["detail"] == SITE_MISSING


async def test_delete_returns_204_with_empty_body(
    client, db_session, user_factory, project_factory
):
    project = await project_factory("D-2")
    site = await _site(db_session, project)
    token = await _login(client, user_factory)

    resp = await client.delete(f"/sites/{site.id}", headers=_auth(token))

    assert resp.status_code == 204
    assert resp.content == b""


# --- S2: bolum engeli ---


async def test_delete_site_with_section_returns_409(
    client, db_session, user_factory, project_factory
):
    project = await project_factory("D-3")
    site = await _site(db_session, project)
    await _section(db_session, site)
    token = await _login(client, user_factory)

    resp = await client.delete(f"/sites/{site.id}", headers=_auth(token))

    assert resp.status_code == 409
    assert resp.json()["detail"] == SECTION_BLOCKER


async def test_delete_site_with_section_leaves_site_and_sections_intact(
    client, db_session, user_factory, project_factory
):
    """S2'nin ASIL amaci: 409 gordukten sonra HICBIR SEY silinmemis olmali.

    Durum kodu tek basina kanit degildir; sayim esitligi cascade'in
    tetiklenmediginin tek gercek kanitidir.
    """
    project = await project_factory("D-4")
    site = await _site(db_session, project)
    await _section(db_session, site, "Kaba İnşaat")
    await _section(db_session, site, "İnce İşler")
    token = await _login(client, user_factory)
    before = await _counts(db_session, site.id)

    resp = await client.delete(f"/sites/{site.id}", headers=_auth(token))

    assert resp.status_code == 409
    assert await _counts(db_session, site.id) == before
    assert before["sites"] == 1
    assert before["sections"] == 2


# --- S3: poz engeli ---


async def test_delete_site_with_boq_item_returns_409(
    client, db_session, user_factory, project_factory
):
    project = await project_factory("D-5")
    site = await _site(db_session, project)
    group = await _group(db_session, site)
    await _item(db_session, site, group)
    token = await _login(client, user_factory)
    before = await _counts(db_session, site.id)

    resp = await client.delete(f"/sites/{site.id}", headers=_auth(token))

    assert resp.status_code == 409
    assert resp.json()["detail"] == BOQ_BLOCKER
    assert await _counts(db_session, site.id) == before
    assert before["boq_items"] == 1
    assert before["boq_groups"] == 1


async def test_delete_site_with_boq_group_only_returns_409(
    client, db_session, user_factory, project_factory
):
    """Kalemsiz GRUP da tek basina engeldir (spec §7.1: `boq_items` VEYA `boq_groups`)."""
    project = await project_factory("D-6")
    site = await _site(db_session, project)
    await _group(db_session, site)
    token = await _login(client, user_factory)
    before = await _counts(db_session, site.id)

    resp = await client.delete(f"/sites/{site.id}", headers=_auth(token))

    assert resp.status_code == 409
    assert resp.json()["detail"] == BOQ_BLOCKER
    assert await _counts(db_session, site.id) == before
    assert before["boq_items"] == 0
    assert before["boq_groups"] == 1


# --- S4: blok engeli ---


async def test_delete_site_with_block_returns_409(
    client, db_session, user_factory, project_factory
):
    project = await project_factory("D-7")
    site = await _site(db_session, project)
    block = await _block(db_session, project, site)
    await _unit(db_session, project, block, "1")
    await _unit(db_session, project, block, "2")
    token = await _login(client, user_factory)
    before = await _counts(db_session, site.id)

    resp = await client.delete(f"/sites/{site.id}", headers=_auth(token))

    assert resp.status_code == 409
    assert resp.json()["detail"] == BLOCK_BLOCKER
    assert await _counts(db_session, site.id) == before
    assert before["blocks"] == 1
    assert before["units"] == 2


# --- S5: taslaga AYRICALIK YOK ---


async def test_delete_draft_site_with_section_returns_409(
    client, db_session, user_factory, project_factory
):
    """ "Taslak zaten yarim, gitsin" kisayolu YAZILMAZ (spec §7.1)."""
    project = await project_factory("D-8")
    site = await _site(db_session, project, is_draft=True)
    await _section(db_session, site)
    token = await _login(client, user_factory)
    before = await _counts(db_session, site.id)

    resp = await client.delete(f"/sites/{site.id}", headers=_auth(token))

    assert resp.status_code == 409
    assert resp.json()["detail"] == SECTION_BLOCKER
    assert await _counts(db_session, site.id) == before


# --- Sira ve mesaj disiplini ---


async def test_delete_stops_at_first_blocker(client, db_session, user_factory, project_factory):
    """Bolum + poz + blok birlikteyken kullaniciya TEK, eyleme donuk mesaj doner."""
    project = await project_factory("D-9")
    site = await _site(db_session, project)
    await _section(db_session, site)
    group = await _group(db_session, site)
    await _item(db_session, site, group)
    await _block(db_session, project, site)
    token = await _login(client, user_factory)
    before = await _counts(db_session, site.id)

    resp = await client.delete(f"/sites/{site.id}", headers=_auth(token))

    assert resp.status_code == 409
    assert resp.json()["detail"] == SECTION_BLOCKER
    assert BOQ_BLOCKER not in resp.text
    assert BLOCK_BLOCKER not in resp.text
    assert await _counts(db_session, site.id) == before


async def test_delete_error_message_omits_counts(client, db_session, user_factory, project_factory):
    """`BLOCK_HAS_UNITS` dersi: hata govdesi gorunurluk disi bilgi (adet) TASIMAZ."""
    project = await project_factory("D-10")
    site = await _site(db_session, project)
    for index in range(3):
        await _section(db_session, site, f"Faz {index}")
    token = await _login(client, user_factory)

    resp = await client.delete(f"/sites/{site.id}", headers=_auth(token))

    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail == SECTION_BLOCKER
    assert not any(character.isdigit() for character in detail)


# --- S9: korkuluk KALICI KILIT uretmiyor ---


async def test_delete_after_removing_sections_returns_204(
    client, db_session, user_factory, project_factory
):
    project = await project_factory("D-11")
    site = await _site(db_session, project)
    section = await _section(db_session, site)
    token = await _login(client, user_factory)

    blocked = await client.delete(f"/sites/{site.id}", headers=_auth(token))
    assert blocked.status_code == 409

    await db_session.delete(section)
    await db_session.flush()
    # ORM kimlik haritasi tazelensin: aksi hâlde santiyenin `sections`
    # koleksiyonu bayat kalir ve silinmis satiri ikinci kez silmeye calisir.
    await db_session.refresh(site, attribute_names=["sections"])

    resp = await client.delete(f"/sites/{site.id}", headers=_auth(token))

    assert resp.status_code == 204
    assert (await _counts(db_session, site.id))["sites"] == 0


# --- 404 / 409 ayrimi ---


async def test_delete_missing_site_returns_404(client, user_factory):
    token = await _login(client, user_factory)

    resp = await client.delete(f"/sites/{uuid.uuid4()}", headers=_auth(token))

    assert resp.status_code == 404
    assert resp.json()["detail"] == SITE_MISSING


@pytest.mark.xfail(
    strict=True,
    reason="IZN-B2: DELETE `admin` kapısı yalnız Sistem Yöneticisi; admin hücreli özel rol "
    "silemez. SIL-B1 testi sysadmin aktörüne çevirecek (SIL hattında).",
)
async def test_delete_invisible_site_returns_same_404_body(
    client, db_session, user_factory, project_factory
):
    """Gorunmeyen GERCEK santiye ile var olmayan UUID AYIRT EDILEMEZ (§7.1 ortak kural).

    Aksi hâlde elinde UUID olan kullanici kaydin hâlâ var oldugunu ve baska bir
    projeye ait oldugunu ogrenirdi.
    """
    project = await project_factory("D-12")
    site = await _site(db_session, project)
    # `patron` sites=full tasir; silme kapisi icin admin'e cekilir ama proje
    # erisimi VERILMEZ — boylece gorunurluk suzgeci tek basina sinanir.
    role_id = (await db_session.execute(select(Role.id).where(Role.key == "patron"))).scalar_one()
    module_id = (
        await db_session.execute(select(Module.id).where(Module.key == "sites"))
    ).scalar_one()
    permission = (
        await db_session.execute(
            select(RolePermission).where(
                RolePermission.role_id == role_id, RolePermission.module_id == module_id
            )
        )
    ).scalar_one()
    permission.access_level = AccessLevel.admin
    await db_session.flush()
    await sync_page_cells(db_session, permission.role_id)
    await user_factory(email="patron-del@t.co", password="parola1234", role_key="patron")
    login = await client.post(
        "/auth/login", json={"email": "patron-del@t.co", "password": "parola1234"}
    )
    token = login.json()["access_token"]

    invisible = await client.delete(f"/sites/{site.id}", headers=_auth(token))
    unknown = await client.delete(f"/sites/{uuid.uuid4()}", headers=_auth(token))

    assert invisible.status_code == unknown.status_code == 404
    assert invisible.json() == unknown.json() == {"detail": SITE_MISSING}


# --- T11: DELETE /sections/{section_id} ---


async def test_delete_section_returns_204(client, db_session, user_factory, project_factory):
    """S8. Santiye ve DIGER bolumler yerinde kalir."""
    project = await project_factory("E-1")
    site = await _site(db_session, project)
    first = await _section(db_session, site, "Kaba İnşaat", sort_order=0)
    second = await _section(db_session, site, "İnce İşler", sort_order=1)
    token = await _login(client, user_factory)

    resp = await client.delete(f"/sections/{first.id}", headers=_auth(token))

    assert resp.status_code == 204
    remaining = await _counts(db_session, site.id)
    assert remaining["sites"] == 1
    assert remaining["sections"] == 1
    assert await db_session.get(Section, second.id) is not None


async def test_delete_section_twice_returns_404(client, db_session, user_factory, project_factory):
    project = await project_factory("E-2")
    site = await _site(db_session, project)
    section = await _section(db_session, site)
    token = await _login(client, user_factory)

    first = await client.delete(f"/sections/{section.id}", headers=_auth(token))
    second = await client.delete(f"/sections/{section.id}", headers=_auth(token))

    assert first.status_code == 204
    assert second.status_code == 404
    assert second.json()["detail"] == SECTION_MISSING


async def test_delete_section_does_not_touch_site(
    client, db_session, user_factory, project_factory
):
    project = await project_factory("E-3")
    site = await _site(db_session, project)
    section = await _section(db_session, site)
    token = await _login(client, user_factory)

    resp = await client.delete(f"/sections/{section.id}", headers=_auth(token))

    assert resp.status_code == 204
    assert (await _counts(db_session, site.id))["sites"] == 1
    assert await db_session.get(Site, site.id) is not None


async def test_delete_section_reorders_nothing(client, db_session, user_factory, project_factory):
    """DAVRANIS KILIDI: kalan bolumlerin `sort_order` degerleri YENIDEN NUMARALANMAZ.

    Yeniden numaralandirmak sessiz bir surpriz olurdu: kullanici bir bolumu
    silince digerlerinin sirasi da degisirdi.
    """
    project = await project_factory("E-4")
    site = await _site(db_session, project)
    first = await _section(db_session, site, "Faz 1", sort_order=0)
    second = await _section(db_session, site, "Faz 2", sort_order=1)
    third = await _section(db_session, site, "Faz 3", sort_order=2)
    token = await _login(client, user_factory)

    resp = await client.delete(f"/sections/{first.id}", headers=_auth(token))

    assert resp.status_code == 204
    await db_session.refresh(second)
    await db_session.refresh(third)
    assert [second.sort_order, third.sort_order] == [1, 2]


async def test_delete_section_returns_empty_body(client, db_session, user_factory, project_factory):
    project = await project_factory("E-5")
    site = await _site(db_session, project)
    section = await _section(db_session, site)
    token = await _login(client, user_factory)

    resp = await client.delete(f"/sections/{section.id}", headers=_auth(token))

    assert resp.status_code == 204
    assert resp.content == b""


# --- T14: S7 ve S9'un poz/blok varyantlari ---
#
# S7 ve S9 SECTION engelinde kanitlanmisti; korkuluk UC AYRI dala sahiptir ve her
# dal kendi 409'unu kendi noktasinda atar. Yalniz ilk dali sinamak, poz veya blok
# dalinin `session.delete`ten SONRA calismasi hâlinde sessiz veri kaybini
# gormezden gelmek olurdu.


async def test_failed_delete_on_boq_blocker_writes_no_audit(
    client, db_session, user_factory, project_factory
):
    """S7 — poz dalinda reddedilen silme de gunluge satir DUSURMEZ."""
    project = await project_factory("F-1")
    site = await _site(db_session, project)
    group = await _group(db_session, site)
    await _item(db_session, site, group)
    token = await _login(client, user_factory)

    resp = await client.delete(f"/sites/{site.id}", headers=_auth(token))

    assert resp.status_code == 409
    assert resp.json()["detail"] == BOQ_BLOCKER
    rows = (
        await db_session.execute(
            select(func.count()).select_from(AuditLog).where(AuditLog.action == AuditAction.delete)
        )
    ).scalar_one()
    assert rows == 0


async def test_failed_delete_on_block_blocker_writes_no_audit(
    client, db_session, user_factory, project_factory
):
    """S7 — blok dalinda reddedilen silme de gunluge satir DUSURMEZ."""
    project = await project_factory("F-2")
    site = await _site(db_session, project)
    await _block(db_session, project, site)
    token = await _login(client, user_factory)

    resp = await client.delete(f"/sites/{site.id}", headers=_auth(token))

    assert resp.status_code == 409
    assert resp.json()["detail"] == BLOCK_BLOCKER
    rows = (
        await db_session.execute(
            select(func.count()).select_from(AuditLog).where(AuditLog.action == AuditAction.delete)
        )
    ).scalar_one()
    assert rows == 0


async def test_delete_after_removing_boq_returns_204(
    client, db_session, user_factory, project_factory
):
    """S9 (poz varyanti) — poz korkulugu KALICI KILIT uretmiyor."""
    project = await project_factory("F-3")
    site = await _site(db_session, project)
    group = await _group(db_session, site)
    item = await _item(db_session, site, group)
    token = await _login(client, user_factory)

    blocked = await client.delete(f"/sites/{site.id}", headers=_auth(token))
    assert blocked.status_code == 409
    assert blocked.json()["detail"] == BOQ_BLOCKER

    await db_session.delete(item)
    await db_session.delete(group)
    await db_session.flush()

    resp = await client.delete(f"/sites/{site.id}", headers=_auth(token))

    assert resp.status_code == 204, resp.text
    assert (await _counts(db_session, site.id))["sites"] == 0


async def test_delete_after_removing_blocks_returns_204(
    client, db_session, user_factory, project_factory
):
    """S9 (blok varyanti) — blok korkulugu KALICI KILIT uretmiyor."""
    project = await project_factory("F-4")
    site = await _site(db_session, project)
    block = await _block(db_session, project, site)
    token = await _login(client, user_factory)

    blocked = await client.delete(f"/sites/{site.id}", headers=_auth(token))
    assert blocked.status_code == 409
    assert blocked.json()["detail"] == BLOCK_BLOCKER

    await db_session.delete(block)
    await db_session.flush()

    resp = await client.delete(f"/sites/{site.id}", headers=_auth(token))

    assert resp.status_code == 204, resp.text
    assert (await _counts(db_session, site.id))["sites"] == 0


async def test_delete_section_leaves_sibling_boq_and_blocks_intact(
    client, db_session, user_factory, project_factory
):
    """S8 tamamlayicisi — bolum silmek santiyenin DIGER alt kayitlarina dokunmaz.

    `sections.id`'yi hedefleyen FK yok, ama poz/blok satirlari SANTIYEYE baglidir;
    bolum silme yolunun yanlislikla santiye kimligiyle calismadigi olculmelidir.
    """
    project = await project_factory("F-5")
    site = await _site(db_session, project)
    section = await _section(db_session, site)
    group = await _group(db_session, site)
    await _item(db_session, site, group)
    await _block(db_session, project, site)
    token = await _login(client, user_factory)
    before = await _counts(db_session, site.id)

    resp = await client.delete(f"/sections/{section.id}", headers=_auth(token))

    assert resp.status_code == 204
    after = await _counts(db_session, site.id)
    assert after["sections"] == before["sections"] - 1
    assert after["sites"] == before["sites"] == 1
    assert after["boq_groups"] == before["boq_groups"] == 1
    assert after["boq_items"] == before["boq_items"] == 1
    assert after["blocks"] == before["blocks"] == 1


# --- 59/60: CASCADE'li ama KORKULUKSUZ yedi FK ---
#
# HEPSI BOLUMSUZ SANTIYEDE kosar. Kaydin cekirdegi budur: `site_has_sections`
# bu satirlari YAPISAL OLARAK kapsamaz (`section_id` hepsinde NULLABLE), yani
# bolumu/pozu/blogu olmayan ama puantajli/gunluklu/belgeli/planli bir santiye
# bes korkulugun besini de gecer ve tek istekte 204 alir.


async def test_delete_site_with_timesheet_returns_409(
    client, db_session, user_factory, project_factory
):
    """Puantaj BORDRONUN GIRDISIDIR: sessizce gitmesi kanit tabanini yok eder."""
    project = await project_factory("G-1")
    site = await _site(db_session, project)
    owner = await user_factory(email="pt-owner@t.co", password="parola1234", role_key="patron")
    personnel = await _personnel(db_session)
    await _timesheet(db_session, project, site, personnel, owner)
    token = await _login(client, user_factory)
    before = await _counts(db_session, site.id)

    resp = await client.delete(f"/sites/{site.id}", headers=_auth(token))

    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"] == TIMESHEET_BLOCKER
    assert await _counts(db_session, site.id) == before
    assert before["sections"] == 0
    assert before["timesheet_entries"] == 1


async def test_delete_site_with_diary_returns_409(
    client, db_session, user_factory, project_factory
):
    """Gunlukte ISG/kaza notu vardir; CASCADE onu da goturur."""
    project = await project_factory("G-2")
    site = await _site(db_session, project)
    owner = await user_factory(email="sd-owner@t.co", password="parola1234", role_key="patron")
    await _diary(db_session, project, site, owner)
    token = await _login(client, user_factory)
    before = await _counts(db_session, site.id)

    resp = await client.delete(f"/sites/{site.id}", headers=_auth(token))

    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"] == DIARY_BLOCKER
    assert await _counts(db_session, site.id) == before
    assert before["sections"] == 0
    assert before["diary_entries"] == 1


async def test_delete_site_with_documents_returns_409(
    client, db_session, user_factory, project_factory
):
    """`documents` gidince `document_blobs.document_id` CASCADE'i BAYTLARI da siler."""
    project = await project_factory("G-3")
    site = await _site(db_session, project)
    await _document(db_session, project, site)
    token = await _login(client, user_factory)
    before = await _counts(db_session, site.id)

    resp = await client.delete(f"/sites/{site.id}", headers=_auth(token))

    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"] == DOCUMENT_BLOCKER
    assert await _counts(db_session, site.id) == before
    assert before["sections"] == 0
    assert before["documents"] == 1


async def test_delete_site_with_empty_document_folder_returns_409(
    client, db_session, user_factory, project_factory
):
    """Belgesiz KLASOR de tek basina engeldir (`site_has_boq`nun grup dali dersi)."""
    project = await project_factory("G-4")
    site = await _site(db_session, project)
    await _folder(db_session, project, site)
    token = await _login(client, user_factory)
    before = await _counts(db_session, site.id)

    resp = await client.delete(f"/sites/{site.id}", headers=_auth(token))

    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"] == DOCUMENT_BLOCKER
    assert await _counts(db_session, site.id) == before
    assert before["documents"] == 0
    assert before["document_folders"] == 1


async def test_delete_site_with_plan_rows_returns_409(
    client, db_session, user_factory, project_factory
):
    """Ekipman satirinin `section_id`si ZATEN NULL — bolum korkulugu kapsamaz."""
    project = await project_factory("G-5")
    site = await _site(db_session, project)
    await _plan_row(db_session, project, site)
    token = await _login(client, user_factory)
    before = await _counts(db_session, site.id)

    resp = await client.delete(f"/sites/{site.id}", headers=_auth(token))

    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"] == PLAN_BLOCKER
    assert await _counts(db_session, site.id) == before
    assert before["sections"] == 0
    assert before["plan_rows"] == 1


async def test_delete_site_with_plan_goal_only_returns_409(
    client, db_session, user_factory, project_factory
):
    """Izgarasi bos ama HEDEFI olan santiye de engellenir (uc tablo `or_`lanir)."""
    project = await project_factory("G-6")
    site = await _site(db_session, project)
    await _plan_goal(db_session, project, site)
    token = await _login(client, user_factory)
    before = await _counts(db_session, site.id)

    resp = await client.delete(f"/sites/{site.id}", headers=_auth(token))

    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"] == PLAN_BLOCKER
    assert await _counts(db_session, site.id) == before
    assert before["plan_rows"] == 0
    assert before["plan_goals"] == 1


async def test_delete_site_with_plan_sprint_only_returns_409(
    client, db_session, user_factory, project_factory
):
    """Yalniz SPRINT'i olan santiye de engellenir."""
    project = await project_factory("G-7")
    site = await _site(db_session, project)
    await _plan_sprint(db_session, site)
    token = await _login(client, user_factory)
    before = await _counts(db_session, site.id)

    resp = await client.delete(f"/sites/{site.id}", headers=_auth(token))

    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"] == PLAN_BLOCKER
    assert await _counts(db_session, site.id) == before
    assert before["plan_sprints"] == 1


async def test_new_blockers_run_after_the_existing_five(
    client, db_session, user_factory, project_factory
):
    """SIRA KILIDI: bolum + puantaj birlikteyken MESAJ HALA bolum mesajidir.

    Yeni dort korkuluk zincirin SONUNA eklendi; basina eklenseydi bugun bolum
    mesaji goren kullanici yarin puantaj mesaji gorurdu (sessiz UX kirilmasi).
    """
    project = await project_factory("G-8")
    site = await _site(db_session, project)
    await _section(db_session, site)
    owner = await user_factory(email="sira-owner@t.co", password="parola1234", role_key="patron")
    personnel = await _personnel(db_session, "Mehmet Demir")
    await _timesheet(db_session, project, site, personnel, owner)
    token = await _login(client, user_factory)

    resp = await client.delete(f"/sites/{site.id}", headers=_auth(token))

    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"] == SECTION_BLOCKER


async def test_delete_after_removing_plan_rows_returns_204(
    client, db_session, user_factory, project_factory
):
    """Yeni korkuluk KALICI KILIT uretmiyor (S9 deseninin plan varyanti)."""
    project = await project_factory("G-9")
    site = await _site(db_session, project)
    row = await _plan_row(db_session, project, site)
    token = await _login(client, user_factory)

    blocked = await client.delete(f"/sites/{site.id}", headers=_auth(token))
    assert blocked.status_code == 409
    assert blocked.json()["detail"] == PLAN_BLOCKER

    await db_session.delete(row)
    await db_session.flush()

    resp = await client.delete(f"/sites/{site.id}", headers=_auth(token))

    assert resp.status_code == 204, resp.text
    assert (await _counts(db_session, site.id))["sites"] == 0


async def test_delete_site_with_closed_sprint_returns_204(
    client, db_session, user_factory, project_factory
):
    """🔴 KILITLENME BEKCISI: KAPALI sprint santiyeyi silinemez YAPMAMALI.

    Olcum (`site_planning/write.py:274-304`): `save_sprint` sprint satirini ASLA
    SILMEZ, yalniz `is_active`i false'a ceker; sprint silen baska bir uc yoktur.
    Korkuluk `is_active` suzgeci olmadan yazilsaydi, seridine bir kez ad yazip
    sonra bosaltan santiye BIR DAHA silinemezdi — ustelik hata metni UI'da
    GORUNMEYEN bir satiri isaret ederdi.
    """
    project = await project_factory("G-10")
    site = await _site(db_session, project)
    sprint = await _plan_sprint(db_session, site)
    sprint.is_active = False
    await db_session.flush()
    token = await _login(client, user_factory)

    resp = await client.delete(f"/sites/{site.id}", headers=_auth(token))

    assert resp.status_code == 204, resp.text
