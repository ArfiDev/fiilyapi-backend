"""OK-1A / T4 — onay kutusu SATIRI: mockup alanları + IDOR süzgeci.

Mockup `projedesign/Onay Kutusu.dc.html` kartı BEŞ şey basar ve motorun bildiği
olgular bunların yalnız İKİSİNİ karşılıyordu (tip · oluşturan+zaman · adım
şeridi). Eksik üçü — **başlık · alt başlık · tutar(lar)** — evrak ailelerinden
okunur:

| Alan | Taşeron hakedişi | Satın alma talebi | İşveren hakedişi |
|---|---|---|---|
| `title` | `:126` | `:161` | `:219` |
| `subtitle` | `:127` | `:162` | `:220` |
| `gross_amount` | `:138` "Brüt" | `:173` "Sipariş Tutarı" | `:227` "Hakediş Tutarı" |
| `net_amount` | `:139` "Net" | **YOK** → `null` | `:228` "Net Tahsil" |

🔴 Mockup'taki BEŞ karttan ikisi (bordro `:91`, günlük kayıt `:194`) OK-1B'nin
işidir ve bu dilimde zincire GİRMEZ (K4); onlara göre alan EKLENMEZ.

🔴 IDOR: `GET /approvals` `projects.service.visible_projects` üzerinden süzülür.
T1+T2 bunu "bugün zincire bağlı evrak yok" diye ertelemişti; T3 tam da onu
değiştirdi. `total` süzgeci SQL `COUNT`un İÇİNDE taşır (BOR-TEMİZ kanonu):
`items` boş ama `total` > 0 olan bir kurulum SAHTE-YEŞİLDİR.
"""

import uuid
from decimal import Decimal

from app.modules.approvals import service
from app.modules.approvals.models import (
    ApprovalChain,
    ApprovalDocumentType,
    ApprovalRole,
    ApprovalStep,
)
from tests.modules.approvals.conftest import proje_rolu_ver

_TASERON = ApprovalDocumentType.subcontractor_progress_payment
_ISVEREN = ApprovalDocumentType.progress_payment
_SATINALMA = ApprovalDocumentType.purchase_request

#: Üç ailenin ilk adımının sahibi olan rol. IZN-B3b: bir kişi bir projede TEK rol taşır; tek
#: turda üç satır görecek aktör, her ailenin projesinde O AİLENİN ilk adım rolüyle üyedir.
_ILK_ADIM_ROLU = {
    _TASERON: ApprovalRole.site_chief,
    _ISVEREN: ApprovalRole.accounting,
    _SATINALMA: ApprovalRole.procurement,
}


async def _zincir(seeded_db, document_type, document_id, creator, amount=Decimal("100.00")):
    return await service.create_chain(
        seeded_db,
        document_type=document_type,
        document_id=document_id,
        amount=amount,
        created_by_user_id=creator.id,
    )


# --------------------------------------------------------------------------- #
# 1. Satır zenginleştirmesi — mockup alanları
# --------------------------------------------------------------------------- #


async def test_taseron_satiri_MOCKUP_alanlarini_tasir(
    client, seeded_db, aktor_fabrikasi, evrak_fabrikasi, giris
):
    """Mockup `:126` başlık · `:127` alt başlık · `:138` Brüt · `:139` Net.

    Brüt = 100 × ₺1.000 = ₺100.000 · KDV %20 = ₺20.000 · avans tavanı 0
    (sözleşme kalemi yok) · teminat %5 = ₺5.000 ⇒ **net ₺115.000**.
    """
    yaratan = await aktor_fabrikasi("t4-yaratan@ok1a.co", full_name="Sercan Öztürk")
    await aktor_fabrikasi(
        "t4-sef@ok1a.co", role_key="site_chief", approval_roles=[ApprovalRole.site_chief]
    )
    basliklar = await giris("t4-sef@ok1a.co")

    document_id, _ = await evrak_fabrikasi(
        _TASERON,
        creator=yaratan,
        subcontractor_name="Akın İnşaat",
        work_category="Betonarme",
        description="Kat 6–8",
        period=(2026, 7),
    )
    await _zincir(seeded_db, _TASERON, document_id, yaratan)

    yanit = await client.get("/approvals", headers=basliklar)

    assert yanit.status_code == 200, yanit.text
    (satir,) = yanit.json()["items"]
    assert satir["title"] == "Akın İnşaat — Hakediş #47 (Betonarme)"
    assert satir["subtitle"] == "Güneşkent 1 · Kat 6–8 · 07/2026"
    assert satir["gross_amount"] == "100000.00"
    assert satir["net_amount"] == "115000.00"


async def test_satinalma_satirinda_NET_TUTAR_NULLDUR(
    client, seeded_db, aktor_fabrikasi, evrak_fabrikasi, giris
):
    """🔴 Satın alma talebinde brüt/net AYRIMI YOKTUR: mockup `:173` TEK kutu
    basar ("Sipariş Tutarı"), hakediş kartlarındaki `:139`/`:228` ikinci kutusu
    ORADA YOKTUR. Uydurulmuş bir "net" ikinci bir gerçek doğururdu.
    """
    yaratan = await aktor_fabrikasi("t4-sa-yaratan@ok1a.co", full_name="Y. Kaya")
    await aktor_fabrikasi(
        "t4-sa@ok1a.co", role_key="procurement", approval_roles=[ApprovalRole.procurement]
    )
    basliklar = await giris("t4-sa@ok1a.co")

    document_id, _ = await evrak_fabrikasi(
        _SATINALMA,
        creator=yaratan,
        justification="3 teklif alındı, en uygun seçildi",
    )
    await _zincir(seeded_db, _SATINALMA, document_id, yaratan)

    yanit = await client.get("/approvals", headers=basliklar)

    assert yanit.status_code == 200, yanit.text
    (satir,) = yanit.json()["items"]
    assert satir["title"] == "C25/30 Hazır Beton — 320 m³"
    assert satir["subtitle"] == "Güneşkent 1 · 3 teklif alındı, en uygun seçildi"
    assert satir["gross_amount"] == "592000.00"
    assert satir["net_amount"] is None, "satın almada net UYDURULAMAZ (mockup :173 tek kutu)"


async def test_isveren_satiri_MOCKUP_alanlarini_tasir(
    client, seeded_db, aktor_fabrikasi, evrak_fabrikasi, giris
):
    """Mockup `:219` başlık · `:220` alt başlık (şantiye kırılımı `A-Blok +
    B-Blok`) · `:227` Hakediş Tutarı · `:228` Net Tahsil."""
    yaratan = await aktor_fabrikasi("t4-is-yaratan@ok1a.co", full_name="A. Demir")
    await aktor_fabrikasi(
        "t4-muh@ok1a.co", role_key="accounting", approval_roles=[ApprovalRole.accounting]
    )
    basliklar = await giris("t4-muh@ok1a.co")

    document_id, _ = await evrak_fabrikasi(
        _ISVEREN,
        creator=yaratan,
        site_adlari=("A-Blok", "B-Blok"),
        period=(2026, 7),
    )
    await _zincir(seeded_db, _ISVEREN, document_id, yaratan)

    yanit = await client.get("/approvals", headers=basliklar)

    assert yanit.status_code == 200, yanit.text
    (satir,) = yanit.json()["items"]
    assert satir["title"] == "Güneşkent 1 — İşveren Hakediş #5"
    assert satir["subtitle"] == "A-Blok + B-Blok · 07/2026"
    assert satir["gross_amount"] == "100000.00"
    assert satir["net_amount"] == "115000.00"


async def test_gross_amount_CANLIDIR_amount_snapshot_DONMUS_carpandir(
    client, seeded_db, aktor_fabrikasi, evrak_fabrikasi, giris
):
    """İki sayı AYNI ŞEY DEĞİLDİR ve karıştırılırsa ikisi de yalan söyler.

    `amount_snapshot` zincir kurulurken donan EŞİK ÇARPANIDIR (MK-2 kanonu);
    `gross_amount` mockup `:138`in bugünkü değeridir. Kart donmuş çarpanı
    basmaz, evrağın kendi tutarını basar.
    """
    yaratan = await aktor_fabrikasi("t4-snap-yaratan@ok1a.co")
    await aktor_fabrikasi(
        "t4-snap-sef@ok1a.co", role_key="site_chief", approval_roles=[ApprovalRole.site_chief]
    )
    basliklar = await giris("t4-snap-sef@ok1a.co")

    document_id, _ = await evrak_fabrikasi(_TASERON, creator=yaratan)
    await _zincir(seeded_db, _TASERON, document_id, yaratan, amount=Decimal("1.00"))

    yanit = await client.get("/approvals", headers=basliklar)

    (satir,) = yanit.json()["items"]
    assert satir["amount_snapshot"] == "1.00"
    assert satir["gross_amount"] == "100000.00"


async def test_kaynagi_COZULEMEYEN_zincir_kutuda_GORUNMEZ(
    client, seeded_db, aktor_fabrikasi, giris
):
    """FAIL-CLOSED (SA kanonu): evrağı bulunamayan zincirin PROJESİ de
    bilinmez, dolayısıyla görünürlük kararı VERİLEMEZ. Bilinmeyen BÜYÜK
    sayılır — satır gizlenir. Açık bırakılsaydı, evrağı silinmiş bir zincir
    kapsamı ne olursa olsun HERKESE görünürdü.
    """
    yaratan = await aktor_fabrikasi("t4-oksuz-yaratan@ok1a.co")
    await aktor_fabrikasi(
        "t4-oksuz-sef@ok1a.co", role_key="site_chief", approval_roles=[ApprovalRole.site_chief]
    )
    basliklar = await giris("t4-oksuz-sef@ok1a.co")

    # `create_chain` BOŞ proje çözemez (IZN-B3b: adım rolleri belgenin projesinde sahip ister);
    # evrağı silinmiş zincir DOĞRUDAN yazılır. Aktör "Tüm projeler" + ana rol `site_chief` olduğu
    # için adımın SAHİBİDİR — gizleyen tek şey kaynağın çözülememesidir (fail-closed).
    zincir = ApprovalChain(
        document_type=_TASERON,
        document_id=uuid.uuid4(),
        threshold_snapshot=Decimal("500000.00"),
        amount_snapshot=Decimal("100.00"),
        created_by_user_id=yaratan.id,
    )
    seeded_db.add(zincir)
    await seeded_db.flush()
    seeded_db.add(
        ApprovalStep(chain_id=zincir.id, step_no=1, approval_role=ApprovalRole.site_chief)
    )
    await seeded_db.flush()

    yanit = await client.get("/approvals", headers=basliklar)

    govde = yanit.json()
    assert govde["items"] == []
    assert govde["total"] == 0


# --------------------------------------------------------------------------- #
# 2. IDOR — proje görünürlüğü
# --------------------------------------------------------------------------- #


async def test_GORMEDIGI_projenin_evraki_kutuda_YOK_ve_KARSI_TARAF_ile_TUTAR_SIZMAZ(
    client, seeded_db, aktor_fabrikasi, evrak_fabrikasi, giris
):
    """🔴 IDOR bekçisi. Aktörün onay ROLÜ doğrudur, adım da SIRADAKİDİR —
    tek engel PROJE KAPSAMIDIR. Sızıntı iddiası satır sayısıyla YETİNMEZ:
    karşı tarafın adı ve tutar gövdenin HİÇBİR YERİNDE geçmemelidir (başlık,
    alt başlık ve tutar alanları üç ayrı sızma yüzeyidir).
    """
    yaratan = await aktor_fabrikasi("t4-idor-yaratan@ok1a.co")
    gorunen = await aktor_fabrikasi("t4-idor-gorunen@ok1a.co", full_name="Görünen")
    kapsam_disi_id, kapsam_disi_proje = await evrak_fabrikasi(
        _TASERON,
        creator=yaratan,
        subcontractor_name="Gizli Taşeron A.Ş.",
        unit_price=Decimal("7777.00"),
        quantity=Decimal("1"),
    )
    gorunur_id, gorunur_proje = await evrak_fabrikasi(
        _TASERON, creator=gorunen, subcontractor_name="Açık Taşeron"
    )
    await aktor_fabrikasi(
        "t4-idor-sef@ok1a.co",
        role_key="site_chief",
        approval_roles=[ApprovalRole.site_chief],
        projeler=[gorunur_proje],
    )
    basliklar = await giris("t4-idor-sef@ok1a.co")

    await _zincir(seeded_db, _TASERON, kapsam_disi_id, yaratan)
    await _zincir(seeded_db, _TASERON, gorunur_id, gorunen)

    yanit = await client.get("/approvals", headers=basliklar)

    assert yanit.status_code == 200, yanit.text
    govde = yanit.json()
    assert [satir["document_id"] for satir in govde["items"]] == [str(gorunur_id)]
    assert "Gizli Taşeron A.Ş." not in yanit.text
    assert "7777" not in yanit.text
    assert str(kapsam_disi_proje.id) not in yanit.text


async def test_TOPLAM_yetki_suzgecini_SQL_COUNTUN_ICINDE_tasir(
    client, seeded_db, aktor_fabrikasi, evrak_fabrikasi, giris
):
    """🔴 BOR-TEMİZ kanonu: süzgeç `COUNT`un İÇİNDEDİR.

    `items` süzülüp `total` süzülmeseydi kullanıcı GÖREMEDİĞİ kayıtları SAYAR
    ve sayfalayıcı boş sayfalar üretirdi — `items` boş ama `total` > 0 olan bir
    kurulum SAHTE-YEŞİLDİR. Üç evrak var, biri görünür: `total` 1 olmalı.
    """
    yaratan = await aktor_fabrikasi("t4-count-yaratan@ok1a.co")
    gorunur_id, gorunur_proje = await evrak_fabrikasi(_TASERON, creator=yaratan)
    for _ in range(2):
        gizli_id, _proje = await evrak_fabrikasi(_TASERON, creator=yaratan)
        await _zincir(seeded_db, _TASERON, gizli_id, yaratan)
    await _zincir(seeded_db, _TASERON, gorunur_id, yaratan)

    await aktor_fabrikasi(
        "t4-count-sef@ok1a.co",
        role_key="site_chief",
        approval_roles=[ApprovalRole.site_chief],
        projeler=[gorunur_proje],
    )
    basliklar = await giris("t4-count-sef@ok1a.co")

    yanit = await client.get("/approvals?limit=1", headers=basliklar)

    govde = yanit.json()
    assert govde["total"] == 1, "total, görünmeyen iki evrağı da saymamalıydı"
    assert len(govde["items"]) == 1


async def test_KAPSAMSIZ_aktor_HICBIR_satir_gormez(
    client, seeded_db, aktor_fabrikasi, evrak_fabrikasi, giris
):
    """Hiç `user_project_access` satırı olmayan aktör: `visible_projects` boş
    küme döner ve süzgeç BOŞ KÜMEYİ "hepsi" saymamalıdır (klasik `IN ()` tuzağı).
    """
    yaratan = await aktor_fabrikasi("t4-kapsamsiz-yaratan@ok1a.co")
    document_id, _ = await evrak_fabrikasi(_TASERON, creator=yaratan)
    await _zincir(seeded_db, _TASERON, document_id, yaratan)

    await aktor_fabrikasi(
        "t4-kapsamsiz@ok1a.co",
        role_key="site_chief",
        approval_roles=[ApprovalRole.site_chief],
        tum_projeler=False,
    )
    basliklar = await giris("t4-kapsamsiz@ok1a.co")

    govde = (await client.get("/approvals", headers=basliklar)).json()

    assert govde["items"] == []
    assert govde["total"] == 0
    assert "my_approval_roles" not in govde  # IZN-B3b: alan kalktı (rol artık projeye bağlı)


# --------------------------------------------------------------------------- #
# 3. KANON E + K10 — karar/renk sunucuda üretilmez
# --------------------------------------------------------------------------- #


#: Satırın TAM alan kümesi. "şu alan YOK" biçiminde bir iddia ZAYIFTIR: bir gün
#: `is_urgent` eklendiğinde listede olmadığı için sessizce yeşil kalırdı. Tam
#: küme iddiası, EKLENEN her alanı da yakalar (T3'te "tam küme değiştirme"
#: kanonuyla aynı gerekçe).
_SATIR_ALANLARI = {
    "chain_id",
    "document_type",
    "document_id",
    "created_by_name",
    "created_at",
    "threshold_snapshot",
    "amount_snapshot",
    "current_step_no",
    "steps",
    "title",
    "subtitle",
    "gross_amount",
    "net_amount",
    "can_decide",
}


async def test_satirin_TAM_ALAN_KUMESI_karar_ya_da_RENK_TASIMAZ(
    client, seeded_db, aktor_fabrikasi, evrak_fabrikasi, giris
):
    """🔴 KANON E + K10: `can_approve` gibi bir KARAR alanı ve aciliyet/renk gibi
    bir SUNUM alanı YOKTUR. `can_decide` bir OLGUDUR (adımın sahibi aktör mü) ve
    kutuda süzgecin kendisidir (IZN-B3b; `treasury/upcoming.py` emsali)."""
    yaratan = await aktor_fabrikasi("t4-kanone-yaratan@ok1a.co")
    await aktor_fabrikasi(
        "t4-kanone@ok1a.co", role_key="site_chief", approval_roles=[ApprovalRole.site_chief]
    )
    basliklar = await giris("t4-kanone@ok1a.co")
    document_id, _ = await evrak_fabrikasi(_TASERON, creator=yaratan)
    await _zincir(seeded_db, _TASERON, document_id, yaratan)

    (satir,) = (await client.get("/approvals", headers=basliklar)).json()["items"]

    assert set(satir) == _SATIR_ALANLARI
    assert set(satir["steps"][0]) == {"step_no", "approval_role", "decided_at", "decided_by_name"}
    assert satir["can_decide"] is True


async def test_UC_AILE_TEK_CAGRIDA_doner(
    client, seeded_db, aktor_fabrikasi, evrak_fabrikasi, giris
):
    """Mockup tek listede üç ayrı evrak ailesini yan yana basar (`:118` ·
    `:152` · `:211`). Uç bunları TEK çağrıda vermeli."""
    yaratan = await aktor_fabrikasi("t4-uclu-yaratan@ok1a.co")
    aktor = await aktor_fabrikasi("t4-uclu@ok1a.co", role_key="hr_manager", tum_projeler=False)
    basliklar = await giris("t4-uclu@ok1a.co")

    for tip in (_TASERON, _ISVEREN, _SATINALMA):
        document_id, proje = await evrak_fabrikasi(tip, creator=yaratan)
        await proje_rolu_ver(seeded_db, aktor, proje, _ILK_ADIM_ROLU[tip].value)
        await _zincir(seeded_db, tip, document_id, yaratan)

    govde = (await client.get("/approvals", headers=basliklar)).json()

    assert govde["total"] == 3
    assert {satir["document_type"] for satir in govde["items"]} == {
        "subcontractor_progress_payment",
        "progress_payment",
        "purchase_request",
    }
    assert all(satir["title"] for satir in govde["items"])
