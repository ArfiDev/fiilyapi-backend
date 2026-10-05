"""Silme sonrası düzeltme kancaları (SIL-B2): silinen satırın TÜRETİLMİŞ etkisini onarır.

Ağaçtaki bir satır silinince ağaç DIŞINDA kalan bir kaydın türetilmiş durumu bayatlayabilir
(ödemesi silinen faturanın `collected` damgası, ödemesine dayanan `paid` hakediş). Bu bilgi
satır gittikten sonra okunamaz; bu yüzden kanca İKİ AŞAMALIDIR:

1. `once(session, agac)` silmeden ÖNCE koşar, gerekli kimlikleri okur ve bir `sonra` döner;
2. `sonra(session)` ağaç silindikten SONRA, AYNI işlemde koşar.

Motor modül bilmez; her aile kancasını kendi `silme_kaydi.py`sinde `sonrasi_kaydet` ile kaydeder.
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.silme.cozucu import SilmeAgaci

Sonra = Callable[[AsyncSession], Awaitable[None]]
Once = Callable[[AsyncSession, SilmeAgaci], Awaitable[Sonra | None]]


@dataclass(frozen=True)
class SonrasiKancasi:
    ad: str
    tablo: str  # ağaçta bu tablonun satırı varsa koşar
    once: Once


_KANCALAR: list[SonrasiKancasi] = []


def sonrasi_kaydet(kanca: SonrasiKancasi) -> None:
    """Idempotent: aynı adlı kanca ikinci kez kaydedilmez."""
    if all(k.ad != kanca.ad for k in _KANCALAR):
        _KANCALAR.append(kanca)


def kayitli_sonrasi() -> tuple[SonrasiKancasi, ...]:
    return tuple(_KANCALAR)


async def hazirla(session: AsyncSession, agac: SilmeAgaci) -> list[Sonra]:
    """Ağaçtaki tablolar için `once` aşamasını koşar; silmeden sonra çalışacak işleri döner."""
    isler: list[Sonra] = []
    for kanca in _KANCALAR:
        if not agac.kayitlar.get(kanca.tablo):
            continue
        sonra = await kanca.once(session, agac)
        if sonra is not None:
            isler.append(sonra)
    return isler
