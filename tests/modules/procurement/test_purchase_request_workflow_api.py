"""SA T3 — talebin onay akışı: `submit` / `approve` / `reject` + ₺500K eşiği.

Spec: `docs/superpowers/specs/2026-08-12-sa-satinalma-design.md` §3, §7 S1/S2.

## Bu dosyanın iki ağır sorumluluğu

1. **Geçiş matrisi dışı her çift 409.** Tablo-güdümlü test matrisin TÜM
   tamamlayıcısını (durum × işlem kartezyeni eksi tabloda olanlar) dolaşır:
   yeni bir durum ya da işlem eklendiğinde varsayılan davranış REDDETMEKTİR ve
   bu testi güncellemeden yeni bir kapı açılamaz.
2. **Eşik ATLATMA senaryosu.** ₺500K sınırı `approve` ANINDA, GÜNCEL
   kalemlerden yeniden hesaplanır. Talebin üstünde donmuş bir tutar YOKTUR
   (`purchase_requests`ta tutar kolonu açılmadı) ama savunma derinliği yine de
   BİZZAT denenir: `pending_approval` durumundaki bir talebin kalemleri DB'de
   şişirilir, sonra düşük seviyeli rolle onay denenir → 403.
"""

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.modules.procurement import transitions
from app.modules.procurement.models import (
    PurchaseRequest,
    PurchaseRequestLine,
    PurchaseRequestStatus,
)
from app.modules.users.models import ProjectMember

_YOL = "/purchase-requests"


async def _durum(session, request_id) -> PurchaseRequestStatus:
    await session.flush()
    kayit = (
        await session.execute(select(PurchaseRequest).where(PurchaseRequest.id == request_id))
    ).scalar_one()
    await session.refresh(kayit)
    return kayit.status


# --- submit ---


async def test_submit_taslagi_onaya_gonderir(
    client, sef_headers, seeded_db, gorunen_proje, talep_fabrikasi
):
    talep = await talep_fabrikasi(gorunen_proje, lines=[("10.000", "500.00")])

    yanit = await client.post(f"{_YOL}/{talep.id}/submit", headers=sef_headers)

    assert yanit.status_code == 200, yanit.text
    assert yanit.json()["status"] == "pending_approval"
    assert await _durum(seeded_db, talep.id) is PurchaseRequestStatus.pending_approval


async def test_submit_eksik_talepte_tum_engelleri_tek_422de_verir(
    client, sef_headers, gorunen_proje, talep_fabrikasi
):
    """`validation.submit_blockers` LİSTE döndürür ve uç hepsini birden gösterir.

    Kullanıcıya eksikleri birer birer keşfettirmek FST gibi uzun bir formda
    kabul edilemez — bu yüzden ilk engelde durulmaz.
    """
    talep = await talep_fabrikasi(gorunen_proje, needed_by=None, lines=[])

    yanit = await client.post(f"{_YOL}/{talep.id}/submit", headers=sef_headers)

    assert yanit.status_code == 422, yanit.text
    detay = yanit.json()["detail"]
    assert "İhtiyaç tarihi zorunludur" in detay
    assert "En az bir malzeme kalemi gereklidir" in detay


async def test_submit_FIYATSIZ_kalemi_reddeder(
    client, sef_headers, seeded_db, gorunen_proje, talep_fabrikasi
):
    """🛑 T5 BULGUSU — eşiğin BİRİNCİ katmanı.

    Tahmini fiyat taslakta opsiyoneldir ama onaya gönderirken zorunludur:
    ₺500K eşiği (FST 166) tahmini TOPLAMDAN hesaplanır ve fiyatsız kalem
    toplama girmez. Mockup da bunu söyler — FST 168 kutusu hükmünü
    ("₺340.900 · Patron onayı gerekmiyor") toplamdan verir, yani toplam onay
    akışının GİRDİSİDİR.
    """
    talep = await talep_fabrikasi(gorunen_proje, lines=[("15.000", None)])

    yanit = await client.post(f"{_YOL}/{talep.id}/submit", headers=sef_headers)

    assert yanit.status_code == 422, yanit.text
    assert "tahmini birim fiyat" in yanit.json()["detail"].lower()
    assert await _durum(seeded_db, talep.id) is PurchaseRequestStatus.draft


async def test_submit_engellendiginde_durum_DEGISMEZ(
    client, sef_headers, seeded_db, gorunen_proje, talep_fabrikasi
):
    talep = await talep_fabrikasi(gorunen_proje, needed_by=None, lines=[])

    assert (await client.post(f"{_YOL}/{talep.id}/submit", headers=sef_headers)).status_code == 422
    assert await _durum(seeded_db, talep.id) is PurchaseRequestStatus.draft


# --- approve / reject ---


async def test_approve_otomatik_teklif_bekleniyora_gecer_ve_damgalar(
    client, pm_headers, seeded_db, gorunen_proje, talep_fabrikasi, kullanici_kimligi
):
    """§3: `approve` ARA bir "onaylandı" durumu üretmez, doğrudan `quote_wait`.

    Onaydan sonraki iş teklif toplamaktır; ayrı bir "approved" durumu ekranda
    hiçbir şey ifade etmez ve SAT rozetlerinde de yoktur.
    """
    talep = await talep_fabrikasi(
        gorunen_proje, status=PurchaseRequestStatus.pending_approval, lines=[("2.000", "1000.00")]
    )

    yanit = await client.post(f"{_YOL}/{talep.id}/approve", headers=pm_headers)

    assert yanit.status_code == 200, yanit.text
    govde = yanit.json()
    assert govde["status"] == "quote_wait"
    assert govde["approved_by_user_id"] == str(await kullanici_kimligi("pm@satinalma.co"))
    assert govde["approved_at"] is not None
    assert govde["rejected_at"] is None
    assert await _durum(seeded_db, talep.id) is PurchaseRequestStatus.quote_wait


async def test_reject_gerekce_zorunludur(client, pm_headers, gorunen_proje, talep_fabrikasi):
    """TH emsali: gerekçesiz ret 422. Boş/boşluk gerekçe de reddedilir."""
    talep = await talep_fabrikasi(
        gorunen_proje, status=PurchaseRequestStatus.pending_approval, lines=[("1.000", "10.00")]
    )

    for govde in ({}, {"reason": ""}, {"reason": "   "}):
        yanit = await client.post(f"{_YOL}/{talep.id}/reject", json=govde, headers=pm_headers)
        assert yanit.status_code == 422, (govde, yanit.text)


async def test_reject_gerekceyi_ve_damgayi_yazar(
    client, pm_headers, seeded_db, gorunen_proje, talep_fabrikasi
):
    talep = await talep_fabrikasi(
        gorunen_proje, status=PurchaseRequestStatus.pending_approval, lines=[("1.000", "10.00")]
    )

    yanit = await client.post(
        f"{_YOL}/{talep.id}/reject", json={"reason": "Bütçe dönemi kapandı"}, headers=pm_headers
    )

    assert yanit.status_code == 200, yanit.text
    govde = yanit.json()
    assert govde["status"] == "rejected"
    assert govde["rejection_reason"] == "Bütçe dönemi kapandı"
    assert govde["rejected_at"] is not None
    assert govde["approved_by_user_id"] is None
    assert await _durum(seeded_db, talep.id) is PurchaseRequestStatus.rejected


# --- Geçiş matrisi ---


def _matris_disi_ciftler() -> list[tuple[PurchaseRequestStatus, transitions.RequestAction]]:
    """Kartezyen eksi tablo = REDDEDİLMESİ gereken her çift."""
    return [
        (durum, islem)
        for durum in PurchaseRequestStatus
        for islem in transitions.RequestAction
        if (durum, islem) not in transitions.REQUEST_TRANSITIONS
    ]


def test_matris_tam_olarak_dort_gecis_tanimlar():
    """§3'ün ELLE tetiklenen geçiş kümesi: submit · approve · reject ·
    select-and-order.

    Sayı bir korkuluktur: beşinci bir geçiş sessizce eklenemesin. `delivered`
    BURADA YOKTUR ve T4'ten sonra da yoktur — elle bir uçla ulaşılabilir olsaydı
    stok girişi olmadan "teslim edildi" damgası düşerdi. Teslim, `stock_link`in
    AYRI tablosundan geçer (`REQUEST_DELIVERY_TRANSITIONS`, aşağıda) ve o tablo
    kendi literal testine de sahiptir (`test_stock_entry_delivery_chain`).
    """
    assert transitions.REQUEST_DELIVERY_TRANSITIONS == frozenset(
        {(PurchaseRequestStatus.ordered, PurchaseRequestStatus.delivered)}
    )
    assert transitions.REQUEST_TRANSITIONS == {
        (PurchaseRequestStatus.draft, transitions.RequestAction.submit): (
            PurchaseRequestStatus.pending_approval
        ),
        (PurchaseRequestStatus.pending_approval, transitions.RequestAction.approve): (
            PurchaseRequestStatus.quote_wait
        ),
        (PurchaseRequestStatus.pending_approval, transitions.RequestAction.reject): (
            PurchaseRequestStatus.rejected
        ),
        (PurchaseRequestStatus.quote_wait, transitions.RequestAction.select_and_order): (
            PurchaseRequestStatus.ordered
        ),
    }


@pytest.mark.parametrize(
    ("durum", "islem"),
    [
        (durum, islem)
        for durum, islem in _matris_disi_ciftler()
        # `select-and-order` kendi ucuyla (teklif altında) sınanır — orada
        # gövde bir TEKLİF kimliği ister, bu yüzden bu tabloda yeri yoktur.
        if islem is not transitions.RequestAction.select_and_order
    ],
)
async def test_matris_disi_her_gecis_409(
    client, admin_headers, gorunen_proje, talep_fabrikasi, durum, islem
):
    """Tabloda OLMAYAN her çift 409 — "tanımlı olanı say, gerisini reddet".

    Aktör `admin_headers`tır (`procurement=_A`): yetki HİÇBİR dalda engel
    değildir, dolayısıyla dönen kod yalnızca DURUM çakışmasını gösterir.
    """
    talep = await talep_fabrikasi(gorunen_proje, status=durum, lines=[("1.000", "10.00")])
    govde = {"reason": "gerekçe"} if islem is transitions.RequestAction.reject else None

    yanit = await client.post(f"{_YOL}/{talep.id}/{islem.value}", json=govde, headers=admin_headers)

    assert yanit.status_code == 409, (durum, islem, yanit.text)
    assert yanit.json()["detail"] == "Satın alma talebinin durumu bu işleme uygun değil"


# --- ₺500K eşiği ---


def test_esik_tek_kaynak_AYARDIR():
    """Sihirli sayı YOK — ve artık sabit de YOK: eşik AYARDAN okunur (OK-1A R6).

    🔴 **UYARLANDI (OK-1A T2).** Eski hâli `transitions.APPROVAL_THRESHOLD_TRY`
    sabitinin ₺500.000 olduğunu iddia ediyordu. OK-1A eşiği
    `company.approval_threshold_try` ayarına taşıdı: iki eşik (satınalmanın
    sabiti + onay zincirinin ayarı) bir arada yaşasaydı kaçınılmaz olarak
    AYRIŞIRLAR ve aynı tutardaki bir talep satınalmada eşiğin altında, onay
    zincirinde üstünde sayılırdı.

    Sabitin YOKLUĞU da iddiadır: modül ona geri dönerse test kırılır. Sayının
    kendisi artık kolonun `server_default`ı ve
    `approvals.definitions.DEFAULT_APPROVAL_THRESHOLD_TRY`dir — TEK kaynak.

    `APPROVAL_THRESHOLD_LEVEL` DEĞİŞMEDİ: "kim onaylayabilir" sorusu hâlâ izin
    seviyesiyle yanıtlanır (onay ROLÜ zincirin işidir, bu kapının değil).
    """
    from app.core.access import AccessLevel
    from app.modules.approvals import definitions

    assert not hasattr(transitions, "APPROVAL_THRESHOLD_TRY"), (
        "eşik yeniden sabite döndü — iki eşik doğar ve ayrışır (OK-1A R6)"
    )
    assert definitions.DEFAULT_APPROVAL_THRESHOLD_TRY == Decimal("500000.00")
    assert transitions.APPROVAL_THRESHOLD_LEVEL is AccessLevel.full


async def test_esik_AYARDAN_okunur_ayar_dusunce_kapi_KAPANIR(
    client, admin_headers, pm_headers, gorunen_proje, talep_fabrikasi, seeded_db
):
    """🔴 R6'nın DAVRANIŞ kanıtı — sabit yerine ayar okunuyor mu?

    Yukarıdaki `hasattr` iddiası tek başına SAHTE-YEŞİL olabilirdi: sabit
    silinip yerine yine gömülü bir sayı yazılsaydı o test yeşil kalırdı. Bu
    test ayarı UÇTAN değiştirir ve aynı tutarın kapıyı önce GEÇTİĞİNİ, ayar
    düşünce GEÇEMEDİĞİNİ ölçer.
    """
    ucuz = await talep_fabrikasi(
        gorunen_proje,
        status=PurchaseRequestStatus.pending_approval,
        lines=[("1.000", "100000.00")],
    )
    once = await client.post(f"{_YOL}/{ucuz.id}/approve", headers=pm_headers)
    assert once.status_code == 200, once.text

    ayar = await client.put(
        "/approvals/settings",
        json={"approval_threshold_try": "50000.00"},
        headers=admin_headers,
    )
    assert ayar.status_code == 200, ayar.text

    ayni_tutar = await talep_fabrikasi(
        gorunen_proje,
        status=PurchaseRequestStatus.pending_approval,
        lines=[("1.000", "100000.00")],
    )
    sonra = await client.post(f"{_YOL}/{ayni_tutar.id}/approve", headers=pm_headers)
    assert sonra.status_code == 403, sonra.text


@pytest.mark.parametrize(
    ("fiyat", "beklenen"),
    [
        # Sınır ALTI: PM (`_APR`) onaylar.
        ("499999.99", 200),
        # Sınır ÜSTÜ — eşik DAHİL (≥): PM onaylayamaz.
        ("500000.00", 403),
        ("500000.01", 403),
    ],
)
async def test_esik_sinirlari_normal_onaycida(
    client, pm_headers, gorunen_proje, talep_fabrikasi, fiyat, beklenen
):
    talep = await talep_fabrikasi(
        gorunen_proje, status=PurchaseRequestStatus.pending_approval, lines=[("1.000", fiyat)]
    )

    yanit = await client.post(f"{_YOL}/{talep.id}/approve", headers=pm_headers)

    assert yanit.status_code == beklenen, yanit.text


async def test_esik_ustunu_ust_seviye_rol_onaylar(
    client, satinalma_headers, gorunen_proje, talep_fabrikasi
):
    """`procurement` rolü `_F`dir; FST 166'nın "Patron"u da `_F`. Eşik üstü onay
    kapısı `full`tur — YENİ bir rol/izin İCAT EDİLMEDİ."""
    talep = await talep_fabrikasi(
        gorunen_proje, status=PurchaseRequestStatus.pending_approval, lines=[("1.000", "750000.00")]
    )

    yanit = await client.post(f"{_YOL}/{talep.id}/approve", headers=satinalma_headers)

    assert yanit.status_code == 200, yanit.text
    assert yanit.json()["status"] == "quote_wait"


async def test_kalemsiz_talepte_toplam_sifir_sayilir(
    client, pm_headers, gorunen_proje, talep_fabrikasi
):
    """Hiç kalemi olmayan talepte harcanacak tutar YOKTUR — eşik altındadır.

    (Böyle bir talep `submit`ten geçemez; buradaki kayıt doğrudan
    `pending_approval` kurulur ve yalnız EŞİK kuralının sıfır davranışını
    ölçer.)
    """
    talep = await talep_fabrikasi(
        gorunen_proje, status=PurchaseRequestStatus.pending_approval, lines=[]
    )

    assert (await client.post(f"{_YOL}/{talep.id}/approve", headers=pm_headers)).status_code == 200


async def test_FIYATSIZ_kalem_esigin_USTU_sayilir_fail_closed(
    client, pm_headers, seeded_db, gorunen_proje, talep_fabrikasi
):
    """🛑 T5 BULGUSU — eşik atlatmanın DB'siz yolu.

    Fiyatsız kalem toplama girmez (`SUM` NULL'ları yutar), yani "eksik fiyat"
    ile "düşük tutar" aynı 0'ı üretir. Eskiden bu eşik ALTI sayılıyordu:
    ₺2M'lik bir talep, tahmini fiyat alanı boş bırakılarak toplam 0 gösterir
    ve DB'ye hiç dokunmadan, yalnızca bir alanı doldurmayarak en düşük
    yetkiliden geçerdi. Artık BİLİNMEYEN TUTAR BÜYÜK sayılır (fail-closed).
    """
    talep = await talep_fabrikasi(
        gorunen_proje,
        status=PurchaseRequestStatus.pending_approval,
        lines=[("900000.000", None)],
    )

    yanit = await client.post(f"{_YOL}/{talep.id}/approve", headers=pm_headers)

    assert yanit.status_code == 403, yanit.text
    assert await _durum(seeded_db, talep.id) is PurchaseRequestStatus.pending_approval


async def test_fiyatsiz_kalemi_ust_seviye_rol_onaylayabilir(
    client, satinalma_headers, gorunen_proje, talep_fabrikasi
):
    """Fail-closed kural bir KİLİT değil YÖNLENDİRMEDİR: tutarı bilinmeyen
    talep üst seviyeye çıkar, orada onaylanabilir. Aksi hâlde fiyatsız talep
    hiç onaylanamaz olur ve akış tıkanırdı."""
    talep = await talep_fabrikasi(
        gorunen_proje,
        status=PurchaseRequestStatus.pending_approval,
        lines=[("900000.000", None)],
    )

    yanit = await client.post(f"{_YOL}/{talep.id}/approve", headers=satinalma_headers)

    assert yanit.status_code == 200, yanit.text


async def test_esik_ATLATMA_denemesi_onay_aninda_yakalanir(
    client, pm_headers, seeded_db, gorunen_proje, talep_fabrikasi
):
    """🛑 Dilimin 1 numaralı tuzağı — BİZZAT denenen saldırı.

    Senaryo: talep düşük tutarla onaya gönderilir (`pending_approval`), sonra
    kalemleri ŞİŞİRİLİR ve düşük seviyeli rolle onay denenir. Eşik, submit
    anında hesaplanıp saklanan bir değerden okunsaydı onay GEÇERDİ.

    Şişirme burada DB'den yapılır çünkü PATCH ucu `pending_approval`da 409
    verir (T2). Test bu yüzden uç katmanının değil EŞİK KURALININ savunma
    derinliğini ölçer: kural, kaydın o ANKİ kalemlerine bakmak zorundadır.
    """
    talep = await talep_fabrikasi(
        gorunen_proje, status=PurchaseRequestStatus.pending_approval, lines=[("1.000", "1000.00")]
    )

    satir = (
        await seeded_db.execute(
            select(PurchaseRequestLine).where(PurchaseRequestLine.request_id == talep.id)
        )
    ).scalar_one()
    satir.estimated_unit_price = Decimal("900000.00")
    await seeded_db.flush()

    yanit = await client.post(f"{_YOL}/{talep.id}/approve", headers=pm_headers)

    assert yanit.status_code == 403, yanit.text
    assert yanit.json()["detail"] == (
        "Bu tutardaki bir talebi onaylamak için üst seviye yetki gerekir"
    )
    assert await _durum(seeded_db, talep.id) is PurchaseRequestStatus.pending_approval


# --- Yetki ve kapsam ---


@pytest.mark.parametrize("islem", ["approve", "reject"])
async def test_onay_uclari_talep_yazma_seviyesiyle_acilmaz(
    client, sef_headers, gorunen_proje, talep_fabrikasi, islem
):
    """Şef (`_REQ`) talebi AÇAR ama ONAYLAMAZ — onay kapısı `approve`dur."""
    talep = await talep_fabrikasi(
        gorunen_proje, status=PurchaseRequestStatus.pending_approval, lines=[("1.000", "10.00")]
    )

    yanit = await client.post(
        f"{_YOL}/{talep.id}/{islem}", json={"reason": "olmaz"}, headers=sef_headers
    )

    assert yanit.status_code == 403, yanit.text


async def test_submit_talep_yazma_seviyesi_ister(
    client, yetkisiz_headers, gorunen_proje, talep_fabrikasi
):
    talep = await talep_fabrikasi(gorunen_proje, lines=[("1.000", "10.00")])

    yanit = await client.post(f"{_YOL}/{talep.id}/submit", headers=yetkisiz_headers)

    assert yanit.status_code == 403, yanit.text


@pytest.mark.parametrize("islem", ["submit", "approve", "reject"])
async def test_gorunmeyen_projenin_talebinde_islem_404(
    client, admin_headers, satinalma_headers, gorunmeyen_proje, talep_fabrikasi, islem
):
    """IDOR: kapsam süzgeci DURUM kontrolünden ÖNCE koşar — görünmeyen bir
    talebin durumu hakkında 409 ile bilgi sızdırılmaz."""
    talep = await talep_fabrikasi(
        gorunmeyen_proje,
        status=PurchaseRequestStatus.pending_approval,
        lines=[("1.000", "10.00")],
        created_by_email="admin@satinalma.co",
    )

    yanit = await client.post(
        f"{_YOL}/{talep.id}/{islem}", json={"reason": "x"}, headers=satinalma_headers
    )

    assert yanit.status_code == 404, yanit.text
    assert yanit.json()["detail"] == "Satın alma talebi bulunamadı"


async def test_olmayan_talep_404(client, admin_headers):
    yanit = await client.post(
        f"{_YOL}/00000000-0000-0000-0000-000000000000/submit", headers=admin_headers
    )
    assert yanit.status_code == 404, yanit.text


# --- Denetim ---


async def test_her_gecis_denetim_satiri_yazar(
    client, admin_headers, seeded_db, user_factory, gorunen_proje, talep_fabrikasi
):
    """🔴 OK-1A T3 UYARLAMASI — iddia GENİŞLETİLDİ, zayıflatılmadı.

    `submit` artık gerçek bir onay zinciri açar (Satınalma → Proje Müdürü →
    Muhasebe) ve `/approve` zincirin SIRADAKİ adımını ilerletir. Bu testin
    konusu "her geçiş TAM BİR denetim satırı yazar"dır ve o KORUNUR; değişen
    şey ikinci satırın ARA ADIM metnini taşımasıdır — talep `pending_approval`da
    KALDIĞI için "onaylandı" yazmak günlüğe OLMAMIŞ bir olguyu yazmak olurdu.

    🔴 Metnin talep NUMARASINI taşıması AYRICA iddia edilir: uyarlama sırasında
    ölçülen ilk hâlde ara adım satırı kimliksizdi ve "hangi talep" sorusu
    günlükte yanıtsız kalıyordu (`approvals.service.audit_detail` düzeltildi).
    """
    from app.modules.approvals.models import ApprovalRole, UserApprovalRole
    from app.modules.audit.models import AuditAction, AuditLog

    async def _sayim() -> int:
        return (await seeded_db.execute(select(AuditLog))).scalars().all().__len__()

    onaycı = await user_factory(
        email="sa-denetim@ok1a.co", password="parola1234", role_key="procurement"
    )
    seeded_db.add(
        ProjectMember(user_id=onaycı.id, project_id=gorunen_proje.id, role_id=onaycı.role_id)
    )
    seeded_db.add(UserApprovalRole(user_id=onaycı.id, approval_role=ApprovalRole.procurement))
    await seeded_db.flush()
    giris = await client.post("/auth/login", json={"email": onaycı.email, "password": "parola1234"})
    onay_basliklari = {"Authorization": f"Bearer {giris.json()['access_token']}"}

    talep = await talep_fabrikasi(
        gorunen_proje, lines=[("1.000", "10.00")], needed_by=date(2026, 9, 1)
    )
    once = await _sayim()

    assert (
        await client.post(f"{_YOL}/{talep.id}/submit", headers=admin_headers)
    ).status_code == 200
    ara = await client.post(f"{_YOL}/{talep.id}/approve", headers=onay_basliklari)
    assert ara.status_code == 200, ara.text
    # ARA ADIM: talep ilerlemedi, ama denetim satırı YAZILDI.
    assert ara.json()["status"] == "pending_approval"

    assert await _sayim() == once + 2
    # `occurred_at`e göre SIRALANMAZ: Postgres'te `now()` işlem boyu sabittir ve
    # aynı transaction'daki tüm satırlar birebir aynı damgayı alır (T2'nin
    # `list_requests` dersi) — sıralama o eşitlikte kararsız kalırdı. Kayıtlar
    # bu yüzden METİNDEN çözülür.
    kayitlar = (
        (
            await seeded_db.execute(
                select(AuditLog).where(AuditLog.detail.contains(talep.request_no))
            )
        )
        .scalars()
        .all()
    )
    assert {kayit.action for kayit in kayitlar} == {AuditAction.update, AuditAction.approve}
    metinler = sorted(kayit.detail for kayit in kayitlar)
    assert metinler == [
        f"Onay adımı onaylandı: Satın alma talebi · adım 1/3 · Satınalma · {talep.request_no}",
        f"Satın alma talebi onaya gönderildi: {talep.request_no}",
    ]
