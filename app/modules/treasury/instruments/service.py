"""FIN-1 is kurallari — liste · olustur · detay · PATCH · gecis · silme · ozet.

## 🔴 Bu dosyanin YAZMADIGI uc sey

1. **Gecis kurallari.** Tablo `transitions.py`dedir; burada `if status == ...`
   YOKTUR. Iki yere yazilsaydi biri terminal korumasini, oteki yon uyumunu
   unuturdu.
2. **Turevler.** `is_due` ve KPI pencereleri `derive.py`dedir.
3. **Yetki.** Uc kapi (`view`/`full`/`admin`) router'dadir (`require_permission`).

## 🔴 ODM-1 — K5 GECERSIZ: GECIS ARTIK FIS ATAR (2026-08-27)

FIN-1'in K5 karari *"cek tahsil edilince yevmiye fisi atmak AYRI bir dilimin
isidir"* diyordu ve o dilim ODM-1'dir. `change_status` artik iki sinif is yapar
(`_post_transition`): `collected`/`paid` yeni bir fis YAZAR (`101`/`103`
kapanir), `returned`/`cancelled` ise bagli odemelerin fisini STORNO eder.

Modul yine de `accounting/` altinda tek satir degistirmez: fisleme
`posting.post_document` ve `accounting.state_service`in KENDI kapilarindan
gecer. Portfoy artik yalnizca bir ENVANTER degil, defterin `101`/`103` ara
hesaplarinin KARSILIGIDIR.

## Hangi kural hangi koda duser

| Durum | Kod | Sinif |
|---|---|---|
| Var olmayan **ya da gorunmeyen** kayit | 404 | `NotFoundError` |
| Govde ici referans (proje / banka hesabi) gorunmuyor | 404 | `NotFoundError` |
| Bicim ihlali (uzunluk, olcek, `limit` tavani, bilinmeyen alan, `status`) | 422 | Pydantic |
| `due_date < issue_date` (BIRLESIK degerler uzerinde) | 422 | `TreasuryValidationError` |
| Zorunlu alana acikca `null` | 422 | `TreasuryValidationError` |
| Gecersiz/terminal/yon-aykiri gecis | 409 | `ConflictError` |
| Portfoy disi kaydin silinmesi | 409 | `ConflictError` |

Govde ici referansin **403 DEGIL 404** olmasi bilinclidir (ST kanonu): 403 "bu
kayit VAR ama goremezsin" bilgisini sizdirirdi.

## 🔴 EŞİK = KİLİT (Ik-2/Ik-3 kanonu)

Durum gecisi bir DURUM DENETIMIDIR: "kaynak terminal mi", "yon uyuyor mu"
sorulari okunan satira baglidir. Kilitsiz akista iki eszamanli istek AYNI
`portfolio` degerini okur ve **IKISI DE** kapidan gecer — ikincisi birincinin
yazdigini ezer ve `collected` bir cek sessizce `cancelled` olur. Bu yuzden
`change_status` satiri **DENETIMLERDEN ONCE** `with_for_update` ile kilitler.
Ayni sira `update_instrument` ve `delete_instrument`ta da gecerlidir; kilit
sirasi tum uclarda SABITTIR (yalniz `financial_instruments` satiri) —
ikinci bir tablo kilitlenmedigi icin deadlock yolu yoktur.
"""

import uuid
from collections.abc import Sequence
from datetime import date

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import (
    ConflictError,
    NotFoundError,
    RelatedRecordsExistError,
    TreasuryValidationError,
)
from app.modules.audit import messages
from app.modules.projects.service import sirket_liste_ciftleri, visible_projects
from app.modules.treasury import posting as treasury_posting
from app.modules.treasury.instruments import (
    derive,
    guards,
    posting,
    repository,
    summary,
    transitions,
)
from app.modules.treasury.instruments.schemas import (
    FinancialInstrumentCreate,
    FinancialInstrumentListResponse,
    FinancialInstrumentResponse,
    FinancialInstrumentSummaryResponse,
    FinancialInstrumentUpdate,
)
from app.modules.treasury.models import (
    BankAccount,
    FinancialInstrument,
    FinancialInstrumentDirection,
    FinancialInstrumentKind,
    FinancialInstrumentStatus,
)
from app.modules.users.models import User

__all__ = [
    "PERMISSION_MODULE",
    "build_summary",
    "change_status",
    "create_instrument",
    "delete_instrument",
    "list_instruments",
    "response_of",
    "update_instrument",
    "visible_instrument",
]

PERMISSION_MODULE = "treasury"
"""Izin anahtari — 🔴 **YENI IZIN MODULU ACILMADI** (K9).

`treasury` seed'de ZATEN vardir ("Hazine", grup MALI, `roles/seed_data.py:103`)
ve matris satiri `[_A, _F, _N, _N, _N, _F, _V, _N]`dir. Cek/senet HAZINENIN
varligidir; ayri bir modul acilsaydi izin migration'i dogar ve migration slotu
bu dilimde zaten doludur.

Kapilar:
* okuma (`view`)  → PM · muhasebe · patron · sysadmin
* yazma (`full`)  → muhasebe · patron · sysadmin (**PM yazamaz**)
* silme (`admin`) → YALNIZ sysadmin — `full` silmeyi KAPSAMAZ (repo kanonu)
"""

#: PATCH'te acikca `null` gonderilse bile KORUNAN alanlar: kolonlari NOT NULL'dir
#: ve sema hepsini `| None` yazar (govde kismidir). `invoicing._NOT_NULL_FIELDS`
#: deseninin aynisi.
_NOT_NULL_FIELDS = (
    "instrument_kind",
    "direction",
    "serial_no",
    "drawer_name",
    "issue_date",
    "due_date",
    "amount",
)


async def _visible_project_ids(
    session: AsyncSession, actor: User, *, liste: bool = False
) -> list[uuid.UUID]:
    """`liste=True`: şirket türü liste/özet ucu (IZN-B5f 23b) — satırlar kişinin O PROJEDEKİ
    rolünün `çek/senet` sayfa izniyle süzülür (detay ucuyla aynı kural)."""
    ciftler = sirket_liste_ciftleri(PERMISSION_MODULE) if liste else ()
    return [p.id for p in await visible_projects(session, actor, sirket_ciftleri=ciftler)]


def _assert_date_order(issue_date: date, due_date: date) -> None:
    """🔴 Kural BIRLESIK degerler uzerinde kosar (`_assert_cash_has_name` deseni).

    PATCH kismi govde gonderir: kullanici yalniz `due_date` yollasa bile
    kayittaki `issue_date` ile karsilastirilmalidir. Semaya yazilsaydi ayni kural
    PATCH yolunda IKINCI kez ve farkli bicimde yazilirdi.

    Ayni gun MESRUDUR (`>=`): goruldugunde odenen cek.
    """
    if due_date < issue_date:
        raise TreasuryValidationError(guards.DUE_BEFORE_ISSUE)


async def _assert_references(
    session: AsyncSession,
    actor: User,
    project_id: uuid.UUID | None,
    bank_account_id: uuid.UUID | None,
) -> None:
    """Govdedeki IKI varlik referansi — ikisi de **404** (ST kanonu).

    * `project_id` GORUNUR kumede olmali: gorunmeyen bir projeye cek baglamak,
      o projenin varligini dogrulayan bir yan kanal acardi.
    * `bank_account_id` yalnizca VAR OLMALI: banka hesabi SIRKET GENELIDIR
      (HZ-1 K3, tabloda proje FK'si yoktur) ve kapsam suzgeci YOKTUR — burada
      bir kapsam denetimi yazmak OLMAYAN bir suzgeci varmis gibi gosterirdi.
    """
    if project_id is not None and project_id not in await _visible_project_ids(session, actor):
        raise NotFoundError(guards.PROJECT_INVALID)
    if bank_account_id is not None and await session.get(BankAccount, bank_account_id) is None:
        raise NotFoundError(guards.BANK_ACCOUNT_INVALID)


async def visible_instrument(
    session: AsyncSession, actor: User, instrument_id: uuid.UUID, *, for_update: bool = False
) -> FinancialInstrument:
    """Tekil erisimin TEK kapisi — okuma da yazma da buradan gecer.

    `for_update=True` satiri DENETIMLERDEN ONCE kilitler (EŞİK=KİLİT): kapsam
    denetimi kilitli satir uzerinde kosar, boylece kilit ile karar arasina baska
    bir islem giremez.

    Projesi gorunur kumede degilse **404** doner ve govde var OLMAYAN kimliginki
    ile BIREBIR AYNIDIR. `project_id` NULL kayit (sirket geneli cek) modul
    izniyle GORUNUR — `repository.scope_clause`in ucuncu hali.
    """
    instrument = await repository.get_instrument(session, instrument_id, for_update=for_update)
    if instrument is None:
        raise NotFoundError(guards.INSTRUMENT_MISSING)
    if instrument.project_id is not None:
        if instrument.project_id not in await _visible_project_ids(session, actor):
            raise NotFoundError(guards.INSTRUMENT_MISSING)
    return instrument


def response_of(instrument: FinancialInstrument, *, as_of: date) -> FinancialInstrumentResponse:
    """Yanit kurulumunun TEK yeri — dort uc de buradan gecer."""
    return FinancialInstrumentResponse.from_row(instrument, as_of=as_of)


# --- Uc 1: liste ---


async def list_instruments(
    session: AsyncSession,
    actor: User,
    *,
    direction: FinancialInstrumentDirection | None,
    instrument_kind: FinancialInstrumentKind | None,
    status: FinancialInstrumentStatus | None,
    project_id: uuid.UUID | None,
    due_before: date | None,
    due_after: date | None,
    q: str | None,
    limit: int,
    offset: int,
) -> FinancialInstrumentListResponse:
    """E10 tablosunun veri kaynagi. Kapsam suzgeci `total`a DA uygulanir:
    gorunmeyen projenin ceki "sayfa disinda kalmis" gibi bile gorunmez."""
    suzgecler = {
        "direction": direction,
        "instrument_kind": instrument_kind,
        "status": status,
        "project_id": project_id,
        "due_before": due_before,
        "due_after": due_after,
        "q": q,
    }
    project_ids = await _visible_project_ids(session, actor, liste=True)
    kayitlar = await repository.list_instruments(
        session, project_ids, limit=limit, offset=offset, **suzgecler
    )
    total = await repository.count_instruments(session, project_ids, **suzgecler)
    as_of = derive.as_of_today()
    return FinancialInstrumentListResponse(
        items=[response_of(kayit, as_of=as_of) for kayit in kayitlar],
        total=total,
        limit=limit,
        offset=offset,
        as_of=as_of,
    )


# --- Uc 2: olustur ---


async def create_instrument(
    session: AsyncSession, actor: User, data: FinancialInstrumentCreate
) -> tuple[FinancialInstrument, str]:
    """Dogrulamalarin HEPSI yazimdan ONCEDIR (`create_invoice` deseni).

    🔴 Yeni kayit HER ZAMAN `portfolio` dogar: `status` govdede yoktur (K7) ve
    varsayilan modeldedir. Gecmise donuk bir kayit icin yol POST + gecis ucudur,
    boylece her durum degisimi K2 tablosundan ve denetim gunlugunden gecer.
    """
    _assert_date_order(data.issue_date, data.due_date)
    await _assert_references(session, actor, data.project_id, data.bank_account_id)

    instrument = FinancialInstrument(
        instrument_kind=data.instrument_kind,
        direction=data.direction,
        serial_no=data.serial_no,
        drawer_name=data.drawer_name,
        description=data.description,
        bank_name=data.bank_name,
        issue_date=data.issue_date,
        due_date=data.due_date,
        amount=data.amount,
        status=FinancialInstrumentStatus.portfolio,
        project_id=data.project_id,
        bank_account_id=data.bank_account_id,
    )
    session.add(instrument)
    await session.flush()
    await session.refresh(instrument)
    return instrument, messages.financial_instrument_created(
        instrument.serial_no, instrument.drawer_name
    )


# --- Uc 4: PATCH ---


async def update_instrument(
    session: AsyncSession, actor: User, instrument_id: uuid.UUID, data: FinancialInstrumentUpdate
) -> tuple[FinancialInstrument, str]:
    """Kismi guncelleme — kurallar BIRLESIK degerler uzerinde kosar.

    Kayit DENETIMLERDEN ONCE kilitlenir (TOCTOU).

    `exclude_unset` SARTTIR: gonderilmeyen alan ile acikca `null` gonderilen alan
    AYNI SEY DEGILDIR. Onsuz, yalniz `bank_name` duzelten bir istek kaydin
    aciklamasini ve proje bagini SESSIZCE silerdi.

    🔴 **TERMINAL KAYITTA YON/TUR DEGISTIRILEMEZ** (409). Emirde yoktu, T4'te
    eklendi ve gerekcesi K7'nin ta kendisidir: PATCH yonu degistirebilseydi
    `collected` bir "alinan" cek "verilen"e cevrilir ve K2'nin ASLA
    uretemeyecegi `(issued, collected)` cifti PATCH uzerinden dogardi —
    invaryantin IKINCI yazma kapisi (BOQ-SEC-B kanonu). Portfoydeyken serbesttir:
    orada hicbir yon-durum ciftini bozmaz.

    `updated_at` sunucu damgasidir ve UPDATE'ten sonra ORM'deki deger BAYATTIR;
    async baglamda tembel yukleme `MissingGreenlet` = **500** demektir (P11
    dersi). Acik `refresh` bu pencereyi kapatir.
    """
    instrument = await visible_instrument(session, actor, instrument_id, for_update=True)
    verilen = data.model_dump(exclude_unset=True)

    for alan in _NOT_NULL_FIELDS:
        if alan in verilen and verilen[alan] is None:
            raise TreasuryValidationError(f"{guards.REQUIRED_FIELD_CLEARED} ({alan})")

    yon_degisiyor = any(
        alan in verilen and verilen[alan] is not None and verilen[alan] != getattr(instrument, alan)
        for alan in ("direction", "instrument_kind")
    )
    if yon_degisiyor and instrument.status in transitions.TERMINAL_STATUSES:
        raise ConflictError(guards.TERMINAL_STATUS_DIRECTION)
    # 🔴 IKINCI YON KAPISI — DURUM DEGIL **BAG**. Portfoydeki bir evrakta yon
    # serbesttir ve oyle KALIR (yukaridaki kapinin gerekcesi), ama BAGLI ODEMESI
    # varsa serbest DEGILDIR: bagin yon uyumu odeme yazilirken bir kez dogrulanir
    # (`payments_service._UYUMLU_YON`, 422) ve PATCH onu GERIYE DONUK
    # gecersizlestirirdi. Odeme fisinin nakit bacagi FATURANIN akisindan,
    # tahsil/odeme fisininki ENSTRUMANIN `direction`indan secilir; ikisi ayrisinca
    # `101` hic kapanmaz ve terminal durumdan cikis olmadigi icin duzeltecek
    # ikinci gecis DOGAMAZ. Sayim KILITLI satir uzerinde kosar (ESIK = KILIT):
    # kilitsiz olsaydi bir PATCH ile bir odeme yazimi birbirini kacirirdi.
    if yon_degisiyor and await repository.count_payments_for_instrument(session, instrument.id):
        raise ConflictError(guards.DIRECTION_LOCKED_BY_PAYMENTS)

    issue_date = verilen.get("issue_date") or instrument.issue_date
    due_date = verilen.get("due_date") or instrument.due_date
    _assert_date_order(issue_date, due_date)

    project_id = verilen["project_id"] if "project_id" in verilen else instrument.project_id
    bank_account_id = (
        verilen["bank_account_id"] if "bank_account_id" in verilen else instrument.bank_account_id
    )
    # 🔴 Referanslar YALNIZ govdede GELDIYSE dogrulanir degil, BIRLESIK degerler
    # uzerinde dogrulanir: kaydin eski projesi bu arada gorunmez olmus olabilir
    # ve o hâlde kullanici baska bir alani duzeltemez hâle gelirdi. Bu yuzden
    # denetim yalniz DEGISEN referansa uygulanir.
    if "project_id" in verilen:
        await _assert_references(session, actor, project_id, None)
    if "bank_account_id" in verilen:
        await _assert_references(session, actor, None, bank_account_id)

    for alan, deger in verilen.items():
        if alan in _NOT_NULL_FIELDS and deger is None:  # pragma: no cover - yukarida elendi
            continue
        setattr(instrument, alan, deger)

    await session.flush()
    await session.refresh(instrument)
    return instrument, messages.financial_instrument_updated(
        instrument.serial_no, instrument.drawer_name
    )


# --- Uc 5: durum gecisi ---


async def _post_transition(
    session: AsyncSession,
    actor: User,
    instrument: FinancialInstrument,
    target: FinancialInstrumentStatus,
) -> None:
    """🔴 ODM-1 — geçişin MALİ karşılığı. İKİ sınıf, İKİ ayrı yol.

    * `collected`/`paid` → **YENİ FİŞ**: `101`/`103` kapanır, para nakde iner.
      Tutar Σ BAĞLI ÖDEMELERDİR (D3, gerekçe `instruments/posting.py`de) ve
      bağlı ödeme yoksa HİÇBİR ŞEY yazılmaz.
    * `returned`/`cancelled` → **STORNO** (D6): karşılıksız/iptal bir çekte
      cari KAPANMAMIŞ olmalıdır. Bağlı HER ödemenin fişi
      `treasury.posting.reverse_payment` ile tersine çevrilir (`120 B / 101 A`),
      alacak yeniden AÇILIR ve `101` boşalır. 🔴 Fonksiyon **ÇAĞRILIR,
      KOPYALANMAZ** (K3): ikinci bir storno yazımı bir gün `state_service`
      yerine fiş silmeye kayar ve mali iz sessizce kaybolurdu.
      🔴 Ödeme SATIRI SİLİNMEZ — mali iz kalır; o parayı nakitten dışlayan şey
      `balance.cash_realized_condition()`ın süzgecidir (D2).

    Kilit sırası BOZULMAZ: satır kilidi çağıranın İLK işidir ve buraya yalnız
    KİLİTLİ satırla gelinir; `post_document` kendi danışma kilidini EN SONDA
    alır, yani satır kilidi sırasına yeni bir halka SOKMAZ.

    🔴 Fiş AYNI transaction'dadır: kapalı dönem (**409**) ya da eksik eşleme
    (**422**) durum damgasını da geri alır — "tahsil edilmiş ama fişsiz" bir
    evrak DOĞMAZ.
    """
    if target not in posting.POSTING_STATUSES and target not in posting.REVERSING_STATUSES:
        return
    odemeler = await repository.payments_with_accounts(session, instrument.id)
    if target in posting.POSTING_STATUSES:
        await posting.post_instrument(session, actor, instrument, odemeler)
        return
    for payment, _account in odemeler:
        await treasury_posting.reverse_payment(session, actor, payment.id)


async def change_status(
    session: AsyncSession,
    actor: User,
    instrument_id: uuid.UUID,
    target: FinancialInstrumentStatus,
) -> tuple[FinancialInstrument, str]:
    """🔴 K7 — durumun TEK yazma kapisi. Sira: KILIT → kapsam → tablo → yazma.

    Kilit her seyden ONCE alinir (EŞİK=KİLİT, madde (a) TOCTOU): tablo kontrolu
    kilitli satir uzerinde kosar. Kilitsiz olsaydi iki eszamanlı istek AYNI
    `portfolio` degerini okur, IKISI DE tablodan gecer ve ikincisi birincinin
    yazdigini ezerdi — `collected` bir cek sessizce `cancelled` olurdu.
    """
    instrument = await visible_instrument(session, actor, instrument_id, for_update=True)
    transitions.assert_transition(instrument.direction, instrument.status, target)
    instrument.status = target
    await session.flush()
    await session.refresh(instrument)
    await _post_transition(session, actor, instrument, target)
    return instrument, messages.financial_instrument_status_changed(
        instrument.serial_no, instrument.drawer_name, target.value
    )


# --- Uc 6: DELETE ---


async def delete_instrument(session: AsyncSession, actor: User, instrument_id: uuid.UUID) -> str:
    """YALNIZ `portfolio` iken ve BAGLI ODEMESI YOKKEN silinir; ikisi de **409**.

    🔴 Silme kapisi da satiri ONCE kilitler: kilitsiz olsaydi bir gecis ile bir
    silme yarisir ve tahsil edilmis bir cek silinebilirdi (mali izin kaybi).

    🔴 **BAGLI ODEME SAYIMI** (`delete_account`in BIREBIR aynasi). Durum kapisi
    TEK BASINA yetmez: `payments.financial_instrument_id` FK'si
    **`ON DELETE SET NULL`**dur, yani DB silmeyi ENGELLEMEZ — bagi sessizce
    KOPARIR. Bagsiz odemeyi `balance.cash_realized_condition` DAIMA nakit saydigi
    icin, portfoydeki bir cek silindiginde banka bakiyesi HENUZ TAHSIL EDILMEMIS
    tutar kadar ANINDA siser; ustelik `101 Alinan Cekler`i bosaltacak tek olay
    (`collected` gecisi) artik dogamaz cunku evrak satiri YOKTUR. Her fis tek
    basina dengeli oldugu icin mizan dogru gorunur ve kusur hicbir kolon
    farkiyla ele vermez. Sayim KILITLI satir uzerinde kosar (ESIK = KILIT).

    Denetim metni silmeden ONCE kurulur; sonra kurulsaydi numara/keside
    guvenilir okunamaz ve silinenin NE OLDUGU kaybolurdu.
    """
    instrument = await visible_instrument(session, actor, instrument_id, for_update=True)
    if instrument.status is not FinancialInstrumentStatus.portfolio:
        raise ConflictError(guards.TERMINAL_STATUS_DELETE)
    if await repository.count_payments_for_instrument(session, instrument.id):
        raise RelatedRecordsExistError(guards.INSTRUMENT_HAS_PAYMENTS)
    detail = messages.financial_instrument_deleted(instrument.serial_no, instrument.drawer_name)
    await session.delete(instrument)
    await session.flush()
    return detail


# --- Uc 7: ozet ---


async def build_summary(session: AsyncSession, actor: User) -> FinancialInstrumentSummaryResponse:
    """E10:69-90'in dort karti — kapsam suzgeci `summary.py`nin `WHERE`indedir."""
    project_ids: Sequence[uuid.UUID] = await _visible_project_ids(session, actor, liste=True)
    return await summary.build_summary(session, project_ids, as_of=derive.as_of_today())
