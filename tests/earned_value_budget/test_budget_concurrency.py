"""§3.11 B1-13 — bütçe yazmasının şantiye satırı kilidi (`_lock_site`, `FOR UPDATE`) bekçisi.

Yarış: hiç revizyonu olmayan şantiyede iki eşzamanlı "ilk yazma". Kilitsiz ikisi de
"taslak yok, revizyon yok" okur ve ikisi de Rev 0 INSERT eder; kısmi UQ
(`uq_ev_revisions_one_draft`) ikincisini `IntegrityError` ile keser → kullanıcıya 500.
Kilitle ikinci yazma sıraya girer, birinci commit edince AYNI taslağı görür ve başarılıdır.

## Neden paylaşılan `db_session` YETMEZ
Kök `db_session` her testi TEK bağlantıda bir SAVEPOINT'e sarar ve asla commit etmez: iki
görev aynı bağlantıyı paylaşır, satır kilidi hiç yarışmaz. Bu dosya kendi tek kullanımlık
veritabanını açar (FIS-NO emsali), iki BAĞIMSIZ oturumla yarıştırır ve sonunda düşürür.

## 🔴 Ölçüldü: "ikinci görev bekledi" TEK BAŞINA kilidi KANITLAMAZ
Kilitsiz hâlde de ikinci oturum BEKLER — ama başka yerde: birincinin commit edilmemiş Rev 0
satırıyla çakışan UQ kontrolünde (`INSERT INTO ev_revisions`, `Lock/transactionid`). Yalın bir
`not done` bariyeri bu yüzden KÖR olurdu. Bariyer beklemenin NEREDE olduğunu da ölçer:
`pg_stat_activity`de (`datname` = bu dosyanın veritabanı, `pid` ≠ gözlemcinin kendisi)
bekleyen sorgu `sites … FOR UPDATE` olmalıdır. Pozitif kontrol (`_lock_site` no-op) aynı
senaryonun gerçekten ikinci Rev 0 INSERT'ine ve `IntegrityError`a düştüğünü gösterir —
aletin ölçtüğünün kanıtı.

⚠️ FAT-1 dersi: bekçi HEM TEK BAŞINA HEM DOSYA/PAKET BÜTÜN koşturulup raporlanır.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass

import asyncpg
import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import settings
from app.core.db import Base
from app.modules.boq.models import BoqGroup
from app.modules.catalog.models import EvDiscipline
from app.modules.earned_value import budget_service as svc
from app.modules.earned_value.access import SiteContext
from app.modules.earned_value.engine import ContractorType
from app.modules.earned_value.models import EvGroupDiscipline, EvRevision
from app.modules.projects.models import Project
from app.modules.roles.models import Role
from app.modules.sites.models import Site
from app.modules.users.models import User
from tests._yaris import YARIS_TAVANI_SN

pytestmark = pytest.mark.asyncio

#: Bariyer süreleri. Bekleyen sorguyu arama sabit uyku DEĞİL, koşul yoklamasıdır.
#: FIX-B3: ortak geniş tavan (5 sn CI yükünde yetmiyordu; bkz. `tests/_yaris.py`).
_BEKLEME_SINIRI = YARIS_TAVANI_SN
_YOKLAMA_ARALIGI = 0.05
_KESISME_PAYI = 0.3


def _asyncpg_dsn(database: str) -> str:
    base = settings.test_database_url.replace("postgresql+asyncpg://", "postgresql://")
    return base.rsplit("/", 1)[0] + f"/{database}"


def _sqlalchemy_dsn(database: str) -> str:
    return settings.test_database_url.rsplit("/", 1)[0] + f"/{database}"


async def _admin(sql: str) -> None:
    conn = await asyncpg.connect(_asyncpg_dsn("postgres"))
    try:
        await conn.execute(sql)
    finally:
        await conn.close()


@dataclass(frozen=True, slots=True)
class _Ortam:
    database: str
    engine: AsyncEngine
    Session: async_sessionmaker[AsyncSession]
    actor_id: uuid.UUID
    project_id: uuid.UUID
    site_id: uuid.UUID
    group_ids: tuple[uuid.UUID, uuid.UUID]
    discipline_id: uuid.UUID


@asynccontextmanager
async def _yaris_ortami():  # noqa: ANN201
    """Tek kullanımlık veritabanı + en küçük FK zemini (COMMIT'li); sonunda GERÇEKTEN düşer."""
    database = f"ev_butce_yaris_{uuid.uuid4().hex[:8]}"
    await _admin(f'CREATE DATABASE "{database}"')
    engine = create_async_engine(_sqlalchemy_dsn(database))
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        async with factory() as s:
            role = Role(key="ev_butce_yaris", name="EV Bütçe Yarış Rolü")
            s.add(role)
            await s.flush()
            user = User(
                email="ev-yaris@butce.co", password_hash="x", full_name="Yarış", role_id=role.id
            )
            project = Project(code="EV-YARIS", name="Yarış Projesi")
            s.add_all([user, project])
            await s.flush()
            site = Site(project_id=project.id, code="EV-Y", name="Yarış Şantiyesi")
            s.add(site)
            await s.flush()
            g1 = BoqGroup(site_id=site.id, name="Betonarme", sort_order=1)
            g2 = BoqGroup(site_id=site.id, name="Duvar", sort_order=2)
            disc = EvDiscipline(
                code="KAB",
                name="Kaba",
                color="#2563eb",
                default_contractor_type=ContractorType.OWN,
                sort_order=1,
            )
            s.add_all([g1, g2, disc])
            await s.flush()
            await s.commit()
            ortam = _Ortam(
                database, engine, factory, user.id, project.id, site.id, (g1.id, g2.id), disc.id
            )
        yield ortam
    finally:
        await engine.dispose()
        await _admin(f'DROP DATABASE IF EXISTS "{database}" WITH (FORCE)')


async def _ilk_yazma(ortam: _Ortam, session: AsyncSession, group_id: uuid.UUID) -> uuid.UUID:
    """`PUT …/budget/group-disciplines`in servis gövdesi — COMMIT ETMEZ. Taslağın id'sini döner."""
    actor = await session.get(User, ortam.actor_id)
    site = await session.get(Site, ortam.site_id)
    project = await session.get(Project, ortam.project_id)
    assert actor and site and project
    ctx = SiteContext(site=site, project=project)
    await svc.set_group_disciplines(session, ctx, actor, [(group_id, ortam.discipline_id)])
    row = await session.scalar(
        select(EvGroupDiscipline).where(EvGroupDiscipline.boq_group_id == group_id)
    )
    assert row is not None
    return row.revision_id


async def _ikinci_oturum(ortam: _Ortam, group_id: uuid.UUID) -> uuid.UUID:
    async with ortam.Session() as session:
        rev_id = await _ilk_yazma(ortam, session, group_id)
        await session.commit()
        return rev_id


async def _bekleyen_sorgu(ortam: _Ortam) -> str:
    """Bu veritabanında `Lock` bekleyen oturumun sorgusu (gözlemcinin kendisi hariç).

    🔴 `pg_stat_activity` SUNUCU GENELİDİR: `datname` süzgeci olmadan komşu bir testin
    bekleyeni bariyeri tatmin ederdi. Her yoklama YENİ bir transaction'da koşar —
    istatistik görüntüsü transaction başına önbelleklenir.
    """
    sql = text(
        "SELECT query FROM pg_stat_activity "
        "WHERE datname = :db AND pid <> pg_backend_pid() AND wait_event_type = 'Lock'"
    )
    loop = asyncio.get_running_loop()
    son = loop.time() + _BEKLEME_SINIRI
    while loop.time() < son:
        async with ortam.engine.connect() as conn:
            rows = (await conn.execute(sql, {"db": ortam.database})).scalars().all()
        if rows:
            assert len(rows) == 1, rows
            return " ".join(rows[0].split())
        await asyncio.sleep(_YOKLAMA_ARALIGI)
    raise AssertionError(
        f"ikinci oturum {_BEKLEME_SINIRI} sn içinde HİÇ kilit beklemedi — yarış penceresi açılmadı"
    )


async def _sonlandir(task: asyncio.Task | None) -> None:
    """Öksüz görev bırakma: bir iddia kırmızıya dönse de bağlantı iş ortasında kopmasın."""
    if task is None:
        return
    if not task.done():
        with contextlib.suppress(BaseException):
            await asyncio.wait_for(asyncio.shield(task), timeout=_BEKLEME_SINIRI)
    if not task.done():
        task.cancel()
    with contextlib.suppress(BaseException):
        await task


@dataclass(frozen=True, slots=True)
class _Sonuc:
    bekleyen: str
    birinci: uuid.UUID
    ikinci: uuid.UUID | BaseException


async def _yaris(ortam: _Ortam) -> _Sonuc:
    """Tutulan-kilit senaryosu: oturum 1 ilk yazmayı yapar ve COMMIT ETMEDEN bekler;
    oturum 2 başlatılır, beklemesi ölçülür; sonra oturum 1 commit eder."""
    task: asyncio.Task[uuid.UUID] | None = None
    async with ortam.Session() as birinci:
        try:
            rev1 = await _ilk_yazma(ortam, birinci, ortam.group_ids[0])
            task = asyncio.create_task(_ikinci_oturum(ortam, ortam.group_ids[1]))
            bekleyen = await _bekleyen_sorgu(ortam)
            await asyncio.sleep(_KESISME_PAYI)
            assert not task.done(), "ikinci oturum birinci commit etmeden BİTTİ — yarış kesişmedi"
            await birinci.commit()
        except BaseException:
            # Kırmızı ASILMADAN kırmızı görünmeli: kilit bırakılır, görev sonlandırılır.
            await birinci.rollback()
            await _sonlandir(task)
            raise
    assert task is not None
    try:
        ikinci: uuid.UUID | BaseException = await asyncio.wait_for(task, _BEKLEME_SINIRI)
    except (IntegrityError, TimeoutError) as exc:
        ikinci = exc
    return _Sonuc(bekleyen, rev1, ikinci)


async def _revizyonlar(ortam: _Ortam) -> list[tuple[int, str]]:
    async with ortam.Session() as s:
        rows = await s.execute(
            select(EvRevision.number, EvRevision.status).where(EvRevision.site_id == ortam.site_id)
        )
        return [(n, st.value) for n, st in rows.all()]


async def test_B1_13_concurrent_first_writes_share_one_rev0_draft() -> None:
    async with _yaris_ortami() as ortam:
        sonuc = await _yaris(ortam)

        assert "FROM sites" in sonuc.bekleyen and "FOR UPDATE" in sonuc.bekleyen, (
            "ikinci oturum şantiye satırı kilidinde BEKLEMİYOR — `_lock_site` kilitlemiyor; "
            f"bekleyen sorgu: {sonuc.bekleyen}"
        )
        assert sonuc.ikinci == sonuc.birinci, f"ikinci yazma aynı taslağı görmedi: {sonuc.ikinci!r}"
        assert await _revizyonlar(ortam) == [(0, "draft")]
        async with ortam.Session() as s:
            eslemeler = await s.scalar(
                select(func.count())
                .select_from(EvGroupDiscipline)
                .where(EvGroupDiscipline.revision_id == sonuc.birinci)
            )
        assert eslemeler == 2  # iki yazmanın ikisi de TEK taslağa işledi


async def test_B1_13_KONTROL_without_site_lock_second_rev0_hits_integrity_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """POZİTİF KONTROL: kilit no-op iken AYNI senaryo gerçekten ikinci Rev 0'a düşer.

    Bu vaka kırmızıya dönerse (ör. ikinci yazma başarılı) senaryo yarışı artık üretmiyor
    demektir ve üstteki bekçinin yeşili hiçbir şey kanıtlamaz.
    """

    async def _kilitsiz(session: AsyncSession, site_id: uuid.UUID) -> None:
        return None

    monkeypatch.setattr(svc, "_lock_site", _kilitsiz)
    async with _yaris_ortami() as ortam:
        sonuc = await _yaris(ortam)

        assert sonuc.bekleyen.startswith("INSERT INTO ev_revisions"), sonuc.bekleyen
        assert isinstance(sonuc.ikinci, IntegrityError), f"ikinci Rev 0 kesilmedi: {sonuc.ikinci!r}"
        assert "uq_ev_revisions" in str(sonuc.ikinci.orig)
        assert await _revizyonlar(ortam) == [(0, "draft")]
