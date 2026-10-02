import asyncio
from logging.config import fileConfig

from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from alembic import context
from app.core.config import settings
from app.core.db import Base
from app.core.db_guard import ALEMBIC_IZIN_DEGISKENI, uzak_veritabani_kapisi

# TUM modullerin `models` modulu BURADA import edilir (TB1). Import bir YAN ETKI
# icindir: model modulu yuklenmeden tablolari `Base.metadata`ya kaydolmaz ve
# autogenerate / `alembic check` o tablolari "silinecek" diye raporlar (sahte diff).
#
# Bu liste `app/modules/*/models.py` ile BIREBIR ayni olmak zorundadir.
#
# >>> YENI BIR MODUL ACARSAN models.py'sini BURAYA DA EKLE. <<<
from app.modules.accounting import models as accounting_models  # noqa: F401
from app.modules.ai import models as ai_models  # noqa: F401
from app.modules.approvals import models as approvals_models  # noqa: F401
from app.modules.audit import models as audit_models  # noqa: F401
from app.modules.boq import models as boq_models  # noqa: F401
from app.modules.catalog import models as catalog_models  # noqa: F401
from app.modules.company import models as company_models  # noqa: F401
from app.modules.contracts import models as contracts_models  # noqa: F401
from app.modules.customers import models as customers_models  # noqa: F401
from app.modules.documents import models as documents_models  # noqa: F401
from app.modules.earned_value import models as earned_value_models  # noqa: F401
from app.modules.equipment import models as equipment_models  # noqa: F401
from app.modules.inventory import models as inventory_models  # noqa: F401
from app.modules.invoicing import models as invoicing_models  # noqa: F401
from app.modules.offers import models as offers_models  # noqa: F401
from app.modules.payroll import models as payroll_models  # noqa: F401
from app.modules.personnel import models as personnel_models  # noqa: F401
from app.modules.posting import models as posting_models  # noqa: F401
from app.modules.procurement import models as procurement_models  # noqa: F401
from app.modules.progress_payments import models as progress_payments_models  # noqa: F401
from app.modules.projects import models as projects_models  # noqa: F401
from app.modules.roles import models as roles_models  # noqa: F401
from app.modules.sales import models as sales_models  # noqa: F401
from app.modules.settings import models as settings_models  # noqa: F401
from app.modules.site_diary import models as site_diary_models  # noqa: F401
from app.modules.site_planning import models as site_planning_models  # noqa: F401
from app.modules.sites import models as sites_models  # noqa: F401
from app.modules.subcontractor_progress_payments import (  # noqa: F401
    models as subcontractor_progress_payments_models,
)
from app.modules.timesheet import models as timesheet_models  # noqa: F401
from app.modules.treasury import models as treasury_models  # noqa: F401
from app.modules.units import models as units_models  # noqa: F401
from app.modules.users import models as users_models  # noqa: F401

# this is the Alembic Config object, which provides
# access to the values within the .ini file in use.
config = context.config
config.set_main_option("sqlalchemy.url", settings.database_url)

# 🔴 FAIL-CLOSED UZAK DB KAPISI. `alembic.ini:89 sqlalchemy.url` BOŞtur; alembic'in TEK
# URL kaynağı `.env`dir ve oradaki `DATABASE_URL` UZAK Railway CANLI veritabanını
# gösterir. Override'sız koşulan `upgrade`/`downgrade`/`stamp` doğrudan canlıya vurur
# (P7'de oldu: canlı damgalandı, konteyner açılamadı). Bkz. app/core/db_guard.py.
uzak_veritabani_kapisi(
    settings.database_url, izin_degiskeni=ALEMBIC_IZIN_DEGISKENI, baglam="alembic"
)

# Interpret the config file for Python logging.
# This line sets up loggers basically.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# add your model's MetaData object here
# for 'autogenerate' support
target_metadata = Base.metadata

# other values from the config, defined by the needs of env.py,
# can be acquired:
# my_important_option = config.get_main_option("my_important_option")
# ... etc.


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode.

    This configures the context with just a URL
    and not an Engine, though an Engine is acceptable
    here as well.  By skipping the Engine creation
    we don't even need a DBAPI to be available.

    Calls to context.execute() here emit the given string to the
    script output.

    """
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    # 🔴 `transaction_per_migration=True` — HER MIGRATION KENDİ İŞLEMİNDE KOŞAR
    # (MU-SEED T7 bulgusu, kusuru YALNIZ CI'daki PG 16 gördü).
    #
    # Varsayılan (`False`) TÜM zinciri TEK işleme sokar. Postgres ise
    # `ALTER TYPE … ADD VALUE` ile eklenen bir enum değerinin **aynı işlemde
    # KULLANILMASINI** yasaklar. MT-1 (`c8d9e0f1a2b3`) `equity`yi ekler,
    # MU-SEED (`e5f6a7b8c9d0`) o değerle satır INSERT eder; taze bir
    # veritabanında zincir baştan koştuğunda ikisi aynı işleme düşüyordu ve
    # migration şu hatayla patlıyordu:
    #     asyncpg.exceptions.UnsafeNewEnumValueUsageError:
    #     unsafe use of new value "equity" of enum type chart_account_type
    #
    # 🔴 **YEREL PG 18 BUNU GÖREMEZ:** kısıtlama PG 17'de KALDIRILDI. Yerelde
    # (PG 18) upgrade→downgrade→upgrade turu YEŞİL geçiyordu; CI'daki PG 16
    # kırmızı verdi. WORKFLOW'un "PG SÜRÜM TUZAĞI" kanonunun birebir hâli.
    # MT-1'in kendi docstring'i tuzağı yazmıştı (*"yeni değer AYNI işlemde
    # KULLANILMADIĞI sürece"*); değeri fiilen kullanan ilk migration MU-SEED'dir.
    #
    # Sonuç: migration'lar artık tek tek commit'lenir. Zincir ortasında bir
    # migration patlarsa öncekiler UYGULANMIŞ kalır ve `alembic_version` en son
    # başarılı revizyonu gösterir — bir sonraki deploy KALDIĞI YERDEN devam eder.
    # Bu, "hepsi ya da hiçbiri"nden daha güvenlidir: `Dockerfile` açılışta
    # `alembic upgrade head && uvicorn …` koşar, alembic patlarsa `&&` kısa devre
    # yapar ve uygulama zaten açılmaz.
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        transaction_per_migration=True,
    )

    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """In this scenario we need to create an Engine
    and associate a connection with the context.

    """

    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode."""

    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
