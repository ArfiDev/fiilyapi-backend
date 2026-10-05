"""DSC-B0 — disiplin kapsami portu + EV saglayicisi + silme sayimi (user_count).

Bu dilim HICBIR ucu suzmez; burada yalniz (1) port bos/dolu davranisi, (2) kalem→disiplin
TEK SQL tanimi (AKTIF ?? TASLAK, santiye siniri, arsiv yok), (3) SQL ↔ Python esdegerligi ve
fail-closed NULL, (4) atanmis disiplin silme 409'u, (5) FK CASCADE/RESTRICT sinanir.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import discipline_scope as port
from app.core.discipline_scope import UNRESTRICTED, DisciplineScope
from app.modules.boq.models import BoqGroup, BoqItem
from app.modules.catalog.models import EvDiscipline
from app.modules.earned_value import discipline_adapter, guards
from app.modules.earned_value.models import (
    EvGroupDiscipline,
    EvRevision,
    RevisionStatus,
)
from app.modules.site_diary.models import SiteDiaryEntry, SiteDiaryLine
from app.modules.sites.models import Site
from app.modules.users.models import ProjectMemberDiscipline, User
from tests._proje_ekibi import disiplin_ata, ekibe_ekle

URL = "/earned-value/disciplines"


@pytest.fixture
def portu_koru():
    """`unregister_all()` kullanan testler sonunda kaydi geri yukler (SAHTE-YESIL onlemi)."""
    snapshot = port.registered()
    yield
    port.restore(snapshot)


# --- yardimcilar ------------------------------------------------------------------------


async def _revision(
    session: AsyncSession, site: Site, status: RevisionStatus, number: int
) -> EvRevision:
    rev = EvRevision(
        site_id=site.id,
        number=number,
        status=status,
        frozen_at=None if status is RevisionStatus.DRAFT else datetime.now(UTC),
    )
    session.add(rev)
    await session.flush()
    return rev


async def _group_item(session: AsyncSession, site: Site, code: str) -> tuple[BoqGroup, BoqItem]:
    group = BoqGroup(site_id=site.id, name=f"GRUP {code}")
    session.add(group)
    await session.flush()
    item = BoqItem(
        site_id=site.id,
        group_id=group.id,
        code=code,
        description=f"Kalem {code}",
        unit="m³",
        quantity=Decimal("1"),
        unit_price=Decimal("1"),
        sort_order=0,
    )
    session.add(item)
    await session.flush()
    return group, item


async def _map(session: AsyncSession, rev: EvRevision, group: BoqGroup, disc: EvDiscipline):
    session.add(EvGroupDiscipline(revision_id=rev.id, boq_group_id=group.id, discipline_id=disc.id))
    await session.flush()


async def _disc_of(session: AsyncSession, item: BoqItem) -> uuid.UUID | None:
    return (await port.item_disciplines(session, [item.id]))[item.id]


@pytest.fixture
async def civil(disiplin_fabrikasi) -> EvDiscipline:
    return await disiplin_fabrikasi("CIV", "İnşaat")


@pytest.fixture
async def elek(disiplin_fabrikasi) -> EvDiscipline:
    return await disiplin_fabrikasi("ELK", "Elektrik")


@pytest.fixture
async def kullanici(seeded_db: AsyncSession, user_factory) -> User:
    return await user_factory(
        email=f"dsc.{uuid.uuid4().hex[:8]}@dsc-b0.co", password="parola1234", role_key="site_chief"
    )


# --- port: kayit yokken bos -------------------------------------------------------------


async def test_kayit_yokken_port_bos(seeded_db: AsyncSession, portu_koru) -> None:
    port.unregister_all()
    user_id = uuid.uuid4()
    assert await port.user_scope(seeded_db, user_id) == UNRESTRICTED
    assert not UNRESTRICTED.is_restricted
    assert str(port.item_visible_clause(UNRESTRICTED, BoqItem)) == "true"
    assert str(port.item_discipline_expr(BoqItem)) == "NULL"
    item_id = uuid.uuid4()
    assert await port.item_disciplines(seeded_db, [item_id]) == {item_id: None}
    assert await port.item_disciplines(seeded_db, []) == {}
    # kayit yokken KISITLI kapsam verilse bile ifade NULL.IN(..) → hicbir sey gorunmez
    scope = DisciplineScope(frozenset({uuid.uuid4()}))
    assert "IN" in str(port.item_visible_clause(scope, BoqItem))


def test_bos_atama_kisitsiz_sayilir() -> None:
    assert DisciplineScope.of(set()) is UNRESTRICTED
    assert not DisciplineScope(frozenset()).is_restricted
    assert DisciplineScope.of({uuid.uuid4()}).is_restricted


def test_app_main_yuklenince_port_kayitli() -> None:
    import app.main  # noqa: F401

    assert port.registered() is discipline_adapter.PROVIDER


def test_ikinci_farkli_saglayici_hata_ayni_nesne_idempotent() -> None:
    port.register_provider(discipline_adapter.PROVIDER)  # ayni nesne: no-op
    with pytest.raises(RuntimeError):
        port.register_provider(discipline_adapter.EvDisciplineProvider())


# --- eslesme: R(site) = AKTIF ?? TASLAK ---------------------------------------------------


async def test_revizyonsuz_santiye_null(seeded_db, santiye, civil) -> None:
    _, item = await _group_item(seeded_db, santiye, "A1")
    assert await _disc_of(seeded_db, item) is None


async def test_yalniz_taslak_taslagin_eslemesi(seeded_db, santiye, civil) -> None:
    group, item = await _group_item(seeded_db, santiye, "A1")
    draft = await _revision(seeded_db, santiye, RevisionStatus.DRAFT, 1)
    await _map(seeded_db, draft, group, civil)
    assert await _disc_of(seeded_db, item) == civil.id


async def test_yalniz_aktif_aktifin_eslemesi(seeded_db, santiye, civil) -> None:
    group, item = await _group_item(seeded_db, santiye, "A1")
    active = await _revision(seeded_db, santiye, RevisionStatus.ACTIVE, 1)
    await _map(seeded_db, active, group, civil)
    assert await _disc_of(seeded_db, item) == civil.id


async def test_aktif_ve_taslak_farkli_esleme_AKTIF_kazanir(seeded_db, santiye, civil, elek) -> None:
    group, item = await _group_item(seeded_db, santiye, "A1")
    active = await _revision(seeded_db, santiye, RevisionStatus.ACTIVE, 1)
    draft = await _revision(seeded_db, santiye, RevisionStatus.DRAFT, 2)
    await _map(seeded_db, active, group, civil)
    await _map(seeded_db, draft, group, elek)
    assert await _disc_of(seeded_db, item) == civil.id  # taslak (elek) DEGIL


async def test_taslak_ONCE_aktif_SONRA_eklenince_de_aktif_kazanir(
    seeded_db, santiye, civil, elek
) -> None:
    """ORDER BY silinirse LIMIT 1 heap sirasina kalir; onceki test aktifi ONCE ekledigi icin
    sansla dogru cikardi. Burada taslak once eklenir: siralama olmadan taslak (elek) doner."""
    group, item = await _group_item(seeded_db, santiye, "A1")
    draft = await _revision(seeded_db, santiye, RevisionStatus.DRAFT, 1)
    active = await _revision(seeded_db, santiye, RevisionStatus.ACTIVE, 2)
    await _map(seeded_db, draft, group, elek)
    await _map(seeded_db, active, group, civil)
    assert await _disc_of(seeded_db, item) == civil.id


async def test_arsiv_yok_sayilir(seeded_db, santiye, civil, elek) -> None:
    group, item = await _group_item(seeded_db, santiye, "A1")
    archived = await _revision(seeded_db, santiye, RevisionStatus.ARCHIVED, 1)
    await _map(seeded_db, archived, group, civil)
    assert await _disc_of(seeded_db, item) is None  # yalniz arsiv → NULL
    draft = await _revision(seeded_db, santiye, RevisionStatus.DRAFT, 2)
    await _map(seeded_db, draft, group, elek)
    assert await _disc_of(seeded_db, item) == elek.id  # arsivi degil taslagi gorur


async def test_eslemesiz_grup_null(seeded_db, santiye, civil) -> None:
    _, item = await _group_item(seeded_db, santiye, "A1")
    other_group, _ = await _group_item(seeded_db, santiye, "A2")
    active = await _revision(seeded_db, santiye, RevisionStatus.ACTIVE, 1)
    await _map(seeded_db, active, other_group, civil)  # A1'in grubu eslenmedi
    assert await _disc_of(seeded_db, item) is None


async def test_iki_santiye_baska_santiyenin_revizyonu_secilmez(
    seeded_db, santiye, ikinci_santiye, civil, elek
) -> None:
    """A'da AKTIF, B'de yalniz TASLAK: B kalemi kendi taslaginin eslemesini almali.
    `EvRevision.site_id == item.site_id` sarti silinirse ORDER BY/LIMIT A'nin aktifini secer
    (B kalemi grubuna eslemesi yok → NULL doner) ve bu test kirmizi olur."""
    _, item_a = await _group_item(seeded_db, santiye, "A1")
    group_b, item_b = await _group_item(seeded_db, ikinci_santiye, "B1")
    await _revision(seeded_db, santiye, RevisionStatus.ACTIVE, 1)
    draft_b = await _revision(seeded_db, ikinci_santiye, RevisionStatus.DRAFT, 1)
    await _map(seeded_db, draft_b, group_b, elek)
    assert await _disc_of(seeded_db, item_b) == elek.id
    assert await _disc_of(seeded_db, item_a) is None


# --- SQL ↔ Python esdegerligi, fail-closed ------------------------------------------------


async def test_sql_ve_python_ayni_kume_null_kisitlida_gorunmez(
    seeded_db, santiye, civil, elek
) -> None:
    g1, i1 = await _group_item(seeded_db, santiye, "C1")
    g2, i2 = await _group_item(seeded_db, santiye, "E1")
    _, i3 = await _group_item(seeded_db, santiye, "N1")  # eslemesiz → NULL
    rev = await _revision(seeded_db, santiye, RevisionStatus.ACTIVE, 1)
    await _map(seeded_db, rev, g1, civil)
    await _map(seeded_db, rev, g2, elek)
    ids = [i1.id, i2.id, i3.id]

    py = await port.item_disciplines(seeded_db, ids)
    assert py == {i1.id: civil.id, i2.id: elek.id, i3.id: None}

    scope = DisciplineScope.of({civil.id})
    visible = set(
        (
            await seeded_db.execute(
                select(BoqItem.id).where(
                    BoqItem.id.in_(ids), port.item_visible_clause(scope, BoqItem)
                )
            )
        ).scalars()
    )
    assert visible == {i for i, d in py.items() if d in scope.discipline_ids}
    assert visible == {i1.id}  # NULL kalem (i3) ve baska disiplin (i2) GORUNMEZ

    unrestricted = set(
        (
            await seeded_db.execute(
                select(BoqItem.id).where(
                    BoqItem.id.in_(ids), port.item_visible_clause(UNRESTRICTED, BoqItem)
                )
            )
        ).scalars()
    )
    assert unrestricted == set(ids)  # NULL dahil hepsi gorunur


async def test_tum_disiplinler_atanmis_kullanicida_NULL_kalem_gorunmez(
    seeded_db, santiye, kullanici, civil, elek
) -> None:
    """NOT IN mutanti: tumleyen ("kullanicinin OLMAYAN disiplinleri") bos oldugunda
    `NULL NOT IN (bos kume)` TRUE olur → fail-open. Sirketin TUM disiplinleri atanir."""
    g1, i1 = await _group_item(seeded_db, santiye, "C1")
    _, i_null = await _group_item(seeded_db, santiye, "N1")
    rev = await _revision(seeded_db, santiye, RevisionStatus.ACTIVE, 1)
    await _map(seeded_db, rev, g1, civil)
    await disiplin_ata(seeded_db, kullanici, santiye.project_id, civil.id)
    await disiplin_ata(seeded_db, kullanici, santiye.project_id, elek.id)
    scope = await port.user_scope(seeded_db, kullanici.id, santiye.project_id)
    assert scope.discipline_ids == frozenset(
        (await seeded_db.execute(select(EvDiscipline.id))).scalars()
    )  # tumleyen bos
    rows = await seeded_db.execute(
        select(BoqItem.id).where(
            BoqItem.id.in_([i1.id, i_null.id]), port.item_visible_clause(scope, BoqItem)
        )
    )
    assert set(rows.scalars()) == {i1.id}


# --- baska tablodan suzme: visible_item_ids -------------------------------------------------


async def _diary_line(
    session, santiye, item: BoqItem, code: str, user: User, day: int
) -> SiteDiaryLine:
    entry = SiteDiaryEntry(
        site_id=santiye.id,
        project_id=santiye.project_id,
        entry_date=date(2026, 1, day),
        created_by=user.id,
    )
    session.add(entry)
    await session.flush()
    line = SiteDiaryLine(
        entry_id=entry.id,
        boq_item_id=item.id,
        code=code,
        description="d",
        unit="m",
        unit_price=Decimal("1"),
        quantity=Decimal("1"),
    )
    session.add(line)
    await session.flush()
    return line


async def _visible_codes(session, scope) -> set[str]:
    stmt = select(SiteDiaryLine.code).where(
        SiteDiaryLine.boq_item_id.in_(port.visible_item_ids(scope, BoqItem))
    )
    return set((await session.execute(stmt)).scalars())


async def test_visible_item_ids_baska_tablodan_suzer_tek_esleme(
    seeded_db, santiye, civil, elek, kullanici
) -> None:
    """Ölçülen tuzak: `item_visible_clause` cikplak kullanilirsa (BoqItem dis FROM'da yok)
    tek eslemede {'X','C'} doner (fail-open). `visible_item_ids` yalniz {'C'} vermeli."""
    gc, ic = await _group_item(seeded_db, santiye, "C")
    ge, ie = await _group_item(seeded_db, santiye, "E")
    _, i_null = await _group_item(seeded_db, santiye, "X")  # eslemesiz
    rev = await _revision(seeded_db, santiye, RevisionStatus.ACTIVE, 1)
    await _map(seeded_db, rev, gc, civil)
    await _map(seeded_db, rev, ge, elek)
    for day, item in enumerate((ic, ie, i_null), start=1):
        await _diary_line(seeded_db, santiye, item, item.code, kullanici, day)

    assert await _visible_codes(seeded_db, DisciplineScope.of({civil.id})) == {"C"}
    assert await _visible_codes(seeded_db, DisciplineScope.of({elek.id})) == {"E"}
    assert await _visible_codes(seeded_db, UNRESTRICTED) == {"C", "E", "X"}


async def test_visible_item_ids_cok_eslemede_CardinalityViolation_yok(
    seeded_db, santiye, civil, elek, kullanici
) -> None:
    g1, i1 = await _group_item(seeded_db, santiye, "C1")
    g2, i2 = await _group_item(seeded_db, santiye, "E1")
    rev = await _revision(seeded_db, santiye, RevisionStatus.ACTIVE, 1)
    await _map(seeded_db, rev, g1, civil)
    await _map(seeded_db, rev, g2, elek)
    await _diary_line(seeded_db, santiye, i1, "C1", kullanici, 1)
    await _diary_line(seeded_db, santiye, i2, "E1", kullanici, 2)
    scope = DisciplineScope.of({civil.id, elek.id})
    assert await _visible_codes(seeded_db, scope) == {"C1", "E1"}


# --- item_disciplines parcalama --------------------------------------------------------------


async def test_item_disciplines_parametre_sinirini_asan_id_sayisi_hata_vermez(seeded_db) -> None:
    """asyncpg 32767 parametre siniri: 40k id parcalanmazsa `InterfaceError` (olculdu)."""
    ids = [uuid.uuid4() for _ in range(40_000)]
    result = await port.item_disciplines(seeded_db, ids)
    assert len(result) == 40_000
    assert set(result.values()) == {None}


async def test_item_disciplines_parcalar_birlestirilir(
    seeded_db, santiye, civil, monkeypatch
) -> None:
    monkeypatch.setattr(port, "ITEM_ID_CHUNK", 2)
    group, _ = await _group_item(seeded_db, santiye, "P0")
    items = [(await _group_item(seeded_db, santiye, f"P{n}"))[1] for n in range(1, 6)]
    rev = await _revision(seeded_db, santiye, RevisionStatus.ACTIVE, 1)
    await _map(seeded_db, rev, group, civil)
    for it in items:
        it.group_id = group.id
    await seeded_db.flush()
    result = await port.item_disciplines(seeded_db, [it.id for it in items])
    assert result == {it.id: civil.id for it in items}  # 3 parca, tek sozluk


async def test_user_scope_atama_yok_kisitsiz_var_kisitli(
    seeded_db, santiye, kullanici, civil, elek
) -> None:
    proje = santiye.project_id
    assert await port.user_scope(seeded_db, kullanici.id, proje) == UNRESTRICTED
    assert await port.user_scope(seeded_db, kullanici.id) == UNRESTRICTED  # çok proje de kısıtsız
    await disiplin_ata(seeded_db, kullanici, proje, civil.id)
    await disiplin_ata(seeded_db, kullanici, proje, elek.id)
    scope = await port.user_scope(seeded_db, kullanici.id, proje)
    assert scope.is_restricted
    assert scope.discipline_ids == frozenset({civil.id, elek.id})


async def test_user_scope_PROJE_BASINA_baska_projede_kisitsiz_cok_projede_harita(
    seeded_db, santiye, kullanici, civil, elek, project_factory
) -> None:
    """IZN-B3: aynı kişi A projesinde civil ile kısıtlı, B projesinde atamasız (kısıtsız); proje
    baglamsiz (çok proje) kapsam yalnız KISITLI projelerin haritasıdır."""
    a, b = santiye.project_id, (await project_factory("DSC-B")).id
    await disiplin_ata(seeded_db, kullanici, a, civil.id)
    await ekibe_ekle(seeded_db, kullanici, b)  # üye ama disiplin yok
    assert (await port.user_scope(seeded_db, kullanici.id, a)).discipline_ids == frozenset(
        {civil.id}
    )
    assert await port.user_scope(seeded_db, kullanici.id, b) == UNRESTRICTED
    assert await port.user_scope(seeded_db, kullanici.id, uuid.uuid4()) == UNRESTRICTED  # üye değil
    cok = await port.user_scope(seeded_db, kullanici.id)
    assert cok.is_multi_project and cok.is_restricted and cok.discipline_ids is None
    assert cok.by_project == {a: frozenset({civil.id})}
    assert cok.for_project(a).discipline_ids == frozenset({civil.id})
    assert cok.for_project(b) == UNRESTRICTED
    # Çok proje kapsamı tek-proje koduna FAIL-CLOSED: hiçbir kalem görünmez.
    g, i = await _group_item(seeded_db, santiye, "MP1")
    rev = await _revision(seeded_db, santiye, RevisionStatus.ACTIVE, 1)
    await _map(seeded_db, rev, g, civil)
    rows = await seeded_db.execute(
        select(BoqItem.id).where(BoqItem.id == i.id, port.item_visible_clause(cok, BoqItem))
    )
    assert list(rows.scalars()) == []


async def test_partition_by_project_kisitsizlari_tek_parcada_kisitlilari_ayri_toplar() -> None:
    a, b, c = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    d1, d2 = uuid.uuid4(), uuid.uuid4()
    cok = DisciplineScope.of_projects({a: {d1}, b: set(), c: {d1, d2}})
    parcalar = port.partition_by_project(cok, [a, b, c, uuid.UUID(int=7)])
    kapsamlar = {frozenset(ids): kapsam for kapsam, ids in parcalar}
    assert len(parcalar) == 3
    assert {k for k in kapsamlar if b in k} == {frozenset({b, uuid.UUID(int=7)})}
    assert kapsamlar[frozenset({b, uuid.UUID(int=7)})] == UNRESTRICTED
    assert kapsamlar[frozenset({a})].discipline_ids == frozenset({d1})
    # Tek-proje / kısıtsız kapsam: tek parça.
    assert port.partition_by_project(UNRESTRICTED, [a, b]) == [(UNRESTRICTED, [a, b])]
    assert port.partition_by_project(DisciplineScope.of({d1}), []) == []


async def test_tum_projeler_kisisi_bayat_disiplin_satirina_ragmen_kisitsiz(
    seeded_db, santiye, kullanici, civil
) -> None:
    await disiplin_ata(seeded_db, kullanici, santiye.project_id, civil.id)
    assert (await port.user_scope(seeded_db, kullanici.id, santiye.project_id)).is_restricted
    kullanici.all_projects = True
    await seeded_db.flush()
    assert await port.user_scope(seeded_db, kullanici.id, santiye.project_id) == UNRESTRICTED
    assert await port.user_scope(seeded_db, kullanici.id) == UNRESTRICTED


# --- silme sayimi ---------------------------------------------------------------------


async def test_atanmis_disiplin_409_atama_kalkinca_silinir(
    client: AsyncClient, admin, seeded_db, santiye, kullanici, civil
) -> None:
    await disiplin_ata(seeded_db, kullanici, santiye.project_id, civil.id)
    row = next(r for r in (await client.get(URL, headers=admin)).json() if r["id"] == str(civil.id))
    assert row["user_count"] == 1
    resp = await client.delete(f"{URL}/{civil.id}", headers=admin)
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"] == guards.DISCIPLINE_ASSIGNED_TO_USERS
    assert await seeded_db.get(EvDiscipline, civil.id) is not None

    await seeded_db.execute(delete(ProjectMemberDiscipline))
    await seeded_db.flush()
    row = next(r for r in (await client.get(URL, headers=admin)).json() if r["id"] == str(civil.id))
    assert row["user_count"] == 0
    assert (await client.delete(f"{URL}/{civil.id}", headers=admin)).status_code == 204


# --- FK davranisi ------------------------------------------------------------------------


async def test_kullanici_silinince_ekip_ve_atamalari_gider(
    seeded_db, santiye, kullanici, civil
) -> None:
    await disiplin_ata(seeded_db, kullanici, santiye.project_id, civil.id)
    await seeded_db.execute(delete(User).where(User.id == kullanici.id))
    await seeded_db.flush()
    left = (await seeded_db.execute(select(ProjectMemberDiscipline))).scalars().all()
    assert left == []
    assert await seeded_db.get(EvDiscipline, civil.id) is not None


async def test_atanmis_disiplin_dogrudan_silinemez_RESTRICT(
    seeded_db, santiye, kullanici, civil
) -> None:
    await disiplin_ata(seeded_db, kullanici, santiye.project_id, civil.id)
    with pytest.raises(IntegrityError):
        async with seeded_db.begin_nested():
            await seeded_db.execute(delete(EvDiscipline).where(EvDiscipline.id == civil.id))
