from collections.abc import AsyncGenerator
from typing import Annotated

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.core.config import Settings, settings


class Base(DeclarativeBase):
    pass


#: Salt-okunur (AI) motorun havuz sınırları. Ana motor SQLAlchemy varsayılanında
#: kalır (5 + 10 = 15); AI hattı ana uygulamanın havuzunu tüketemesin diye AÇIKÇA
#: küçük ve taşmasız kurulur. Sihirli sayı olmasın diye adlandırıldı.
READ_ONLY_POOL_SIZE = 3
READ_ONLY_MAX_OVERFLOW = 0


def build_engine(cfg: Settings, *, read_only: bool = False):
    """Yapılandırılmış zaman aşımlarıyla async engine kurar.

    connect_args asyncpg'ye iletilir: `timeout` bağlantı açma, `command_timeout` ise tek
    bir sorgunun üst sınırıdır. Böylece asılı bir sorgu instance'ı süresiz kilitleyemez.

    `read_only=True` (AI-0a T3) bağlantıya PostgreSQL düzeyinde salt-okunurluk
    çakar: `default_transaction_read_only=on` **bağlantı ömrü boyunca** geçerli bir
    sunucu ayarıdır (`server_settings` asyncpg'ye startup parametresi olarak gider),
    tek bir transaction'a bağlı DEĞİLDİR.

    🔴 Bu ayrım ölçülmüş ve bilerek seçilmiştir: salt-okunurluk PostgreSQL'de
    TRANSACTION kapsamlıdır. `SET TRANSACTION READ ONLY` yazsaydık, bir rollback'ten
    sonra SQLAlchemy YENİ bir transaction açar ve o transaction **yazılabilir**
    olurdu — ajan döngüsü bir araç hatasını yutup devam ettiğinde tam olarak bu olur.
    `server_settings` bağlantı düzeyinde durduğu için rollback onu düşürmez. Bekçi
    testi INSERT'ü bilerek bir rollback'ten SONRA dener.

    ⚠️ Bu bir savunma katmanıdır, tek savunma değildir: `default_transaction_read_only`
    uygulama içinden `SET` ile geri alınabilir; GRANT alınamaz. Ayrı bir PG rolü
    (`GRANT SELECT`) hâlâ üstün çözümdür ve açık borçtur.
    """
    connect_args: dict[str, object] = {
        "timeout": cfg.db_connect_timeout,
        "command_timeout": cfg.db_command_timeout,
    }
    if not read_only:
        return create_async_engine(
            cfg.database_url,
            pool_pre_ping=True,
            connect_args=connect_args,
        )
    connect_args["server_settings"] = {"default_transaction_read_only": "on"}
    return create_async_engine(
        cfg.database_url,
        pool_pre_ping=True,
        pool_size=READ_ONLY_POOL_SIZE,
        max_overflow=READ_ONLY_MAX_OVERFLOW,
        connect_args=connect_args,
    )


engine = build_engine(settings)
SessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """İstek başına bir session açar; hattın ucundaki yazıları garanti eder.

    `async with SessionLocal()` yalnızca session'ı kapatır, commit ETMEZ — hâlâ açık bir
    transaction varsa SQLAlchemy bunu session kapanırken sessizce geri alır (rollback).
    Bu yüzden burada temiz çıkışta açıkça commit ediyoruz; bir istisna oluşursa rollback
    yapıp yeniden fırlatıyoruz. Aksi halde örn. `last_login_at` gibi flush edilmiş ama
    commit edilmemiş yazılar prod'da sessizce kaybolur.
    """
    async with SessionLocal() as session:
        try:
            yield session
            # 🔴 `commit()` `try`in İÇİNDEDİR, `else` dalında DEĞİL: commit'in
            # KENDİ patlaması da aşağıdaki açık `rollback` dalına girsin.
            # `else`deyken transaction'ın geri alınması session kapanışının
            # ÖRTÜK davranışına kalıyordu (`AsyncSession.__aexit__` yalnız
            # `close()` çağırır). Bekçi: tests/core/test_getdb_commit_hatasi.py.
            await session.commit()
        except Exception:
            await session.rollback()
            raise


# 🔴 Uçların ve bağımlılıkların KULLANDIĞI takma ad budur; çıplak `Depends(get_db)`
# yazılmaz. `scope="function"`da `get_db`nin commit'i YANITTAN ÖNCE koşar:
# teardown hatası (ör. IntegrityError→409) exception handler'lara ve istemciye
# 5xx olarak ulaşır. Varsayılan "request" kapsamında commit yanıt yazıldıktan
# SONRA koşar, başarısız yazma 200 döner. "request" kapsamlı `get_db` yalnız
# `documents/deps.py`daki akış varyantında izinlidir. FastAPI önbellek anahtarı
# kapsamı içerir: aynı istekte iki kapsam karışırsa İKİ oturum açılır.
DbSession = Annotated[AsyncSession, Depends(get_db, scope="function")]
