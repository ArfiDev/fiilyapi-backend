"""SA-KILIT — "Siparis Ver" ucunun ESZAMANLILIK bekcisi (CANLIDA DURAN PARA KUSURU).

Iki es zamanli `select-and-order` AYNI talebe **IKI SIPARIS** yaziyordu ve
tedarikciye para IKI KEZ taahhut ediliyordu. Sebep kusurun kendisinden daha
ogreticidir ve bu dosyanin VAR OLMA sebebi odur:

## BEKLEMEK YENIDEN-DOGRULAMA DEGILDIR

Onarim oncesi ikinci istek de **BEKLIYORDU** — yani "eszamanlilik yok" diye
gecistirilemezdi. Bekliyordu cunku `apply_request_transition` icindeki
`UPDATE purchase_requests` birincinin SATIR KILIDINE carpiyordu. Ama gecis
matrisi o UPDATE'ten **ONCE**, BELLEKTEKI (bayat) `quote_wait` degeri uzerinde
kosmustu. Bloke cozulunce karar YENIDEN sorulmuyordu: ikinci istek bayat
karariyla devam edip ikinci siparisi yaziyordu.

Onarim: uc `service.visible_request_locked` ile acilir — kapsam suzgeci +
`SELECT … FOR UPDATE` + `populate_existing=True`. Ikinci istek artik TABLO
KONTROLUNDEN ONCE kilide gider ve bloke cozulunce satiri TAZE okur
(`ordered`), matris (`ordered`, `select-and-order`) ciftini tanimadigi icin
**409** doner.

## BEKCININ NE OKUDUGU AYRICA KANITLANIR (kor bekci riski)

Bu istegin yolunda ALAKASIZ bir kilit daha vardir: `numbering.generate_order_
number` bir `pg_advisory_xact_lock(82502, yil)` alir. O kilit yarisi YUTABILIR
ve testi sahte-yesil yapabilirdi. Yutmadigi OLCULEREK gosterilir:
`test_ikinci_istek_SATIR_kilidinde_bekler_DANISMA_kilidinde_degil` bloke aninda
`pg_locks`/`pg_stat_activity`e bakip beklemenin `transactionid` (satir kilidi)
oldugunu, `advisory` OLMADIGINI dogrular. Ayrica numara dizisi de olculur:
ikinci istek numara uretimine HIC VARMAZ (`SP-2026-0002` dogmaz).

## Neden paylasilan `db_session` YETMEZ

Tek baglanti + savepoint gercek eszamanlilik uretmez (FAT-1 dersi: izole/soguk
havuzda yaris hic olusmayabilir). Bu yuzden tek kullanimlik bir veritabani
acilir ve IKI GERCEK oturum kosar; `.env`/`TEST_DATABASE_URL` veritabani
ELLENMEZ. Desen `test_procurement_numbering.py`den alinmistir.
"""

import asyncio
import uuid
from datetime import date
from decimal import Decimal

import asyncpg
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from starlette.requests import Request as StarletteRequest

from app.core.config import settings
from app.core.db import Base
from app.core.errors import ConflictError
from app.modules.procurement import guards, service, stock_link
from app.modules.procurement import router as procurement_router
from app.modules.procurement.models import (
    PurchaseOrderStatus,
    PurchaseRequest,
    PurchaseRequestStatus,
)
from app.modules.procurement.schemas import PurchaseOrderUpdate
from app.modules.users.models import User

# Talebin miktari ve teklifin birim fiyati — siparis tutari bunlarin CARPIMIDIR
# (`transitions.order_total_from_quote`). Somut sayilar, cift siparisin parasal
# buyuklugunu raporda okunur kilmak icindir.
_MIKTAR = Decimal("100.000")
_BIRIM_FIYAT = Decimal("2500.00")
_BEKLENEN_TUTAR = _MIKTAR * _BIRIM_FIYAT  # ₺250.000

#: Ikinci istegin KILIT BEKLEMESINE girmesi icin taninan TAVAN. Sabit bir
#: `sleep` DEGILDIR: `_kilit_beklemesini_bekle` beklemeyi GORUNCE hemen doner,
#: bu sure yalnizca "hic olusmadi" karari icin ust sinirdir.
_KILIT_BEKLEME_TAVANI_SN = 20.0


def _asyncpg_dsn(database: str) -> str:
    base = settings.test_database_url.replace("postgresql+asyncpg://", "postgresql://")
    return base.rsplit("/", 1)[0] + f"/{database}"


def _sqlalchemy_dsn(database: str) -> str:
    return settings.test_database_url.rsplit("/", 1)[0] + f"/{database}"


async def _create_scratch_database() -> str:
    database = f"sakilit_{uuid.uuid4().hex[:8]}"
    admin = await asyncpg.connect(_asyncpg_dsn("postgres"))
    try:
        await admin.execute(f'CREATE DATABASE "{database}"')
    finally:
        await admin.close()
    return database


async def _drop_scratch_database(database: str) -> None:
    admin = await asyncpg.connect(_asyncpg_dsn("postgres"))
    try:
        await admin.execute(f'DROP DATABASE IF EXISTS "{database}" WITH (FORCE)')
    finally:
        await admin.close()


class _Zemin:
    """Yarisin ihtiyac duydugu en kucuk FK zemininin kimlikleri."""

    __slots__ = ("project_id", "quote_id", "request_id", "supplier_id", "user_id")

    def __init__(self) -> None:
        self.user_id = uuid.uuid4()
        self.project_id = uuid.uuid4()
        self.supplier_id = uuid.uuid4()
        self.request_id = uuid.uuid4()
        self.quote_id = uuid.uuid4()


async def _zemin_kur(database: str) -> _Zemin:
    """Ham SQL ile zemin: rol · kullanici · TUM PROJE erisimi · proje ·
    tedarikci · `quote_wait` talebi · kalem · teklif.

    Kapsam `users.all_projects` ile verilir (`projects` izin
    satiri gerekmez): `service.visible_request` oradan gecer.
    """
    z = _Zemin()
    role_id = uuid.uuid4()
    raw = await asyncpg.connect(_asyncpg_dsn(database))
    try:
        await raw.execute(
            "INSERT INTO roles (id, key, name, emoji, description, is_system) "
            "VALUES ($1, 'sa_kilit', 'SA Kilit', '', '', false)",
            role_id,
        )
        await raw.execute(
            "INSERT INTO users (id, email, password_hash, full_name, title, role_id, "
            "status, token_version, all_projects) "
            "VALUES ($1, 'sakilit@ornek.test', 'x', 'SA Kilit', '', $2, 'active', 0, true)",
            z.user_id,
            role_id,
        )
        await raw.execute(
            "INSERT INTO projects (id, code, name, status, budget, progress_pct) "
            "VALUES ($1, 'P-KILIT', 'Kilit', 'active', 0, 0)",
            z.project_id,
        )
        await raw.execute(
            "INSERT INTO suppliers (id, name, payment_terms, is_active) "
            "VALUES ($1, 'Demirsan A.S.', 'days_30', true)",
            z.supplier_id,
        )
        await raw.execute(
            "INSERT INTO purchase_requests (id, request_no, request_date, priority, "
            "project_id, status, created_by_user_id) "
            "VALUES ($1, 'SAT-2026-0001', $2, 'normal', $3, 'quote_wait', $4)",
            z.request_id,
            date(2026, 8, 23),
            z.project_id,
            z.user_id,
        )
        await raw.execute(
            "INSERT INTO purchase_request_lines (id, request_id, free_text_name, "
            "free_text_unit, quantity, estimated_unit_price, sort_order) "
            "VALUES ($1, $2, 'Insaat demiri', 'ton', $3, $4, 0)",
            uuid.uuid4(),
            z.request_id,
            _MIKTAR,
            _BIRIM_FIYAT,
        )
        await raw.execute(
            "INSERT INTO purchase_quotes (id, request_id, supplier_id, unit_price, "
            "delivery_time, payment_terms, shipping_included, is_selected) "
            "VALUES ($1, $2, $3, $4, '3 is gunu', 'days_30', true, false)",
            z.quote_id,
            z.request_id,
            z.supplier_id,
            _BIRIM_FIYAT,
        )
    finally:
        await raw.close()
    return z


async def _anlik_bekleme(database: str, pid: int) -> tuple[str | None, set[str]]:
    """UCUNCU bir baglantidan tek anlik goruntu: (`wait_event_type`, bekleyen
    `locktype` kumesi)."""
    raw = await asyncpg.connect(_asyncpg_dsn(database))
    try:
        tip = await raw.fetchval("SELECT wait_event_type FROM pg_stat_activity WHERE pid = $1", pid)
        satirlar = await raw.fetch(
            "SELECT locktype FROM pg_locks WHERE pid = $1 AND NOT granted", pid
        )
    finally:
        await raw.close()
    return tip, {satir["locktype"] for satir in satirlar}


async def _kilit_beklemesini_bekle(database: str, pid: int) -> set[str]:
    """🔴 SABIT SUREYLE ORNEKLEME YAPMA — KILIT BEKLEMESI GORUNENE KADAR BEKLE.

    Olculdu (2026-08-23): ikinci gorev `asyncio.sleep(0.6)` sonunda HENUZ
    `ClientRead`tedir — yani "bitmemis" olmasi kilitte bekledigi ANLAMINA
    GELMEZ; oraya ~1.5s'de variyor. Sabit anda ornekleyen bir bekci hem FLAKY
    olurdu hem de yanlis seyi olcerdi.

    Bu yuzden `wait_event_type = 'Lock'` gorunene kadar YOKLANIR. Donen kume
    beklenen kilitlerin `locktype`lari; cagiran onun `transactionid` mi
    `advisory` mi oldugunu KARARA baglar.
    """
    dongu = asyncio.get_running_loop()
    bitis = dongu.time() + _KILIT_BEKLEME_TAVANI_SN
    son_tip: str | None = None
    while dongu.time() < bitis:
        son_tip, turler = await _anlik_bekleme(database, pid)
        if son_tip == "Lock" and turler:
            return turler
        await asyncio.sleep(0.05)
    raise AssertionError(
        f"ikinci istek {_KILIT_BEKLEME_TAVANI_SN}s icinde HIC KILIT BEKLEMESINE girmedi "
        f"(son wait_event_type={son_tip!r}) — yaris OLUSMADI, bu test bir sey bekcilemiyor"
    )


async def _kullanici(session: AsyncSession, user_id: uuid.UUID) -> User:
    kullanici = await session.get(User, user_id)
    assert kullanici is not None
    return kullanici


def _sahte_istek() -> StarletteRequest:
    """Denetim satirinin ihtiyac duydugu en kucuk `Request` (yalniz `client_ip`)."""
    return StarletteRequest(
        {
            "type": "http",
            "method": "POST",
            "path": "/purchase-requests/x/quotes/y/select-and-order",
            "headers": [],
            "query_string": b"",
            "client": ("127.0.0.1", 9999),
        }
    )


async def _siparis_ver(session: AsyncSession, z: _Zemin) -> None:
    """🔴 UCUN KENDISI cagrilir — testin ikinci bir kopyasi YOKTUR.

    Kapsam kapisinin (`visible_request` mi `visible_request_locked` mi)
    SECIMI tam olarak olculen seydir; burada tekrar yazilsaydi uc geri
    alinsa bile test YESIL kalirdi (sahte-yesil).
    """
    kullanici = await _kullanici(session, z.user_id)
    await procurement_router.select_and_order_endpoint(
        request=_sahte_istek(),
        request_id=z.request_id,
        quote_id=z.quote_id,
        user=kullanici,
        session=session,
    )
    await session.commit()


async def _sayimlar(database: str, z: _Zemin) -> dict[str, object]:
    raw = await asyncpg.connect(_asyncpg_dsn(database))
    try:
        return {
            "siparis": await raw.fetchval(
                "SELECT count(*) FROM purchase_orders WHERE request_id = $1", z.request_id
            ),
            "numaralar": [
                satir["order_no"]
                for satir in await raw.fetch(
                    "SELECT order_no FROM purchase_orders ORDER BY order_no"
                )
            ],
            "durum": await raw.fetchval(
                "SELECT status::text FROM purchase_requests WHERE id = $1", z.request_id
            ),
            "secili_teklif": await raw.fetchval(
                "SELECT count(*) FROM purchase_quotes WHERE request_id = $1 AND is_selected",
                z.request_id,
            ),
            "toplam_tutar": await raw.fetchval(
                "SELECT coalesce(sum(total_amount), 0) FROM purchase_orders WHERE request_id = $1",
                z.request_id,
            ),
        }
    finally:
        await raw.close()


@pytest.fixture
async def yaris_zemini():
    """Tek kullanimlik veritabani + zemin + oturum fabrikasi."""
    database = await _create_scratch_database()
    engine = create_async_engine(_sqlalchemy_dsn(database))
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        z = await _zemin_kur(database)
        yield database, async_sessionmaker(engine, expire_on_commit=False), z
    finally:
        await engine.dispose()
        await _drop_scratch_database(database)


async def test_es_zamanli_siparis_ver_TEK_siparis_yazar(yaris_zemini):
    """🔴 CANLIDA DURAN PARA KUSURUNUN BEKCISI.

    Iki oturum ayni talebe ayni anda "Siparis Ver" der. Onarim oncesi IKINCISI
    de BASARILI oluyor ve talep ₺250.000'i IKI KEZ (toplam ₺500.000) taahhut
    ediyordu. Onarim sonrasi ikinci istek **409** alir.
    """
    database, Session, z = yaris_zemini

    async with Session() as birinci, Session() as ikinci:
        ikinci_pid = await ikinci.scalar(select(func.pg_backend_pid()))

        # Birinci istek yolunu SONUNA kadar kosar ama COMMIT ETMEZ: satir
        # kilidi elinde kalir ve ikinciyi gercekten bloke eder.
        kullanici_bir = await _kullanici(birinci, z.user_id)
        await procurement_router.select_and_order_endpoint(
            request=_sahte_istek(),
            request_id=z.request_id,
            quote_id=z.quote_id,
            user=kullanici_bir,
            session=birinci,
        )

        gorev = asyncio.create_task(_siparis_ver(ikinci, z))

        # 🔴 POZITIF KONTROL: ikinci istek GERCEKTEN birincinin kilidinde
        # bekliyor. Bu satir olmadan sirayla kosan bir "eszamanlilik testi"
        # hicbir seyi bekcilemez.
        await _kilit_beklemesini_bekle(database, ikinci_pid)
        assert not gorev.done(), "ikinci istek kilitte bekliyor ama gorev bitmis — celiski"

        await birinci.commit()

        with pytest.raises(ConflictError) as hata:
            await asyncio.wait_for(gorev, timeout=15)

    assert str(hata.value.args[0]) == guards.INVALID_REQUEST_TRANSITION

    olculen = await _sayimlar(database, z)
    assert olculen["siparis"] == 1, f"talebe {olculen['siparis']} siparis yazildi (1 beklenir)"
    assert olculen["toplam_tutar"] == _BEKLENEN_TUTAR
    assert olculen["durum"] == "ordered"
    assert olculen["secili_teklif"] == 1
    # Ikinci istek numara uretimine HIC VARMADI — danisma kilidi bile alinmadi.
    assert olculen["numaralar"] == ["SP-2026-0001"]


async def test_ikinci_istek_SATIR_kilidinde_bekler_DANISMA_kilidinde_degil(yaris_zemini):
    """🔴 BEKCININ NE OKUDUGU — kor bekci riski (emrin T1 uyarisi).

    Bu istegin yolunda ALAKASIZ bir kilit daha vardir: `numbering.generate_
    order_number`in `pg_advisory_xact_lock(82502, yil)` cagrisi. O kilit yarisi
    YUTSAYDI test sahte-yesil olurdu: "ikinci istek bekledi" gorunur ama
    bekledigi sey talebin satiri DEGIL numara sayaci olurdu ve `visible_request_
    locked`i geri almak testi KIRMIZI YAPMAZDI.

    Bloke aninda ucuncu bir baglantidan olculur: bekleyen kilit `transactionid`
    (satir kilidi) OLMALI, `advisory` OLMAMALIDIR.
    """
    _database, Session, z = yaris_zemini

    async with Session() as birinci, Session() as ikinci:
        ikinci_pid = await ikinci.scalar(select(func.pg_backend_pid()))
        assert ikinci_pid is not None

        kullanici_bir = await _kullanici(birinci, z.user_id)
        await procurement_router.select_and_order_endpoint(
            request=_sahte_istek(),
            request_id=z.request_id,
            quote_id=z.quote_id,
            user=kullanici_bir,
            session=birinci,
        )

        gorev = asyncio.create_task(_siparis_ver(ikinci, z))
        turler = await _kilit_beklemesini_bekle(_database, ikinci_pid)

        await birinci.commit()
        with pytest.raises(ConflictError):
            await asyncio.wait_for(gorev, timeout=15)

    assert "advisory" not in turler, (
        f"ikinci istek DANISMA kilidinde bekliyor ({turler}) — yarisi numara sayaci yutmus, "
        "bu test talebin satir kilidini BEKCILEMIYOR"
    )
    assert "transactionid" in turler, (
        f"beklenen satir kilidi (transactionid) bulunamadi; bekleyen turler: {turler}"
    )


async def test_ikinci_istek_TAZE_durum_okur_bayat_degil(yaris_zemini):
    """Kilit TEK BASINA yetmez: `populate_existing=True` de sarttir.

    Ikinci oturum talebi kilitten ONCE okumus olabilir (ORM kimlik haritasi).
    Kilit alindiktan sonra satir TAZE okunmazsa karar yine BAYAT `quote_wait`
    uzerinden verilir ve kilit hicbir sey bekcilemez — `repository.get_request_
    locked`in docstring'inin yazdigi tuzak budur.

    Bu test ikinci oturumu talebi ONCEDEN yuklemeye zorlar.
    """
    _database, Session, z = yaris_zemini

    async with Session() as birinci, Session() as ikinci:
        ikinci_pid = await ikinci.scalar(select(func.pg_backend_pid()))

        # Ikinci oturum talebi BAYAT haliyle kimlik haritasina alir.
        bayat = await ikinci.get(PurchaseRequest, z.request_id)
        assert bayat is not None
        assert bayat.status is PurchaseRequestStatus.quote_wait

        kullanici_bir = await _kullanici(birinci, z.user_id)
        await procurement_router.select_and_order_endpoint(
            request=_sahte_istek(),
            request_id=z.request_id,
            quote_id=z.quote_id,
            user=kullanici_bir,
            session=birinci,
        )

        gorev = asyncio.create_task(_siparis_ver(ikinci, z))
        await _kilit_beklemesini_bekle(_database, ikinci_pid)

        await birinci.commit()
        with pytest.raises(ConflictError):
            await asyncio.wait_for(gorev, timeout=15)

    olculen = await _sayimlar(_database, z)
    assert olculen["siparis"] == 1


async def test_DB_katmani_da_cift_siparisi_reddeder(yaris_zemini):
    """T4 — uygulama kilidi ATLANSA BILE veritabani ikinci siparisi kabul etmez.

    Uygulama katmani tek savunma olsaydi, `select_and_order`i cagirmayan
    (bugun olmayan) ikinci bir yazma yolu ayni kusuru geri getirirdi. Kisit
    `uq_purchase_orders_request_id`dir.

    `request_id` NULL olan DOGRUDAN siparisler (SIP 35) kisittan ETKILENMEZ:
    Postgres UNIQUE'i coklu NULL'a IZIN VERIR ve o davranis burada ACIKCA
    olculur — kisit mesru bir akisi kirmiyor.
    """
    database, Session, z = yaris_zemini

    raw = await asyncpg.connect(_asyncpg_dsn(database))
    try:
        # Ayni talebe IKINCI siparis — DB reddetmeli.
        await raw.execute(
            "INSERT INTO purchase_orders (id, order_no, request_id, supplier_id, project_id, "
            "total_amount, status, created_by_user_id) "
            "VALUES ($1, 'SP-2026-9001', $2, $3, $4, 1, 'approved', $5)",
            uuid.uuid4(),
            z.request_id,
            z.supplier_id,
            z.project_id,
            z.user_id,
        )
        with pytest.raises(asyncpg.UniqueViolationError):
            await raw.execute(
                "INSERT INTO purchase_orders (id, order_no, request_id, supplier_id, "
                "project_id, total_amount, status, created_by_user_id) "
                "VALUES ($1, 'SP-2026-9002', $2, $3, $4, 1, 'approved', $5)",
                uuid.uuid4(),
                z.request_id,
                z.supplier_id,
                z.project_id,
                z.user_id,
            )

        # DOGRUDAN siparis (talepsiz) — IKISI de gecmeli.
        for numara in ("SP-2026-9003", "SP-2026-9004"):
            await raw.execute(
                "INSERT INTO purchase_orders (id, order_no, request_id, supplier_id, "
                "project_id, total_amount, status, created_by_user_id) "
                "VALUES ($1, $2, NULL, $3, $4, 1, 'approved', $5)",
                uuid.uuid4(),
                numara,
                z.supplier_id,
                z.project_id,
                z.user_id,
            )
        talepsiz = await raw.fetchval(
            "SELECT count(*) FROM purchase_orders WHERE request_id IS NULL"
        )
    finally:
        await raw.close()

    assert talepsiz == 2, "UNIQUE kisiti talepsiz (NULL) siparisleri KIRDI — SIP 35 akisi bozulur"


def test_siparis_ucu_KILITLI_kapiyi_kullanir():
    """Yapisal bekci: `select-and-order` ucu `visible_request_locked` ile acilir.

    Eszamanlilik testi pahalidir (tek kullanimlik veritabani); bu ucuz bekci
    onarimin sessizce geri alinmasini yakalar. `visible_request` (KILITSIZ)
    geri gelirse burasi kirmizi olur.
    """
    from pathlib import Path

    kaynak = Path(service.__file__).resolve().parent.parent / "router.py"
    metin = kaynak.read_text(encoding="utf-8")
    govde = metin.split("async def select_and_order_endpoint")[1].split("\n@router")[0]

    assert "service.visible_request_locked(" in govde
    assert "service.visible_request(" not in govde


# --------------------------------------------------------------------------- #
# T3 — SIPARIS tarafi: `update_order` AYNI SINIFTAN MI? (olculdu: EVET)
# --------------------------------------------------------------------------- #
#
# TB-PROC "etkisi dusuk gorunuyor ama OLCMEDIM" demisti. Olculdu ve etki dusuk
# DEGIL: stok girisinin `delivered` damgasi ile es zamanli bir PATCH, teslim
# damgasini KAYBEDIYOR ve siparis/talep ikilisini CELISKILI birakiyordu.
#
# Olculen uc siralama (338697d tabani):
#   A) s1=STOK TESLIMI      || s2=PATCH{in_transit} -> SIPARIS=in_transit
#                                                      TALEP=delivered   🔴 CELISKI
#   B) s1=PATCH{in_transit} || s2=STOK TESLIMI      -> SIPARIS=delivered
#                                                      TALEP=delivered   ✅ dogru
#   C) s1=PATCH{in_transit} || s2=PATCH{in_transit} -> IKISI de BASARILI 🔴 409 yok
#
# B dogru sonuclandigi icin `stock_link.resolve_order` KILITLENMEDI (olcum yok,
# degisiklik yok — KAPSAM DISI). Kapatilan A ve C'dir: kapi
# `service.visible_order_locked`a alindi.


async def _siparis_kur(database: str, z: _Zemin) -> uuid.UUID:
    """Talebe bagli, `approved` bir siparis + talebi `ordered` yap."""
    order_id = uuid.uuid4()
    raw = await asyncpg.connect(_asyncpg_dsn(database))
    try:
        await raw.execute(
            "INSERT INTO purchase_orders (id, order_no, request_id, supplier_id, project_id, "
            "total_amount, status, created_by_user_id) "
            "VALUES ($1, 'SP-2026-0500', $2, $3, $4, 1000, 'approved', $5)",
            order_id,
            z.request_id,
            z.supplier_id,
            z.project_id,
            z.user_id,
        )
        await raw.execute(
            "UPDATE purchase_requests SET status = 'ordered' WHERE id = $1", z.request_id
        )
    finally:
        await raw.close()
    return order_id


async def _patch_in_transit(session: AsyncSession, z: _Zemin, order_id: uuid.UUID) -> None:
    """PATCH ucunun KENDISI — testte yeniden yazilmaz (sahte-yesil olmasin)."""
    kullanici = await _kullanici(session, z.user_id)
    await procurement_router.update_order_endpoint(
        request=_sahte_istek(),
        order_id=order_id,
        data=PurchaseOrderUpdate(status=PurchaseOrderStatus.in_transit),
        user=kullanici,
        session=session,
    )


async def _durumlar(database: str, z: _Zemin, order_id: uuid.UUID) -> tuple[str, str]:
    raw = await asyncpg.connect(_asyncpg_dsn(database))
    try:
        return (
            await raw.fetchval("SELECT status::text FROM purchase_orders WHERE id = $1", order_id),
            await raw.fetchval(
                "SELECT status::text FROM purchase_requests WHERE id = $1", z.request_id
            ),
        )
    finally:
        await raw.close()


async def test_stok_teslimiyle_es_zamanli_PATCH_teslim_damgasini_SILEMEZ(yaris_zemini):
    """🔴 T3-A: teslim damgasinin kaybolmasi (OLCULDU, dusuk etki DEGIL).

    Stok girisi siparisi (ve talebi) `delivered` damgalarken es zamanli bir
    `PATCH {"status": "in_transit"}` geliyordu. PATCH kapisi kilitsizken bayat
    `approved`i okuyup matristen geciyor, bloke cozulunce `in_transit` yaziyordu:
    **mal girmis ama siparis "yolda"**, bagli talep ise `delivered`. Ikisi
    CELISKILI ve teslim damgasi KAYIP.

    Kapi kilitli oldugunda ikinci istek TAZE `delivered` okur ve
    `(delivered, in_transit)` matriste olmadigi icin **409** alir.
    """
    database, Session, z = yaris_zemini
    order_id = await _siparis_kur(database, z)

    async with Session() as birinci, Session() as ikinci:
        ikinci_pid = await ikinci.scalar(select(func.pg_backend_pid()))

        kullanici_bir = await _kullanici(birinci, z.user_id)
        siparis = await stock_link.resolve_order(birinci, kullanici_bir, order_id)
        await stock_link.stamp_delivery(birinci, siparis)

        async def _ikinci_akis() -> None:
            await _patch_in_transit(ikinci, z, order_id)
            await ikinci.commit()

        gorev = asyncio.create_task(_ikinci_akis())
        await _kilit_beklemesini_bekle(database, ikinci_pid)

        await birinci.commit()
        with pytest.raises(ConflictError) as hata:
            await asyncio.wait_for(gorev, timeout=15)

    assert str(hata.value.args[0]) == guards.INVALID_ORDER_TRANSITION

    siparis_durumu, talep_durumu = await _durumlar(database, z, order_id)
    assert siparis_durumu == "delivered", "teslim damgasi SILINDI"
    assert talep_durumu == "delivered", "siparis teslim ama talep degil — CELISKI"


async def test_iki_es_zamanli_PATCH_ikincisi_409_alir(yaris_zemini):
    """🔴 T3-C: ayni gecisi iki kez uygulayan yaris.

    Matris `(in_transit, in_transit)` ciftini TANIMAZ; yani ikinci istek 409
    ALMALIDIR. Kilitsiz kapida IKISI de basariliydi — `assert_order_transition`
    bayat `approved` uzerinde kosuyordu.
    """
    database, Session, z = yaris_zemini
    order_id = await _siparis_kur(database, z)

    async with Session() as birinci, Session() as ikinci:
        ikinci_pid = await ikinci.scalar(select(func.pg_backend_pid()))
        await _patch_in_transit(birinci, z, order_id)

        async def _ikinci_akis() -> None:
            await _patch_in_transit(ikinci, z, order_id)
            await ikinci.commit()

        gorev = asyncio.create_task(_ikinci_akis())
        await _kilit_beklemesini_bekle(database, ikinci_pid)

        await birinci.commit()
        with pytest.raises(ConflictError):
            await asyncio.wait_for(gorev, timeout=15)

    siparis_durumu, _ = await _durumlar(database, z, order_id)
    assert siparis_durumu == "in_transit"


def test_siparis_PATCH_ucu_KILITLI_kapiyi_kullanir():
    """Ucuz yapisal bekci — T3 onariminin sessizce geri alinmasini yakalar."""
    from pathlib import Path

    kaynak = Path(service.__file__).resolve().parent.parent / "router.py"
    metin = kaynak.read_text(encoding="utf-8")
    govde = metin.split("async def update_order_endpoint")[1].split("\n@router")[0]

    assert "service.visible_order_locked(" in govde
    assert "service.visible_order(" not in govde


# --------------------------------------------------------------------------- #
# T3 SUPURGESI — MODUL GENELI INVARYANT
# --------------------------------------------------------------------------- #


def test_TUM_yazma_uclari_KILITLI_kapiyi_kullanir():
    """🔴 MODULUN TEK YAPISAL KURALI, tek yerde bekcilenir.

    T3 taramasi sunu gosterdi: kusur `select-and-order`a OZGU DEGILDI, tek bir
    yapisal kuralin ON DORT ucta tutarsiz uygulanmasiydi. Kural:

        DURUM'a bakarak karar veren her YAZMA ucu, o satiri KILITLI okumak
        zorundadir; OKUMA uclari kilit ALMAZ.

    Kilitsiz kapiyla olculen ihlaller (338697d tabani, hepsi GERCEK yaris —
    `wait_event_type = Lock/transactionid` dogrulandi):

    * `select-and-order` || `select-and-order` -> IKI SIPARIS, ₺500.000 (asil kusur)
    * stok teslimi       || `PATCH` siparis    -> teslim damgasi KAYIP, ikili CELISKILI
    * `PATCH` siparis    || `PATCH` siparis    -> ikisi de gecti, 409 YOK
    * `submit`           || `PATCH` talep      -> `_assert_draft` ATLATILDI
    * `select-and-order` || `POST` teklif      -> `_assert_quote_wait` ATLATILDI
    * `select-and-order` || `DELETE` teklif    -> secili teklif SILINDI

    Uc uc bazinda bekcilemek yerine invaryant BURADA durur: yeni bir yazma ucu
    kilitsiz kapiyla eklenirse bu test kirmizi olur ve ekleyen kisi karari
    BILINCLI vermek zorunda kalir.
    """
    from pathlib import Path

    metin = (Path(service.__file__).resolve().parent.parent / "router.py").read_text(
        encoding="utf-8"
    )
    yazma = {"post", "patch", "put", "delete"}
    ihlaller: list[str] = []
    okuma_kilitli: list[str] = []

    for parca in metin.split("\n@router.")[1:]:
        yontem = parca.split("(", 1)[0].strip().lower()
        ad = parca.split("async def ", 1)[1].split("(", 1)[0]
        govde = parca.split("):", 1)[-1]
        kapilar = {k for k in ("visible_request", "visible_order") if f"service.{k}(" in govde}
        kilitli = {
            k
            for k in ("visible_request_locked", "visible_order_locked")
            if f"service.{k}(" in govde
        }

        if yontem in yazma and kapilar:
            ihlaller.append(f"{yontem.upper()} {ad} -> {sorted(kapilar)}")
        if yontem == "get" and kilitli:
            okuma_kilitli.append(f"GET {ad} -> {sorted(kilitli)}")

    assert not ihlaller, (
        "KILITSIZ kapi kullanan YAZMA ucu var — es zamanli iki istek BAYAT durum "
        f"uzerinden karar verir: {ihlaller}"
    )
    assert not okuma_kilitli, (
        "OKUMA ucu satir kilidi aliyor — listeler/detaylar yazmalarin arkasinda "
        f"bekler, gereksiz cekisme: {okuma_kilitli}"
    )
