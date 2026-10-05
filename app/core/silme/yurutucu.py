"""Silme yürütücüsü (SIL-B1): ağacı YAPRAKTAN köke, TABLO TABLO siler.

DB'nin CASCADE'ine GÜVENİLMEZ: ağaçtaki her satır açıkça silinir ve sıra, FK'nin yönüne göre
"alt tablo önce, üst tablo sonra"dır. Böylece:

* `RESTRICT` / `NO ACTION` çocuklar (ör. blokta ünite, ünitede satış) önce gider ve DB
  kısıtı hiçbir noktada tetiklenmez;
* önizlemede sayılan satırlarla silinen satırlar BİREBİR aynıdır (CASCADE'in sessizce
  sildiği, önizlemede görünmeyen satır kalmaz).

Sıra, ağaçtaki tablolar arasındaki FK + kanca kenarlarının topolojik sıralamasıdır (`detach`
ve `sirayi_etkilemez` kenarları sıralamayı etkilemez: SET NULL satırı silmeyi engellemez).
Tablolar arası DÖNGÜ varsa `SilmeDongusuError` fırlatılır; bugünkü şemada döngü
YOKTUR (bekçi testi çakar).
"""

from collections import defaultdict

from sqlalchemy import MetaData, delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.silme.cozucu import PARCA_BOYU, PkDemeti, SilmeAgaci, _parcala, pk_in
from app.core.silme.graf import tum_kenarlar


class SilmeDongusuError(RuntimeError):
    """Ağaçtaki tablolar arasında FK döngüsü var: güvenli silme sırası yok."""


def silme_sirasi(metadata: MetaData, tablolar: set[str]) -> list[str]:
    """Alt tablo önce gelecek şekilde topolojik sıra (Kahn). Kendine bağlı kenarlar yok sayılır."""
    bekleyen: dict[str, set[str]] = {t: set() for t in tablolar}
    for kenar in tum_kenarlar(metadata):
        if kenar.iliski == "detach" or kenar.ust == kenar.alt or kenar.sirayi_etkilemez:
            continue
        if kenar.ust in tablolar and kenar.alt in tablolar:
            # üst, alt silinene kadar bekler
            bekleyen[kenar.ust].add(kenar.alt)
    sira: list[str] = []
    hazir = sorted(t for t, b in bekleyen.items() if not b)
    ters: dict[str, set[str]] = defaultdict(set)
    for ust, altlar in bekleyen.items():
        for alt in altlar:
            ters[alt].add(ust)
    while hazir:
        tablo = hazir.pop(0)
        sira.append(tablo)
        for ust in sorted(ters.get(tablo, ())):
            bekleyen[ust].discard(tablo)
            if not bekleyen[ust] and ust not in sira and ust not in hazir:
                hazir.append(ust)
    if len(sira) != len(tablolar):
        kalan = sorted(set(tablolar) - set(sira))
        raise SilmeDongusuError(f"Silme sırası kurulamadı, FK döngüsü: {kalan}")
    return sira


def _kendine_restrict_var_mi(metadata: MetaData, tablo: str) -> bool:
    return any(
        k.ust == tablo and k.alt == tablo and k.iliski in ("restrict", "linked")
        for k in tum_kenarlar(metadata)
    )


async def agaci_sil(session: AsyncSession, metadata: MetaData, agac: SilmeAgaci) -> int:
    """Ağacın tüm satırlarını (kök dahil) siler; silinen satır sayısını döner. Commit ETMEZ."""
    toplam = 0
    for tablo in silme_sirasi(metadata, set(agac.kayitlar)):
        t = metadata.tables[tablo]
        idler: list[PkDemeti] = sorted(agac.kayitlar[tablo], key=lambda pk: "/".join(map(str, pk)))
        # Kendine RESTRICT bağlı tablo (fiş ← storno) TEK ifadede silinir: parça sınırı
        # ana satırı stornosundan önce silip kısıtı tetiklemesin.
        boy = len(idler) if _kendine_restrict_var_mi(metadata, tablo) else PARCA_BOYU
        for parca in _parcala(idler, boy):
            sonuc = await session.execute(delete(t).where(pk_in(t, parca)))
            toplam += sonuc.rowcount or 0
    return toplam
