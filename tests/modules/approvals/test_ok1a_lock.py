"""OK-1A — 🔴 EŞİK = KİLİT. `approve_next_step` zincir satırını DENETİMDEN ÖNCE kilitler.

## Niçin `client`/`seeded_db` KULLANILMAZ

Kök `tests/conftest.py`deki `db_session` her testi TEK bağlantı üzerinde
SAVEPOINT'e sarar ve dış transaction'ı asla gerçekten COMMIT ETMEZ — o session
üzerinde iki görev AYNI bağlantıyı paylaşır ve gerçek satır kilidi test
EDİLEMEZ. Bu dosya `test_fin1_lock.py` desenini izler: `test_engine` üzerinden
İKİ BAĞIMSIZ bağlantı, gerçek commit, gerçek temizlik.

## 🔴 ÇAKIŞMA PENCERESİ DETERMİNİSTİKTİR — sabit `sleep` YOKTUR

FAT-1'in kilit testi mutasyonlu hâlde **dosya bütün koşulunca** kırmızıydı ama
**TEK BAŞINA koşulunca YEŞİLDİ**: izole koşuda havuz SOĞUKTUR, ilk görev bağlantı
kurulumunu beklerken ikincisi henüz başlamamış olur ve iki görev hiç çakışmaz.
Bu yüzden her görev ÖNCE kendi bağlantısını ısıtır (gerçek bir sorgu koşturur,
transaction'ı başlatır) ve ANCAK ONDAN SONRA `asyncio.Barrier`a varır.

## 🔴 POZİTİF KONTROL — ölçüm aleti gerçekten ölçüyor mu (ÖLÇÜLDÜ)

Mutasyon: `repository.get_chain_for_update` içindeki `.with_for_update()`
KALDIRILIR. Üç bekçi bu mutasyon altında tek tek koşuldu:

| bekçi | izole ×3 | dosya bütün |
|---|---|---|
| `..._ESZAMANLI_iki_onay...` (`gather` + baraj) | **3/3 YEŞİL** | YEŞİL |
| `..._TUTULAN_KILIT` (bayat okuma)              | **3/3 KIRMIZI** | KIRMIZI |
| `..._POZITIF_KONTROL` (kilit gerçekten alınıyor mu) | **3/3 KIRMIZI** | KIRMIZI |

🔴 **Yani `gather` bekçisi bu mutasyonu HİÇ GÖRMÜYOR** — FAT-1'in kırılgan
bekçisinin daha da kötü hâli (orada 3'te 2 kırmızıydı). Yarış fiilen kurulmuyor:
görevler barajdan sonra da sıraya giriyor ve ikinci görev, birincinin commit'ini
GÖRMÜŞ hâlde okuyor. Yarışan sayısı 4'e çıkarılarak da denendi — mutasyon yine
yakalanmadı (yalnızca iddia aritmetiği bozuldu).

Bu yüzden `gather` bekçisi burada **DAVRANIŞ KATMANIDIR** ve kilidin kanıtı
SAYILMAZ; kanıt aşağıdaki İKİ DETERMİNİSTİK bekçidir. Biri kilidin ETKİSİNİ
(bayat okuma yok), öteki kilidin VARLIĞINI (ikinci istek gerçekten bekliyor)
ölçer.

## Ayrışma noktası BLOKE OLMAK değil, NE OKUDUĞUDUR (FIN-1 dersi)

tx0 yalnızca kilit tutsaydı, kilidi KALDIRILMIŞ kod da kendi `UPDATE`inin örtük
satır kilidinde bloke olurdu ve "ilerlemedi" iddiası mutasyonda da YEŞİL kalırdı.
Bu yüzden tx0 1. ADIMI GERÇEKTEN KARARA BAĞLAR ve commit etmeden bekler:

* **kilit varken:** karşı görev `FOR UPDATE`te bekler, tx0 commit edince TAZE
  durumu okur ve sıradaki adımın 2 olduğunu görür → `step_no=1` isteği **409**;
* **kilit yokken:** BAYAT durumu okur (1. adım kararsız görünür), bekçilerden
  geçer, `UPDATE`i tx0'ınkinin üstüne yazar → 1. adımın onaylayanı SESSİZCE
  DEĞİŞİR. Klasik kayıp güncelleme — ve onay zincirinde bu, **imzanın
  değişmesi** demektir.
"""

import asyncio
import contextlib
import uuid
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.errors import ConflictError
from app.core.security import hash_password
from app.modules.approvals import definitions, repository, service
from app.modules.approvals.models import (
    ApprovalChain,
    ApprovalDocumentType,
    ApprovalStep,
)
from app.modules.roles.models import Role
from app.modules.users.models import User
from tests.conftest import test_engine

_SessionFactory = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)

#: Rol anahtari ve e-postalar TESTE OZELDIR: bu dosya GERCEKTEN commit ettigi
#: icin sizinti ancak yaratilan satirlarin tam bilinmesiyle kapanir.
#: IZN-B3b: adım sahipliği ROL ANAHTARINDAN gelir ("Tüm projeler" + ANA rol) — yarışan şeflerin
#: rolü `site_chief` olmak ZORUNDADIR. Test DB'sinde COMMIT'li seed yoktur (testler geri alınır);
#: varsa o rol kullanılır ve temizlikte SİLİNMEZ.
_ROL_ANAHTARI = "site_chief"
_EPOSTALAR = ("ok1a-kilit0@conc.co", "ok1a-kilit1@conc.co", "ok1a-kilit2@conc.co")
_TASERON = ApprovalDocumentType.subcontractor_progress_payment

_TAVAN_SANIYE = 15
#: "Bloke kalmali" iddiasinin UST SINIRI. Pencere ACMAZ.
_BLOKE_TAVANI = 2


class _Kurulum:
    def __init__(self, document_id, chain_id, actor_ids, role_id) -> None:  # noqa: ANN001
        # `role_id None` = rol bu testten ONCE vardı, temizlikte SİLİNMEZ.
        self.document_id = document_id
        self.chain_id = chain_id
        self.actor_ids = actor_ids
        self.role_id = role_id


async def _kur() -> _Kurulum:
    """Zincir DOGRUDAN yazilir (`create_chain` DEGIL).

    Gerekce: `create_chain` sirket tekil satirini (`company`) yoksa YARATIR ve bu
    dosya GERCEKTEN commit eder — o satir turun geri kalanina sizardi. Bu testin
    olctugu sey zaten `approve_next_step`tir; esik anlik degil SNAPSHOT'tan okunur
    ve snapshot burada acikca yazilir.
    """
    document_id = uuid.uuid4()
    async with _SessionFactory() as session:
        role = await session.scalar(select(Role).where(Role.key == _ROL_ANAHTARI))
        olusturulan_rol_id = None
        if role is None:
            role = Role(key=_ROL_ANAHTARI, name="Şantiye Şefi")
            session.add(role)
            await session.flush()
            olusturulan_rol_id = role.id
        # 0 = evrağı YARATAN ("Tüm projeler" DEĞİL: hiçbir adımı onaylayamaz) · 1-2 = yarışan
        # şefler ("Tüm projeler" + ana rol `site_chief` = adımın sahibi).
        aktorler = [
            User(
                email=eposta,
                password_hash=hash_password("parola1234"),
                full_name=f"Kilit Aktörü {sira}",
                role_id=role.id,
                all_projects=sira > 0,
            )
            for sira, eposta in enumerate(_EPOSTALAR)
        ]
        session.add_all(aktorler)
        await session.flush()

        chain = ApprovalChain(
            document_type=_TASERON,
            document_id=document_id,
            threshold_snapshot=definitions.DEFAULT_APPROVAL_THRESHOLD_TRY,
            amount_snapshot=Decimal("100.00"),
            created_by_user_id=aktorler[0].id,
            created_at=datetime.now(UTC),
        )
        session.add(chain)
        await session.flush()
        for sira, rol in enumerate(definitions.CHAIN_DEFINITIONS[_TASERON], start=1):
            session.add(ApprovalStep(chain_id=chain.id, step_no=sira, approval_role=rol))
        await session.commit()
        return _Kurulum(document_id, chain.id, [a.id for a in aktorler], olusturulan_rol_id)


async def _gorevleri_bosalt(*gorevler) -> None:  # noqa: ANN002
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
        # Adimlar CASCADE ile gider; zincir acikca dusurulur.
        await session.execute(delete(ApprovalChain).where(ApprovalChain.id == kurulum.chain_id))
        await session.execute(delete(User).where(User.id.in_(kurulum.actor_ids)))
        if kurulum.role_id is not None:
            await session.execute(delete(Role).where(Role.id == kurulum.role_id))
        await session.commit()


async def _guvenli_temizlik(kurulum: _Kurulum, *gorevler) -> None:  # noqa: ANN002
    await _gorevleri_bosalt(*gorevler)
    with contextlib.suppress(Exception):
        await asyncio.wait_for(_temizle(kurulum), timeout=_TAVAN_SANIYE)


async def _isin_ve_bekle(session: AsyncSession, actor_id, baraj: asyncio.Barrier) -> User:  # noqa: ANN001
    """🔴 ISINMA + BARAJ — determinizmin tamami buradadir.

    `session.get` baglantiyi havuzdan CEKER, transaction'i baslatir ve gercek bir
    sorgu kosturur. Baraja ancak ondan sonra varilir; isinma barajdan SONRA
    yapilsaydi izole kosuda baglanti kurulum gecikmesi iki gorevi siraya sokar ve
    pencere HIC ACILMAZDI.
    """
    actor = await session.get(User, actor_id)
    await asyncio.wait_for(baraj.wait(), timeout=_TAVAN_SANIYE)
    return actor


async def _onayla(kurulum: _Kurulum, aktor_sirasi: int, baraj: asyncio.Barrier | None) -> str:
    async with _SessionFactory() as session:
        if baraj is None:
            actor = await session.get(User, kurulum.actor_ids[aktor_sirasi])
        else:
            actor = await _isin_ve_bekle(session, kurulum.actor_ids[aktor_sirasi], baraj)
        try:
            await service.approve_next_step(
                session,
                actor=actor,
                document_type=_TASERON,
                document_id=kurulum.document_id,
                step_no=1,
            )
        except ConflictError:
            await session.rollback()
            return "conflict"
        await session.commit()
        return "approved"


async def _once_onayla_ve_tut(kurulum: _Kurulum, hazir: asyncio.Event, birak: asyncio.Event) -> str:
    """tx0: 1. adimi GERCEKTEN karara baglar ama COMMIT ETMEDEN bekler."""
    async with _SessionFactory() as session:
        actor = await session.get(User, kurulum.actor_ids[1])
        await service.approve_next_step(
            session,
            actor=actor,
            document_type=_TASERON,
            document_id=kurulum.document_id,
            step_no=1,
        )
        hazir.set()
        await birak.wait()
        await session.commit()
        return "committed"


async def _adim_kararlari(chain_id) -> list[tuple[int, uuid.UUID | None]]:  # noqa: ANN001
    """Son sozu DB soyler: kararlar TAZE bir baglantidan okunur."""
    async with _SessionFactory() as session:
        satirlar = (
            await session.execute(
                select(ApprovalStep)
                .where(ApprovalStep.chain_id == chain_id)
                .order_by(ApprovalStep.step_no)
            )
        ).scalars()
        return [(satir.step_no, satir.decided_by_user_id) for satir in satirlar]


async def test_ESZAMANLI_iki_onay_AYNI_adimi_IKI_KEZ_ilerletemez() -> None:
    """🔴 ASIL MUTASYON REGRESYONU — EŞİK = KİLİT.

    İki gerçek bağlantıdaki İKİ FARKLI şef aynı zincirin 1. adımını aynı anda
    onaylamaya çalışır. Doğru davranış: BİRİ geçer, ÖTEKİ **409** alır.

    ⚠️ **BU BEKÇİ KİLİDİN KANITI DEĞİLDİR** (modül docstring'indeki ölçüm
    tablosu): `.with_for_update()` kaldırıldığında 3/3 YEŞİL kaldı — yarış
    fiilen kurulmuyor. DAVRANIŞ katmanı olarak kalır; kilidin kanıtı aşağıdaki
    iki deterministik bekçidir.
    """
    kurulum = await _kur()
    baraj = asyncio.Barrier(2)
    gorevler: list[asyncio.Task] = []
    try:
        gorevler = [
            asyncio.create_task(_onayla(kurulum, 1, baraj)),
            asyncio.create_task(_onayla(kurulum, 2, baraj)),
        ]
        sonuclar = list(await asyncio.wait_for(asyncio.gather(*gorevler), timeout=_TAVAN_SANIYE))

        assert sonuclar.count("approved") == 1, (
            f"iki eşzamanlı onay da geçti ({sonuclar}) — `approve_next_step` zincir "
            "satırını BEKÇİLERDEN ÖNCE kilitlemiyor; sıra koruması eşzamanlı istekte kör"
        )
        assert sonuclar.count("conflict") == 1, sonuclar

        kararlar = await _adim_kararlari(kurulum.chain_id)
        assert kararlar[0][1] in kurulum.actor_ids[1:], kararlar
        assert kararlar[1][1] is None, "2. adım hiç ilerlememeliydi"
    finally:
        await _guvenli_temizlik(kurulum, *gorevler)


async def test_ADIM_kaydi_DENETIMDEN_ONCE_kilitlenir_TUTULAN_KILIT() -> None:
    """🔴 KİLİDİN ASIL BEKÇİSİ — deterministik, zamanlamadan BAĞIMSIZ.

    Yukarıdaki `gather` bekçisi yakalama gücünü iki görevin gerçekten
    kesişmesine borçludur (FAT-1'de izole koşuda kör kaldı). Bu test yerine
    BAYAT OKUMAYI ölçer: tx0 1. adımı karara bağlayıp COMMIT ETMEDEN bekler.

    * kilit YERİNDEYSE ikinci görev `FOR UPDATE`te bekler, tx0 commit edince
      sıradaki adımın **2** olduğunu görür ve `step_no=1` isteği **409** alır;
    * kilit KALDIRILIRSA ikinci görev BAYAT durumu okur, bekçilerden geçer ve
      tx0'ın imzasını EZER → iki iddia da KIRMIZI, **her turda**.
    """
    kurulum = await _kur()
    hazir = asyncio.Event()
    birak = asyncio.Event()
    tx0: asyncio.Task | None = None
    gecici: asyncio.Task | None = None
    try:
        tx0 = asyncio.create_task(_once_onayla_ve_tut(kurulum, hazir, birak))
        await asyncio.wait_for(hazir.wait(), timeout=_TAVAN_SANIYE)

        gecici = asyncio.create_task(_onayla(kurulum, 2, None))
        # Karsi gorev kilitte bekliyor olmali; tavan yalnizca bozuk bir kurulumun
        # testi asmasini engeller, PENCERE ACMAZ.
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(asyncio.shield(gecici), timeout=_BLOKE_TAVANI)

        birak.set()
        assert await asyncio.wait_for(tx0, timeout=_TAVAN_SANIYE) == "committed"
        sonuc = await asyncio.wait_for(gecici, timeout=_TAVAN_SANIYE)

        assert sonuc == "conflict", (
            "ikinci onay, tx0'ın karara bağladığı 1. adımı GÖRMEDEN geçti — "
            "`approve_next_step` zinciri BEKÇİLERDEN ÖNCE kilitlemiyor (BAYAT OKUMA)"
        )
        kararlar = await _adim_kararlari(kurulum.chain_id)
        assert kararlar[0][1] == kurulum.actor_ids[1], (
            "1. adımın ONAYLAYANI değişti — tx0'ın imzası EZİLDİ (kayıp güncelleme)"
        )
        assert kararlar[1][1] is None, kararlar
    finally:
        birak.set()
        await _guvenli_temizlik(kurulum, tx0, gecici)


async def _kilidi_tut(kurulum: _Kurulum, hazir: asyncio.Event, birak: asyncio.Event) -> str:
    """tx0: zincir satirinin KILIDINI alir ama HICBIR SEY YAZMAZ.

    Yazmamasi KASITLIDIR ve bu testin ayirt ediciligi tam olarak buradan gelir:
    tx0 bir adima yazsaydi, kilidi KALDIRILMIS kod da kendi `UPDATE`inin ortuk
    satir kilidinde bloke olurdu ve "ikinci istek bekledi" iddiasi mutasyonda da
    YESIL kalirdi (FIN-1 dersi). Hicbir yazma olmadigi icin bloke olmanin TEK
    sebebi `FOR UPDATE`tir.
    """
    async with _SessionFactory() as session:
        chain = await repository.get_chain_for_update(session, _TASERON, kurulum.document_id)
        assert chain is not None
        hazir.set()
        await birak.wait()
        await session.rollback()
        return "released"


async def test_ZINCIR_KILIDI_GERCEKTEN_ALINIYOR_POZITIF_KONTROL() -> None:
    """🔴 ÖLÇÜM ALETİNİN KENDİSİNİ ÖLÇER (madde 9, FIS-NO dersi).

    FIS-NO'da eşzamanlılık bekçisi, istek yolunda daha önce alınmış ALAKASIZ bir
    kilit tarafından maskeleniyordu ve özellik HİÇ YOKKEN bile yeşil geçiyordu.
    Bu test o sınıfı kapatır: `approve_next_step`in gerçekten ZİNCİR SATIRINI
    kilitleyip kilitlemediğini, başka hiçbir kilidin karışamayacağı bir
    kurulumda ölçer.

    tx0 yalnızca `FOR UPDATE` tutar, HİÇBİR SATIRA YAZMAZ. O hâlde:

    * kilit YERİNDEYSE ikinci istek tx0 serbest bırakana kadar İLERLEYEMEZ;
    * kilit KALDIRILIRSA ikinci isteği tutacak BAŞKA HİÇBİR ŞEY YOKTUR ve istek
      tx0 daha elini kaldırmadan biter → iddia KIRMIZI, her turda.

    Son iddia da önemlidir: tx0 bırakınca ikinci istek GEÇER. Kilidin kalıcı bir
    tıkanma üretmediği, yalnızca sıraya soktuğu böylece kanıtlanır.
    """
    kurulum = await _kur()
    hazir = asyncio.Event()
    birak = asyncio.Event()
    tx0: asyncio.Task | None = None
    bekleyen: asyncio.Task | None = None
    try:
        tx0 = asyncio.create_task(_kilidi_tut(kurulum, hazir, birak))
        await asyncio.wait_for(hazir.wait(), timeout=_TAVAN_SANIYE)

        bekleyen = asyncio.create_task(_onayla(kurulum, 2, None))
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(asyncio.shield(bekleyen), timeout=_BLOKE_TAVANI)

        assert not bekleyen.done(), (
            "ikinci onay, tx0 zincir satırının kilidini TUTARKEN tamamlandı — "
            "`approve_next_step` zincir satırını HİÇ KİLİTLEMİYOR (tx0 hiçbir "
            "satıra yazmadı, bloke edecek başka bir şey YOKTU)"
        )

        birak.set()
        assert await asyncio.wait_for(tx0, timeout=_TAVAN_SANIYE) == "released"
        assert await asyncio.wait_for(bekleyen, timeout=_TAVAN_SANIYE) == "approved", (
            "kilit bırakıldıktan sonra ikinci onay geçmeliydi — kilit sıraya sokar, tıkamaz"
        )
    finally:
        birak.set()
        await _guvenli_temizlik(kurulum, tx0, bekleyen)
