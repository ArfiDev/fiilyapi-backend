"""Taşeron hakedişi durum makinesi (T4; spec §5).

## Tablo PAYLAŞILIR, kopyalanmaz

Geçiş tablosunun ŞEKLİ `progress_payments/transitions.py:_TRANSITION_SHAPE`te
TEK kopyadır; burada yalnız kendi enum tipimize BAĞLANIR
(`build_transition_table`). `PaymentAction` de oradan İTHAL EDİLİR — uç yolları
(`…/mark-paid`) iki ailede birebir aynıdır, ikinci bir eşleme sözlüğü doğmaz.

Yön TEK taraflıdır (`subcontractor_progress_payments` → `progress_payments`),
`calculations`/`guards` paylaşımıyla aynı yön; ters import ASLA açılmaz.

## İşverenden İKİ FARK (spec §5)

1. **`reject` damga BIRAKIR.** İşverende gerekçe yalnız denetim günlüğüne giden
   opsiyonel bir metindir; burada `rejected_at` + `rejection_reason` KOLONLARINA
   yazılır ve gerekçe ZORUNLUDUR. Kaynağı L177 "Revize Gerekli" rozetidir:
   rozet beşinci bir durum DEĞİL, `draft AND rejected_at IS NOT NULL` türevidir
   (`schemas`/`read` tarafında `is_revision_required`).
2. **`submit` damgayı TEMİZLER.** Yeniden onaya gönderilen hakediş artık
   "revize bekleyen" değildir; `rejected_at`/`rejection_reason` NULL'lanır.
   İşverendeki "reject `submitted_at`'i temizlemez" asimetrisi burada da
   korunur (gönderim GERÇEKTEN olmuştur, damga yeniden `submit`te üzerine yazılır).

## 🔴 PARA-GERCEK — `paid` DAMGASI ARTIK BEDAVA DEĞİL

Kullanıcının kuralı: *"Nakit olarak görmeden veya çekin vadesi gelip de tahsil
edilmeden 'ödendi' gözükmemesi gerekiyor."* Canlıda ÜÇ taşeron hakedişi
arkalarında hiçbir ödeme kaydı olmadan `paid` görünüyordu.

`mark_paid` artık `_assert_para_gercek`ten geçer: hakedişin bağlayıcı faturasına
yazılmış ve NAKDE GEÇMİŞ ödemeler netini karşılamıyorsa **409**. Nakdin tanımı
`treasury.balance.cash_realized_condition`tır (banka bakiyesinin tek kaynağı) —
yani çek portföyde beklerken hakediş ödenmiş SAYILMAZ, tahsil/ödeme damgası
düştüğü an SAYILIR.

🔴 Kapı İLERİ yöndedir: tablo DEĞİŞMEDİ, `paid` hâlâ TERMİNALDİR. Gerekçe ve
kapsam dışı bırakılan karar (ödeme silinirse/karşılıksız çıkarsa) kardeş
dosyanın docstring'inde TEK KOPYA olarak durur.

## Kotanın nihai bekçisi: ONAY anı (spec §4)

`approve` kotayı KİLİT ALTINDA yeniden doğrular ve bunu **sırasız TAM küme**
üzerinden yapar (`lines.completed_quantities_for(exclude_payment_id=…)`). Satır
yazma yolundaki "yalnız artışta koşar" inceltmesi BURADA GEÇERSİZDİR: orada amaç
kotası sonradan düşürülen bir taslağın düzeltilebilir kalmasıdır; burada ise
kayıt kümülatif kümeye (`approved|paid`) GİRİYOR — aşmış bir hakedişin onayı
aşımı KALICILAŞTIRIR. Kendisi kümeden dışlanır, yoksa `unapprove` + yeniden
`approve` kendi miktarını iki kez sayardı.
"""

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import NamedTuple

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ConflictError, SiteValidationError
from app.core.timezone import to_display
from app.modules.approvals import service as approvals_service
from app.modules.approvals.models import ApprovalDocumentType
from app.modules.contracts.models import SubcontractorContract
from app.modules.invoicing import source_posting
from app.modules.invoicing.models import Invoice
from app.modules.progress_payments import calculations
from app.modules.progress_payments.transitions import PaymentAction, build_transition_table
from app.modules.projects.models import Project
from app.modules.subcontractor_progress_payments import (
    guards,
    lines,
    posting,
    repository,
    service,
)
from app.modules.subcontractor_progress_payments.models import (
    SubcontractorPaymentStatus,
    SubcontractorProgressPayment,
)
from app.modules.treasury import realized
from app.modules.users.models import User

_ZERO = Decimal("0")

#: Bu evrak ailesinin onay zincirindeki kimligi (mockup `Onay Kutusu.dc.html:120-144`).
_DOCUMENT_TYPE = ApprovalDocumentType.subcontractor_progress_payment

__all__ = ["TRANSITIONS", "PaymentAction", "TransitionResult", "perform"]

#: Spec §5 tablosu — ŞEKLİ işveren modülünden gelir, TİPİ buradan.
TRANSITIONS: dict[tuple[SubcontractorPaymentStatus, PaymentAction], SubcontractorPaymentStatus] = (
    build_transition_table(SubcontractorPaymentStatus)
)


class TransitionResult(NamedTuple):
    """`perform()` dönüşü — denetim günlüğü sözleşme/proje adlarını da ister.

    `previous_approver_name`/`previous_approved_at` YALNIZ `unapprove`de doludur
    (işveren H10 dersinin birebiri): `_stamp` damgaları NULL'lamadan ÖNCE
    okunur — router döndüğünde değerler zaten silinmiştir, yeniden sorgulayamaz.
    """

    payment: SubcontractorProgressPayment
    contract: SubcontractorContract
    project: Project
    previous_approver_name: str | None = None
    previous_approved_at: datetime | None = None
    #: OK-1A T3 — zincirin bu istekte verdiği karar (`None` => zincirsiz ESKİ kayıt).
    chain_step: approvals_service.ChainDecision | None = None
    #: `/unapprove`te geri sarılan adım (`None` => zincirsiz ya da imza yok).
    chain_rewind: approvals_service.ChainRewind | None = None


async def _revalidate_quota(
    session: AsyncSession, contract: SubcontractorContract, payment: SubcontractorProgressPayment
) -> None:
    """Spec §4 tavanını ONAY anında yeniden doğrular (modül docstring'indeki gerekçe).

    Kural `lines.check_quota` ile TEK kopyadır, toplama `lines.
    completed_quantities_for` ile TEK kopyadır — ikinci bir doğruluk tanımı
    açılmaz. 🔴 İKİNCİ tavan (ÇİFT SAYIM / TH-PRJGENEL) burada da koşar
    (`lines.check_source_quota`): aynı işveren kalemine bağlı BAŞKA bir taşeron
    sözleşmesi araya girip onaylanmışsa, yazma anında sığan evrak onay anında
    AŞAR — yalnız yazma yolunda sınansaydı iki taslak önce yazılıp sonra ikisi
    de onaylanarak tavan aşılabilirdi.

    Bağı kopmuş satır (`contract_item_id IS NULL`) atlanır: kalemi silinmiş
    satırın kotası da yoktur, onayı engellemek evrağı kilitlerdi (kümülatiften
    de düşer — `completed_quantities` ile aynı ONAYLI SAPMA).
    """
    item_ids = [line.contract_item_id for line in payment.lines if line.contract_item_id]
    if not item_ids:
        return
    completed = await lines.completed_quantities_for(
        session, contract.id, exclude_payment_id=payment.id
    )
    items = await repository.get_contract_items_by_ids(session, item_ids)
    source_quotas = await lines.source_quotas_for(
        session, list(items.values()), exclude_payment_id=payment.id
    )
    # Bu evrağın KENDİ satırlarının kaynak tüketimi — evrak kümülatif kümeye
    # BİRLİKTE girer, satırları tek tek sınamak gövdeyi çift saydırırdı.
    used_by_source: dict[uuid.UUID, Decimal] = {}
    for line in payment.lines:
        item = items.get(line.contract_item_id) if line.contract_item_id else None
        if item is None:
            continue
        lines.check_quota(item, completed.get(item.id, _ZERO), line.quantity)
        source_id = item.source_contract_item_id
        lines.check_source_quota(item, source_quotas, used_by_source, line.quantity)
        if source_id is not None:
            used_by_source[source_id] = used_by_source.get(source_id, _ZERO) + line.quantity


async def _fisle(
    session: AsyncSession,
    actor: User,
    payment: SubcontractorProgressPayment,
    contract: SubcontractorContract,
    action: PaymentAction,
    new_status: SubcontractorPaymentStatus,
) -> None:
    """🔴 MU-3D — taşeron hakedişinin yevmiye fişi. İşveren ailesinin AYNASI.

    Gerekçelerin tamamı (kanca neden GEÇİŞE değil BELGEYE bağlı — ve o
    denetimin bugün neden EŞDEĞER olduğu · sıra neden damga → fiş ·
    `unapprove` neden STORNO yazar) kardeş dosyada
    `progress_payments.transitions._fisle`de TEK KOPYA olarak durur.

    🔴 Bu ailede sözleşme bedeli bir KOLON DEĞİLDİR: `subcontractor_contracts`ta
    `amount` yoktur, bedel her okumada kalemlerden toplanır
    (`repository.get_contract_amount`). Avans tavanı buna bağlı olduğu için
    fişin tabanı da buradan geçer — ve fiş yazıldıktan sonra bir kalem
    düzeltilse bile fişin tutarı DEĞİŞMEZ (fişin kendisi snapshot'tır).
    """
    if action is PaymentAction.unapprove:
        await posting.reverse_subcontractor_payment(session, actor, payment.id)
        return
    if new_status is not SubcontractorPaymentStatus.approved:
        return
    contract_amount = await repository.get_contract_amount(session, payment.contract_id)
    # 🔴 `sequence_no` ARTAN sıra ŞARTTIR: avans mahsubu zinciri sıralıdır ve
    #    her adımın tavanı bir öncekinin sonucuna bağlıdır. ÖNE ALINDI (kayıt
    #    53/54): gerçek taban bilinmeden ters-sıra kusuru ölçülemez — aşağı.
    prior = await repository.list_completed_payments(
        session, payment.contract_id, before_sequence_no=payment.sequence_no
    )
    advance_recovered = calculations.cumulative_state(prior, contract_amount).advance_recovered
    base = posting.posting_base_for(payment, contract_amount, advance_recovered)
    # 🔴 KRIT-HAKEDIS K3 — gerekçe kardeş dosyada TEK KOPYA
    #    (`invoicing.source_posting.source_replaced_by_invoice`).
    if await source_posting.source_replaced_by_invoice(
        session, Invoice.subcontractor_progress_payment_id, payment.id
    ):
        # 🔴 KAYIT 53/54 — TERS SIRA (gerekçe kardeş dosyada TEK KOPYA).
        mismatch = await source_posting.replacing_invoice_base_mismatch(
            session, Invoice.subcontractor_progress_payment_id, payment.id, base
        )
        if mismatch is not None:
            raise SiteValidationError(
                guards.SOURCE_REPLACED_BASE_MISMATCH.format(fatura=mismatch, hakedis=base)
            )
        return
    await posting.post_subcontractor_payment(
        session,
        actor,
        payment,
        base=base,
        # 🔴 ONAY GÜNÜ — `period_year`/`period_month` DEĞİL (gerekçe kardeş dosyada).
        entry_date=to_display(payment.approved_at).date(),
        # 🔴 `to_display` ŞART, çıplak `.date()` DEĞİL (TB5 yerel takvim
        #    bekçisi bunu yakaladı): `approved_at` bir `timestamptz`tir ve
        #    ham `.date()` UTC gününü verir. TR UTC+3 olduğu için gece
        #    00:00-03:00 arasında onaylanan bir hakedişin fişi BİR GÜN
        #    GERİYE düşer — ay sınırında ise ÖNCEKİ AYIN mizanına, hatta
        #    KAPALI bir döneme. Gün sınırı tek kaynaktan okunur.
        subcontractor_name=contract.subcontractor_name,
    )


def _stamp(
    payment: SubcontractorProgressPayment,
    action: PaymentAction,
    actor: User,
    reason: str | None,
) -> None:
    """Spec §5 tablosunun damga kolonu."""
    now = datetime.now(UTC)
    if action is PaymentAction.submit:
        payment.submitted_at = now
        # "Revize Gerekli" rozeti SÖNER: kayıt yeniden onay sürecindedir.
        payment.rejected_at = None
        payment.rejection_reason = None
    elif action is PaymentAction.approve:
        payment.approved_at = now
        payment.approved_by = actor.id
    elif action is PaymentAction.reject:
        payment.rejected_at = now
        payment.rejection_reason = reason
    elif action is PaymentAction.mark_paid:
        payment.paid_at = now
    elif action is PaymentAction.unapprove:
        # Onay GERİ ÇEKİLDİ: damgalar da silinir — bırakılsaydı onay bekleyen
        # bir kayıt "onaylayan" bilgisi taşır, denetimde yanlış kişiyi gösterirdi.
        payment.approved_at = None
        payment.approved_by = None


async def _assert_para_gercek(session: AsyncSession, payment: SubcontractorProgressPayment) -> None:
    """🔴 PARA-GERCEK — `mark-paid`in ÖN KOŞULU. İşveren ikizinin AYNASI.

    Canlıda ÜÇ taşeron hakedişi arkalarında tek kuruş ödeme olmadan `paid`
    damgası taşıyordu; `mark_paid` hiçbir şeye bakmıyordu.

    Eşik bağlayıcı FATURANIN `total`idir (hakediş neti DEĞİL) ve gerekçesi
    kardeş modül `treasury.realized`dedir.

    🔴 FAT-HAK — brüt de verilir; `chain_amount` ile AYNI tek kopyadan
    (`calculations.gross_total`) gelir ve gerekçesi işveren ikizindedir.
    """
    await realized.assert_realized_covers(
        session,
        Invoice.subcontractor_progress_payment_id,
        payment.id,
        source_gross=calculations.gross_total(payment.lines),
    )


async def _apply_action_rules(
    session: AsyncSession,
    contract: SubcontractorContract,
    payment: SubcontractorProgressPayment,
    action: PaymentAction,
    reason: str | None,
) -> str | None:
    """İşleme özgü korkuluklar — HEPSİ kilit altında koşar; kırpılmış gerekçeyi döner."""
    if action is PaymentAction.submit:
        guards.validate_submit(payment)
    elif action is PaymentAction.approve:
        await _revalidate_quota(session, contract, payment)
    elif action is PaymentAction.mark_paid:
        await _assert_para_gercek(session, payment)
    elif action is PaymentAction.reject:
        return guards.validate_reject(reason)
    return None


async def _resolve_username(session: AsyncSession, user_id: uuid.UUID | None) -> str | None:
    if user_id is None:
        return None
    user = await session.get(User, user_id)
    return user.full_name if user is not None else None


def chain_amount(payment: SubcontractorProgressPayment) -> Decimal | None:
    """Zincirin eşikle karşılaştıracağı tutar — **BRÜT** (sözleşme R5).

    `amounts.build_block(...).gross` ile AYNI sayıdır ve AYNI tek kopyadan
    gelir (`calculations.gross_total`); `build_block` yolu seçilseydi sözleşme
    bedeli ve kümülatif avans için İKİ SORGU daha koşar, dönen sayı değişmezdi.
    KDV, avans mahsubu ve teminat kesintisi eşiğe GİRMEZ: eşik BRÜT tutara
    bakar (R5), yoksa aynı iş iki farklı KDV oranıyla iki farklı imza zinciri
    doğururdu.

    🔴 NULL-EŞİK / FAIL-CLOSED (SA kanonu): satır YOKSA `None` döner ve motor
    bunu eşiğin ÜSTÜ sayar.
    """
    if not payment.lines:
        return None
    return calculations.gross_total(payment.lines)


async def _chain_decision(
    session: AsyncSession,
    actor: User,
    payment: SubcontractorProgressPayment,
    action: PaymentAction,
    reason: str | None,
) -> tuple[approvals_service.ChainDecision | None, approvals_service.ChainRewind | None]:
    """OK-1A T3 — evrağın onay ZİNCİRİYLE bağı (işveren ikizinin birebiri).

    🔴 KİLİT SIRASI: evrak satırı ZATEN kilitlidir (`visible_payment_locked`,
    sözleşme → hakediş); zincir ANCAK ondan sonra kilitlenir. Sıra ÜÇ evrak
    ailesinde de AYNIDIR (deadlock).
    """
    if action is PaymentAction.submit:
        await approvals_service.create_chain(
            session,
            document_type=_DOCUMENT_TYPE,
            document_id=payment.id,
            amount=chain_amount(payment),
            created_by_user_id=payment.created_by,
        )
        return None, None
    if action is PaymentAction.approve:
        return (
            await approvals_service.approve_next_step(
                session,
                actor=actor,
                document_type=_DOCUMENT_TYPE,
                document_id=payment.id,
                require_chain=False,
            ),
            None,
        )
    if action is PaymentAction.reject:
        return (
            await approvals_service.reject_chain(
                session,
                actor=actor,
                document_type=_DOCUMENT_TYPE,
                document_id=payment.id,
                reason=reason,
                require_chain=False,
            ),
            None,
        )
    if action is PaymentAction.unapprove:
        return None, await approvals_service.rewind_last_step(
            session, document_type=_DOCUMENT_TYPE, document_id=payment.id
        )
    return None, None


async def perform(
    session: AsyncSession,
    actor: User,
    payment_id: uuid.UUID,
    action: PaymentAction,
    *,
    reason: str | None = None,
) -> TransitionResult:
    """Tek geçiş yolu. Sıra: kapsam → kilit → tablo → korkuluk → damga.

    Kapsam süzgeci (404) korkuluklardan ÖNCE koşar: görünmeyen bir hakedişin
    durumu hakkında 409/422 ile bilgi sızdırılmaz (spec §9.0). Kilit sırası
    `service.create`/`save_lines` ile AYNIDIR (önce sözleşme, sonra hakediş) —
    ters sırada kilitleyen bir yol karşılıklı kilitlenme doğurur.

    ZORUNLULUK: `unapprove` için eski `approved_by`/`approved_at` `_stamp` onları
    NULL'lamadan ÖNCE okunur; sıra tersine çevrilirse denetim mesajı sessizce
    "Bilinmiyor" ile gider (hata FIRLAMAZ — bu yüzden testle korunur).
    """
    context = await service.visible_payment_locked(session, actor, payment_id)
    payment, contract, project = context

    new_status = TRANSITIONS.get((payment.status, action))
    if new_status is None:
        raise ConflictError(guards.INVALID_STATUS_TRANSITION)

    trimmed_reason = await _apply_action_rules(session, contract, payment, action, reason)

    chain_step, chain_rewind = await _chain_decision(
        session, actor, payment, action, trimmed_reason
    )
    if action is PaymentAction.approve and chain_step is not None and not chain_step.is_complete:
        # 🔴 ARA ADIM: evrak `pending_approval`da KALIR (durum makinesi
        # DEĞİŞMEDİ; `approved`a giden yol zincirin SON adımından geçiyor).
        # Koşul YALNIZ `approve` içindir: `reject` de `is_complete=False` döner
        # ama zinciri DAMGALAR (silmez) ve evrağı `draft`a taşıması GEREKİR.
        await session.flush()
        await session.refresh(payment)
        return TransitionResult(payment, contract, project, chain_step=chain_step)

    previous_approver_name: str | None = None
    previous_approved_at: datetime | None = None
    if action is PaymentAction.unapprove:
        previous_approved_at = payment.approved_at
        previous_approver_name = await _resolve_username(session, payment.approved_by)

    payment.status = new_status
    _stamp(payment, action, actor, trimmed_reason)
    await _fisle(session, actor, payment, contract, action, new_status)
    await session.flush()
    # `updated_at` server `onupdate` ile yenilendiği için expire olur; açık
    # refresh olmadan yanıt inşası `MissingGreenlet` verir.
    await session.refresh(payment)
    return TransitionResult(
        payment,
        contract,
        project,
        previous_approver_name,
        previous_approved_at,
        chain_step=chain_step,
        chain_rewind=chain_rewind,
    )
