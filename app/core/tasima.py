"""IZN-B5f (madde 22, CEO kararı 22-iii) — kaydı PROJELER ARASI TAŞIYAN PATCH'ler.

Taşıma = gövdedeki `project_id` / `site_id` kaydın bugünkü projesinden (P) FARKLI bir hedef (Q)
projeye çözülmesidir (fatura, çek/senet, satınalma talebi, makine, çalışma/yakıt kaydı, kira
faturası). HF1: taşıma isteğinde bağlam çözülmez (`None`), uç kapısı yalnız ANA rolle geçer.
Bu yüzden ne kaynak ne hedef proje rolü kapıda sınanmış olur; `assert_tasima_yetkisi` ikisini de
sınar:

(c) **Kaynakta da yazma yetkisi.** P varsa kapının AYNI çiftleri P'de geçmeli
    (`page_gate.decide`, proje bağlamı = P: ekibindeyse P'deki rol, değilse ana rol). Geçmezse
    **403**, taşımasız PATCH'in kapı gövdesiyle AYNI ("Bu işlem için yetkiniz yok"; kayıt kişiye
    zaten görünür). Hedef `null` (projesiz) da bu kontrolden geçer.

(b) **Taşıma = hedefte de Düzenler.** Aynı çiftler hedef Q'da da geçmeli (kişi Q ekibindeyse Q'daki
    rol, değilse ana rol). Geçmezse **404** (hedefte yalnız Görür / Hiçbir şey olan kişiye hedefin
    varlığı sızmaz; gövde modülün kendi "proje/şantiye bulunamadı" metniyle AYNI). Sistem
    Yöneticisi `decide` içinde geçer. P == Q taşıma DEĞİLDİR.

(a) **Yanıt taşıma ÖNCESİ bağlamla maskelenir.** Yanıtın satırı artık yeni `project_id`'yi taşır;
    satır başına maske (`MaskeKumeleri.proje_basina`) o hedef projenin rolüne geçip gizliliği
    AÇARDI. Yardımcı kaynak projeyi isteğin scope'una yazar (`TASIMA_KAYNAK_KEY`); maske bağlamı
    (`mask_route.MaskeBaglami.kumeler`) bu bayrakta satır başına maskeyi kapatır ve bütün yanıtı
    kaynak projedeki rolün (P yoksa birleşimin) gizli kümesiyle maskeler.

BİLİNÇLİ KABUL (CEO): kayıt taşındıktan SONRA hedef projedeki rol hedefin tutarlarını GET'te görür.
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import HTTPException, Request, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import NotFoundError
from app.core.gate_context import PageFlag
from app.core.page_gate import decide

__all__ = ["TASIMA_KAYNAK_KEY", "assert_tasima_yetkisi"]

#: İstek scope'unda taşıma bayrağı: değer `(kaynak_proje_id | None,)` demetidir.
TASIMA_KAYNAK_KEY = "izn_b5f.tasima_kaynak"

#: Taşımasız PATCH'in kapı gövdesi (`permissions._DENIED`) ile AYNI.
_YETKISIZ = "Bu işlem için yetkiniz yok"

_HEDEF_ALANLARI = ("project_id", "site_id")


async def _hedefler(
    session: AsyncSession, data: BaseModel
) -> tuple[list[tuple[str, uuid.UUID]], bool]:
    """Gövdenin GÖNDERİLMİŞ taşıma alanlarının `(alan, proje)` çiftleri + açık `null` var mı."""
    from app.modules.projects.context import BODY_RESOLVERS  # döngüyü önler

    hedefler: list[tuple[str, uuid.UUID]] = []
    has_null = False
    for alan in _HEDEF_ALANLARI:
        if alan not in data.model_fields_set or not hasattr(data, alan):
            continue
        deger = getattr(data, alan)
        if deger is None:
            has_null = True
            continue
        proje = await BODY_RESOLVERS[alan](session, deger)
        if proje is not None:
            hedefler.append((alan, proje))
    return hedefler, has_null


async def assert_tasima_yetkisi(
    session: AsyncSession,
    request: Request,
    user: Any,
    pairs: tuple[PageFlag, ...],
    data: BaseModel,
    *,
    proje_yok: str,
    santiye_yok: str,
) -> None:
    """Gövde kaydı başka projeye taşıyorsa: kaynakta (403) ve hedefte (404) kapı çiftleri geçmeli
    ve yanıt maskesi kaynak bağlama kilitlenir. Taşıma yoksa HİÇBİR şey yapmaz."""
    if not any(alan in data.model_fields_set for alan in _HEDEF_ALANLARI):
        return
    from app.modules.projects.context import _route_path, resolve_path_project  # döngüyü önler

    _, kaynak = await resolve_path_project(session, _route_path(request), request.path_params)
    hedefler, has_null = await _hedefler(session, data)
    tasinan = [(alan, proje) for alan, proje in hedefler if proje != kaynak]
    if not tasinan and not (has_null and kaynak is not None):
        return  # P == Q ya da projesiz → projesiz: taşıma değil
    if kaynak is not None and not await decide(
        session, user, pairs, project_id=kaynak, record=False
    ):
        raise HTTPException(status.HTTP_403_FORBIDDEN, _YETKISIZ)
    for alan, hedef in tasinan:
        if not await decide(session, user, pairs, project_id=hedef, record=False):
            raise NotFoundError(proje_yok if alan == "project_id" else santiye_yok)
    request.scope[TASIMA_KAYNAK_KEY] = (kaynak,)
