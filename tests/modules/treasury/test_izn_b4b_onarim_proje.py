"""IZN-B4b onarımı — `GET /treasury/upcoming-payments` satırı KENDİ projesindeki rolle maskelenir;
bordro kaynaklı satırın tutarı `maas_kisisel` ile de gizlenir.
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from decimal import Decimal

from app.core.field_mask import MaskeKumeleri, maskele
from app.core.sayfalar import HiddenCategory
from app.core.timezone import today
from app.modules.invoicing.models import InvoiceDirection, InvoiceStatus
from app.modules.treasury.schemas import UpcomingPaymentItem, UpcomingSourceType
from tests._hassas_alan import rol_gizli
from tests._proje_ekibi import ekibe_ekle
from tests.modules.treasury.conftest import _auth

H = HiddenCategory
_PAROLA = "parola1234"


async def test_yaklasan_odeme_satiri_KENDI_projesindeki_rolle_maskelenir(
    client, seeded_db, user_factory, project_factory, fatura_fabrikasi
):
    proje_a = await project_factory("UP-A", name="Proje A")
    proje_b = await project_factory("UP-B", name="Proje B")
    vade = today() + timedelta(days=5)
    kw = dict(
        direction=InvoiceDirection.incoming,
        status=InvoiceStatus.approved,
        due_date=vade,
        total="1000.00",
    )
    fatura_a = await fatura_fabrikasi(project=proje_a, **kw)
    fatura_b = await fatura_fabrikasi(project=proje_b, **kw)
    r_ana = await rol_gizli(seeded_db, "up_ana", set())
    r_a = await rol_gizli(seeded_db, "up_a", set())
    r_b = await rol_gizli(seeded_db, "up_b", {H.banka_kasa})
    user = await user_factory(email="up@b4b.co", password=_PAROLA, role_key=r_ana.key)
    await ekibe_ekle(seeded_db, user, proje_a.id, r_a.id)
    await ekibe_ekle(seeded_db, user, proje_b.id, r_b.id)
    giris = await client.post("/auth/login", json={"email": "up@b4b.co", "password": _PAROLA})
    basliklar = _auth(giris.json()["access_token"])

    yanit = await client.get("/treasury/upcoming-payments?days=30", headers=basliklar)

    assert yanit.status_code == 200, yanit.text
    satirlar = {s["source_id"]: s for s in yanit.json()["items"]}
    assert satirlar[str(fatura_a.id)]["project_id"] == str(proje_a.id)
    assert satirlar[str(fatura_a.id)]["amount"] is not None  # POZİTİF KONTROL: A açık
    assert satirlar[str(fatura_b.id)]["amount"] is None  # B: banka_kasa gizli


def _satir(kaynak: UpcomingSourceType) -> UpcomingPaymentItem:
    return UpcomingPaymentItem(
        source_type=kaynak,
        source_id=uuid.uuid4(),
        counterparty=None,
        document_no="2026-07",
        due_date=today(),
        days_remaining=3,
        amount=Decimal("123456.00"),
    )


def test_bordro_kaynakli_satir_maas_kisisel_ile_de_gizlenir() -> None:
    """Saf maske: yalnız `maas_kisisel` gizliyken bordro satırı `null`, fatura satırı AÇIK."""
    kumeler = MaskeKumeleri(varsayilan=frozenset({H.maas_kisisel}))

    bordro = maskele(_satir(UpcomingSourceType.payroll), kumeler)
    fatura = maskele(_satir(UpcomingSourceType.invoice), kumeler)

    assert bordro.amount is None
    assert fatura.amount == Decimal("123456.00")  # POZİTİF KONTROL
