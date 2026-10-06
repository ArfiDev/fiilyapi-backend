from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status

from app.core.access import is_system_admin
from app.core.config import settings
from app.core.db import DbSession
from app.core.deps import get_current_user
from app.core.mask_route import MaskeRotasi
from app.core.ratelimit import client_ip, limiter
from app.core.sayfalar import SAYFA_BY_KEY, sistem_yoneticisi_sayfalari
from app.core.security import TokenError, create_access_token, create_refresh_token, decode_token
from app.modules.audit import messages
from app.modules.audit.models import AuditAction
from app.modules.audit.service import record_audit
from app.modules.auth.schemas import LoginRequest, MeResponse, PageGrant, RefreshRequest, TokenPair
from app.modules.auth.service import AuthError, authenticate
from app.modules.auth.team import load_team, team_projects, team_role_pages
from app.modules.pages.grants import grants_from_cells
from app.modules.roles.repository import (
    list_role_hidden_categories,
    list_role_page_cells,
)
from app.modules.users.models import User, UserStatus

router = APIRouter(route_class=MaskeRotasi, prefix="/auth", tags=["auth"])


@router.post("/login", response_model=TokenPair)
@limiter.limit(settings.login_rate_limit)
async def login(
    request: Request,
    payload: LoginRequest,
    session: DbSession,
) -> TokenPair:
    try:
        user = await authenticate(session, payload.email, payload.password)
    except AuthError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Kimlik bilgileri hatalı"
        ) from exc

    # Yalnizca basarili giris denetime yazilir; basarisiz denemeler kapsam disidir
    # (plan §Kapsam disi) — hiz siniri zaten kotuye kullanimi frenliyor.
    await record_audit(
        session,
        action=AuditAction.login,
        detail=messages.LOGIN_DETAIL,
        actor_user_id=user.id,
        ip_address=client_ip(request),
    )

    return TokenPair(
        access_token=create_access_token(user.id, user.token_version),
        refresh_token=create_refresh_token(user.id, user.token_version),
    )


@router.post("/refresh", response_model=TokenPair)
@limiter.limit(settings.refresh_rate_limit)
async def refresh(
    request: Request,
    payload: RefreshRequest,
    session: DbSession,
) -> TokenPair:
    try:
        decoded = decode_token(payload.refresh_token, expected_type="refresh")
    except TokenError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Oturum süresi dolmuş"
        ) from exc

    # Kullanıcıyı yeniden yükleyip durumunu kontrol etmeden yeni token basmak,
    # pasife alınmış bir kullanıcının eski refresh token'ıyla 30 gün boyunca
    # yeni access token üretmeye devam etmesine izin verir. get_current_user ile
    # aynı kuralı burada da uyguluyoruz. Kullanıcı yok ile pasif arasında fark
    # göstermemek için ikisinde de aynı yanıtı dönüyoruz.
    user = await session.get(User, decoded.user_id)
    if (
        user is None
        or user.status is not UserStatus.active
        or user.token_version != decoded.token_version
    ):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Oturum süresi dolmuş")

    # 🔴 REFRESH TOKEN BURADA YENILENMEZ (auth-refresh-rotasyon, kayit 25).
    # Onceden bu satir `create_refresh_token(...)` cagirip O ANDAN itibaren 30 gunluk
    # YENI bir refresh token basiyordu; eskisi de gecersizlesmedigi icin (rotasyon,
    # jti, kara liste yok) oturumun MUTLAK omru her cagrida sifirlaniyordu. Sonuc:
    # calinmis bir refresh token 30 gun degil SINIRSIZ yasiyordu — saldirgan her
    # 29 gunde bir /auth/refresh cagirdikca saat basa donuyordu.
    # Sunulan token'i aynen geri vererek 30 gunluk tavani GERCEKTEN tavan yapiyoruz.
    # Gercek rotasyon (jti + reuse tespiti) migration ister, ayri dilimde yapilacak;
    # BFF zaten donen refresh token'i saklamiyor (frontend/src/lib/auth/backend.ts:145),
    # bu yuzden mesru kullanicinin davranisi degismez.
    return TokenPair(
        access_token=create_access_token(user.id, user.token_version),
        refresh_token=payload.refresh_token,
    )


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
) -> None:
    """token_version'ı artırır — o ana dek basılmış tüm token'lar (access + refresh) geçersiz
    olur (gerçek sunucu-taraflı çıkış). BFF ayrıca httpOnly cookie'yi siler."""
    user.token_version += 1
    await session.flush()
    return None


@router.get("/me", response_model=MeResponse)
async def me(
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
) -> MeResponse:
    """Oturum sahibi: kimlik + sayfa hücreleri (`pages`) + gizli alanlar + proje ekibi.
    Kapılar `pages` hücrelerinden karar verir (IZN-B6b: eski `permissions` haritası kalktı)."""
    admin = is_system_admin(user)
    if admin:
        pages = {
            key: PageGrant(level=level, approve=approve)
            for key, (level, approve) in sistem_yoneticisi_sayfalari().items()
        }
        hidden_fields = []
    else:
        # Katalogda olmayan (kaldırılmış) anahtar yanıta GİRMEZ: şema `page_key` enum'udur.
        rows = [
            cell
            for cell in await list_role_page_cells(session, user.role_id)
            if cell.page_key in SAYFA_BY_KEY
        ]
        # Hücresiz sayfa katalogdan `none` ile dolar (IZN-B6a-me).
        pages = grants_from_cells(rows)
        hidden_fields = await list_role_hidden_categories(session, user.role_id)
    # "Tüm projeler" ve Sistem Yöneticisi kişide ekip satırı YOK SAYILIR: ana rolle çalışır.
    team = [] if (user.all_projects or admin) else await load_team(session, user.id)
    return MeResponse(
        id=user.id,
        email=user.email,
        full_name=user.full_name,
        title=user.title,
        role_key=user.role.key,
        is_system_admin=admin,
        status=user.status,
        pages=pages,
        hidden_fields=hidden_fields,
        all_projects=user.all_projects,
        projects=await team_projects(session, team),
        role_pages=await team_role_pages(session, team, user.role.key),
    )
