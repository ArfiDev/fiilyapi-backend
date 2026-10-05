"""🔴 MU-3D — **FİŞLENEN OLAY KÜMESİNİ** ölçen bekçi (SAHTE-YEŞİLİN 8. HÂLİ).

## Neden SAYIM YETMEZ

MU-3B'de ölçüldü: *"şu geçiş fazladan fiş üretmiyor"* diyen bir test, fişlenen
eylem kümesine yeni bir üye eklendiğinde **KIRMIZIYA DÖNMEDİ** — çünkü
`post_document` idempotandır, ikinci çağrı `created=False` döner ve FİŞ SAYISI
yine 1 kalır. Test SONUCU ölçüyordu, KÜMEYİ değil.

MU-3D fişlenen olay kümesini ÜÇ AİLE birden genişletiyor ve genişlememesi
gerekenler tam olarak bu dilimin çift sayım riskidir:

* `submit` / `reject` — para taşımaz;
* `mark-paid` / `pay` — nakit bacağı MU-3C'nindir, buradan fiş ATILMAZ;
* kira ailesinde `draft → pending_verification` — DOĞRULANMAMIŞ tutar.

Bu yüzden burada ölçülen şey **KÜMENİN KENDİSİDİR** ve evren BAĞIMSIZ BİR
KAYNAKTAN türetilir: iki hakediş ailesinin olayları `transitions.TRANSITIONS`
TABLOSUNDAN, kira ailesininki `rental_transitions.TRANSITIONS` KENAR
KÜMESİNDEN. Elle yazılmış bir liste, tablo büyüdüğünde SESSİZCE eksik kalırdı.

## Ölçüm: "yeni KAYNAK DAMGALI fiş doğdu mu"

Storno `source_type`/`source_id` TAŞIMAZ (taşısaydı `uq_journal_entries_source`
orijinalle çakışırdı). Bu yüzden ölçüm `source_type IS NOT NULL` olan YENİ
fişler üzerindedir — geri alma yolunun ürettiği storno bir "fişleme olayı"
DEĞİLDİR ve karşılığı `test_mu3d_hakedis_fisleme.py`dedir.

## 🔴 MUTASYON KANITI (raporda sayıyla)

* `transitions._fisle`den `post_*` çağrısı SİLİNİRSE → küme o ailenin üyesini
  KAYBEDER → KIRMIZI.
* `_fisle`nin ölçütü `new_status is approved` yerine `action is approve`
  YAPILIRSA → zincirli evrakta fiş erken doğar (ayrı test).
* `rental_service.pay_invoice` fiş atmaya BAŞLARSA → küme yeni bir üye
  KAZANIR → KIRMIZI.

Üçü de programı GERÇEKTEN değiştirir (eşdeğer mutant değildir).
"""

import uuid

import pytest
from sqlalchemy import select

from app.modules.accounting.models import JournalEntry, JournalSourceType
from app.modules.equipment import rental_service, rental_transitions
from app.modules.equipment.models.enums import RentalInvoiceStatus
from app.modules.progress_payments import transitions as isveren_transitions
from app.modules.progress_payments.models import ProgressPaymentStatus
from app.modules.progress_payments.transitions import PaymentAction
from app.modules.subcontractor_progress_payments import transitions as taseron_transitions
from app.modules.subcontractor_progress_payments.models import SubcontractorPaymentStatus
from tests._para_gercek import parayi_yatir
from tests.modules.approvals.conftest import rol_sahipleri_dolgusu  # noqa: F401  (fixture)
from tests.modules.equipment._mk2_para_gercek import kira_parasini_yatir
from tests.modules.posting._mu3d import (
    aktor,
    esleme_kur,
    isveren_hakedisi,
    kira_hakedisi,
    taseron_hakedisi,
)

pytestmark = pytest.mark.usefixtures("rol_sahipleri_dolgusu")


async def _kaynak_damgalari(session) -> set[tuple[JournalSourceType, uuid.UUID]]:
    """Deftere düşmüş TÜM kaynak damgaları — storno (NULL damga) HARİÇ."""
    rows = (
        await session.execute(
            select(JournalEntry.source_type, JournalEntry.source_id).where(
                JournalEntry.source_type.is_not(None)
            )
        )
    ).all()
    return {(tur, kimlik) for tur, kimlik in rows}


def _hakedis_olaylari(matris, onek: str) -> tuple[str, ...]:
    """Bir hakediş ailesinin olay evreni — GEÇİŞ TABLOSUNDAN türetilir.

    Elle yazılsaydı tabloya yeni bir geçiş eklendiğinde bu bekçi onu HİÇ
    denemez ve o geçiş fiş atmaya başlasa bile yeşil kalırdı.
    """
    return tuple(sorted(f"{onek}.{kaynak.value}->{eylem.value}" for (kaynak, eylem) in matris))


def _kira_olaylari() -> tuple[str, ...]:
    """Kira ailesi bir KENAR KÜMESİDİR (eylem enum'u YOKTUR) — şekli farklıdır."""
    return tuple(
        sorted(
            f"rental.{kaynak.value}->{hedef.value}"
            for kaynak, hedef in rental_transitions.TRANSITIONS
        )
    )


async def test_FISLENEN_OLAY_KUMESI_UC_AILENIN_TABLOLARINDAN_TURETILIR(seeded_db, user_factory):
    """🔴 KÜMEYİ ölçer, SAYIYI değil.

    Her olay tek tek koşturulur ve *"yeni KAYNAK DAMGALI fiş doğdu mu"* sorusu
    ayrı ayrı sorulur. Sonuç bir KÜME olarak karşılaştırılır: tek bir sayı
    (toplam fiş adedi) karşılaştırılsaydı, bir olayın fiş atmaya başlaması
    başka bir olayınkiyle telafi edilebilirdi.
    """
    await esleme_kur(seeded_db)
    kullanici = await aktor(seeded_db, user_factory)

    fisleyen: set[str] = set()
    denenen: list[str] = []
    sayac = iter(range(1, 999))

    # --- İŞVEREN: tablodan türeyen HER geçiş ---
    for kaynak, eylem in sorted(
        isveren_transitions.TRANSITIONS, key=lambda c: (c[0].value, c[1].value)
    ):
        olay = f"isveren.{kaynak.value}->{eylem.value}"
        denenen.append(olay)
        payment, _c, _p = await isveren_hakedisi(
            seeded_db, kullanici, kod=f"MU3D-K{next(sayac)}", sequence_no=1
        )
        payment.status = kaynak
        if kaynak in (ProgressPaymentStatus.approved, ProgressPaymentStatus.paid):
            # `unapprove`/`mark-paid` damgasız bir kayıtta koşamaz.
            from datetime import datetime

            from app.core.timezone import DISPLAY_TIMEZONE

            payment.approved_at = datetime.now(DISPLAY_TIMEZONE)
            payment.approved_by = kullanici.id
        await seeded_db.flush()

        # 🔴 PARA-GERCEK: `mark-paid` artık arkasında GERÇEKLEŞMİŞ para ister;
        #    kurulmazsa bu tur, ölçtüğü fişleme kuralını değil kapının 409'unu
        #    gösteren bir kırmızı verirdi.
        if eylem is PaymentAction.mark_paid:
            await parayi_yatir(seeded_db, payment.id, taseron=False)

        once = await _kaynak_damgalari(seeded_db)
        # `reject` gerekçe ZORUNLU ister (taşeron ailesinde kolon, işverende
        #    denetim metni) — verilmezse test, ölçtüğü kuralı değil kurulumu
        #    gösteren bir kırmızı verirdi.
        await isveren_transitions.perform(
            seeded_db,
            kullanici,
            payment.id,
            eylem,
            reason="MU-3D bekçi turu" if eylem is PaymentAction.reject else None,
        )
        if await _kaynak_damgalari(seeded_db) - once:
            fisleyen.add(olay)

    # --- TAŞERON ---
    for kaynak, eylem in sorted(
        taseron_transitions.TRANSITIONS, key=lambda c: (c[0].value, c[1].value)
    ):
        olay = f"taseron.{kaynak.value}->{eylem.value}"
        denenen.append(olay)
        payment, _c = await taseron_hakedisi(
            seeded_db, kullanici, kod=f"MU3D-T{next(sayac)}", sequence_no=1
        )
        payment.status = kaynak
        if kaynak in (SubcontractorPaymentStatus.approved, SubcontractorPaymentStatus.paid):
            from datetime import datetime

            from app.core.timezone import DISPLAY_TIMEZONE

            payment.approved_at = datetime.now(DISPLAY_TIMEZONE)
            payment.approved_by = kullanici.id
        await seeded_db.flush()

        # 🔴 PARA-GERCEK: `mark-paid` artık arkasında GERÇEKLEŞMİŞ para ister;
        #    kurulmazsa bu tur, ölçtüğü fişleme kuralını değil kapının 409'unu
        #    gösteren bir kırmızı verirdi.
        if eylem is PaymentAction.mark_paid:
            await parayi_yatir(seeded_db, payment.id, taseron=True)

        once = await _kaynak_damgalari(seeded_db)
        # `reject` gerekçe ZORUNLU ister (taşeron ailesinde kolon, işverende
        #    denetim metni) — verilmezse test, ölçtüğü kuralı değil kurulumu
        #    gösteren bir kırmızı verirdi.
        await taseron_transitions.perform(
            seeded_db,
            kullanici,
            payment.id,
            eylem,
            reason="MU-3D bekçi turu" if eylem is PaymentAction.reject else None,
        )
        if await _kaynak_damgalari(seeded_db) - once:
            fisleyen.add(olay)

    # --- KİRA: kenar kümesinden türeyen HER kenar ---
    _EYLEM = {
        (RentalInvoiceStatus.draft, RentalInvoiceStatus.pending_verification): "approve_invoice",
        (
            RentalInvoiceStatus.pending_verification,
            RentalInvoiceStatus.approved,
        ): "approve_invoice",
        (RentalInvoiceStatus.approved, RentalInvoiceStatus.paid): "pay_invoice",
        (
            RentalInvoiceStatus.approved,
            RentalInvoiceStatus.pending_verification,
        ): "reject_invoice",
    }
    for kaynak, hedef in sorted(
        rental_transitions.TRANSITIONS, key=lambda c: (c[0].value, c[1].value)
    ):
        olay = f"rental.{kaynak.value}->{hedef.value}"
        denenen.append(olay)
        invoice, _s = await kira_hakedisi(seeded_db, status=kaynak)
        if _EYLEM[(kaynak, hedef)] == "pay_invoice":
            # PARA-GERCEK (2026-09-19) — `pay` artık gerçekleşmiş para ister.
            # Damga ANLIK GÖRÜNTÜSÜNDEN ÖNCE yazılır: kurulumun kendisi bir
            # "fişleme olayı" sayılmamalıdır.
            await kira_parasini_yatir(seeded_db, invoice.id)
        once = await _kaynak_damgalari(seeded_db)
        await getattr(rental_service, _EYLEM[(kaynak, hedef)])(seeded_db, kullanici, invoice.id)
        if await _kaynak_damgalari(seeded_db) - once:
            fisleyen.add(olay)

    # 🔴 Evrenin GERÇEKTEN denendiğini çakar: bir tablo boşalsa ya da bir olay
    #    atlansa, aşağıdaki küme eşitliği yine tutabilirdi.
    beklenen_evren = sorted(
        _hakedis_olaylari(isveren_transitions.TRANSITIONS, "isveren")
        + _hakedis_olaylari(taseron_transitions.TRANSITIONS, "taseron")
        + _kira_olaylari()
    )
    assert sorted(denenen) == beklenen_evren, (
        "evren eksik denendi — geçiş tabloları ile denenen olaylar AYRIŞTI"
    )

    assert fisleyen == {
        "isveren.pending_approval->approve",
        "taseron.pending_approval->approve",
        "rental.pending_verification->approved",
    }, (
        "FİŞLENEN OLAY KÜMESİ DEĞİŞTİ. Mali olarak bağlayıcı geçiş her ailede "
        "BİRDİR (onay); `submit`/`reject` para taşımaz, `mark-paid`/`pay` bir "
        "ÖDEMEDİR (nakit bacağı MU-3C'nindir) ve kira ailesinde ilk adım "
        "DOĞRULANMAMIŞ bir tutardır. Yeni bir üye ÇİFT SAYIMDIR. "
        f"fişleyen={sorted(fisleyen)}"
    )


async def test_HER_ONAYLI_HAKEDISIN_canli_fisi_VARDIR(seeded_db, user_factory):
    """🔴 KÜMENİN ÖTEKİ YÖNÜ: fişlenen hakedişler kümesi = ONAYLI hakedişler kümesi.

    Bağımsız kaynak, ailelerin KENDİ TABLOLARIDIR. Bir yazma yolu fişlemeyi
    atlarsa (ya da ileride ikinci bir onay ucu açılırsa) iki küme AYRIŞIR; fiş
    SAYISINI ölçen bir test bunu göremezdi çünkü sayı yine "onay sayısı kadar"
    görünebilirdi.
    """
    from app.modules.progress_payments.models import ProgressPayment
    from app.modules.subcontractor_progress_payments.models import (
        SubcontractorProgressPayment,
    )

    await esleme_kur(seeded_db)
    kullanici = await aktor(seeded_db, user_factory)

    for sira in (1, 2):
        payment, _c, _p = await isveren_hakedisi(seeded_db, kullanici, kod=f"MU3D-A{sira}")
        await isveren_transitions.perform(seeded_db, kullanici, payment.id, PaymentAction.approve)
    for sira in (1, 2):
        payment, _c = await taseron_hakedisi(seeded_db, kullanici, kod=f"MU3D-B{sira}")
        await taseron_transitions.perform(seeded_db, kullanici, payment.id, PaymentAction.approve)

    for model, durum, source_type in (
        (ProgressPayment, ProgressPaymentStatus.approved, JournalSourceType.progress_payment),
        (
            SubcontractorProgressPayment,
            SubcontractorPaymentStatus.approved,
            JournalSourceType.subcontractor_progress_payment,
        ),
    ):
        onayli = set(
            (await seeded_db.execute(select(model.id).where(model.status == durum))).scalars().all()
        )
        fislenen = set(
            (
                await seeded_db.execute(
                    select(JournalEntry.source_id).where(JournalEntry.source_type == source_type)
                )
            )
            .scalars()
            .all()
        )
        assert onayli, f"{source_type} kurulumu ONAYLI kayıt YAZMADI"
        assert fislenen == onayli, (
            f"{source_type}: FİŞSİZ onaylı hakediş {sorted(onayli - fislenen)} · "
            f"hakedişsiz fiş {sorted(fislenen - onayli)}"
        )
