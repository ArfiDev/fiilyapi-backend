"""DSC-B2 — günlük `PUT lines` YARIŞI: kısıtlı (civil) + kısıtlı (elek) eşzamanlı yazım.

F7: `client` YOK; ayrı tek kullanımlık DB (`_yaris_ortami`), aktör başına AYRI Session,
servis düzeyi (`service.save_lines`). Sabit `sleep` yok: gözlemci `pg_stat_activity`den T2'nin
`site_diary_entries … FOR UPDATE`te BEKLEDİĞİNİ görür (`tests._yaris.kilitte_bekleyen_sorgu`);
her bekleme `asyncio.wait_for` ya da yoklama tavanıyla sınırlıdır (asla sonsuz değil).

## F6 — kilitsiz de veri kaybı YOK (eşdeğer mutant), bu yüzden kilit "bekler" ile kanıtlanır
Birleştirme yolu (`apply_lines_scoped`) gizli satır NESNELERİNİ değiştirmez: T2 kilitsiz
okusa da civil'in satırına UPDATE/DELETE üretmez, kayıp doğmaz. Yani "sonuç doğru" iddiası
kilit mutantını YAKALAMAZ. Kilidi kanıtlayan iddia T2'nin `site_diary_entries … FOR UPDATE`te
BEKLEMESİDİR; pozitif kontrol (1) kilidi kaldırır → T2 hiç beklemeden biter → kırmızı.
Pozitif kontrol (2): gizli satırı SİL-YENİDEN-EKLE eden mutant + kilitsiz → sonuç bozulur
(IntegrityError ya da civil verisi kaybı) — yani "sonuç doğru" iddiasının kendisi de
sahte-yeşil değildir.

## S7 — kısıtlı + KISITSIZ eşzamanlı: "son yazan kazanır" (dokümante karar)
Kilit iki yolu serileştirir; ama kısıtsız kullanıcının TAM DEĞİŞTİRMESİ, gövdesinde kısıtlının
satırı yoksa o satırı siler (kısıtsız ekran her şeyi görür, gövdesi ekranın tamamıdır). Bu
bir hata değil, kısıtsız semantiğidir; testte bu yön KİLİTLENMEZ.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import text

import app.main  # noqa: F401 — EV disiplin sağlayıcısını porta kaydeder
from app.core.discipline_scope import DisciplineScope, visible_item_set
from app.modules.boq.models import BoqItem
from app.modules.catalog.models import EvDiscipline
from app.modules.earned_value.engine import ContractorType
from app.modules.earned_value.models import (
    EvGroupDiscipline,
    EvRevision,
    RevisionStatus,
    UserDiscipline,
)
from app.modules.site_diary import lines as lines_mod
from app.modules.site_diary import repository, service
from app.modules.site_diary.models import DiaryStatus, SiteDiaryEntry, SiteDiaryLine
from app.modules.site_diary.schemas import SiteDiaryLineInput, SiteDiaryLinesSave
from app.modules.users.models import User, UserProjectAccess
from tests._yaris import YARIS_TAVANI_SN, kilitte_bekleyen_sorgu
from tests.earned_value_budget.test_budget_concurrency import _Ortam, _sonlandir, _yaris_ortami

pytestmark = pytest.mark.asyncio

ESKI = datetime(2020, 1, 1, 12, 0, tzinfo=UTC)
GUN = date(2026, 5, 7)
CIVIL_MIKTAR = Decimal(9)
ELEK_MIKTAR = Decimal(8)


class _Zemin:
    """COMMIT'li zeminin kimlikleri: iki kısıtlı kullanıcı, iki kalem, günlük."""

    def __init__(self, civil: uuid.UUID, elek: uuid.UUID, i1: uuid.UUID, i2: uuid.UUID) -> None:
        self.civil, self.elek, self.i1, self.i2 = civil, elek, i1, i2
        self.kab: uuid.UUID
        self.duv: uuid.UUID
        self.entry: uuid.UUID


async def _zemin(ortam: _Ortam) -> _Zemin:
    """civil (KAB) + elek (DUV): I1(G1→KAB) · I2(G2→DUV); günlükte I1·2 · I2·3 · NULL·1."""
    async with ortam.Session() as s:
        civil = await s.get(User, ortam.actor_id)
        assert civil is not None
        elek = User(
            email="elek@yaris.co", password_hash="x", full_name="Elek", role_id=civil.role_id
        )
        duv = EvDiscipline(
            code="DUV",
            name="Elektrik",
            color="#16a34a",
            default_contractor_type=ContractorType.SUBCON,
            sort_order=2,
        )
        s.add_all([elek, duv])
        await s.flush()
        rev = EvRevision(
            site_id=ortam.site_id, number=0, status=RevisionStatus.ACTIVE, frozen_at=ESKI
        )
        s.add(rev)
        await s.flush()
        s.add_all(
            [
                EvGroupDiscipline(
                    revision_id=rev.id,
                    boq_group_id=ortam.group_ids[0],
                    discipline_id=ortam.discipline_id,
                ),
                EvGroupDiscipline(
                    revision_id=rev.id, boq_group_id=ortam.group_ids[1], discipline_id=duv.id
                ),
                UserDiscipline(user_id=civil.id, discipline_id=ortam.discipline_id),
                UserDiscipline(user_id=elek.id, discipline_id=duv.id),
                UserProjectAccess(
                    user_id=civil.id, project_id=ortam.project_id, all_projects=False
                ),
                UserProjectAccess(user_id=elek.id, project_id=ortam.project_id, all_projects=False),
            ]
        )
        kalemler = [
            BoqItem(
                site_id=ortam.site_id,
                group_id=grup,
                code=kod,
                description=ad,
                unit="m3",
                quantity=Decimal(10),
                unit_price=Decimal(10),
                sort_order=n,
            )
            for n, (grup, kod, ad) in enumerate(
                [(ortam.group_ids[0], "01.001", "Beton"), (ortam.group_ids[1], "02.001", "Kablo")],
                start=1,
            )
        ]
        s.add_all(kalemler)
        entry = SiteDiaryEntry(
            site_id=ortam.site_id,
            project_id=ortam.project_id,
            entry_date=GUN,
            status=DiaryStatus.draft,
            created_by=civil.id,
        )
        s.add(entry)
        await s.flush()
        i1, i2 = kalemler
        s.add_all(
            [
                _satir(entry.id, i1, Decimal(2)),
                _satir(entry.id, i2, Decimal(3)),
                _satir(entry.id, None, Decimal(1)),
            ]
        )
        await s.flush()
        await s.execute(
            text("UPDATE site_diary_lines SET created_at=:t, updated_at=:t WHERE entry_id=:e"),
            {"t": ESKI, "e": entry.id},
        )
        zemin = _Zemin(civil.id, elek.id, i1.id, i2.id)
        zemin.kab, zemin.duv, zemin.entry = ortam.discipline_id, duv.id, entry.id
        await s.commit()
        return zemin


def _satir(entry_id: uuid.UUID, kalem: BoqItem | None, miktar: Decimal) -> SiteDiaryLine:
    return SiteDiaryLine(
        entry_id=entry_id,
        boq_item_id=None if kalem is None else kalem.id,
        section_id=None,
        code="00.000" if kalem is None else kalem.code,
        description="Bağı kopmuş" if kalem is None else kalem.description,
        unit="m3",
        unit_price=Decimal(10),
        quantity=miktar,
    )


async def _yaz(
    ortam: _Ortam, session, kim: uuid.UUID, disiplin: uuid.UUID, z: _Zemin, kalem, miktar
):  # noqa: ANN001, ANN202
    """`PUT …/lines` servis gövdesi — COMMIT ETMEZ."""
    actor = await session.get(User, kim)
    govde = SiteDiaryLinesSave(
        lines=[SiteDiaryLineInput(boq_item_id=kalem, quantity=miktar, section_id=None)]
    )
    await service.save_lines(session, actor, z.entry, govde, DisciplineScope.of({disiplin}))


async def _hamlar(ortam: _Ortam, z: _Zemin) -> dict[uuid.UUID | None, tuple]:
    async with ortam.engine.connect() as conn:
        sonuc = await conn.execute(
            text("SELECT * FROM site_diary_lines WHERE entry_id = :e ORDER BY id"), {"e": z.entry}
        )
        return {r.boq_item_id: tuple(r) for r in sonuc.all()}


_GOZLEM_SQL = text(
    "SELECT a.pid, pg_blocking_pids(a.pid) AS engel, "
    "(SELECT count(*) FROM pg_locks l WHERE l.pid = a.pid AND l.granted "
    "AND l.mode = 'RowShareLock' AND l.relation = 'site_diary_entries'::regclass) AS rowshare "
    "FROM pg_stat_activity a WHERE a.datname = current_database() "
    "AND a.pid <> pg_backend_pid() AND a.wait_event_type = 'Lock'"
)


async def _yaris(
    ortam: _Ortam, z: _Zemin, ekstra: dict | None = None
) -> tuple[str | None, BaseException | None, BaseException | None]:
    """T1 civil (commit yok) → T2 elek → gözlemci T2'nin beklemesini görür → T1 commit.

    Döner: (bekleyen sorgu | None, gözlemci hatası, T2 hatası). Gözlemci hatası = T2 kilitte
    BEKLEMEDEN bitti (kilitsiz mutantın imzası).
    """

    async def _ikinci() -> None:
        async with ortam.Session() as s2:
            await _yaz(ortam, s2, z.elek, z.duv, z, z.i2, ELEK_MIKTAR)
            await s2.commit()

    task: asyncio.Task[None] | None = None
    bekleyen: str | None = None
    gozlemci: BaseException | None = None
    async with ortam.Session() as s1:
        try:
            await _yaz(ortam, s1, z.civil, z.kab, z, z.i1, CIVIL_MIKTAR)
            t1_pid, t1_now = (await s1.execute(text("SELECT pg_backend_pid(), now()"))).one()
            if ekstra is not None:
                ekstra.update(t1_pid=t1_pid, t1_now=t1_now)
            task = asyncio.create_task(_ikinci())
            try:
                bekleyen = await kilitte_bekleyen_sorgu(
                    ortam.engine, task, mesaj="T2 (elek) kilit beklemesi"
                )
                if ekstra is not None:  # kilidin SAHİBİ ve T2'nin tuttuğu masa kilidi
                    async with ortam.engine.connect() as conn:
                        ekstra["gozlem"] = (await conn.execute(_GOZLEM_SQL)).all()
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
    except Exception as exc:  # noqa: BLE001 — mutant yolunda IntegrityError beklenir
        t2_hata = exc
    return bekleyen, gozlemci, t2_hata


async def test_iki_kisitli_eszamanli_put_lines_kilitle_serilesir_ve_iki_yazma_yerinde(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with _yaris_ortami() as ortam:
        z = await _zemin(ortam)
        once = await _hamlar(ortam, z)
        ekstra: dict = {}
        bekleyen, gozlemci, t2_hata = await _yaris(ortam, z, ekstra)

        assert gozlemci is None, f"T2 kilitte BEKLEMEDEN bitti: {gozlemci!r}"
        assert bekleyen is not None
        # pg_stat_activity.query 1024 karaktere KIRPILIR: geniş kolon listesi `FOR UPDATE`i keser.
        # Kilitte bekleyebilecek tek deyim günlük SELECT'idir (kilitsiz sürümü hiç beklemez —
        # pozitif kontrol 1), dolayısıyla başı yeter.
        assert bekleyen.startswith("SELECT site_diary_entries.id AS"), bekleyen
        # Kilidin sahibi T1: T2'yi engelleyen TEK pid T1'in backend'i ve T2 günlük masasında
        # RowShareLock'u (FOR UPDATE'in masa kilidi) TUTUYOR.
        ((t2_pid, engel, rowshare),) = ekstra["gozlem"]
        assert list(engel) == [ekstra["t1_pid"]] and t2_pid != ekstra["t1_pid"], ekstra
        assert rowshare >= 1, "T2 site_diary_entries üzerinde RowShareLock tutmuyor"
        assert t2_hata is None, f"T2 başarısız: {t2_hata!r}"
        sonra = await _hamlar(ortam, z)
        # iki yazma da yerinde (civil 9, elek 8) ve satır kümesi aynı
        assert sonra[z.i1][_MIKTAR] == CIVIL_MIKTAR
        assert sonra[z.i2][_MIKTAR] == ELEK_MIKTAR
        assert set(sonra) == set(once)
        # civil satırı TAM tuple: yalnız miktar ve updated_at (= T1'in now()'ı) değişti
        beklenen = list(once[z.i1])
        beklenen[_MIKTAR] = CIVIL_MIKTAR
        beklenen[_KOLONLAR.index("updated_at")] = ekstra["t1_now"]
        assert list(sonra[z.i1]) == beklenen
        # NULL satır bayt bayt aynı
        assert sonra[None] == once[None]


_KOLONLAR = [c.name for c in SiteDiaryLine.__table__.columns]
_MIKTAR = _KOLONLAR.index("quantity")


async def test_KONTROL_kilitsiz_T2_beklemeden_biter_bekleme_iddiasi_kirmizi(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """POZİTİF KONTROL (1): `get_entry_locked` kilitsiz → T2 `FOR UPDATE`te BEKLEMEZ. Sonuç
    yine doğru (F6: eşdeğer mutant) — bu yüzden kilidi kanıtlayan tek iddia bekleme iddiasıdır."""

    async def _kilitsiz(session, entry_id):  # noqa: ANN001, ANN202
        return await repository.get_entry(session, entry_id)

    monkeypatch.setattr(repository, "get_entry_locked", _kilitsiz)
    async with _yaris_ortami() as ortam:
        z = await _zemin(ortam)
        bekleyen, gozlemci, t2_hata = await _yaris(ortam, z)

        assert bekleyen is None and isinstance(gozlemci, AssertionError), (bekleyen, gozlemci)
        assert "BEKLEMEDEN bitti" in str(gozlemci)
        assert t2_hata is None  # eşdeğer mutant: kayıp yok


async def test_KONTROL_sil_yeniden_ekle_ve_kilitsiz_sonucu_bozar(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """POZİTİF KONTROL (2): gizli satırı SİL-YENİDEN-EKLE eden mutant + kilitsiz → T2 gizli
    (civil) satırını kopyalayıp eski değeriyle yazar: IntegrityError ya da civil verisi kaybı.
    Kilitli gerçek yolda bu senaryo yukarıdaki testte TEMİZ geçer."""
    orijinal = lines_mod.apply_lines_scoped

    async def _sil_ekle(session, entry, inputs, scope):  # noqa: ANN001, ANN202
        dropped = await orijinal(session, entry, inputs, scope)
        gorunur = (
            await visible_item_set(
                session, scope, [ln.boq_item_id for ln in entry.lines if ln.boq_item_id]
            )
            or set()
        )
        acik = [ln for ln in entry.lines if ln.boq_item_id in gorunur]
        gizli = [ln for ln in entry.lines if ln.boq_item_id not in gorunur]
        kopyalar = [
            SiteDiaryLine(
                entry_id=ln.entry_id,
                boq_item_id=ln.boq_item_id,
                section_id=ln.section_id,
                code=ln.code,
                description=ln.description,
                unit=ln.unit,
                unit_price=ln.unit_price,
                quantity=ln.quantity,
            )
            for ln in gizli
        ]
        entry.lines = acik  # önce SİL (UQ çakışmasın) ...
        await session.flush()
        entry.lines = [*acik, *kopyalar]  # ... sonra YENİDEN EKLE
        await session.flush()
        return dropped

    async def _kilitsiz(session, entry_id):  # noqa: ANN001, ANN202
        return await repository.get_entry(session, entry_id)

    monkeypatch.setattr(lines_mod, "apply_lines_scoped", _sil_ekle)
    monkeypatch.setattr(repository, "get_entry_locked", _kilitsiz)
    async with _yaris_ortami() as ortam:
        z = await _zemin(ortam)
        once = await _hamlar(ortam, z)
        _, _, t2_hata = await _yaris(ortam, z)

        sonra = await _hamlar(ortam, z)
        civil_kayip = sonra.get(z.i1) is None or (
            sonra[z.i1][_KOLONLAR.index("quantity")] != CIVIL_MIKTAR
            or sonra[z.i1][0] != once[z.i1][0]
        )
        assert t2_hata is not None or civil_kayip, "mutant + kilitsiz de temiz geçti"
