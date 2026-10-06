"""B4-B7 blok/ünite uç testlerinin PAYLAŞILAN kurulumu.

`test_units_api.py` 800 satır tavanını aşınca bölündü (`_journal.py` emsali):
yardımcılar KOPYALANMADI, buraya alındı — iki kopya olsaydı biri güncellenip
öteki kalır ve iki dosya AYNI ismi taşıyan FARKLI gövdelerle koşardı.

Bu yardımcıları ünite ailesinin ONDAN FAZLA test dosyası kullanıyor; daha önce
bir TEST dosyasından (`test_units_api`) import ediliyorlardı. Artık kaynakları
bu yardımcı modüldür. Hiçbir testin iddiası bu bölmeyle değişmedi.
"""

import uuid

from sqlalchemy import func, select

from app.modules.sites.models import Site
from app.modules.units.models import Block, Unit, UnitKind


async def _login(client, user_factory, role_key: str, email: str | None = None) -> str:
    address = email or f"{role_key}@t.co"
    await user_factory(email=address, password="parola1234", role_key=role_key)
    resp = await client.post("/auth/login", json={"email": address, "password": "parola1234"})
    return resp.json()["access_token"]


async def _login_with_access(client, session, user_factory, role_key: str) -> str:
    """system_admin disindaki roller icin gorunurluk `user_project_access`'ten gelir."""
    address = f"{role_key}@t.co"
    user = await user_factory(email=address, password="parola1234", role_key=role_key)
    user.all_projects = True
    await session.flush()
    resp = await client.post("/auth/login", json={"email": address, "password": "parola1234"})
    return resp.json()["access_token"]


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _site(session, project, code: str = "SANTIYE-1", name: str = "Merkez") -> Site:
    site = Site(project_id=project.id, code=code, name=name)
    session.add(site)
    await session.flush()
    return site


async def _block(session, project, site, name: str = "A Blok", **kwargs) -> Block:
    block = Block(project_id=project.id, site_id=site.id, name=name, **kwargs)
    session.add(block)
    await session.flush()
    return block


async def _unit(session, project, block, unit_no: str = "1", **kwargs) -> Unit:
    defaults: dict = {"unit_kind": UnitKind.apartment}
    defaults.update(kwargs)
    unit = Unit(project_id=project.id, block_id=block.id, unit_no=unit_no, **defaults)
    session.add(unit)
    await session.flush()
    return unit


def _bos_sayac() -> dict[str, int]:
    """P3.1 §4.3: `UnitKindBreakdown` bes sayacli (UE 74). Karar 13: EKRAN
    etiketleri degismez, yalniz sayaclar eklenir."""
    return {"apartment": 0, "shop": 0, "office": 0, "warehouse": 0, "parking": 0, "total": 0}


async def _count_units_in_block(session, block_id: uuid.UUID) -> int:
    """B7 test 6'nin KANITIDIR: 409 sonrasi unite sayisinin DEGISMEDIGINI olcer.

    Durum kodunu dogrulamak yetmez — cascade yanlislikla acilsaydi 409 yine
    donebilir ama uniteler gitmis olurdu. Sayim tek gercek kanittir.
    """
    result = await session.execute(
        select(func.count()).select_from(Unit).where(Unit.block_id == block_id)
    )
    return int(result.scalar_one())


async def _block_exists(session, block_id: uuid.UUID) -> bool:
    result = await session.execute(
        select(func.count()).select_from(Block).where(Block.id == block_id)
    )
    return int(result.scalar_one()) == 1
