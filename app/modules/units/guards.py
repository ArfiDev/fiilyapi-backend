"""Blok/unite yazma yolunun ORTAK korkuluklari ve Turkce hata metinleri.

Tekil uclar (`service.py`) ve toplu uclar (`batch.py`) AYNI kurallara tabidir
(spec §7.7, §7.8: "tekil POST ile AYNI kural"). Kurallar burada TEK kopya
durur; iki modul de KOPYALAMAZ, CAGIRIR — iki kopya zamanla ayrisir ve
ayrisan taraf sessiz bir veri/yetki sizintisi olur.
"""

import uuid
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import (
    DuplicateError,
    NotFoundError,
    ProjectTypeMismatchError,
    UnitValidationError,
)
from app.modules.projects.models import Project, ProjectType

# Gorunurluk suzgeci P1'DEN GELIR (spec §8): kopya bir erisim mantigi YAZILMAZ.
# Iki ayri suzgec zamanla ayrisir ve ayrisan taraf sessiz bir yetki sizintisi
# olur. Ayni desen P2 `sites/service.py:15` ve P4 `boq/service.py`'de de var.
from app.modules.projects.service import visible_projects
from app.modules.sites import repository as sites_repository
from app.modules.sites.models import Site
from app.modules.units import repository
from app.modules.units.importer import normalize_header
from app.modules.units.models import Block, Unit, UnitOwnerSide, UnitSalesStatus
from app.modules.users.models import User

# 404 GOVDESI DE AYIRT EDICI OLMAMALIDIR (P2 `sites/service.py` dersi): gorunmeyen
# proje ile var olmayan proje ayni mesaji doner, aksi hâlde elinde UUID olan
# kullanici kaydin var oldugunu ve baskasina ait oldugunu ayirt edebilirdi.
PROJECT_MISSING = "Proje bulunamadı"

# Spec §7.11 tablosundan BIREBIR alinmistir — yeniden yazilmaz. Mesaj sabitleri
# `boq/service.py` deseniyle modul duzeyindedir (ayri `errors.py` acilmaz: mevcut
# desen alan HATA SINIFLARINI `app/core/errors.py`'de, METINLERI modul icinde tutar).
BLOCK_MISSING = "Blok bulunamadı"
UNIT_MISSING = "Ünite bulunamadı"
SITE_MISSING = "Şantiye bulunamadı"
DUPLICATE_BLOCK = "Bu blok adı bu projede zaten kullanılıyor"
DUPLICATE_BLOCK_CODE = "Bu blok kodu bu projede zaten kullanılıyor"
DUPLICATE_UNIT = "Bu ünite numarası bu blokta zaten kullanılıyor"
BULK_NUMBERS_TAKEN = "Üretilecek ünite numaralarından bazıları blokta zaten var"
NO_SITE_FOR_BLOCK = "Blok tanımlamadan önce projeye şantiye eklenmelidir"
SITE_REQUIRED = "Birden fazla şantiye var, blok için şantiye seçilmelidir"
OWNER_SIDE_NOT_ALLOWED = "Ünite payı yalnızca kat karşılığı projelerde belirlenebilir"
ALLOCATION_WRONG_TYPE = "Paylaşım yalnızca kat karşılığı projelerde kaydedilebilir"
# P9 spec §4.2 (PG 190/221): hissedar secimi YALNIZ ARSA satirinda vardir;
# "Yuklenici payi"na hissedar atanmasi anlamsizdir.
SHAREHOLDER_WRONG_SIDE = "Hissedar yalnızca arsa payı ünitesine atanabilir"
# Baska projenin hissedari VAR OLMAYANLA ayni yaniti alir (IDOR-8): aksi hâlde
# elinde UUID olan kullanici kaydin var oldugunu ayirt edebilirdi.
SHAREHOLDER_MISSING = "Hissedar bulunamadı"
NET_GT_GROSS = "Net alan brüt alandan büyük olamaz"
# P8 T3 (satis spec §3): `reserved`/`sold` SATIS KAYDINDAN turer
# (`sales/service._UNIT_STATUS_BY_SALE_STATUS`). Acilis vitrininde elle
# secilirlerse unite, `unit_sales`te SATIRI OLMADAN satilmis gorunur.
OPENING_STATUS_NEEDS_SALE = (
    'Ünite açılışında satış durumu "Satıldı"/"Rezerve" seçilemez; bu durumlar satış kaydından türer'
)
# Karar 9 (spec §4.2, §8.3): kume KODDA sabittir ve `schemas.VatRate` zorlar —
# metin diger tum alan mesajlariyla birlikte BURADA durur.
INVALID_VAT_RATE = "KDV oranı yalnızca %1, %10 veya %20 olabilir"
DUPLICATE_IN_PAYLOAD = "Aynı ünite listede birden çok kez var"
# Spec §8.3 / §6.1: kismi aktarimda dosya artik "islenemedi" durumuna DUSMEZ;
# yalniz HIC gecerli satir yoksa 422 doner. `created=0` ile 200 donmek
# kullanicinin "aktarildi" sanmasina yol acardi.
IMPORT_NOTHING_TO_WRITE = "Aktarılabilecek geçerli satır yok"
# Spec §8.3 — kat sablonu (TU 96-133). Kural `UnitBulkCreate.model_validator`'da
# zorlanir; METIN diger tum alan mesajlariyla birlikte BURADA durur.
SLOT_COUNT_MISMATCH = "Kat şablonu satır sayısı kat başına daire sayısıyla eşleşmiyor"
SLOT_SEQUENCE_INVALID = "Kat şablonunda sıra numaraları geçersiz veya tekrarlı"
# Kayıt 49/50: `uq_blocks_project_name` TAM EŞİTLİKTİR (repository.get_block_by_name
# de öyle), yani "A Blok" ve "A BLOK" aynı projede birlikte yaşayabilir. İçe
# aktarma blok sözlüğünü `normalize_header(name)` ile anahtarladığı için iki
# blok TEK anahtara çöker ve çakışmayan satırlar SESSİZCE hayatta kalan bloğa
# yazılırdı (KeyError/500 kaydı 84c87ff'te kapandı, bu sessiz-yazma kaydı AÇIK
# kalmıştı). Kökten çözüm (fonksiyonel unique index) migration ister; burada
# UCUZ VE YETERLİ seçenek: çakışma varsa içe aktarmayı TÜMÜYLE reddet.
DUPLICATE_BLOCK_NORMALIZED = (
    "Projede yalnız büyük/küçük harf ya da boşlukla ayrılan iki blok var: "
    "'{a}' / '{b}' — içe aktarmadan önce blok adlarını ayrıştırın"
)


# --- Gorunurluk (spec §8) ---


async def visible_project(
    session: AsyncSession, actor: User, project_id: uuid.UUID, missing: str = PROJECT_MISSING
) -> Project:
    """Kullanici projeyi goremiyorsa 404 — 403 DEGIL: varligin kendisi sizdirilmaz."""
    visible = await visible_projects(session, actor)
    project = next((p for p in visible if p.id == project_id), None)
    if project is None:
        raise NotFoundError(missing)
    return project


async def visible_block(
    session: AsyncSession, actor: User, block_id: uuid.UUID
) -> tuple[Block, Project]:
    """Blok → proje → gorunurluk (spec §7.3).

    Gorunmeyen projenin blogu **404** doner, 403 DEGIL; ustelik var olmayan blok
    ile AYNI mesaji verir (IDOR-5/IDOR-7) — aksi hâlde elinde UUID olan kullanici
    kaydin var oldugunu ve baskasina ait oldugunu ayirt edebilirdi.
    """
    block = await repository.get_block(session, block_id)
    if block is None:
        raise NotFoundError(BLOCK_MISSING)
    project = await visible_project(session, actor, block.project_id, BLOCK_MISSING)
    return block, project


async def visible_unit(
    session: AsyncSession, actor: User, unit_id: uuid.UUID
) -> tuple[Unit, Project]:
    """Unite → proje → gorunurluk (spec §7.6). `visible_block` ile ayni gerekce:

    gorunmeyen projenin unitesi 404 doner, 403 DEGIL, ve var olmayan unite ile
    AYNI mesaji verir (IDOR-4/IDOR-7).
    """
    unit = await repository.get_unit(session, unit_id)
    if unit is None:
        raise NotFoundError(UNIT_MISSING)
    project = await visible_project(session, actor, unit.project_id, UNIT_MISSING)
    return unit, project


async def block_in_project(session: AsyncSession, project: Project, block_id: uuid.UUID) -> Block:
    """Spec §7.5 / IDOR-9: govdedeki `block_id` baska projenin blogu olabilir.

    Proje sinirini asan blok **404** doner (422 degil): blogun varligi da
    gizlidir — kullanici o projeyi hic goremiyor olabilir.
    """
    block = await repository.get_block(session, block_id)
    if block is None or block.project_id != project.id:
        raise NotFoundError(BLOCK_MISSING)
    return block


# --- Santiye cozumu (spec §4.5) ---


async def resolve_site(
    session: AsyncSession, project_id: uuid.UUID, site_id: uuid.UUID | None
) -> Site:
    """Spec §4.5 tablosunun BES satiri da burada karsilanir.

    | santiye sayisi | `site_id` | davranis |
    |---|---|---|
    | 1 | yok | otomatik atanir — mockup'ta secici YOK (KY 38 / KK 39) |
    | 1 | var | dogrulanir; projeye ait degilse 404 |
    | 0 | — | 422 |
    | >=2 | yok | 422 — otomatik atama yanlis veri uretirdi |
    | >=2 | var | dogrulanir |

    Sifir santiye kontrolu `site_id` kontrolunden ONCE gelir: spec tablosunun 3.
    satiri `site_id` sutununu "—" (fark etmez) olarak isaretler ve o durumda
    "once santiye ekleyin" mesaji kullaniciya yol gosterir.
    """
    sites = await sites_repository.list_sites_for_project(session, project_id)
    if not sites:
        raise UnitValidationError(NO_SITE_FOR_BLOCK)
    if site_id is not None:
        site = next((s for s in sites if s.id == site_id), None)
        if site is None:
            raise NotFoundError(SITE_MISSING)
        return site
    if len(sites) > 1:
        raise UnitValidationError(SITE_REQUIRED)
    return sites[0]


# --- Benzersizlik (spec §4.3) ---


async def ensure_block_name_unique(
    session: AsyncSession,
    project_id: uuid.UUID,
    name: str,
    exclude_block_id: uuid.UUID | None = None,
) -> None:
    """`uq_blocks_project_name` — acik SELECT ile ONDEN (spec §4.3, P4 deseni)."""
    if await repository.get_block_by_name(session, project_id, name, exclude_block_id) is not None:
        raise DuplicateError(DUPLICATE_BLOCK)


def ensure_no_normalized_block_collision(blocks: list[Block]) -> None:
    """Kayıt 49/50: içe aktarma blok sözlüğü `normalize_header(name)` ile anahtarlanır.

    `uq_blocks_project_name` TAM EŞİTLİK olduğu için "A Blok" ve "A BLOK" aynı
    projede birlikte yaşayabilir; ikisi normalizasyonda TEK anahtara çöker ve
    çakışmayan satırlar sessizce hayatta kalan bloğa yazılırdı (kaydı gölgelenen
    blok içe aktarma yoluyla ERİŞİLEMEZ hâle gelirdi). Fonksiyonel unique index
    (migration gerektirir) yerine UCUZ VE YETERLİ çözüm: çakışma varsa dosyanın
    o bloğa hiç değinmediği durumlar dâhil, içe aktarma TÜMÜYLE reddedilir —
    sessiz seçim yerine kullanıcı adları ayrıştırıp yeniden dener.
    """
    seen: dict[str, str] = {}
    for block in blocks:
        key = normalize_header(block.name)
        if key in seen and seen[key] != block.name:
            raise UnitValidationError(DUPLICATE_BLOCK_NORMALIZED.format(a=seen[key], b=block.name))
        seen[key] = block.name


async def ensure_block_code_unique(
    session: AsyncSession,
    project_id: uuid.UUID,
    code: str,
    exclude_block_id: uuid.UUID | None = None,
) -> None:
    """`uq_blocks_project_code` — acik SELECT ile ONDEN (spec §3.2).

    Kullanici kodu ELLE girerse aynen kabul edilir (BE 71 alani serbest
    yazilabilir); yalniz benzersizlik dogrulanir → cakisma **409**.
    """
    if await repository.get_block_by_code(session, project_id, code, exclude_block_id) is not None:
        raise DuplicateError(DUPLICATE_BLOCK_CODE)


async def ensure_unit_no_unique(
    session: AsyncSession,
    block_id: uuid.UUID,
    unit_no: str,
    exclude_unit_id: uuid.UUID | None = None,
) -> None:
    """`uq_units_block_no` — acik SELECT ile ONDEN (spec §4.3). A Blok "1" ile
    B Blok "1" ayni anda vardir (SY 76/106), bu yuzden kapsam bloktur."""
    if await repository.get_unit_by_no(session, block_id, unit_no, exclude_unit_id) is not None:
        raise DuplicateError(DUPLICATE_UNIT)


# --- Alan kurallari (spec §3.3, §4.3) ---


def ensure_owner_side_allowed(project: Project, owner_side: UnitOwnerSide | None) -> None:
    """Spec §3.3: `owner_side` YALNIZ `kat_karsiligi` projede dolu olabilir.

    DB `CHECK` ile zorlanamaz (`project_type` baska tabloda), bu yuzden tek yazma
    yolunda servis korkulugudur (P4 `BoqGroupSiteMismatchError` deseni). NULL her
    tipte serbesttir: paylasim noterden SONRA girilir (KKP 78, spec §5.3).
    """
    if owner_side is not None and project.project_type is not ProjectType.kat_karsiligi:
        raise ProjectTypeMismatchError(OWNER_SIDE_NOT_ALLOWED)


def ensure_opening_sales_status(sales_status: UnitSalesStatus | None) -> None:
    """Satis spec §3: unitenin vitrin durumu SATIS KAYDINDAN turetilir.

    `UnitUpdate`ten alan cikarilirken (schemas.py "elle giris kilitlenir") bu
    gerekce YALNIZ PATCH'e uygulanmisti; `UnitCreate` ikinci bir yazici olarak
    kalmisti ve `sales_status="sold"` govdesi uniteyi `unit_sales`te SATIRI
    OLMADAN satilmis gosteriyordu. Sema/DB ile zorlanamaz (kural baska tablonun
    varligina bakar), bu yuzden `ensure_net_le_gross` gibi servis korkulugudur.

    `closed` ("Satisa Kapali") kumenin DISINDADIR: satis kaydindan TUREMEZ
    (haritada karsiligi yoktur) ve stok disi olmasi zaten kastedilendir.
    """
    if sales_status in (UnitSalesStatus.reserved, UnitSalesStatus.sold):
        raise UnitValidationError(OPENING_STATUS_NEEDS_SALE)


def ensure_net_le_gross(gross: Decimal | None, net: Decimal | None) -> None:
    """`ck_units_net_le_gross` DB'de de var; buradaki kontrol IntegrityError'in
    anlamsiz "Veri butunlugu hatasi" 409'una dusmemek icindir (spec §4.3, FDS 59)."""
    if gross is not None and net is not None and net > gross:
        raise UnitValidationError(NET_GT_GROSS)
