"""Task C3 — `app/modules/contracts/schemas.py` icin okuma/yazma semasi testleri.

Spec kaynagi: docs/superpowers/specs/2026-07-30-alt-proje-2-p5-sozlesmeler-design.md
§10 (sema listesi), §6.1-§6.5 (yanit alanlari), §2.2 (kapsam disi alanlar),
§3.2-§3.6 (alan uzunluk sinirlari — modeldeki String(N) ile birebir).
"""

import uuid
from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.modules.contracts.models import ContractStatus, PaymentPeriod
from app.modules.contracts.schemas import (
    ContractAllocationInput,
    ContractDistributionResponse,
    ContractDistributionSave,
    ContractListItem,
    ContractListResponse,
    ContractSummary,
    ContractType,
    EmployerContractDetail,
    EmployerContractGroupUpdate,
    EmployerContractItemCreate,
    EmployerContractItemResponse,
    EmployerContractItemsResponse,
    EmployerContractItemUpdate,
    SubcontractorContractCreate,
    SubcontractorContractDetail,
    SubcontractorContractItemCreate,
    SubcontractorContractItemResponse,
    SubcontractorContractItemUpdate,
    SubcontractorContractUpdate,
    SubcontractorCreate,
    SubcontractorListResponse,
    SubcontractorResponse,
    SubcontractorUpdate,
)
from app.modules.progress_payments.schemas import ProgressPaymentSummary

# --- Brief'teki taban testler (Adim 1) ---


def test_miktar_sifir_olamaz():
    with pytest.raises(ValidationError):
        EmployerContractItemCreate(
            group_id=uuid.uuid4(),
            code="03.001",
            description="Beton",
            unit="m³",
            quantity=0,
            unit_price=100,
        )


def test_taseron_kalemi_fiyatsiz_kabul_edilir():
    kalem = SubcontractorContractItemCreate(
        code="03.001", description="Beton", unit="m³", quantity=10, unit_price=None
    )
    assert kalem.unit_price is None


def test_taseron_kalemi_negatif_fiyat_reddedilir():
    with pytest.raises(ValidationError):
        SubcontractorContractItemCreate(
            code="03.001", description="Beton", unit="m³", quantity=10, unit_price=-1
        )


# --- ContractType ---


def test_contract_type_gecerli_degerler():
    assert ContractType.__args__ == ("employer", "subcontractor")


# --- Alan uzunluk sinirlari (spec §3.2-§3.6, modeldeki String(N) ile birebir) ---


def test_employer_item_code_50_karakteri_asamaz():
    with pytest.raises(ValidationError):
        EmployerContractItemCreate(
            group_id=uuid.uuid4(),
            code="x" * 51,
            description="Beton",
            unit="m³",
            quantity=1,
            unit_price=1,
        )


def test_employer_item_unit_50_karakteri_asamaz():
    with pytest.raises(ValidationError):
        EmployerContractItemCreate(
            group_id=uuid.uuid4(),
            code="03.001",
            description="Beton",
            unit="x" * 51,
            quantity=1,
            unit_price=1,
        )


def test_subcontractor_name_200_karakteri_asamaz():
    with pytest.raises(ValidationError):
        SubcontractorCreate(name="x" * 201)


def test_subcontractor_phone_30_karakteri_asamaz():
    with pytest.raises(ValidationError):
        SubcontractorCreate(name="Ahmet Taahhüt", phone="1" * 31)


def test_subcontractor_email_255_karakteri_asamaz():
    with pytest.raises(ValidationError):
        SubcontractorCreate(name="Ahmet Taahhüt", email="a" * 251 + "@x.co")


def test_subcontractor_category_100_karakteri_asamaz():
    with pytest.raises(ValidationError):
        SubcontractorCreate(name="Ahmet Taahhüt", category="x" * 101)


def test_subcontractor_contract_contract_no_100_karakteri_asamaz():
    with pytest.raises(ValidationError):
        SubcontractorContractCreate(contract_no="x" * 101)


def test_subcontractor_contract_work_category_100_karakteri_asamaz():
    with pytest.raises(ValidationError):
        SubcontractorContractCreate(work_category="x" * 101)


def test_subcontractor_contract_item_code_50_karakteri_asamaz():
    with pytest.raises(ValidationError):
        SubcontractorContractItemCreate(
            code="x" * 51, description="Beton", unit="m³", quantity=1, unit_price=None
        )


# --- Yuzde/gun kisitlari (guards.py'de tekrarlanmayan pydantic kurallari) ---


def test_advance_pct_100u_asamaz():
    with pytest.raises(ValidationError):
        SubcontractorContractCreate(advance_pct=Decimal("101"))


def test_retainage_pct_negatif_olamaz():
    with pytest.raises(ValidationError):
        SubcontractorContractCreate(retainage_pct=Decimal("-1"))


def test_payment_term_days_negatif_olamaz():
    with pytest.raises(ValidationError):
        SubcontractorContractCreate(payment_term_days=-1)


def test_vat_pct_varsayilani_yirmi():
    """Taşeron hakedişi spec §8 S1: mockup çelişkiliydi (liste %18, form %20)."""
    assert SubcontractorContractCreate().vat_pct == Decimal("20")


@pytest.mark.parametrize("oran", [Decimal("1"), Decimal("10"), Decimal("20")])
def test_vat_pct_izinli_kume(oran: Decimal):
    assert SubcontractorContractCreate(vat_pct=oran).vat_pct == oran
    assert SubcontractorContractUpdate(vat_pct=oran).vat_pct == oran


@pytest.mark.parametrize("oran", [Decimal("18"), Decimal("0"), Decimal("8")])
def test_vat_pct_kume_disi_422(oran: Decimal):
    """%18 eski oran ARTEFAKTIDIR — küme {1, 10, 20} (spec §8 S1)."""
    with pytest.raises(ValidationError):
        SubcontractorContractCreate(vat_pct=oran)
    with pytest.raises(ValidationError):
        SubcontractorContractUpdate(vat_pct=oran)


def test_subcontractor_contract_varsayilanlari_spec_ile_birebir():
    sozlesme = SubcontractorContractCreate()
    assert sozlesme.advance_pct == Decimal("10")
    assert sozlesme.retainage_pct == Decimal("5")
    assert sozlesme.vat_pct == Decimal("20")
    assert sozlesme.payment_period == PaymentPeriod.monthly
    assert sozlesme.payment_term_days == 30
    assert sozlesme.materials_by_contractor is False
    assert sozlesme.subcontractor_files_own_sgk is False
    assert sozlesme.status == ContractStatus.active
    assert sozlesme.is_draft is False
    assert sozlesme.items == []


# --- Ic ice kalemler (spec §6.5: sozlesme + kalemler atomik) ---


def test_subcontractor_contract_create_kalemleri_ic_ice_tasir():
    sozlesme = SubcontractorContractCreate(
        items=[
            SubcontractorContractItemCreate(
                code="03.001", description="Beton", unit="m³", quantity=10, unit_price=None
            )
        ]
    )
    assert len(sozlesme.items) == 1
    assert sozlesme.items[0].code == "03.001"


# --- Update semalari: tum alanlar opsiyonel (kismi PATCH) ---


def test_employer_item_update_bos_govde_kabul_edilir():
    guncelleme = EmployerContractItemUpdate()
    assert guncelleme.quantity is None
    assert guncelleme.unit_price is None


def test_employer_group_update_bos_govde_kabul_edilir():
    guncelleme = EmployerContractGroupUpdate()
    assert guncelleme.name is None


def test_subcontractor_update_bos_govde_kabul_edilir():
    guncelleme = SubcontractorUpdate()
    assert guncelleme.name is None


def test_subcontractor_contract_update_bos_govde_kabul_edilir():
    guncelleme = SubcontractorContractUpdate()
    assert guncelleme.status is None


def test_subcontractor_contract_item_update_bos_govde_kabul_edilir():
    guncelleme = SubcontractorContractItemUpdate()
    assert guncelleme.quantity is None


# --- Kapsam disi alanlar (spec §2.2): yanit semalarinda ACIKCA yer alir ---


def _employer_item_response(**overrides):
    taban = dict(
        id=uuid.uuid4(),
        group_id=uuid.uuid4(),
        code="03.001",
        description="Beton",
        unit="m³",
        quantity=Decimal("10"),
        unit_price=Decimal("100"),
        sort_order=0,
        catalog_item_id=None,
        source_code=None,
        distributed_quantity=Decimal("0"),
        remaining_quantity=Decimal("10"),
    )
    taban.update(overrides)
    return EmployerContractItemResponse(**taban)


def _sifir_hakedis_ozeti() -> ProgressPaymentSummary:
    return ProgressPaymentSummary(
        contract_amount=Decimal("1000"),
        cumulative_gross=Decimal("0.00"),
        progress_pct=Decimal("0.00"),
        advance_deduction_total=Decimal("0.00"),
        retention_total=Decimal("0.00"),
        net_total=Decimal("0.00"),
        payment_count=0,
        pending_count=0,
        remaining=Decimal("1000"),
    )


def test_employer_contract_detail_kapsam_disi_alanlar_acik_doner():
    detay = EmployerContractDetail(
        project_id=uuid.uuid4(),
        contract_no="SZL-2025-001",
        signature_date=None,
        amount=Decimal("1000"),
        advance_pct=Decimal("20"),
        retainage_pct=Decimal("5"),
        vat_pct=Decimal("20"),
        late_penalty_daily=None,
        has_price_escalation=False,
        index_type=None,
        status=ContractStatus.active,
        start_date=None,
        end_date=None,
        employer_name="ABC Insaat",
        contractor_name="XYZ Yuklenici",
        items_total=Decimal("900"),
        items_total_diff=Decimal("100"),
        advance_amount=Decimal("200"),
        progress_payment_summary=_sifir_hakedis_ozeti(),
    )
    # P7/H9 (spec §9.6): `progress_payments` yer tutucu listesinden ÇIKTI;
    # H9 denetim O2 sonrası `progress_payment_summary` ZORUNLU alandır — uç
    # her zaman gerçek bir özet döner (hakediş yoksa sıfırlarla), `None`
    # DEĞİL (uç yanıtı ayrıca `test_summary.py`de doğrulanır).
    assert detay.progress_payment_summary == _sifir_hakedis_ozeti()
    assert detay.milestones is None
    assert detay.documents is None
    assert "progress_payments" not in detay.pending_modules
    # P-YT4 (2026-08-23): `"project_schedule"` bir FOSİLDİ — depoda o ada sahip
    # ne izin modülü ne paket ne dosya vardı. Milestone'ın gerçek sahibi
    # `sites` (`SectionMilestone`) ve CANLI; anahtar ona döndü. Fosil anahtar
    # yasağının bekçisi: `test_pyt4_yer_tutucu_denetimi.py`.
    assert detay.pending_modules == ["sites", "documents"]


def test_employer_contract_detail_hakedis_ozeti_zorunludur():
    """H9 denetim O2: alan verilmezse `ValidationError` — `None` varsayılanı

    KALDIRILDI, uç sözleşmesi her zaman dolu bir özet taşımayı garanti eder.
    """
    with pytest.raises(ValidationError):
        EmployerContractDetail(
            project_id=uuid.uuid4(),
            contract_no="SZL-2025-001",
            signature_date=None,
            amount=Decimal("1000"),
            advance_pct=Decimal("20"),
            retainage_pct=Decimal("5"),
            vat_pct=Decimal("20"),
            late_penalty_daily=None,
            has_price_escalation=False,
            status=ContractStatus.active,
            start_date=None,
            end_date=None,
            employer_name="ABC Insaat",
            contractor_name="XYZ Yuklenici",
            items_total=Decimal("900"),
            items_total_diff=Decimal("100"),
            advance_amount=Decimal("200"),
        )


def test_subcontractor_contract_detail_kapsam_disi_alanlar_acik_doner():
    detay = SubcontractorContractDetail(
        id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        site_id=None,
        subcontractor_id=None,
        subcontractor_name="Ahmet Taahhüt",
        work_category="Elektrik",
        contract_no=None,
        signature_date=None,
        is_notarized=False,
        start_date=None,
        end_date=None,
        late_penalty_daily=None,
        advance_pct=Decimal("10"),
        retainage_pct=Decimal("5"),
        vat_pct=Decimal("20"),
        payment_period=PaymentPeriod.monthly,
        payment_term_days=30,
        materials_by_contractor=False,
        subcontractor_files_own_sgk=False,
        vat_withholding=False,
        status=ContractStatus.active,
        is_draft=False,
        items=[],
        contract_total=Decimal("0"),
        items_missing_price=0,
    )
    # ⚠️ Buradaki yer tutucu TAŞERON hakedişidir (spec §1.2) — P7 yalnız işveren
    # tarafını yazdı. Anahtar bu yüzden ÇIKARILMADI, `subcontractor_progress_
    # payments` olarak YENİDEN ADLANDIRILDI (P7/H9).
    assert detay.progress_payment_summary is None
    assert detay.documents is None
    assert detay.pending_modules == ["subcontractor_progress_payments", "documents"]


def test_contract_summary_hakedis_toplami_kapsam_disi():
    ozet = ContractSummary(
        total_amount=Decimal("1000"),
        active_count=1,
        expiring_this_month_count=0,
    )
    # P7/H9: düz `Decimal | None`. Bu test ŞEMA VARSAYILANINI ölçer, servis
    # dalını değil: alan verilmezse `None` kalır. TH-SUM sonrası liste ucu
    # İKİ sekmede de gerçek toplamı geçirir (`0.00` dahil) — bu iddia oradaki
    # davranışa değil, tipin uyumluluğuna bekçilik eder.
    assert ozet.progress_payment_total is None


def test_subcontractor_contract_item_response_bagsiz_kalem_group_null_doner():
    kalem = SubcontractorContractItemResponse(
        id=uuid.uuid4(),
        contract_id=uuid.uuid4(),
        source_contract_item_id=None,
        code="03.001",
        description="Beton",
        unit="m³",
        quantity=Decimal("10"),
        unit_price=Decimal("100"),
        sort_order=0,
        group=None,
    )
    assert kalem.group is None
    assert kalem.line_total == Decimal("1000.00")


def test_subcontractor_contract_item_response_fiyatsiz_kalem_line_total_NULL():
    """🔴 KAPSAM MASKESİ (kullanıcı kararı 2026-09-19) — türev, girdisi yoksa `None` DÖNER.

    Bu test ESKİDEN `line_total == Decimal("0")` diyordu ve o iddia İKİ ayrı şeyi
    aynı sayıya çökertiyordu: *"fiyat girilmedi"* ile *"fiyatı görmeye yetkin
    yok"*. `limited` kapsamındaki rol için `unit_price` MASKELENİR ve eski hâl
    gizlenmiş bir bedeli ekrana **"0,00 TL"** diye basardı — gizlemekten kötüdür,
    çünkü kullanıcı sahte bir sayıya bakıp ona göre karar verir.

    🔴 "Fiyatsız kalem sözleşme bedeline 0 katkı verir" kuralı KALKMADI — o kural
    SATIRIN KENDİ tutarında değil, toplamayı yapan yerde yaşar
    (`service._subcontractor_amount` fiyatsız satırı kendisi eler). İki soru
    ayrıdır: *bu satırın tutarı kaç* (bilinmiyor → `None`) ve *bu satır toplama
    ne katar* (0). Eskiden birinciyi ikincisinin cevabıyla yanıtlıyorduk.
    """
    kalem = SubcontractorContractItemResponse(
        id=uuid.uuid4(),
        contract_id=uuid.uuid4(),
        source_contract_item_id=None,
        code="03.001",
        description="Beton",
        unit="m³",
        quantity=Decimal("10"),
        unit_price=None,
        sort_order=0,
        group=None,
    )
    assert kalem.line_total is None


# --- Liste / dagitim gövdeleri ---


def test_contract_list_response_ozet_ve_kalemler():
    yanit = ContractListResponse(
        summary=ContractSummary(
            total_amount=Decimal("1000"), active_count=1, expiring_this_month_count=0
        ),
        items=[
            ContractListItem(
                id=uuid.uuid4(),
                title="ABC Konut Projesi",
                contract_no="SZL-2025-001",
                counterparty_name="ABC Insaat",
                amount=Decimal("1000"),
                start_date=None,
                end_date=None,
                status=ContractStatus.active,
                is_draft=False,
            )
        ],
    )
    assert yanit.items[0].progress_pct is None


def test_contract_allocation_input_miktar_pozitif_olmali():
    with pytest.raises(ValidationError):
        ContractAllocationInput(
            contract_item_id=uuid.uuid4(), site_id=uuid.uuid4(), quantity=Decimal("-1")
        )


def test_contract_allocation_input_quantity_null_kaldirma_anlamina_gelir():
    tahsis = ContractAllocationInput(
        contract_item_id=uuid.uuid4(), site_id=uuid.uuid4(), quantity=None
    )
    assert tahsis.quantity is None


def test_contract_distribution_save_bos_govde_kabul_edilir():
    govde = ContractDistributionSave()
    assert govde.allocations == []


def test_contract_distribution_response_sayaclari_dondurur():
    yanit = ContractDistributionResponse(
        sites=[],
        groups=[],
        undistributed_item_count=2,
        undistributed_item_names=["03.005", "03.006"],
        site_summaries=[],
        distributed_item_count=8,
        total_item_count=10,
    )
    assert yanit.undistributed_item_count == 2
    assert yanit.total_item_count == 10


def test_employer_contract_items_response_grup_ici_kalem_tasir():
    yanit = EmployerContractItemsResponse(
        groups=[
            {
                "id": uuid.uuid4(),
                "name": "A — Betonarme İşleri",
                "sort_order": 0,
                "items": [_employer_item_response()],
            }
        ]
    )
    assert yanit.groups[0].items[0].code == "03.001"


def test_subcontractor_list_response_ve_response_alanlari():
    liste = SubcontractorListResponse(
        items=[
            SubcontractorResponse(
                id=uuid.uuid4(),
                name="Ahmet Taahhüt",
                tax_number=None,
                contact_person=None,
                phone=None,
                email=None,
                category="Elektrik",
                is_active=True,
            )
        ]
    )
    assert liste.items[0].category == "Elektrik"
