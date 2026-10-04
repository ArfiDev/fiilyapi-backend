"""GKS-B1 — iki eşzamanlı `POST /sites/{id}/diary` (+ `lines[]`) AYNI gün için: biri 201, biri 409.

UQ (site_id, entry_date) modelde (`uq_site_diary_entries_site_date`) ve migration'da
(`b5c6d7e8f9a0_site_diary_cekirdegi`) VARDIR. `_assert_date_free` ön kontrolü commit EDİLMEMİŞ
satırı göremez; yarışı kaybeden istek INSERT'te UQ'yu bekler, kazanan commit edince
`IntegrityError` alır ve GENEL `IntegrityError → 409` işleyicisi ("Veri bütünlüğü hatası")
devreye girir. Satır ekli istekte de değişen bir şey YOKTUR: UQ ihlali günlük INSERT'ünde olur.

Neden `client`/`seeded_db` KULLANILMAZ: `tests/contracts/test_distribution_concurrency.py` ile aynı
gerekçe (kök `db_session` tek bağlantı + SAVEPOINT; gerçek satır kilidi ölçülemez). Burada iki
BAĞIMSIZ bağlantı ve gerçek commit vardır; istek kendi oturumuyla (gerçek `get_db` sözleşmesi:
temiz çıkışta commit, istisnada rollback) koşar. Yarış DETERMİNİSTİKTİR: birinci istek, ikincisi
UQ kilidinde BEKLEYENE kadar commit'ini bir kapıda tutar (`kilitte_bekleyen_sorgu`).
"""

import asyncio
import contextlib
import uuid
from collections.abc import AsyncGenerator
from datetime import date
from decimal import Decimal

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.access import AccessLevel
from app.core.db import get_db
from app.core.sayfalar import PageLevel
from app.core.security import create_access_token, hash_password
from app.main import app
from app.modules.boq.models import BoqGroup, BoqItem
from app.modules.projects.models import Project
from app.modules.roles.models import Module, ModuleGroup, Role, RolePagePermission, RolePermission
from app.modules.site_diary import guards, service
from app.modules.site_diary.models import SiteDiaryEntry, SiteDiaryLine
from app.modules.sites.models import Site
from app.modules.users.models import User, UserProjectAccess
from tests._yaris import YARIS_TAVANI_SN, kilitte_bekleyen_sorgu
from tests.conftest import test_engine

pytestmark = pytest.mark.asyncio

_SessionFactory = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
_ROLE_KEY = "gks_b1_yarisi"
_GUN = date(2026, 8, 3)
_INTEGRITY_DETAIL = "Veri bütünlüğü hatası"


class _Kurulum:
    def __init__(self, user: User, project_id, site_id, item_id, module_created: bool) -> None:
        self.user = user
        self.project_id = project_id
        self.site_id = site_id
        self.item_id = item_id
        self.module_created = module_created

    @property
    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {create_access_token(self.user.id, 0)}"}

    def govde(self, miktar: str = "4") -> dict:
        return {
            "entry_date": _GUN.isoformat(),
            "lines": [{"boq_item_id": str(self.item_id), "quantity": miktar, "section_id": None}],
        }


@pytest.fixture(autouse=True)
async def _mu3d_esleme() -> None:
    """Paket conftest'indeki AUTOUSE fikstürü BİLİNÇLİ ezer: o `seeded_db` açar (commit EDİLMEMİŞ
    `modules`/`roles` satırları) ve bu dosyanın gerçek commit'li kurulumu aynı benzersiz anahtarda
    o transaction'ı SÜRESİZ bekler (ölçüldü: askıda kaldı). Hakediş fişlemesi bu dosyada yok."""


@pytest.fixture
async def kurulum():
    veri = await _kur()
    try:
        yield veri
    finally:
        await _temizle(veri)


async def _kur() -> _Kurulum:
    async with _SessionFactory() as session:
        modul = (
            await session.execute(select(Module).where(Module.key == service.PERMISSION_MODULE))
        ).scalar_one_or_none()
        olusturuldu = modul is None
        if modul is None:
            modul = Module(key=service.PERMISSION_MODULE, name="Günlük", group=ModuleGroup.SAHA)
            session.add(modul)
        role = Role(key=_ROLE_KEY, name="GKS-B1 Yarışı")
        session.add(role)
        await session.flush()
        session.add(
            RolePermission(role_id=role.id, module_id=modul.id, access_level=AccessLevel.full)
        )
        # IZN-B2: kapılar sayfa hücresinden karar verir (`site_diary:view/full` = Günlük Kayıt).
        session.add(
            RolePagePermission(
                role_id=role.id,
                page_key="saha.gunluk_kayit",
                level=PageLevel.edit,
                can_approve=False,
            )
        )
        user = User(
            email="gks-b1-yaris@sd.co",
            password_hash=hash_password("parola1234"),
            full_name="GKS-B1 Yarış",
            role_id=role.id,
        )
        project = Project(code="GKS-B1-Y", name="GKS-B1 Yarış Projesi")
        session.add_all([user, project])
        await session.flush()
        session.add(UserProjectAccess(user_id=user.id, project_id=project.id, all_projects=False))
        site = Site(project_id=project.id, code="GKS-B1-S", name="Yarış Şantiyesi")
        session.add(site)
        await session.flush()
        group = BoqGroup(site_id=site.id, name="A", sort_order=0)
        session.add(group)
        await session.flush()
        item = BoqItem(
            site_id=site.id,
            group_id=group.id,
            code="01.001",
            description="Beton",
            unit="m3",
            quantity=Decimal("100"),
            unit_price=Decimal("10.00"),
            sort_order=0,
        )
        session.add(item)
        await session.commit()
        return _Kurulum(user, project.id, site.id, item.id, olusturuldu)


async def _temizle(k: _Kurulum) -> None:
    async with _SessionFactory() as session:
        await session.execute(delete(SiteDiaryEntry).where(SiteDiaryEntry.site_id == k.site_id))
        await session.execute(delete(BoqItem).where(BoqItem.site_id == k.site_id))
        await session.execute(delete(BoqGroup).where(BoqGroup.site_id == k.site_id))
        await session.execute(delete(Site).where(Site.id == k.site_id))
        await session.execute(delete(Project).where(Project.id == k.project_id))
        await session.execute(delete(User).where(User.id == k.user.id))
        await session.execute(delete(Role).where(Role.key == _ROLE_KEY))
        if k.module_created:
            await session.execute(delete(Module).where(Module.key == service.PERMISSION_MODULE))
        await session.commit()


class _Kapi:
    """`get_db` sözleşmesinin kopyası + commit'i tutan kapı (yarışı belirler)."""

    def __init__(self) -> None:
        self.birinci_hazir = asyncio.Event()
        self.birak = asyncio.Event()

    async def get_db(self) -> AsyncGenerator[AsyncSession, None]:
        async with _SessionFactory() as session:
            try:
                yield session
                self.birinci_hazir.set()  # işleyici bitti; commit'ten ÖNCE
                await asyncio.wait_for(self.birak.wait(), timeout=YARIS_TAVANI_SN)
                await session.commit()
            except Exception:
                await session.rollback()
                raise


@pytest.fixture
async def http():
    kapi = _Kapi()
    app.dependency_overrides[get_db] = kapi.get_db
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as istemci:
        try:
            yield istemci, kapi
        finally:
            kapi.birak.set()
            app.dependency_overrides.clear()


async def _yaris(http, k: _Kurulum) -> tuple:
    """İstek 1 commit kapısında BEKLER; istek 2 UQ kilidinde bloke olunca kapı açılır."""
    istemci, kapi = http
    adres = f"/sites/{k.site_id}/diary"
    t1 = asyncio.create_task(istemci.post(adres, json=k.govde("4"), headers=k.headers))
    t2: asyncio.Task | None = None
    try:
        await asyncio.wait_for(kapi.birinci_hazir.wait(), timeout=YARIS_TAVANI_SN)
        t2 = asyncio.create_task(istemci.post(adres, json=k.govde("9"), headers=k.headers))
        bekleyen = await kilitte_bekleyen_sorgu(
            test_engine,
            t2,
            mesaj="ikinci istek UQ kilidinde BEKLEMEDEN ilerledi — yarış kurulamadı",
        )
        assert "site_diary_entries" in bekleyen, bekleyen
        kapi.birak.set()
        return await asyncio.wait_for(t1, YARIS_TAVANI_SN), await asyncio.wait_for(
            t2, YARIS_TAVANI_SN
        )
    finally:
        kapi.birak.set()
        for gorev in (t1, t2):
            if gorev is not None and not gorev.done():
                gorev.cancel()
            if gorev is not None:
                with contextlib.suppress(BaseException):
                    await gorev


async def _dogrula_tek_gunluk(k: _Kurulum, kazanan_miktar: str) -> None:
    async with _SessionFactory() as session:
        gunluk = await session.scalar(
            select(func.count())
            .select_from(SiteDiaryEntry)
            .where(SiteDiaryEntry.site_id == k.site_id)
        )
        miktarlar = (
            (
                await session.execute(
                    select(SiteDiaryLine.quantity)
                    .join(SiteDiaryEntry, SiteDiaryEntry.id == SiteDiaryLine.entry_id)
                    .where(SiteDiaryEntry.site_id == k.site_id)
                )
            )
            .scalars()
            .all()
        )
    assert gunluk == 1
    assert miktarlar == [Decimal(kazanan_miktar)]  # kaybedenin satırı sızmadı


async def test_iki_eszamanli_post_lines_biri_201_digeri_409_500_yok(http, kurulum) -> None:
    yanit1, yanit2 = await _yaris(http, kurulum)

    assert yanit1.status_code == 201, yanit1.text
    assert yanit2.status_code == 409, yanit2.text  # 500 DEĞİL
    # Ön kontrol commit edilmemiş satırı göremez → gerçek bariyer genel IntegrityError ağıdır.
    assert yanit2.json()["detail"] == _INTEGRITY_DETAIL
    await _dogrula_tek_gunluk(kurulum, "4")


async def test_pozitif_kontrol_on_kontrol_devre_disiyken_de_409_500_degil(
    http, kurulum, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`_assert_date_free` no-op: UQ + genel `IntegrityError → 409` ağı TEK BAŞINA yeter."""

    async def _atla(*args, **kwargs) -> None:
        return None

    monkeypatch.setattr(service, "_assert_date_free", _atla)

    yanit1, yanit2 = await _yaris(http, kurulum)

    assert (yanit1.status_code, yanit2.status_code) == (201, 409), (yanit1.text, yanit2.text)
    assert yanit2.json()["detail"] == _INTEGRITY_DETAIL
    await _dogrula_tek_gunluk(kurulum, "4")


async def test_ardisik_ikinci_post_on_kontrol_mesajiyla_409(http, kurulum) -> None:
    """Yarış yok (birinci commit etti): kullanıcı net cümleyi görür, ağın genel cümlesini değil."""
    istemci, kapi = http
    kapi.birak.set()
    adres = f"/sites/{kurulum.site_id}/diary"
    ilk = await istemci.post(adres, json=kurulum.govde("4"), headers=kurulum.headers)
    ikinci = await istemci.post(adres, json=kurulum.govde("9"), headers=kurulum.headers)

    assert (ilk.status_code, ikinci.status_code) == (201, 409)
    assert ikinci.json()["detail"] == guards.ENTRY_DATE_TAKEN
    await _dogrula_tek_gunluk(kurulum, "4")


async def test_uq_modelde_ve_migrationda_var(kurulum) -> None:
    """Ölçüm sabitleme: UQ yoksa yukarıdaki 409 kanıtı anlamsızdır."""
    adlar = {c.name for c in SiteDiaryEntry.__table__.constraints}
    assert "uq_site_diary_entries_site_date" in adlar
    from pathlib import Path

    migration = Path("alembic/versions/b5c6d7e8f9a0_site_diary_cekirdegi.py").read_text()
    assert "uq_site_diary_entries_site_date" in migration
    assert uuid.UUID(int=0)  # (import kullanımı)
