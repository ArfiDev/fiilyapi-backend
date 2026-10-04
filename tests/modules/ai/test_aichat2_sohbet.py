"""AI-CHAT-2 / K2 — sohbet saklama + **SAHİPLİK KAPISI**.

Kullanıcı kararı A3 (2026-08-30): *soru + özet saklanır, araç sonuç gövdeleri
HİÇ saklanmaz.* Bu dosya kararın iki yarısını da bekçiler:

1. **Şema**: araç sonuç gövdesini taşıyabilecek bir kolon YOKTUR.
2. **Sahiplik**: bir kullanıcı başkasının sohbetini listeleyemez/okuyamaz ve
   bekçi kapıya **ÇARPAR** (mutasyonla kanıtlı).
"""

from __future__ import annotations

import dataclasses
import uuid

import pytest
from sqlalchemy import select

from app.core.security import create_access_token
from app.modules.ai import conversations
from app.modules.ai.models import AiConversation, AiMessage, AiMessageRole
from app.modules.ai.schemas import BLOKLAR_SAKLANMADI

pytestmark = pytest.mark.asyncio


def _bearer(user) -> dict[str, str]:
    return {"Authorization": f"Bearer {create_access_token(user.id, user.token_version)}"}


async def _sohbet_ac(session, user, soru: str) -> uuid.UUID:
    kimlik = await conversations.turu_baslat(
        session, user_id=user.id, conversation_id=None, soru=soru
    )
    assert kimlik is not None
    await session.flush()
    return kimlik


# --------------------------------------------------------------------------- #
# 1 — ŞEMA: araç sonuç gövdesi taşıyan kolon YOK
# --------------------------------------------------------------------------- #


def test_ai_messages_ARAC_SONUC_GOVDESI_tasiyan_kolon_TASIMAZ() -> None:
    """🔴 A3'ün yapısal kilidi. `data`/`result`/`payload` adlı bir kolon açılırsa
    bordro satırı, TCKN ve personel verisi tabloya YAZILABİLİR hâle gelir."""
    kolonlar = {c.name for c in AiMessage.__table__.columns}
    assert kolonlar == {
        "id",
        "conversation_id",
        "role",
        "content",
        "created_at",
        "tool_names",
        "tool_states",
        "finish_reason",
        "duration_ms",
    }
    yasak = {"data", "veri", "result", "sonuc", "payload", "arguments", "argumanlar", "body"}
    assert not (kolonlar & yasak)
    # JSONB bir kolon **hiç yoktur**: serbest bir gövde kanalı açardı.
    assert all("JSON" not in str(c.type).upper() for c in AiMessage.__table__.columns)


def test_mesaj_rolu_IKI_uye_arac_rolu_YOK() -> None:
    """🔴 `arac` rolü yok: araç sonucu saklanmıyor, dolayısıyla rolü de yok."""
    assert {u.value for u in AiMessageRole} == {"kullanici", "asistan"}


def test_karar_bedeli_EKRANDA_yazar() -> None:
    """Sessizce boş kart basmak yasak; sebep dürüstçe bildirilir."""
    assert "kart" in BLOKLAR_SAKLANMADI.lower()
    assert "SAKLANMAZ" in BLOKLAR_SAKLANMADI


# --------------------------------------------------------------------------- #
# 2 — SAHİPLİK KAPISI (K-IKIZ1): bekçi kapıya ÇARPAR
# --------------------------------------------------------------------------- #


async def test_KIKIZ1_baskasinin_sohbeti_LISTEDE_GORUNMEZ(client, seeded_db, user_factory) -> None:
    a = await user_factory("a@fiil.test", "Parola123!", "patron", full_name="A")
    b = await user_factory("b@fiil.test", "Parola123!", "patron", full_name="B")
    await _sohbet_ac(seeded_db, a, "A'nın gizli sorusu: bordroyu göster")

    yanit = await client.get("/ai/conversations", headers=_bearer(b))
    assert yanit.status_code == 200
    govde = yanit.json()
    assert govde["total"] == 0
    assert govde["items"] == []
    # 🔴 BAYT düzeyinde: A'nın sorusu B'nin yanıtında HİÇ geçmemeli.
    assert "gizli sorusu" not in yanit.text


async def test_KIKIZ1_baskasinin_sohbeti_404_403_DEGIL(client, seeded_db, user_factory) -> None:
    """🔴 403 "bu var ama senin değil" der ve bir VARLIK SIZINTISIDIR (S14)."""
    a = await user_factory("a2@fiil.test", "Parola123!", "patron")
    b = await user_factory("b2@fiil.test", "Parola123!", "patron")
    kimlik = await _sohbet_ac(seeded_db, a, "A'nın sorusu")

    yanit = await client.get(f"/ai/conversations/{kimlik}", headers=_bearer(b))
    assert yanit.status_code == 404, yanit.text
    assert "A'nın sorusu" not in yanit.text

    # Var olmayan bir kimlik **BAYT BAYT AYNI** cevabı alır.
    yok = await client.get(f"/ai/conversations/{uuid.uuid4()}", headers=_bearer(b))
    assert yok.status_code == 404
    assert yok.json() == yanit.json()


async def test_KIKIZ1_baskasinin_sohbetine_MESAJ_EKLENEMEZ(client, seeded_db, user_factory) -> None:
    """`POST /ai/chat` gövdesindeki `conversation_id` bir SAHİPLİK kapısıdır."""
    a = await user_factory("a3@fiil.test", "Parola123!", "patron")
    b = await user_factory("b3@fiil.test", "Parola123!", "patron")
    kimlik = await _sohbet_ac(seeded_db, a, "A'nın sorusu")

    yanit = await client.post(
        "/ai/chat",
        headers=_bearer(b),
        json={"mesaj": "B araya giriyor", "conversation_id": str(kimlik)},
    )
    # 🔴 503 (sağlayıcı yok) DEĞİL 404 olmalı: sahiplik kapısı sağlayıcı
    # kurulumundan ÖNCE gelmiyorsa, sağlayıcı bir gün yapılandırıldığında kapı
    # sessizce açılırdı. (Bugün sağlayıcı yapılandırılmadığı için 503 de
    # görülebilir; ikisi de "yazılmadı" demektir ve test ikisini AYIRIR.)
    assert yanit.status_code in (404, 503)
    if yanit.status_code == 503:
        pytest.fail(
            "Sahiplik kapısı sağlayıcı kurulumundan SONRA koşuyor: sağlayıcı "
            "yapılandırıldığı gün B, A'nın sohbetine yazabilir."
        )
    # Ve A'nın sohbetine hiçbir şey eklenmemiş olmalı.
    mesajlar = (
        await seeded_db.scalars(select(AiMessage).where(AiMessage.conversation_id == kimlik))
    ).all()
    assert [m.content for m in mesajlar] == ["A'nın sorusu"]


async def test_KIKIZ1_sohbet_silme_yalniz_sistem_yoneticisi_baskasinin_sohbetini_de_siler(
    client, seeded_db, user_factory
) -> None:
    """SIL-B1 (KARARLAR 7034741): silme YALNIZ Sistem Yöneticisi'nindir ve başkasının sohbetini de
    kapsar. SAHİBİ Sistem Yöneticisi değilse kendi sohbetini DE silemez (403, istisna yok)."""
    sahip = await user_factory("a4@fiil.test", "Parola123!", "patron")
    baska = await user_factory("b4@fiil.test", "Parola123!", "patron")
    yonetici = await user_factory("c4@fiil.test", "Parola123!", "system_admin")
    kimlik = await _sohbet_ac(seeded_db, sahip, "A'nın sorusu")

    yol = f"/ai/conversations/{kimlik}"
    assert (await client.delete(yol, headers=_bearer(baska))).status_code == 403
    assert (await client.delete(yol, headers=_bearer(sahip))).status_code == 403  # kendi sohbeti DE
    assert await seeded_db.get(AiConversation, kimlik) is not None
    # Sistem Yöneticisi BAŞKASININ sohbetini siler; mesajlar CASCADE ile gider
    assert (await client.delete(yol, headers=_bearer(yonetici))).status_code == 204
    seeded_db.expunge_all()
    assert await seeded_db.get(AiConversation, kimlik) is None
    assert (
        await seeded_db.scalars(select(AiMessage).where(AiMessage.conversation_id == kimlik))
    ).all() == []
    # ikinci silme: sohbet yok
    assert (await client.delete(yol, headers=_bearer(yonetici))).status_code == 404


async def test_POZITIF_KONTROL_kendi_sohbetini_OKUYABILIR(client, seeded_db, user_factory) -> None:
    """🔴 §5-19: bekçi hiçbir şeyi ölçmüyor olmasın — kapı **açılabiliyor** mu.

    Bu test olmadan yukarıdaki dört test "uç 404 döndürüyor" diye de yeşil
    kalırdı; yani kapıyı değil bir arızayı ölçerlerdi.
    """
    a = await user_factory("a5@fiil.test", "Parola123!", "patron")
    kimlik = await _sohbet_ac(seeded_db, a, "Bu ayki hakediş ne kadar?")

    liste = await client.get("/ai/conversations", headers=_bearer(a))
    assert liste.status_code == 200
    assert liste.json()["total"] == 1
    kart = liste.json()["items"][0]
    assert kart["title"] == "Bu ayki hakediş ne kadar?"
    # Mockup'ın "4 mesaj · 09:42" satırının ilk yarısı.
    assert kart["message_count"] == 1

    detay = await client.get(f"/ai/conversations/{kimlik}", headers=_bearer(a))
    assert detay.status_code == 200
    g = detay.json()
    assert [m["content"] for m in g["messages"]] == ["Bu ayki hakediş ne kadar?"]
    assert g["messages"][0]["role"] == "kullanici"
    assert g["bloklar_saklanmadi_notu"] == BLOKLAR_SAKLANMADI


async def test_MUTASYON_sahiplik_kosulu_TEK_YERDE_ve_KALDIRILAMAZ(seeded_db, user_factory) -> None:
    """🔴 §5-20: çağrı yeri de mutanttır.

    `sohbetim()` sahiplik koşulunu **taşıyor** mu: aynı kimlik, iki kullanıcı,
    iki farklı sonuç. Koşul silinirse ikisi de satırı döndürür ve bu test kırmızı
    olur.
    """
    a = await user_factory("a6@fiil.test", "Parola123!", "patron")
    b = await user_factory("b6@fiil.test", "Parola123!", "patron")
    kimlik = await _sohbet_ac(seeded_db, a, "soru")

    assert await conversations.sohbetim(seeded_db, user_id=a.id, conversation_id=kimlik) is not None
    assert await conversations.sohbetim(seeded_db, user_id=b.id, conversation_id=kimlik) is None
    assert await conversations.mesajlarim(seeded_db, user_id=b.id, conversation_id=kimlik) is None
    # `sohbet_sil` sahiplik süzgeci TAŞIMAZ (SIL-B1): okuma yolları sahiplik kapılıdır, silme değil.
    assert await conversations.sohbet_sil(seeded_db, conversation_id=kimlik) is True
    assert await conversations.sohbet_sil(seeded_db, conversation_id=kimlik) is False


# --------------------------------------------------------------------------- #
# 3 — Başlık ve saklanan alanlar
# --------------------------------------------------------------------------- #


def test_baslik_ILK_SORUDAN_turetilir_ve_KIRPILIR() -> None:
    assert conversations.baslik_uret("  Bu   ay  ne   kadar?  ") == "Bu ay ne kadar?"
    uzun = "a" * 200
    baslik = conversations.baslik_uret(uzun)
    assert len(baslik) <= conversations.BASLIK_TAVANI
    assert baslik.endswith("…")
    # `title` kolonu 120; taban bunun ALTINDA kalmalı.
    assert conversations.BASLIK_TAVANI < AiConversation.__table__.c.title.type.length


async def test_cevap_METIN_ADLAR_ve_HALLER_saklar_GOVDE_saklamaz(
    seeded_db, user_factory, monkeypatch
) -> None:
    """Saklanan **ALAN ŞEKLİ**: metin birleşir, adlar ve hâller ÇİFT kalır.

    🔴 **BU TEST KALICILIĞI ÖLÇMEZ — ölçmediği kanıtlandı (AI-SOHBET-FIX).**
    Aşağıdaki `monkeypatch` `cevabi_sakla`nın "ayrı session"ını testin KENDİ
    session'ına bağlar ve `commit`i `flush`a çevirir. Yani kusurun tek sebebi —
    *ayrı bir transaction, commit edilmemiş sohbeti göremez* — burada hiç
    gerçekleşmez ve FK asla ihlal edilmez (§5-19: bekçi ölçtüğü yolu kendisi
    kuruyorsa hiçbir şey ölçmüyordur). Üstüne `cevabi_sakla` DOĞRUDAN çağrılır,
    `POST /ai/chat`e uğranmaz (§5-20: çağrı yeri de mutanttır).

    Bu yüzden sohbet geçmişi canlıda AYLARCA hiç kaydedilmedi ve dört kapı da
    yeşil kaldı. Kalıcılığın gerçek bekçisi `test_aisohbetfix_kalicilik.py`dir;
    bu test yalnız **alan eşlemesini** korur ve o işi yapmaya devam eder.
    """
    a = await user_factory("a7@fiil.test", "Parola123!", "patron")
    kimlik = await _sohbet_ac(seeded_db, a, "soru")

    class _Sahte:
        async def __aenter__(self):
            return seeded_db

        async def __aexit__(self, *_):
            return False

    monkeypatch.setattr(conversations, "SessionLocal", lambda: _Sahte())
    monkeypatch.setattr(seeded_db, "commit", seeded_db.flush)

    await conversations.cevabi_sakla(
        conversation_id=kimlik,
        metin="Temmuz 2026 için hakediş dağılımı: ...",
        tool_names=["projeleri_listele", "gosterge_ozeti"],
        tool_states=["Ok", "Restricted"],
        finish_reason="bitti",
        duration_ms=1800,
    )
    await seeded_db.flush()
    mesajlar = (
        await seeded_db.scalars(
            select(AiMessage).where(
                AiMessage.conversation_id == kimlik, AiMessage.role == AiMessageRole.asistan
            )
        )
    ).all()
    assert len(mesajlar) == 1
    m = mesajlar[0]
    assert m.content.startswith("Temmuz 2026")
    # ADLAR ve HÂLLER — aynı sırada, aynı uzunlukta bir ÇİFT.
    assert m.tool_names == ["projeleri_listele", "gosterge_ozeti"]
    assert m.tool_states == ["Ok", "Restricted"]
    assert len(m.tool_names) == len(m.tool_states)
    # 🔴 `bitti` ile `filtrelendi` AYRI TUTULUR (§5-30).
    assert m.finish_reason == "bitti"
    assert m.duration_ms == 1800


def test_conversations_py_SessionLocal_kullanan_IKINCI_dosya_BILINCLI() -> None:
    """Bu bir bekçi değil bir **kayıt**: S15 kümesinin genişlemesi ADIYLA tanınır.

    Gerçek bekçi `test_ai0b_yapisal.py::test_S15_...`tir ve orada küme
    `{audit.py, conversations.py}` olarak yazılıdır.
    """
    import pathlib

    kaynak = (pathlib.Path(conversations.__file__)).read_text(encoding="utf-8")
    assert "async with SessionLocal() as session:" in kaynak
    assert "§5-33" in kaynak, "genişlemenin gerekçesi dosyanın içinde yazılı olmalı"


def test_baglanti_kalemi_dondurulemez() -> None:
    """Bloklar `frozen` — bir çağrı yeri onları yerinde değiştiremez."""
    from app.modules.ai.blocks import BaglantiKalemi
    from app.modules.ai.navigation import EkranAnahtari

    kalem = BaglantiKalemi(etiket="x", ekran=EkranAnahtari.stok)
    with pytest.raises(dataclasses.FrozenInstanceError):
        kalem.etiket = "y"  # type: ignore[misc]
