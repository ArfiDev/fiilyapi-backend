"""Hakediş satırlarının TEK yazma yolu — task H5 (spec §6.5, §9.2, §5).

## ⚠️ Semantik: DEĞİŞTİRME (replace), P5'in TERSİ

`PUT /progress-payments/{id}/lines` gövdesi ekranın TAMAMIDIR: gövdede geçmeyen
satır **SİLİNİR**. Bu, `contracts/distribution.py`'nin `PUT …/contract/distribution`
**BİRLEŞTİRME** semantiğinin TERSİDİR — orada gövdede geçmeyen hücre KORUNUR ve
silmek için açıkça `quantity: null` gerekir. İki uç frontend'de yan yana
kullanılacağı için karıştırılması en tehlikeli kontrat hatasıdır (spec §10/2);
`tests/progress_payments/test_lines.py::test_degistirme_semantigi_govdede_olmayan_satir_silinir`
farkı doğrudan doğrular.

## Tek yol

Satır üreten HER yol buradan geçer: `PUT …/lines` *ve* `POST /projects/{id}/progress-payments`
gövdesindeki iç içe `lines[]`. Korkulukları yalnız `PUT`'a koymak, aynı geçersiz
veriyi `POST` üzerinden yazmaya izin veren bir arka kapı bırakırdı — spec §6.5
başlığı "her durumda koşar" der.

## Sıra — ÖNCE TÜM DOĞRULAMALAR, SONRA TEK YAZMA (P5 C8 dersi)

Doğrulama yazmanın arasına serpiştirilirse, ikinci satırda patlayan bir istek
birincisini çoktan session'a eklemiş olur; `get_db` rollback'i yalnız DIŞ
katmandır. İç katman — hiç yazmamak — `_resolve` ile `_apply`'ın AYRIK olmasıyla
kurulur: `_resolve` hiçbir şey yazmaz, `_apply` hiçbir şey doğrulamaz.
"""

import uuid
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import DuplicateError, SiteValidationError
from app.modules.contracts.models import EmployerContractItem
from app.modules.progress_payments import calculations, guards, repository
from app.modules.progress_payments.models import ProgressPayment, ProgressPaymentLine
from app.modules.progress_payments.schemas import ProgressPaymentLineInput
from app.modules.projects.models import Project, ProjectContract
from app.modules.site_diary import bridge
from app.modules.subcontractor_progress_payments.models import QuantitySource

_ZERO = Decimal("0")

# (contract_item_id, site_id) — hakediş satırının hücre kimliği; kısmi benzersiz
# indeks `uq_progress_payment_lines_item_site` ile aynı üçlünün son iki alanı.
LineKey = tuple[uuid.UUID, uuid.UUID]


@dataclass(frozen=True)
class _ResolvedLine:
    """Doğrulaması BİTMİŞ satır planı — henüz hiçbir şey yazılmadı."""

    key: LineKey
    item: EmployerContractItem
    group_name: str | None
    quantity: Decimal
    coefficient: Decimal
    # SD-2 damgası (TB4 B1): SUNUCUDA türetilir, gövdeden ALINMAZ.
    quantity_source: QuantitySource


async def completed_totals(
    session: AsyncSession,
    project_id: uuid.UUID,
    *,
    before_sequence_no: int | None = None,
    exclude_payment_id: uuid.UUID | None = None,
) -> dict[LineKey, tuple[Decimal, Decimal]]:
    """(kalem, şantiye) → (miktar, tutar) toplamı; `status ∈ {approved, paid}`.

    ## TEK fonksiyon, İKİ mod — hangisi nerede (H6 denetimi K1, 2026-07-31)

    * **`before_sequence_no=N` — sıra tabanlı.** Spec §6.6'nın `prev = sequence_no
      daha küçük VE approved|paid` tanımı. E15'in "Önceki"/"Toplam" KOLONLARI
      (`service._line_rows`) ve §6.3 avans mahsubu zinciri bu modu kullanır: ikisi
      de tarihsel bir SIRALAMANIN ifadesidir ("bu evraktan önce ne olmuştu").
    * **`exclude_payment_id=X` — sırasız TAM küme (kendisi hariç).** §6.5/2 kota
      TAVANI bu modu kullanır: `satır-yazma` (`_resolve`) ve `onay yeniden
      doğrulaması` (`transitions._revalidate_quota`).

    Kota neden sıraya bağlanamaz (kapatılan KRİTİK açık): kota bir TOPLAM
    kısıtıdır, kronolojik değildir. Sıra tabanlı okunduğunda büyük sıra numaralı
    hakediş ÖNCE onaylanırsa, küçük numaralı olan onu "önceki" saymaz — onay
    sırasını değiştirmek tavanı aşmanın meşru uçlarla işleyen bir yolu olurdu
    (seq1=600 onayla → seq2=400 → seq1'i geri çek/reddet → 1.000'e yükselt →
    ikisini de onayla = 1.400 > 1.000). Kota artık `sequence_no`'dan BAĞIMSIZDIR;
    §6.6 gösterim kolonları ise sıra tabanlı KALIR (kullanıcıya gösterilen
    "Önceki" evrakın tarihsel yerini anlatır).

    P5'in "iki farklı doğruluk tanımı" bulgusuna karşı korunan şey burada da
    korunur: iki mod da BU TEK gövdeden toplar, ikinci bir toplama kopyası AÇILMAZ.
    """
    payments = await repository.list_completed_payments(
        session,
        project_id,
        before_sequence_no=before_sequence_no,
        exclude_payment_id=exclude_payment_id,
    )
    return totals_from_payments(payments)


def totals_from_payments(
    payments: list[ProgressPayment],
) -> dict[LineKey, tuple[Decimal, Decimal]]:
    """`completed_totals`'ın SORGUSUZ gövdesi — hakedişler ÇAĞIRAN tarafından
    (toplu çekimle) çözülmüşse toplama burada yapılır (H4 denetimi O1).

    Toplama kuralının TEK kopyası budur: `completed_totals` da bu fonksiyonu
    çağırır, `service._line_rows` de. İkinci bir toplama kopyası AÇILMAZ (P5'in
    "iki farklı doğruluk tanımı" bulgusu).
    """
    totals: dict[LineKey, tuple[Decimal, Decimal]] = {}
    for prior in payments:
        for line in prior.lines:
            if line.contract_item_id is None:
                # Kalemi silinmiş satır (`SET NULL`) hücre kimliğini KAYBETMİŞTİR:
                # hangi (kalem, şantiye) çiftine yazılacağı bilinemez. Kümülatif
                # toplamdan kalıcı olarak düşer — ONAYLI SAPMA, spec §6.5 notu
                # (H6 denetimi D3); `transitions._revalidate_quota` aynı satırı
                # aynı gerekçeyle atlar.
                continue
            key = (line.contract_item_id, line.site_id)
            prev_qty, prev_amt = totals.get(key, (_ZERO, _ZERO))
            line_amt = calculations.line_total(
                line.contract_unit_price, line.coefficient, line.quantity
            )
            totals[key] = (prev_qty + line.quantity, prev_amt + line_amt)
    return totals


def _stamp(
    diary_totals: dict[bridge.EmployerCellKey, Decimal], key: LineKey, quantity: Decimal
) -> QuantitySource:
    """Damgaya ÇEVİRME kuralının TEK kopyası (`bridge.is_diary_quantity` →
    enum). İki çağıranı vardır — satır yazma yolu (`_resolve`) ve dönem
    değişince yeniden damgalama (`restamp_for_period`) — ve ikisi de buradan
    okur: ikinci bir kopya, bir yolda `diary`, diğerinde `manual` üreten sessiz
    bir ayrışma demek olurdu.
    """
    return (
        QuantitySource.diary
        if bridge.is_diary_quantity(diary_totals.get(key), quantity)
        else QuantitySource.manual
    )


async def restamp_for_period(
    session: AsyncSession, payment: ProgressPayment, project_id: uuid.UUID
) -> None:
    """Hakedişin DÖNEMİ değiştiğinde (`PATCH …/{id}`) MEVCUT satırların damgasını
    yeniden türetir — T5 bulgusu.

    Damganın süzgeci hakedişin kendi dönemidir: satırlara hiç dokunmayan bir
    `PATCH` dönemi Haziran'dan Temmuz'a taşıdığında, Haziran günlüğüyle birebir
    eşleştiği için `diary` damgalanmış satır Temmuz günlüğüyle hiç ilgisi
    olmadan rozetli kalırdı — yani "günlükten geldi" iddiası gövdeden tek bir
    alan sızdırmadan SAHTELEŞİRDİ.

    Miktarlar DEĞİŞMEZ; yalnız iddia yeniden sınanır. Kural `_resolve` ile aynı
    tek kaynaktan (`site_diary.bridge` + `_stamp`) okunur, ikinci bir toplama
    ya da eşleşme mantığı açılmaz.
    """
    diary_totals = await bridge.employer_period_totals(
        session, project_id, year=payment.period_year, month=payment.period_month
    )
    for line in payment.lines:
        if line.contract_item_id is None:
            # Bağı kopmuş satırın hücre kimliği (kalem, şantiye) YOKTUR: hangi
            # günlük toplamıyla kıyaslanacağı bilinemez → iddia düşer.
            line.quantity_source = QuantitySource.manual
            continue
        line.quantity_source = _stamp(
            diary_totals, (line.contract_item_id, line.site_id), line.quantity
        )


async def _resolve(
    session: AsyncSession,
    project: Project,
    contract: ProjectContract,
    inputs: list[ProgressPaymentLineInput],
    *,
    default_coefficient: Decimal,
    exclude_payment_id: uuid.UUID | None,
    existing: dict[LineKey, ProgressPaymentLine],
    period: tuple[int | None, int | None],
) -> list[_ResolvedLine]:
    """Spec §6.5'in DÖRT kuralı + FF kilidi (§10/5). **Hiçbir yazma YAPMAZ.**

    Sıra plandaki sırayla birebir: gövde-içi çift → şantiye-proje → kalem-sözleşme
    → dağıtım ön şartı → kota tavanı → FF kilidi. Sorgular satır başına DEĞİL,
    gövdenin tamamı için toplu koşar (N+1 yok).

    `existing` = bu hakedişte HÂLİHAZIRDA duran hücreler (yeni hakedişte boş).
    İki kural buradan okur: katsayı öntanımı (§4.1) ve kota kontrolünün ARTIŞ
    koşulu (§6.5/2) — ikisi de "satırın mevcut hâli" bilgisine muhtaçtır.

    `exclude_payment_id` = düzenlenen hakediş (oluşturmada `None`, kayıt henüz
    yoktur). Kota TAM kümeden okunur (`completed_totals`'ın sırasız modu, H6
    denetimi K1): yazma anında da sıra tabanlı okunsaydı, büyük sıralı bir
    hakediş onaylıyken küçük sıralı taslak onu görmez ve tavanı aşan miktar
    SESSİZCE yazılabilirdi (zincirin sızdıran adımı buydu).

    `period` = hakedişin KENDİ dönemi (`period_year`, `period_month`) — SD-2
    damgasının (TB4 B1) süzgeci. Damga burada türetilir çünkü `_apply` hiçbir şey
    SORGULAMAZ; günlük toplamı satır başına değil, gövdenin tamamı için TEK kez
    okunur (N+1 yok).
    """
    item_ids = [entry.contract_item_id for entry in inputs]
    site_ids = [entry.site_id for entry in inputs]
    items_with_group = await repository.get_employer_items_with_group_by_ids(session, item_ids)
    sites = await repository.get_sites_by_ids(session, site_ids)
    quotas = await repository.get_distributed_quotas(session, item_ids, site_ids)
    prior_totals = await completed_totals(
        session, project.id, exclude_payment_id=exclude_payment_id
    )
    period_year, period_month = period
    diary_totals = await bridge.employer_period_totals(
        session, project.id, year=period_year, month=period_month
    )

    seen: set[LineKey] = set()
    resolved: list[_ResolvedLine] = []
    for entry in inputs:
        key: LineKey = (entry.contract_item_id, entry.site_id)
        if key in seen:
            raise DuplicateError(guards.DUPLICATE_CELL)
        seen.add(key)

        site = sites.get(entry.site_id)
        if site is None or site.project_id != project.id:
            raise SiteValidationError(guards.SITE_PROJECT_MISMATCH)

        item_and_group = items_with_group.get(entry.contract_item_id)
        if item_and_group is None or item_and_group[0].project_id != project.id:
            raise SiteValidationError(guards.ITEM_PROJECT_MISMATCH)
        item, group_name = item_and_group

        quota = quotas.get(key)
        if quota is None:
            raise SiteValidationError(guards.ITEM_NOT_DISTRIBUTED)

        existing_line = existing.get(key)
        current_quantity = _ZERO if existing_line is None else existing_line.quantity

        # Kümülatif = TÜM tamamlanmış hakedişler + bu satır. Bu hakedişin
        # KENDİ eski miktarı toplama GİRMEZ: değiştirme semantiğinde eski satır
        # yerini yenisine bırakır, iki kez sayılırsa aynı taslağı yeniden
        # kaydetmek aşım verirdi.
        #
        # Kontrol YALNIZ ARTIŞTA koşar (kullanıcı kararı 2026-07-31, H5 denetimi
        # O1): kota SONRADAN düşürülürse (dağıtım revize edilir) taslakta duran
        # satır zaten aşmış olur — kural azaltmaya da uygulansaydı kullanıcı
        # `quantity: 0` göndererek bile taslağı kurtaramaz, hakediş kilitlenirdi.
        # Azaltma ve `0` HER ZAMAN serbest; yeni aşım (miktarı artırarak kotayı
        # geçme) yine sert 422'dir. Yeni satırda "mevcut miktar" 0'dır — yani
        # kotayı aşan YENİ satır bu inceltmeden faydalanmaz.
        completed_quantity = prior_totals.get(key, (_ZERO, _ZERO))[0]
        if entry.quantity > current_quantity and completed_quantity + entry.quantity > quota:
            raise SiteValidationError(guards.QUANTITY_EXCEEDS_QUOTA)

        # §4.1: öntanım YALNIZ yeni satıra iner; var olan satırın katsayısı
        # gönderilmediğinde KORUNUR (sessizce 1.000'e düşmez).
        #
        # FF kilidi (§10/5) GÖNDERİLEN değere uygulanır, yazılacak değere DEĞİL:
        # saklanan ≠1 katsayılar grandfather'lanır (bkz. `guards.validate_coefficient`
        # gerekçesi — aksi hâlde FF sonradan kapatılınca taslak kilitlenirdi).
        guards.validate_coefficient(
            entry.coefficient, has_price_escalation=contract.has_price_escalation
        )
        coefficient = entry.coefficient
        if coefficient is None:
            coefficient = (
                default_coefficient if existing_line is None else existing_line.coefficient
            )

        # SD-2 (kullanıcı kararı S1): damga HER seferinde yeniden türetilir —
        # satırın eski damgası KORUNMAZ, çünkü miktar değiştiyse "günlükten
        # geldi" iddiası da düşer.
        quantity_source = _stamp(diary_totals, key, entry.quantity)

        resolved.append(
            _ResolvedLine(
                key=key,
                item=item,
                group_name=group_name,
                quantity=entry.quantity,
                coefficient=coefficient,
                quantity_source=quantity_source,
            )
        )
    return resolved


def _new_line(plan: _ResolvedLine) -> ProgressPaymentLine:
    """Snapshot beşlisi (spec §5/D3-D5) YALNIZ yeni satırda kalemden kopyalanır —
    otorite sözleşme kalemidir, BOQ satırı değil. Mevcut satırın snapshot'ı
    DONMUŞTUR; tazeleme yalnız `refresh-prices` ucundadır (H7)."""
    return ProgressPaymentLine(
        contract_item_id=plan.item.id,
        site_id=plan.key[1],
        code=plan.item.code,
        source_code=plan.item.source_code,
        description=plan.item.description,
        unit=plan.item.unit,
        contract_unit_price=plan.item.unit_price,
        coefficient=plan.coefficient,
        quantity=plan.quantity,
        group_name=plan.group_name,
        quantity_source=plan.quantity_source,
    )


async def build_lines(
    session: AsyncSession,
    project: Project,
    contract: ProjectContract,
    inputs: list[ProgressPaymentLineInput],
    *,
    default_coefficient: Decimal,
    period: tuple[int | None, int | None],
) -> list[ProgressPaymentLine]:
    """Oluşturma yolunun (`POST …/progress-payments` iç içe `lines[]`) satırları.

    `PUT …/lines` ile AYNI `_resolve` korkuluklarından geçer — tek yol. SD-2
    damgası da bu yüzden burada da iner: damgayı yalnız `PUT`a koymak, aynı
    satırı `POST` üzerinden damgasız yazan bir arka kapı bırakırdı.

    `period` hakediş HENÜZ YAZILMADIĞI için çağırandan (gövdeden) gelir.

    `exclude_payment_id` YOK: hakediş henüz YAZILMADI, kimliği de yoktur; zaten
    `draft` doğduğu için kota kümesinin (`approved|paid`) içinde olamaz.
    """
    resolved = await _resolve(
        session,
        project,
        contract,
        inputs,
        default_coefficient=default_coefficient,
        exclude_payment_id=None,
        existing={},
        period=period,
    )
    lines = []
    for sort_order, plan in enumerate(resolved):
        line = _new_line(plan)
        line.sort_order = sort_order
        lines.append(line)
    return lines


async def apply_lines(
    session: AsyncSession,
    project: Project,
    contract: ProjectContract,
    payment: ProgressPayment,
    inputs: list[ProgressPaymentLineInput],
) -> int:
    """`PUT …/lines` gövdesini hakedişe uygular (DEĞİŞTİRME semantiği).

    Var olan hücre KORUNUR (kimliği ve snapshot'ı ile), yalnız miktar/katsayı/sıra
    güncellenir; gövdede geçmeyen hücre `delete-orphan` ile SİLİNİR.

    ## Bağı kopmuş satırlar: SESSİZ ATLAMA YOK (spec §10/7, H5 denetimi O3)

    Kalemi silinmiş satır (`contract_item_id IS NULL`, FK `SET NULL`) gövdeden
    ADRESLENEMEZ — gövde tablonun tamamı olduğu için ilk kaydetmede düşer. Bu
    kaçınılmazdır ama SESSİZ OLAMAZ: düşen satır sayısı DÖNDÜRÜLÜR ve yanıtın
    `dropped_orphan_count` alanıyla kullanıcıya bildirilir. 409 ile onay istenip
    ikinci tura çıkılmaz (mockup'ta böyle bir adım yok — zarif düşüş + bildirim).
    """
    existing = {
        (line.contract_item_id, line.site_id): line
        for line in payment.lines
        if line.contract_item_id is not None
    }
    dropped_orphan_count = sum(1 for line in payment.lines if line.contract_item_id is None)
    resolved = await _resolve(
        session,
        project,
        contract,
        inputs,
        default_coefficient=payment.default_coefficient,
        exclude_payment_id=payment.id,
        existing=existing,
        period=(payment.period_year, payment.period_month),
    )

    # --- Buradan itibaren yazma; doğrulama YOK (yukarıdaki sıra kısıtı). ---
    lines: list[ProgressPaymentLine] = []
    for sort_order, plan in enumerate(resolved):
        line = existing.get(plan.key)
        if line is None:
            line = _new_line(plan)
        else:
            line.quantity = plan.quantity
            line.coefficient = plan.coefficient
            # Damga TAZELENİR (SD-2): var olan satırın eski `diary` iddiası,
            # miktar günlükten ayrıldığı anda düşmelidir.
            line.quantity_source = plan.quantity_source
        line.sort_order = sort_order
        lines.append(line)
    payment.lines = lines
    await session.flush()
    return dropped_orphan_count
