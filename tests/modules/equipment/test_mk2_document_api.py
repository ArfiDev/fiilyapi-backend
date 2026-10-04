"""MK-2 T4 — ekipman belgesi uçları (spec §2.3/§4, K7).

Kapı `equipment` iznidir; okuma `view`, yazma `full`. Görünmeyen ekipmanın
belgesi 404'tür (K9/K20 — `tests/modules/equipment/conftest.py` fixture'ları).
"""

from datetime import timedelta

from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import timezone
from app.modules.equipment.models import EquipmentDocument, EquipmentDocumentType

PDF = b"%PDF-1.4 sahte muayene raporu"


def _multipart(filename: str = "muayene.pdf", content: bytes = PDF) -> dict:
    return {"file": (filename, content, "application/pdf")}


_TYPE_SEED = (
    ("invoice_or_contract", "Fatura / Kira Sözleşmesi", True, 1),
    ("periodic_inspection", "Periyodik Muayene Raporu", True, 2),
    ("ce_certificate", "CE Belgesi / Uygunluk", False, 3),
    ("manual", "Kullanım Kılavuzu", False, 4),
    ("insurance_policy", "Sigorta Poliçesi", False, 5),
    ("delivery_photos", "Teslim Fotoğrafları", False, 6),
)


async def _seed_types(seeded_db: AsyncSession) -> dict[str, EquipmentDocumentType]:
    """Test DB'si `Base.metadata.create_all`den kurulur — alembic seed'i
    (migration `f9a0b1c2d3e4`) BURADA ÇALIŞMAZ; altı tip elle kurulur
    (`PersonnelDocumentType` test deseninin birebiri)."""
    existing = {t.code: t for t in (await seeded_db.scalars(select(EquipmentDocumentType))).all()}
    if existing:
        return existing
    for code, name, is_required, sort_order in _TYPE_SEED:
        seeded_db.add(
            EquipmentDocumentType(
                code=code, name=name, is_required=is_required, sort_order=sort_order
            )
        )
    await seeded_db.flush()
    return {t.code: t for t in (await seeded_db.scalars(select(EquipmentDocumentType))).all()}


# --- Rota sırası bekçisi ---------------------------------------------------


async def test_rota_sirasi_document_types_UUID_SANILMAZ(
    client: AsyncClient, seeded_db: AsyncSession, admin_headers
) -> None:
    """🔴 `/equipment/document-types` `/equipment/{equipment_id}`e YAKALANMAMALI.

    Sıra bozulsaydı "document-types" bir UUID sanılır ve 422 dönerdi.
    """
    await _seed_types(seeded_db)
    resp = await client.get("/equipment/document-types", headers=admin_headers)
    assert resp.status_code == 200, resp.text
    assert len(resp.json()["items"]) == 6


# --- Tip kataloğu -----------------------------------------------------------


async def test_altı_sabit_tip_seed_edilmis(
    client: AsyncClient, seeded_db: AsyncSession, admin_headers
) -> None:
    await _seed_types(seeded_db)
    resp = await client.get("/equipment/document-types", headers=admin_headers)
    items = resp.json()["items"]
    codes = {i["code"] for i in items}
    assert codes == {
        "invoice_or_contract",
        "periodic_inspection",
        "ce_certificate",
        "manual",
        "insurance_policy",
        "delivery_photos",
    }
    required = {i["code"] for i in items if i["is_required"]}
    assert required == {"invoice_or_contract", "periodic_inspection"}


# --- Yükleme -----------------------------------------------------------------


async def test_belge_yuklenir(
    client: AsyncClient, seeded_db: AsyncSession, ekipman_fabrikasi, admin_headers
) -> None:
    ekipman = await ekipman_fabrikasi("Kule Vinç KV-01")
    types = await _seed_types(seeded_db)

    resp = await client.post(
        f"/equipment/{ekipman.id}/documents",
        data={"type_id": str(types["periodic_inspection"].id), "valid_until": "2027-01-01"},
        files=_multipart(),
        headers=admin_headers,
    )

    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["filename"] == "muayene.pdf"
    assert body["mime_type"] == "application/pdf"
    assert body["size_bytes"] == len(PDF)
    assert body["type_code"] == "periodic_inspection"
    assert body["valid_until"] == "2027-01-01"


async def test_yuklenen_baytlar_dogru_saklanir(
    client: AsyncClient, seeded_db: AsyncSession, ekipman_fabrikasi, admin_headers
) -> None:
    ekipman = await ekipman_fabrikasi("Kule Vinç KV-01")
    types = await _seed_types(seeded_db)

    resp = await client.post(
        f"/equipment/{ekipman.id}/documents",
        data={"type_id": str(types["manual"].id)},
        files=_multipart(),
        headers=admin_headers,
    )
    doc_id = resp.json()["id"]

    seeded_db.expire_all()
    stored = await seeded_db.get(EquipmentDocument, doc_id)
    assert stored.content == PDF


async def test_desteklenmeyen_uzanti_422(
    client: AsyncClient, seeded_db: AsyncSession, ekipman_fabrikasi, admin_headers
) -> None:
    ekipman = await ekipman_fabrikasi("Kule Vinç KV-01")
    types = await _seed_types(seeded_db)

    resp = await client.post(
        f"/equipment/{ekipman.id}/documents",
        data={"type_id": str(types["manual"].id)},
        files=_multipart(filename="virus.exe"),
        headers=admin_headers,
    )
    assert resp.status_code == 422, resp.text


async def test_gecersiz_belge_tipi_422(
    client: AsyncClient, seeded_db: AsyncSession, ekipman_fabrikasi, admin_headers
) -> None:
    ekipman = await ekipman_fabrikasi("Kule Vinç KV-01")

    resp = await client.post(
        f"/equipment/{ekipman.id}/documents",
        data={"type_id": "00000000-0000-0000-0000-000000000000"},
        files=_multipart(),
        headers=admin_headers,
    )
    assert resp.status_code == 422, resp.text


async def test_boyut_tavani_asilirsa_413(
    client: AsyncClient, seeded_db: AsyncSession, ekipman_fabrikasi, admin_headers, monkeypatch
) -> None:
    from app.core.config import settings

    monkeypatch.setattr(settings, "document_max_bytes", 10)
    ekipman = await ekipman_fabrikasi("Kule Vinç KV-01")
    types = await _seed_types(seeded_db)

    resp = await client.post(
        f"/equipment/{ekipman.id}/documents",
        data={"type_id": str(types["manual"].id)},
        files=_multipart(content=b"0123456789ABCDEF-cok-uzun-icerik"),
        headers=admin_headers,
    )
    assert resp.status_code == 413, resp.text


async def test_gorunmeyen_ekipmana_yukleme_404(
    client: AsyncClient,
    seeded_db: AsyncSession,
    ekipman_fabrikasi,
    gorunmeyen_santiye,
    sef_headers,
) -> None:
    ekipman = await ekipman_fabrikasi("Marina Vinç", site=gorunmeyen_santiye)
    types = await _seed_types(seeded_db)

    resp = await client.post(
        f"/equipment/{ekipman.id}/documents",
        data={"type_id": str(types["manual"].id)},
        files=_multipart(),
        headers=sef_headers,
    )
    assert resp.status_code == 404, resp.text


async def test_okuma_yetkisi_yazamaz_403(
    client: AsyncClient, seeded_db: AsyncSession, ekipman_fabrikasi, muhendis_headers
) -> None:
    ekipman = await ekipman_fabrikasi("Kule Vinç KV-01")
    types = await _seed_types(seeded_db)

    resp = await client.post(
        f"/equipment/{ekipman.id}/documents",
        data={"type_id": str(types["manual"].id)},
        files=_multipart(),
        headers=muhendis_headers,
    )
    assert resp.status_code == 403, resp.text


# --- Liste -------------------------------------------------------------------


async def test_belge_listesi(
    client: AsyncClient, seeded_db: AsyncSession, ekipman_fabrikasi, admin_headers
) -> None:
    ekipman = await ekipman_fabrikasi("Kule Vinç KV-01")
    types = await _seed_types(seeded_db)
    await client.post(
        f"/equipment/{ekipman.id}/documents",
        data={"type_id": str(types["manual"].id)},
        files=_multipart(),
        headers=admin_headers,
    )

    resp = await client.get(f"/equipment/{ekipman.id}/documents", headers=admin_headers)
    assert resp.status_code == 200, resp.text
    items = resp.json()["items"]
    assert len(items) == 1
    assert items[0]["type_code"] == "manual"
    assert "content" not in items[0]


async def test_gorunmeyen_ekipmanin_listesi_404(
    client: AsyncClient,
    seeded_db: AsyncSession,
    ekipman_fabrikasi,
    gorunmeyen_santiye,
    sef_headers,
) -> None:
    ekipman = await ekipman_fabrikasi("Marina Vinç", site=gorunmeyen_santiye)
    resp = await client.get(f"/equipment/{ekipman.id}/documents", headers=sef_headers)
    assert resp.status_code == 404, resp.text


# --- İndirme / Silme -----------------------------------------------------------


async def test_belge_indirilir_nosniff_ile(
    client: AsyncClient, seeded_db: AsyncSession, ekipman_fabrikasi, admin_headers
) -> None:
    ekipman = await ekipman_fabrikasi("Kule Vinç KV-01")
    types = await _seed_types(seeded_db)
    create_resp = await client.post(
        f"/equipment/{ekipman.id}/documents",
        data={"type_id": str(types["manual"].id)},
        files=_multipart(),
        headers=admin_headers,
    )
    doc_id = create_resp.json()["id"]

    resp = await client.get(f"/equipment/documents/{doc_id}/download", headers=admin_headers)
    assert resp.status_code == 200, resp.text
    assert resp.content == PDF
    assert resp.headers["x-content-type-options"] == "nosniff"
    assert resp.headers["content-type"] == "application/pdf"
    assert "attachment" in resp.headers["content-disposition"]


async def test_gorunmeyen_ekipmanin_belgesi_indirilemez_404(
    client: AsyncClient,
    seeded_db: AsyncSession,
    ekipman_fabrikasi,
    gorunmeyen_santiye,
    admin_headers,
    sef_headers,
) -> None:
    ekipman = await ekipman_fabrikasi("Marina Vinç", site=gorunmeyen_santiye)
    types = await _seed_types(seeded_db)
    create_resp = await client.post(
        f"/equipment/{ekipman.id}/documents",
        data={"type_id": str(types["manual"].id)},
        files=_multipart(),
        headers=admin_headers,
    )
    doc_id = create_resp.json()["id"]

    resp = await client.get(f"/equipment/documents/{doc_id}/download", headers=sef_headers)
    assert resp.status_code == 404, resp.text


async def test_belge_silinir(
    client: AsyncClient, seeded_db: AsyncSession, ekipman_fabrikasi, admin_headers
) -> None:
    ekipman = await ekipman_fabrikasi("Kule Vinç KV-01")
    types = await _seed_types(seeded_db)
    create_resp = await client.post(
        f"/equipment/{ekipman.id}/documents",
        data={"type_id": str(types["manual"].id)},
        files=_multipart(),
        headers=admin_headers,
    )
    doc_id = create_resp.json()["id"]

    resp = await client.delete(f"/equipment/documents/{doc_id}", headers=admin_headers)
    assert resp.status_code == 204, resp.text

    again = await client.get(f"/equipment/documents/{doc_id}/download", headers=admin_headers)
    assert again.status_code == 404, again.text


async def test_var_olmayan_belge_404(
    client: AsyncClient, seeded_db: AsyncSession, admin_headers
) -> None:
    resp = await client.get(
        "/equipment/documents/00000000-0000-0000-0000-000000000000/download",
        headers=admin_headers,
    )
    assert resp.status_code == 404, resp.text


# --- Özet (K7) ----------------------------------------------------------------


async def _upload(
    client: AsyncClient,
    equipment_id,
    type_id,
    headers,
    *,
    valid_until: str | None,
    filename: str = "belge.pdf",
) -> str:
    data = {"type_id": str(type_id)}
    if valid_until is not None:
        data["valid_until"] = valid_until
    resp = await client.post(
        f"/equipment/{equipment_id}/documents",
        data=data,
        files=_multipart(filename=filename),
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


async def test_ozet_sinir_gunleri(
    client: AsyncClient, seeded_db: AsyncSession, ekipman_fabrikasi, admin_headers
) -> None:
    """K7: `expiring_soon` = bugün ≤ valid_until ≤ bugün+30; `expired` = valid_until < bugün.

    Dört sınır günü: bugün (expiring) · +30 (expiring) · +31 (ne ikisi de değil)
    · dün (expired).
    """
    ekipman = await ekipman_fabrikasi("Kule Vinç KV-01")
    types = await _seed_types(seeded_db)
    manual_id = types["manual"].id
    today = timezone.today()

    await _upload(
        client,
        ekipman.id,
        manual_id,
        admin_headers,
        valid_until=today.isoformat(),
        filename="bugun.pdf",
    )
    await _upload(
        client,
        ekipman.id,
        manual_id,
        admin_headers,
        valid_until=(today + timedelta(days=30)).isoformat(),
        filename="artı30.pdf",
    )
    await _upload(
        client,
        ekipman.id,
        manual_id,
        admin_headers,
        valid_until=(today + timedelta(days=31)).isoformat(),
        filename="artı31.pdf",
    )
    await _upload(
        client,
        ekipman.id,
        manual_id,
        admin_headers,
        valid_until=(today - timedelta(days=1)).isoformat(),
        filename="dun.pdf",
    )

    resp = await client.get("/equipment/documents/summary", headers=admin_headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["expiring_soon"] == 2, body
    assert body["expired"] == 1, body


async def test_ozet_zorunlu_tip_eksikleri(
    client: AsyncClient, seeded_db: AsyncSession, ekipman_fabrikasi, admin_headers
) -> None:
    """`missing` yalnız ZORUNLU tipler (İK-1 semantiği) — burada iki zorunlu tip
    var, hiçbiri yüklenmedi → bir aktif ekipman × iki zorunlu tip = 2."""
    await _seed_types(seeded_db)
    await ekipman_fabrikasi("Boş Ekipman")

    resp = await client.get("/equipment/documents/summary", headers=admin_headers)
    assert resp.status_code == 200, resp.text
    assert resp.json()["missing"] == 2


async def test_ozet_zorunlu_tip_yuklenince_eksik_dusuyor(
    client: AsyncClient, seeded_db: AsyncSession, ekipman_fabrikasi, admin_headers
) -> None:
    ekipman = await ekipman_fabrikasi("Dolu Ekipman")
    types = await _seed_types(seeded_db)
    await _upload(
        client,
        ekipman.id,
        types["invoice_or_contract"].id,
        admin_headers,
        valid_until=None,
        filename="fatura.pdf",
    )
    await _upload(
        client,
        ekipman.id,
        types["periodic_inspection"].id,
        admin_headers,
        valid_until=None,
        filename="muayene.pdf",
    )

    resp = await client.get("/equipment/documents/summary", headers=admin_headers)
    assert resp.json()["missing"] == 0


# --- FRM-1: document_no / issued_at / note + PATCH ----------------------------
#
# 🔴 Bu bloğun TAMAMI API ucundan geçer (POST = multipart Form, PATCH = JSON
# gövde). Modeli doğrudan kuran bir test şema katmanını ASLA sınamaz — bu
# dilimin bilinen ana riski.


async def test_post_uc_yeni_alani_kaydeder(
    client: AsyncClient, seeded_db: AsyncSession, ekipman_fabrikasi, admin_headers
) -> None:
    """POST multipart Form alanları: `document_no` · `issued_at` · `note`."""
    ekipman = await ekipman_fabrikasi("Kule Vinç KV-01")
    types = await _seed_types(seeded_db)

    resp = await client.post(
        f"/equipment/{ekipman.id}/documents",
        data={
            "type_id": str(types["periodic_inspection"].id),
            "document_no": "TC-48-MUA-2026",
            "issued_at": "2026-01-15",
            "note": "Yıllık periyodik muayene.",
            "valid_until": "2027-01-01",
        },
        files=_multipart(),
        headers=admin_headers,
    )

    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["document_no"] == "TC-48-MUA-2026"
    assert body["issued_at"] == "2026-01-15"
    assert body["note"] == "Yıllık periyodik muayene."
    assert body["valid_until"] == "2027-01-01"

    liste = await client.get(f"/equipment/{ekipman.id}/documents", headers=admin_headers)
    (satir,) = liste.json()["items"]
    assert satir["document_no"] == "TC-48-MUA-2026"
    assert satir["issued_at"] == "2026-01-15"
    assert satir["note"] == "Yıllık periyodik muayene."


async def test_post_yeni_alanlar_hic_gecilmezse_null_kalir(
    client: AsyncClient, seeded_db: AsyncSession, ekipman_fabrikasi, admin_headers
) -> None:
    """🔴 VARSAYILAN YOL: üç alan HİÇ gönderilmez — mevcut davranış bozulmaz."""
    ekipman = await ekipman_fabrikasi("Kule Vinç KV-01")
    types = await _seed_types(seeded_db)

    resp = await client.post(
        f"/equipment/{ekipman.id}/documents",
        data={"type_id": str(types["manual"].id)},
        files=_multipart(),
        headers=admin_headers,
    )

    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["document_no"] is None
    assert body["issued_at"] is None
    assert body["note"] is None
    assert body["valid_until"] is None

    seeded_db.expire_all()
    stored = await seeded_db.get(EquipmentDocument, body["id"])
    assert stored.document_no is None
    assert stored.issued_at is None
    assert stored.note is None


async def _upload_for_patch(client, seeded_db, ekipman_fabrikasi, headers, **form) -> tuple:
    ekipman = await ekipman_fabrikasi("Kule Vinç KV-01")
    types = await _seed_types(seeded_db)
    data = {"type_id": str(types["periodic_inspection"].id), **form}
    resp = await client.post(
        f"/equipment/{ekipman.id}/documents",
        data=data,
        files=_multipart(),
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    return ekipman, resp.json()["id"]


async def test_patch_dort_alani_gunceller(
    client: AsyncClient, seeded_db: AsyncSession, ekipman_fabrikasi, admin_headers
) -> None:
    _ekipman, doc_id = await _upload_for_patch(client, seeded_db, ekipman_fabrikasi, admin_headers)

    resp = await client.patch(
        f"/equipment/documents/{doc_id}",
        json={
            "document_no": "TC-48-MUA-2026",
            "issued_at": "2026-02-01",
            "note": "Düzeltildi.",
            "valid_until": "2027-02-01",
        },
        headers=admin_headers,
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["document_no"] == "TC-48-MUA-2026"
    assert body["issued_at"] == "2026-02-01"
    assert body["note"] == "Düzeltildi."
    assert body["valid_until"] == "2027-02-01"
    # Künye alanları PATCH kapsamı DIŞINDA — değişmemiş olmalı.
    assert body["filename"] == "muayene.pdf"
    assert body["type_code"] == "periodic_inspection"


async def test_patch_gonderilmeyen_alana_DOKUNULMAZ(
    client: AsyncClient, seeded_db: AsyncSession, ekipman_fabrikasi, admin_headers
) -> None:
    """`exclude_unset`: "gönderilmedi" ≠ "null gönderildi" (birinci yarı)."""
    _ekipman, doc_id = await _upload_for_patch(
        client,
        seeded_db,
        ekipman_fabrikasi,
        admin_headers,
        document_no="ESKI-NO",
        issued_at="2026-01-01",
        note="eski not",
        valid_until="2027-01-01",
    )

    resp = await client.patch(
        f"/equipment/documents/{doc_id}",
        json={"note": "yeni not"},
        headers=admin_headers,
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["note"] == "yeni not"
    assert body["document_no"] == "ESKI-NO"
    assert body["issued_at"] == "2026-01-01"
    assert body["valid_until"] == "2027-01-01"


async def test_patch_null_gonderilen_alan_TEMIZLENIR(
    client: AsyncClient, seeded_db: AsyncSession, ekipman_fabrikasi, admin_headers
) -> None:
    """`exclude_unset`: açıkça `null` göndermek alanı temizler (ikinci yarı)."""
    _ekipman, doc_id = await _upload_for_patch(
        client,
        seeded_db,
        ekipman_fabrikasi,
        admin_headers,
        document_no="ESKI-NO",
        issued_at="2026-01-01",
        note="eski not",
        valid_until="2027-01-01",
    )

    resp = await client.patch(
        f"/equipment/documents/{doc_id}",
        json={"document_no": None, "issued_at": None, "note": None, "valid_until": None},
        headers=admin_headers,
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["document_no"] is None
    assert body["issued_at"] is None
    assert body["note"] is None
    assert body["valid_until"] is None


async def test_patch_dosya_kunyesi_DEGISTIRILEMEZ(
    client: AsyncClient, seeded_db: AsyncSession, ekipman_fabrikasi, admin_headers
) -> None:
    """🔴 KAPSAM DAR: dosya adı/mime/tip gövdede gönderilse bile YOK SAYILIR."""
    _ekipman, doc_id = await _upload_for_patch(client, seeded_db, ekipman_fabrikasi, admin_headers)
    types = await _seed_types(seeded_db)

    resp = await client.patch(
        f"/equipment/documents/{doc_id}",
        json={
            "filename": "sahte.exe",
            "mime_type": "application/x-msdownload",
            "type_id": str(types["manual"].id),
            "size_bytes": 1,
            "note": "kapsam denemesi",
        },
        headers=admin_headers,
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["note"] == "kapsam denemesi"
    assert body["filename"] == "muayene.pdf"
    assert body["mime_type"] == "application/pdf"
    assert body["type_code"] == "periodic_inspection"
    assert body["size_bytes"] == len(PDF)

    seeded_db.expire_all()
    stored = await seeded_db.get(EquipmentDocument, doc_id)
    assert stored.content == PDF


async def test_post_document_no_100_karakterde_kabul_101de_422(
    client: AsyncClient, seeded_db: AsyncSession, ekipman_fabrikasi, admin_headers
) -> None:
    """T6 MUTASYON TURUNDA açılan boşluk: POST'un `max_length=100` kapısı
    KÖRDÜ — kaldırıldığında hiçbir test kırılmıyordu.

    🔴 Burada `documents.filename`in aksine İKİNCİ BİR KATMAN YOKTUR: doğrulama
    düşerse 101 karakter doğrudan `varchar(100)` kolonuna gider ve kullanıcı
    düzeltilebilir bir 422 yerine 500 (`StringDataRightTruncation`) görür.
    Sınır `equipment_documents.document_no` kolonuyla AYNI olmalıdır.
    """
    ekipman = await ekipman_fabrikasi("Kule Vinç KV-01")
    types = await _seed_types(seeded_db)
    ortak = {"type_id": str(types["periodic_inspection"].id)}

    tam = await client.post(
        f"/equipment/{ekipman.id}/documents",
        data={**ortak, "document_no": "N" * 100},
        files=_multipart(),
        headers=admin_headers,
    )
    asan = await client.post(
        f"/equipment/{ekipman.id}/documents",
        data={**ortak, "document_no": "N" * 101},
        files=_multipart(),
        headers=admin_headers,
    )

    assert tam.status_code == 201, tam.text
    assert tam.json()["document_no"] == "N" * 100
    assert asan.status_code == 422, asan.text


async def test_patch_document_no_100_karakterde_kabul_101de_422(
    client: AsyncClient, seeded_db: AsyncSession, ekipman_fabrikasi, admin_headers
) -> None:
    """POST'taki ikizin PATCH yarısı. İKİSİ BİRDEN gerekli: tek giriş noktasına
    konan sınır, öteki uçtan girilen sınırsız değeri engellemez (`documents`
    modülünün `description` tavanı emsalinin birebiri)."""
    _ekipman, doc_id = await _upload_for_patch(client, seeded_db, ekipman_fabrikasi, admin_headers)

    tam = await client.patch(
        f"/equipment/documents/{doc_id}",
        json={"document_no": "N" * 100},
        headers=admin_headers,
    )
    asan = await client.patch(
        f"/equipment/documents/{doc_id}",
        json={"document_no": "N" * 101},
        headers=admin_headers,
    )

    assert tam.status_code == 200, tam.text
    assert tam.json()["document_no"] == "N" * 100
    assert asan.status_code == 422, asan.text


async def test_patch_var_olmayan_belge_404(
    client: AsyncClient, seeded_db: AsyncSession, admin_headers
) -> None:
    resp = await client.patch(
        "/equipment/documents/00000000-0000-0000-0000-000000000000",
        json={"note": "x"},
        headers=admin_headers,
    )
    assert resp.status_code == 404, resp.text


async def test_patch_gorunmeyen_ekipmanin_belgesi_404(
    client: AsyncClient,
    seeded_db: AsyncSession,
    ekipman_fabrikasi,
    gorunmeyen_santiye,
    admin_headers,
    sef_headers,
) -> None:
    """IDOR korkuluğu mevcut uçlarla AYNI: 403 değil 404 (varlık sızdırılmaz)."""
    ekipman = await ekipman_fabrikasi("Marina Vinç", site=gorunmeyen_santiye)
    types = await _seed_types(seeded_db)
    create_resp = await client.post(
        f"/equipment/{ekipman.id}/documents",
        data={"type_id": str(types["manual"].id)},
        files=_multipart(),
        headers=admin_headers,
    )
    doc_id = create_resp.json()["id"]

    resp = await client.patch(
        f"/equipment/documents/{doc_id}",
        json={"note": "x"},
        headers=sef_headers,
    )
    assert resp.status_code == 404, resp.text


async def test_patch_okuma_yetkisi_yazamaz_403(
    client: AsyncClient, seeded_db: AsyncSession, ekipman_fabrikasi, admin_headers, muhendis_headers
) -> None:
    _ekipman, doc_id = await _upload_for_patch(client, seeded_db, ekipman_fabrikasi, admin_headers)

    resp = await client.patch(
        f"/equipment/documents/{doc_id}",
        json={"note": "x"},
        headers=muhendis_headers,
    )
    assert resp.status_code == 403, resp.text


async def test_patch_denetim_gunlugu_yazar(
    client: AsyncClient, seeded_db: AsyncSession, ekipman_fabrikasi, admin_headers
) -> None:
    from app.modules.audit.models import AuditAction, AuditLog

    _ekipman, doc_id = await _upload_for_patch(client, seeded_db, ekipman_fabrikasi, admin_headers)

    resp = await client.patch(
        f"/equipment/documents/{doc_id}",
        json={"note": "denetim"},
        headers=admin_headers,
    )
    assert resp.status_code == 200, resp.text

    seeded_db.expire_all()
    rows = (
        await seeded_db.scalars(select(AuditLog).where(AuditLog.action == AuditAction.update))
    ).all()
    assert any("Ekipman belgesi" in r.detail for r in rows), [r.detail for r in rows]


# --- 🔴 K20 KAPSAMI: belge özeti (kayıt 181) --------------------------------
#
# `document_service.build_summary(session, *, today)` imzasında `actor` YOKTU ve
# üç toplu sorgusunun hiçbiri `repository.scope()`tan geçmiyordu. Modülün öteki
# HER okuma yolu kapsamdan geçerken (`list_documents`:57, `_visible_document`:137,
# `visible_equipment` → K20) bu uç geçmiyordu.
#
# Sızıntı yalnız sayaç değildir: `expiring`/`expired` DİZİLERİ `equipment_name`
# ve `type_name` taşır, yani görünmeyen projenin makine adları doğrudan basılır.
#
# ⚠️ `test_equipment_idor.py` kapsam bekçilerini toplar ama oradaki "summary"
# testi `/equipment/summary` ucunundur (MK-1 özeti) — `/equipment/documents/
# summary` BAŞKA bir uçtur ve bekçisi yoktu. Dosya docstring'inin "summary"
# kelimesi bu ucu KAPSAMIYORDU.


async def test_belge_ozeti_gorunmeyen_projenin_ekipmanini_SAYMAZ(
    client: AsyncClient,
    seeded_db: AsyncSession,
    ekipman_fabrikasi,
    admin_headers,
    sef_headers,
    gorunen_santiye,
    gorunmeyen_santiye,
) -> None:
    types = await _seed_types(seeded_db)
    manual_id = types["manual"].id
    today = timezone.today()

    gorunen = await ekipman_fabrikasi("Görünen Vinç", site=gorunen_santiye)
    gizli = await ekipman_fabrikasi("Gizli Ekskavatör", site=gorunmeyen_santiye)
    await _upload(
        client,
        gorunen.id,
        manual_id,
        admin_headers,
        valid_until=(today - timedelta(days=1)).isoformat(),
        filename="gorunen.pdf",
    )
    await _upload(
        client,
        gizli.id,
        manual_id,
        admin_headers,
        valid_until=(today - timedelta(days=1)).isoformat(),
        filename="gizli.pdf",
    )

    resp = await client.get("/equipment/documents/summary", headers=sef_headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert body["expired"] == 1, f"sayaç kapsamdan geçmedi: {body}"
    adlar = [satir["equipment_name"] for satir in body["expired_documents"]]
    assert "Gizli Ekskavatör" not in adlar, f"görünmeyen projenin makine ADI sızdı: {adlar}"
    assert adlar == ["Görünen Vinç"], adlar


async def test_belge_ozeti_eksikleri_de_kapsamdan_gecer(
    client: AsyncClient,
    seeded_db: AsyncSession,
    ekipman_fabrikasi,
    sef_headers,
    gorunen_santiye,
    gorunmeyen_santiye,
) -> None:
    """`missing` de süzülür: iki zorunlu tip × YALNIZ görünen ekipman = 2."""
    await _seed_types(seeded_db)
    await ekipman_fabrikasi("Görünen Vinç", site=gorunen_santiye)
    await ekipman_fabrikasi("Gizli Ekskavatör", site=gorunmeyen_santiye)

    resp = await client.get("/equipment/documents/summary", headers=sef_headers)
    assert resp.status_code == 200, resp.text
    assert resp.json()["missing"] == 2, resp.json()


async def test_belge_ozeti_DEPODAKI_ekipmani_HERKESE_sayar(
    client: AsyncClient,
    seeded_db: AsyncSession,
    ekipman_fabrikasi,
    sef_headers,
) -> None:
    """K20 depo istisnası: `site_id IS NULL` makine kapsam süzgecine TABİ DEĞİL.

    Bu iddia olmadan onarım "her şeyi süz" yönünde aşırıya kaçar ve henüz
    şantiyeye atanmamış makineyi HİÇ KİMSE göremez.
    """
    await _seed_types(seeded_db)
    await ekipman_fabrikasi("Depodaki Vinç", site=None)

    resp = await client.get("/equipment/documents/summary", headers=sef_headers)
    assert resp.status_code == 200, resp.text
    assert resp.json()["missing"] == 2, resp.json()
