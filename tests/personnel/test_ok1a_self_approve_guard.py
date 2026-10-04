"""OK-1A T5 — bir kişi KENDİ izin talebini ONAYLAYAMAZ (kullanıcı kararı 2026-08-21).

Görev emri: `GOREV-EMRI-OK1A-BACKEND.md` T5.

## Kapsam — bu bir KAPI, zincir DEĞİL

🔴 İzin talebi onay ZİNCİRİNE **girmez** (K4): ₺ tutarı yoktur, eşik anlamsızdır.
`ApprovalDocumentType`a yeni üye AÇILMADI, `approvals` motoru izne BAĞLANMADI.
Bu dilim mevcut **tek adımlı** onaya (`personnel` full+) konan tek bir 403
kapısıdır.

## "Kendi talebi" NEREDEN okunur

Talep → `personnel_id` → `Personnel.user_id`. 🔴 ÖLÇÜLDÜ (test DB, `psql \\d personnel`):
`user_id` **NULLABLE**tır (NOT NULL kısıtı yok) ve **TEKİL DEĞİLDİR** (yalnız
`ix_personnel_user_id`, UNIQUE değil). NULL ise talep aktöre ait DEĞİLDİR ve onay
GEÇER — `test_user_id_NULL_...` bunu sabitler.

Tekillik sorunu bu YÖNDE yoktur: bir talebin TEK personeli, bir personelin TEK
`user_id`si vardır. (Belirsizlik ters yöndedir — kullanıcıdan personele — ve
İK-2.1 orada zaten FAIL-CLOSED 409 döner.)

## İSTİSNA: yalnız `admin`

Emsal `approvals/service.py::_assert_can_decide`: **`full` YETMEZ**. `patron`
sistem rolü `personnel=full`dur; istisna ona da açılsaydı "tek kişilik ekipte
kilitlenmeyi önle" gerekçesi, kendi talebini onaylayan ikinci bir sınıfa
dönüşürdü. İstisna denetim günlüğüne `messages.APPROVAL_ON_BEHALF_MARK` ile
geçer — 🔴 yeni `AuditAction` üyesi AÇILMADI, ayrım METİNDEDİR.
"""

from datetime import date, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import timezone
from app.modules.audit import messages
from app.modules.audit.models import AuditLog
from app.modules.personnel import guards
from app.modules.personnel.models import LeaveRequest, LeaveStatus, LeaveType, Personnel
from app.modules.site_diary.models import WorkerSource
from app.modules.users.models import User
from tests.personnel._ik2_leave_decision import _yeni_denetim_metinleri


# ~2 yıl 2 ay kıdem → 4857 birinci kademe (14 gün). Bugüne göre türetilir ki
# test bir yıl sonra sessizce başka bir kıdem penceresine kaymasın.
def _kidemli_giris() -> date:
    return timezone.today() - timedelta(days=800)


async def _login(
    client: AsyncClient, user_factory, role_key: str, email: str
) -> tuple[User, dict[str, str]]:
    user = await user_factory(email=email, password="parola1234", role_key=role_key)
    resp = await client.post("/auth/login", json={"email": email, "password": "parola1234"})
    assert resp.status_code == 200, resp.text
    return user, {"Authorization": f"Bearer {resp.json()['access_token']}"}


async def _personel(session: AsyncSession, full_name: str, user: User | None = None) -> Personnel:
    kayit = Personnel(
        full_name=full_name,
        source=WorkerSource.company,
        hire_date=_kidemli_giris(),
        user_id=None if user is None else user.id,
    )
    session.add(kayit)
    await session.flush()
    return kayit


async def _talep(session: AsyncSession, personel: Personnel, tip: LeaveType) -> LeaveRequest:
    """Talep DOĞRUDAN yazılır: bu dilim TALEP AÇMAYI değil ONAYI sınar; talebi
    hangi ucun açtığı kapının davranışını değiştirmemelidir."""
    kayit = LeaveRequest(
        personnel_id=personel.id,
        leave_type_id=tip.id,
        start_date=timezone.today().replace(month=1, day=1) + timedelta(days=200),
        end_date=timezone.today().replace(month=1, day=1) + timedelta(days=202),
        days=3,
        status=LeaveStatus.pending,
    )
    session.add(kayit)
    await session.flush()
    return kayit


@pytest.fixture
async def yillik(seeded_db: AsyncSession) -> LeaveType:
    tip = LeaveType(name="Yıllık İzin", deducts_from_annual=True, color="#2563eb", sort_order=1)
    seeded_db.add(tip)
    await seeded_db.flush()
    return tip


# --- 1. Normal kullanıcı: KENDİ talebi → 403 -------------------------------


@pytest.mark.asyncio
async def test_kendi_izin_talebini_ONAYLAYAMAZ_403(client, seeded_db, user_factory, yillik):
    """`hr_manager` (`personnel=full`) kendi talebini onaylayamaz.

    Durum kodu TEK BAŞINA iddia edilmez: `_FULL` kapısı da 403 döndürür ve
    yanlış sebeple 403 veren bir uygulamada "403 geldi" YEŞİL kalırdı
    (`approvals/guards.py` dersi). Metin korkuluk sabitiyle karşılaştırılır.
    """
    user, headers = await _login(client, user_factory, "hr_manager", "ik-kendi@ok1a.co")
    personel = await _personel(seeded_db, "İK Yöneticisi", user)
    talep = await _talep(seeded_db, personel, yillik)

    resp = await client.post(f"/leave-requests/{talep.id}/approve", headers=headers)
    assert resp.status_code == 403, resp.text
    assert resp.json()["detail"] == guards.LEAVE_APPROVE_OWN_REQUEST

    # Ve talep HÂLÂ `pending` — kapı sızdırmadı.
    await seeded_db.refresh(talep)
    assert talep.status is LeaveStatus.pending
    assert talep.decided_by is None


@pytest.mark.asyncio
async def test_full_TEK_BASINA_istisna_ACMAZ_patron_da_403(client, seeded_db, user_factory, yillik):
    """🔴 `patron` `personnel=full`dur ama `admin` DEĞİLDİR → istisna KAPALI.

    `approvals/service.py::_has_document_admin` ile aynı karar: istisna `full`e
    açılsaydı kendi talebini onaylayan ikinci bir sınıf doğardı.
    """
    user, headers = await _login(client, user_factory, "patron", "patron-kendi@ok1a.co")
    personel = await _personel(seeded_db, "Patron", user)
    talep = await _talep(seeded_db, personel, yillik)

    resp = await client.post(f"/leave-requests/{talep.id}/approve", headers=headers)
    assert resp.status_code == 403, resp.text
    assert resp.json()["detail"] == guards.LEAVE_APPROVE_OWN_REQUEST


# --- 2. `admin` istisnası + "vekâleten" izi --------------------------------


@pytest.mark.asyncio
async def test_admin_KENDI_talebini_onaylar_ve_denetimde_VEKALETEN_isareti_kalir(
    client, seeded_db, user_factory, yillik
):
    """`system_admin` (`personnel=admin`) TEK istisnadır; iz METİNDEDİR.

    İşaret dize olarak GÖMÜLMEZ — motorun sabiti (`messages.APPROVAL_ON_BEHALF_MARK`)
    ile iddia edilir ki metin değişirse test onunla birlikte kaysın.
    """
    user, headers = await _login(client, user_factory, "system_admin", "admin-kendi@ok1a.co")
    personel = await _personel(seeded_db, "Sistem Yöneticisi", user)
    talep = await _talep(seeded_db, personel, yillik)
    onceki = set(await seeded_db.scalars(select(AuditLog.id)))

    resp = await client.post(f"/leave-requests/{talep.id}/approve", headers=headers)
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == LeaveStatus.approved.value
    assert resp.json()["decided_by"] == str(user.id)

    metinler = await _yeni_denetim_metinleri(seeded_db, onceki)
    assert len(metinler) == 1, metinler
    assert messages.APPROVAL_ON_BEHALF_MARK in metinler[0], metinler[0]
    # Evrağın kendi cümlesi KAYBOLMAZ — işaret onun ÜSTÜNE eklenir.
    assert "onaylandı" in metinler[0]
    assert "Sistem Yöneticisi" in metinler[0]


# --- 3. Regresyon: BAŞKASININ talebi etkilenmez ---------------------------


@pytest.mark.asyncio
async def test_baskasinin_talebini_onaylamak_ETKILENMEZ(client, seeded_db, user_factory, yillik):
    """Bugünkü davranış aynen: `full` kullanıcı BAŞKASININ talebini onaylar ve
    denetim satırı "vekâleten" işaretini TAŞIMAZ."""
    user, headers = await _login(client, user_factory, "hr_manager", "ik-baskasi@ok1a.co")
    await _personel(seeded_db, "İK Yöneticisi", user)  # aktörün KENDİ kaydı da var
    baskasi = await _personel(seeded_db, "Ali Kaya")
    talep = await _talep(seeded_db, baskasi, yillik)
    onceki = set(await seeded_db.scalars(select(AuditLog.id)))

    resp = await client.post(f"/leave-requests/{talep.id}/approve", headers=headers)
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == LeaveStatus.approved.value

    metinler = await _yeni_denetim_metinleri(seeded_db, onceki)
    assert len(metinler) == 1, metinler
    assert messages.APPROVAL_ON_BEHALF_MARK not in metinler[0], metinler[0]


@pytest.mark.asyncio
async def test_admin_BASKASININ_talebinde_vekaleten_isareti_YOK(
    client, seeded_db, user_factory, yillik
):
    """İşaret `admin` OLMANIN değil, KENDİ TALEBİ olmanın izidir."""
    user, headers = await _login(client, user_factory, "system_admin", "admin-baskasi@ok1a.co")
    await _personel(seeded_db, "Sistem Yöneticisi", user)
    baskasi = await _personel(seeded_db, "Veli Demir")
    talep = await _talep(seeded_db, baskasi, yillik)
    onceki = set(await seeded_db.scalars(select(AuditLog.id)))

    resp = await client.post(f"/leave-requests/{talep.id}/approve", headers=headers)
    assert resp.status_code == 200, resp.text
    metinler = await _yeni_denetim_metinleri(seeded_db, onceki)
    assert len(metinler) == 1, metinler
    assert messages.APPROVAL_ON_BEHALF_MARK not in metinler[0], metinler[0]


# --- 4. `user_id` NULL → aktöre ait SAYILMAZ ------------------------------


@pytest.mark.asyncio
async def test_user_id_NULL_personel_aktore_ait_SAYILMAZ_onay_GECER(
    client, seeded_db, user_factory, yillik
):
    """🔴 ÖLÇÜLDÜ: `personnel.user_id` NULLABLE'dır ve saha personelinin çoğunun
    login'i YOKTUR. NULL'ı "eşleşti" saymak (ya da aktörün `user_id`si NULL
    olabilirmiş gibi davranmak) TÜM login'siz personelin izinlerini KİLİTLERDİ.
    """
    _, headers = await _login(client, user_factory, "hr_manager", "ik-null@ok1a.co")
    loginsiz = await _personel(seeded_db, "Login'siz İşçi")
    assert loginsiz.user_id is None
    talep = await _talep(seeded_db, loginsiz, yillik)

    resp = await client.post(f"/leave-requests/{talep.id}/approve", headers=headers)
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == LeaveStatus.approved.value
