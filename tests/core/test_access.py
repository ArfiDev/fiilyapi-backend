from app.core.access import AccessLevel, satisfies


def test_access_exports_pure_domain_without_fastapi():
    # access.py FastAPI/DB'ye bağımlı OLMAMALI — saf domain.
    import app.core.access as access_module

    with open(access_module.__file__, encoding="utf-8") as handle:
        text = handle.read()
    assert "fastapi" not in text.lower()
    assert "get_db" not in text


def test_level_ordering():
    assert satisfies(AccessLevel.admin, AccessLevel.full)
    assert satisfies(AccessLevel.approve, AccessLevel.approve)
    assert not satisfies(AccessLevel.view, AccessLevel.draft)


def test_scope_sokuldu():
    """IZN-B6b: veri kapsamı (`Scope`, `DROPPED_SCOPES`) uygulamadan söküldü."""
    import app.core.access as access_module

    assert not hasattr(access_module, "Scope")
    assert not hasattr(access_module, "DROPPED_SCOPES")


def test_can_delete_soekuldu_silme_yalniz_sistem_yoneticisi():
    """SIL-B1 (K4: istisna YOK): `can_delete` ve taslak istisnası kaldırıldı."""
    import app.core.access as access_module

    assert not hasattr(access_module, "can_delete")
    assert not hasattr(access_module, "Deletable")


def test_permissions_module_has_no_function_level_domain_imports():
    # Döngü yapısal kırıldıysa require_permission gövdesinde artık girintili
    # (fonksiyon içi) "from app.modules.roles.repository import" OLMAMALI.
    import app.core.permissions as perm_module

    with open(perm_module.__file__, encoding="utf-8") as handle:
        body = handle.read()
    assert body.count("from app.core.deps import get_current_user") == 1  # modül seviyesinde, 1 kez
    assert "\n        from app.modules.roles.repository import get_permission" not in body
