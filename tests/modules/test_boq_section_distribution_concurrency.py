"""🔴 BDG-B1.2 — `PUT /sites/{site_id}/boq/section-distribution` EŞİK = KİLİT bekçisi.

Matris ucu BİRLEŞTİRMEdir: gövdede geçmeyen hücre KORUNUR. Dolayısıyla tek kalem
ucunun (tam küme değiştirme, `test_boq_allocation_concurrency.py`) aksine iki
eşzamanlı matris isteği aynı kalemin FARKLI bölümlerine yazarsa paylar TOPLANIR:
kilit olmadan ikisi de "mevcut 0" okur, ikisi de 60 yazar ve 100'lük kalem 120
dağıtılmış görünür. Kilit (`repository.lock_items`: tek sorgu, `ORDER BY id`,
`FOR UPDATE`, `populate_existing`) bu pencereyi kapatır.

Neden `client`/`seeded_db` KULLANILMAZ: `tests/conftest.py`'deki `db_session` her
testi TEK bağlantıda SAVEPOINT'e sarar ve asla COMMIT ETMEZ — iki görev aynı
bağlantıyı paylaşır, gerçek satır kilidi test EDİLEMEZ. Bu dosya emsalin
(`tests/modules/test_boq_allocation_concurrency.py`) desenini izler: İKİ BAĞIMSIZ
bağlantı, gerçek commit, sonunda gerçek temizlik.

Maskelenme kanonu: istek yolunda ALAKASIZ bir kilit (ör. şantiye satırı) yarışı
yutabilir ve test yine yeşil kalır. Bu yüzden (1) bekleyen sorgunun METNİ iddia
edilir (`boq_items` + `FOR UPDATE` + `ORDER BY boq_items.id`), (2) POZİTİF KONTROL
testi `lock_items`i kilitsiz sürümle değiştirip aynı senaryonun KIRMIZI sonuca
(Σ > kota) döndüğünü gösterir — bekçinin kör olmadığı dosyanın içinde kanıtlıdır.
"""

import asyncio
import contextlib
import uuid
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from decimal import Decimal

import pytest
from sqlalchemy import delete, event, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.discipline_scope import UNRESTRICTED
from app.core.errors import ConflictError, SiteValidationError
from app.core.security import hash_password
from app.modules.boq import repository, section_distribution, service
from app.modules.boq.models import BoqGroup, BoqItem, BoqItemSectionAllocation
from app.modules.boq.schemas import (
    BoqItemAllocationInput,
    BoqItemAllocationsReplace,
    SectionDistributionCellInput,
    SectionDistributionSave,
)
from app.modules.projects.models import Project, ProjectStatus, ProjectType
from app.modules.roles.models import Role
from app.modules.sites.models import Section, Site
from app.modules.users.models import ProjectMember, User
from tests._yaris import YARIS_TAVANI_SN, kilitte_bekleyen_sorgu
from tests.conftest import test_engine

pytestmark = pytest.mark.asyncio

_SessionFactory = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)

#: Anahtarlar TESTE ÖZELDİR: dosya GERÇEKTEN commit eder, sızıntı ancak yaratılan
#: satırların tam bilinmesiyle kapanır.
_ROL_ANAHTARI = "bdg_b12_conc_admin"
_EPOSTA = "bdg-b12@conc.co"
_PROJE_KODU = "BDGB12-CONC"
_KOD_A = "BDG.A"
_KOD_B = "BDG.B"

#: Kalem kotası 100; her istek 60 ister — TEK BAŞINA sığar, BİRLİKTE (120) sığmaz.
KOTA = Decimal("100.000")
YARIM_USTU = Decimal("60.000")

#: Kesişen iki kalemde her iki isteğin de sığdığı pay (2 × 40 = 80 ≤ 100).
SIGAN = Decimal("40.000")

#: Kilitlenme turu: serbest kesişimde tekrar sayısı.
_KILITLENME_TURU = 5

Hucre = tuple[uuid.UUID, uuid.UUID, Decimal]


@dataclass(frozen=True)
class _Kurulum:
    item_a: uuid.UUID
    item_b: uuid.UUID
    section_ids: list[uuid.UUID]
    site_id: uuid.UUID
    project_id: uuid.UUID
    actor_id: uuid.UUID
    role_id: uuid.UUID


@dataclass(frozen=True)
class _YarisSonucu:
    bekleyen: str | None  # ikinci görevin kilitte beklediği sorgu
    gozlemci: AssertionError | None  # bariyer gözlemcisinin hatası (kilitsiz imzası)
    ikinci_once_bitti: bool  # ikinci görev, birinci kilidi bırakmadan BİTTİ mi
    birinci: str
    ikinci: str


async def _kur() -> _Kurulum:
    async with _SessionFactory() as session:
        role = Role(key=_ROL_ANAHTARI, name="BDG Matris Eşzamanlılık Rolü")
        session.add(role)
        await session.flush()

        aktor = User(
            email=_EPOSTA,
            password_hash=hash_password("parola1234"),
            full_name="Matris Aktörü",
            role_id=role.id,
        )
        project = Project(
            code=_PROJE_KODU,
            name="Matris Eşzamanlılık Projesi",
            status=ProjectStatus.active,
            budget=Decimal("1000000.00"),
            progress_pct=Decimal("0.00"),
            project_type=ProjectType.taahhut,
        )
        session.add_all([aktor, project])
        await session.flush()
        aktor.all_projects = True

        site = Site(project_id=project.id, code="BDG-CONC", name="Matris Şantiyesi")
        session.add(site)
        await session.flush()

        group = BoqGroup(site_id=site.id, name="BETON İŞLERİ")
        sections = [
            Section(site_id=site.id, name="Kat 1-5", sort_order=1),
            Section(site_id=site.id, name="Kat 6-10", sort_order=2),
        ]
        session.add_all([group, *sections])
        await session.flush()

        items = [
            BoqItem(
                site_id=site.id,
                group_id=group.id,
                code=code,
                description=f"Kalem {code}",
                unit="m³",
                quantity=KOTA,
                unit_price=Decimal("100.00"),
            )
            for code in (_KOD_A, _KOD_B)
        ]
        session.add_all(items)
        await session.flush()
        await session.commit()
        return _Kurulum(
            item_a=items[0].id,
            item_b=items[1].id,
            section_ids=[s.id for s in sections],
            site_id=site.id,
            project_id=project.id,
            actor_id=aktor.id,
            role_id=role.id,
        )


async def _temizle(kurulum: _Kurulum) -> None:
    item_ids = [kurulum.item_a, kurulum.item_b]
    async with _SessionFactory() as session:
        await session.execute(
            delete(BoqItemSectionAllocation).where(
                BoqItemSectionAllocation.boq_item_id.in_(item_ids)
            )
        )
        await session.execute(delete(BoqItem).where(BoqItem.site_id == kurulum.site_id))
        await session.execute(delete(Section).where(Section.site_id == kurulum.site_id))
        await session.execute(delete(BoqGroup).where(BoqGroup.site_id == kurulum.site_id))
        await session.execute(delete(Site).where(Site.id == kurulum.site_id))
        await session.execute(
            delete(ProjectMember).where(ProjectMember.user_id == kurulum.actor_id)
        )
        await session.execute(delete(User).where(User.id == kurulum.actor_id))
        await session.execute(delete(Project).where(Project.id == kurulum.project_id))
        await session.execute(delete(Role).where(Role.id == kurulum.role_id))
        await session.commit()


@pytest.fixture
async def kurulum():
    veri = await _kur()
    try:
        yield veri
    finally:
        await _temizle(veri)


async def _sonlandir(*gorevler: asyncio.Task | None) -> None:
    """Temizlikten ÖNCE görevleri kapatır — MUTASYON DENETİMİ İÇİN ŞART.

    Bir iddia kırmızıya döndüğünde birinci görev commit etmemiş transaction'ında
    satır kilidi tutuyor olabilir; kapatılmazsa temizliğin DELETE'i o kilidi
    SÜRESİZ bekler ve kırmızı test askıya dönüşür. Önce nazikçe (kilit serbest),
    takılan kalırsa iptal.
    """
    for gorev in gorevler:
        if gorev is None:
            continue
        if not gorev.done():
            with contextlib.suppress(BaseException):
                await asyncio.wait_for(asyncio.shield(gorev), timeout=YARIS_TAVANI_SN)
        if not gorev.done():
            gorev.cancel()
        with contextlib.suppress(BaseException):
            await gorev


async def _aktor(session: AsyncSession, actor_id: uuid.UUID) -> User:
    return (await session.execute(select(User).where(User.id == actor_id))).scalar_one()


def _matris_govdesi(hucreler: Iterable[Hucre]) -> SectionDistributionSave:
    return SectionDistributionSave(
        allocations=[
            SectionDistributionCellInput(boq_item_id=i, section_id=s, quantity=q)
            for i, s, q in hucreler
        ]
    )


async def _tut_ve_commit(
    session: AsyncSession, kilit_alindi: asyncio.Event | None, kilidi_birak: asyncio.Event | None
) -> None:
    """Kilit alındıysa sinyal gelene kadar COMMIT ETMEZ — `flush` kilidi BIRAKMAZ."""
    if kilit_alindi is not None and kilidi_birak is not None:
        kilit_alindi.set()
        await kilidi_birak.wait()
    await session.commit()


async def _matris_kaydet(
    kurulum: _Kurulum,
    hucreler: list[Hucre],
    kilit_alindi: asyncio.Event | None = None,
    kilidi_birak: asyncio.Event | None = None,
) -> str:
    async with _SessionFactory() as session:
        actor = await _aktor(session, kurulum.actor_id)
        try:
            await section_distribution.save_section_distribution(
                session, actor, kurulum.site_id, _matris_govdesi(hucreler), UNRESTRICTED
            )
        except SiteValidationError as exc:
            await session.rollback()
            return f"asim: {exc}"
        await _tut_ve_commit(session, kilit_alindi, kilidi_birak)
        return "ok"


async def _tek_kalem_kaydet(
    kurulum: _Kurulum,
    item_id: uuid.UUID,
    paylar: dict[uuid.UUID, Decimal],
    kilit_alindi: asyncio.Event | None = None,
    kilidi_birak: asyncio.Event | None = None,
) -> str:
    govde = BoqItemAllocationsReplace(
        allocations=[BoqItemAllocationInput(section_id=s, quantity=q) for s, q in paylar.items()]
    )
    async with _SessionFactory() as session:
        actor = await _aktor(session, kurulum.actor_id)
        try:
            await service.replace_allocations(session, actor, item_id, govde)
        except ConflictError:
            await session.rollback()
            return "conflict"
        await _tut_ve_commit(session, kilit_alindi, kilidi_birak)
        return "ok"


Birinci = Callable[[asyncio.Event, asyncio.Event], Awaitable[str]]
Ikinci = Callable[[], Awaitable[str]]


async def _yaris(birinci: Birinci, ikinci: Ikinci) -> _YarisSonucu:
    """Bariyerli yarış: birinci yazmayı bitirip KİLİDİ TUTAR, ikinci o sırada başlar.

    Çıplak `asyncio.gather` iki görevi kritik anda KESİŞTİRMEYEBİLİR; burada
    ikincinin birincinin kilidinde BEKLEDİĞİ `pg_stat_activity` ile doğrudan
    ölçülür. Kilitsiz hâlde gözlemci "BEKLEMEDEN bitti" der — hata yutulmaz,
    `gozlemci`de döner ve çağıran iddia eder (pozitif kontrol bunu kullanır).
    """
    kilit_alindi = asyncio.Event()
    kilidi_birak = asyncio.Event()
    gorev1: asyncio.Task[str] | None = None
    gorev2: asyncio.Task[str] | None = None
    bekleyen: str | None = None
    gozlemci: AssertionError | None = None
    try:
        gorev1 = asyncio.create_task(birinci(kilit_alindi, kilidi_birak))
        await asyncio.wait_for(kilit_alindi.wait(), timeout=YARIS_TAVANI_SN)

        gorev2 = asyncio.create_task(ikinci())
        try:
            bekleyen = await kilitte_bekleyen_sorgu(
                test_engine,
                gorev2,
                mesaj="ikinci yazma, birincinin kilidi serbest bırakılmadan ilerleyebildi — "
                "kalem satırları `FOR UPDATE` ile KİLİTLENMİYOR olabilir",
            )
        except AssertionError as exc:
            gozlemci = exc
        ikinci_once_bitti = gorev2.done()

        kilidi_birak.set()
        sonuc1 = await asyncio.wait_for(gorev1, timeout=YARIS_TAVANI_SN)
        sonuc2 = await asyncio.wait_for(gorev2, timeout=YARIS_TAVANI_SN)
    finally:
        kilidi_birak.set()
        await _sonlandir(gorev1, gorev2)
    return _YarisSonucu(bekleyen, gozlemci, ikinci_once_bitti, sonuc1, sonuc2)


async def _paylar(kurulum: _Kurulum) -> dict[uuid.UUID, dict[uuid.UUID, Decimal]]:
    async with _SessionFactory() as session:
        satirlar = (
            (
                await session.execute(
                    select(BoqItemSectionAllocation).where(
                        BoqItemSectionAllocation.boq_item_id.in_([kurulum.item_a, kurulum.item_b])
                    )
                )
            )
            .scalars()
            .all()
        )
    sonuc: dict[uuid.UUID, dict[uuid.UUID, Decimal]] = {kurulum.item_a: {}, kurulum.item_b: {}}
    for row in satirlar:
        sonuc[row.boq_item_id][row.section_id] = row.quantity
    return sonuc


def _toplam(paylar: dict[uuid.UUID, Decimal]) -> Decimal:
    return sum(paylar.values(), Decimal("0"))


def _kalem_kilidi_mi(sorgu: str | None) -> bool:
    return sorgu is not None and "FROM boq_items" in sorgu and "FOR UPDATE" in sorgu


async def _ayni_kalem_farkli_bolum_yarisi(kurulum: _Kurulum) -> _YarisSonucu:
    """Test 1 ve pozitif kontrolün ORTAK senaryosu: A/bölüm-0 ← 60, A/bölüm-1 ← 60."""
    s0, s1 = kurulum.section_ids
    return await _yaris(
        lambda alindi, birak: _matris_kaydet(
            kurulum, [(kurulum.item_a, s0, YARIM_USTU)], alindi, birak
        ),
        lambda: _matris_kaydet(kurulum, [(kurulum.item_a, s1, YARIM_USTU)]),
    )


async def test_iki_esZamanli_matris_PUT_ayni_kalem_farkli_bolum_kotayi_asamaz(
    kurulum: _Kurulum,
) -> None:
    """🔴 Birleştirme semantiğinde paylar TOPLANIR: kilit yoksa 60 + 60 = 120 > 100.

    Kilitle ikinci istek `boq_items ... FOR UPDATE`te BEKLER (iki AYRI bağlantının
    da kanıtı), birinci commit edince TAZE payları okur ve 422 alır.
    """
    sonuc = await _ayni_kalem_farkli_bolum_yarisi(kurulum)
    paylar = await _paylar(kurulum)

    # Değer iddiaları ÖNCE: kilitsiz mutantta kırmızının sebebi (Σ > kota) görünsün.
    assert _toplam(paylar[kurulum.item_a]) <= KOTA, (
        f"Σ pay {_toplam(paylar[kurulum.item_a])} > kota {KOTA} — yarışla aşıldı "
        f"(sonuçlar: {sonuc.birinci!r}, {sonuc.ikinci!r})"
    )
    assert sonuc.birinci == "ok"
    assert sonuc.ikinci.startswith("asim") and _KOD_A in sonuc.ikinci, sonuc.ikinci
    assert paylar[kurulum.item_a] == {kurulum.section_ids[0]: YARIM_USTU}

    assert sonuc.gozlemci is None, str(sonuc.gozlemci)
    assert not sonuc.ikinci_once_bitti, "ikinci istek kilit tutulurken BİTTİ"
    assert _kalem_kilidi_mi(sonuc.bekleyen), sonuc.bekleyen
    assert sonuc.bekleyen is not None and "ORDER BY boq_items.id" in sonuc.bekleyen, (
        f"ikinci istek `lock_items`ten başka bir kilitte bekliyor (maskelenme?): {sonuc.bekleyen}"
    )


async def test_tek_kalem_ucu_tutarken_matris_PUT_bekler_ve_taze_payi_gorur(
    kurulum: _Kurulum,
) -> None:
    """Çapraz uç (1/2): tek kalem ucu A/bölüm-0 ← 60 yazıp kilidi tutar; matris
    A/bölüm-1 ← 60 ister. İki uç AYNI satır kilidinde serileşmeseydi matris
    "mevcut 0" görür ve Σ 120 yazılırdı."""
    s0, s1 = kurulum.section_ids
    sonuc = await _yaris(
        lambda alindi, birak: _tek_kalem_kaydet(
            kurulum, kurulum.item_a, {s0: YARIM_USTU}, alindi, birak
        ),
        lambda: _matris_kaydet(kurulum, [(kurulum.item_a, s1, YARIM_USTU)]),
    )
    paylar = await _paylar(kurulum)

    assert _toplam(paylar[kurulum.item_a]) <= KOTA, (
        f"Σ pay {_toplam(paylar[kurulum.item_a])} > kota {KOTA} — iki uç serileşmedi "
        f"(sonuçlar: {sonuc.birinci!r}, {sonuc.ikinci!r})"
    )
    assert sonuc.birinci == "ok"
    assert sonuc.ikinci.startswith("asim") and _KOD_A in sonuc.ikinci, sonuc.ikinci
    assert paylar[kurulum.item_a] == {s0: YARIM_USTU}

    assert sonuc.gozlemci is None, str(sonuc.gozlemci)
    # 🔴 Metin iddiası ŞART: kilitsiz mutantta matris yine BEKLER — ama tek kalem
    # ucunun `FOR UPDATE`'i ile çakışan FK `FOR KEY SHARE`i yüzünden `INSERT`te,
    # karar verildikten SONRA (ölçüldü). Yalnız "bekledi mi" bakan bekçi kör olurdu.
    assert _kalem_kilidi_mi(sonuc.bekleyen), sonuc.bekleyen


async def test_matris_PUT_tutarken_tek_kalem_ucu_bekler(kurulum: _Kurulum) -> None:
    """Çapraz uç (2/2): matris A/bölüm-0 ← 60 yazıp kilidi tutar; tek kalem ucu
    (tam küme) {bölüm-0: 30, bölüm-1: 50} ister → bekler, sonra matrisin satırını
    GÖREREK günceller (kimlik korunur, çoğalma yok); Σ 80 ≤ 100.

    ⚠️ Bu yön `lock_items`i BEKÇİLEMEZ (ölçüldü: `with_for_update` silinince de
    yeşil): matrisin eklediği tahsis satırının FK'si kalem satırında
    `FOR KEY SHARE` tutar ve tek kalem ucunun `FOR UPDATE`'i onda bekler. Bu test
    tek kalem ucunun kendi kilidini ve iki ucun sonuç tutarlılığını sabitler;
    `lock_items` bekçisi test 1, ters yön testi ve pozitif kontroldür."""
    s0, s1 = kurulum.section_ids
    sonuc = await _yaris(
        lambda alindi, birak: _matris_kaydet(
            kurulum, [(kurulum.item_a, s0, YARIM_USTU)], alindi, birak
        ),
        lambda: _tek_kalem_kaydet(
            kurulum, kurulum.item_a, {s0: Decimal("30.000"), s1: Decimal("50.000")}
        ),
    )

    assert sonuc.gozlemci is None, str(sonuc.gozlemci)
    assert _kalem_kilidi_mi(sonuc.bekleyen), sonuc.bekleyen
    assert (sonuc.birinci, sonuc.ikinci) == ("ok", "ok")

    paylar = await _paylar(kurulum)
    assert paylar[kurulum.item_a] == {s0: Decimal("30.000"), s1: Decimal("50.000")}
    assert _toplam(paylar[kurulum.item_a]) <= KOTA


async def test_kesisen_kalemler_TERS_sirada_kilitlenme_yaratmaz(kurulum: _Kurulum) -> None:
    """Kilitlenme (deadlock) bekçisi: iki istek kesişen {A, B} kümesini gövdede
    TERS sırada gönderir. Kilit tek sorguda `ORDER BY id` ile alındığından ikisi
    de AYNI ilk satırda buluşur — biri bekler, ikisi birden asılmaz.

    1. Bariyerli tur: ikinci istek gerçekten kesişen kilitte bekler, sonra biter.
    2. Serbest tur: bariyersiz `gather` × `_KILITLENME_TURU`; PG kilitlenme
       saptasaydı `DeadlockDetectedError` yükselir ve test kırmızı olurdu.
    Son durum her turda invarianttan geçer (Σ ≤ kota, her iki kalemde).
    """
    a, b = kurulum.item_a, kurulum.item_b
    s0, s1 = kurulum.section_ids

    sonuc = await _yaris(
        lambda alindi, birak: _matris_kaydet(
            kurulum, [(b, s0, SIGAN), (a, s0, SIGAN)], alindi, birak
        ),
        lambda: _matris_kaydet(kurulum, [(a, s1, SIGAN), (b, s1, SIGAN)]),
    )
    assert sonuc.gozlemci is None, str(sonuc.gozlemci)
    assert _kalem_kilidi_mi(sonuc.bekleyen), sonuc.bekleyen
    assert (sonuc.birinci, sonuc.ikinci) == ("ok", "ok")

    for tur in range(_KILITLENME_TURU):
        pay = Decimal(10 + tur).quantize(Decimal("0.001"))
        sonuclar = await asyncio.wait_for(
            asyncio.gather(
                _matris_kaydet(kurulum, [(b, s0, pay), (a, s0, pay)]),
                _matris_kaydet(kurulum, [(a, s1, pay), (b, s1, pay)]),
            ),
            timeout=YARIS_TAVANI_SN,
        )
        assert sonuclar == ["ok", "ok"], (tur, sonuclar)

    paylar = await _paylar(kurulum)
    son = Decimal(10 + _KILITLENME_TURU - 1).quantize(Decimal("0.001"))
    for item_id in (a, b):
        assert paylar[item_id] == {s0: son, s1: son}
        assert _toplam(paylar[item_id]) <= KOTA


async def test_kilit_mevcut_pay_okumasindan_ONCE_tek_sorguda_id_sirasiyla_alinir(
    kurulum: _Kurulum,
) -> None:
    """Kilit SQL'de görünür ve YERİ sabittir.

    1. `boq_items` gövdedeki TÜM kalemler için TEK `FOR UPDATE` sorgusunda,
       `ORDER BY boq_items.id` ile kilitlenir (kalem başına ayrı kilit gövde
       sırasını izler → ters sıralı iki istekte kilitlenme çevrimi);
    2. mevcut payları okuyan `boq_item_section_allocations` sorgusu kilitten
       SONRA gelir — önce gelseydi TOCTOU penceresi açık kalır, değer iddiaları
       ise tek istekte yine yeşil olurdu.
    """
    s0, s1 = kurulum.section_ids
    ifadeler: list[str] = []

    def kaydet(conn, cursor, statement, parameters, context, executemany) -> None:  # noqa: ANN001
        ifadeler.append(" ".join(statement.split()))

    event.listen(test_engine.sync_engine, "before_cursor_execute", kaydet)
    try:
        sonuc = await _matris_kaydet(
            kurulum, [(kurulum.item_b, s1, SIGAN), (kurulum.item_a, s0, SIGAN)]
        )
    finally:
        event.remove(test_engine.sync_engine, "before_cursor_execute", kaydet)
    assert sonuc == "ok"

    kilit = [i for i, ifade in enumerate(ifadeler) if _kalem_kilidi_mi(ifade)]
    pay_okumasi = [
        i
        for i, ifade in enumerate(ifadeler)
        if ifade.startswith("SELECT") and "FROM boq_item_section_allocations" in ifade
    ]
    assert len(kilit) == 1, f"kalem kilidi TEK sorgu değil: {[ifadeler[i] for i in kilit]}"
    assert "ORDER BY boq_items.id" in ifadeler[kilit[0]], (
        f"kilit sırası deterministik değil (ORDER BY id yok) — deadlock riski: {ifadeler[kilit[0]]}"
    )
    assert pay_okumasi, f"mevcut paylar hiç okunmadı: {ifadeler}"
    assert kilit[0] < pay_okumasi[0], (
        f"mevcut paylar KİLİTTEN ÖNCE okunmuş — TOCTOU penceresi açık: {ifadeler}"
    )


async def test_POZITIF_KONTROL_kilitsiz_lock_items_kotayi_asar(
    kurulum: _Kurulum, monkeypatch: pytest.MonkeyPatch
) -> None:
    """POZİTİF KONTROL — ana bekçinin KÖR OLMADIĞININ kanıtı.

    `lock_items` aynı sorgunun `with_for_update` OLMAYAN sürümüyle değiştirilir.
    Aynı senaryoda ikinci istek HİÇBİR kilitte beklemez (yolda kalemi serileştiren
    başka/alakasız bir kilit YOK), birincinin commit edilmemiş 60'ını görmez ve
    o da 60 yazar: Σ 120 > 100. Yani test 1'in iddiaları kilit kalkınca KIRMIZIdır.
    """

    async def _kilitsiz_lock_items(
        session: AsyncSession, item_ids: Iterable[uuid.UUID]
    ) -> dict[uuid.UUID, BoqItem]:
        ids = sorted(set(item_ids))
        result = await session.execute(
            select(BoqItem)
            .where(BoqItem.id.in_(ids))
            .order_by(BoqItem.id)
            .execution_options(populate_existing=True)
        )
        return {item.id: item for item in result.scalars().all()}

    monkeypatch.setattr(repository, "lock_items", _kilitsiz_lock_items)

    sonuc = await _ayni_kalem_farkli_bolum_yarisi(kurulum)

    assert sonuc.gozlemci is not None and "BEKLEMEDEN bitti" in str(sonuc.gozlemci), (
        f"kilitsiz sürümde ikinci istek yine bekledi — başka bir kilit maskeliyor: "
        f"{sonuc.bekleyen!r} / {sonuc.gozlemci!r}"
    )
    assert sonuc.ikinci_once_bitti
    assert (sonuc.birinci, sonuc.ikinci) == ("ok", "ok")

    paylar = await _paylar(kurulum)
    assert _toplam(paylar[kurulum.item_a]) == 2 * YARIM_USTU
    assert _toplam(paylar[kurulum.item_a]) > KOTA
