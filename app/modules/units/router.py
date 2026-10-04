import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, Query, Request, Response, UploadFile, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import http
from app.core.access import AccessLevel
from app.core.db import DbSession
from app.core.deps import get_current_user
from app.core.errors import UnitValidationError
from app.core.openapi import COMMON_ERROR_RESPONSES
from app.core.permissions import kapsam_kapisi, require_permission, require_system_admin
from app.core.ratelimit import client_ip
from app.core.scoped_route import kapsam_rotasi, kapsamdan_oku, kapsamla_maskele
from app.modules.audit.models import AuditAction
from app.modules.audit.service import record_audit
from app.modules.silme import service as silme_service
from app.modules.silme.params import DELETE_WITH_PREVIEW_RESPONSES, PreviewTokenQuery
from app.modules.units import batch, export, guards, importer, service
from app.modules.units.export import build_units_workbook
from app.modules.units.models import UnitKind, UnitSalesStatus
from app.modules.units.schemas import (
    BlockCreate,
    BlockListResponse,
    BlockResponse,
    BlockUpdate,
    UnitAllocationRequest,
    UnitBulkCreate,
    UnitBulkPreview,
    UnitCreate,
    UnitImportResult,
    UnitImportValidation,
    UnitListResponse,
    UnitOwnerSideFilter,
    UnitResponse,
    UnitUpdate,
)
from app.modules.units.template import build_template_workbook
from app.modules.users.models import User

# `boq/router.py` ve `audit/router.py` ile AYNI sabit: uc modul de kendi
# kopyasini tutar (mevcut desen), ortak bir `core` sabiti ACILMAZ.
XLSX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

# Uclar iki ayri kok altina dagilir (P4 deseni): proje baglamli uclar
# `/projects/...`, kimligi yukari cozumleyen tekil uclar `/blocks/...` ve
# `/units/...` altindadir — bu yuzden router prefix TASIMAZ.
#
# BFF TUZAGI (frontend dilimi icin): IKI kok var, `units` VE `blocks`. Ikisi de
# `src/app/api/backend/[...path]/route.ts` ALLOWED_ROOTS listesine eklenmezse
# ilgili modul YALNIZ CANLIDA 404 verir.
# 🔴 KAPSAM MASKESI — IKI PARCA DA GEREKLI (kullanici karari 2026-09-19):
#    `route_class` donen modeli maskeler, `dependencies` aktorun kapsamini
#    kopruye yazar. Biri eksikse maske SESSIZCE `all` gorur ve hicbir sey
#    gizlemez. Cifti `tests/core/test_kapsam_baglantisi.py` cakar.
#
# 🔴 ANAHTAR `units` DEGIL `projects`: kapsam, ucu KORUYAN izin modulunden
#    okunur ve bu router (asagida) `require_permission("projects", ...)` kullanir
#    — `units` bir izin modulu DEGILDIR (spec §8). Anahtari `units` yazmak
#    matriste karsiligi olmayan bir satir sorar, `actor_scope` `all` doner ve
#    maske sessizce olurdu. Bu tam olarak 2026-09-19 denetiminin buldugu
#    kusurun ikizidir (o gun router HIC bagli degildi ve `projects=limited` olan
#    rol unite satis bedelini, maliyetini ve beklenen karini tam goruyordu).
router = APIRouter(
    tags=["units"],
    responses=COMMON_ERROR_RESPONSES,
    route_class=kapsam_rotasi("projects", kapsamdan_oku),
    dependencies=[kapsam_kapisi("projects")],
)

# Spec §8: YENI IZIN MODULU ACILMAZ — blok ve unite projenin alt kayitlaridir,
# `projects` modulunun seviyeleri kullanilir. Modul sayisi 17'de kalir.
_VIEW = require_permission("projects", AccessLevel.view)
# Yazma uclari `full` ister (spec §8): `view` yetmez (IDOR-13).
_FULL = require_permission("projects", AccessLevel.full)
# SILME uclari `require_system_admin` ile kapilidir (SIL-B1): modul seviyesi degil rol ANAHTARI.


async def _audit(
    request: Request,
    session: AsyncSession,
    user: User,
    action: AuditAction,
    detail: str,
) -> None:
    """Denetim satiri (spec §9, B5 deseni).

    Metin servis katmanindan HAZIR gelir: silme uclarinda adlar kayit yok
    olmadan once okunmak zorundadir ve router onlari sonradan hicbir sorguyla
    geri getiremez (bkz. `service.py` §9 notu). Yalniz YAZMA uclari cagirir —
    okuma uclari denetim satiri URETMEZ (P4 T7 kurali).

    `record_audit` commit etmez: satir asil islemle AYNI transaction'a girer,
    dolayisiyla reddedilen (409/422) bir istek denetim satiri da birakmaz.
    """
    await record_audit(
        session,
        action=action,
        detail=detail,
        actor_user_id=user.id,
        ip_address=client_ip(request),
    )


@router.get("/projects/{project_id}/blocks", response_model=BlockListResponse, dependencies=[_VIEW])
async def list_blocks_endpoint(
    project_id: uuid.UUID,
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
) -> BlockListResponse:
    """Spec §7.1. Blok seciciler (unite formu, toplu uretim formu) bu ucu kullanir."""
    return await service.list_blocks(session, user, project_id)


@router.get("/projects/{project_id}/units", response_model=UnitListResponse, dependencies=[_VIEW])
async def list_units_endpoint(
    project_id: uuid.UUID,
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
    block_id: Annotated[uuid.UUID | None, Query()] = None,
    site_id: Annotated[uuid.UUID | None, Query()] = None,
    kind: Annotated[UnitKind | None, Query()] = None,
    owner_side: Annotated[UnitOwnerSideFilter | None, Query()] = None,
    floor: Annotated[str | None, Query(max_length=20)] = None,
    sales_status: Annotated[UnitSalesStatus | None, Query()] = None,
) -> UnitListResponse:
    """Spec §7.4. Suzgecler YALNIZ listeyi daraltir; `totals` daima projenin
    tamamini sayar. `site_id` blok uzerinden cozulur (`units`'te `site_id` yok)."""
    return await service.list_units(
        session,
        user,
        project_id,
        block_id=block_id,
        site_id=site_id,
        kind=kind,
        owner_side=owner_side,
        floor=floor,
        sales_status=sales_status,
    )


@router.post(
    "/projects/{project_id}/blocks",
    response_model=BlockResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[_FULL],
)
async def create_block_endpoint(
    request: Request,
    project_id: uuid.UUID,
    data: BlockCreate,
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
) -> BlockResponse:
    """Spec §7.2. Tek santiyeli projede `site_id` gonderilmezse otomatik atanir
    (§4.5) — mockup'ta santiye secici yoktur (KY 38 / KK 39)."""
    block, detail = await service.create_block(session, user, project_id, data)
    await _audit(request, session, user, AuditAction.create, detail)
    return await service.block_response(session, block)


@router.patch("/blocks/{block_id}", response_model=BlockResponse, dependencies=[_FULL])
async def update_block_endpoint(
    request: Request,
    block_id: uuid.UUID,
    data: BlockUpdate,
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
) -> BlockResponse:
    """Spec §7.3. Kimlik YUKARI cozumlenir (blok → proje → gorunurluk);
    gorunmeyen projenin blogu 404 doner, 403 DEGIL."""
    block, detail = await service.update_block(session, user, block_id, data)
    await _audit(request, session, user, AuditAction.update, detail)
    return await service.block_response(session, block)


@router.post(
    "/projects/{project_id}/units",
    response_model=UnitResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[_FULL],
)
async def create_unit_endpoint(
    request: Request,
    project_id: uuid.UUID,
    data: UnitCreate,
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
) -> UnitResponse:
    """Spec §7.5. Govdedeki `block_id` bu projeye ait olmali (IDOR-9), aksi hâlde 404."""
    unit, detail = await service.create_unit(session, user, project_id, data)
    await _audit(request, session, user, AuditAction.create, detail)
    return await service.unit_response(session, unit)


@router.patch("/units/{unit_id}", response_model=UnitResponse, dependencies=[_FULL])
async def update_unit_endpoint(
    request: Request,
    unit_id: uuid.UUID,
    data: UnitUpdate,
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
) -> UnitResponse:
    """Spec §7.6. Kimlik YUKARI cozumlenir (unite → proje → gorunurluk);
    `block_id` ile ayni proje icinde tasima serbesttir."""
    unit, detail = await service.update_unit(session, user, unit_id, data)
    await _audit(request, session, user, AuditAction.update, detail)
    return await service.unit_response(session, unit)


@router.delete(
    "/units/{unit_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses=DELETE_WITH_PREVIEW_RESPONSES,
    dependencies=[require_system_admin()],
)
async def delete_unit_endpoint(
    request: Request,
    unit_id: uuid.UUID,
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
    preview_token: PreviewTokenQuery = None,
) -> None:
    """Spec §7.9 (SIL-B1 ile degisti). YALNIZ Sistem Yoneticisi; ONIZLEME ZORUNLU.

    Unitenin bagli kayitlari (satis, taksit, belge…) ile BIRLIKTE silinir. Once
    `GET /admin/silme/unit/{id}/onizleme`, onay, sonra bu uc `preview_token` ile cagrilir
    (eksikse 428, ağac degistiyse 409 `preview_stale`, mali bagli kayit varsa 409
    `financial_pending`). Denetim satirina silinen ve bagi kopan kayitlarin tam dokumu yazilir."""
    detail = await silme_service.sil(session, "unit", unit_id, preview_token)
    await _audit(request, session, user, AuditAction.delete, detail)


@router.delete(
    "/blocks/{block_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses=DELETE_WITH_PREVIEW_RESPONSES,
    dependencies=[require_system_admin()],
)
async def delete_block_endpoint(
    request: Request,
    block_id: uuid.UUID,
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
    preview_token: PreviewTokenQuery = None,
) -> None:
    """Spec §7.9 (SIL-B1 ile degisti). YALNIZ Sistem Yoneticisi; ONIZLEME ZORUNLU.

    Blok, UNITELERI ve onlarin bagli kayitlari ile BIRLIKTE silinir (eski "unitesi olan blok 409"
    korkulugu Sistem Yoneticisi icin kalkti: uniteler once, blok sonra silinir). Once
    `GET /admin/silme/block/{id}/onizleme`, onay, sonra bu uc `preview_token` ile cagrilir
    (eksikse 428, agac degistiyse 409 `preview_stale`, mali bagli kayit varsa 409
    `financial_pending`). Yetki kapisi her seyden ONCE calisir."""
    detail = await silme_service.sil(session, "block", block_id, preview_token)
    await _audit(request, session, user, AuditAction.delete, detail)


@router.post(
    "/projects/{project_id}/units/bulk",
    response_model=UnitListResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[_FULL],
)
async def bulk_create_units_endpoint(
    request: Request,
    project_id: uuid.UUID,
    data: UnitBulkCreate,
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
) -> UnitListResponse:
    """Spec §7.7. HEP-YA-HIC: uretilen numaralardan biri bile blokta varsa
    HICBIRI yazilmaz (409). Yanit guncel tam listedir — ekran tabloyu yeniden
    cizer, ikinci bir GET'e gerek kalmaz.

    Denetim: 24 unite uretilse de ISTEK BASINA TEK satir yazilir (spec §9)."""
    result, detail = await batch.bulk_create_units(session, user, project_id, data)
    await _audit(request, session, user, AuditAction.create, detail)
    return result


@router.post(
    "/projects/{project_id}/units/bulk/preview",
    response_model=UnitBulkPreview,
    dependencies=[_FULL],
)
async def preview_bulk_units_endpoint(
    project_id: uuid.UUID,
    data: UnitBulkCreate,
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
) -> UnitBulkPreview:
    """Spec §5.4 (TU 159-182). **HICBIR SEY YAZMAZ** ve **DENETIM URETMEZ**.

    `dry_run` bayragi yerine AYRI UC olmasinin uc gerekcesi (spec §5.4):
    1. Yanit sekilleri farklidir — gercek uretim `201 UnitListResponse` doner;
       onizlemede `id`'si olan unite yoktur. Tek uc `response_model`'i bir
       `Union`'a zorlar ve `gen:api` ciktisinda sessiz `undefined` sinifi dogar.
    2. Denetim ayrimi temiz kalir: "yazan uc denetim yazar" kurali bir bayraga
       BAGLI HALE GELMEZ. Bu yuzden bu fonksiyon `_audit` CAGIRMAZ ve `Request`
       parametresi bile ALMAZ — denetim yazmak icin gereken IP bu ucta yoktur.
    3. Uretim mantigi tek kopyadir: iki uc da `bulk.generate_units`'i cagirir.

    Izin `full` KALIR: onizleme yazma akisinin parcasidir ve `view` kullanicisina
    fiyat uretim kurallarini acmaz. Cakisma HATA DEGILDIR (TU 177) — satirlar
    `conflict=true` ile 200 doner; blokaj yalniz `POST …/units/bulk`'tadir (409).
    """
    return await batch.preview_bulk_units(session, user, project_id, data)


@router.patch(
    "/projects/{project_id}/units/allocation",
    response_model=UnitListResponse,
    dependencies=[_FULL],
)
async def update_allocation_endpoint(
    request: Request,
    project_id: uuid.UUID,
    data: UnitAllocationRequest,
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
) -> UnitListResponse:
    """Spec §7.10 (KKP 25). Paylar TOPLU URETIMDE atanmaz, SONRADAN bu ucla
    girilir: paylasim noterden sonra belli olur (KKP 78).

    ATOMIK: tek satir bile reddedilirse hicbiri yazilmaz. Listedeki bir unite
    BASKA projeye aitse 404 doner (IDOR-8) ve bu projenin hicbir satiri
    degismez. Yanit guncel tam listedir — ekran tabloyu yeniden cizer.

    Denetim: 42 unitelik bir kayit TEK satir yazar (spec §9) — satir basina
    gunluk, denetim gunlugunu okunamaz hâle getirirdi.
    """
    result, detail = await batch.update_allocation(session, user, project_id, data)
    await _audit(request, session, user, AuditAction.update, detail)
    return result


@router.post(
    "/projects/{project_id}/units/import/validate",
    response_model=UnitImportValidation,
    dependencies=[_FULL],
)
async def validate_import_endpoint(
    project_id: uuid.UUID,
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
    file: Annotated[UploadFile, File()],
    site_id: Annotated[uuid.UUID | None, Form()] = None,
    include_warnings: Annotated[bool, Form()] = True,
) -> UnitImportValidation:
    """Spec §6.2 (EI 92-197, "Yeniden Doğrula"). **HICBIR SEY YAZMAZ.**

    `bulk/preview` ile AYNI uc gerekceyle ayri uctur (`dry_run` bayragi DEGIL):
    1. Yanit sekli farklidir (`UnitImportValidation` != `UnitImportResult`); tek
       uc `response_model`'i bir `Union`'a zorlar ve `gen:api` ciktisinda sessiz
       `undefined` sinifi dogar.
    2. Denetim ayrimi temiz kalir: bu fonksiyon `_audit` CAGIRMAZ ve `Request`
       parametresi bile ALMAZ — denetim yazmak icin gereken IP burada YOKTUR.
    3. Kural TEK KOPYADIR: iki uc de `batch._plan_rows`'tan beslenir.

    DOSYA SAKLANMADIGI ICIN (P3 §7.8'in degismeyen siniri) "Yeniden Doğrula →
    Aktar" akisinda dosya IKI KEZ yuklenir. Tarayicida bu bedavadir: `File`
    nesnesi zaten istemcinin bellegindedir. Frontend dilimi bunu bilerek yazar.
    """
    try:
        importer.ensure_xlsx(file.filename)
        importer.ensure_size(file.size)
    except importer.ImportFileError as exc:
        raise UnitValidationError(str(exc)) from exc
    return await batch.validate_import(
        session,
        user,
        project_id,
        await file.read(),
        site_id=site_id,
        include_warnings=include_warnings,
    )


@router.post(
    "/projects/{project_id}/units/import",
    response_model=UnitImportResult,
    dependencies=[_FULL],
)
async def import_units_endpoint(
    request: Request,
    project_id: uuid.UUID,
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
    file: Annotated[UploadFile, File()],
    site_id: Annotated[uuid.UUID | None, Form()] = None,
    include_warnings: Annotated[bool, Form()] = True,
) -> UnitImportResult:
    """Spec §6.1-§6.5. BELGE SAKLAMA ALTYAPISI GEREKMEZ ve kurulmayacaktir: dosya
    bellekte okunur, uniteler yaratilir, dosya ATILIR. Diske, S3'e, veritabanina
    hicbir sey yazilmaz — P3'e sigmasinin tek sebebi budur.

    KISMI AKTARIM (P3'un hep-ya-hic karari BILEREK tersine cevrildi, spec §6.1):
    gecerli satirlar yazilir, hatalilar raporlanir. Hic gecerli satir yoksa 422 —
    `created=0` ile 200 donmek kullanicinin "aktarildi" sanmasina yol acardi.

    `site_id` (EI 61 "Hedef Şantiye", karar 3) YALNIZ yeni blok acarken kullanilir.
    `include_warnings` EI 192 kutucugudur; varsayilani mockup'taki gibi ISARETLIDIR.

    Boyut IKI KEZ olculur: once istemcinin bildirdigi `size` ile (henuz govde
    bellege alinmadan), sonra GERCEKTEN okunan `bytes` uzunluguyla
    (`parse_units_file`) — istemci basligina guvenilmez.
    """
    try:
        importer.ensure_xlsx(file.filename)
        importer.ensure_size(file.size)
    except importer.ImportFileError as exc:
        raise UnitValidationError(str(exc)) from exc
    result, detail = await batch.import_units(
        session,
        user,
        project_id,
        await file.read(),
        site_id=site_id,
        include_warnings=include_warnings,
    )
    await _audit(request, session, user, AuditAction.create, detail)
    return result


@router.get(
    "/projects/{project_id}/units/import/template",
    dependencies=[_VIEW],
    response_class=Response,
    responses={200: {"content": {XLSX_MEDIA_TYPE: {}}, "description": "Excel sablonu"}},
)
async def units_import_template_endpoint(
    project_id: uuid.UUID,
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
) -> Response:
    """Spec §6.7 (EI 37, 87 "Şablon İndir"). 12 baslikli BOS `.xlsx`.

    IZIN `view`'DIR ve bu, modulun tek "view yeter" yazma-akisi ucudur (spec
    §6.2 karari): sablon hicbir proje verisi tasimaz, `full`'a kapatmak veri
    GIRECEK kullaniciyi akisin ilk adimindan mahrum birakirdi.

    GORUNURLUK KAPISI YINE DE VARDIR (spec §12.6/I3): govde proje verisi
    tasimasa da 200/404 farki tek basina bir PROJE VARLIK ORAKULUDUR.

    Okuma ucudur — `_audit` CAGIRMAZ ve `Request` parametresi bile ALMAZ
    (P4 T7 kurali; `validate` ucuyle ayni gerekce).
    """
    project = await guards.visible_project(session, user, project_id)
    return Response(
        content=build_template_workbook().getvalue(),
        media_type=XLSX_MEDIA_TYPE,
        headers={
            "Content-Disposition": http.content_disposition(f"unite-sablonu-{project.code}.xlsx")
        },
    )


@router.get(
    "/projects/{project_id}/units/export.xlsx",
    dependencies=[_VIEW],
    response_class=Response,
    responses={200: {"content": {XLSX_MEDIA_TYPE: {}}, "description": "Excel dosyasi"}},
)
async def units_export_endpoint(
    project_id: uuid.UUID,
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
) -> Response:
    """P9 T4 (KKP 24 "Excel"): paylasim tablosunun Excel ciktisi (spec §5).

    Zarf `service.list_units` ile — LISTE UCUYLA AYNI cagridan — gelir: gorunurluk
    kapisi (gorunmeyen proje 404), satir sirasi ve tum degerler ekranla birebir
    ayni kaynaktan cikar. Ikinci bir hesap/sorgu yolu ACILMAZ (timesheet export
    emsali: bir kere kur, iki kere bas).

    SUZGEC ALMAZ (liste ucundaki `block_id`/`kind`/... parametreleri): KKP'nin
    Excel dugmesi paylasim tablosunun TAMAMINI indirir; kismi dosya, tfoot
    toplamlariyla (proje geneli) celisen bir belge uretirdi.

    Okuma ucudur — `_audit` CAGIRMAZ ve `Request` parametresi bile ALMAZ
    (P4 T7 kurali; sablon ucuyle ayni gerekce).
    """
    project = await guards.visible_project(session, user, project_id)
    units = await service.units_for_project(session, project)
    # 🔴 MASKE BURADA ELLE UYGULANIR — rota sarmalayicisi bu ucu ATLAR.
    #
    # `_sarili` yalniz `BaseModel` donuslerini maskeler; bu uc bir `Response`
    # (xlsx baytlari) doner ve sarmalayici bir dosyanin ICINE bakamaz. Elle
    # cagirmasaydik `projects = view/limited` olan rol (santiye sefi, saha
    # muhendisi, IK) ekranda `—` gordugu rayic degeri AYNI kapidan
    # (`projects:view`) dosya olarak TAM indirirdi — `boq/export`te bulunan
    # kacagin birebir ikizi (denetim 2026-09-19).
    #
    # `export.py` DEGISMEZ ve degismemelidir: `_text()` zaten `None`u "—" basar,
    # yani maskeli deger ekrandaki ile AYNI isareti uretir. Maskeyi export
    # katmanina tasimak, sunum katmanina bir yetki karari yuklerdi.
    units = kapsamla_maskele(units, "projects")
    return Response(
        content=build_units_workbook(units).getvalue(),
        media_type=XLSX_MEDIA_TYPE,
        headers={"Content-Disposition": http.content_disposition(export.filename(project.code))},
    )
