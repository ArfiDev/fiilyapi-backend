"""Silme ağacı çözücüsü (SIL-B1, IZN-PLAN §4): ÖNİZLEME ve SİLME AYNI fonksiyonu çağırır.

`agac_coz` kökten başlayıp `graf.Kenar`lar boyunca iner:

* `cascade` / `restrict` / `linked` kenarlarındaki alt satırlar ağaca GİRER (silinecekler);
* `detach` kenarlarındaki alt satırlar ağaca girmez, `kopacak` listesine yazılır;
* ziyaret edilen satır bir daha işlenmez (döngü koruması: self-FK ve karşılıklı bağlar).

Kayıtlar `(tablo, pk_demeti)` ile tanımlanır; bileşik PK'lı tablolar (`ev_day_codes`) da çalışır.
"""

import hashlib
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import MetaData, Table, any_, bindparam, func, select, tuple_
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.silme.etiketler import tablo_bilgisi
from app.core.silme.graf import ILISKI_GUCU, Kenar, tum_kenarlar

PkDemeti = tuple[Any, ...]

#: Sorgu başına en çok kaç kayıt kimliği (asyncpg parametre tavanı 32767).
PARCA_BOYU = 1000
ORNEK_SAYISI = 5


@dataclass
class SilmeAgaci:
    kok_tablo: str
    kok_pk: PkDemeti
    #: Ağaçtaki TÜM satırlar (kök dahil), tablo başına.
    kayitlar: dict[str, set[PkDemeti]] = field(default_factory=dict)
    #: Tablonun kökle EN GÜÇLÜ ilişkisi: restrict > linked > cascade.
    iliski: dict[str, str] = field(default_factory=dict)
    #: Silinmeyecek, bağı kopacak satırlar (SET NULL), tablo başına.
    kopacak: dict[str, set[PkDemeti]] = field(default_factory=dict)

    def bagimlilar(self) -> dict[str, set[PkDemeti]]:
        """Kök HARİÇ silinecek satırlar; boş tablolar düşer."""
        sonuc: dict[str, set[PkDemeti]] = {}
        for tablo, idler in self.kayitlar.items():
            kalan = idler - {self.kok_pk} if tablo == self.kok_tablo else idler
            if kalan:
                sonuc[tablo] = kalan
        return sonuc

    def bagimli_sayisi(self) -> int:
        return sum(len(v) for v in self.bagimlilar().values())

    def karma(self) -> str:
        """Ağacın parmak izi: silinecek VE bağı kopacak her satırın kimliği. 128 bit."""
        satirlar = [f"{t}|{_pk_metni(pk)}" for t, idler in self.kayitlar.items() for pk in idler]
        satirlar += [f"~{t}|{_pk_metni(pk)}" for t, idler in self.kopacak.items() for pk in idler]
        satirlar.sort()
        return hashlib.sha256("\n".join(satirlar).encode()).hexdigest()[:32]


def _pk_metni(pk: PkDemeti) -> str:
    return "/".join(str(parca) for parca in pk)


def _parcala(idler: Sequence[PkDemeti], boy: int = PARCA_BOYU) -> list[list[PkDemeti]]:
    return [list(idler[i : i + boy]) for i in range(0, len(idler), boy)]


def kolon_in(kolon, degerler: Sequence[Any]):  # type: ignore[no-untyped-def]
    """`kolon = ANY(:dizi)` — TEK bağ parametresi: `IN (...)`in 32767 parametre tavanı yoktur."""
    return kolon == any_(bindparam(None, list(degerler), type_=ARRAY(kolon.type)))


def tek_pk_mi(tablo: Table) -> bool:
    return len(list(tablo.primary_key.columns)) == 1


def pk_in(tablo: Table, idler: Sequence[PkDemeti]):  # type: ignore[no-untyped-def]
    """Tek kolonlu PK'da `= ANY(dizi)` (boyut sınırı yok), bileşik PK'da satır karşılaştırması
    (çağıran `_parcala` ile parçalar)."""
    # `list(tablo.primary_key)`: `Table` ve `Alias` (ColumnSet; self-join
    # kancaları) için çalışır.
    kolonlar = list(tablo.primary_key)
    if len(kolonlar) == 1:
        return kolon_in(kolonlar[0], [pk[0] for pk in idler])
    return tuple_(*kolonlar).in_([tuple(pk) for pk in idler])


def pk_parcalari(tablo: Table, idler: Sequence[PkDemeti]) -> list[list[PkDemeti]]:
    """Tek kolonlu PK: TEK parça (dizi parametresi). Bileşik PK: `PARCA_BOYU`luk parçalar."""
    liste = list(idler)
    return [liste] if tek_pk_mi(tablo) else _parcala(liste)


async def _alt_kimlikleri(
    session: AsyncSession,
    metadata: MetaData,
    kenar: Kenar,
    ust_idler: Sequence[PkDemeti],
    *,
    kilitle: bool = False,
) -> set[PkDemeti]:
    """`kilitle=True`: bulunan ALT satırlar `FOR UPDATE` ile kilitlenir (silme yolu).

    Üst satırlar bir önceki seviyede ZATEN kilitlidir; kilitli bir üst satıra alt satır eklemek
    (INSERT'in aldığı `FOR KEY SHARE`) bu kilitle çakışıp BEKLER. Böylece karma hesaplandıktan
    sonra ağacın hiçbir derinliğine önizlenmemiş satır girmez.
    """
    alt = metadata.tables[kenar.alt]
    ust = metadata.tables[kenar.ust]
    alt_pk = list(alt.primary_key.columns)
    bulunan: set[PkDemeti] = set()
    for parca in pk_parcalari(ust, ust_idler):
        if kenar.kisayol_kolonu is not None:
            sorgu = select(*alt_pk).where(
                kolon_in(alt.c[kenar.kisayol_kolonu], [pk[0] for pk in parca])
            )
        else:
            ust_ad = ust.alias("silme_ust") if alt is ust else ust
            sorgu = (
                select(*alt_pk)
                .select_from(alt.join(ust_ad, kenar.kosul(alt, ust_ad)))
                .where(pk_in(ust_ad, parca))
            )
        if kilitle:
            sorgu = sorgu.with_for_update(of=alt)
        for satir in await session.execute(sorgu):
            bulunan.add(tuple(satir))
    return bulunan


def _kenar_indeksi(metadata: MetaData) -> dict[str, list[Kenar]]:
    indeks: dict[str, list[Kenar]] = defaultdict(list)
    for kenar in tum_kenarlar(metadata):
        indeks[kenar.ust].append(kenar)
    return indeks


async def agac_coz(
    session: AsyncSession,
    metadata: MetaData,
    kok_tablo: str,
    kok_pk: PkDemeti,
    *,
    kilitle: bool = False,
) -> SilmeAgaci:
    """Kökün bağımlı ağacını kurar. YAZMAZ.

    `kilitle=True` (YALNIZ silme yolu): ağaca giren her satır seviye seviye `FOR UPDATE` ile
    kilitlenir ve karma KİLİTLİ ağaçtan hesaplanır. Çağıran kökü ÖNCEDEN kilitlemiş olmalıdır.
    Önizleme `kilitle=False` ile koşar (okuma, kilit almaz)."""
    agac = SilmeAgaci(kok_tablo=kok_tablo, kok_pk=kok_pk)
    agac.kayitlar[kok_tablo] = {kok_pk}
    kenarlar = _kenar_indeksi(metadata)

    sinir: dict[str, set[PkDemeti]] = {kok_tablo: {kok_pk}}
    while sinir:
        sonraki: dict[str, set[PkDemeti]] = defaultdict(set)
        for ust_tablo, idler in sinir.items():
            for kenar in kenarlar.get(ust_tablo, []):
                if kenar.iliski == "detach":
                    continue
                bulunan = await _alt_kimlikleri(
                    session, metadata, kenar, list(idler), kilitle=kilitle
                )
                yeni = bulunan - agac.kayitlar.setdefault(kenar.alt, set())
                if yeni:
                    agac.kayitlar[kenar.alt] |= yeni
                    sonraki[kenar.alt] |= yeni
                if bulunan:
                    eski = agac.iliski.get(kenar.alt)
                    if eski is None or ILISKI_GUCU[kenar.iliski] > ILISKI_GUCU[eski]:
                        agac.iliski[kenar.alt] = kenar.iliski
        sinir = dict(sonraki)

    await _kopacaklari_bul(session, metadata, agac, kenarlar)
    agac.kayitlar = {t: v for t, v in agac.kayitlar.items() if v}
    return agac


async def _kopacaklari_bul(
    session: AsyncSession,
    metadata: MetaData,
    agac: SilmeAgaci,
    kenarlar: dict[str, list[Kenar]],
) -> None:
    for ust_tablo, idler in agac.kayitlar.items():
        for kenar in kenarlar.get(ust_tablo, []):
            if kenar.iliski != "detach" or not idler:
                continue
            bulunan = await _alt_kimlikleri(session, metadata, kenar, list(idler))
            bulunan -= agac.kayitlar.get(kenar.alt, set())
            if bulunan:
                agac.kopacak.setdefault(kenar.alt, set()).update(bulunan)


async def mali_sayisi(
    session: AsyncSession, metadata: MetaData, tablo: str, idler: set[PkDemeti]
) -> int:
    """Tablonun ağaçtaki satırlarından kaçı MALİ (bkz. `etiketler` modül docstring'i)."""
    mali = tablo_bilgisi(tablo).mali
    if mali is False:
        return 0
    if mali is True:
        return len(idler)
    t = metadata.tables[tablo]
    toplam = 0
    for parca in pk_parcalari(t, sorted(idler, key=_pk_metni)):
        sayim = select(func.count()).select_from(t).where(pk_in(t, parca), mali(t))
        toplam += int((await session.execute(sayim)).scalar_one())
    return toplam


async def ornekler(
    session: AsyncSession, metadata: MetaData, tablo: str, idler: set[PkDemeti]
) -> list[str]:
    """İlk `ORNEK_SAYISI` kaydın adı: tablo başına TEK sorgu, `ORDER BY ad LIMIT 5` (doğal sıra).
    Bileşik PK'lı tabloda (örnek kolonu olan yok) yalnız ilk `PARCA_BOYU` kimlik taranır."""
    kolon_adlari = tablo_bilgisi(tablo).ornek
    if not kolon_adlari:
        return []
    t = metadata.tables[tablo]
    secilen = pk_parcalari(t, sorted(idler, key=_pk_metni))[0]
    kolonlar = [t.c[ad] for ad in kolon_adlari]
    sorgu = select(*kolonlar).where(pk_in(t, secilen)).order_by(*kolonlar).limit(ORNEK_SAYISI)
    satirlar = (await session.execute(sorgu)).all()
    return [" · ".join(str(d) for d in satir if d is not None) for satir in satirlar]
