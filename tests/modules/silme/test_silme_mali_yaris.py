"""SIL-B2 — mali silme ile eşzamanlı ÖDEME ekleme yarışı (SIL-B1 kilit deseninin mali ikizi).

Senaryo: fatura silinirken (karma doğrulandı, kök ve ağaç `FOR UPDATE` KİLİTLİ) başka bir bağlantı
AYNI faturaya ödeme ekler. Kilit çalışıyorsa ekleme BEKLER (INSERT'in `FOR KEY SHARE`i kilitli
fatura satırıyla çakışır) ve silme bitince FK hatasıyla düşer: önizlenmemiş ödeme ASLA oluşmaz,
yetim ödeme/fiş kalmaz. Kilit YOKSA ekleme hemen commit olur ve silmeyi bozar.

`db_session` KULLANILMAZ: iki GERÇEK bağlantı, gerçek commit ve gerçek temizlik
(`test_silme_yaris.py` deseni).
"""

import asyncio
import uuid
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import delete, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.security import hash_password
from app.modules.invoicing.models import (
    Invoice,
    InvoiceDirection,
    InvoiceDocumentType,
    InvoiceStatus,
)
from app.modules.projects.models import Project
from app.modules.roles.models import Role
from app.modules.silme import service
from app.modules.silme.schemas import DeleteKind
from app.modules.treasury.models import BankAccount, BankAccountType, Payment, PaymentMethodKind
from app.modules.users.models import User
from tests._yaris import YARIS_TAVANI_SN, kilitte_bekleyen_sorgu
from tests.conftest import test_engine

pytestmark = pytest.mark.asyncio

_Fabrika = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
_ROL = "sil_b2_yaris_rolu"


async def _kurulum() -> dict[str, uuid.UUID]:
    async with _Fabrika() as oturum:
        rol = Role(key=_ROL, name="SIL-B2 Yarış Rolü")
        oturum.add(rol)
        await oturum.flush()
        kullanici = User(
            email="sil-b2-yaris@test.co",
            password_hash=hash_password("parola1234"),
            full_name="Yarış Kullanıcısı",
            role_id=rol.id,
        )
        proje = Project(code="SIL-B2-YARIS", name="Mali Yarış Projesi")
        oturum.add_all([kullanici, proje])
        await oturum.flush()
        fatura = Invoice(
            direction=InvoiceDirection.outgoing,
            invoice_no="F-YARIS-1",
            document_type=InvoiceDocumentType.earchive,
            status=InvoiceStatus.sent,
            issue_date=date(2026, 3, 5),
            party_name="Taraf",
            project_id=proje.id,
            subtotal=Decimal("1000.00"),
            tax_base=Decimal("1000.00"),
            vat_amount=Decimal("0.00"),
            total=Decimal("1000.00"),
            created_by_id=kullanici.id,
        )
        hesap = BankAccount(
            bank_name="Yarış Bankası", account_type=BankAccountType.checking, display_name="Y"
        )
        oturum.add_all([fatura, hesap])
        await oturum.flush()
        kimlikler = {
            "rol": rol.id,
            "kullanici": kullanici.id,
            "proje": proje.id,
            "fatura": fatura.id,
            "hesap": hesap.id,
        }
        await oturum.commit()
    return kimlikler


async def _temizle(k: dict[str, uuid.UUID]) -> None:
    async with _Fabrika() as oturum:
        await oturum.execute(delete(Payment).where(Payment.bank_account_id == k["hesap"]))
        await oturum.execute(delete(Invoice).where(Invoice.id == k["fatura"]))
        await oturum.execute(delete(BankAccount).where(BankAccount.id == k["hesap"]))
        await oturum.execute(delete(Project).where(Project.id == k["proje"]))
        await oturum.execute(delete(User).where(User.id == k["kullanici"]))
        await oturum.execute(delete(Role).where(Role.id == k["rol"]))
        await oturum.commit()


async def _sayim(tablo: str, kosul: str = "true") -> int:
    async with _Fabrika() as oturum:
        return int(
            (await oturum.execute(text(f"SELECT count(*) FROM {tablo} WHERE {kosul}"))).scalar_one()
        )


async def test_fatura_silinirken_eszamanli_odeme_ekleme_bekler_yetim_odeme_dogmaz(
    monkeypatch,
) -> None:
    k = await _kurulum()
    gorev_a: asyncio.Task[str] | None = None
    gorev_b: asyncio.Task[None] | None = None
    devam = asyncio.Event()
    try:
        async with _Fabrika() as oturum:
            token = (await service.onizle(oturum, DeleteKind.invoice, k["fatura"])).preview_token

        kilitli = asyncio.Event()
        gercek = service.agaci_sil

        async def bariyerli(oturum, metadata, agac):  # karma doğrulandı, silme HENÜZ başlamadı
            kilitli.set()
            await devam.wait()
            return await gercek(oturum, metadata, agac)

        monkeypatch.setattr(service, "agaci_sil", bariyerli)

        async def silici() -> str:
            async with _Fabrika() as oturum:
                detay = await service.sil(oturum, "invoice", k["fatura"], token)
                await oturum.commit()
                return detay

        async def ekleyici() -> None:
            async with _Fabrika() as oturum:
                oturum.add(
                    Payment(
                        invoice_id=k["fatura"],
                        bank_account_id=k["hesap"],
                        method=PaymentMethodKind.transfer,
                        amount=Decimal("100.00"),
                        paid_on=date(2026, 3, 20),
                        created_by_id=k["kullanici"],
                    )
                )
                await oturum.commit()

        gorev_a = asyncio.create_task(silici())
        await asyncio.wait_for(kilitli.wait(), timeout=YARIS_TAVANI_SN)  # A kilitleri aldı, TUTUYOR

        gorev_b = asyncio.create_task(ekleyici())
        bekleyen = await kilitte_bekleyen_sorgu(
            test_engine, gorev_b, mesaj="ödeme ekleme kilitli faturada BEKLEMELİ"
        )
        assert "payments" in bekleyen, bekleyen
        assert not gorev_b.done()  # `not done` bariyeri

        devam.set()
        detay = await asyncio.wait_for(gorev_a, timeout=YARIS_TAVANI_SN)

        with pytest.raises(IntegrityError):  # fatura gitti: bekleyen ekleme FK ile DÜŞER
            await asyncio.wait_for(gorev_b, timeout=YARIS_TAVANI_SN)
        assert await _sayim("payments", f"bank_account_id = '{k['hesap']}'") == 0
        assert await _sayim("invoices", f"id = '{k['fatura']}'") == 0
        assert "Fatura silindi" in detay
    finally:
        devam.set()
        for gorev in (gorev_a, gorev_b):
            if gorev is not None and not gorev.done():
                gorev.cancel()
        await asyncio.gather(
            *(g for g in (gorev_a, gorev_b) if g is not None), return_exceptions=True
        )
        await _temizle(k)
