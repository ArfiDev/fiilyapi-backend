"""Silme kapsamının PROJE görünürlüğü (SIL-B2): ağaç kökün projesinin DIŞINA taşabilir.

Şantiye silmesi zincirleme uzar: satır → onaylı hakediş başlığı → fatura → ödeme → ortak çek →
çekin diğer ödemeleri (başka projenin faturasında). Önizleme bunu `other_projects` ile gösterir.

Satırın projesi: tablonun kendi `project_id` kolonu; yoksa `etiketler.PROJE_YOLU` (türev tablolar:
ödeme → fatura); o da yoksa `site_id` → `sites.project_id`. Projesi belirlenemeyen tablolar
(muhasebe fişi, onay zinciri…) SAYILMAZ: fişin kendi proje kolonu yoktur.
"""

import uuid
from collections import Counter
from dataclasses import dataclass

from sqlalchemy import MetaData, Select, Table, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.silme.cozucu import PkDemeti, SilmeAgaci, pk_in, pk_parcalari
from app.core.silme.etiketler import PROJE_YOLU


@dataclass(frozen=True)
class DigerProje:
    project_id: uuid.UUID
    name: str
    count: int


def _proje_sorgusu(metadata: MetaData, tablo: Table) -> Select | None:
    """Tablonun satırlarını proje başına sayan sorgu iskeleti (`WHERE` çağırana ait); projesiz
    tabloda `None`."""
    if "project_id" in tablo.c:
        kolon = tablo.c.project_id
        return select(kolon, func.count()).select_from(tablo).group_by(kolon)
    yol = PROJE_YOLU.get(tablo.name)
    if yol is None and "site_id" in tablo.c:
        yol = ("site_id", "sites")
    if yol is None:
        return None
    fk_kolonu, ust_ad = yol
    ust = metadata.tables[ust_ad]
    kolon = ust.c.project_id
    return (
        select(kolon, func.count())
        .select_from(tablo.join(ust, tablo.c[fk_kolonu] == ust.c.id))
        .group_by(kolon)
    )


async def proje_dagilimi(
    session: AsyncSession, metadata: MetaData, tablo: str, idler: set[PkDemeti]
) -> Counter[uuid.UUID]:
    """`tablo`nun verilen satırlarının proje başına sayısı (projesiz satırlar düşer)."""
    t = metadata.tables[tablo]
    iskelet = _proje_sorgusu(metadata, t)
    sonuc: Counter[uuid.UUID] = Counter()
    if iskelet is None or not idler:
        return sonuc
    for parca in pk_parcalari(t, sorted(idler, key=str)):
        sorgu = iskelet.where(pk_in(t, parca))
        for proje_id, adet in await session.execute(sorgu):
            if proje_id is not None:
                sonuc[proje_id] += int(adet)
    return sonuc


async def diger_projeler(
    session: AsyncSession, metadata: MetaData, agac: SilmeAgaci
) -> list[DigerProje]:
    """Ağaçtan KÖKÜN PROJESİ DIŞINDAKİ projelerde silinecek kayıtlar (adet azalan, sonra ad).

    Kökün projesi `proje_dagilimi(kök)`: tek projeye düşer. Belirlenemezse (kök fiş…) her proje
    "diğer" sayılır: kullanıcıya fazla göstermek eksik göstermekten güvenlidir (fail-closed)."""
    kok = await proje_dagilimi(session, metadata, agac.kok_tablo, {agac.kok_pk})
    kok_projesi = next(iter(kok), None)
    toplam: Counter[uuid.UUID] = Counter()
    for tablo, idler in agac.kayitlar.items():
        toplam.update(await proje_dagilimi(session, metadata, tablo, idler))
    toplam.pop(kok_projesi, None)  # type: ignore[arg-type]
    if not toplam:
        return []
    projeler = metadata.tables["projects"]
    adlar = dict(
        (
            await session.execute(
                select(projeler.c.id, projeler.c.name).where(projeler.c.id.in_(list(toplam)))
            )
        ).all()
    )
    sonuc = [DigerProje(pid, str(adlar.get(pid, "")), adet) for pid, adet in toplam.items()]
    return sorted(sonuc, key=lambda p: (-p.count, p.name))
