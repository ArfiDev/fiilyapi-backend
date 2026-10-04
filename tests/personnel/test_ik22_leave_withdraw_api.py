"""İK-2.2 — kullanıcı KENDİ bekleyen izin talebini GERİ ÇEKER (HTTP uçtan uca).

Yeni uç: `POST /leave-requests/{request_id}/withdraw` → 200, `LeaveRequestResponse`.

## Bu bir SİLME DEĞİL, DURUM GEÇİŞİdir

`pending -> withdrawn`. Kayıt DB'de KALIR (`test_..._kayit_SILINMEZ`): silme,
talebin var olduğu bilgisini de yok ederdi — İZ kuyruğundan düşen ama denetimde
izi kalan bir "geri çekildi" satırı, kaybolan bir satırdan daha dürüsttür. Bu
yüzden `DELETE /leave-requests/{id}` ucu OLDUĞU GİBİ durur; bu uç onun yerine
GEÇMEZ, onun YANINDA durur (İK/admin siler, SAHİBİ geri çeker).

## Kapı: SAHİPLİK, modül izni DEĞİL

🔴 Uçta `personnel` izni ARANMAZ — İK-2.1 self uçlarının emsali. Yetkinin kaynağı
kaydın SAHİPLİĞİdir (`Personnel.user_id` köprüsü). Kapı `_VIEW`/`_FULL` olsaydı
matriste `personnel=none` olan `procurement` rolündeki çalışan kendi talebini
açabilir (İK-2.1) ama GERİ ÇEKEMEZDİ — bu dilimin var oluş nedeni tam olarak
budur ve `test_personnel_izni_OLMAYAN_self_kullanici_GECER` onu çiviler.

🔴 **`admin` İSTİSNASI YOKTUR.** OK-1A T5'in `admin` istisnası ONAY içindi
(vekâleten karar); geri çekme bir KARAR değil, talep sahibinin KENDİ beyanından
dönmesidir — vekâleten geri çekilen bir talep "sahibi vazgeçti" der ve YALAN
söylerdi. Admin başkasının talebini SİLEBİLİR (o yol açık ve denetim izi
"silindi" der), geri ÇEKEMEZ.

## 404 AYIRT EDİLEMEZ

Sahip olmayan aktör, var olmayan bir id ile BİREBİR aynı cevabı alır (kod + gövde).
Ayrı bir 403 dönmek "bu id'de bir talep VAR" bilgisini sızdırırdı (IDOR keşfi).

## 🔴 SAHTE-YEŞİL KORKULUĞU

Uç yokken FastAPI'nin kendi 404'ü (`{"detail": "Not Found"}`) gelir. Yalnız
`status_code == 404` diyen bir iddia HİÇBİR ŞEYİ bekçilemez ve T2'den sonra da
yeşil kalır. Bu dosyadaki HER 404 iddiası gövde metnini de çakar.
"""

import uuid
from datetime import date, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import timezone
from app.modules.audit.models import AuditAction, AuditLog
from app.modules.personnel import guards
from app.modules.personnel.models import LeaveRequest, LeaveStatus, LeaveType, Personnel
from app.modules.site_diary.models import WorkerSource
from app.modules.users.models import User

# ~2 yıl 2 ay kıdem → 4857 birinci kademe (14 gün). Bugüne göre TÜRETİLİR ki test
# bir yıl sonra sessizce başka bir kıdem penceresine kaymasın (OK-1A dersi).


def _kidemli_giris() -> date:
    return timezone.today() - timedelta(days=800)


def _yil() -> int:
    return timezone.today().year


def _baslangic() -> date:
    return date(_yil(), 9, 1)


def _bitis() -> date:
    return date(_yil(), 9, 3)


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


async def _talep(
    session: AsyncSession,
    personel: Personnel,
    tip: LeaveType,
    *,
    durum: LeaveStatus = LeaveStatus.pending,
    baslangic: date | None = None,
    bitis: date | None = None,
) -> LeaveRequest:
    """Talep DOĞRUDAN yazılır: bu dilim TALEP AÇMAYI değil GERİ ÇEKMEYİ sınar;
    talebi hangi ucun açtığı geri çekmenin davranışını değiştirmemelidir."""
    baslangic = baslangic or _baslangic()
    bitis = bitis or _bitis()
    kayit = LeaveRequest(
        personnel_id=personel.id,
        leave_type_id=tip.id,
        start_date=baslangic,
        end_date=bitis,
        days=(bitis - baslangic).days + 1,
        status=durum,
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


@pytest.fixture
async def calisan(client: AsyncClient, seeded_db: AsyncSession, user_factory):
    """🔴 `procurement` rolü (matriste `personnel=_N`) + KENDİ personel kaydı.

    Rol BİLİNÇLİ seçildi (İK-2.1 emsali): bu kullanıcı `personnel` modülünde
    HİÇBİR yetkiye sahip değildir. Geri çekme çalışıyorsa yetkinin kaynağı
    sahipliktir, modül izni değil.
    """
    user, headers = await _login(client, user_factory, "procurement", "calisan@ik22.co")
    kayit = await _personel(seeded_db, "Mehmet Yılmaz", user)
    return user, headers, kayit


@pytest.fixture
async def ik_calisan(client: AsyncClient, seeded_db: AsyncSession, user_factory):
    """`hr_manager` (`personnel=_F`) + KENDİ personel kaydı — mutlu yolun aktörü."""
    user, headers = await _login(client, user_factory, "hr_manager", "ik-calisan@ik22.co")
    kayit = await _personel(seeded_db, "Ayşe Demir", user)
    return user, headers, kayit


@pytest.fixture
async def baskasi(seeded_db: AsyncSession) -> Personnel:
    """Login'i OLMAYAN başka bir personel — IDOR hedefi (`user_id` NULL)."""
    return await _personel(seeded_db, "Ali Kaya")


async def _yeni_denetim_satirlari(session: AsyncSession, onceki: set[uuid.UUID]) -> list[AuditLog]:
    rows = await session.scalars(select(AuditLog))
    return [row for row in rows if row.id not in onceki]


# --- 1. Mutlu yol: sahip + `pending` → 200, DURUM GEÇİŞİ ------------------


async def test_sahip_bekleyen_talebini_geri_ceker_200(client, seeded_db, ik_calisan, yillik):
    """`pending -> withdrawn` + karar damgası + kayıt DB'de KALIR.

    `reject_reason is None` iddiası bilinçlidir: `_stamp_decision`e `None`
    geçilmezse (ör. red metninin kopyalanması) geri çekilen talep ekranda bir
    RED GEREKÇESİ taşırdı ve kullanıcı reddedildiğini sanırdı.
    """
    user, headers, kayit = ik_calisan
    talep = await _talep(seeded_db, kayit, yillik)
    talep_id = talep.id

    resp = await client.post(f"/leave-requests/{talep_id}/withdraw", headers=headers)
    assert resp.status_code == 200, resp.text
    govde = resp.json()
    assert govde["id"] == str(talep_id)
    assert govde["status"] == "withdrawn"
    assert govde["decided_by"] == str(user.id)
    assert govde["decided_at"] is not None
    assert govde["reject_reason"] is None

    await seeded_db.refresh(talep)
    assert talep.status is LeaveStatus.withdrawn
    assert talep.decided_by == user.id
    assert talep.decided_at is not None
    assert talep.reject_reason is None

    # 🔴 SİLME DEĞİL: satır DB'de DURUYOR. Kimlik haritasına değil, DB'ye sorulur.
    sayi = await seeded_db.scalar(
        select(func.count()).select_from(LeaveRequest).where(LeaveRequest.id == talep_id)
    )
    assert sayi == 1, "geri çekme kaydı SİLDİ — bu bir durum geçişi olmalıydı"


# --- 2. 🔴 DİLİMİN KALBİ: `personnel=none` self kullanıcı GEÇER -----------


async def test_personnel_izni_OLMAYAN_self_kullanici_GECER(client, seeded_db, calisan, yillik):
    """🔴 `procurement` (`personnel=_N`) KENDİ talebini geri çeker → 200.

    Uçta `_VIEW`/`_FULL` kapısı OLSAYDI bu istek 403 alırdı. Aynı kullanıcının
    klasik uçlarda HÂLÂ 403 aldığı da çakılır: genişleme SADECE bu uçtadır,
    `personnel` modülü açılmadı.
    """
    user, headers, kayit = calisan
    talep = await _talep(seeded_db, kayit, yillik)

    # Aynı kullanıcı klasik uçlarda HÂLÂ kapıda durur — genişleme dar.
    assert (await client.get("/leave-requests", headers=headers)).status_code == 403
    assert (await client.get("/personnel", headers=headers)).status_code == 403

    resp = await client.post(f"/leave-requests/{talep.id}/withdraw", headers=headers)
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "withdrawn"
    assert resp.json()["decided_by"] == str(user.id)


# --- 3. Sahip DEĞİL → var olmayan id'den AYIRT EDİLEMEZ -------------------


async def test_sahibi_OLMAYAN_404_var_olmayan_id_den_AYIRT_EDILEMEZ(
    client, seeded_db, calisan, baskasi, yillik
):
    """K: sahiplik denetimi KİLİTTEN ÖNCE ve cevap 403 DEĞİL 404'tür.

    403 dönmek "bu id'de gerçek bir talep var" bilgisini sızdırırdı — saldırgan
    id uzayını tarayarak var olan talepleri sayabilirdi. İki cevap BİREBİR aynı
    olmalıdır: hem kod hem gövde.
    """
    _, headers, _ = calisan
    baskasinin = await _talep(seeded_db, baskasi, yillik)
    yok = uuid.uuid4()

    var_olan = await client.post(f"/leave-requests/{baskasinin.id}/withdraw", headers=headers)
    olmayan = await client.post(f"/leave-requests/{yok}/withdraw", headers=headers)

    assert var_olan.status_code == 404, var_olan.text
    assert var_olan.json()["detail"] == guards.LEAVE_REQUEST_MISSING
    assert olmayan.status_code == 404, olmayan.text
    assert olmayan.json()["detail"] == guards.LEAVE_REQUEST_MISSING
    # Ve ikisi BİREBİR aynı — ayırt edilemez.
    assert var_olan.status_code == olmayan.status_code
    assert var_olan.json() == olmayan.json()

    # Başkasının talebi DOKUNULMADAN `pending` kaldı.
    await seeded_db.refresh(baskasinin)
    assert baskasinin.status is LeaveStatus.pending
    assert baskasinin.decided_by is None


# --- 4. 🔴 `admin` istisnası YOK ------------------------------------------


async def test_admin_BASKASININ_talebini_geri_CEKEMEZ_ama_SILEBILIR(
    client, seeded_db, admin_headers, baskasi, yillik
):
    """🔴 OK-1A T5'in `admin` istisnası BURAYA TAŞINMAZ.

    Onayda istisna vardı çünkü onay bir KARARDIR ve tek kişilik ekipte kilitlenme
    doğururdu. Geri çekme bir karar değil, TALEP SAHİBİNİN BEYANINDAN DÖNMESİdir;
    vekâleten geri çekme denetim günlüğüne "sahibi vazgeçti" yazdırırdı — yalan.

    404'ün "kayıt gerçekten yok"tan geldiği sanılmasın diye AYNI admin AYNI kaydı
    hemen ardından SİLEBİLİYOR: yani kayıt oradaydı, kapalı olan bu UÇtu.
    """
    talep = await _talep(seeded_db, baskasi, yillik)
    talep_id = talep.id

    resp = await client.post(f"/leave-requests/{talep_id}/withdraw", headers=admin_headers)
    assert resp.status_code == 404, resp.text
    assert resp.json()["detail"] == guards.LEAVE_REQUEST_MISSING

    # Kayıt VARDI: aynı aktör aynı kaydı silebiliyor (silme yolu DEĞİŞMEDİ).
    sil = await client.delete(f"/leave-requests/{talep_id}", headers=admin_headers)
    assert sil.status_code == 204, sil.text


# --- 5. `pending` değilse → 409 (YENİ ve AYRI sabit) ----------------------


@pytest.mark.parametrize("durum", [LeaveStatus.approved, LeaveStatus.rejected])
async def test_karara_baglanmis_talep_geri_CEKILEMEZ_409(
    client, seeded_db, ik_calisan, yillik, durum: LeaveStatus
):
    """Karara bağlanmış talep geri çekilemez: onay/red kararının izi SİLİNEMEZ.

    Düzeltme yolu İK'nın kararı değiştirmesidir, sahibin kararı geri almasi değil.
    """
    _, headers, kayit = ik_calisan
    talep = await _talep(seeded_db, kayit, yillik, durum=durum)

    resp = await client.post(f"/leave-requests/{talep.id}/withdraw", headers=headers)
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"] == guards.LEAVE_WITHDRAW_NOT_PENDING

    await seeded_db.refresh(talep)
    assert talep.status is durum, "409'a rağmen durum DEĞİŞTİ"


async def test_ikinci_geri_cekme_409(client, seeded_db, ik_calisan, yillik):
    """İkinci geri çekme 409 — damga ÜZERİNE YAZILMAZ (idempotent 200 DEĞİL).

    200 dönmek `decided_at`i sessizce tazeler ve "ne zaman vazgeçti" sorusunun
    cevabını kaydırırdı.
    """
    _, headers, kayit = ik_calisan
    talep = await _talep(seeded_db, kayit, yillik)

    ilk = await client.post(f"/leave-requests/{talep.id}/withdraw", headers=headers)
    assert ilk.status_code == 200, ilk.text
    await seeded_db.refresh(talep)
    # Damga DB'den DB'ye karşılaştırılır: JSON serileştirmesinin saat dilimi
    # biçimi (`Z` / `+00:00`) testi ilgisiz bir sebeple kırmızıya düşürebilirdi.
    ilk_damga = talep.decided_at

    ikinci = await client.post(f"/leave-requests/{talep.id}/withdraw", headers=headers)
    assert ikinci.status_code == 409, ikinci.text
    assert ikinci.json()["detail"] == guards.LEAVE_WITHDRAW_NOT_PENDING

    await seeded_db.refresh(talep)
    assert talep.status is LeaveStatus.withdrawn
    assert talep.decided_at == ilk_damga, "ikinci istek karar damgasını TAZELEDİ"


def test_geri_cekme_409_metni_AYRI_bir_SABITTIR():
    """🔴 Yanlış sabite bağlanma korkuluğu.

    `LEAVE_NOT_PENDING` (düzenleme/silme) ve `LEAVE_DECISION_NOT_PENDING`
    (onay/red) metinleri kullanıcıya YANLIŞ eylemi anlatır: "onaylanabilir ya da
    reddedilebilir" cümlesi, talebini geri çekmeye çalışan çalışana onun
    yapamayacağı bir işi tarif eder. T2 hazır bir sabite bağlanırsa kod/metin
    testleri yine yeşil kalırdı — bu test onu yakalar.

    🔴 T3 MUTASYON BULGUSU (şef ölçümü): sabitin METNİ bu dosyada HİÇBİR YERDE
    çakılı değildi. `LEAVE_WITHDRAW_NOT_PENDING` gövdesi anlamsız bir dizeyle
    değiştirildiğinde 12 testin 12'si de YEŞİL kaldı (409 iddiaları üretim
    sabitini üretim sabitiyle karşılaştırıyordu) ve başka hiçbir katman metni
    korumuyordu. Beklenen metin bu yüzden — denetim satırı testindeki gibi —
    ELLE yazılır: kullanıcının GÖRDÜĞÜ cümle üretimden kopyalanarak değil,
    bağımsız olarak kilitlenir.
    """
    assert guards.LEAVE_WITHDRAW_NOT_PENDING != guards.LEAVE_NOT_PENDING
    assert guards.LEAVE_WITHDRAW_NOT_PENDING != guards.LEAVE_DECISION_NOT_PENDING
    assert guards.LEAVE_WITHDRAW_NOT_PENDING == "Yalnız bekleyen izin talebi geri çekilebilir"


# --- 6. Kimlik ------------------------------------------------------------


async def test_kimliksiz_istek_401(client, seeded_db, ik_calisan, yillik):
    """Kapı "herkese" değil "kimliği doğrulanmış herkese"dir."""
    _, _, kayit = ik_calisan
    talep = await _talep(seeded_db, kayit, yillik)

    resp = await client.post(f"/leave-requests/{talep.id}/withdraw")
    assert resp.status_code == 401, resp.text

    await seeded_db.refresh(talep)
    assert talep.status is LeaveStatus.pending


# --- 7. Denetim izi -------------------------------------------------------


async def test_denetim_satiri_dustu_ve_METNI_dogru(client, seeded_db, calisan, yillik):
    """Denetim satırı `update` eylemidir ve metni TALEBİN KİMLİĞİNİ taşır.

    Metin ELLE kurulur — `messages.leave_request_withdrawn` ÇAĞRILMAZ: üretim
    ifadesini teste kopyalamak, ifadenin kendisi yanlışsa testi de birlikte
    yanlışa taşır (sahte-yeşil).

    🔴 Yeni `AuditAction` üyesi AÇILMAZ (gerçek Postgres enum, migration isterdi);
    `update` kullanılır ve ayrım METİNDEDİR — `leave_request_self_created` kanonu.
    """
    _, headers, kayit = calisan
    talep = await _talep(seeded_db, kayit, yillik)
    onceki = set(await seeded_db.scalars(select(AuditLog.id)))

    resp = await client.post(f"/leave-requests/{talep.id}/withdraw", headers=headers)
    assert resp.status_code == 200, resp.text

    satirlar = await _yeni_denetim_satirlari(seeded_db, onceki)
    assert len(satirlar) == 1, [s.detail for s in satirlar]
    assert satirlar[0].action is AuditAction.update
    ilk, son = _baslangic(), _bitis()
    beklenen = f"İzin talebi geri çekildi: Mehmet Yılmaz · Yıllık İzin · {ilk} - {son}"
    assert satirlar[0].detail == beklenen


# --- 8. Bakiye DEĞİŞMEZ ---------------------------------------------------


async def test_bakiye_DEGISMEZ_pending_zaten_dusulmuyordu(
    client, seeded_db, ik_headers, ik_calisan, yillik
):
    """🔴 ÖLÇÜLDÜ: `used` YALNIZ `approved` + `deducts_from_annual` günleri toplar
    (`repository._deductible_approved_between`). Yani `pending` talep bakiyeden
    HİÇ düşülmüyordu ve geri çekmenin İADE EDECEĞİ bir şey YOK.

    Bu test o olguyu ÇİVİLER: T2'ye "geri çekmede bakiyeye dokun" refleksi
    gelirse (ör. `carried_over`a ekleme, `used`tan düşme) burası kırmızı olur —
    kullanıcının hakkı SESSİZCE BÜYÜRDÜ.
    """
    _, headers, kayit = ik_calisan
    talep = await _talep(seeded_db, kayit, yillik)
    yol = f"/leave-balances/{kayit.id}/{_yil()}"

    once = await client.get(yol, headers=ik_headers)
    assert once.status_code == 200, once.text

    resp = await client.post(f"/leave-requests/{talep.id}/withdraw", headers=headers)
    assert resp.status_code == 200, resp.text

    sonra = await client.get(yol, headers=ik_headers)
    assert sonra.status_code == 200, sonra.text
    assert sonra.json() == once.json(), "geri çekme bakiyeyi DEĞİŞTİRDİ"
    # Ve taban gerçekten ölçülebilir bir değerdi (boş/None bir karşılaştırma değil).
    assert once.json()["used"] == 0
    assert once.json()["annual_entitlement"] == 14


# --- 9. Bekleyen kuyruğundan DÜŞER, `withdrawn` süzgeciyle GÖRÜNÜR --------


async def test_geri_cekilen_talep_bekleyen_kuyrugundan_DUSER(
    client, seeded_db, ik_headers, ik_calisan, yillik
):
    """İZ 46 KPI (`count_pending_leave_requests`) `status == pending` sayar.

    Geri çekilen talep kuyruktan düşmeliydi — düşmezse İK'nın "Bekleyen" sayacı
    kalıcı olarak kirlenir. Aynı testte YENİ enum üyesinin SÜZGEÇ olarak da
    çalıştığı çakılır (`?status=withdrawn`): üye yalnız kolona yazılıp
    `LeaveStatus` süzgecine girmezse ekran onu bir daha bulamaz.
    """
    _, headers, kayit = ik_calisan
    geri_cekilecek = await _talep(seeded_db, kayit, yillik)
    await _talep(seeded_db, kayit, yillik, baslangic=date(_yil(), 11, 2), bitis=date(_yil(), 11, 4))

    once = await client.get("/leave-requests?status=pending", headers=ik_headers)
    assert once.status_code == 200, once.text
    assert once.json()["total"] == 2

    resp = await client.post(f"/leave-requests/{geri_cekilecek.id}/withdraw", headers=headers)
    assert resp.status_code == 200, resp.text

    sonra = await client.get("/leave-requests?status=pending", headers=ik_headers)
    assert sonra.status_code == 200, sonra.text
    assert sonra.json()["total"] == 1
    assert str(geri_cekilecek.id) not in {s["id"] for s in sonra.json()["items"]}

    cekilenler = await client.get("/leave-requests?status=withdrawn", headers=ik_headers)
    assert cekilenler.status_code == 200, cekilenler.text
    assert [s["id"] for s in cekilenler.json()["items"]] == [str(geri_cekilecek.id)]
