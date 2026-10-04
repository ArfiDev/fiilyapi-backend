"""İK-2 T3 — izin kararı/bakiye testlerinin PAYLAŞILAN fikstür ve yardımcıları.

Gövdeler `test_ik2_leave_decision_api.py`den TAŞINDI (kopyalanmadı): iki test
dosyası (`test_ik2_leave_decision_api.py` · `test_ik2_leave_balances_api.py`)
aynı personel/izin tipi kurulumunu kullanır; iki kopya kaçınılmaz olarak
ayrışırdı.

🔴 Modül AYNI DİZİNDEDİR ve fikstürler `conftest`e DEĞİL buraya konuldu:
`conftest` bölünmesi görünürlüğü daraltmaz ama alt PAKETE taşımak daraltırdı.
Her test dosyası ihtiyacı olan fikstürü AÇIKÇA import eder.

🔴 Fikstürler `@pytest.fixture(name="…")` + `…_fixture` modül adıyla tanımlıdır:
import edilen ad testlerin PARAMETRE adıyla çakışınca `ruff` F811 üretiyordu;
`name=` kayıt adını korur, modül düzeyindeki adı ayrıştırır.
"""

import uuid
from datetime import date, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import timezone
from app.modules.audit.models import AuditLog
from app.modules.documents.models import Document
from app.modules.personnel.models import (
    LeaveRequest,
    LeaveStatus,
    LeaveType,
    Personnel,
)
from app.modules.projects.models import Project
from app.modules.site_diary.models import WorkerSource


def _yil() -> int:
    # Cagri aninda hesaplanir: import aninda donan "bugun" TR gece yarisindan sonra geride kalir
    # (TMP-FIX #167).
    return timezone.today().year


def _kidemli_giris() -> date:
    # ~2 yıl 2 ay kıdem → 4857 birinci kademe (14 gün). Bugüne göre türetilir.
    return timezone.today() - timedelta(days=800)


def _yeni_giris() -> date:
    # ~4 ay kıdem → 1 yıl DOLMADI → hak YOK (İZ 163).
    return timezone.today() - timedelta(days=120)


def _gun(ay: int, gun: int) -> str:
    return date(_yil(), ay, gun).isoformat()


@pytest.fixture(name="proje")
async def proje_fixture(seeded_db: AsyncSession, project_factory) -> Project:
    return await project_factory(code="IK2-T3-A", name="İzin Kararı Projesi")


@pytest.fixture(name="personel")
async def personel_fixture(seeded_db: AsyncSession, proje: Project) -> Personnel:
    """Kıdemi 1 yılı GEÇMİŞ personel → yıllık hak 14 gün."""
    kayit = Personnel(
        full_name="Ayşe Demir",
        trade="Büro Şefi",
        source=WorkerSource.company,
        assigned_project_id=proje.id,
        hire_date=_kidemli_giris(),
    )
    seeded_db.add(kayit)
    await seeded_db.flush()
    return kayit


@pytest.fixture(name="kidemsiz_personel")
async def kidemsiz_personel_fixture(seeded_db: AsyncSession) -> Personnel:
    """Kıdemi 1 yılı DOLMAMIŞ personel → hak YOK (İZ 163 "1 yıl dolunca")."""
    kayit = Personnel(
        full_name="Sercan Öztürk",
        source=WorkerSource.company,
        hire_date=_yeni_giris(),
    )
    seeded_db.add(kayit)
    await seeded_db.flush()
    return kayit


@pytest.fixture(name="tarihsiz_personel")
async def tarihsiz_personel_fixture(seeded_db: AsyncSession) -> Personnel:
    """`hire_date` NULL — kıdem BİLİNMEZ. 🔴 fail-closed kanonun asıl deneği."""
    kayit = Personnel(full_name="Tarihsiz Kayıt", source=WorkerSource.company, hire_date=None)
    seeded_db.add(kayit)
    await seeded_db.flush()
    return kayit


@pytest.fixture(name="yillik")
async def yillik_fixture(seeded_db: AsyncSession) -> LeaveType:
    tip = LeaveType(name="Yıllık İzin", deducts_from_annual=True, color="#2563eb", sort_order=1)
    seeded_db.add(tip)
    await seeded_db.flush()
    return tip


@pytest.fixture(name="hastalik")
async def hastalik_fixture(seeded_db: AsyncSession) -> LeaveType:
    """`deducts_from_annual=False` — İZ 87 "Rapor" yıllık haktan DÜŞMEZ."""
    tip = LeaveType(name="Hastalık İzni", requires_document=True, sort_order=2)
    seeded_db.add(tip)
    await seeded_db.flush()
    return tip


@pytest.fixture(name="arsiv_belgesi")
async def arsiv_belgesi_fixture(seeded_db: AsyncSession, proje: Project) -> Document:
    belge = Document(
        project_id=proje.id, filename="rapor.pdf", mime_type="application/pdf", size_bytes=10
    )
    seeded_db.add(belge)
    await seeded_db.flush()
    return belge


async def _talep(
    session: AsyncSession,
    personel: Personnel,
    tip: LeaveType,
    baslangic: date,
    bitis: date,
    durum: LeaveStatus = LeaveStatus.pending,
) -> LeaveRequest:
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


async def _post_talep(client, headers, personel, tip, baslangic: str, bitis: str) -> str:
    yanit = await client.post(
        "/leave-requests",
        json={
            "personnel_id": str(personel.id),
            "leave_type_id": str(tip.id),
            "start_date": baslangic,
            "end_date": bitis,
        },
        headers=headers,
    )
    assert yanit.status_code == 201, yanit.text
    return yanit.json()["id"]


async def _yeni_denetim_metinleri(session: AsyncSession, onceki: set[uuid.UUID]) -> list[str]:
    rows = await session.scalars(select(AuditLog))
    return [row.detail for row in rows if row.id not in onceki]
