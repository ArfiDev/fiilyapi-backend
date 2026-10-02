"""TKL-B6.8b BD-5 — eszamanli iki proje olusturma: kod uretici DANISMA KILIDI altinda.

Kilitsiz `max+1` iki oturumda AYNI kodu uretir (biri digerinin commit etmedigi satiri goremez);
`projects.code` UQ'su ikinciyi 409'a ceviren bir CIKIS verir. Kilitle ikinci istek birincinin
commit'ini BEKLER ve ardisik kodu alir.

Duzenek emsali: `tests/modules/offers/test_offers_race.py` (tutulan-kilit + `pg_stat_activity`
bariyeri + POZITIF KONTROL). Not (kapsam DISI, ayni sinif borc): SNT santiye kodu sirket genelinde
max+1'dir (UQ proje basina).
"""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.timezone import today
from app.modules.projects import service as project_service
from app.modules.projects.models import Project
from app.modules.projects.schemas import ProjectCreate

from .offers.test_offers_race import _ortam, _yaris

pytestmark = pytest.mark.asyncio


def _taslak(ad: str) -> ProjectCreate:
    return ProjectCreate(name=ad, project_type="taahhut", is_draft=True)


async def _kodlar(ortam) -> list[str]:
    async with ortam.Session() as s:
        return sorted((await s.scalars(select(Project.code))).all())


async def _iki_olusturma(ortam, *, adlar: tuple[str, str]):
    def _is_for(ad: str):
        async def _is(session: AsyncSession) -> None:
            await project_service.create_project(session, _taslak(ad))

        return _is

    return await _yaris(ortam, _is_for(adlar[0]), _is_for(adlar[1]))


async def test_BD5_iki_eszamanli_proje_ardisik_iki_kod_ikisi_commit() -> None:
    yil = today().year
    async with _ortam(gonderilmis=False) as ortam:
        # AYNI ad: kod kilidi (slug'dan once, islem boyu) slug ayirmasini da serilestirir.
        bekleyen, hata = await _iki_olusturma(ortam, adlar=("Yarış Projesi", "Yarış Projesi"))

        assert "pg_advisory_xact_lock" in bekleyen, bekleyen
        assert hata is None, f"ikinci olusturma temiz gecmeliydi: {hata!r}"
        assert await _kodlar(ortam) == [f"PRJ-{yil}-001", f"PRJ-{yil}-002"]


async def test_BD5_KONTROL_kilitsiz_ikinci_olusturma_UQ_cokusu(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """POZITIF KONTROL: kilit KAPALI → ikisi de max'i 0 okur, ikisi `PRJ-YYYY-001` yazar; ikinci
    INSERT birincinin UQ girdisinde BEKLER, commit sonrasi IntegrityError (jenerik 409). Bu
    kirmizi, ustteki bekcinin KILIDI gercekten olctugunun kanitidir."""

    async def _kilitsiz(session: AsyncSession, year: int) -> None:
        return None

    monkeypatch.setattr(project_service, "_lock_project_code_sequence", _kilitsiz)
    async with _ortam(gonderilmis=False) as ortam:
        # FARKLI ad: slug carpismasi karismasin, UQ ihlali KOD'dan gelsin.
        bekleyen, hata = await _iki_olusturma(ortam, adlar=("Yarış A", "Yarış B"))

        assert bekleyen.startswith("INSERT INTO projects"), bekleyen
        assert isinstance(hata, IntegrityError), f"kilitsiz de temiz gecti: {hata!r}"
        assert "ix_projects_code" in str(hata.orig) or "projects_code_key" in str(hata.orig)
        assert len(await _kodlar(ortam)) == 1


async def test_R2_elle_uyumlu_kod_otomatikle_serilesir_ardisik_kod() -> None:
    """TKL-B6.9 R2: A ELLE `PRJ-{yil}-001` verir (commit etmeden bekler); B otomatik uretir. Elle
    yol da yil kilidini aldigindan B KILITTE bekler, A commit edince `002` alir (UQ cokusu yok)."""
    yil = today().year

    async def elle(session: AsyncSession) -> None:
        await project_service.create_project(
            session,
            ProjectCreate(
                code=f"PRJ-{yil}-001", name="Elle", project_type="taahhut", is_draft=True
            ),
        )

    async def oto(session: AsyncSession) -> None:
        await project_service.create_project(session, _taslak("Oto"))

    async with _ortam(gonderilmis=False) as ortam:
        bekleyen, hata = await _yaris(ortam, elle, oto)

        assert "pg_advisory_xact_lock" in bekleyen, bekleyen
        assert hata is None, f"otomatik uretim temiz gecmeliydi: {hata!r}"
        assert await _kodlar(ortam) == [f"PRJ-{yil}-001", f"PRJ-{yil}-002"]


async def test_R2_KONTROL_elle_yolda_kilit_kapaliyken_UQ_cokusu(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """POZITIF KONTROL: kilit KAPALI → otomatik uretim elle satiri (commit'siz) gormez, ayni
    `001`i yazar; commit sonrasi IntegrityError. Ustteki bekcinin kilidi olctugunun kanitidir."""
    yil = today().year

    async def _kilitsiz(session: AsyncSession, year: int) -> None:
        return None

    monkeypatch.setattr(project_service, "_lock_project_code_sequence", _kilitsiz)

    async def elle(session: AsyncSession) -> None:
        await project_service.create_project(
            session,
            ProjectCreate(
                code=f"PRJ-{yil}-001", name="Elle", project_type="taahhut", is_draft=True
            ),
        )

    async def oto(session: AsyncSession) -> None:
        await project_service.create_project(session, _taslak("Oto"))

    async with _ortam(gonderilmis=False) as ortam:
        bekleyen, hata = await _yaris(ortam, elle, oto)

        assert bekleyen.startswith("INSERT INTO projects"), bekleyen
        assert isinstance(hata, IntegrityError), f"kilitsiz de temiz gecti: {hata!r}"
        assert len(await _kodlar(ortam)) == 1
