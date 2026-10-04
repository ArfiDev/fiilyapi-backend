"""Silinebilir kayıt türlerinin kayıt defteri (SIL-B1). Her aile kendi türünü KAYDEDER.

`core` ürün modüllerini ithal etmez: tür tanımı (kök tablo, Türkçe ad, kök adının ve
denetim metninin nasıl kurulacağı) ilgili modülün `silme_kaydi.py` dosyasında yaşar ve
`app/modules/silme/kayitlar.py` hepsini ithal eder.
"""

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession


@dataclass(frozen=True)
class KokBilgisi:
    """Silinmeden ÖNCE okunur: satır gittikten sonra ad ve denetim metni kurulamaz."""

    ad: str
    #: Mevcut denetim metni (ör. `messages.site_deleted(...)`); bağlı kayıt özeti sonuna eklenir.
    denetim_metni: str


KokOkuyucu = Callable[[AsyncSession, uuid.UUID], Awaitable[KokBilgisi | None]]


@dataclass(frozen=True)
class SilmeTuru:
    anahtar: str  # URL parçası / DeleteKind üyesi
    tablo: str  # kök tablo
    etiket: str  # "Şantiye"
    bulunamadi: str  # 404 mesajı ("Şantiye bulunamadı")
    kok_oku: KokOkuyucu  # kayıt yoksa None


_TURLER: dict[str, SilmeTuru] = {}


def tur_kaydet(tur: SilmeTuru) -> None:
    _TURLER[tur.anahtar] = tur


def tur_getir(anahtar: str) -> SilmeTuru:
    return _TURLER[anahtar]


def kayitli_turler() -> dict[str, SilmeTuru]:
    return dict(_TURLER)
