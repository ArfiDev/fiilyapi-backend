"""DSC-B2 — EV günlük dağıtım YARIŞI: günün İLK kaydı, iki farklı disiplin eşzamanlı.

F7: `client` YOK; ayrı tek kullanımlık DB (`_yaris_ortami`), aktör başına AYRI Session,
servis düzeyi (`diary_adapter.save_allocation`). Sabit `sleep` yok: gözlemci `pg_stat_activity`den
T2'nin `sites … FOR UPDATE`te BEKLEDİĞİNİ ve engelleyenin T1 olduğunu görür
(`tests._yaris.kilitte_bekleyen_sorgu` + `pg_blocking_pids`); her bekleme tavanlıdır.

## Neden kilit ŞART (F6'nın tersi)
Gün boşken (İLK kayıt) iki disiplin de paylaşılan kişi satırını (`ev_day_rows`, kişi × gün
tekil) EKLEMEK ister. Kilitsiz T2 boş okur, T1'in commit edilmemiş satırıyla
`uq_ev_day_rows_personnel` üzerinde çakışır → `IntegrityError` (kullanıcıya 500).
Pozitif kontrol kilidi kaldırır ve bunu GÖSTERİR: kilit iddiası sahte-yeşil değildir.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from app.core.discipline_scope import DisciplineScope
from app.modules.boq.models import BoqItem, BoqItemSectionAllocation
from app.modules.catalog.models import EvDiscipline
from app.modules.earned_value import access, diary_adapter
from app.modules.earned_value import budget_ops as ops
from app.modules.earned_value import budget_service as svc
from app.modules.earned_value.access import SiteContext
from app.modules.earned_value.engine import ContractorType
from app.modules.earned_value.models import EvDayCell, EvDayCode, EvDayRow
from app.modules.personnel.models import Personnel
from app.modules.projects.models import Project
from app.modules.site_diary.models import WorkerSource
from app.modules.sites.models import Section, Site
from app.modules.timesheet.models import TimesheetEntry
from app.modules.users.models import User
from tests._yaris import YARIS_TAVANI_SN, kilitte_bekleyen_sorgu
from tests.earned_value_budget.test_budget_concurrency import _Ortam, _sonlandir, _yaris_ortami

pytestmark = pytest.mark.asyncio

GUN = date(2026, 5, 5)


class _Zemin:
    def __init__(self) -> None:
        self.civil: uuid.UUID
        self.elek: uuid.UUID
        self.kab: uuid.UUID
        self.duv: uuid.UUID
        self.k1: str
        self.k2: str
        self.ali: uuid.UUID


async def _zemin(ortam: _Ortam) -> _Zemin:
    """Donmuş baseline: G1→KAB (I1) · G2→DUV (I2), tek bölüm, Ali 9 sa; COMMIT'li."""
    z = _Zemin()
    async with ortam.Session() as s:
        civil = await s.get(User, ortam.actor_id)
        site = await s.get(Site, ortam.site_id)
        project = await s.get(Project, ortam.project_id)
        assert civil and site and project
        elek = User(
            email="elek@ev-yaris.co", password_hash="x", full_name="Elek", role_id=civil.role_id
        )
        duv = EvDiscipline(
            code="DUV",
            name="Elektrik",
            color="#16a34a",
            default_contractor_type=ContractorType.SUBCON,
            sort_order=2,
        )
        bolum = Section(
            site_id=site.id,
            name="A Blok",
            start_date=date(2026, 5, 4),
            end_date=date(2026, 5, 15),
            planned_worker_count=5,
            sort_order=1,
        )
        ali = Personnel(
            full_name="Ali Usta",
            trade="Kalıpçı",
            source=WorkerSource.company,
            is_active=True,
            is_draft=False,
        )
        s.add_all([elek, duv, bolum, ali])
        await s.flush()
        kalemler = [
            BoqItem(
                site_id=site.id,
                group_id=grup,
                code=kod,
                description=ad,
                unit="m3",
                quantity=Decimal(10),
                unit_price=Decimal(0),
                sort_order=n,
            )
            for n, (grup, kod, ad) in enumerate(
                [(ortam.group_ids[0], "01.001", "Beton"), (ortam.group_ids[1], "02.001", "Kablo")],
                start=1,
            )
        ]
        s.add_all(kalemler)
        await s.flush()
        s.add_all(
            [
                BoqItemSectionAllocation(
                    boq_item_id=k.id, section_id=bolum.id, quantity=Decimal(10)
                )
                for k in kalemler
            ]
        )
        s.add(
            TimesheetEntry(
                personnel_id=ali.id,
                site_id=site.id,
                project_id=project.id,
                work_date=GUN,
                hours=Decimal(9),
                created_by=civil.id,
            )
        )
        ctx = SiteContext(site=site, project=project)
        await svc.set_group_disciplines(
            s,
            ctx,
            civil,
            [(ortam.group_ids[0], ortam.discipline_id), (ortam.group_ids[1], duv.id)],
        )
        await svc.patch_leaves(
            s,
            ctx,
            civil,
            [svc.LeafChange(k.id, bolum.id, {"unit_mhr": Decimal(2)}) for k in kalemler],
        )
        await ops.freeze(s, ctx, civil, None, None)
        z.civil, z.elek, z.kab, z.duv = civil.id, elek.id, ortam.discipline_id, duv.id
        z.k1, z.k2 = (f"l:{k.id}:{bolum.id}" for k in kalemler)
        z.ali = ali.id
        await s.commit()
    return z


async def _kaydet(session, kim, disiplin, ortam, ali, node, saat):  # noqa: ANN001, ANN202
    """`PUT …/allocation` servis gövdesi (kısıtlı) — COMMIT ETMEZ."""
    actor = await session.get(User, kim)
    await diary_adapter.save_allocation(
        session,
        ortam.site_id,
        GUN,
        actor,
        [(node, "direct")],
        [diary_adapter.CellIn("personnel", ali, node, Decimal(saat))],
        None,
        scope=DisciplineScope.of({disiplin}),
        reason_provided=False,
    )


_GOZLEM = text(
    "SELECT a.pid, pg_blocking_pids(a.pid) AS engel, a.wait_event_type FROM pg_stat_activity a "
    "WHERE a.datname = current_database() AND a.pid <> pg_backend_pid() "
    "AND a.wait_event_type = 'Lock'"
)


async def _yaris(ortam: _Ortam, z: _Zemin):  # noqa: ANN202
    """T1 civil (commit yok) → T2 elek → gözlemci T2'nin beklemesini görür → T1 commit.
    Döner: (bekleyen sorgu | None, gözlem, gözlemci hatası, T2 hatası, T1 pid)."""

    async def _ikinci() -> None:
        async with ortam.Session() as s2:
            await _kaydet(s2, z.elek, z.duv, ortam, z.ali, z.k2, 5)
            await s2.commit()

    task: asyncio.Task[None] | None = None
    bekleyen: str | None = None
    gozlem: list = []
    gozlemci: BaseException | None = None
    async with ortam.Session() as s1:
        try:
            await _kaydet(s1, z.civil, z.kab, ortam, z.ali, z.k1, 4)
            t1_pid = (await s1.execute(text("SELECT pg_backend_pid()"))).scalar_one()
            task = asyncio.create_task(_ikinci())
            try:
                bekleyen = await kilitte_bekleyen_sorgu(
                    ortam.engine, task, mesaj="T2 (elek) kilit beklemesi"
                )
                async with ortam.engine.connect() as conn:
                    gozlem = list((await conn.execute(_GOZLEM)).all())
            except AssertionError as exc:
                gozlemci = exc
            await s1.commit()
        except BaseException:
            await s1.rollback()
            await _sonlandir(task)
            raise
    assert task is not None
    t2_hata: BaseException | None = None
    try:
        await asyncio.wait_for(task, YARIS_TAVANI_SN)
    except TimeoutError:
        raise
    except Exception as exc:  # noqa: BLE001 — kilitsiz yolda IntegrityError beklenir
        t2_hata = exc
    return bekleyen, gozlem, gozlemci, t2_hata, t1_pid


async def _sonuc(ortam: _Ortam):  # noqa: ANN202
    async with ortam.Session() as s:
        satirlar = (await s.execute(select(EvDayRow).where(EvDayRow.day == GUN))).scalars().all()
        hucreler = (await s.execute(select(EvDayCell))).scalars().all()
        kodlar = (await s.execute(select(EvDayCode).where(EvDayCode.day == GUN))).scalars().all()
    return satirlar, {c.node_id: c.hours for c in hucreler}, {c.node_id for c in kodlar}


async def test_gunun_ilk_kaydinda_iki_disiplin_site_kilidiyle_serilesir_iki_taraf_yerinde() -> None:
    async with _yaris_ortami() as ortam:
        z = await _zemin(ortam)
        bekleyen, gozlem, gozlemci, t2_hata, t1_pid = await _yaris(ortam, z)

        assert gozlemci is None, f"T2 kilitte BEKLEMEDEN bitti: {gozlemci!r}"
        assert bekleyen is not None and "FROM sites" in bekleyen and "FOR UPDATE" in bekleyen, (
            bekleyen
        )
        ((t2_pid, engel, tur),) = gozlem
        assert list(engel) == [t1_pid] and t2_pid != t1_pid and tur == "Lock", gozlem
        assert t2_hata is None, f"T2 başarısız: {t2_hata!r}"
        satirlar, hucreler, kodlar = await _sonuc(ortam)
        assert len(satirlar) == 1 and satirlar[0].personnel_id == z.ali  # paylaşılan satır TEK
        assert hucreler == {z.k1: Decimal(4), z.k2: Decimal(5)}
        assert kodlar == {z.k1, z.k2}


async def test_KONTROL_kilitsiz_T2_beklemez_uq_cakismasi_kirmizi(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """POZİTİF KONTROL: kilit kaldırılınca T2 `sites FOR UPDATE`te BEKLEMEZ; T1'in
    commit edilmemiş kişi satırında (`uq_ev_day_rows_personnel`) bekler ve commit sonrası
    IntegrityError alır — yani ana testin kilit iddiası kilit kalkınca KIRMIZIya döner."""
    orijinal = access.assert_site_writable

    async def _kilitsiz(session, site_id, *, message, lock=True):  # noqa: ANN001, ANN202, ARG001
        await orijinal(session, site_id, message=message, lock=False)

    monkeypatch.setattr(diary_adapter, "assert_site_writable", _kilitsiz)
    async with _yaris_ortami() as ortam:
        z = await _zemin(ortam)
        bekleyen, _, gozlemci, t2_hata, _ = await _yaris(ortam, z)

        assert gozlemci is None and bekleyen is not None
        assert "FOR UPDATE" not in bekleyen and bekleyen.startswith("INSERT INTO ev_day_rows"), (
            bekleyen
        )
        assert isinstance(t2_hata, IntegrityError), f"kilitsiz de temiz geçti: {t2_hata!r}"
