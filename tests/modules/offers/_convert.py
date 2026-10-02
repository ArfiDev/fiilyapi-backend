"""TKL-B6.2 test yardimcilari: kazanilmis teklif kurma, govde ureticisi, sahte tohum kancasi."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from app.core import contract_seed
from app.core.contract_seed import ContractSeedRequest, SeedWarning

from ._offers import durum_yap, grup, kalem, revizyon, teklif, tum_kalemler

D = Decimal


def url(offer_id: str) -> str:
    return f"/offers/{offer_id}/convert"


@dataclass
class Kazanilmis:
    offer_id: str
    offer_no: str
    #: poz adi → teklif kalemi (`id`, `unit_mhr`, `catalog_item_id` ...)
    kalemler: dict[str, dict]


async def kazanilmis_teklif(client, admin, isveren, katalog, **over) -> Kazanilmis:
    """Kaba: Beton (10, maliyet 100, adam-saat KATALOG 1.5) + Kalip (5, maliyet 80, adam-saat 3 —
    katalog 2'den FARKLI); Ince: Demir (2, maliyet 50, katalog 0.75). Son revizyon `won`."""
    o = await teklif(client, admin, isveren, **over)
    kaba = await grup(client, admin, o["id"], name="Kaba")
    ince = await grup(client, admin, o["id"], name="İnce")
    await kalem(
        client, admin, o["id"], kaba["id"], katalog[0].id, quantity="10", cost_unit_price="100"
    )
    await kalem(
        client, admin, o["id"], kaba["id"], katalog[1].id,
        quantity="5", cost_unit_price="80", unit_mhr="3",
    )  # fmt: skip
    await kalem(
        client, admin, o["id"], ince["id"], katalog[2].id, quantity="2", cost_unit_price="50"
    )
    await durum_yap(client, admin, o["id"], "won")
    rev = await revizyon(client, admin, o["id"])
    por_ad = {k["description"]: k for k in tum_kalemler(rev)}
    return Kazanilmis(o["id"], o["offer_no"], por_ad)


def kalem_govdesi(k: dict, code: str, **over) -> dict:
    return {
        "catalog_item_id": k["catalog_item_id"],
        "offer_item_id": k["id"],
        "code": code,
        "description": k["description"],
        "unit": k["unit"],
        "quantity": "10",
        "unit_price": "120.50",
        **over,
    }


def govde(kz: Kazanilmis, **over) -> dict:
    """Dort kalem, iki grup. Kaba: Beton (teklif kalemi, adam-saat = katalog) 10 x 120.50 · Kalip
    (teklif kalemi, adam-saat katalogdan FARKLI) 5 x 99.99; Ince: Demir (teklif kalemi) 2 x 10 ·
    YENI (ekranda katalogdan eklenen, `offer_item_id` yok) 1 x 5.00 → Σ = 1205.00 + 499.95 +
    20.00 + 5.00 = 1729.95."""
    k = kz.kalemler
    govde_ = {
        "project": {
            "name": "A Blok Projesi",
            "city": "İstanbul",
            "start_date": "2026-11-01",
            "end_date": "2027-10-31",
        },
        "contract": {
            "contract_no": "SZL-2026-01",
            "signature_date": "2026-10-15",
            "has_price_escalation": False,
        },
        "groups": [
            {
                "name": "Kaba",
                "items": [
                    kalem_govdesi(k["Beton"], "B-01"),
                    kalem_govdesi(k["Kalıp"], "B-02", quantity="5", unit_price="99.99"),
                ],
            },
            {
                "name": "İnce",
                "items": [
                    kalem_govdesi(k["Demir"], "I-01", quantity="2", unit_price="10"),
                    kalem_govdesi(
                        k["Demir"], "I-02", offer_item_id=None, quantity="1", unit_price="5.00"
                    ),
                ],
            },
        ],
    }
    govde_.update(over)
    return govde_


class SahteTohum:
    """Sahte EV kancasi: gelen `ContractSeedRequest`leri saklar, `uyarilar` doner, `hata` varsa
    firlatir (rollback testi)."""

    def __init__(self) -> None:
        self.istekler: list[ContractSeedRequest] = []
        self.uyarilar: list[SeedWarning] = []
        #: True: `group_disciplines`in ilk grubuna OZGU bir uyari da doner.
        self.gruba_ozgu = False
        self.hata: Exception | None = None

    async def __call__(self, session, req: ContractSeedRequest) -> list[SeedWarning]:  # noqa: ANN001
        self.istekler.append(req)
        if self.hata is not None:
            raise self.hata
        uyarilar = list(self.uyarilar)
        if self.gruba_ozgu and req.group_disciplines:
            uyarilar.append(
                SeedWarning("grup_uyarisi", "Grup uyarısı", next(iter(req.group_disciplines)))
            )
        return uyarilar


def kanca_kur() -> tuple[SahteTohum, tuple]:
    foto = contract_seed.registered()
    contract_seed.unregister_all()
    sahte = SahteTohum()
    contract_seed.register_seed_hook(sahte)
    return sahte, foto
