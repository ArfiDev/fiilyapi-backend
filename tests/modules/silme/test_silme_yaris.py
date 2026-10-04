"""SIL-B1 onarımı — silme ile eşzamanlı DERİN ekleme yarışı (çürütücü bulgusu).

Neden `client`/`db_session` KULLANILMAZ: `db_session` (conftest) her testi TEK bağlantıda
SAVEPOINT'e sarar; iki görev AYNI bağlantıyı paylaşır, gerçek satır kilidi test EDİLEMEZ. Bu test
`test_engine`'den İKİ BAĞIMSIZ bağlantı açar, kurulum verisini GERÇEKTEN commit eder ve sonunda
GERÇEKTEN temizler (`tests/progress_payments/test_concurrency.py` deseni).

Senaryo: şantiye silinirken (karma doğrulandı, kök ve ağaç KİLİTLİ) başka bir bağlantı MEVCUT
bölüme `section_milestones` (derinlik-2) ekler. Kilit çalışıyorsa ekleme BEKLER (INSERT'in aldığı
`FOR KEY SHARE`, kilitli bölüm satırıyla çakışır) ve silme bitince FK hatasıyla düşer: önizlenmemiş
satır ASLA sessizce silinmez, denetim sayısı doğru kalır. Kilit YOKSA ekleme hemen commit olur ve
DB CASCADE onu önizlenmeden siler: bekleme iddiası (`kilitte_bekleyen_sorgu`) KIRMIZI olur.
"""

import asyncio
import uuid
from datetime import date

import pytest
from sqlalchemy import delete, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.silme.hatalar import DeletePreviewStaleError
from app.modules.projects.models import Project
from app.modules.silme import service
from app.modules.silme.schemas import DeleteKind
from app.modules.sites.models import Section, SectionMilestone, Site
from tests._yaris import YARIS_TAVANI_SN, kilitte_bekleyen_sorgu
from tests.conftest import test_engine

pytestmark = pytest.mark.asyncio

_Fabrika = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)


async def _kurulum() -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    async with _Fabrika() as oturum:
        proje = Project(code="SIL-YARIS-1", name="Silme Yarışı Projesi")
        oturum.add(proje)
        await oturum.flush()
        site = Site(project_id=proje.id, code="SNT-YARIS", name="Yarış Şantiyesi")
        oturum.add(site)
        await oturum.flush()
        bolum = Section(site_id=site.id, name="Mevcut Bölüm")
        oturum.add(bolum)
        await oturum.flush()
        kimlikler = (proje.id, site.id, bolum.id)
        await oturum.commit()
    return kimlikler


async def _temizle(proje_id: uuid.UUID) -> None:
    async with _Fabrika() as oturum:
        await oturum.execute(
            delete(Project).where(Project.id == proje_id)
        )  # CASCADE: şantiye ağacı
        await oturum.commit()


async def _sayim(tablo: str, kosul: str = "true") -> int:
    async with _Fabrika() as oturum:
        return int(
            (await oturum.execute(text(f"SELECT count(*) FROM {tablo} WHERE {kosul}"))).scalar_one()
        )


async def test_karma_sonrasi_derin_ekleme_bekler_onizlenmeyen_satir_silinmez(monkeypatch) -> None:
    proje_id, site_id, bolum_id = await _kurulum()
    gorev_a: asyncio.Task[str] | None = None
    gorev_b: asyncio.Task[None] | None = None
    devam = asyncio.Event()
    try:
        async with _Fabrika() as oturum:
            token = (await service.onizle(oturum, DeleteKind.site, site_id)).preview_token

        kilitli = asyncio.Event()
        gercek = service.agaci_sil

        async def bariyerli(oturum, metadata, agac):  # karma doğrulandı, silme HENÜZ başlamadı
            kilitli.set()
            await devam.wait()
            return await gercek(oturum, metadata, agac)

        monkeypatch.setattr(service, "agaci_sil", bariyerli)

        async def silici() -> str:
            async with _Fabrika() as oturum:
                detay = await service.sil(oturum, "site", site_id, token)
                await oturum.commit()
                return detay

        async def ekleyici() -> None:
            async with _Fabrika() as oturum:
                oturum.add(
                    SectionMilestone(
                        section_id=bolum_id, title="Geç Kalan", milestone_date=date(2026, 7, 1)
                    )
                )
                await oturum.commit()

        gorev_a = asyncio.create_task(silici())
        await asyncio.wait_for(kilitli.wait(), timeout=YARIS_TAVANI_SN)  # A kilitleri aldı, TUTUYOR

        gorev_b = asyncio.create_task(ekleyici())
        bekleyen = await kilitte_bekleyen_sorgu(
            test_engine, gorev_b, mesaj="derin ekleme kilitli ağaçta BEKLEMELİ"
        )
        assert "section_milestones" in bekleyen, bekleyen
        assert not gorev_b.done()  # `not done` bariyeri: ekleme hâlâ bloke

        devam.set()
        detay = await asyncio.wait_for(gorev_a, timeout=YARIS_TAVANI_SN)

        # Silme bitince bölüm yok: bekleyen ekleme FK ile DÜŞER, satır ASLA oluşmaz
        with pytest.raises(IntegrityError):
            await asyncio.wait_for(gorev_b, timeout=YARIS_TAVANI_SN)
        assert await _sayim("section_milestones") == 0
        assert await _sayim("sites", f"id = '{site_id}'") == 0
        # denetim sayısı önizlemeyle birebir: yalnız `Bölüm 1` (milestone hiç girmedi)
        assert "1 bağlı kayıtla birlikte silindi (Bölüm 1)" in detay
    finally:
        devam.set()
        for gorev in (gorev_a, gorev_b):
            if gorev is not None and not gorev.done():
                gorev.cancel()
        await asyncio.gather(
            *(g for g in (gorev_a, gorev_b) if g is not None), return_exceptions=True
        )
        await _temizle(proje_id)


async def test_silme_sirasinda_kisit_ihlali_genel_409_degil_preview_stale(monkeypatch) -> None:
    """Derinlik-2 RESTRICT yarışı: kısıt ihlali opak 409 yerine `preview_stale` döner."""
    proje_id, site_id, _ = await _kurulum()
    try:
        async with _Fabrika() as oturum:
            token = (await service.onizle(oturum, DeleteKind.site, site_id)).preview_token

        async def kisit_ihlali(oturum, metadata, agac):
            raise IntegrityError("DELETE", {}, Exception("restrict"))

        monkeypatch.setattr(service, "agaci_sil", kisit_ihlali)
        async with _Fabrika() as oturum:
            with pytest.raises(DeletePreviewStaleError):
                await service.sil(oturum, "site", site_id, token)
            await oturum.rollback()
        assert await _sayim("sites", f"id = '{site_id}'") == 1  # hiçbir şey silinmedi
    finally:
        await _temizle(proje_id)
