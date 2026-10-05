"""Belge arşivi kapsam kararları + klasör iş kuralları (spec §2, §3).

İKİ KATMANLI koruma (`site_planning/service.py` deseninin birebiri): `documents`
izni router'da YETKİYİ verir, bu modül `projects.service.visible_projects` ile
KAPSAMI belirler. Görünmeyen projedeki GERÇEK klasör ile var OLMAYAN kimlik
AYIRT EDİLEMEZ 404 döner.

## Depolama soyutlaması

Bu dosyada somut DB gerçeklemesinin ADI GEÇMEZ ve GEÇMEMELİDİR: içerik işlemleri
`StorageBackend` arayüzü üzerinden yapılır, örnek `deps.get_storage_backend`ten
gelir. Kural bir testle korunur (`test_servis_somut_backendi_import_etmez`) —
R2/S3 geçişinin "tek sınıf" olması bu kurala bağlıdır.

İçerik yardımcıları (`store_/open_/delete_document_content`) T2'de KLASÖR
uçlarınca kullanılmaz; T3'ün yükleme/indirme/silme akışlarının tek giriş
kapısıdır ve soyutlamanın servis katmanındaki yüzünü şimdiden sabitler.
"""

import uuid
from collections.abc import AsyncIterator
from typing import NamedTuple

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.access import AccessLevel
from app.core.errors import (
    DocumentValidationError,
    DuplicateError,
    NotFoundError,
    RelatedRecordsExistError,
)
from app.core.page_gate import gate_flags
from app.modules.audit import messages
from app.modules.documents import files, guards, repository
from app.modules.documents.models import Document, DocumentFolder
from app.modules.documents.schemas import DocumentFolderCreate, DocumentUpdate
from app.modules.documents.storage import StorageBackend
from app.modules.projects.models import Project
from app.modules.projects.service import visible_projects
from app.modules.sites import repository as sites_repository
from app.modules.sites.models import Site
from app.modules.users.models import User

PERMISSION_MODULE = "documents"

#: IZN-B3: belge görünürlüğü O PROJEDEKİ rolün belge sayfalarını Görmesine bağlıdır; çiftler AÇIKÇA
#: verilir (kapısız yollar — örn. personel kendi izin talebine belge bağlar — üyelikle yetinemez).
_BELGE_GORUR = gate_flags(PERMISSION_MODULE, AccessLevel.view)
"""Spec §7 S2 (ONAYLI): arşivin kendi izin modülü (20., grup MALI).

`contracts`/`sales` satırlarından bilinçli olarak ayrışır — arşiv gizli veri
değil ORTAK hafızadır, hiçbir rol `none` değildir. Okuma `view`, klasör açma/
adlandırma `full`, silme `admin` kapısındadır (`full` silmeyi KAPSAMAZ).
"""


class FolderContext(NamedTuple):
    """Kapsam süzgecinden geçmiş klasör + projesi + (varsa) şantiyesi."""

    folder: DocumentFolder
    project: Project
    site: Site | None


class DocumentContext(NamedTuple):
    """Kapsam süzgecinden geçmiş belge + projesi + (varsa) şantiyesi."""

    document: Document
    project: Project
    site: Site | None


# --- Kapsam süzgeçleri (IDOR) ---


async def visible_project(session: AsyncSession, actor: User, project_id: uuid.UUID) -> Project:
    """Görünmeyen proje ile var olmayan proje AYNI 404 gövdesini döner.

    Metin `sites` modülünün TEK cümlesidir (kopya üretilmez): iki modül aynı
    kayıt için farklı cümle dönerse fark, kaydın varlığını sızdırır.
    """
    visible = await visible_projects(session, actor, pairs=_BELGE_GORUR)
    project = next((p for p in visible if p.id == project_id), None)
    if project is None:
        raise NotFoundError(guards.PROJECT_MISSING)
    return project


async def visible_folder(session: AsyncSession, actor: User, folder_id: uuid.UUID) -> FolderContext:
    """Klasör → proje (→ şantiye). Görünmeyen kapsamdaki klasör de 404'tür.

    Proje 404'ü klasör 404'üne ÇEVRİLİR: aktör klasör kimliğini sormuştur,
    "proje bulunamadı" cevabı klasörün BAŞKA bir projede var olduğunu ele verirdi.
    """
    folder = await repository.get_folder(session, folder_id)
    if folder is None:
        raise NotFoundError(guards.FOLDER_MISSING)
    visible = await visible_projects(session, actor, pairs=_BELGE_GORUR)
    project = next((p for p in visible if p.id == folder.project_id), None)
    if project is None:
        raise NotFoundError(guards.FOLDER_MISSING)
    site = (
        None if folder.site_id is None else await sites_repository.get_site(session, folder.site_id)
    )
    return FolderContext(folder=folder, project=project, site=site)


# --- Klasör okuma ---


async def list_folders(
    session: AsyncSession, actor: User, project_id: uuid.UUID, site_id: uuid.UUID | None
) -> list[DocumentFolder]:
    """KARAR (T2, spec §3 netleştirmesi): `site_id` bir SÜZGEÇTİR, "hepsi" DEĞİL.

    * parametre YOKSA → yalnız PROJE DÜZEYİ klasörler (`site_id IS NULL`)
    * `site_id=<id>` → YALNIZ o şantiyenin klasörleri

    Gerekçe: E12 kökü "proje/şantiye" ikilisidir, yani ekran her an TEK bir kökün
    içindedir. Parametresiz çağrıya "proje düzeyi + tüm şantiyeler" döndürseydik
    gövdeye bakan istemci hangi kökte olduğunu ayırt edemez, `site_id`si dolu ve
    boş klasörler aynı listede karışırdı; ayrıca çok şantiyeli bir projede kök
    ekranı, hiç göstermeyeceği yüzlerce klasörü çekerdi.

    `site_id`nin bu projeye ait olduğu AYRICA doğrulanmaz: yabancı bir şantiye
    kimliği zaten BOŞ liste döner ve hiçbir veri sızdırmaz — 422 dönmek, kimliğin
    var olduğunu ele verirdi.
    """
    await visible_project(session, actor, project_id)
    return await repository.list_folders(session, project_id, site_id)


# --- Klasör yazma ---


async def _assert_site_in_project(
    session: AsyncSession, project: Project, site_id: uuid.UUID | None
) -> Site | None:
    """`site_id` bu projenin şantiyesi mi? Değilse 422 (404 DEĞİL — `guards`)."""
    if site_id is None:
        return None
    site = await sites_repository.get_site(session, site_id)
    if site is None or site.project_id != project.id:
        raise DocumentValidationError(guards.SITE_NOT_IN_PROJECT)
    return site


async def _assert_folder_in_scope(
    session: AsyncSession,
    project: Project,
    site_id: uuid.UUID | None,
    folder_id: uuid.UUID | None,
    *,
    message: str,
) -> DocumentFolder | None:
    """Hedef klasör AYNI proje VE AYNI şantiye düzeyinde mi? Değilse 422.

    Şantiye eşitliği `site_id` üzerinden yapılır ve NULL da bir DEĞERDİR: proje
    düzeyi bir klasör, şantiye kapsamlı bir kaydın (alt klasör ya da belge)
    kabı OLAMAZ.

    Doğrulamadan sonra klasör PAYLAŞIMLI kilitlenir — T2'nin silme yolundaki
    `FOR UPDATE` ile çakışarak "boş klasörü sil" ile "içine kayıt yaz" yarışını
    kapatır. Kilitsiz bırakılsaydı yüklenen belge, silinmiş klasörün içine
    yazılır ve `folder_id` SET NULL ile sessizce köke düşerdi.

    Alt klasör (`parent_id`) ve belge (`folder_id`) yolları AYNI kuralı
    paylaşır; ayrılan tek şey kullanıcıya dönen CÜMLEDİR, o da parametredir.
    """
    if folder_id is None:
        return None
    folder = await repository.get_folder(session, folder_id)
    if folder is None or folder.project_id != project.id or folder.site_id != site_id:
        raise DocumentValidationError(message)
    await repository.lock_folder_shared(session, folder_id)
    return folder


async def _assert_parent_in_scope(
    session: AsyncSession,
    project: Project,
    site_id: uuid.UUID | None,
    parent_id: uuid.UUID | None,
) -> None:
    """Klasör ağacı dalı — ortak korkuluğu KENDİ cümlesiyle çağırır."""
    await _assert_folder_in_scope(
        session, project, site_id, parent_id, message=guards.PARENT_SCOPE_MISMATCH
    )


async def _assert_name_free(
    session: AsyncSession,
    project_id: uuid.UUID,
    site_id: uuid.UUID | None,
    parent_id: uuid.UUID | None,
    name: str,
    *,
    exclude_id: uuid.UUID | None = None,
) -> None:
    """Kapsam içinde ad tekilliği — UYGULAMA KATMANINDA.

    ⚠️ Bu kontrol "ihtiyaten" değil ZORUNLUDUR: `uq_document_folder_scope_name`
    Postgres'in `NULLS DISTINCT` semantiği yüzünden `site_id` VEYA `parent_id`den
    biri NULL olduğu ANDA fiilen çalışmaz (T1 bulgusu, T2'de ölçüldü). Yani DB
    yalnız şantiye kapsamlı ALT klasörleri korur; kalan üç dalda (proje düzeyi
    kök, proje düzeyi alt, şantiye kök) tek savunma BU FONKSİYONDUR.

    DB kısıtı korunan dalda İKİNCİ KATMANDIR: kontrol ile INSERT arasında başka
    bir istek aynı adı yazarsa `IntegrityError` → 409 eşlemesi devreye girer
    (testle dondurulmuştur). Korunmayan dallarda o yarış çift kayıtla
    sonuçlanabilir — bilinen ve kabul edilen sınır; kapatmak kısmi tekil indeks
    açan bir migration gerektirir (T1'de kapsam dışı bırakıldı).
    """
    mevcut = await repository.find_folder_by_name(
        session, project_id, site_id, parent_id, name, exclude_id=exclude_id
    )
    if mevcut is not None:
        raise DuplicateError(guards.DUPLICATE_FOLDER_NAME)


async def create_folder(
    session: AsyncSession, actor: User, project_id: uuid.UUID, data: DocumentFolderCreate
) -> tuple[DocumentFolder, str]:
    """Yeni klasör + denetim metni.

    Sıra sabittir: kapsam (404) → alan doğrulamaları (422) → ad tekilliği (409).
    Önce 409 bakılsaydı, yabancı bir `site_id` gönderen kullanıcı o kapsamda
    hangi adların KULLANILDIĞINI öğrenebilirdi.
    """
    project = await visible_project(session, actor, project_id)
    name = data.name.strip()
    site = await _assert_site_in_project(session, project, data.site_id)
    await _assert_parent_in_scope(session, project, data.site_id, data.parent_id)
    await _assert_name_free(session, project.id, data.site_id, data.parent_id, name)

    folder = DocumentFolder(
        project_id=project.id,
        site_id=data.site_id,
        parent_id=data.parent_id,
        name=name,
        created_by=actor.id,
    )
    session.add(folder)
    await session.flush()
    detail = messages.document_folder_created(
        project.name, None if site is None else site.name, folder.name
    )
    return folder, detail


async def rename_folder(
    session: AsyncSession, context: FolderContext, name: str
) -> tuple[DocumentFolder, str]:
    """Yalnız ad değişir (spec §3) — kapsam alanlarına DOKUNULMAZ.

    Eski ad denetim metni için değişiklikten ÖNCE okunur (`role_renamed` dersi):
    sonra okunsaydı günlükte yeni ad iki kez çıkar ve neyin değiştiği kaybolurdu.
    """
    folder = context.folder
    yeni = name.strip()
    await _assert_name_free(
        session,
        folder.project_id,
        folder.site_id,
        folder.parent_id,
        yeni,
        exclude_id=folder.id,
    )
    eski = folder.name
    folder.name = yeni
    await session.flush()
    detail = messages.document_folder_renamed(
        context.project.name,
        None if context.site is None else context.site.name,
        eski,
        yeni,
    )
    return folder, detail


async def delete_folder(session: AsyncSession, context: FolderContext) -> str:
    """YALNIZ BOŞ klasör silinir; dolu klasör 409 (`guards` tablosu).

    CASCADE'e kayılmaz ve kayamaz: `documents.folder_id` ile
    `document_folders.parent_id` FK'lerinin İKİSİ de `SET NULL`dır — yani DB
    hiçbir şeyi engellemez, kayıtlar sessizce kapsamın köküne düşer. Bu
    fonksiyon kaldırılırsa bir klasörü silmek, içindeki 200 belgeyi kullanıcının
    haberi olmadan "klasörsüz" hâle getirir.

    YARIŞ: satır önce DIŞLAYICI kilitlenir (`FOR UPDATE`); alt klasör açan yol
    aynı satırı `FOR SHARE` ile kilitlediği için kontrol ile `DELETE` arasına
    yeni bir alt klasör giremez. İkisi de AYNI transaction içindedir (istek başına
    tek oturum), dolayısıyla kontrol ile silme atomiktir.
    ⚠️ T3 NOTU: belge yükleme ucu da klasörü `lock_folder_shared` ile
    kilitlemelidir; yoksa belge ayağında aynı yarış açık kalır.

    Dönen değer DENETİM METNİDİR ve satır yok olmadan ÖNCE kurulur (`sites`
    dersi): sonra kurulsaydı günlüğe adsız bir kayıt düşerdi. Engellenen silme
    (409) istisna attığı için denetime HİÇBİR ŞEY yazmaz — günlük gerçekleşen
    olayı kaydeder, denemeyi değil.
    """
    folder = context.folder
    await repository.lock_folder_for_update(session, folder.id)
    if await repository.folder_has_documents(session, folder.id):
        raise RelatedRecordsExistError(guards.FOLDER_HAS_DOCUMENTS)
    if await repository.folder_has_children(session, folder.id):
        raise RelatedRecordsExistError(guards.FOLDER_HAS_CHILDREN)
    detail = messages.document_folder_deleted(
        context.project.name,
        None if context.site is None else context.site.name,
        folder.name,
    )
    await session.delete(folder)
    await session.flush()
    return detail


# --- Belge kapsamı (IDOR) ---


async def visible_document(
    session: AsyncSession, actor: User, document_id: uuid.UUID
) -> DocumentContext:
    """Belge → proje (→ şantiye). Görünmeyen kapsamdaki belge de 404'tür.

    Proje 404'ü belge 404'üne ÇEVRİLİR: aktör belge kimliğini sormuştur,
    "proje bulunamadı" cevabı belgenin BAŞKA bir projede var olduğunu ele
    verirdi (`visible_folder` deseninin birebiri).
    """
    document = await repository.get_document(session, document_id)
    if document is None:
        raise NotFoundError(guards.DOCUMENT_MISSING)
    visible = await visible_projects(session, actor, pairs=_BELGE_GORUR)
    project = next((p for p in visible if p.id == document.project_id), None)
    if project is None:
        raise NotFoundError(guards.DOCUMENT_MISSING)
    site = (
        None
        if document.site_id is None
        else await sites_repository.get_site(session, document.site_id)
    )
    return DocumentContext(document=document, project=project, site=site)


# --- Belge okuma ---


async def list_documents(
    session: AsyncSession,
    actor: User,
    project_id: uuid.UUID,
    site_id: uuid.UUID | None,
    folder_id: uuid.UUID | None,
    q: str | None,
    limit: int | None,
) -> list[Document]:
    """Künye listesi (spec §3). Üç süzgeç kararı BURADA dondurulur:

    1. **`project_id` ZORUNLUDUR** (uç imzasında da zorunlu): her iki mockup da
       her an TEK bir kökün içindedir (E12 sol ağacının kökleri projelerdir;
       SB bir şantiye sekmesidir). Projeler arası birleşik arşiv ekranı YOKTUR;
       opsiyonel olsaydı uç, görünen tüm projelerin tüm belgelerini sınırsız
       döndüren ve hiçbir ekranın istemediği bir sorguya dönerdi.
    2. **`site_id` T2'nin klasör listesiyle AYNI semantiktedir** — verilmezse
       proje düzeyi (`IS NULL`), verilirse yalnız o şantiye. İki uç aynı kökü
       farklı yorumlasaydı ekran klasörleri bir kapsamdan, belgeleri başka
       kapsamdan çizerdi.
    3. **`folder_id` verilmezse klasör süzgeci YOKTUR** — kapsamın tamamı döner.
       Mockup gereği: SB'nin kökü "Tüm Belgeler"dir ve listedeki satırlar farklı
       klasörlere aittir ("Günlük Raporlar", "Fotoğraflar", "İş Güvenliği").

    `site_id`nin bu projeye ait olduğu AYRICA doğrulanmaz (T2 gerekçesi):
    yabancı kimlik zaten boş liste döner, 422 dönmek varlığını ele verirdi.
    """
    await visible_project(session, actor, project_id)
    return await repository.list_documents(session, project_id, site_id, folder_id, q, limit)


# --- Belge yazma ---


async def upload_document(
    session: AsyncSession,
    storage: StorageBackend,
    actor: User,
    *,
    project_id: uuid.UUID,
    site_id: uuid.UUID | None,
    folder_id: uuid.UUID | None,
    description: str | None,
    filename: str,
    content: bytes,
) -> tuple[Document, str]:
    """Künye + içerik — TEK transaction, ATOMİK.

    `storage.put` künye INSERT'i ile AYNI transaction içindedir ve kendi
    transaction'ını AÇMAZ (`StorageBackend` sözleşmesi). `put` patlarsa istek
    hata verir, `get_db` rollback eder ve künye de yazılmaz — "künye var, baytı
    yok" yarımlığı imkânsızdır (kanıt: `test_blob_yazilamazsa_kunye_de_yazilmaz`).
    Sıra tersine çevrilemez: `document_blobs.document_id` künyeye FK'dır, önce
    künye flush edilmelidir.

    Dosya adı ve uzantı doğrulaması ÇAĞIRANDA yapılır (`files.py`): baytlar
    okunmadan ÖNCE reddedebilmek için — yasak uzantılı 50 MB'lık bir gövdeyi
    sonuna kadar okumanın anlamı yoktur.

    `uploaded_by_name` SNAPSHOT'tır (SB:144): kullanıcı silinse ya da adı
    değişse bile arşiv kaydı ne yazıyorsa odur.
    """
    project = await visible_project(session, actor, project_id)
    site = await _assert_site_in_project(session, project, site_id)
    await _assert_folder_in_scope(
        session, project, site_id, folder_id, message=guards.FOLDER_SCOPE_MISMATCH
    )

    document = Document(
        folder_id=folder_id,
        project_id=project.id,
        site_id=site_id,
        filename=filename,
        mime_type=files.mime_for_filename(filename),
        size_bytes=len(content),
        description=description,
        uploaded_by_user_id=actor.id,
        uploaded_by_name=actor.full_name,
    )
    session.add(document)
    await session.flush()
    await storage.put(document.id, content)

    detail = messages.document_uploaded(
        project.name, None if site is None else site.name, document.filename
    )
    return document, detail


async def update_document(
    session: AsyncSession, context: DocumentContext, data: DocumentUpdate
) -> tuple[Document, str]:
    """Ad / açıklama / klasör (taşıma). Kapsam alanları DEĞİŞMEZ (`schemas`).

    Gönderilmeyen alan ile `null` gönderilen alan `model_fields_set` ile ayrılır.

    Yeni ad da beyaz listeden geçer ve `mime_type` ondan YENİDEN TÜRETİLİR:
    geçmeseydi yükleme kapısı iki adımda atlatılırdı (`rapor.pdf` yükle →
    `rapor.exe` yap); türetilmeseydi künye yanlış tip taşır ve indirme ucu onu
    olduğu gibi sunardı.

    Taşımada hedef klasörün kapsamı belgeninkiyle AYNI olmalıdır (422) ve klasör
    PAYLAŞIMLI kilitlenir — silme yarışı belge ayağında da kapalıdır.
    """
    document = context.document
    verilen = data.model_dump(exclude_unset=True)

    if "filename" in verilen:
        yeni_ad = files.normalize_filename(verilen["filename"])
        files.assert_allowed_extension(yeni_ad)
        document.filename = yeni_ad
        document.mime_type = files.mime_for_filename(yeni_ad)
    if "description" in verilen:
        document.description = verilen["description"]
    if "folder_id" in verilen:
        await _assert_folder_in_scope(
            session,
            context.project,
            document.site_id,
            verilen["folder_id"],
            message=guards.FOLDER_SCOPE_MISMATCH,
        )
        document.folder_id = verilen["folder_id"]

    await session.flush()
    detail = messages.document_updated(
        context.project.name,
        None if context.site is None else context.site.name,
        document.filename,
    )
    return document, detail


async def delete_document(
    session: AsyncSession, storage: StorageBackend, context: DocumentContext
) -> str:
    """Künye + baytlar. Denetim metni kayıt YOK OLMADAN ÖNCE kurulur.

    İçerik ÖNCE `StorageBackend.delete` ile silinir: `document_blobs`ta FK
    CASCADE zaten yetimi engeller ama o yalnız DB backend'i için geçerlidir —
    R2/S3 backend'inde künye silinip baytlar bucket'ta kalırdı. CASCADE ikinci
    katman olarak DURUYOR (ayrıca test edilir).
    """
    document = context.document
    detail = messages.document_deleted(
        context.project.name,
        None if context.site is None else context.site.name,
        document.filename,
    )
    await storage.delete(document.id)
    await session.delete(document)
    await session.flush()
    return detail


# --- İçerik (depolama soyutlamasının servis yüzü) ---


async def store_document_content(
    storage: StorageBackend, document_id: uuid.UUID, data: bytes
) -> None:
    """Baytları backend'e yazar. Somut depolama tipi BİLİNMEZ."""
    await storage.put(document_id, data)


def open_document_content(storage: StorageBackend, document_id: uuid.UUID) -> AsyncIterator[bytes]:
    """İçeriği PARÇALI okur — `StreamingResponse`in besleyicisi (spec §3).

    `async def` DEĞİL: üreteci sarmalamak (`async for … yield`) araya gereksiz
    bir katman koyar ve backend'in parça sınırlarını değiştirme ihtimali doğurur.
    """
    return storage.stream(document_id)


async def delete_document_content(storage: StorageBackend, document_id: uuid.UUID) -> None:
    await storage.delete(document_id)


async def start_document_stream(
    storage: StorageBackend, document_id: uuid.UUID
) -> AsyncIterator[bytes]:
    """İLK parçayı yanıt başlamadan ÖNCE alır, kalanı akıtan üreteci döndürür.

    Gerekçe: `StreamingResponse` gövdeyi yollamaya başladıktan SONRA yükselen bir
    hata artık 404'e çevrilemez — kullanıcı yarım bir `200` indirir ve bozukluğu
    ancak dosyayı açınca fark eder. İlk parça burada beklendiği için "künye var,
    baytı yok" durumu düzgün bir 404 olur (`test_icerigi_olmayan_kunye_404`).

    Bellek sözü BOZULMAZ: elde tutulan şey TEK parçadır (1 MB), dosyanın tamamı
    değil.
    """
    iterator = storage.stream(document_id)
    ilk = await anext(iterator, None)

    async def _zincir() -> AsyncIterator[bytes]:
        if ilk:
            yield ilk
        async for parca in iterator:
            yield parca

    return _zincir()
