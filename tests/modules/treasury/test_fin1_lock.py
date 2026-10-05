"""FIN-1 T6 — 🔴 EŞİK = KİLİT. Durum gecisi satiri DENETIMDEN ONCE kilitler.

## Nicin `client`/`seeded_db` KULLANILMAZ

Kok `tests/conftest.py`deki `db_session` her testi TEK baglanti uzerinde
SAVEPOINT'e sarar ve dis transaction'i asla gercekten COMMIT ETMEZ — o session
uzerinde iki gorev AYNI baglantiyi paylasir ve gercek satir kilidi test
EDILEMEZ. Bu dosya `test_hz1_payment_lock.py` desenini izler: `test_engine`
uzerinden IKI BAGIMSIZ baglanti, gercek commit, gercek temizlik.

## 🔴 CAKISMA PENCERESI DETERMINISTIKTIR — sabit `sleep` YOKTUR

FAT-1'in kilit testi mutasyonlu hâlde **dosya butun kosulunca** kirmiziydi ama
**TEK BASINA kosulunca YESILDI**: izole kosuda havuz SOGUKTUR, ilk gorev baglanti
kurulumunu beklerken ikincisi henuz baslamamis olur ve iki gorev hic cakismaz.

Bu dosyadaki iki bekci o zaafi devralmaz:

0. 🔴 **Gecis kilidi — TUTULAN KILIT (asil bekci).** T6 mutasyon turunda
   `gather` bekcisi IZOLE kosuda 3 turda 2 kirmizi 1 YESIL verdi; yani tek
   basina FAT-1'in kirilgan bekcisidir. Bu yuzden kilidin VARLIGI ayrica
   deterministik olarak olculur (`test_GECIS_kaydi_DENETIMDEN_ONCE_kilitler_
   TUTULAN_KILIT`) ve `gather` bekcisi yalnizca DAVRANIS katmani olarak kalir.

1. **Gecis yarisi — BARAJ.** Her gorev once kendi baglantisini kurar ve uzerinde
   gercek bir sorgu (`session.get(User, ...)`) kosturur; ANCAK ONDAN SONRA
   `asyncio.Barrier`a varir. Isinma barajdan SONRA yapilsaydi izole kosuda
   baglanti kurulum gecikmesi iki gorevi siraya sokar ve pencere HIC ACILMAZDI.
2. **Kayip guncelleme — TX0 YAZAR VE TUTAR.** tx0 kaydi `collected` yapip
   COMMIT ETMEDEN bekler; karsi gorev (gecis ya da silme) baslatilir.
   * kilit varken: karsi gorev `FOR UPDATE`te bekler, tx0 commit edince TAZE
     `collected` degerini okur ve terminal korumasindan **409** alir;
   * kilit yokken: BAYAT `portfolio` okur, kapidan gecer ve tx0'in yazdigini
     EZER (ya da tahsil edilmis ceki SILER).

   🔴 tx0'in yalnizca kilit TUTMASI (hicbir kolona yazmamasi) YETMEZ ve bu
   OLCULEREK bulundu: o hâlde kilitsiz kod da kendi `UPDATE`inin ortuk satir
   kilidinde bloke olur, "ilerlemedi" iddiasi mutasyonda da YESIL kalir
   (3/3 yesil olculdu). Ayrisma noktasi BLOKE OLMAK degil, **NE OKUDUGU**dur.

## Mutasyonun ne oldugu

`service.change_status` icindeki `for_update=True` kaldirilirsa iki eszamanli
istek AYNI `portfolio` degerini okur, IKISI DE K2 tablosundan gecer ve ikincisi
birincinin yazdigini EZER — `collected` bir cek sessizce `cancelled` olur ve
mali iz kaybolur (Ik-3'te iki eszamanli odeme bordroyu IKI KEZ odemisti).
"""

import asyncio
import contextlib
import uuid
from datetime import date
from decimal import Decimal

from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.errors import ConflictError
from app.core.security import hash_password
from app.modules.roles.models import Role
from app.modules.treasury.instruments import service
from app.modules.treasury.models import (
    FinancialInstrument,
    FinancialInstrumentDirection,
    FinancialInstrumentKind,
    FinancialInstrumentStatus,
)
from app.modules.users.models import ProjectMember, User
from tests.conftest import test_engine

_SessionFactory = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)

#: Rol anahtari ve e-postalar TESTE OZELDIR: bu dosya GERCEKTEN commit ettigi
#: icin sizinti ancak yaratilan satirlarin tam bilinmesiyle kapanir.
_ROL_ANAHTARI = "fin1_conc_admin"
_EPOSTALAR = ("fin1-kilit1@conc.co", "fin1-kilit2@conc.co")

#: Baraj/gorev bekleyislerinin tavani. Kilit DOGRUYKEN gorevler saniyenin
#: altinda biter; tavan yalnizca BOZUK bir kurulumun testi sonsuza asmasini
#: engeller — pencere acmak icin KULLANILMAZ.
_TAVAN_SANIYE = 15

#: "Bloke kalmali" iddiasinin UST SINIRI. Pencere ACMAZ (cakisma zaten tutulan
#: gercek bir kilitle garanti altindadir); yalnizca "bitmemeli"yi sonlu surede
#: karara baglar.
_BLOKE_TAVANI = 2


class _Kurulum:
    def __init__(
        self, instrument_id: uuid.UUID, actor_ids: list[uuid.UUID], role_id: uuid.UUID
    ) -> None:
        self.instrument_id = instrument_id
        self.actor_ids = actor_ids
        self.role_id = role_id


async def _kur() -> _Kurulum:
    """Cek PROJESIZ acilir (`project_id` NULL, `scope_clause`in ucuncu hali).

    Kurulum boylece proje/`user_project_access` satiri YARATMAK ZORUNDA KALMAZ —
    gercekten commit eden bir testte yaratilan her satir bir sizinti riskidir.
    Izin satiri da gerekmez: yetki kapisi ROUTER'dadir, bu dosya SERVISI
    dogrudan cagirir.

    ⚠️ `visible_projects` yine de kullanicinin erisim satirini okur; projesiz
    kayit icin sonucu onemsizdir ama cagri kosar — bu yuzden aktorlere
    `all_projects=True` verilir ve boylece kurulum eksigi kilidin yerine
    gecmez (kilit testi "kapsam yuzunden 404" ile YESIL gorunmemeli).
    """
    async with _SessionFactory() as session:
        role = Role(key=_ROL_ANAHTARI, name="FIN-1 Eşzamanlılık Rolü")
        session.add(role)
        await session.flush()
        aktorler = [
            User(
                email=eposta,
                password_hash=hash_password("parola1234"),
                full_name=f"Kilit Aktörü {sira}",
                role_id=role.id,
            )
            for sira, eposta in enumerate(_EPOSTALAR, start=1)
        ]
        session.add_all(aktorler)
        await session.flush()
        for aktor in aktorler:
            aktor.all_projects = True

        instrument = FinancialInstrument(
            instrument_kind=FinancialInstrumentKind.cheque,
            direction=FinancialInstrumentDirection.received,
            serial_no="FIN1CONC01",
            drawer_name="Eşzamanlılık A.Ş.",
            issue_date=date(2026, 7, 1),
            due_date=date(2026, 7, 25),
            amount=Decimal("1200000.00"),
            status=FinancialInstrumentStatus.portfolio,
        )
        session.add(instrument)
        await session.flush()
        await session.commit()
        return _Kurulum(instrument.id, [a.id for a in aktorler], role.id)


async def _gorevleri_bosalt(*gorevler: asyncio.Task | None) -> None:
    """Temizlikten ONCE gorevleri sonlandirir — MUTASYON DENETIMI ICIN SART.

    Kilit kaldirildiginda iddia kirmiziya doner ve govde ORTADA terk edilir; bir
    gorev hâlâ commit etmemis bir transaction icinde kilit tutuyor olabilir. Bu
    bosaltma olmadan `_temizle`nin DELETE'i o kilidi sonsuza dek bekler ve
    kirmizi test SONSUZ ASKIYA donusurdu (Ik-2 dersi).
    """
    for gorev in gorevler:
        if gorev is None:
            continue
        gorev.cancel()
        with contextlib.suppress(BaseException):
            await gorev


async def _temizle(kurulum: _Kurulum) -> None:
    async with _SessionFactory() as session:
        await session.execute(
            delete(FinancialInstrument).where(FinancialInstrument.id == kurulum.instrument_id)
        )
        await session.execute(
            delete(ProjectMember).where(ProjectMember.user_id.in_(kurulum.actor_ids))
        )
        await session.execute(delete(User).where(User.id.in_(kurulum.actor_ids)))
        await session.execute(delete(Role).where(Role.id == kurulum.role_id))
        await session.commit()


async def _guvenli_temizlik(kurulum: _Kurulum, *gorevler: asyncio.Task | None) -> None:
    """Gorevleri bosalt, sonra TAVANLI temizle.

    Temizligin kendi hatasi `finally` icinde ASIL iddianin hata metnini
    EZMEMELIDIR: mutasyon kosusunda okunmasi gereken sey kilit iddiasidir.
    """
    await _gorevleri_bosalt(*gorevler)
    with contextlib.suppress(Exception):
        await asyncio.wait_for(_temizle(kurulum), timeout=_TAVAN_SANIYE)


async def _isin_ve_bekle(
    session: AsyncSession, actor_id: uuid.UUID, baraj: asyncio.Barrier
) -> User:
    """🔴 ISINMA + BARAJ — determinizmin tamami buradadir.

    `session.get` baglantiyi havuzdan CEKER, transaction'i baslatir ve gercek
    bir sorgu kosturur. Baraja ancak ondan sonra varilir.
    """
    actor = await session.get(User, actor_id)
    await asyncio.wait_for(baraj.wait(), timeout=_TAVAN_SANIYE)
    return actor


async def _gecis(
    kurulum: _Kurulum,
    aktor_sirasi: int,
    hedef: FinancialInstrumentStatus,
    baraj: asyncio.Barrier,
) -> str:
    """Bagimsiz bir baglantida TAM yol: kilit → kapsam → K2 tablosu → yazma."""
    async with _SessionFactory() as session:
        actor = await _isin_ve_bekle(session, kurulum.actor_ids[aktor_sirasi], baraj)
        try:
            await service.change_status(session, actor, kurulum.instrument_id, hedef)
        except ConflictError:
            await session.rollback()
            return "rejected"
        await session.commit()
        return "changed"


async def _once_gecir_ve_tut(kurulum: _Kurulum, hazir: asyncio.Event, birak: asyncio.Event) -> str:
    """tx0: kaydi `collected` yapar ama COMMIT ETMEDEN bekler.

    🔴 **Nicin sadece kilit tutmak YETMEZ (T6 mutasyon turu bulgusu):** tx0
    hicbir kolona yazmadan yalnizca `FOR UPDATE` tutsaydi, kilidi KALDIRILMIS
    bir `change_status` yine de bloke olurdu — cunku kendi `UPDATE`i ortuk satir
    kilidini almak zorundadir. Yani "gorev ilerlemedi" iddiasi mutasyonlu kodda
    da YESIL kalirdi (olculdu: 3/3 yesil) ve bekci hicbir sey kanitlamazdi.

    Bu yuzden tx0 DURUMU DEGISTIRIR. Boylece iki dunya AYRISIR:

    * **kilit varken:** karsi gorev `FOR UPDATE`te bekler, tx0 commit edince
      TAZE degeri (`collected`) okur ve K2 terminal korumasindan **409** alir;
      son durum `collected` KALIR.
    * **kilit yokken:** karsi gorev BAYAT `portfolio` degerini okur (READ
      COMMITTED, tx0 henuz commit etmemis), K2 tablosundan GECER ve `UPDATE`i
      tx0'in commit'inden sonra uygulanir — tx0'in yazdigi durum SESSIZCE
      EZILIR. Klasik KAYIP GUNCELLEME.
    """
    async with _SessionFactory() as session:
        actor = await session.get(User, kurulum.actor_ids[0])
        await service.change_status(
            session, actor, kurulum.instrument_id, FinancialInstrumentStatus.collected
        )
        hazir.set()
        await birak.wait()
        await session.commit()
        return "committed"


async def _tek_gecis(kurulum: _Kurulum, aktor_sirasi: int, hedef: FinancialInstrumentStatus) -> str:
    """Barajsiz, yalniz gecis — KAYIP GUNCELLEME bekcisinin karsi gorevidir."""
    async with _SessionFactory() as session:
        actor = await session.get(User, kurulum.actor_ids[aktor_sirasi])
        try:
            await service.change_status(session, actor, kurulum.instrument_id, hedef)
        except ConflictError:
            await session.rollback()
            return "rejected"
        await session.commit()
        return "changed"


async def _silme(kurulum: _Kurulum, aktor_sirasi: int) -> str:
    async with _SessionFactory() as session:
        actor = await session.get(User, kurulum.actor_ids[aktor_sirasi])
        try:
            await service.delete_instrument(session, actor, kurulum.instrument_id)
        except ConflictError:
            await session.rollback()
            return "rejected"
        await session.commit()
        return "deleted"


async def _durum(instrument_id: uuid.UUID) -> FinancialInstrumentStatus | None:
    """Son sozu DB soyler: durum TAZE bir baglantidan okunur."""
    async with _SessionFactory() as session:
        instrument = await session.get(FinancialInstrument, instrument_id)
        return None if instrument is None else instrument.status


async def test_ESZAMANLI_iki_gecis_BIR_KEZ_gecer() -> None:
    """🔴 ASIL MUTASYON REGRESYONU — EŞİK = KİLİT.

    Iki gercek baglanti AYNI portfoydeki ceke ayni anda FARKLI hedefler yazmaya
    calisir: biri `collected`, oteki `cancelled`. Dogru davranis: BIRI gecer,
    OTEKI terminal korumasindan **409** alir.

    Hedefler bilerek FARKLI secildi: ikisi de `collected` olsaydi kilitsiz kodda
    da sonuc `collected` cikar ve **son durum iddiasi kusuru GIZLERDI** (dengeli
    fisin iki bacaginin birbirini goturmesi sinifi, MT-1 dersi). Farkli hedefle
    kilitsiz kod iki "changed" uretir ve son durum hangi gorevin sonra commit
    ettigine gore DEGISIR.

    `change_status` icindeki `for_update=True` kaldirilirsa asagidaki IKI iddia
    da kirmiziya doner.
    """
    kurulum = await _kur()
    baraj = asyncio.Barrier(2)
    gorevler: list[asyncio.Task] = []
    try:
        gorevler = [
            asyncio.create_task(_gecis(kurulum, 0, FinancialInstrumentStatus.collected, baraj)),
            asyncio.create_task(_gecis(kurulum, 1, FinancialInstrumentStatus.cancelled, baraj)),
        ]
        sonuclar = list(await asyncio.wait_for(asyncio.gather(*gorevler), timeout=_TAVAN_SANIYE))
        assert sonuclar.count("changed") == 1, (
            f"iki eşzamanlı geçiş de geçti ({sonuclar}) — `change_status` kaydı K2 "
            "TABLOSUNDAN ÖNCE kilitlemiyor; terminal koruması eşzamanlı istekte kör"
        )
        assert sonuclar.count("rejected") == 1, sonuclar

        son = await _durum(kurulum.instrument_id)
        assert son in (
            FinancialInstrumentStatus.collected,
            FinancialInstrumentStatus.cancelled,
        ), son
    finally:
        await _guvenli_temizlik(kurulum, *gorevler)


async def test_GECIS_kaydi_DENETIMDEN_ONCE_kilitler_TUTULAN_KILIT() -> None:
    """🔴 KILIDIN ASIL BEKCISI — deterministik, zamanlamadan BAGIMSIZ.

    ## Nicin bu test var (T6 mutasyon turu bulgusu)

    Yukaridaki `asyncio.gather` bekcisi mutasyonlu hâlde IZOLE kosuda **3 turda
    2 kirmizi, 1 YESIL** verdi. Yani tek basina FAT-1'in kirilgan bekcisinin
    ta kendisidir: yakalama gucunu iki gorevin gercekten kesismesine borcludur
    ve kesismezlerse kilitsiz kod da dogru sonucu uretir.

    Bu test yerine KAYIP GUNCELLEMEYI olcer (modul docstring'i md. 2): tx0 kaydi
    `collected` yapip COMMIT ETMEDEN bekler, ikinci gorev `cancelled` dener.

    * kilit YERINDEYSE ikinci gorev `FOR UPDATE`te bekler, tx0 commit edince
      TAZE `collected` degerini okur ve terminal korumasindan **409** alir;
      son durum `collected` KALIR;
    * `for_update=True` KALDIRILIRSA ikinci gorev BAYAT `portfolio` okur, K2
      tablosundan gecer ve tx0'in yazdigini EZER → iddia KIRMIZI, **her turda**.

    🔴 Bu bekcinin ILK hâli tx0'i "yalnizca kilit tutan" bicimde yazmisti ve
    mutasyonda 3/3 YESIL kaldi: kilitsiz kod da KENDI `UPDATE`inin ortuk satir
    kilidinde bloke oluyordu. Ayrisma noktasi BLOKE OLMAK degil, NE OKUDUGUDUR.

    Ikisi birlikte iki katmandir: bu test kilidin ETKISINI (bayat okuma yok),
    `gather` bekcisi ise davranisi (biri gecer, oteki 409) kanitlar.
    """
    kurulum = await _kur()
    hazir = asyncio.Event()
    birak = asyncio.Event()
    tx0: asyncio.Task | None = None
    gecici: asyncio.Task | None = None
    try:
        tx0 = asyncio.create_task(_once_gecir_ve_tut(kurulum, hazir, birak))
        await asyncio.wait_for(hazir.wait(), timeout=_TAVAN_SANIYE)

        gecici = asyncio.create_task(_tek_gecis(kurulum, 1, FinancialInstrumentStatus.cancelled))
        # Karsi gorev kilitte bekliyor olmali; tavan yalnizca bozuk bir kurulumun
        # testi asmasini engeller, PENCERE ACMAZ.
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(asyncio.shield(gecici), timeout=_BLOKE_TAVANI)

        birak.set()
        assert await asyncio.wait_for(tx0, timeout=_TAVAN_SANIYE) == "committed"
        sonuc = await asyncio.wait_for(gecici, timeout=_TAVAN_SANIYE)

        assert sonuc == "rejected", (
            "ikinci geçiş, tx0'ın yazdığı `collected` durumunu görmeden geçti — "
            "`change_status` satırı K2 TABLOSUNDAN ÖNCE kilitlemiyor (BAYAT OKUMA)"
        )
        assert await _durum(kurulum.instrument_id) is FinancialInstrumentStatus.collected, (
            "tx0'ın yazdığı durum EZİLDİ — klasik kayıp güncelleme"
        )
    finally:
        birak.set()
        await _guvenli_temizlik(kurulum, tx0, gecici)


async def test_SILME_de_kaydi_DENETIMDEN_ONCE_kilitler() -> None:
    """🔴 Silme ucu de AYNI kilidi alir — DETERMINISTIK BEKCI.

    Silme "portfoyde mi" sorusunu sorar, yani bir oku-karar-ver-yaz kosar;
    kilitsiz halde eszamanli bir gecisle birlesince TAHSIL EDILMIS bir cek
    silinebilirdi (mali izin kaybi).

    Olculen sey bir zamanlama yarisi DEGIL, BAYAT OKUMADIR (modul docstring'i
    md. 2): tx0 kaydi `collected` yapip commit etmeden bekler.
    * kilit YERINDEYSE silme TAZE `collected` degerini okur ve **409** alir;
    * kilit KALDIRILIRSA silme BAYAT `portfolio` okur ve TAHSIL EDILMIS ceki
      SILER → iddia KIRMIZI.
    """
    kurulum = await _kur()
    hazir = asyncio.Event()
    birak = asyncio.Event()
    tx0: asyncio.Task | None = None
    silici: asyncio.Task | None = None
    try:
        tx0 = asyncio.create_task(_once_gecir_ve_tut(kurulum, hazir, birak))
        await asyncio.wait_for(hazir.wait(), timeout=_TAVAN_SANIYE)

        silici = asyncio.create_task(_silme(kurulum, 1))
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(asyncio.shield(silici), timeout=_BLOKE_TAVANI)

        birak.set()
        assert await asyncio.wait_for(tx0, timeout=_TAVAN_SANIYE) == "committed"
        sonuc = await asyncio.wait_for(silici, timeout=_TAVAN_SANIYE)

        assert sonuc == "rejected", (
            "silme, tx0'ın yazdığı `collected` durumunu görmeden geçti — "
            "`delete_instrument` satırı DENETİMDEN ÖNCE kilitlemiyor"
        )
        assert await _durum(kurulum.instrument_id) is FinancialInstrumentStatus.collected, (
            "TAHSİL EDİLMİŞ bir çek silindi — mali iz kaybı"
        )
    finally:
        birak.set()
        await _guvenli_temizlik(kurulum, tx0, silici)
