"""FAT-1 T3 — PATCH · DELETE · `PUT lines` (spec §7 md. 5, 6, 7).

## Bu dosyanın kilitlediği kararlar

1. **Düzenleme kapısı DURUMDUR, yetki değil → 409.** Giden fatura yalnız
   `draft`ta, gelen fatura yalnız `pending`te düzenlenir; karar tek kaynaktan
   (`transitions.EDITABLE_STATUS`) okunur, uçlar kendi `if status == …`
   denetimini YAZMAZ.
2. **Gelen faturada PATCH yalnız `note`/`due_date`/`payment_method`e dokunur**
   (422 aksi hâlde): gelen fatura satıcının belgesidir, tutarını biz
   düzeltemeyiz.
3. **`DELETE` yalnız `admin`** — `full` (muhasebe) 403 alır; `full` silmeyi
   KAPSAMAZ (repo kanonu). `draft` dışı fatura 409'dur.
4. **`PUT lines` kalem kümesini TOPTAN yazar**; `sort_order` dizinin indeksidir
   ve `line_total` sunucunun hesabıdır — ikisi de gövdeden GELEMEZ (422).
5. Her yazma yolundan sonra başlık toplamları `amounts.py`den YENİDEN hesaplanır:
   oran değişimi tutarı sessizce eski değerde bırakmaz.
"""

import uuid
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.modules.invoicing.models import Invoice, InvoiceDirection, InvoiceLine, InvoiceStatus
from tests._silme_yardimci import sil_aile

_YOL = "/invoices"

_KALEM = {
    "description": "Kaba İnşaat İmalatı",
    "unit": "m³",
    "quantity": "100.000",
    "unit_price": "1000.00",
    "vat_rate": "20.00",
}


# --- Uç 5: PATCH ---


async def test_patch_taslak_giden_fatura_alanlari_gunceller(
    client, muhasebe_headers, fatura_fabrikasi, gorunen_proje
) -> None:
    fatura = await fatura_fabrikasi(project=gorunen_proje)
    resp = await client.patch(
        f"{_YOL}/{fatura.id}",
        headers=muhasebe_headers,
        json={"party_name": "Çelik Holding A.Ş.", "due_date": "2026-08-18"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["party_name"] == "Çelik Holding A.Ş."
    assert resp.json()["due_date"] == "2026-08-18"


async def test_patch_oran_degisimi_toplamlari_YENIDEN_hesaplar(
    client, muhasebe_headers, fatura_fabrikasi, gorunen_proje
) -> None:
    """Kesinti oranı değişince tutar eski değerinde KALMAZ (tek kaynak `amounts`)."""
    fatura = await fatura_fabrikasi(project=gorunen_proje)
    resp = await client.patch(
        f"{_YOL}/{fatura.id}", headers=muhasebe_headers, json={"advance_rate": "20.00"}
    )
    assert resp.status_code == 200, resp.text
    govde = resp.json()
    assert Decimal(govde["advance_amount"]) == Decimal("20000.00")
    assert Decimal(govde["tax_base"]) == Decimal("80000.00")
    assert Decimal(govde["vat_amount"]) == Decimal("16000.00")
    assert Decimal(govde["total"]) == Decimal("96000.00")


async def test_patch_gonderilmis_giden_fatura_409(
    client, muhasebe_headers, fatura_fabrikasi, gorunen_proje
) -> None:
    fatura = await fatura_fabrikasi(project=gorunen_proje, status=InvoiceStatus.sent)
    resp = await client.patch(
        f"{_YOL}/{fatura.id}", headers=muhasebe_headers, json={"party_name": "Yeni"}
    )
    assert resp.status_code == 409, resp.text


async def test_patch_gelen_fatura_pending_uc_alan_serbest(
    client, muhasebe_headers, fatura_fabrikasi, gorunen_proje
) -> None:
    fatura = await fatura_fabrikasi(
        project=gorunen_proje, direction=InvoiceDirection.incoming, status=InvoiceStatus.pending
    )
    resp = await client.patch(
        f"{_YOL}/{fatura.id}",
        headers=muhasebe_headers,
        json={"due_date": "2026-08-18", "payment_method": "transfer", "note": "Kontrol edildi"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["payment_method"] == "transfer"


async def test_patch_gelen_faturada_baska_alan_422(
    client, muhasebe_headers, fatura_fabrikasi, gorunen_proje
) -> None:
    fatura = await fatura_fabrikasi(
        project=gorunen_proje, direction=InvoiceDirection.incoming, status=InvoiceStatus.pending
    )
    resp = await client.patch(
        f"{_YOL}/{fatura.id}", headers=muhasebe_headers, json={"party_name": "Yeni Satıcı"}
    )
    assert resp.status_code == 422, resp.text


async def test_patch_onaylanmis_gelen_fatura_409(
    client, muhasebe_headers, fatura_fabrikasi, gorunen_proje
) -> None:
    fatura = await fatura_fabrikasi(
        project=gorunen_proje, direction=InvoiceDirection.incoming, status=InvoiceStatus.approved
    )
    resp = await client.patch(
        f"{_YOL}/{fatura.id}", headers=muhasebe_headers, json={"note": "geç kalmış düzeltme"}
    )
    assert resp.status_code == 409, resp.text


async def test_patch_gorunmeyen_fatura_404(
    client, muhasebe_headers, fatura_fabrikasi, gorunmeyen_proje
) -> None:
    fatura = await fatura_fabrikasi(project=gorunmeyen_proje)
    resp = await client.patch(
        f"{_YOL}/{fatura.id}", headers=muhasebe_headers, json={"note": "sızıntı denemesi"}
    )
    assert resp.status_code == 404, resp.text


@pytest.mark.parametrize("alan", ["invoice_no", "direction", "status", "total", "subtotal"])
async def test_patch_govdesinde_YASAK_alanlar_422(
    client, muhasebe_headers, fatura_fabrikasi, gorunen_proje, alan: str
) -> None:
    fatura = await fatura_fabrikasi(project=gorunen_proje)
    resp = await client.patch(f"{_YOL}/{fatura.id}", headers=muhasebe_headers, json={alan: "1"})
    assert resp.status_code == 422, f"{alan} sessizce yok sayıldı: {resp.text}"


async def test_patch_not_tavani_TEK_SABITTEN(
    client, muhasebe_headers, fatura_fabrikasi, gorunen_proje
) -> None:
    from app.core.text import FREE_TEXT_MAX_LENGTH

    fatura = await fatura_fabrikasi(project=gorunen_proje)
    resp = await client.patch(
        f"{_YOL}/{fatura.id}",
        headers=muhasebe_headers,
        json={"note": "x" * (FREE_TEXT_MAX_LENGTH + 1)},
    )
    assert resp.status_code == 422, resp.text


async def test_patch_gorunmeyen_kaynak_referansi_404(
    client, muhasebe_headers, fatura_fabrikasi, gorunen_proje, gorunmeyen_siparis
) -> None:
    fatura = await fatura_fabrikasi(project=gorunen_proje)
    resp = await client.patch(
        f"{_YOL}/{fatura.id}",
        headers=muhasebe_headers,
        json={"purchase_order_id": str(gorunmeyen_siparis.id)},
    )
    assert resp.status_code == 404, resp.text


async def test_patch_yalniz_full_pm_403(
    client, pm_headers, fatura_fabrikasi, gorunen_proje
) -> None:
    fatura = await fatura_fabrikasi(project=gorunen_proje)
    resp = await client.patch(f"{_YOL}/{fatura.id}", headers=pm_headers, json={"note": "olmaz"})
    assert resp.status_code == 403, resp.text


# --- Uç 6: DELETE ---


async def test_delete_admin_taslak_faturayi_siler(
    client, admin_headers, fatura_fabrikasi, gorunen_proje, seeded_db
) -> None:
    fatura = await fatura_fabrikasi(project=gorunen_proje)
    fatura_id = fatura.id
    resp = await sil_aile(client, admin_headers, "invoice", fatura_id)
    assert resp.status_code == 204, resp.text
    kalan = (
        await seeded_db.execute(select(Invoice).where(Invoice.id == fatura_id))
    ).scalar_one_or_none()
    assert kalan is None
    kalemler = (
        (await seeded_db.execute(select(InvoiceLine).where(InvoiceLine.invoice_id == fatura_id)))
        .scalars()
        .all()
    )
    assert kalemler == []


async def test_delete_full_seviyesi_403(
    client, muhasebe_headers, fatura_fabrikasi, gorunen_proje
) -> None:
    """`full` silmeyi KAPSAMAZ — muhasebe faturayı düzenler ama silemez."""
    fatura = await fatura_fabrikasi(project=gorunen_proje)
    resp = await client.delete(f"{_YOL}/{fatura.id}", headers=muhasebe_headers)
    assert resp.status_code == 403, resp.text


async def test_delete_gonderilmis_fatura_da_silinir_sisyon_her_kosulda(
    client, admin_headers, fatura_fabrikasi, gorunen_proje, seeded_db
) -> None:
    """SIL-B2: "yalnız taslak" iş kuralı yalnız silme yolunda kalktı (K2)."""
    fatura = await fatura_fabrikasi(project=gorunen_proje, status=InvoiceStatus.sent)
    fatura_id = fatura.id
    resp = await sil_aile(client, admin_headers, "invoice", fatura_id)
    assert resp.status_code == 204, resp.text
    assert await seeded_db.get(Invoice, fatura_id) is None


async def test_delete_gelen_fatura_da_silinir(
    client, admin_headers, fatura_fabrikasi, gorunen_proje, seeded_db
) -> None:
    fatura = await fatura_fabrikasi(
        project=gorunen_proje, direction=InvoiceDirection.incoming, status=InvoiceStatus.pending
    )
    fatura_id = fatura.id
    resp = await sil_aile(client, admin_headers, "invoice", fatura_id)
    assert resp.status_code == 204, resp.text
    assert await seeded_db.get(Invoice, fatura_id) is None


async def test_delete_onizlemesiz_428(
    client, admin_headers, fatura_fabrikasi, gorunen_proje
) -> None:
    fatura = await fatura_fabrikasi(project=gorunen_proje)
    resp = await client.delete(f"{_YOL}/{fatura.id}", headers=admin_headers)
    assert resp.status_code == 428, resp.text
    assert resp.json()["code"] == "preview_required"


async def test_delete_olmayan_fatura_404(client, admin_headers) -> None:
    resp = await client.delete(
        f"{_YOL}/{uuid.uuid4()}", params={"preview_token": "x"}, headers=admin_headers
    )
    assert resp.status_code == 404, resp.text


# --- Uç 7: PUT lines ---


async def test_put_lines_kalemleri_TOPTAN_yazar(
    client, muhasebe_headers, fatura_fabrikasi, gorunen_proje
) -> None:
    fatura = await fatura_fabrikasi(project=gorunen_proje, lines=[("Eski", "1.000", "10.00")])
    resp = await client.put(
        f"{_YOL}/{fatura.id}/lines",
        headers=muhasebe_headers,
        json={
            "lines": [
                dict(_KALEM) | {"description": "Yeni A"},
                dict(_KALEM) | {"description": "Yeni B", "quantity": "2.000"},
            ]
        },
    )
    assert resp.status_code == 200, resp.text
    kalemler = resp.json()["lines"]
    assert [k["description"] for k in kalemler] == ["Yeni A", "Yeni B"]
    assert [k["sort_order"] for k in kalemler] == [0, 1]
    assert Decimal(kalemler[1]["line_total"]) == Decimal("2000.00")


async def test_put_lines_baslik_toplamlarini_yeniden_hesaplar(
    client, muhasebe_headers, fatura_fabrikasi, gorunen_proje
) -> None:
    fatura = await fatura_fabrikasi(project=gorunen_proje)
    resp = await client.put(
        f"{_YOL}/{fatura.id}/lines",
        headers=muhasebe_headers,
        json={"lines": [dict(_KALEM) | {"quantity": "3.000", "unit_price": "100.00"}]},
    )
    assert resp.status_code == 200, resp.text
    govde = resp.json()
    assert Decimal(govde["subtotal"]) == Decimal("300.00")
    assert Decimal(govde["vat_amount"]) == Decimal("60.00")
    assert Decimal(govde["total"]) == Decimal("360.00")


async def test_put_lines_bos_liste_hepsini_siler(
    client, muhasebe_headers, fatura_fabrikasi, gorunen_proje
) -> None:
    fatura = await fatura_fabrikasi(project=gorunen_proje)
    resp = await client.put(
        f"{_YOL}/{fatura.id}/lines", headers=muhasebe_headers, json={"lines": []}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["lines"] == []
    assert Decimal(resp.json()["total"]) == Decimal("0.00")


@pytest.mark.parametrize("alan,deger", [("line_total", "1.00"), ("sort_order", 5)])
async def test_put_lines_govdede_line_total_ve_sort_order_422(
    client, muhasebe_headers, fatura_fabrikasi, gorunen_proje, alan: str, deger
) -> None:
    fatura = await fatura_fabrikasi(project=gorunen_proje)
    resp = await client.put(
        f"{_YOL}/{fatura.id}/lines",
        headers=muhasebe_headers,
        json={"lines": [dict(_KALEM) | {alan: deger}]},
    )
    assert resp.status_code == 422, f"{alan} sessizce yok sayıldı: {resp.text}"


async def test_put_lines_aciklama_tavani_TEK_SABITTEN(
    client, muhasebe_headers, fatura_fabrikasi, gorunen_proje
) -> None:
    from app.core.text import FREE_TEXT_MAX_LENGTH

    fatura = await fatura_fabrikasi(project=gorunen_proje)
    resp = await client.put(
        f"{_YOL}/{fatura.id}/lines",
        headers=muhasebe_headers,
        json={"lines": [dict(_KALEM) | {"description": "z" * (FREE_TEXT_MAX_LENGTH + 1)}]},
    )
    assert resp.status_code == 422, resp.text


async def test_put_lines_taslak_disinda_409(
    client, muhasebe_headers, fatura_fabrikasi, gorunen_proje
) -> None:
    fatura = await fatura_fabrikasi(project=gorunen_proje, status=InvoiceStatus.sent)
    resp = await client.put(
        f"{_YOL}/{fatura.id}/lines", headers=muhasebe_headers, json={"lines": [dict(_KALEM)]}
    )
    assert resp.status_code == 409, resp.text


async def test_put_lines_gelen_faturada_409(
    client, muhasebe_headers, fatura_fabrikasi, gorunen_proje
) -> None:
    """Kalem kümesi yalnız `draft`ta yazılır (§7 md.7) — gelen faturada `draft` yoktur."""
    fatura = await fatura_fabrikasi(
        project=gorunen_proje, direction=InvoiceDirection.incoming, status=InvoiceStatus.pending
    )
    resp = await client.put(
        f"{_YOL}/{fatura.id}/lines", headers=muhasebe_headers, json={"lines": [dict(_KALEM)]}
    )
    assert resp.status_code == 409, resp.text


async def test_put_lines_gorunmeyen_fatura_404(
    client, muhasebe_headers, fatura_fabrikasi, gorunmeyen_proje
) -> None:
    fatura = await fatura_fabrikasi(project=gorunmeyen_proje)
    resp = await client.put(
        f"{_YOL}/{fatura.id}/lines", headers=muhasebe_headers, json={"lines": [dict(_KALEM)]}
    )
    assert resp.status_code == 404, resp.text


async def test_put_lines_yalniz_full_pm_403(
    client, pm_headers, fatura_fabrikasi, gorunen_proje
) -> None:
    fatura = await fatura_fabrikasi(project=gorunen_proje)
    resp = await client.put(
        f"{_YOL}/{fatura.id}/lines", headers=pm_headers, json={"lines": [dict(_KALEM)]}
    )
    assert resp.status_code == 403, resp.text
