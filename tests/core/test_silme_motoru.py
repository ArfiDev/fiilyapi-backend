"""SIL-B1 — `core/silme` motoru: FK grafiği, RESTRICT-önce, FK dışı kanca, döngü koruması, karma.

Gerçek şemaya DOKUNMAZ: her test kendi `MetaData`sında geçici tablolar kurar (dış transaction
testin sonunda geri alınır). Böylece motorun KURALLARI (hangi bağ ağaca girer, hangi sırayla
silinir) ürün tablolarından bağımsız kanıtlanır; ürün tabloları için bekçi/API testleri
`tests/modules/silme/` altındadır.
"""

import uuid

import pytest
from sqlalchemy import (
    Column,
    ForeignKey,
    ForeignKeyConstraint,
    MetaData,
    String,
    Table,
    insert,
    select,
)
from sqlalchemy import Uuid as SAUuid
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.silme import graf
from app.core.silme.cozucu import agac_coz
from app.core.silme.graf import FkDisiBag, kanca_kaydet
from app.core.silme.yurutucu import SilmeDongusuError, agaci_sil, silme_sirasi


def _kimlik() -> uuid.UUID:
    return uuid.uuid4()


def _dunya() -> tuple[MetaData, dict[str, Table]]:
    """kök ← cascade | restrict ← torun | setnull | bag (FK dışı, kancayla)."""
    m = MetaData()
    t: dict[str, Table] = {}
    t["kok"] = Table("sx_kok", m, Column("id", SAUuid, primary_key=True), Column("ad", String))
    t["cascade"] = Table(
        "sx_cascade",
        m,
        Column("id", SAUuid, primary_key=True),
        Column("kok_id", SAUuid, ForeignKey("sx_kok.id", ondelete="CASCADE"), nullable=False),
    )
    t["restrict"] = Table(
        "sx_restrict",
        m,
        Column("id", SAUuid, primary_key=True),
        Column("kok_id", SAUuid, ForeignKey("sx_kok.id", ondelete="RESTRICT"), nullable=False),
    )
    t["torun"] = Table(
        "sx_torun",
        m,
        Column("id", SAUuid, primary_key=True),
        Column(
            "restrict_id", SAUuid, ForeignKey("sx_restrict.id", ondelete="RESTRICT"), nullable=False
        ),
    )
    t["setnull"] = Table(
        "sx_setnull",
        m,
        Column("id", SAUuid, primary_key=True),
        Column("kok_id", SAUuid, ForeignKey("sx_kok.id", ondelete="SET NULL"), nullable=True),
    )
    # FK OLMAYAN bağ: `belge_id` bir kök kimliğidir ama kısıt yoktur (çok biçimli referans).
    t["bag"] = Table(
        "sx_bag",
        m,
        Column("id", SAUuid, primary_key=True),
        Column("tur", String, nullable=False),
        Column("belge_id", SAUuid, nullable=False),
    )
    return m, t


@pytest.fixture
def temiz_kancalar():
    """Testin eklediği kancalar global kayıt defterinde KALMASIN."""
    onceki = list(graf._KANCALAR)
    yield
    graf._KANCALAR[:] = onceki


async def _kur(db_session: AsyncSession, m: MetaData) -> None:
    await db_session.run_sync(lambda s: m.create_all(s.connection()))


async def _ekle(db_session: AsyncSession, tablo: Table, **deger) -> uuid.UUID:
    kimlik = deger.pop("id", None) or _kimlik()
    await db_session.execute(insert(tablo).values(id=kimlik, **deger))
    return kimlik


async def _sayim(db_session: AsyncSession, tablo: Table) -> int:
    return len((await db_session.execute(select(tablo.c.id))).all())


def _bag_kancasi() -> FkDisiBag:
    return FkDisiBag(
        ad="sx_bag.belge",
        ust_tablo="sx_kok",
        alt_tablo="sx_bag",
        kosul=lambda alt, ust: (alt.c.tur == "kok") & (alt.c.belge_id == ust.c.id),
    )


async def _dolu_dunya(db_session, temiz_kancalar):
    m, t = _dunya()
    await _kur(db_session, m)
    kanca_kaydet(_bag_kancasi())
    kok = await _ekle(db_session, t["kok"], ad="K")
    diger = await _ekle(db_session, t["kok"], ad="DİĞER")
    kimlikler = {
        "cascade": await _ekle(db_session, t["cascade"], kok_id=kok),
        "restrict": await _ekle(db_session, t["restrict"], kok_id=kok),
        "setnull": await _ekle(db_session, t["setnull"], kok_id=kok),
        "bag": await _ekle(db_session, t["bag"], tur="kok", belge_id=kok),
        # BAŞKA kökün satırları ve başka TÜRDEN bağ: ağaca GİRMEMELİ
        "diger_cascade": await _ekle(db_session, t["cascade"], kok_id=diger),
        "diger_bag": await _ekle(db_session, t["bag"], tur="baska_tur", belge_id=kok),
    }
    kimlikler["torun"] = await _ekle(db_session, t["torun"], restrict_id=kimlikler["restrict"])
    return m, t, kok, diger, kimlikler


async def test_agac_cascade_restrict_torun_ve_kanca_girer_setnull_kopar(
    db_session, temiz_kancalar
) -> None:
    m, t, kok, _diger, k = await _dolu_dunya(db_session, temiz_kancalar)

    agac = await agac_coz(db_session, m, "sx_kok", (kok,))

    assert agac.kayitlar["sx_cascade"] == {(k["cascade"],)}
    assert agac.kayitlar["sx_restrict"] == {(k["restrict"],)}
    assert agac.kayitlar["sx_torun"] == {(k["torun"],)}  # RESTRICT'in RESTRICT torunu da girer
    assert agac.kayitlar["sx_bag"] == {(k["bag"],)}  # yalnız `tur=kok` olan kanca satırı
    assert agac.iliski["sx_cascade"] == "cascade"
    assert agac.iliski["sx_restrict"] == "restrict"
    assert agac.iliski["sx_bag"] == "linked"
    assert "sx_setnull" not in agac.kayitlar
    assert agac.kopacak == {"sx_setnull": {(k["setnull"],)}}
    assert agac.bagimli_sayisi() == 4  # cascade + restrict + torun + bag (kök hariç)


async def test_silme_restrict_cocugu_once_siler_ve_yalniz_agaci_siler(
    db_session, temiz_kancalar
) -> None:
    m, t, kok, diger, k = await _dolu_dunya(db_session, temiz_kancalar)
    agac = await agac_coz(db_session, m, "sx_kok", (kok,))

    silinen = await agaci_sil(db_session, m, agac)

    assert silinen == 5  # kök dahil
    assert await _sayim(db_session, t["kok"]) == 1  # yalnız `diger` kaldı
    assert await _sayim(db_session, t["restrict"]) == 0
    assert await _sayim(db_session, t["torun"]) == 0
    # BAŞKA kökün ve başka türden bağın satırları dokunulmadı
    kalan_cascade = (await db_session.execute(select(t["cascade"].c.id))).scalars().all()
    assert kalan_cascade == [k["diger_cascade"]]
    assert await _sayim(db_session, t["bag"]) == 1
    # SET NULL satırı SİLİNMEDİ, bağı koptu
    setnull = (await db_session.execute(select(t["setnull"].c.id, t["setnull"].c.kok_id))).one()
    assert setnull == (k["setnull"], None)


async def test_onizleme_agaci_ile_silinen_birebir_ayni(db_session, temiz_kancalar) -> None:
    """Önizleme ve silme AYNI fonksiyonu çağırır: çözülen ağaç silinen kümedir."""
    m, t, kok, _diger, _k = await _dolu_dunya(db_session, temiz_kancalar)
    onizleme = await agac_coz(db_session, m, "sx_kok", (kok,))
    once = {ad: await _sayim(db_session, tablo) for ad, tablo in t.items()}

    await agaci_sil(db_session, m, await agac_coz(db_session, m, "sx_kok", (kok,)))

    sonra = {ad: await _sayim(db_session, tablo) for ad, tablo in t.items()}
    silinen = {t[ad].name: once[ad] - sonra[ad] for ad in t if once[ad] != sonra[ad]}
    beklenen = {tablo: len(idler) for tablo, idler in onizleme.kayitlar.items()}
    assert silinen == beklenen


async def test_karma_baglanti_eklenince_ve_kopacak_degisince_degisir(
    db_session, temiz_kancalar
) -> None:
    m, t, kok, _diger, _k = await _dolu_dunya(db_session, temiz_kancalar)
    ilk = (await agac_coz(db_session, m, "sx_kok", (kok,))).karma()
    assert (await agac_coz(db_session, m, "sx_kok", (kok,))).karma() == ilk  # kararlı

    await _ekle(db_session, t["cascade"], kok_id=kok)
    ikinci = (await agac_coz(db_session, m, "sx_kok", (kok,))).karma()
    assert ikinci != ilk

    await _ekle(db_session, t["setnull"], kok_id=kok)  # silinmeyecek ama bağı kopacak
    assert (await agac_coz(db_session, m, "sx_kok", (kok,))).karma() != ikinci


async def test_kayit_dongusu_sonsuz_donguye_girmez(db_session) -> None:
    """A→B ve B→A birbirine CASCADE ile bağlı kayıtlar: ziyaret kümesi döngüyü keser."""
    m = MetaData()
    dugum = Table(
        "sx_dugum",
        m,
        Column("id", SAUuid, primary_key=True),
        Column("ust_id", SAUuid, ForeignKey("sx_dugum.id", ondelete="CASCADE"), nullable=True),
    )
    await _kur(db_session, m)
    a, b = _kimlik(), _kimlik()
    await _ekle(db_session, dugum, id=a, ust_id=None)
    await _ekle(db_session, dugum, id=b, ust_id=a)
    await db_session.execute(dugum.update().where(dugum.c.id == a).values(ust_id=b))

    agac = await agac_coz(db_session, m, "sx_dugum", (a,))

    assert agac.kayitlar["sx_dugum"] == {(a,), (b,)}
    assert await agaci_sil(db_session, m, agac) == 2
    assert await _sayim(db_session, dugum) == 0


async def test_bilesik_pk_ve_bilesik_fk_calisir(db_session) -> None:
    m = MetaData()
    ust = Table(
        "sx_ust",
        m,
        Column("a", SAUuid, primary_key=True),
        Column("b", String, primary_key=True),
    )
    alt = Table(
        "sx_alt",
        m,
        Column("id", SAUuid, primary_key=True),
        Column("a", SAUuid, nullable=False),
        Column("b", String, nullable=False),
        ForeignKeyConstraint(["a", "b"], ["sx_ust.a", "sx_ust.b"], ondelete="RESTRICT"),
    )
    await _kur(db_session, m)
    a = _kimlik()
    await db_session.execute(insert(ust).values(a=a, b="x"))
    await db_session.execute(insert(ust).values(a=a, b="y"))
    alt_id = await _ekle(db_session, alt, a=a, b="x")

    agac = await agac_coz(db_session, m, "sx_ust", (a, "x"))

    assert agac.kayitlar["sx_alt"] == {(alt_id,)}
    assert await agaci_sil(db_session, m, agac) == 2
    assert await _sayim(db_session, alt) == 0
    assert len((await db_session.execute(select(ust.c.b))).all()) == 1  # ("y") kaldı


def test_tablo_dongusu_silme_sirasini_reddeder() -> None:
    """İki tablo birbirine RESTRICT/CASCADE ile bağlıysa güvenli sıra YOKTUR: açık hata."""
    m = MetaData()
    Table(
        "sx_x",
        m,
        Column("id", SAUuid, primary_key=True),
        Column("y_id", SAUuid, ForeignKey("sx_y.id", ondelete="RESTRICT")),
    )
    Table(
        "sx_y",
        m,
        Column("id", SAUuid, primary_key=True),
        Column("x_id", SAUuid, ForeignKey("sx_x.id", ondelete="RESTRICT")),
    )
    with pytest.raises(SilmeDongusuError):
        silme_sirasi(m, {"sx_x", "sx_y"})


def test_silme_sirasi_alt_tablo_once_gelir() -> None:
    m, _ = _dunya()
    sira = silme_sirasi(m, {"sx_kok", "sx_restrict", "sx_torun", "sx_cascade"})
    assert sira.index("sx_torun") < sira.index("sx_restrict") < sira.index("sx_kok")
    assert sira.index("sx_cascade") < sira.index("sx_kok")


async def test_32767_parametre_tavanini_asan_cocuk_sayisi_calisir(db_session) -> None:
    """Kimlik kümeleri `= ANY(dizi)` ile TEK parametre gider: `IN (...)`in 32767 tavanı yok."""
    m, t = _dunya()
    await _kur(db_session, m)
    kok = await _ekle(db_session, t["kok"], ad="K")
    cocuklar = [{"id": _kimlik(), "kok_id": kok} for _ in range(40_000)]
    await db_session.execute(insert(t["cascade"]), cocuklar)

    agac = await agac_coz(db_session, m, "sx_kok", (kok,), kilitle=True)

    assert len(agac.kayitlar["sx_cascade"]) == 40_000
    assert await agaci_sil(db_session, m, agac) == 40_001
    assert await _sayim(db_session, t["cascade"]) == 0


async def test_kilitli_cozum_ayni_agaci_ve_ayni_karmayi_verir(db_session, temiz_kancalar) -> None:
    m, _t, kok, _diger, _k = await _dolu_dunya(db_session, temiz_kancalar)

    kilitsiz = await agac_coz(db_session, m, "sx_kok", (kok,))
    kilitli = await agac_coz(db_session, m, "sx_kok", (kok,), kilitle=True)

    assert kilitli.kayitlar == kilitsiz.kayitlar
    assert kilitli.karma() == kilitsiz.karma()
