"""`PATCH /documents/{id}` (`full`) ve `DELETE /documents/{id}` (`admin`) — T3.

## PATCH

Üç alan değişir: `filename` · `description` · `folder_id` (TAŞIMA). Taşımada
hedef klasörün `project_id`/`site_id`si belgeninkiyle AYNI olmalıdır (422);
`folder_id: null` belgeyi kapsamın KÖKÜNE taşır (izinli — SB'de klasörsüz
belgeler de listelenir).

**Ad değişince `mime_type` YENİDEN TÜRETİLİR.** Uzantı otorite olduğuna göre
(bkz. `test_files.py`), `Rapor.pdf` → `Rapor.xlsx` yeniden adlandırmasından sonra
künyede `application/pdf` kalsaydı indirme ucu yanlış tip sunardı. Aynı sebeple
yeni ad da beyaz listeden geçer: geçmeseydi yükleme kapısı, yeniden adlandırma
ile atlatılabilirdi (`rapor.pdf` yükle → `rapor.exe` yap).

## DELETE

`admin` kapısıdır (`full` silmeyi KAPSAMAZ — `app/core/access.py`). Baytlar hem
`StorageBackend.delete` ile hem de `document_blobs` FK'sinin CASCADE'iyle gider;
ikisi ayrı ayrı kanıtlanır (CASCADE tek başına yalnız DB backend'i için
yeterlidir, R2/S3 backend'inde temizliği `delete` çağrısı yapar).

⚠️ **UÇ AÇILIR AMA EKRANDA BASILMAZ** (spec §3, bilinçli): mockup'ta silme
aksiyonu YOKTUR. Frontend dilimi bu ucu bir düğmeye BAĞLAMAYACAK.
"""

import uuid

from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.audit.models import AuditAction, AuditLog
from app.modules.documents.models import Document, DocumentBlob


async def _audit_details(seeded_db: AsyncSession, action: AuditAction) -> list[str]:
    rows = (
        (await seeded_db.execute(select(AuditLog).where(AuditLog.action == action))).scalars().all()
    )
    return [row.detail for row in rows]


# --- PATCH: ad / açıklama ---


async def test_ad_degistirilir(client: AsyncClient, proje, belge_fabrikasi, sef_headers) -> None:
    belge = await belge_fabrikasi(proje, "Hakedis_47.pdf")

    resp = await client.patch(
        f"/documents/{belge.id}", json={"filename": "Hakediş_47_Onaylı.pdf"}, headers=sef_headers
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["filename"] == "Hakediş_47_Onaylı.pdf"


async def test_ad_degisince_mime_yeniden_turetilir(
    client: AsyncClient, proje, belge_fabrikasi, sef_headers
) -> None:
    belge = await belge_fabrikasi(proje, "Rapor.pdf", mime_type="application/pdf")

    resp = await client.patch(
        f"/documents/{belge.id}", json={"filename": "Rapor.xlsx"}, headers=sef_headers
    )

    assert resp.status_code == 200, resp.text
    assert (
        resp.json()["mime_type"]
        == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )


async def test_yeniden_adlandirma_beyaz_listeyi_atlatamaz(
    client: AsyncClient, proje, belge_fabrikasi, sef_headers
) -> None:
    """Aksi hâlde yükleme kapısı iki adımda aşılırdı."""
    belge = await belge_fabrikasi(proje, "Rapor.pdf")

    resp = await client.patch(
        f"/documents/{belge.id}", json={"filename": "Rapor.exe"}, headers=sef_headers
    )

    assert resp.status_code == 422, resp.text


async def test_yeni_ad_da_normalize_edilir(
    client: AsyncClient, proje, belge_fabrikasi, sef_headers
) -> None:
    belge = await belge_fabrikasi(proje, "Rapor.pdf")

    resp = await client.patch(
        f"/documents/{belge.id}", json={"filename": "../../etc/Rapor.pdf"}, headers=sef_headers
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["filename"] == "Rapor.pdf"


async def test_aciklama_degistirilir_ve_silinebilir(
    client: AsyncClient, proje, belge_fabrikasi, sef_headers
) -> None:
    belge = await belge_fabrikasi(proje, "Foto.zip", description="48 fotoğraf")

    guncel = await client.patch(
        f"/documents/{belge.id}", json={"description": "52 fotoğraf"}, headers=sef_headers
    )
    temiz = await client.patch(
        f"/documents/{belge.id}", json={"description": None}, headers=sef_headers
    )

    assert guncel.json()["description"] == "52 fotoğraf"
    assert temiz.json()["description"] is None


async def test_aciklama_tavani_asilirsa_422(
    client: AsyncClient, proje, belge_fabrikasi, sef_headers
) -> None:
    """Açıklama tavanı (kullanıcı kararı 2026-08-04): 2000 karakter.

    Kolon `Text` olduğu için DB hiçbir şeyi engellemez — tavanı yalnız şema
    koyar. Sınır DEĞERİ de sınanır: 2000 geçer, 2001 geçmez; yoksa off-by-one
    bir düzeltme testi kırmadan geçebilir.
    """
    belge = await belge_fabrikasi(proje, "Foto.zip")

    tam = await client.patch(
        f"/documents/{belge.id}", json={"description": "ç" * 2000}, headers=sef_headers
    )
    asan = await client.patch(
        f"/documents/{belge.id}", json={"description": "ç" * 2001}, headers=sef_headers
    )

    assert tam.status_code == 200, tam.text
    assert len(tam.json()["description"]) == 2000
    assert asan.status_code == 422, asan.text


async def test_gonderilmeyen_alan_degismez(
    client: AsyncClient, proje, belge_fabrikasi, sef_headers
) -> None:
    """`description` GÖNDERİLMEMEK ile `null` GÖNDERMEK farklı şeylerdir."""
    belge = await belge_fabrikasi(proje, "Foto.zip", description="48 fotoğraf")

    resp = await client.patch(
        f"/documents/{belge.id}", json={"filename": "Fotograflar.zip"}, headers=sef_headers
    )

    assert resp.json()["description"] == "48 fotoğraf"


async def test_bos_govde_hicbir_seyi_bozmaz(
    client: AsyncClient, proje, belge_fabrikasi, sef_headers
) -> None:
    belge = await belge_fabrikasi(proje, "Foto.zip", description="48 fotoğraf")

    resp = await client.patch(f"/documents/{belge.id}", json={}, headers=sef_headers)

    assert resp.status_code == 200, resp.text
    assert resp.json()["filename"] == "Foto.zip"
    assert resp.json()["description"] == "48 fotoğraf"


# --- PATCH: klasör taşıma ---


async def test_ayni_kapsamdaki_klasore_tasinir(
    client: AsyncClient, proje, santiye, klasor_fabrikasi, belge_fabrikasi, sef_headers
) -> None:
    kaynak = await klasor_fabrikasi(proje, "Günlük Raporlar", site=santiye)
    hedef = await klasor_fabrikasi(proje, "Hakedişler", site=santiye)
    belge = await belge_fabrikasi(proje, "Rapor.pdf", site=santiye, folder=kaynak)

    resp = await client.patch(
        f"/documents/{belge.id}", json={"folder_id": str(hedef.id)}, headers=sef_headers
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["folder_id"] == str(hedef.id)


async def test_klasorsuz_koke_tasinabilir(
    client: AsyncClient, proje, santiye, klasor_fabrikasi, belge_fabrikasi, sef_headers
) -> None:
    klasor = await klasor_fabrikasi(proje, "Hakedişler", site=santiye)
    belge = await belge_fabrikasi(proje, "Rapor.pdf", site=santiye, folder=klasor)

    resp = await client.patch(
        f"/documents/{belge.id}", json={"folder_id": None}, headers=sef_headers
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["folder_id"] is None


async def test_baska_kapsamin_klasorune_tasinamaz_422(
    client: AsyncClient, proje, santiye, klasor_fabrikasi, belge_fabrikasi, sef_headers
) -> None:
    """Belge PROJE düzeyi, hedef klasör ŞANTİYE kapsamlı — ayrışırsa belge iki
    farklı kökte görünürdü."""
    hedef = await klasor_fabrikasi(proje, "Hakedişler", site=santiye)
    belge = await belge_fabrikasi(proje, "Rapor.pdf")

    resp = await client.patch(
        f"/documents/{belge.id}", json={"folder_id": str(hedef.id)}, headers=sef_headers
    )

    assert resp.status_code == 422, resp.text


async def test_baska_projenin_klasorune_tasinamaz_422(
    client: AsyncClient, proje, ikinci_proje, klasor_fabrikasi, belge_fabrikasi, sef_headers
) -> None:
    hedef = await klasor_fabrikasi(ikinci_proje, "Yabancı")
    belge = await belge_fabrikasi(proje, "Rapor.pdf")

    resp = await client.patch(
        f"/documents/{belge.id}", json={"folder_id": str(hedef.id)}, headers=sef_headers
    )

    assert resp.status_code == 422, resp.text


async def test_var_olmayan_klasore_tasinamaz_422(
    client: AsyncClient, proje, belge_fabrikasi, sef_headers
) -> None:
    belge = await belge_fabrikasi(proje, "Rapor.pdf")

    resp = await client.patch(
        f"/documents/{belge.id}", json={"folder_id": str(uuid.uuid4())}, headers=sef_headers
    )

    assert resp.status_code == 422, resp.text


async def test_tasima_hedef_klasoru_paylasimli_kilitler(
    client: AsyncClient, proje, santiye, klasor_fabrikasi, belge_fabrikasi, sef_headers, monkeypatch
) -> None:
    from app.modules.documents import repository

    kilitlenen: list[uuid.UUID] = []
    orijinal = repository.lock_folder_shared

    async def izleyen(session, folder_id):
        kilitlenen.append(folder_id)
        await orijinal(session, folder_id)

    monkeypatch.setattr(repository, "lock_folder_shared", izleyen)
    hedef = await klasor_fabrikasi(proje, "Hakedişler", site=santiye)
    belge = await belge_fabrikasi(proje, "Rapor.pdf", site=santiye)

    resp = await client.patch(
        f"/documents/{belge.id}", json={"folder_id": str(hedef.id)}, headers=sef_headers
    )

    assert resp.status_code == 200, resp.text
    assert hedef.id in kilitlenen


async def test_patch_denetime_yazilir(
    client: AsyncClient, seeded_db: AsyncSession, proje, belge_fabrikasi, sef_headers
) -> None:
    belge = await belge_fabrikasi(proje, "Rapor.pdf")

    await client.patch(
        f"/documents/{belge.id}", json={"filename": "Rapor_Rev2.pdf"}, headers=sef_headers
    )

    detaylar = await _audit_details(seeded_db, AuditAction.update)
    assert any("Rapor_Rev2.pdf" in d for d in detaylar)


# --- PATCH: IDOR + yetki ---


async def test_gorunmeyen_belge_patch_404(
    client: AsyncClient, ikinci_proje, belge_fabrikasi, sef_headers
) -> None:
    belge = await belge_fabrikasi(ikinci_proje, "Gizli.pdf")

    gorunmeyen = await client.patch(
        f"/documents/{belge.id}", json={"filename": "Yeni.pdf"}, headers=sef_headers
    )
    yok = await client.patch(
        f"/documents/{uuid.uuid4()}", json={"filename": "Yeni.pdf"}, headers=sef_headers
    )

    assert gorunmeyen.status_code == yok.status_code == 404
    assert gorunmeyen.json() == yok.json()


async def test_salt_okur_rol_patch_edemez_403(
    client: AsyncClient, proje, belge_fabrikasi, pm_headers
) -> None:
    belge = await belge_fabrikasi(proje, "Rapor.pdf")

    resp = await client.patch(
        f"/documents/{belge.id}", json={"filename": "Yeni.pdf"}, headers=pm_headers
    )

    assert resp.status_code == 403


# --- DELETE ---


async def test_belge_silinir(
    client: AsyncClient, seeded_db: AsyncSession, proje, belge_fabrikasi, admin_headers
) -> None:
    belge = await belge_fabrikasi(proje, "Rapor.pdf", data=b"veri", size_bytes=4)

    resp = await client.delete(f"/documents/{belge.id}", headers=admin_headers)

    assert resp.status_code == 204, resp.text
    kalan = (await seeded_db.execute(select(func.count()).select_from(Document))).scalar_one()
    assert kalan == 0


async def test_silmede_baytlar_da_gider(
    client: AsyncClient, seeded_db: AsyncSession, proje, belge_fabrikasi, admin_headers
) -> None:
    belge = await belge_fabrikasi(proje, "Rapor.pdf", data=b"veri", size_bytes=4)

    await client.delete(f"/documents/{belge.id}", headers=admin_headers)

    kalan = (
        await seeded_db.execute(
            select(DocumentBlob.document_id).where(DocumentBlob.document_id == belge.id)
        )
    ).scalar_one_or_none()
    assert kalan is None


async def test_blob_FK_CASCADE_ile_de_gider(
    seeded_db: AsyncSession, proje, belge_fabrikasi
) -> None:
    """CASCADE KANITI — uçtan bağımsız, DB düzeyinde.

    Uç ayrıca `StorageBackend.delete` çağırır (R2/S3 backend'i için gerekli);
    bu test o çağrıyı devre dışı bırakarak yalnız FK'nin sözünü ölçer. Yetim
    bayt kalırsa `document_blobs` sonsuza kadar şişerdi.
    """
    belge = await belge_fabrikasi(proje, "Rapor.pdf", data=b"veri", size_bytes=4)

    await seeded_db.delete(belge)
    await seeded_db.flush()

    kalan = (
        await seeded_db.execute(
            select(DocumentBlob.document_id).where(DocumentBlob.document_id == belge.id)
        )
    ).scalar_one_or_none()
    assert kalan is None


async def test_silme_depolama_backendine_de_soyler(
    client: AsyncClient, proje, belge_fabrikasi, admin_headers
) -> None:
    """DB dışı bir backend'de (R2/S3) CASCADE YOKTUR — temizliği `delete` yapar."""
    from app.main import app
    from app.modules.documents.deps import get_storage_backend

    silinen: list[uuid.UUID] = []

    class _IzleyenBackend:
        async def put(self, document_id: uuid.UUID, data: bytes) -> None: ...

        async def stream(self, document_id: uuid.UUID):
            yield b""

        async def delete(self, document_id: uuid.UUID) -> None:
            silinen.append(document_id)

    belge = await belge_fabrikasi(proje, "Rapor.pdf", data=b"veri", size_bytes=4)
    app.dependency_overrides[get_storage_backend] = _IzleyenBackend
    try:
        resp = await client.delete(f"/documents/{belge.id}", headers=admin_headers)
    finally:
        app.dependency_overrides.pop(get_storage_backend, None)

    assert resp.status_code == 204, resp.text
    assert silinen == [belge.id]


async def test_silme_denetime_yazilir(
    client: AsyncClient, seeded_db: AsyncSession, proje, belge_fabrikasi, admin_headers
) -> None:
    belge = await belge_fabrikasi(proje, "Rapor.pdf", data=b"veri", size_bytes=4)

    await client.delete(f"/documents/{belge.id}", headers=admin_headers)

    detaylar = await _audit_details(seeded_db, AuditAction.delete)
    assert any("Rapor.pdf" in d and proje.name in d for d in detaylar)


async def test_tam_yetkili_rol_silemez_403(
    client: AsyncClient, proje, belge_fabrikasi, sef_headers
) -> None:
    """`full` silmeyi KAPSAMAZ (`sites`/`units`/`boq` deseni)."""
    belge = await belge_fabrikasi(proje, "Rapor.pdf")

    resp = await client.delete(f"/documents/{belge.id}", headers=sef_headers)

    assert resp.status_code == 403


async def test_gorunmeyen_belge_silinemez_403_kapi_once_kosar(
    client: AsyncClient, ikinci_proje, belge_fabrikasi, sef_headers
) -> None:
    """DELETE ucunun IDOR yüzeyi (SIL-B1 sonrası): kapı (yalnız Sistem Yöneticisi) handler'dan
    ÖNCE koşar. Projeyi görmeyen `site_chief` için görünmeyen belge ile var olmayan kimlik AYNI
    403'ü alır; belgenin varlığı sızmaz. (Eskiden `documents` hücresi `admin`e yükseltilip 404
    sınanırdı; silme kapısı artık seviye değil rol anahtarıdır.)"""
    belge = await belge_fabrikasi(ikinci_proje, "Gizli.pdf")

    gorunmeyen = await client.delete(f"/documents/{belge.id}", headers=sef_headers)
    yok = await client.delete(f"/documents/{uuid.uuid4()}", headers=sef_headers)

    # SIL-B1: kapı (yalnız Sistem Yöneticisi) handler'dan ÖNCE koşar; görünmeyen ve var olmayan
    # belge AYNI 403'ü alır (varlık sızmaz).
    assert gorunmeyen.status_code == yok.status_code == 403
    assert gorunmeyen.json() == yok.json()
