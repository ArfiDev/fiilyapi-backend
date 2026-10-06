"""IZN-B4d — AI okuma araçları, günlük ve makine maskeli yanıtıyla ÇALIŞIR.

`ai/tools/schemas.py` zarfları üst kaynağın `Decimal | None` alanlarını `Decimal` ZORUNLU
tuttuğunda, maliyet kategorisi gizli rolde araç `ValidationError` ile ölür ve huni bunu
`ust_kaynak_hatasi`na düşürür (IZN-B4b'de hakediş araçlarında yaşanan kusurun aynısı).

Düzeltilen alanlar ve onları taşıyan araç:

| Araç | Alan |
|---|---|
| `gunluk_kayit` | `AiGunlukKayit.lines_total` |
| `makine_calisma` | `AiMakineCalismasi.total_cost` |
| `makine_yakit` | `AiMakineYakiti.total_amount`, `AiYakitSatiri.amount` |

Desen `test_p8_kapsam_maskesi.py`'dir (o dosyaya DOKUNULMAZ). Her araç iki kez ölçülür: gizli rolde
araç YAŞAR + alan `None` + tutar izi gövdede YOK; bayraksız rolde aynı tutar GÖRÜNÜR (pozitif
kontrol — tohum çürürse üstteki iki iddia boş küme üzerinde de yeşil kalırdı).
"""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal

import pytest

from app.core.sayfalar import HiddenCategory
from app.modules.ai import audit as ai_audit
from app.modules.ai.registry import ToolRegistry
from app.modules.ai.result import Ok, ToolError
from app.modules.ai.tools.catalog import READ_TOOLS
from app.modules.equipment.models import (
    Equipment,
    EquipmentCategory,
    EquipmentFuelLog,
    EquipmentRatePeriod,
    EquipmentRentalInvoice,
    EquipmentWorkLog,
    RentalInvoiceStatus,
    WorkLogType,
)
from app.modules.procurement.models import PaymentTerms, Supplier
from app.modules.site_diary.models import DiaryStatus, SiteDiaryEntry, SiteDiaryLine
from app.modules.sites.models import Site, SiteStatus
from app.modules.users.models import ProjectMember
from tests._hassas_alan import rol_gizle

_ROL = "patron"

#: Her izi BİRBİRİNDEN farklı: yanlış alanı okuyan bir assert da yeşil kalmasın.
_GUNLUK_TOPLAM = "5550.00"  # 3 × 1850,00
_CALISMA_MALIYETI = "2800.00"  # 10 saat = 1 gün × 2800
_YAKIT_TUTARI = "4550.00"  # 100 lt × 45,50
_KIRA_FATURASI = "98765.00"
_IZLER = ("5550", "2800", "4550", "98765")


@pytest.fixture(autouse=True)
def _denetim_sussun(monkeypatch):
    async def _sahte(**kwargs):
        return None

    monkeypatch.setattr(ai_audit, "record_tool_call", _sahte)


@pytest.fixture
async def kurulum(seeded_db, user_factory, project_factory):
    proje = await project_factory(code="B4D-AI", name="B4d AI Projesi")
    santiye = Site(
        project_id=proje.id,
        code="B4D-AI-S1",
        name="B4d AI Şantiyesi",
        status=SiteStatus.active,
        start_date=date(2026, 1, 1),
    )
    seeded_db.add(santiye)
    kullanici = await user_factory("b4d-ai@fiil.example.com", "Sifre1234!", _ROL)
    seeded_db.add(
        ProjectMember(user_id=kullanici.id, project_id=proje.id, role_id=kullanici.role_id)
    )
    await seeded_db.flush()

    # Günlük: tutar satır birim fiyatından türer (3 × 1850).
    gunluk = SiteDiaryEntry(
        site_id=santiye.id,
        project_id=proje.id,
        entry_date=date(2026, 7, 15),
        status=DiaryStatus.draft,
        created_by=kullanici.id,
    )
    gunluk.lines.append(
        SiteDiaryLine(
            code="01.001",
            description="Kazı",
            unit="m³",
            unit_price=Decimal("1850.00"),
            quantity=Decimal("3"),
        )
    )
    seeded_db.add(gunluk)

    # Makine DEPODA (`site_id IS NULL`): kapsam OR'u gereği her aktöre görünür.
    makine = Equipment(
        name="Ekskavatör B4D",
        category=EquipmentCategory.machinery,
        site_id=None,
        rate_amount=Decimal("2800"),
        rate_period=EquipmentRatePeriod.daily,
    )
    seeded_db.add(makine)
    await seeded_db.flush()
    seeded_db.add_all(
        [
            EquipmentWorkLog(
                equipment_id=makine.id,
                work_date=date(2026, 7, 10),
                site_id=None,
                record_type=WorkLogType.worked,
                hours=Decimal("10"),
            ),
            EquipmentFuelLog(
                equipment_id=makine.id,
                fuel_date=date(2026, 7, 10),
                site_id=None,
                liters=Decimal("100"),
                unit_price=Decimal("45.50"),
            ),
        ]
    )
    tedarikci = Supplier(name="B4D Kiralama A.Ş.", payment_terms=PaymentTerms.days_30)
    seeded_db.add(tedarikci)
    await seeded_db.flush()
    seeded_db.add(
        EquipmentRentalInvoice(
            supplier_id=tedarikci.id,
            invoice_no="B4D-FT-1",
            period_year=2026,
            period_month=7,
            rate_period=EquipmentRatePeriod.hourly,
            status=RentalInvoiceStatus.draft,
            invoice_amount=Decimal(_KIRA_FATURASI),
        )
    )
    await seeded_db.flush()
    seeded_db.expunge(kullanici)  # p8 ile aynı gerekçe: okuma düzlemi aynı session'ı kullanır
    return {"user": kullanici, "site_id": santiye.id}


_ARAC_GIRDISI = {
    "gunluk_kayit": lambda k: {"site_id": str(k["site_id"])},
    "makine_calisma": lambda k: {"year": 2026, "month": 7},
    "makine_yakit": lambda k: {"year": 2026, "month": 7},
    "makine_kira": lambda k: {},
}

#: Araç → (alanın yolu, bayraksız roldeki beklenen değer).
#: Yol: `data` içinde anahtar/indeks zinciri (liste araçlarında `data` satır listesidir).
_ALANLAR = {
    "gunluk_kayit": [((0, "lines_total"), _GUNLUK_TOPLAM)],
    "makine_calisma": [
        (("total_cost",), _CALISMA_MALIYETI),
        (("rows", 0, "cost"), _CALISMA_MALIYETI),
    ],
    "makine_yakit": [
        (("total_amount",), _YAKIT_TUTARI),
        (("rows", 0, "amount"), _YAKIT_TUTARI),
    ],
    "makine_kira": [((0, "invoice_amount"), _KIRA_FATURASI)],
}


def _al(veri, yol):
    for anahtar in yol:
        veri = veri[anahtar]
    return veri


async def _cagir(ad, kurulum, transport_factory, actor_factory):
    return await ToolRegistry(READ_TOOLS).invoke(
        arac_adi=ad,
        argumanlar=_ARAC_GIRDISI[ad](kurulum),
        actor=await actor_factory(kurulum["user"]),
        transport=transport_factory(kurulum["user"]),
    )


@pytest.mark.parametrize("ad", sorted(_ARAC_GIRDISI))
async def test_bayraksiz_rolde_tutar_gorunur_POZITIF_KONTROL(
    ad, seeded_db, kurulum, transport_factory, actor_factory
) -> None:
    await rol_gizle(seeded_db, _ROL)
    sonuc = await _cagir(ad, kurulum, transport_factory, actor_factory)
    assert isinstance(sonuc, Ok), f"{ad} → {type(sonuc).__name__}: {sonuc!r}"
    for yol, beklenen in _ALANLAR[ad]:
        assert Decimal(_al(sonuc.data, yol)) == Decimal(beklenen), f"{ad}.{yol}"


@pytest.mark.parametrize("ad", sorted(_ARAC_GIRDISI))
async def test_maliyet_kar_gizli_rolde_arac_YASAR_alan_null_tutar_sizmaz(
    ad, seeded_db, kurulum, transport_factory, actor_factory
) -> None:
    await rol_gizle(seeded_db, _ROL, HiddenCategory.maliyet_kar)
    sonuc = await _cagir(ad, kurulum, transport_factory, actor_factory)
    assert not (isinstance(sonuc, ToolError) and sonuc.kod == "ust_kaynak_hatasi"), (
        f"{ad} maliyet gizliyken PATLADI (zorunlu Decimal?)"
    )
    assert isinstance(sonuc, Ok), f"{ad} → {type(sonuc).__name__}"
    for yol, _ in _ALANLAR[ad]:
        assert _al(sonuc.data, yol) is None, f"{ad}.{yol} gizli rolde null olmalı"
    govde = json.dumps(sonuc.govde(), ensure_ascii=False)
    for iz in _IZLER:
        assert iz not in govde, f"{ad}: tutar sızdı ({iz})"


async def test_makine_calisma_gizli_rolde_bedel_notu_BILINMIYOR_DEMEZ(
    seeded_db, kurulum, transport_factory, actor_factory
) -> None:
    """Gizli bedel "bilinmiyor" değildir; model ikisini karıştırıp yalan söylemesin."""
    await rol_gizle(seeded_db, _ROL, HiddenCategory.maliyet_kar)
    sonuc = await _cagir("makine_calisma", kurulum, transport_factory, actor_factory)
    assert isinstance(sonuc, Ok)
    assert "GİZLİ" in sonuc.data["bilinmeyen_bedel_notu"]
    assert "BİLİNMİYOR" not in sonuc.data["bilinmeyen_bedel_notu"]
    assert Decimal(sonuc.data["total_hours"]) == Decimal("10")  # saat maskelenmez
