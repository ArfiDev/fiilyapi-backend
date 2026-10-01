"""Task 4 — santiye/bolum semalari (spec §4.1)."""

import uuid

import pytest
from pydantic import ValidationError

from app.modules.projects.schemas import CountPlaceholder, MetricPlaceholder
from app.modules.sites import schemas as sites_schemas
from app.modules.sites.schemas import (
    SectionCreate,
    SectionResponse,
    SectionUpdate,
    SiteCard,
    SiteCreate,
    SiteUpdate,
)


def test_placeholders_are_reused_from_projects_module():
    """Spec §3: MetricPlaceholder/CountPlaceholder KOPYALANMAZ, import edilir."""
    assert sites_schemas.MetricPlaceholder is MetricPlaceholder
    assert sites_schemas.CountPlaceholder is CountPlaceholder


def _card_kwargs() -> dict:
    return {
        "id": uuid.uuid4(),
        "code": "A-BLOK",
        "name": "A-Blok Şantiyesi",
        "status": "active",
        "address": "Kuyubaşı Mah.",
        "city": "Ankara",
        "city_inherited": False,
        "site_manager_name": "Sercan Öztürk",
        "start_date": "2025-03-01",
        "end_date": "2026-12-31",
        "delivery_date": None,
        "remaining_days": 157,
        "section_count": 5,
        "worker_count": CountPlaceholder(pending_module="timesheet"),
        "progress_pct": MetricPlaceholder(pending_module="progress_payments"),
        # T4 — sema genislemesi: SiteCard'in yeni alanlarinin VARSAYILANI YOK,
        # bu yuzden yardimci burada hepsini acikca verir.
        "is_draft": False,
        "site_manager_user_id": None,
        "safety_officer_user_id": None,
        "safety_officer_name": None,
        "safety_officer_is_outsourced": False,
        "neighborhood": None,
        "parcel": None,
        "gps_coordinates": None,
        "land_area_m2": None,
        "construction_area_m2": None,
        "floor_info": None,
        "budget": None,
        "facilities": sites_schemas.SiteFacilities(),
        "electricity_subscription_no": None,
        "water_subscription_no": None,
        "planned_worker_count": None,
    }


def test_site_card_serializes_placeholder_fields():
    card = SiteCard(**_card_kwargs())
    dumped = card.model_dump()

    assert dumped["worker_count"] == {
        "available": False,
        "count": None,
        "pending_module": "timesheet",
    }
    assert dumped["progress_pct"] == {
        "available": False,
        "value": None,
        "pending_module": "progress_payments",
    }
    assert dumped["remaining_days"] == 157
    assert dumped["city_inherited"] is False


def test_site_card_accepts_negative_remaining_days():
    """Spec §4.2: gecikme NEGATIF doner, kirpilmaz."""
    card = SiteCard(**{**_card_kwargs(), "remaining_days": -42})
    assert card.remaining_days == -42


def test_site_card_accepts_null_remaining_days():
    card = SiteCard(**{**_card_kwargs(), "remaining_days": None})
    assert card.remaining_days is None


def test_section_response_carries_four_placeholders():
    section = SectionResponse(
        id=uuid.uuid4(),
        code=None,
        name="Kat 6-10 Kaba İnşaat",
        status="active",
        manager_name="Sercan Öztürk",
        start_date="2026-01-01",
        end_date="2026-09-30",
        sort_order=2,
        progress_pct=MetricPlaceholder(pending_module="progress_payments"),
        boq_item_count=CountPlaceholder(pending_module="boq"),
        budget=MetricPlaceholder(pending_module="boq"),
        worker_count=CountPlaceholder(pending_module="timesheet"),
        # BLM-SAY: kayitli iki kolon LISTE semasina tasindi — VARSAYILANLARI
        # YOKTUR (yukaridaki P11 gerekcesi): dolduran taraf unutursa
        # ValidationError patlar, sessizce `None` DOGMAZ.
        planned_worker_count=None,
        # BLF-B1: `budget_amount` yanittan KALKTI.
        # P11 additive alanlari (spec §3): VARSAYILANLARI YOKTUR — alani
        # doldurmayi unutan bir donusturucu sessizce degil, ValidationError ile
        # patlamalidir; bu yuzden dogrudan kurulan her govde de onlari verir.
        depends_on_section_id=None,
        milestones=[],
    )

    assert section.boq_item_count.pending_module == "boq"
    assert section.budget.pending_module == "boq"
    assert section.progress_pct.available is False


def test_section_response_has_no_delay_risk_field():
    """Spec §3.3: "3 gecikme riski" ne gercek ne yer tutucu olarak DONMEZ."""
    assert "delay_risk_count" not in SectionResponse.model_fields
    assert "delay_risk" not in SectionResponse.model_fields


def test_site_has_no_contract_amount_input():
    """Spec §2.1: santiyeye elle sozlesme bedeli girilemez."""
    assert "contract_amount" not in SiteCreate.model_fields
    assert "contract_amount" not in SiteUpdate.model_fields


def test_section_has_no_budget_input():
    """Spec §2.2: bolum bedeli BOQ turevidir, elle girilmez."""
    assert "budget" not in SectionCreate.model_fields
    assert "budget" not in SectionUpdate.model_fields


def test_site_create_defaults():
    data = SiteCreate(name="A-Blok Şantiyesi")
    assert data.code is None  # servis ad'dan turetir (spec §8 acik soru 2)
    assert data.status.value == "active"
    assert data.city is None


def test_section_create_defaults_to_planned():
    data = SectionCreate(name="Kat 6-10")
    assert data.status.value == "planned"
    assert data.sort_order == 0


def test_site_create_rejects_blank_name():
    with pytest.raises(ValidationError):
        SiteCreate(name="")


def test_updates_are_fully_optional():
    assert SiteUpdate().model_dump(exclude_unset=True) == {}
    assert SectionUpdate().model_dump(exclude_unset=True) == {}


def test_section_update_cannot_move_section_between_sites():
    assert "site_id" not in SectionUpdate.model_fields


def test_site_update_cannot_move_site_between_projects():
    assert "project_id" not in SiteUpdate.model_fields


# ---------------------------------------------------------------------- #
# T4 — Santiye formu genislemesi semalari (spec §6.1 / §6.2)
# ---------------------------------------------------------------------- #

_NEW_CARD_FIELDS = (
    "is_draft",
    "site_manager_user_id",
    "safety_officer_user_id",
    "safety_officer_name",
    "safety_officer_is_outsourced",
    "neighborhood",
    "parcel",
    "gps_coordinates",
    "land_area_m2",
    "construction_area_m2",
    "floor_info",
    "budget",
    "facilities",
    "electricity_subscription_no",
    "water_subscription_no",
    "planned_worker_count",
)

# P2 sozlesmesi: bu alanlarin HICBIRI kaldirilamaz/yeniden adlandirilamaz.
_P2_CARD_FIELDS = (
    "id",
    "code",
    "name",
    "status",
    "address",
    "city",
    "city_inherited",
    "site_manager_name",
    "start_date",
    "end_date",
    "delivery_date",
    "remaining_days",
    "section_count",
    "worker_count",
    "progress_pct",
)

_FACILITY_FIELDS = (
    "closed_warehouse",
    "open_storage",
    "cold_storage",
    "site_office",
    "canteen",
    "changing_room_wc",
    "dormitory",
    "infirmary",
)


def test_facilities_input_all_default_false():
    """§14.2 kilidi: mockup'taki on-isaretler ORNEK VERIDIR, varsayilan degil."""
    facilities = sites_schemas.SiteFacilitiesInput()

    assert set(_FACILITY_FIELDS) == set(sites_schemas.SiteFacilitiesInput.model_fields)
    assert all(getattr(facilities, field) is False for field in _FACILITY_FIELDS)


def test_site_create_facilities_default_factory():
    data = SiteCreate(name="A")
    assert all(getattr(data.facilities, field) is False for field in _FACILITY_FIELDS)


def test_site_section_input_has_no_estimated_amount():
    """§3.4: "Tahmini Bedel" yer tutucudur, saklanmaz; govdede gelirse yok sayilir."""
    assert "estimated_amount" not in sites_schemas.SiteSectionInput.model_fields
    parsed = sites_schemas.SiteSectionInput(name="Kat 1-5", estimated_amount="500000")
    assert not hasattr(parsed, "estimated_amount")


def test_site_section_input_has_no_sort_order():
    """Sira govdeden GELMEZ: dizi sirasindan atanir (0,1,2...)."""
    assert "sort_order" not in sites_schemas.SiteSectionInput.model_fields


def test_site_create_new_defaults():
    data = SiteCreate(name="A")
    assert data.status.value == "active"
    assert data.is_draft is False
    assert data.sections == []
    assert data.code is None


@pytest.mark.parametrize(
    "field",
    ["land_area_m2", "construction_area_m2", "budget", "planned_worker_count"],
)
def test_site_create_rejects_negative_amounts(field: str):
    with pytest.raises(ValidationError):
        SiteCreate(**{"name": "A", field: -1})


def test_site_create_accepts_construction_area_m2():
    """§13/16 kilidi: modelde vardi, semada YOKTU — bu dilimde eklendi."""
    from decimal import Decimal

    assert SiteCreate(name="A", construction_area_m2="1200.50").construction_area_m2 == Decimal(
        "1200.50"
    )


@pytest.mark.parametrize("value", ["abc", "39,9042", "", "39.9042, 32.8597"])
def test_gps_is_free_text_no_validation(value: str):
    """§3.5: sunucu GPS metnini DOGRULAMAZ — bugun hicbir tuketicisi yok."""
    assert SiteCreate(name="A", gps_coordinates=value).gps_coordinates == value


def test_gps_max_length_enforced():
    with pytest.raises(ValidationError):
        SiteCreate(name="A", gps_coordinates="x" * 51)


def test_floor_info_is_string_not_int():
    """Mockup 86 serbest metin — `floor_count: int` ICAT EDILMEZ."""
    assert SiteCreate(name="A", floor_info="2 bodrum + 10 normal").floor_info == (
        "2 bodrum + 10 normal"
    )
    assert "floor_count" not in SiteCreate.model_fields


def test_site_update_all_fields_optional():
    data = SiteUpdate()
    assert data.model_fields_set == set()
    assert data.model_dump(exclude_unset=True) == {}


def test_site_update_has_no_project_id():
    assert "project_id" not in SiteUpdate.model_fields


def test_site_update_has_no_sections():
    """Bolumler P2 uclariyla yonetilir (§7.3)."""
    assert "sections" not in SiteUpdate.model_fields


def test_site_update_tracks_unset_vs_null():
    """ "Gonderilmedi" != "null yapildi"."""
    assert SiteUpdate(city=None).model_dump(exclude_unset=True) == {"city": None}
    assert "city" not in SiteUpdate(name="A").model_dump(exclude_unset=True)


def test_site_card_keeps_all_p2_fields():
    """Geriye uyum agi: P2 alanlarinin HICBIRI kaldirilmadi/yeniden adlandirilmadi."""
    missing = [field for field in _P2_CARD_FIELDS if field not in SiteCard.model_fields]
    assert missing == []


def test_site_card_has_sixteen_new_fields():
    missing = [field for field in _NEW_CARD_FIELDS if field not in SiteCard.model_fields]
    assert missing == []
    assert len(_NEW_CARD_FIELDS) == 16


def test_site_counts_has_draft():
    assert sites_schemas.SiteCounts.model_fields["draft"].annotation is int


def test_section_response_has_manager_user_id():
    assert "manager_user_id" in SectionResponse.model_fields


def test_section_response_budget_is_metric_placeholder_boq():
    """§14.3 kilidi: "Tahmini Bedel" sutununun kaynagi BU yer tutucudur."""
    budget = MetricPlaceholder(pending_module="boq")
    assert budget.pending_module == "boq"
    assert budget.available is False
    assert SectionResponse.model_fields["budget"].annotation is MetricPlaceholder


def test_section_response_has_no_estimated_amount():
    assert "estimated_amount" not in SectionResponse.model_fields


def test_metric_placeholder_imported_not_redefined():
    assert sites_schemas.MetricPlaceholder is MetricPlaceholder
    assert sites_schemas.CountPlaceholder is CountPlaceholder


def test_no_duration_days_field_anywhere():
    """§3.6: sure TUREVDIR, yanitta alan YOK."""
    for model in (SiteCard, SiteCreate, SiteUpdate, SectionResponse):
        assert "duration_days" not in model.model_fields
