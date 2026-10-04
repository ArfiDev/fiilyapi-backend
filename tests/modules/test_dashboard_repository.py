import uuid

from app.modules.projects.repository import list_projects_for_user
from app.modules.users.models import ProjectMember


async def test_all_projects_user_sees_every_project(db_session, user_factory, project_factory):
    await project_factory("GK-A", name="Güneşkent A-Blok")
    await project_factory("OSB-1", name="Çelik OSB Fabrika")
    user = await user_factory(email="kapsam1@t.co", password="parola1234", role_key="patron")
    user.all_projects = True
    await db_session.flush()

    projects = await list_projects_for_user(db_session, user.id)

    assert [p.code for p in projects] == ["GK-A", "OSB-1"]


async def test_limited_user_sees_only_granted_projects(db_session, user_factory, project_factory):
    granted = await project_factory("GK-A")
    await project_factory("OSB-1")
    user = await user_factory(email="kapsam2@t.co", password="parola1234", role_key="patron")
    db_session.add(ProjectMember(user_id=user.id, project_id=granted.id, role_id=user.role_id))
    await db_session.flush()

    projects = await list_projects_for_user(db_session, user.id)

    assert [p.code for p in projects] == ["GK-A"]


async def test_user_without_access_rows_sees_nothing(db_session, project_factory):
    await project_factory("GK-A")

    projects = await list_projects_for_user(db_session, uuid.uuid4())

    assert projects == []
