"""IZN-B3 — proje ekibi modeli (`project_members`, `users.all_projects`) — ham model davranışı.

Eski `user_project_access` tablosu DONDURULDU (B6'da düşer); erişimin tek yazma yeri yenidir.
"""

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.modules.users.models import ProjectMember


async def test_ekip_satiri_projeye_ve_role_baglanir(db_session, user_factory, project_factory):
    user = await user_factory(email="u@t.co", password="parola1234", role_key="site_chief")
    project = await project_factory("GK-A")
    db_session.add(ProjectMember(user_id=user.id, project_id=project.id, role_id=user.role_id))
    await db_session.flush()

    rows = (
        (await db_session.execute(select(ProjectMember).where(ProjectMember.user_id == user.id)))
        .scalars()
        .all()
    )
    assert len(rows) == 1
    assert (rows[0].project_id, rows[0].role_id) == (project.id, user.role_id)


async def test_ayni_kisi_ayni_projeye_iki_kez_yazilamaz(db_session, user_factory, project_factory):
    user = await user_factory(email="d@t.co", password="parola1234", role_key="site_chief")
    project = await project_factory("GK-B")
    db_session.add(ProjectMember(user_id=user.id, project_id=project.id, role_id=user.role_id))
    await db_session.flush()
    async with db_session.begin_nested():
        db_session.add(ProjectMember(user_id=user.id, project_id=project.id, role_id=user.role_id))
        with pytest.raises(IntegrityError):
            await db_session.flush()


async def test_tum_projeler_bayragi_kullanici_kolonudur(db_session, user_factory):
    user = await user_factory(email="a@t.co", password="parola1234", role_key="patron")
    assert user.all_projects is False
    user.all_projects = True
    await db_session.flush()
    await db_session.refresh(user)
    assert user.all_projects is True
