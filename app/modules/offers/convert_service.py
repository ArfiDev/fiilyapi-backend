"""Teklif → proje DONUSTURME servisi (TKL-B6.2, TKL-PLAN §4.2/§4.5).

TEK islem, ara commit YOK (commit'i `get_db` yapar; hata = tum islem geri alinir). Sira:

1. `lock_offer` (`FOR UPDATE`) — on kosullar KILIT ALTINDA: son revizyon `won` olmali (SO-36),
   `project_id` bos olmali (cift donusturme = 409, SO-36). Kilitsiz iki eszamanli donusturme iki
   proje dogururdu (`offers.project_id` UQ'su ikinciyi yakalamaz: ayni satirin UPDATE'i).
2. DOGRULAMA YAZMADAN ONCE, tek gecista toplu 422 (kod/grup adi tekilligi, teklif kalemi
   iliskisi, fiyat farki tutarliligi, bedel tavani); sonra varlik 404'leri (katalog, disiplin;
   her biri TEK sorgu).
3. `create_project` (taahhut; isveren = teklifin isvereni; `open_site` ise satir ici santiye).
4. `seed_contract` — gruplar + kalemler (govde sirasi).
5. `open_site` ise `distribute_in_full` — tum kalemler tam miktarla santiyeye (S-D2).
6. `contract_seed.run_seed_hooks` — adam-saat tohumu PORTU (EV bunu kaydeder; kayit yoksa bos,
   `earned_value` import EDILMEZ). Kanca hatasi YUKSELIR → islem geri alinir.
7. Teklif `project_id/converted_at/converted_by_user_id` (arsiv).

Denetim satirlari router isidir (IP orada). Servis `ConvertResult` doner.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import contract_seed
from app.core.contract_seed import ContractItemSeed, ContractSeedRequest
from app.core.errors import ConflictError, NotFoundError, OfferValidationError
from app.modules.catalog.guards import CATALOG_ITEM_MISSING, DISCIPLINE_MISSING
from app.modules.catalog.models import EvCatalogItem, EvDiscipline
from app.modules.contracts import seed_service
from app.modules.contracts.seed_service import SeedGroupInput, SeedItemInput
from app.modules.offers.convert_schemas import ConvertRequest, ConvertWarning
from app.modules.offers.locking import latest_revision, lock_offer
from app.modules.offers.models import (
    Offer,
    OfferItem,
    OfferPriceEscalation,
    OfferRevision,
    OfferRevisionStatus,
)
from app.modules.projects import repository as project_repository
from app.modules.projects import service as project_service
from app.modules.projects.models import Project, ProjectType
from app.modules.projects.schemas import (
    ProjectContractInput,
    ProjectCreate,
    ProjectSiteInput,
)
from app.modules.sites import repository as sites_repository
from app.modules.sites.models import Site
from app.modules.users.models import User

__all__ = [
    "ALREADY_CONVERTED",
    "AMOUNT_TOO_LARGE",
    "NOT_WON",
    "PROJECT_CODE_TAKEN",
    "WARN_DISCIPLINES_IGNORED",
    "ConvertResult",
    "convert_offer",
]

ALREADY_CONVERTED = "Teklif zaten dönüştürüldü"
NOT_WON = "Yalnız son revizyonu kazanılmış (won) olan teklif projeye dönüştürülebilir"
#: Elle verilen proje kodu baska projede var (409; yazmadan ONCE, hicbir sey yazilmaz).
PROJECT_CODE_TAKEN = "Bu proje kodu zaten kullanılıyor"
AMOUNT_TOO_LARGE_MESSAGE = "Kalem toplamı sözleşme bedeli sınırını aşıyor"
AMOUNT_TOO_LARGE = f"contract.amount: {AMOUNT_TOO_LARGE_MESSAGE}"
#: Santiyesiz donusturmede elle disiplin eslemesi saklanmaz (SO-32) → yanitta uyari.
WARN_DISCIPLINES_IGNORED = "group_disciplines_ignored_without_site"

_MONEY = Decimal("0.01")
#: `project_contracts.amount` = `Numeric(18, 2)`.
_AMOUNT_LIMIT = Decimal(10) ** 16


@dataclass(frozen=True, slots=True)
class ConvertResult:
    offer: Offer
    project: Project
    site: Site | None
    item_count: int
    warnings: list[ConvertWarning]


@dataclass(frozen=True, slots=True)
class _Line:
    """Govdedeki bir kalem + sunucunun cozdugu adam-saat bilgisi (port icin)."""

    unit_mhr: Decimal
    rate_is_offer: bool


def _money(value: Decimal) -> Decimal:
    return value.quantize(_MONEY, rounding=ROUND_HALF_UP)


def _item_total(body: ConvertRequest) -> Decimal:
    return sum(
        (_money(item.quantity * item.unit_price) for group in body.groups for item in group.items),
        Decimal(0),
    )


# ------------------------------------------------------------------------- dogrulama


@dataclass(frozen=True, slots=True)
class _Issue:
    """Tek govde hatasi: yapisal `loc` (FE satir vurgusu) + mesaj; `detail` metni `label: mesaj`.

    `label` bos ise `loc`un nokta/koseli yolu (`groups[0].items[3].code`) kullanilir — mevcut
    `detail` metni bu sayede AYNEN korunur (BD-4).
    """

    loc: tuple[str | int, ...]
    message: str
    label: str | None = None

    @property
    def detail(self) -> str:
        return f"{self.label or _path(self.loc)}: {self.message}"

    def as_error(self) -> dict[str, object]:
        return {"loc": list(self.loc), "message": self.message}


def _path(loc: tuple[str | int, ...]) -> str:
    out = ""
    for part in loc:
        out += f"[{part}]" if isinstance(part, int) else (f".{part}" if out else part)
    return out


def _static_errors(
    body: ConvertRequest, revision: OfferRevision, offer_items: dict[uuid.UUID, OfferItem]
) -> list[_Issue]:
    """Yazmadan ONCE, DB'ye sormadan bulunabilen TUM govde hatalari (toplu mesaj icin)."""
    errors: list[_Issue] = []
    group_names: set[str] = set()
    codes: set[str] = set()
    for g_index, group in enumerate(body.groups):
        # SO-30: BOQ grubu ADLA acilir → ayni adli iki grup tek BOQ grubuna birlesirdi
        if group.name in group_names:
            errors.append(_Issue(("groups", g_index, "name"), f"Aynı adlı grup var ({group.name})"))
        group_names.add(group.name)
        for i_index, item in enumerate(group.items):
            where = ("groups", g_index, "items", i_index)
            if item.code in codes:  # SO-29
                errors.append(_Issue((*where, "code"), f"Kalem kodu tekrar ediyor ({item.code})"))
            codes.add(item.code)
            if item.offer_item_id is None:
                continue
            source = offer_items.get(item.offer_item_id)
            loc = (*where, "offer_item_id")
            if source is None:
                errors.append(_Issue(loc, "Kalem teklifin son revizyonunda bulunamadı"))
            elif source.catalog_item_id != item.catalog_item_id:
                errors.append(_Issue(loc, "Teklif kaleminin katalog bağı gövdedekiyle uyuşmuyor"))
    for name in body.group_disciplines:
        if name not in group_names:
            errors.append(
                _Issue(
                    ("group_disciplines", name),
                    f"«{name}» adlı grup gövdede yok",
                    label="group_disciplines",
                )
            )
    errors.extend(_escalation_errors(body, revision))
    if _item_total(body) >= _AMOUNT_LIMIT and body.contract.amount is None:
        errors.append(_Issue(("contract", "amount"), AMOUNT_TOO_LARGE_MESSAGE))
    return errors


def _resolve_index_type(body: ConvertRequest, revision: OfferRevision):
    contract = body.contract
    if contract.index_type is not None:
        return contract.index_type
    if revision.price_escalation == OfferPriceEscalation.tuik:
        return revision.price_index_type
    return None


def _escalation_errors(body: ConvertRequest, revision: OfferRevision) -> list[_Issue]:
    contract = body.contract
    if not contract.has_price_escalation:
        return [
            _Issue(("contract", name), "Fiyat farkı kapalıyken verilemez")
            for name, value in (
                ("index_type", contract.index_type),
                ("base_index_value", contract.base_index_value),
            )
            if value is not None
        ]
    errors: list[_Issue] = []
    if _resolve_index_type(body, revision) is None:
        errors.append(
            _Issue(("contract", "index_type"), "Fiyat farkı açıkken endeks türü zorunludur")
        )
    if contract.base_index_value is None:
        errors.append(
            _Issue(("contract", "base_index_value"), "Fiyat farkı açıkken baz endeks zorunludur")
        )
    return errors


async def _load_catalog(
    session: AsyncSession, body: ConvertRequest
) -> dict[uuid.UUID, EvCatalogItem]:
    ids = {item.catalog_item_id for group in body.groups for item in group.items}
    rows = await session.scalars(select(EvCatalogItem).where(EvCatalogItem.id.in_(ids)))
    found = {row.id: row for row in rows}
    if found.keys() != ids:
        raise NotFoundError(CATALOG_ITEM_MISSING)
    return found


async def _assert_disciplines_exist(session: AsyncSession, body: ConvertRequest) -> None:
    ids = set(body.group_disciplines.values())
    if not ids:
        return
    found = set(await session.scalars(select(EvDiscipline.id).where(EvDiscipline.id.in_(ids))))
    if found != ids:
        raise NotFoundError(DISCIPLINE_MISSING)


# ----------------------------------------------------------------------------- kurma


def _project_input(body: ConvertRequest, offer: Offer, revision: OfferRevision) -> ProjectCreate:
    contract = body.contract
    fields: dict[str, object] = {
        "contract_no": contract.contract_no,
        "signature_date": contract.signature_date,
        "amount": contract.amount if contract.amount is not None else _item_total(body),
        "vat_pct": contract.vat_pct if contract.vat_pct is not None else revision.vat_pct,
        "has_price_escalation": contract.has_price_escalation,
        "index_type": _resolve_index_type(body, revision)
        if contract.has_price_escalation
        else None,
        "base_index_value": contract.base_index_value,
    }
    # Verilmeyen = sozlesme semasinin varsayilani (SO-39): anahtar HIC verilmez.
    for name in ("advance_pct", "retainage_pct", "late_penalty_daily"):
        value = getattr(contract, name)
        if value is not None:
            fields[name] = value
    project = body.project
    return ProjectCreate(
        code=project.code,  # None = sunucu uretir (PRJ-YYYY-NNN, danisma kilidi altinda)
        name=project.name,
        project_type=ProjectType.taahhut,
        category=project.category,
        city=project.city,
        parcel=project.parcel,
        address=project.address,
        start_date=project.start_date,
        end_date=project.end_date,
        employer_id=offer.employer_id,
        contract=ProjectContractInput(**fields),
        sites=[ProjectSiteInput(name=body.site_name or project.name)] if body.open_site else [],
    )


def _seed_inputs(body: ConvertRequest) -> list[SeedGroupInput]:
    return [
        SeedGroupInput(
            name=group.name,
            items=[
                SeedItemInput(
                    catalog_item_id=item.catalog_item_id,
                    code=item.code,
                    description=item.description,
                    unit=item.unit,
                    quantity=item.quantity,
                    unit_price=item.unit_price,
                )
                for item in group.items
            ],
        )
        for group in body.groups
    ]


def _lines(
    body: ConvertRequest,
    catalog: dict[uuid.UUID, EvCatalogItem],
    offer_items: dict[uuid.UUID, OfferItem],
) -> list[_Line]:
    """Adam-saat: teklif kaleminden (`rate_is_offer` = katalog standardindan FARKLI ise, SO-33);
    kalem teklifte yoksa katalog standardi ve `rate_is_offer=False`."""
    lines: list[_Line] = []
    for group in body.groups:
        for item in group.items:
            standard = catalog[item.catalog_item_id].standard_unit_mhr
            source = offer_items.get(item.offer_item_id) if item.offer_item_id else None
            unit_mhr = source.unit_mhr if source is not None else standard
            lines.append(_Line(unit_mhr, unit_mhr != standard))
    return lines


def _to_warnings(
    raw: Sequence[contract_seed.SeedWarning], names: dict[uuid.UUID, str]
) -> list[ConvertWarning]:
    return [
        ConvertWarning(
            code=w.code,
            message=w.message,
            group_name=names.get(w.group_id) if w.group_id is not None else None,
        )
        for w in raw
    ]


async def _offer_items(
    session: AsyncSession, revision: OfferRevision
) -> dict[uuid.UUID, OfferItem]:
    rows = await session.scalars(select(OfferItem).where(OfferItem.revision_id == revision.id))
    return {row.id: row for row in rows}


async def convert_offer(
    session: AsyncSession, actor: User, offer_id: uuid.UUID, body: ConvertRequest
) -> ConvertResult:
    offer = await lock_offer(session, offer_id)
    revision = await latest_revision(session, offer.id)
    if offer.project_id is not None:
        raise ConflictError(ALREADY_CONVERTED)
    if revision.status != OfferRevisionStatus.won:
        raise ConflictError(NOT_WON)

    offer_items = await _offer_items(session, revision)
    errors = _static_errors(body, revision, offer_items)
    if errors:
        raise OfferValidationError(
            "; ".join(issue.detail for issue in errors), [issue.as_error() for issue in errors]
        )
    catalog = await _load_catalog(session, body)
    await _assert_disciplines_exist(session, body)
    lines = _lines(body, catalog, offer_items)
    # BD-2: elle verilen kod yazmadan ONCE denetlenir (yoksa DB UQ'su jenerik 409 verirdi).
    # Eszamanli ayni kodla yazanlari DB UQ'su yakalar (jenerik 409; tum islem geri alinir).
    code = body.project.code
    if code is not None and await project_repository.project_code_exists(session, code):
        raise ConflictError(PROJECT_CODE_TAKEN)

    project = await project_service.create_project(session, _project_input(body, offer, revision))
    seeded = await seed_service.seed_contract(session, project, _seed_inputs(body))
    site: Site | None = None
    if body.open_site:
        (site, *_rest) = await sites_repository.list_sites_for_project(session, project.id)
        await seed_service.distribute_in_full(session, seeded, site.id)

    group_ids = {s.group.name: s.group.id for s in seeded.groups}
    group_names = {group_id: name for name, group_id in group_ids.items()}
    warnings: list[ConvertWarning] = []
    mapping = {group_ids[name]: disc for name, disc in body.group_disciplines.items()}
    if site is None and mapping:
        mapping = {}
        warnings.append(
            ConvertWarning(
                code=WARN_DISCIPLINES_IGNORED,
                message="Şantiye açılmadığı için grup–disiplin eşlemesi saklanmadı; "
                "Planlama'da eşleyin",
            )
        )
    request = ContractSeedRequest(
        project_id=project.id,
        site_id=site.id if site is not None else None,
        start=project.start_date,
        end=project.end_date,
        items=tuple(
            ContractItemSeed(
                contract_item_id=item.id,
                catalog_item_id=item.catalog_item_id,
                unit_mhr=line.unit_mhr,
                rate_is_offer=line.rate_is_offer,
            )
            for item, line in zip(seeded.items, lines, strict=True)
        ),
        group_disciplines=mapping,
        actor_id=actor.id,
        label=f"Rev.0 — {offer.offer_no}",
    )
    warnings.extend(_to_warnings(await contract_seed.run_seed_hooks(session, request), group_names))

    offer.project_id = project.id
    offer.converted_at = datetime.now(UTC)
    offer.converted_by_user_id = actor.id
    await session.flush()
    return ConvertResult(
        offer=offer,
        project=project,
        site=site,
        item_count=len(seeded.items),
        warnings=warnings,
    )
