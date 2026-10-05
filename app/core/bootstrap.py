import logging

from sqlalchemy import func, select

from app.core.config import settings
from app.core.db import SessionLocal
from app.core.security import hash_password
from app.modules.company.models import Company
from app.modules.roles.models import SYSTEM_ADMIN_KEY, Role
from app.modules.users.models import User, UserStatus

logger = logging.getLogger(__name__)


async def ensure_first_admin() -> None:
    """DB'de hiç kullanıcı yoksa env'deki bilgilerle ilk sistem yöneticisini oluşturur.

    Kullanıcı oluşturma ucu `user_management` admin yetkisi ister; ilk kurulumda hiçbir
    yetkili kullanıcı olmadığından (tavuk-yumurta) tohumlama buradan yapılır.
    `ADMIN_EMAIL`/`ADMIN_PASSWORD` boşsa ya da DB'de zaten kullanıcı varsa hiçbir şey yapılmaz;
    böylece açılışta tekrar tekrar çalışsa da yalnızca bir kez etki eder.
    """
    if not settings.admin_email or not settings.admin_password:
        return

    async with SessionLocal() as session:
        existing = await session.scalar(select(func.count()).select_from(User))
        if existing:
            return

        role = await session.scalar(select(Role).where(Role.key == SYSTEM_ADMIN_KEY))
        if role is None:
            logger.warning("İlk admin oluşturulamadı: '%s' rolü seed edilmemiş.", SYSTEM_ADMIN_KEY)
            return

        admin = User(
            all_projects=True,  # IZN-B3: "Tüm projeler" artık kullanıcı kolonunda
            email=settings.admin_email,
            password_hash=hash_password(settings.admin_password),
            full_name="Sistem Yöneticisi",
            title="Sistem Yöneticisi",
            role_id=role.id,
            status=UserStatus.active,
        )
        session.add(admin)
        await session.commit()
        logger.info("İlk sistem yöneticisi oluşturuldu: %s", settings.admin_email)


async def ensure_company() -> None:
    """DB'de sirket satiri yoksa bos tek satiri olusturur (spec §4.1: tek sirket).

    Idempotent: satir varsa hicbir sey yapmaz. GET /company okuma yolunda da get-or-create
    vardir; bu bootstrap yalnizca ilk istegin yazma yapmamasi icin sicak-baslatmadir.
    """
    async with SessionLocal() as session:
        existing = await session.scalar(select(func.count()).select_from(Company))
        if existing:
            return
        session.add(Company())
        await session.commit()
        logger.info("Bos sirket satiri olusturuldu.")
