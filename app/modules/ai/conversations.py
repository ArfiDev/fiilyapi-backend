"""Sohbet saklama — A3 kararının kod hâli (AI-CHAT-2 / K2).

## 🔴 SAHİPLİK KAPISI TEK YERDE

Her sorgu `WHERE user_id = :actor` taşır ve bu koşul **çağıran tarafından
verilmez, burada kurulur**. `ai:view` yetkisi olan herkes AI'ı kullanabilir; bu
**başkasının sorularını okumak** demek DEĞİLDİR. Modül kapısı ile sahiplik
kapısı iki ayrı şeydir ve modül kapısı sahipliği İKAME ETMEZ.

Bulunamayan/başkasına ait sohbet **404** döner, 403 değil: 403 "bu var ama senin
değil" der ve varlık sızıntısıdır (S14).

## 🔴 `SessionLocal` KULLANAN İKİNCİ DOSYA — bilinçli genişleme

`test_ai0b_yapisal.py::test_S15_...` `ai/**` altında `SessionLocal(` kullanan tek
dosyanın `audit.py` olmasını bekçiliyordu. Bu dosya o kümeye **ikinci** üye olarak
girer ve bekçi bunu ADIYLA tanıyacak şekilde güncellenir — sessizce gevşetilmez.

Gerekçe ölçüldü ve §5-33'ün ta kendisidir: asistan cevabı ancak akış **bittikten**
sonra bilinir. `AiSessionLocal` salt-okunurdur ve INSERT'i PostgreSQL düzeyinde
reddeder. Geriye tek doğru seçenek kalır: **kendi yazılabilir session'ı**, tıpkı
`audit.py` gibi.

🔴 **DÜZELTİLDİ (AI-SOHBET-FIX).** Bu paragraf eskiden *"akış gövdesi `get_db`
bağımlılığı söküldükten sonra koşar; istek session'ı o anda kapalıdır"* diyordu.
**Sıra ölçüldü ve TERSİ çıktı**: FastAPI'nin `yield` bağımlılıkları akış gövdesi
BİTTİKTEN SONRA sökülür, yani `cevabi_sakla` koşarken istek session'ı hâlâ AÇIK
ve COMMIT EDİLMEMİŞTİR. Ayrı session'ın bu satırları görememesinin sebebi
"kapalılık" değil **commit edilmemişlik**tir ve canlıyı öldüren de buydu:

    ForeignKeyViolationError: ai_messages_conversation_id_fkey
    (conversation_id) is not present in table "ai_conversations"

Sonuç doğru, gerekçe yanlıştı (kanon #40: bir kanona uymadan önce gerekçesini
yeniden ÖLÇ). Ayrı session doğru çözümdür; eksik olan şey `turu_baslat`ın
yazısının akış başlamadan **COMMIT EDİLMESİYDİ** — bkz. `turu_baslat`.
"""

from __future__ import annotations

import uuid

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import SessionLocal
from app.modules.ai.models import AiConversation, AiMessage, AiMessageRole

#: `title` kolonu 120; başlık ilk sorudan türetilir ve **kırpılır**.
BASLIK_TAVANI = 80


def baslik_uret(soru: str) -> str:
    """İlk sorudan başlık. 🔴 Modelden başlık İSTENMEZ.

    Mockup'ın "Güneşkent Hakediş Analizi" gibi **özetlenmiş** başlıkları ikinci
    bir model çağrısı ister; bu, her turun maliyetini ve gecikmesini iki katına
    çıkarır. Sapma raporda gerekçelendirilir: başlık sorunun kendisidir.
    """
    tek_satir = " ".join(soru.split())
    if len(tek_satir) <= BASLIK_TAVANI:
        return tek_satir or "Yeni sohbet"
    return tek_satir[: BASLIK_TAVANI - 1].rstrip() + "…"


async def sohbetlerim(
    session: AsyncSession, *, user_id: uuid.UUID, limit: int, offset: int
) -> tuple[list[tuple[AiConversation, int]], int]:
    """Aktörün **kendi** sohbetleri + her birinin mesaj sayısı.

    Mesaj sayısı mockup'ın "4 mesaj · 09:42" satırının ilk yarısıdır ve
    `COUNT`la ölçülür — `len(items)` DEĞİL: liste sayfalanır, sayaç sayfalanmaz.
    """
    sayac = (
        select(AiMessage.conversation_id, func.count().label("adet"))
        .group_by(AiMessage.conversation_id)
        .subquery()
    )
    toplam = await session.scalar(
        select(func.count()).select_from(AiConversation).where(AiConversation.user_id == user_id)
    )
    satirlar = await session.execute(
        select(AiConversation, func.coalesce(sayac.c.adet, 0))
        .outerjoin(sayac, sayac.c.conversation_id == AiConversation.id)
        .where(AiConversation.user_id == user_id)
        .order_by(AiConversation.updated_at.desc())
        .limit(limit)
        .offset(offset)
    )
    return [(k, int(n)) for k, n in satirlar.all()], int(toplam or 0)


async def sohbetim(
    session: AsyncSession, *, user_id: uuid.UUID, conversation_id: uuid.UUID
) -> AiConversation | None:
    """🔴 SAHİPLİK KAPISI. `user_id` koşulu **kaldırılamaz**: bekçi mutasyonu."""
    return await session.scalar(
        select(AiConversation).where(
            AiConversation.id == conversation_id, AiConversation.user_id == user_id
        )
    )


async def mesajlarim(
    session: AsyncSession, *, user_id: uuid.UUID, conversation_id: uuid.UUID
) -> list[AiMessage] | None:
    """Sohbetin mesajları — sahibi değilse `None` (çağıran 404'e çevirir)."""
    if await sohbetim(session, user_id=user_id, conversation_id=conversation_id) is None:
        return None
    satirlar = await session.scalars(
        select(AiMessage)
        .where(AiMessage.conversation_id == conversation_id)
        .order_by(AiMessage.created_at, AiMessage.id)
    )
    return list(satirlar)


async def sohbet_sil(session: AsyncSession, *, conversation_id: uuid.UUID) -> bool:
    """Sohbeti SAHİBİNE BAKMADAN siler; yoksa `False`. Mesajlar FK CASCADE ile gider.

    SIL-B1 (KARARLAR 7034741, K4): silme YALNIZ Sistem Yöneticisi'nindir ve başkasının
    sohbetini de kapsar; sahiplik süzgeci bu fonksiyonda YOKTUR. Sahibi kendi sohbetini
    SİLEMEZ — kapı `require_system_admin`dir (router). Okuma yolları (`sohbetim`,
    `mesajlarim`) sahiplik kapısını TAŞIMAYA devam eder: yalnız silme gevşedi.
    """
    sonuc = await session.execute(
        delete(AiConversation).where(AiConversation.id == conversation_id)
    )
    return bool(sonuc.rowcount)


async def turu_baslat(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    conversation_id: uuid.UUID | None,
    soru: str,
) -> uuid.UUID | None:
    """Soruyu saklar; gerekiyorsa sohbeti açar. Sahibi değilse `None`.

    🔴 **ÇAĞIRAN, AKIŞ BAŞLAMADAN ÖNCE COMMIT ETMEK ZORUNDADIR.** Bu fonksiyon
    yalnız `flush()` eder: satır çağıranın transaction'ında, henüz **görünmez**
    durur. Bir sonraki adım ayrı bir session açıyorsa (`cevabi_sakla`, `audit.py`
    ya da bir `StreamingResponse` gövdesi) o session bu satırı GÖREMEZ ve FK
    ihlaline düşer.

    🔴 Eski yorum burada *"`get_db` temiz çıkışta commit eder; akış gövdesinin
    session'ı ise o sırada zaten kapalıdır"* diyordu. **Sıra ölçüldü, TERSİ**:
    FastAPI `yield` bağımlılıklarını akış gövdesi BİTTİKTEN sonra söker, yani
    `get_db`nin commit'i çok geç gelir. Canlıda her tur bu yüzden patladı; iz
    `app/modules/ai/router.py::ai_chat_endpoint` içindeki commit yorumundadır.

    Commit burada YAPILMAZ, çağıranda yapılır — sebebi de ölçülmüştür: sahiplik
    kapısı (404) ve sağlayıcı kurulumu (503) bu satırın ARDINDAN koşar; burada
    commit edilseydi her başarısız sağlayıcı denemesi cevapsız bir sohbeti
    kullanıcının geçmişine kalıcı olarak yazardı.
    """
    if conversation_id is None:
        sohbet = AiConversation(user_id=user_id, title=baslik_uret(soru))
        session.add(sohbet)
        await session.flush()
        conversation_id = sohbet.id
    else:
        sohbet = await sohbetim(session, user_id=user_id, conversation_id=conversation_id)
        if sohbet is None:
            return None
        sohbet.updated_at = func.now()
    session.add(
        AiMessage(
            conversation_id=conversation_id,
            role=AiMessageRole.kullanici,
            content=soru,
            tool_names=[],
            tool_states=[],
        )
    )
    return conversation_id


async def cevabi_sakla(
    *,
    conversation_id: uuid.UUID,
    metin: str,
    tool_names: list[str],
    tool_states: list[str],
    finish_reason: str | None,
    duration_ms: int | None,
) -> None:
    """Asistan cevabını **kendi yazılabilir session'ında** saklar (§5-33).

    🔴 Araç sonuç **gövdesi** ve yapısal bloklar buraya GİRMEZ (A3). Yalnız
    çağrı adları ve zarf hâlleri saklanır; ikisi aynı sırada bir ÇİFTTİR.

    🔴 Sahiplik burada YENİDEN sorulmaz ve sorulmamalıdır: `conversation_id`
    değeri istemciden değil, `turu_baslat`ın sahiplik kapısından geçmiş
    dönüşünden gelir. İstemciden gelen bir kimliği buraya taşıyan bir çağrı
    yeri açılırsa kapı düşer.
    """
    async with SessionLocal() as session:
        session.add(
            AiMessage(
                conversation_id=conversation_id,
                role=AiMessageRole.asistan,
                content=metin,
                tool_names=tool_names,
                tool_states=tool_states,
                finish_reason=finish_reason,
                duration_ms=duration_ms,
            )
        )
        await session.execute(
            AiConversation.__table__.update()
            .where(AiConversation.id == conversation_id)
            .values(updated_at=func.now())
        )
        await session.commit()
