"""Önizleme ve silme servisi (SIL-B1). İkisi AYNI çözücüyü (`core.silme.cozucu.agac_coz`) kullanır.

`kayitlar` ithali yan etkidir: tür ve kanca kayıt defterlerini doldurur.
"""

import uuid

from sqlalchemy import inspect, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import Base
from app.core.errors import NotFoundError
from app.core.silme import turler
from app.core.silme.cozucu import PkDemeti, SilmeAgaci, agac_coz, mali_sayisi, ornekler
from app.core.silme.etiketler import tablo_bilgisi
from app.core.silme.hatalar import (
    FINANCIAL_PENDING_DETAIL,
    PREVIEW_REQUIRED_DETAIL,
    PREVIEW_STALE_DETAIL,
    DeleteFinancialPendingError,
    DeletePreviewRequiredError,
    DeletePreviewStaleError,
)
from app.core.silme.turler import KokBilgisi, SilmeTuru
from app.core.silme.yurutucu import agaci_sil
from app.modules.audit import messages
from app.modules.silme import kayitlar  # noqa: F401  (tür + kanca kaydı: yan etki)
from app.modules.silme.schemas import (
    DeleteDetachedGroup,
    DeleteKind,
    DeletePreviewGroup,
    DeletePreviewResponse,
    DeleteRelation,
)

_METADATA = Base.metadata


async def _kok_yukle(
    session: AsyncSession, tur: SilmeTuru, record_id: uuid.UUID, *, kilitle: bool
) -> KokBilgisi:
    """Kökü okur (silmede `FOR UPDATE` ile kilitler); yoksa 404. Bilgi silmeden ÖNCE okunur."""
    tablo = _METADATA.tables[tur.tablo]
    sorgu = select(tablo.c.id).where(tablo.c.id == record_id)
    if kilitle:
        sorgu = sorgu.with_for_update()
    if (await session.execute(sorgu)).first() is None:
        raise NotFoundError(tur.bulunamadi)
    bilgi = await tur.kok_oku(session, record_id)
    if bilgi is None:
        raise NotFoundError(tur.bulunamadi)
    return bilgi


async def _mali_var_mi(session: AsyncSession, agac: SilmeAgaci) -> bool:
    """Ağaçta MALİ satır var mı? KÖK de dahildir (`agac.kayitlar` kökü içerir)."""
    for tablo, idler in agac.kayitlar.items():
        if await mali_sayisi(session, _METADATA, tablo, idler):
            return True
    return False


async def _gruplar(session: AsyncSession, agac: SilmeAgaci) -> list[DeletePreviewGroup]:
    gruplar: list[DeletePreviewGroup] = []
    for tablo, idler in agac.bagimlilar().items():
        bilgi = tablo_bilgisi(tablo)
        gruplar.append(
            DeletePreviewGroup(
                table=tablo,
                label=bilgi.etiket,
                count=len(idler),
                relation=DeleteRelation(agac.iliski.get(tablo, "cascade")),
                is_financial=bool(await mali_sayisi(session, _METADATA, tablo, idler)),
                samples=await ornekler(session, _METADATA, tablo, idler),
            )
        )
    return sorted(gruplar, key=lambda g: (-g.count, g.label))


def _kopacaklar(agac: SilmeAgaci) -> list[DeleteDetachedGroup]:
    liste = [
        DeleteDetachedGroup(table=t, label=tablo_bilgisi(t).etiket, count=len(idler))
        for t, idler in agac.kopacak.items()
    ]
    return sorted(liste, key=lambda g: (-g.count, g.label))


async def _agac(
    session: AsyncSession, tur: SilmeTuru, record_id: uuid.UUID, *, kilitle: bool = False
) -> SilmeAgaci:
    kok: PkDemeti = (record_id,)
    return await agac_coz(session, _METADATA, tur.tablo, kok, kilitle=kilitle)


async def onizle(
    session: AsyncSession, kind: DeleteKind, record_id: uuid.UUID
) -> DeletePreviewResponse:
    """Yalnız okur. Silme anında çözülecek ağacın AYNISINI sayar."""
    tur = turler.tur_getir(kind.value)
    kok = await _kok_yukle(session, tur, record_id, kilitle=False)
    agac = await _agac(session, tur, record_id)
    gruplar = await _gruplar(session, agac)
    return DeletePreviewResponse(
        kind=kind,
        id=record_id,
        kind_label=tur.etiket,
        label=kok.ad,
        dependent_count=agac.bagimli_sayisi(),
        groups=gruplar,
        detached=_kopacaklar(agac),
        preview_token=agac.karma(),
    )


def _silinenleri_oturumdan_birak(session: AsyncSession, agac: SilmeAgaci) -> None:
    """Core DELETE ORM kimlik haritasını bilmez: silinen satırların nesneleri oturumdan çıkarılır
    (aksi hâlde aynı oturumdaki sonraki okuma, silinmiş satırın bayat nesnesini görürdü)."""
    for nesne in list(session.identity_map.values()):
        tablo = getattr(nesne, "__table__", None)
        if tablo is None or tablo.name not in agac.kayitlar:
            continue
        pk = tuple(inspect(nesne).mapper.primary_key_from_instance(nesne))
        if pk in agac.kayitlar[tablo.name] and nesne in session:  # expunge zincirleme olabilir
            session.expunge(nesne)


def _ozet_parcalari(gruplar: list[DeletePreviewGroup]) -> list[str]:
    return [f"{g.label} {g.count}" for g in gruplar]


async def sil(
    session: AsyncSession, kind: DeleteKind | str, record_id: uuid.UUID, preview_token: str | None
) -> str:
    """Kökü ve bağlı ağacını TEK işlemde siler; DENETİM METNİNİ döner (satır yazmak çağırana ait).

    Denetim sırası (CEO eki, testle kilitli): belirteç YOK → 428; belirteç BAYAT → 409
    `preview_stale`; ağaçta mali kayıt VAR → 409 `financial_pending`. Üçünde de HİÇBİR ŞEY silinmez.
    """
    anahtar = kind.value if isinstance(kind, DeleteKind) else kind
    if not preview_token:
        raise DeletePreviewRequiredError(PREVIEW_REQUIRED_DETAIL)
    tur = turler.tur_getir(anahtar)
    kok = await _kok_yukle(session, tur, record_id, kilitle=True)
    # 🔴 YARIŞ: ağaçtaki HER satır seviye seviye `FOR UPDATE` ile kilitlenir ve karma KİLİTLİ
    # ağaçtan hesaplanır. Kilitli satıra alt satır eklemek (INSERT'in `FOR KEY SHARE`i) kilitle
    # çakışıp BEKLER; böylece karma doğrulandıktan sonra hiçbir derinliğe önizlenmemiş satır girmez
    # (DB CASCADE'in sessizce silmesi ve denetim sayısının yanlış çıkması engellenir).
    # SERIALIZABLE yerine kilit seçildi: serileştirme hatası keyfi satırda patlar ve yeniden
    # deneme ister; kilit yarışı deterministik biçimde BEKLETİR, yalnız silinen ağaç kilitlenir.
    # Sınır: FK OLMAYAN bağlar (fiş/onay zinciri kancaları) alt satır eklemeyi engellemez; ağaçtaki
    # mevcut satırları kilitlenir, yeni kanca satırı ise bir sonraki karmada görünür.
    agac = await _agac(session, tur, record_id, kilitle=True)
    if agac.karma() != preview_token:
        raise DeletePreviewStaleError(PREVIEW_STALE_DETAIL)
    if await _mali_var_mi(session, agac):
        raise DeleteFinancialPendingError(FINANCIAL_PENDING_DETAIL)
    gruplar = await _gruplar(session, agac)
    sayi = agac.bagimli_sayisi()
    kopan = _kopacaklar(agac)
    try:
        # SAVEPOINT: kısıt ihlali oturumu kullanılamaz bırakmasın (çağıran 409'a çevirir).
        async with session.begin_nested():
            await agaci_sil(session, _METADATA, agac)
    except IntegrityError as hata:
        # Kilitli ağaçta olmaması gereken bir bağ (kancasız FK dışı ya da yarış) → eski önizleme.
        raise DeletePreviewStaleError(PREVIEW_STALE_DETAIL) from hata
    _silinenleri_oturumdan_birak(session, agac)
    return messages.deleted_with_dependents(
        kok.denetim_metni,
        sayi,
        _ozet_parcalari(gruplar),
        [f"{g.label} {g.count}" for g in kopan],
    )
