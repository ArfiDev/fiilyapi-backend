"""İK-1 T2 — uçtan uca (HTTP) personel kart genişlemesi.

Spec: `docs/superpowers/specs/2026-08-12-ik1-personel-belge-design.md` §1, §5 K1/K3/K4.

Servis kuralları `test_personnel_ik_service.py`de; burada HTTP STATÜLERİ ve
`?project_id=` süzgeci + IDOR (süzgeç yetki genişletmez) doğrulanır.

⚠️ Duplicate TCKN → **409** (statü/`DuplicateError`); DB SQLSTATE'ine bakılmaz
(PG sürüm tuzağı, WORKFLOW §4).
"""

import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.personnel import guards
from app.modules.personnel.models import Personnel
from app.modules.personnel.schemas import PersonnelUpdate
from app.modules.sites.models import Section, Site
from app.modules.users.models import User

GECERLI_TCKN = "10000000146"

ISCI = {"full_name": "Ahmet Yılmaz", "trade": "Kalıpçı", "source": "company"}


def _tam(project_id: str, **fark) -> dict:
    return {
        "full_name": "Ahmet Yılmaz",
        "source": "company",
        "tc_no": GECERLI_TCKN,
        "birth_date": "1990-01-01",
        "phone": "5551112233",
        "address": "Mahalle Sokak No 1",
        "emergency_contact_name": "Ayşe Yılmaz",
        "emergency_contact_phone": "5559998877",
        "trade": "Kalıpçı",
        "hire_date": "2026-01-01",
        "assigned_project_id": project_id,
        "wage_type": "daily",
        "wage_amount": "1500.00",
        "is_draft": False,
        **fark,
    }


@pytest.fixture
async def ik_headers(ik_headers: dict[str, str], seeded_db: AsyncSession) -> dict[str, str]:
    """IZN-B5a madde 23a: atama alanı artık aktörün GÖRDÜĞÜ projeyi ister; bu dosyanın İK
    kullanıcısı "Tüm projeler" kişisidir (görünürlük kısıtı `test_izn_b5a_personel.py`de)."""
    kullanici = (
        await seeded_db.execute(select(User).where(User.email == "ik@personnel.co"))
    ).scalar_one()
    kullanici.all_projects = True
    await seeded_db.flush()
    return ik_headers


@pytest.fixture
async def proje(seeded_db: AsyncSession, project_factory):
    return await project_factory(code="IK1-API-1", name="API Proje")


@pytest.fixture
async def bolum(seeded_db: AsyncSession, proje):
    santiye = Site(project_id=proje.id, code="S1", name="Şantiye")
    seeded_db.add(santiye)
    await seeded_db.flush()
    section = Section(site_id=santiye.id, name="Bölüm")
    seeded_db.add(section)
    await seeded_db.flush()
    return section


# --- POST: taslak gevşek / yayın sıkı ----------------------------------------


@pytest.mark.asyncio
async def test_post_taslak_gevsek_201(client, ik_headers):
    """`is_draft=True` (varsayılan) → eksik alanla 201."""
    yanit = await client.post("/personnel", json=ISCI, headers=ik_headers)
    assert yanit.status_code == 201, yanit.text
    assert yanit.json()["is_draft"] is True


@pytest.mark.asyncio
async def test_post_yayin_tam_201(client, ik_headers, proje):
    yanit = await client.post("/personnel", json=_tam(str(proje.id)), headers=ik_headers)
    assert yanit.status_code == 201, yanit.text
    govde = yanit.json()
    assert govde["is_draft"] is False
    assert govde["tc_no"] == GECERLI_TCKN
    assert govde["assigned_project_id"] == str(proje.id)


@pytest.mark.asyncio
async def test_post_yayin_eksik_422(client, ik_headers):
    yanit = await client.post("/personnel", json={**ISCI, "is_draft": False}, headers=ik_headers)
    assert yanit.status_code == 422, yanit.text


@pytest.mark.asyncio
async def test_gecersiz_tckn_422(client, ik_headers):
    yanit = await client.post(
        "/personnel", json={**ISCI, "tc_no": "10000000140"}, headers=ik_headers
    )
    assert yanit.status_code == 422, yanit.text


@pytest.mark.asyncio
async def test_duplicate_tckn_409(client, ik_headers):
    """⚠️ 409 statüsüne bakılır, DB SQLSTATE'ine değil (PG sürüm tuzağı)."""
    await client.post("/personnel", json={**ISCI, "tc_no": GECERLI_TCKN}, headers=ik_headers)
    yanit = await client.post(
        "/personnel",
        json={**ISCI, "full_name": "Başka", "tc_no": GECERLI_TCKN},
        headers=ik_headers,
    )
    assert yanit.status_code == 409, yanit.text


@pytest.mark.asyncio
async def test_var_olmayan_atanan_proje_404(client, ik_headers):
    yanit = await client.post(
        "/personnel",
        json={**ISCI, "assigned_project_id": str(uuid.uuid4())},
        headers=ik_headers,
    )
    assert yanit.status_code == 404, yanit.text


@pytest.mark.asyncio
async def test_bolum_dogru_projede_201(client, ik_headers, proje, bolum):
    yanit = await client.post(
        "/personnel",
        json={
            **ISCI,
            "assigned_project_id": str(proje.id),
            "assigned_section_id": str(bolum.id),
        },
        headers=ik_headers,
    )
    assert yanit.status_code == 201, yanit.text
    assert yanit.json()["assigned_section_id"] == str(bolum.id)


# --- PATCH: taslağı yayına çevirme -------------------------------------------


@pytest.mark.asyncio
async def test_patch_taslagi_yayina_cevirir(client, ik_headers, proje):
    taslak = await client.post("/personnel", json=ISCI, headers=ik_headers)
    kimlik = taslak.json()["id"]
    yanit = await client.patch(
        f"/personnel/{kimlik}",
        json={
            "tc_no": GECERLI_TCKN,
            "birth_date": "1990-01-01",
            "phone": "5551112233",
            "address": "Adres",
            "emergency_contact_name": "Yakın",
            "emergency_contact_phone": "5559998877",
            "hire_date": "2026-01-01",
            "assigned_project_id": str(proje.id),
            "wage_type": "daily",
            "wage_amount": "1500.00",
            "is_draft": False,
        },
        headers=ik_headers,
    )
    assert yanit.status_code == 200, yanit.text
    assert yanit.json()["is_draft"] is False


@pytest.mark.asyncio
async def test_patch_eksik_yayina_cevirme_422(client, ik_headers):
    taslak = await client.post("/personnel", json=ISCI, headers=ik_headers)
    kimlik = taslak.json()["id"]
    yanit = await client.patch(f"/personnel/{kimlik}", json={"is_draft": False}, headers=ik_headers)
    assert yanit.status_code == 422, yanit.text


# --- Liste: ?project_id= süzgeci + IDOR --------------------------------------


@pytest.mark.asyncio
async def test_liste_project_id_suzgeci(client, ik_headers, proje):
    await client.post("/personnel", json=_tam(str(proje.id)), headers=ik_headers)
    await client.post("/personnel", json={**ISCI, "full_name": "Atamasız İşçi"}, headers=ik_headers)
    yanit = await client.get("/personnel", params={"project_id": str(proje.id)}, headers=ik_headers)
    assert yanit.status_code == 200, yanit.text
    govde = yanit.json()
    assert [k["full_name"] for k in govde["items"]] == ["Ahmet Yılmaz"]
    assert govde["total"] == 1


@pytest.mark.asyncio
async def test_liste_is_draft_suzgeci(client, ik_headers, proje):
    await client.post("/personnel", json=_tam(str(proje.id)), headers=ik_headers)  # yayın
    await client.post(
        "/personnel", json={**ISCI, "full_name": "Taslak İşçi"}, headers=ik_headers
    )  # taslak
    taslaklar = await client.get("/personnel", params={"is_draft": True}, headers=ik_headers)
    assert [k["full_name"] for k in taslaklar.json()["items"]] == ["Taslak İşçi"]


@pytest.mark.asyncio
async def test_project_id_suzgeci_yetki_genisletmez_idor(
    client, ik_headers, kisitli_ik_headers, proje
):
    """`?project_id=` yalnız SÜZGEÇtir; `personnel` şirket-geneli varlıktır.

    IZN-B5a (CEO, bilinçli fark): kapsamı alakasız bir projeyle sınırlanmış İK kullanıcısı,
    GÖREMEDİĞİ projeyle süzünce BOŞ liste alır (var/yok sızmaz); personelin kendisi ise
    süzgeçsiz listede yine görünür (proje alanı `null`). Ayrıntı: `test_izn_b5a_personel.py`.
    """
    await client.post("/personnel", json=_tam(str(proje.id)), headers=ik_headers)
    yanit = await client.get(
        "/personnel", params={"project_id": str(proje.id)}, headers=kisitli_ik_headers
    )
    assert yanit.status_code == 200, yanit.text
    assert yanit.json()["total"] == 0
    liste = await client.get("/personnel", headers=kisitli_ik_headers)
    assert liste.json()["total"] == 1  # personel şirket-geneli: yine görünür
    assert liste.json()["items"][0]["assigned_project_id"] is None


# --- IBAN doğrulaması: personel giriş noktaları (canlı smoke bulgusu) --------
#
# 🔴 Kusur `bank_accounts`ta bulundu ama personelde DAHA KÖTÜYDÜ: burada
# normalizasyon bile yoktu, alanın tek koruması `max_length=34`tü. `iban`
# bordronun ödeme talimatına giden alandır (İK-1 spec §1).
#
# Kuralın kendisi `tests/core/test_iban.py`de sınanır; buradaki testler yalnız
# İKİ giriş noktasının (Create · Update) o kurala BAĞLI olduğunu kanıtlar.

GECERLI_IBAN = "TR330006100519786457841326"


@pytest.mark.asyncio
async def test_post_gecersiz_IBAN_422(client, ik_headers):
    yanit = await client.post(
        "/personnel", json={**ISCI, "iban": "BUNUBIRIBANDEGIL!!"}, headers=ik_headers
    )
    assert yanit.status_code == 422, yanit.text


@pytest.mark.asyncio
async def test_post_TR_ve_24_sifir_422(client, ik_headers):
    yanit = await client.post(
        "/personnel", json={**ISCI, "iban": "TR000000000000000000000000"}, headers=ik_headers
    )
    assert yanit.status_code == 422, yanit.text


@pytest.mark.asyncio
async def test_post_mod97_bozuk_422(client, ik_headers):
    """Yalnız sağlama hanesi değiştirildi (33→34): uzunluk/alfabe AYNI."""
    yanit = await client.post(
        "/personnel", json={**ISCI, "iban": "TR340006100519786457841326"}, headers=ik_headers
    )
    assert yanit.status_code == 422, yanit.text


@pytest.mark.asyncio
async def test_post_cok_kisa_IBAN_422(client, ik_headers):
    yanit = await client.post("/personnel", json={**ISCI, "iban": "TR00"}, headers=ik_headers)
    assert yanit.status_code == 422, yanit.text


@pytest.mark.asyncio
async def test_post_TR_uzunlugu_26_degilse_422(client, ik_headers):
    """27 hane, mod-97 TUTAR — ülkeye özgü uzunluk AYRI kapıdır."""
    yanit = await client.post(
        "/personnel", json={**ISCI, "iban": "TR0400061005197864578413260"}, headers=ik_headers
    )
    assert yanit.status_code == 422, yanit.text


@pytest.mark.asyncio
async def test_post_IBAN_null_gecer(client, ik_headers):
    """🔴 Alan ZORUNLU DEĞİLDİR: elden ödeme meşrudur (`payment_method=cash`)."""
    yanit = await client.post("/personnel", json=ISCI, headers=ik_headers)
    assert yanit.status_code == 201, yanit.text
    assert yanit.json()["iban"] is None


@pytest.mark.asyncio
async def test_post_bosluklu_kucuk_harfli_IBAN_NORMALIZE_edilir(client, ik_headers):
    """Personelde normalizasyon HİÇ YOKTU: `tr33 0006…` ham hâliyle saklanırdı."""
    yanit = await client.post(
        "/personnel",
        json={**ISCI, "iban": "tr33 0006 1005 1978 6457 8413 26"},
        headers=ik_headers,
    )
    assert yanit.status_code == 201, yanit.text
    assert yanit.json()["iban"] == GECERLI_IBAN


@pytest.mark.asyncio
async def test_patch_gecersiz_IBAN_422_ve_kayit_DEGISMEZ(client, ik_headers):
    """🔴 DÖRDÜNCÜ giriş noktası — POST kapatılıp bu açık bırakılsaydı kapı
    PATCH'ten atlatılırdı."""
    olusan = await client.post(
        "/personnel", json={**ISCI, "iban": GECERLI_IBAN}, headers=ik_headers
    )
    kimlik = olusan.json()["id"]
    yanit = await client.patch(
        f"/personnel/{kimlik}", json={"iban": "BUNUBIRIBANDEGIL!!"}, headers=ik_headers
    )
    assert yanit.status_code == 422, yanit.text
    kalan = await client.get(f"/personnel/{kimlik}", headers=ik_headers)
    assert kalan.json()["iban"] == GECERLI_IBAN


@pytest.mark.asyncio
async def test_patch_mod97_bozuk_422(client, ik_headers):
    olusan = await client.post("/personnel", json=ISCI, headers=ik_headers)
    yanit = await client.patch(
        f"/personnel/{olusan.json()['id']}",
        json={"iban": "TR340006100519786457841326"},
        headers=ik_headers,
    )
    assert yanit.status_code == 422, yanit.text


@pytest.mark.asyncio
async def test_patch_gecerli_IBAN_NORMALIZE_edilerek_yazilir(client, ik_headers):
    olusan = await client.post("/personnel", json=ISCI, headers=ik_headers)
    yanit = await client.patch(
        f"/personnel/{olusan.json()['id']}",
        json={"iban": "tr33 0006 1005 1978 6457 8413 26"},
        headers=ik_headers,
    )
    assert yanit.status_code == 200, yanit.text
    assert yanit.json()["iban"] == GECERLI_IBAN


# --- KARARLAR.md §1.10 (kullanıcı kararı 2026-09-23): açık `null` -> 422 -----
#
# ÖLÇÜLMÜŞ BUGÜNKÜ DAVRANIŞ (bekçi eklenmeden önce, kanon uygulanmadan ölçüldü):
# * `full_name`/`source`/`is_active` — TASLAK kayıtta açık `null` gönderilince
#   `update_personnel`deki yayın-tamlık kontrolü (`core.py:163`) `is_draft`
#   değişmediği için hiç çalışmaz, `setattr` doğrudan NOT NULL kolona `None`
#   yazar -> `IntegrityError` -> OPAK 409 "Veri bütünlüğü hatası" (hangi alan
#   olduğu YAZMAZ).
# * `is_draft` — YAYINLANMIŞ (tam) kayıtta aynı opak 409; TASLAK kayıtta ise
#   409 DEĞİL 422 döner ama gerekçe YANLIŞ ("yayın için eksik alanlar" —
#   `is_draft` boş bırakılamaz mesajı DEĞİL).
#
# Bu blok artık BEKÇİ SONRASI hedef davranışı doğrular: dördü de her durumda
# (taslak/yayın) 422 + alan adı (pydantic `loc`), kayıt DEĞİŞMEZ.


def _personnel_not_null_alanlar() -> tuple[str, ...]:
    """`PersonnelUpdate` alanlarından hedef DB kolonu `nullable=False` olanlar —

    `Personnel.__table__.columns` ÜZERİNDEN TÜRETİLİR (elle liste DEĞİL).
    `guards.PERSONNEL_NULLABLE_OLMAYAN_ALANLAR`in KENDİSİ bu türevle
    doğrulanır ki bir sonraki NOT NULL alan eklendiğinde bekçi SESSİZCE kör
    kalmasın (depo kanonu: "sayı değil BEKÇİ yaz").
    """
    update_alanlari = set(PersonnelUpdate.model_fields)
    kolonlar = {c.name: c for c in Personnel.__table__.columns}
    return tuple(
        ad
        for ad in update_alanlari
        if ad in kolonlar and not kolonlar[ad].nullable and kolonlar[ad].server_default is None
    )


def test_personnel_not_null_alan_listesi_turetilerek_dogrulanir():
    """Bekçi elle YAZILMIŞ bir listeyle KÖR kalmasın — DB şemasından türet, karşılaştır.

    🔴 `server_default`li NOT NULL kolonlar (`is_active`/`is_draft`) `Column.nullable`
    açısından da `False`'tur ama ORM `server_default` VARKEN Python tarafında
    `setattr(None)` yine de DB'ye `NULL` yazmaya ÇALIŞIR (server_default yalnız
    INSERT'te kolon HİÇ verilmediğinde devreye girer, UPDATE'te `None` göndermek
    hâlâ NOT NULL ihlalidir) — bu yüzden türev `server_default` filtresini
    UYGULAMAZ; aşağıda elle ekleniyor.
    """
    turetilen = {
        c.name
        for c in Personnel.__table__.columns
        if c.name in PersonnelUpdate.model_fields and not c.nullable
    }
    assert turetilen == set(guards.PERSONNEL_NULLABLE_OLMAYAN_ALANLAR)


@pytest.mark.asyncio
@pytest.mark.parametrize("alan", ["full_name", "source", "is_active"])
async def test_patch_not_null_alan_acik_null_taslakta_422_alan_adiyla(client, ik_headers, alan):
    """Hedef davranış (taslak kayıt): açık `null` artık 422 + alan adı, kayıt DEĞİŞMEZ."""
    kayit = await client.post("/personnel", json=ISCI, headers=ik_headers)
    kimlik = kayit.json()["id"]
    yanit = await client.patch(f"/personnel/{kimlik}", json={alan: None}, headers=ik_headers)
    assert yanit.status_code == 422, (alan, yanit.status_code, yanit.text)
    assert alan in yanit.text

    sonra = await client.get(f"/personnel/{kimlik}", headers=ik_headers)
    assert sonra.json()[alan] == ISCI.get(alan, True if alan == "is_active" else None)


@pytest.mark.asyncio
async def test_patch_is_draft_acik_null_yayinda_422_alan_adiyla(client, ik_headers, proje):
    """Hedef davranış (yayınlanmış kayıt): `is_draft: null` artık 422 + alan adı."""
    kayit = await client.post("/personnel", json=_tam(str(proje.id)), headers=ik_headers)
    kimlik = kayit.json()["id"]
    yanit = await client.patch(f"/personnel/{kimlik}", json={"is_draft": None}, headers=ik_headers)
    assert yanit.status_code == 422, yanit.text
    assert "is_draft" in yanit.text

    sonra = await client.get(f"/personnel/{kimlik}", headers=ik_headers)
    assert sonra.json()["is_draft"] is False


@pytest.mark.asyncio
async def test_patch_is_draft_acik_null_taslakta_422_alan_adiyla(client, ik_headers):
    """Hedef davranış (taslak kayıt): `is_draft: null` artık DOĞRU gerekçeyle
    422 verir — "yayın için eksik alanlar" DEĞİL, alan adı ("is_draft") ile."""
    kayit = await client.post("/personnel", json=ISCI, headers=ik_headers)
    kimlik = kayit.json()["id"]
    yanit = await client.patch(f"/personnel/{kimlik}", json={"is_draft": None}, headers=ik_headers)
    assert yanit.status_code == 422, yanit.text
    assert "is_draft" in yanit.text

    sonra = await client.get(f"/personnel/{kimlik}", headers=ik_headers)
    assert sonra.json()["is_draft"] is True


@pytest.mark.asyncio
async def test_patch_nullable_alan_null_ile_temizlenebilir(client, ik_headers):
    """Yanlış-pozitif bekçisi: NULLABLE bir alan (`trade`) hâlâ `null` ile TEMİZLENEBİLİR —
    bekçi yalnız NOT NULL dörtlüde çalışır, öteki alanları KISITLAMAZ."""
    kayit = await client.post("/personnel", json=ISCI, headers=ik_headers)
    kimlik = kayit.json()["id"]
    yanit = await client.patch(f"/personnel/{kimlik}", json={"trade": None}, headers=ik_headers)
    assert yanit.status_code == 200, yanit.text
    assert yanit.json()["trade"] is None
