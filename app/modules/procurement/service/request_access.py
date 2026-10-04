"""Talebin UC KAPISI: kapsam (404) · govde referanslari (404) · durum (409)
ve silme YETKISI (403).

Yazma ve okuma yollari bu dosyayi ORTAK kullanir; kurallarin ikinci bir kopyasi
yoktur. Ayri durmasinin sebebi tam olarak budur: `request_writes` ile
`request_reads` birbirini ithal etmeden ayni kapiyi gecer.

ST §4b kanonu burada isler: *gorunmez/yok VARLIK referansi = 404 · bicim/kural
ihlali = 422.* Var OLMAYAN kimlik ile GORUNMEYEN kimlik AYNI cumleyi alir.
"""

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.access import is_system_admin
from app.core.errors import ConflictError, NotFoundError
from app.modules.procurement import guards, repository
from app.modules.procurement.models import PurchaseRequest, PurchaseRequestStatus
from app.modules.procurement.schemas import PurchaseRequestLineCreate
from app.modules.procurement.service.core import _visible_project_ids
from app.modules.sites import repository as sites_repository
from app.modules.users.models import User


async def visible_request(
    session: AsyncSession, actor: User, request_ref: uuid.UUID | str
) -> PurchaseRequest:
    """Tekil erisimin TEK kapisi — okuma da yazma da buradan gecer.

    Projesi gorunur kumede degilse **404** doner ve govde var OLMAYAN
    kimliginkiyle BIREBIR AYNIDIR — 403 verilseydi elinde kimlik olan kullanici
    kaydin var oldugunu ogrenirdi.
    """
    request = await repository.get_request(session, request_ref)
    if request is None or request.project_id not in await _visible_project_ids(session, actor):
        raise NotFoundError(guards.REQUEST_MISSING)
    return request


async def visible_request_locked(
    session: AsyncSession, actor: User, request_id: uuid.UUID
) -> PurchaseRequest:
    """Kapsam suzgeci + `SELECT … FOR UPDATE` — DURUM GECISLERININ giris kapisi.

    🔴 OK-1A T3: onay zinciri talebin satirini kilitli gormek zorundadir ve
    KILIT SIRASI sabittir (**evrak -> zincir**). Okuma uclari kilit ALMAZ —
    `visible_request` orada kalir; gereksiz satir kilidi listeleri ve detay
    okumalarini yazma islemlerinin arkasinda bekletirdi.

    Kapsam karari (404) kilitten ONCE verilir: gorunmeyen bir kaydin satiri
    bosuna kilitlenmez ve govde var OLMAYAN kimliginkiyle BIREBIR AYNIDIR.
    """
    request = await visible_request(session, actor, request_id)
    locked = await repository.get_request_locked(session, request.id)
    if locked is None:
        # Yarista silinmis olabilir — var olmayan kayitla AYNI 404.
        raise NotFoundError(guards.REQUEST_MISSING)
    return locked


async def _assert_scope(
    session: AsyncSession,
    actor: User,
    project_id: uuid.UUID,
    site_id: uuid.UUID | None,
    section_id: uuid.UUID | None,
) -> None:
    """Govdedeki UC varlik referansi: proje · santiye · bolum. Hepsi **404**.

    ST §4b kanonu: *gorunmez/yok VARLIK referansi = 404 · bicim/kural ihlali =
    422.* Var OLMAYAN kimlik ile GORUNMEYEN kimlik AYNI cumleyi alir.

    Zincir SIKIDIR: santiye talebin PROJESINE, bolum de secilen SANTIYEYE ait
    olmalidir. Santiyesiz bolum secimi de 404'tur — bolum santiyenin ic
    kirilimidir (P2 karari) ve tek basina anlamli degildir. Gevsek birakilsaydi
    talep, projesiyle ilgisi olmayan bir bolume baglanabilir ve raporlar
    sessizce yanlis kirilirdi.
    """
    if project_id not in await _visible_project_ids(session, actor):
        raise NotFoundError(guards.REQUEST_PROJECT_INVALID)

    if site_id is not None:
        site = await sites_repository.get_site(session, site_id)
        if site is None or site.project_id != project_id:
            raise NotFoundError(guards.REQUEST_SITE_INVALID)

    if section_id is not None:
        if site_id is None:
            raise NotFoundError(guards.REQUEST_SECTION_INVALID)
        if await sites_repository.get_section_in_site(session, site_id, section_id) is None:
            raise NotFoundError(guards.REQUEST_SECTION_INVALID)


async def _assert_stock_items_exist(
    session: AsyncSession, lines: list[PurchaseRequestLineCreate]
) -> None:
    """Satirlarin TAMAMI TEK sorguda ve YAZIMDAN ONCE dogrulanir (ST deseni).

    Atomikligin tasiyicisi budur: bozuk bir satir yuzunden ne baslik ne satir
    yazilir. Kod **404**tur — satir ICINDE durmasi bunu degistirmez, referans
    yine bir VARLIGADIR (ST T4-artcisi).
    """
    item_ids = [line.stock_item_id for line in lines if line.stock_item_id is not None]
    if not item_ids:
        return
    eksik = set(item_ids) - await repository.existing_stock_item_ids(session, item_ids)
    if eksik:
        raise NotFoundError(guards.REQUEST_STOCK_ITEM_INVALID)


def _assert_draft(request: PurchaseRequest) -> None:
    """Taslak DISINDA duzenleme/silme **409**dur (spec §4).

    404 (yok) ya da 403 (yetki) DEGIL: kullanicinin yetkisi VARDIR, engelleyen
    sey kaydin DURUMUDUR. Kural ayni zamanda ₺500K esiginin en kisa atlatma
    yolunu da kapatir: dusuk tutarla onaylatip sonra kalem sismek IMKANSIZDIR.
    """
    if request.status is not PurchaseRequestStatus.draft:
        raise ConflictError(guards.REQUEST_NOT_DRAFT)


def can_delete_request(actor: User) -> bool:
    """Yanıttaki `can_delete` bayrağı ile SİLME UCU AYNI kuraldan beslenir (SIL-B1, K4): silme
    YALNIZ Sistem Yöneticisi'nindir; "talebi açan kendi taslağını siler" istisnası KALDIRILDI.
    Ekran düğmeyi gösterip sonra 403 yemesin."""
    return is_system_admin(actor)
