"""B11 — blok/unite uclarinin denetim gunlugu (spec §9).

Iki kural bu dosyada KILITLENIR:

1. **Okuma uclari YAZMAZ.** `GET .../blocks` ve `GET .../units` denetim satiri
   uretmez; aksi hâlde bir liste ekranini acan kullanici gunlugu doldururdu
   (P4 T7 kurali).
2. **Toplu uclarda ISTEK BASINA TEK SATIR.** 24 unitelik toplu uretim, 10
   satirlik ice aktarma ve 42 unitelik paylasim BIR satir yazar — satir basina
   gunluk, denetim gunlugunu okunamaz hâle getirirdi (spec §9).

Sayimlar mutlak yapilir (`== 1`), "en az bir" DEGIL: `>= 1` iddiasi tam da
onlemek istedigimiz 24-satirlik davranisi yesil gosterirdi.
"""

import io
from decimal import Decimal

from openpyxl import Workbook
from sqlalchemy import delete, select

from app.modules.audit import messages
from app.modules.audit.models import AuditAction, AuditLog
from app.modules.projects.models import LandShareShareholder
from app.modules.units.models import Unit, UnitKind, UnitOwnerSide
from app.modules.units.schemas import UnitNumberingPattern
from tests._silme_yardimci import sil_aile
from tests.modules.units._units_api import _block, _login, _site, _unit

_IP = "203.0.113.42"
_IP_HEADER = {"x-forwarded-for": _IP}
_XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

# P3.1 T11 (spec §6.4): 12 kanonik sutun. `Oda Tipi` ve `Brüt m²` ZORUNLU
# oldugu icin satir uretecinin varsayilanlari da dolu gelir (EI 161).
_IMPORT_HEADERS = [
    "Blok",
    "Kat",
    "Ünite No",
    "Tür",
    "Oda Tipi",
    "Brüt m²",
    "Net m²",
    "Cephe",
    "Liste Fiyatı",
    "Rayiç Değer",
    "Maliyet",
    "Sahiplik",
]


async def _admin(client, db_session, user_factory) -> dict[str, str]:
    """Giris yapar ve LOGIN'in urettigi denetim satirini temizler.

    Temizlik sart: `login` de bir denetim eylemidir ve silinmezse "tam bir
    satir yazildi" iddialari toplam sayim uzerinden yanlis olurdu.
    """
    token = await _login(client, user_factory, "system_admin")
    await db_session.execute(delete(AuditLog))
    return {"Authorization": f"Bearer {token}", **_IP_HEADER}


async def _rows(db_session, action: AuditAction | None = None) -> list[AuditLog]:
    stmt = select(AuditLog)
    if action is not None:
        stmt = stmt.where(AuditLog.action == action)
    return list((await db_session.execute(stmt)).scalars().all())


async def _count(db_session) -> int:
    return len(await _rows(db_session))


def _xlsx(rows: list[list]) -> bytes:
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(_IMPORT_HEADERS)
    for row in rows:
        sheet.append(row)
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


# --- Blok uclari (spec §9) ---


async def test_block_create_writes_one_audit_row(client, db_session, user_factory, project_factory):
    project = await project_factory("B11-1", name="Yeşil Vadi")
    await _site(db_session, project)
    headers = await _admin(client, db_session, user_factory)

    resp = await client.post(
        f"/projects/{project.id}/blocks", json={"name": "A Blok"}, headers=headers
    )

    assert resp.status_code == 201
    rows = await _rows(db_session, AuditAction.create)
    assert len(rows) == 1
    assert rows[0].detail == "Yeni blok oluşturuldu: Yeşil Vadi · A Blok"


async def test_block_update_writes_audit(client, db_session, user_factory, project_factory):
    project = await project_factory("B11-2", name="Yeşil Vadi")
    site = await _site(db_session, project)
    block = await _block(db_session, project, site, name="A Blok")
    headers = await _admin(client, db_session, user_factory)

    resp = await client.patch(f"/blocks/{block.id}", json={"name": "B Blok"}, headers=headers)

    assert resp.status_code == 200
    rows = await _rows(db_session, AuditAction.update)
    assert len(rows) == 1
    assert rows[0].detail == "Blok güncellendi: Yeşil Vadi · B Blok"


async def test_block_delete_writes_audit_with_delete_action(
    client, db_session, user_factory, project_factory
):
    project = await project_factory("B11-3", name="Yeşil Vadi")
    site = await _site(db_session, project)
    block = await _block(db_session, project, site, name="A Blok")
    headers = await _admin(client, db_session, user_factory)

    resp = await sil_aile(client, headers, "block", block.id)

    assert resp.status_code == 204
    rows = await _rows(db_session, AuditAction.delete)
    assert len(rows) == 1
    assert rows[0].detail == "Blok silindi: Yeşil Vadi · A Blok"


# --- Unite uclari (spec §9) ---


async def test_unit_create_writes_audit(client, db_session, user_factory, project_factory):
    """Mesajda PROJE ADI da vardir: "A Blok · Daire 1" her projede olabilir,
    proje adi olmadan denetim satiri anlamsizlasir (spec §9)."""
    project = await project_factory("B11-4", name="Yeşil Vadi")
    site = await _site(db_session, project)
    block = await _block(db_session, project, site, name="A Blok")
    headers = await _admin(client, db_session, user_factory)

    resp = await client.post(
        f"/projects/{project.id}/units",
        json={"block_id": str(block.id), "unit_no": "1", "unit_kind": "apartment"},
        headers=headers,
    )

    assert resp.status_code == 201
    rows = await _rows(db_session, AuditAction.create)
    assert len(rows) == 1
    assert rows[0].detail == "Yeni ünite oluşturuldu: Yeşil Vadi · A Blok · 1"


async def test_unit_update_writes_audit(client, db_session, user_factory, project_factory):
    project = await project_factory("B11-5", name="Yeşil Vadi")
    site = await _site(db_session, project)
    block = await _block(db_session, project, site, name="A Blok")
    unit = await _unit(db_session, project, block, "1")
    headers = await _admin(client, db_session, user_factory)

    resp = await client.patch(f"/units/{unit.id}", json={"unit_no": "2"}, headers=headers)

    assert resp.status_code == 200
    rows = await _rows(db_session, AuditAction.update)
    assert len(rows) == 1
    assert rows[0].detail == "Ünite güncellendi: Yeşil Vadi · A Blok · 2"


async def test_unit_delete_writes_audit_with_delete_action(
    client, db_session, user_factory, project_factory
):
    project = await project_factory("B11-6", name="Yeşil Vadi")
    site = await _site(db_session, project)
    block = await _block(db_session, project, site, name="A Blok")
    unit = await _unit(db_session, project, block, "1")
    headers = await _admin(client, db_session, user_factory)

    resp = await sil_aile(client, headers, "unit", unit.id)

    assert resp.status_code == 204
    rows = await _rows(db_session, AuditAction.delete)
    assert len(rows) == 1
    assert rows[0].detail == "Ünite silindi: Yeşil Vadi · A Blok · 1"


# --- Toplu uclar: ISTEK BASINA TEK SATIR (spec §9) ---


async def test_bulk_writes_exactly_one_audit_row_for_24_units(
    client, db_session, user_factory, project_factory
):
    project = await project_factory("B11-7", name="Yeşil Vadi")
    site = await _site(db_session, project)
    block = await _block(db_session, project, site, name="A Blok")
    headers = await _admin(client, db_session, user_factory)

    resp = await client.post(
        f"/projects/{project.id}/units/bulk",
        json={
            "block_id": str(block.id),
            # P3.1 T8: `floor_based` → `floor_sequence` (spec §5.2). Yeniden
            # ADLANDIRMA; bu testin olctugu sey (istek basina TEK denetim
            # satiri) degismedi.
            "numbering": UnitNumberingPattern.floor_sequence.value,
            "start_floor": 1,
            "end_floor": 12,
            "units_per_floor": 2,
            "unit_kind": UnitKind.apartment.value,
        },
        headers=headers,
    )

    assert resp.status_code == 201
    created = (
        await db_session.execute(select(Unit).where(Unit.project_id == project.id))
    ).scalars()
    assert len(list(created)) == 24
    rows = await _rows(db_session, AuditAction.create)
    assert len(rows) == 1
    assert rows[0].detail == "Toplu ünite üretildi: Yeşil Vadi · A Blok · 24 ünite"


async def test_import_writes_exactly_one_audit_row_for_10_units(
    client, db_session, user_factory, project_factory
):
    project = await project_factory("B11-8", name="Yeşil Vadi")
    site = await _site(db_session, project)
    await _block(db_session, project, site, name="A Blok")
    headers = await _admin(client, db_session, user_factory)
    content = _xlsx(
        [
            ["A Blok", None, str(no), "Daire", "3+1", 120, None, None, None, None, None, None]
            for no in range(1, 11)
        ]
    )

    resp = await client.post(
        f"/projects/{project.id}/units/import",
        files={"file": ("uniteler.xlsx", content, _XLSX_MIME)},
        headers=headers,
    )

    assert resp.status_code == 200
    assert resp.json()["created"] == 10
    rows = await _rows(db_session, AuditAction.create)
    assert len(rows) == 1
    assert rows[0].detail == "Üniteler Excel'den içe aktarıldı: Yeşil Vadi · 10 ünite"


async def test_allocation_writes_exactly_one_audit_row_for_42_units(
    client, db_session, user_factory, project_factory
):
    """42 satirlik bir kayit denetim gunlugunu bogar (spec §9): TEK satir yazilir."""
    project = await project_factory("B11-9", name="Yeşil Vadi", project_type="kat_karsiligi")
    site = await _site(db_session, project)
    block = await _block(db_session, project, site, name="A Blok")
    units = [await _unit(db_session, project, block, str(no)) for no in range(1, 43)]
    headers = await _admin(client, db_session, user_factory)

    resp = await client.patch(
        f"/projects/{project.id}/units/allocation",
        json={
            "items": [
                {"unit_id": str(unit.id), "owner_side": UnitOwnerSide.contractor.value}
                for unit in units
            ]
        },
        headers=headers,
    )

    assert resp.status_code == 200
    rows = await _rows(db_session, AuditAction.update)
    assert len(rows) == 1
    assert rows[0].detail == "Ünite paylaşımı güncellendi: Yeşil Vadi · 42 ünite"


async def test_allocation_audit_row_counts_shareholder_assignments(
    client, db_session, user_factory, project_factory
):
    """P9 spec §5: hissedar atamasi MEVCUT dönem-özetine eklenir — istek yine
    TEK satir yazar ve yeni bir `AuditAction` acilmaz."""
    project = await project_factory("B11-9B", name="Yeşil Vadi", project_type="kat_karsiligi")
    site = await _site(db_session, project)
    block = await _block(db_session, project, site, name="A Blok")
    units = [await _unit(db_session, project, block, str(no)) for no in (1, 2, 3)]
    shareholder = LandShareShareholder(
        project_id=project.id, name="A. Yılmaz", share_pct=Decimal("50.00")
    )
    db_session.add(shareholder)
    await db_session.flush()
    headers = await _admin(client, db_session, user_factory)

    resp = await client.patch(
        f"/projects/{project.id}/units/allocation",
        json={
            "items": [
                {
                    "unit_id": str(unit.id),
                    "owner_side": UnitOwnerSide.landowner.value,
                    "shareholder_id": str(shareholder.id) if index < 2 else None,
                }
                for index, unit in enumerate(units)
            ]
        },
        headers=headers,
    )

    assert resp.status_code == 200, resp.text
    rows = await _rows(db_session, AuditAction.update)
    assert len(rows) == 1
    assert rows[0].detail == (
        "Ünite paylaşımı güncellendi: Yeşil Vadi · 3 ünite (2 hissedar ataması)"
    )


# --- Okuma uclari YAZMAZ + ortak alanlar + reddedilen istek ---


async def test_read_endpoints_write_no_audit(client, db_session, user_factory, project_factory):
    project = await project_factory("B11-10", name="Yeşil Vadi")
    site = await _site(db_session, project)
    block = await _block(db_session, project, site, name="A Blok")
    await _unit(db_session, project, block, "1")
    headers = await _admin(client, db_session, user_factory)

    assert (await client.get(f"/projects/{project.id}/blocks", headers=headers)).status_code == 200
    assert (await client.get(f"/projects/{project.id}/units", headers=headers)).status_code == 200

    assert await _count(db_session) == 0


async def test_audit_rows_carry_actor_and_ip(client, db_session, user_factory, project_factory):
    project = await project_factory("B11-11", name="Yeşil Vadi")
    await _site(db_session, project)
    headers = await _admin(client, db_session, user_factory)

    resp = await client.post(
        f"/projects/{project.id}/blocks", json={"name": "A Blok"}, headers=headers
    )

    assert resp.status_code == 201
    rows = await _rows(db_session, AuditAction.create)
    assert len(rows) == 1
    assert rows[0].actor_user_id is not None
    # INET sutunu `ipaddress` nesnesi dondurur; karsilastirma metne cevrilerek yapilir.
    assert str(rows[0].ip_address) == _IP


async def test_import_partial_writes_one_row_carrying_skipped_count(
    client, db_session, user_factory, project_factory
):
    """P3.1 T14 (#55, spec §9): KISMI aktarimda da ISTEK BASINA TEK satir.

    Ve o tek satir `skipped` sayisini TASIR: 4 satirlik dosyadan 3 unite gelmesi
    ile 3 satirlik dosyadan 3 unite gelmesi gunlukte AYNI gorunemez (§6.1).
    """
    project = await project_factory("B11-13", name="Yeşil Vadi")
    site = await _site(db_session, project)
    await _block(db_session, project, site, name="A Blok")
    headers = await _admin(client, db_session, user_factory)
    rows = [
        ["A Blok", None, str(no), "Daire", "3+1", 120, None, None, None, None, None, None]
        for no in range(1, 4)
    ]
    # `Oda Tipi` bos → HATA (spec §6.5, EI 161): satir atlanir, dosya reddedilmez.
    rows.append(["A Blok", None, "4", "Daire", None, 120, None, None, None, None, None, None])

    resp = await client.post(
        f"/projects/{project.id}/units/import",
        files={"file": ("uniteler.xlsx", _xlsx(rows), _XLSX_MIME)},
        headers=headers,
    )

    assert resp.status_code == 200
    body = resp.json()
    assert (body["created"], body["skipped"]) == (3, 1)
    audit_rows = await _rows(db_session, AuditAction.create)
    assert len(audit_rows) == 1
    assert (
        audit_rows[0].detail
        == "Üniteler Excel'den içe aktarıldı: Yeşil Vadi · 3 ünite (1 satır atlandı)"
    )


async def test_preview_validate_and_template_write_no_audit(
    client, db_session, user_factory, project_factory
):
    """P3.1 T15 (spec §9, §12.5/54): P3.1'in UC yeni ucu da denetim YAZMAZ.

    Ucu de yazma akisinin ONIZLEMESIDIR (`bulk/preview`, `import/validate`,
    `import/template`) ve hicbir satir uretmez; denetim yazsalardi "Onizlemeyi
    Yenile" / "Şablon İndir"e her basista gunluge satir dusen bir ekran ortaya
    cikardi (P4 T7 kurali).
    """
    project = await project_factory("B11-14", name="Yeşil Vadi")
    site = await _site(db_session, project)
    block = await _block(db_session, project, site, name="A Blok")
    headers = await _admin(client, db_session, user_factory)
    row = ["A Blok", None, "1", "Daire", "3+1", 120, None, None, None, None, None, None]
    content = _xlsx([row])

    preview = await client.post(
        f"/projects/{project.id}/units/bulk/preview",
        json={
            "block_id": str(block.id),
            "unit_kind": UnitKind.apartment.value,
            "numbering": UnitNumberingPattern.floor_sequence.value,
            "start_floor": 1,
            "end_floor": 2,
            "units_per_floor": 2,
        },
        headers=headers,
    )
    validate = await client.post(
        f"/projects/{project.id}/units/import/validate",
        files={"file": ("uniteler.xlsx", content, _XLSX_MIME)},
        headers=headers,
    )

    template = await client.get(f"/projects/{project.id}/units/import/template", headers=headers)

    assert (preview.status_code, validate.status_code, template.status_code) == (200, 200, 200)
    assert await _count(db_session) == 0


def test_unit_audit_message_texts_are_frozen():
    """P3.1 T14 (spec §9): P3.1'de DEGISMEYEN yedi metin, birebir.

    §9 tablosunun "(değişmedi)" satirlari bir iddiadir; bu test onu kilitler.
    Bir metnin sessizce degismesi eski denetim satirlariyla yenilerinin ayni
    ekranda FARKLI dilde gorunmesine yol acar.
    """
    assert messages.block_created("Yeşil Vadi", "A Blok") == (
        "Yeni blok oluşturuldu: Yeşil Vadi · A Blok"
    )
    assert messages.block_updated("Yeşil Vadi", "A Blok") == "Blok güncellendi: Yeşil Vadi · A Blok"
    assert messages.block_deleted("Yeşil Vadi", "A Blok") == "Blok silindi: Yeşil Vadi · A Blok"
    assert messages.unit_created("Yeşil Vadi", "A Blok", "1") == (
        "Yeni ünite oluşturuldu: Yeşil Vadi · A Blok · 1"
    )
    assert messages.unit_updated("Yeşil Vadi", "A Blok", "1") == (
        "Ünite güncellendi: Yeşil Vadi · A Blok · 1"
    )
    assert messages.unit_deleted("Yeşil Vadi", "A Blok", "1") == (
        "Ünite silindi: Yeşil Vadi · A Blok · 1"
    )
    assert messages.units_bulk_created("Yeşil Vadi", "A Blok", 24) == (
        "Toplu ünite üretildi: Yeşil Vadi · A Blok · 24 ünite"
    )
    assert messages.unit_allocation_updated("Yeşil Vadi", 42, 0) == (
        "Ünite paylaşımı güncellendi: Yeşil Vadi · 42 ünite"
    )


def test_allocation_message_reports_shareholder_assignments():
    """P9 spec §5: hissedar atamasi sayisi MEVCUT dönem-özeti satirina eklenir —
    yeni `AuditAction` ACILMAZ (TB3 T3 emsali). Ek YALNIZ atama varken basilir;
    "(0 hissedar ataması)" her paylasima gurultu eklerdi (`units_imported`
    gerekcesinin aynisi)."""
    assert messages.unit_allocation_updated("Yeşil Vadi", 42, 19) == (
        "Ünite paylaşımı güncellendi: Yeşil Vadi · 42 ünite (19 hissedar ataması)"
    )


def test_units_imported_message_reports_skipped_rows():
    """P3.1 T14 (spec §9, §6.1): `skipped` VARSA metne girer."""
    assert messages.units_imported("Yeşil Vadi", 22, 2) == (
        "Üniteler Excel'den içe aktarıldı: Yeşil Vadi · 22 ünite (2 satır atlandı)"
    )


def test_units_imported_message_stays_short_without_skipped_rows():
    """P3.1 T14: `skipped=0` iken parantezli ek YAZILMAZ — "(0 satır atlandı)"
    her tam aktarmaya gurultu eklerdi."""
    assert messages.units_imported("Yeşil Vadi", 22, 0) == (
        "Üniteler Excel'den içe aktarıldı: Yeşil Vadi · 22 ünite"
    )


async def test_failed_write_writes_no_audit(client, db_session, user_factory, project_factory):
    """409 (cakisan blok adi) ve 422 (net > brut) reddedilen isteklerdir: denetim
    satiri asil islemle AYNI transaction'da oldugu icin gunluk BOS kalir."""
    project = await project_factory("B11-12", name="Yeşil Vadi")
    site = await _site(db_session, project)
    block = await _block(db_session, project, site, name="A Blok")
    headers = await _admin(client, db_session, user_factory)

    duplicate = await client.post(
        f"/projects/{project.id}/blocks", json={"name": "A Blok"}, headers=headers
    )
    invalid = await client.post(
        f"/projects/{project.id}/units",
        json={
            "block_id": str(block.id),
            "unit_no": "1",
            "unit_kind": "apartment",
            "gross_area_m2": str(Decimal("100.00")),
            "net_area_m2": str(Decimal("120.00")),
        },
        headers=headers,
    )

    assert duplicate.status_code == 409
    assert invalid.status_code == 422
    assert await _count(db_session) == 0
