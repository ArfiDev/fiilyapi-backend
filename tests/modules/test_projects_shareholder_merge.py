"""P9 T2 — hissedar listesinin KIMLIK KORUYAN birlestirmesi (spec §4.1).

`units.shareholder_id` FK acildiktan (T1) sonra hissedar listesini toptan silip
yeniden acmak, siradan bir proje PATCH'inde TUM unite atamalarini sessizce
supururdu (ON DELETE SET NULL). Bu dosya sozlesmenin dort ayagini olcer:

1. id tasiyan girdi satiri YERINDE gunceller — satirin `id`'si DEGISMEZ (kimlik kaniti).
2. id'siz eski govde geriye uyumlu calisir.
3. Atanmis unitesi olan hissedari listeden dusurmek 409 verir ve HICBIR hissedar
   satiri degismez (atomiklik — durum kodu tek basina kanit degildir).
4. Atanmamis hissedar serbestce silinir; bilinmeyen/baska projenin id'si 422.
"""

import uuid
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.core.errors import ConflictError, ProjectValidationError
from app.modules.projects.messages import (
    SHAREHOLDER_DUPLICATE_IN_PAYLOAD,
    SHAREHOLDER_UNKNOWN,
    shareholder_has_units,
)
from app.modules.projects.models import LandShareShareholder
from app.modules.projects.schemas import (
    ProjectCreate,
    ProjectLandShareInput,
    ProjectUpdate,
)
from app.modules.projects.service import create_project, update_project
from app.modules.units.models import Unit, UnitKind, UnitOwnerSide
from tests.modules.units._units_api import _block, _site


async def _writer(seeded_db, user_factory, email: str):
    user = await user_factory(email=email, password="parola1234", role_key="patron")
    user.all_projects = True
    await seeded_db.flush()
    return user


def _land_share(shareholders: list[dict]) -> ProjectLandShareInput:
    return ProjectLandShareInput(
        landowner_name="Yılmaz Ailesi",
        our_share_pct=Decimal("55.00"),
        owner_share_pct=Decimal("45.00"),
        shareholders=shareholders,
    )


async def _kat_karsiligi(session, code: str, shareholders: list[dict]):
    return await create_project(
        session,
        ProjectCreate(
            code=code,
            name=f"P9 {code}",
            project_type="kat_karsiligi",
            city="Ankara",
            land_share=_land_share(shareholders),
        ),
    )


async def _assign_unit(session, project, shareholder: LandShareShareholder) -> Unit:
    site = await _site(session, project, code=f"SNT-{project.code}")
    block = await _block(session, project, site)
    unit = Unit(
        project_id=project.id,
        block_id=block.id,
        unit_no="1",
        unit_kind=UnitKind.apartment,
        owner_side=UnitOwnerSide.landowner,
        shareholder_id=shareholder.id,
    )
    session.add(unit)
    await session.flush()
    return unit


async def _rows(session, project_id: uuid.UUID) -> list[LandShareShareholder]:
    result = await session.execute(
        select(LandShareShareholder)
        .where(LandShareShareholder.project_id == project_id)
        .order_by(LandShareShareholder.name)
    )
    return list(result.scalars().all())


# --- 1. Kimlik korunumu ---


async def test_update_with_id_keeps_row_identity(seeded_db, user_factory):
    """Spec §4.1: id eslesirse satir YERINDE guncellenir — `id` DEGISMEZ.

    Kaniti satirin kimligidir: ad/oran degisse bile ayni birincil anahtar kalir,
    boylece ona bagli `units.shareholder_id` SET NULL ile supurulmez.
    """
    actor = await _writer(seeded_db, user_factory, "p9id@t.co")
    project = await _kat_karsiligi(
        seeded_db,
        "P9-ID",
        [
            {"name": "A. Yılmaz", "share_pct": Decimal("60.00")},
            {"name": "B. Yılmaz", "share_pct": Decimal("40.00")},
        ],
    )
    before = {s.name: s.id for s in await _rows(seeded_db, project.id)}
    unit = await _assign_unit(seeded_db, project, next(iter(await _rows(seeded_db, project.id))))

    await update_project(
        seeded_db,
        actor,
        project.id,
        ProjectUpdate(
            land_share=_land_share(
                [
                    {"id": before["A. Yılmaz"], "name": "A. Yılmaz Oğlu", "share_pct": "70.00"},
                    {"id": before["B. Yılmaz"], "name": "B. Yılmaz", "share_pct": "30.00"},
                ]
            )
        ),
    )

    after = await _rows(seeded_db, project.id)
    assert {s.id for s in after} == set(before.values())
    renamed = next(s for s in after if s.id == before["A. Yılmaz"])
    assert renamed.name == "A. Yılmaz Oğlu"
    assert renamed.share_pct == Decimal("70.00")
    # Kimlik korundugu icin unite atamasi ayakta kalir (dilimin tum meselesi budur).
    await seeded_db.refresh(unit)
    assert unit.shareholder_id is not None


async def test_update_without_ids_still_replaces_list(seeded_db, user_factory):
    """Geriye uyum (spec §4.1): id'siz eski govde eskisi gibi calisir."""
    actor = await _writer(seeded_db, user_factory, "p9legacy@t.co")
    project = await _kat_karsiligi(seeded_db, "P9-LEG", [{"name": "Eski", "share_pct": "100.00"}])

    updated = await update_project(
        seeded_db,
        actor,
        project.id,
        ProjectUpdate(
            land_share=_land_share(
                [
                    {"name": "Yeni 1", "share_pct": "70.00"},
                    {"name": "Yeni 2", "share_pct": "30.00"},
                ]
            )
        ),
    )

    assert [s.name for s in updated.shareholders] == ["Yeni 1", "Yeni 2"]


async def test_id_less_entry_creates_new_row_next_to_kept_one(seeded_db, user_factory):
    actor = await _writer(seeded_db, user_factory, "p9mix@t.co")
    project = await _kat_karsiligi(seeded_db, "P9-MIX", [{"name": "Kalan", "share_pct": "100.00"}])
    kept_id = (await _rows(seeded_db, project.id))[0].id

    await update_project(
        seeded_db,
        actor,
        project.id,
        ProjectUpdate(
            land_share=_land_share(
                [
                    {"id": kept_id, "name": "Kalan", "share_pct": "60.00"},
                    {"name": "Eklenen", "share_pct": "40.00"},
                ]
            )
        ),
    )

    rows = await _rows(seeded_db, project.id)
    assert [s.name for s in rows] == ["Eklenen", "Kalan"]
    assert next(s for s in rows if s.name == "Kalan").id == kept_id


# --- 2. Atanmis hissedari dusurme = 409 + atomiklik ---


async def test_removing_assigned_shareholder_returns_409_and_changes_nothing(
    seeded_db, user_factory
):
    """Spec §4.1: sessiz supurme YOK. Durum kodu TEK BASINA kanit degildir —
    diger hissedar satirlarinin degerleri de degismemis olmalidir."""
    actor = await _writer(seeded_db, user_factory, "p9409@t.co")
    project = await _kat_karsiligi(
        seeded_db,
        "P9-409",
        [
            {"name": "Atanmış", "share_pct": "60.00"},
            {"name": "Duran", "share_pct": "40.00"},
        ],
    )
    rows = await _rows(seeded_db, project.id)
    assigned = next(s for s in rows if s.name == "Atanmış")
    other = next(s for s in rows if s.name == "Duran")
    unit = await _assign_unit(seeded_db, project, assigned)

    with pytest.raises(ConflictError) as excinfo:
        await update_project(
            seeded_db,
            actor,
            project.id,
            ProjectUpdate(
                land_share=_land_share(
                    [{"id": other.id, "name": "Duran YENİ", "share_pct": "100.00"}]
                )
            ),
        )

    assert str(excinfo.value) == shareholder_has_units(["Atanmış"])
    after = await _rows(seeded_db, project.id)
    assert [(s.id, s.name, s.share_pct) for s in after] == [
        (assigned.id, "Atanmış", Decimal("60.00")),
        (other.id, "Duran", Decimal("40.00")),
    ]
    await seeded_db.refresh(unit)
    assert unit.shareholder_id == assigned.id


async def test_legacy_id_less_body_hits_409_when_it_would_drop_assigned(seeded_db, user_factory):
    """Bilincli davranis (spec §4.1): id'siz govde artik atanmis hissedari
    dusuremez — eskiden bu istek atamalari sessizce supururdu."""
    actor = await _writer(seeded_db, user_factory, "p9legacy409@t.co")
    project = await _kat_karsiligi(
        seeded_db, "P9-L409", [{"name": "Atanmış", "share_pct": "100.00"}]
    )
    assigned = (await _rows(seeded_db, project.id))[0]
    await _assign_unit(seeded_db, project, assigned)

    with pytest.raises(ConflictError):
        await update_project(
            seeded_db,
            actor,
            project.id,
            ProjectUpdate(land_share=_land_share([{"name": "Bambaşka", "share_pct": "100.00"}])),
        )

    assert [s.name for s in await _rows(seeded_db, project.id)] == ["Atanmış"]


async def test_removing_unassigned_shareholder_is_allowed(seeded_db, user_factory):
    actor = await _writer(seeded_db, user_factory, "p9free@t.co")
    project = await _kat_karsiligi(
        seeded_db,
        "P9-FREE",
        [
            {"name": "Atanmış", "share_pct": "60.00"},
            {"name": "Bos", "share_pct": "40.00"},
        ],
    )
    rows = await _rows(seeded_db, project.id)
    assigned = next(s for s in rows if s.name == "Atanmış")
    await _assign_unit(seeded_db, project, assigned)

    await update_project(
        seeded_db,
        actor,
        project.id,
        ProjectUpdate(
            land_share=_land_share([{"id": assigned.id, "name": "Atanmış", "share_pct": "100.00"}])
        ),
    )

    assert [s.name for s in await _rows(seeded_db, project.id)] == ["Atanmış"]


# --- 3. Bilinmeyen / baska projenin id'si = 422 ---


async def test_unknown_shareholder_id_returns_422(seeded_db, user_factory):
    """Uydurma id SESSIZCE yeni satira donmez."""
    actor = await _writer(seeded_db, user_factory, "p9unk@t.co")
    project = await _kat_karsiligi(seeded_db, "P9-UNK", [{"name": "A", "share_pct": "100.00"}])

    with pytest.raises(ProjectValidationError) as excinfo:
        await update_project(
            seeded_db,
            actor,
            project.id,
            ProjectUpdate(
                land_share=_land_share([{"id": uuid.uuid4(), "name": "A", "share_pct": "100.00"}])
            ),
        )

    assert str(excinfo.value) == SHAREHOLDER_UNKNOWN
    assert [s.name for s in await _rows(seeded_db, project.id)] == ["A"]


async def test_other_projects_shareholder_id_returns_422(seeded_db, user_factory):
    actor = await _writer(seeded_db, user_factory, "p9oth@t.co")
    mine = await _kat_karsiligi(seeded_db, "P9-MINE", [{"name": "Benim", "share_pct": "100.00"}])
    theirs = await _kat_karsiligi(seeded_db, "P9-THRS", [{"name": "Onun", "share_pct": "100.00"}])
    foreign_id = (await _rows(seeded_db, theirs.id))[0].id

    with pytest.raises(ProjectValidationError):
        await update_project(
            seeded_db,
            actor,
            mine.id,
            ProjectUpdate(
                land_share=_land_share(
                    [{"id": foreign_id, "name": "Çalıntı", "share_pct": "100.00"}]
                )
            ),
        )

    assert [s.name for s in await _rows(seeded_db, mine.id)] == ["Benim"]
    assert [s.name for s in await _rows(seeded_db, theirs.id)] == ["Onun"]


async def test_create_with_shareholder_id_returns_422(seeded_db):
    """Yeni projede eslesecek satir yoktur; id tasiyan girdi uydurmadir."""
    with pytest.raises(ProjectValidationError):
        await _kat_karsiligi(
            seeded_db, "P9-NEW", [{"id": uuid.uuid4(), "name": "A", "share_pct": "100.00"}]
        )


# --- 4. API yuzeyi: 409 gercekten HTTP 409'a cikar ---


async def test_api_patch_returns_409_with_turkish_reason(client, seeded_db, user_factory):
    from tests.modules.test_projects_api import _auth, _login

    token = await _login(client, user_factory, "system_admin")
    project = await _kat_karsiligi(
        seeded_db, "P9-API", [{"name": "Atanmış", "share_pct": "100.00"}]
    )
    assigned = (await _rows(seeded_db, project.id))[0]
    await _assign_unit(seeded_db, project, assigned)

    resp = await client.patch(
        f"/projects/{project.id}",
        json={
            "land_share": {
                "landowner_name": "Yılmaz Ailesi",
                "our_share_pct": "55.00",
                "owner_share_pct": "45.00",
                "shareholders": [{"name": "Yeni", "share_pct": "100.00"}],
            }
        },
        headers=_auth(token),
    )

    assert resp.status_code == 409
    assert resp.json()["detail"] == shareholder_has_units(["Atanmış"])
    assert [s.name for s in await _rows(seeded_db, project.id)] == ["Atanmış"]


async def test_duplicate_shareholder_id_returns_422(seeded_db, user_factory):
    """T5 FINAL REVIEW bulgusu: ayni id iki kez -> SESSIZ COKME degil, 422.

    Bulgudan once bu govde 200 doner ve iki girdi tek satira cokerdi: ikinci
    girdinin adi kazanir, ilkinin orani gerekcesiz kaybolurdu. Kullanicinin
    istedigi iki hissedardan biri sessizce yok olmus olurdu — dilimin varlik
    sebebiyle (spec §4.1 "sessiz supurme YOK") ayni siniftan bir hata.
    Allocation ucunun `DUPLICATE_IN_PAYLOAD` kapisinin esi.
    """
    actor = await _writer(seeded_db, user_factory, "p9dup@t.co")
    project = await _kat_karsiligi(seeded_db, "P9-DUP", [{"name": "A", "share_pct": "100.00"}])
    existing_id = (await _rows(seeded_db, project.id))[0].id

    with pytest.raises(ProjectValidationError) as excinfo:
        await update_project(
            seeded_db,
            actor,
            project.id,
            ProjectUpdate(
                land_share=_land_share(
                    [
                        {"id": existing_id, "name": "A", "share_pct": "25.00"},
                        {"id": existing_id, "name": "A ikinci", "share_pct": "25.00"},
                    ]
                )
            ),
        )

    assert str(excinfo.value) == SHAREHOLDER_DUPLICATE_IN_PAYLOAD
    # Atomiklik: reddedilen istek HICBIR satiri degistirmemis olmali.
    rows = await _rows(seeded_db, project.id)
    assert [(s.name, s.share_pct) for s in rows] == [("A", Decimal("100.00"))]
