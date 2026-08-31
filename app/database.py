from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.config import settings


def _to_asyncpg_url(url: str) -> str:
    """
    Railway (و خیلی جاهای دیگه) یه DATABASE_URL خام به‌شکل postgresql://...
    می‌دن که پیش‌فرض SQLAlchemy باهاش سراغ درایور sync (psycopg2) می‌ره.
    ما از asyncpg استفاده می‌کنیم، پس صریح مجبورش می‌کنیم.
    """
    if url.startswith("postgresql://"):
        return url.replace("postgresql://", "postgresql+asyncpg://", 1)
    if url.startswith("postgres://"):  # بعضی پلتفرم‌ها این فرم قدیمی رو می‌دن
        return url.replace("postgres://", "postgresql+asyncpg://", 1)
    return url


engine = create_async_engine(_to_asyncpg_url(settings.database_url), echo=False)
async_session = async_sessionmaker(engine, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


async def get_db():
    async with async_session() as session:
        yield session
