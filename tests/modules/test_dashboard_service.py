from app.modules.dashboard.service import build_summary


async def test_summary_counts_only_active_projects(seeded_db, user_factory, project_factory):
    await project_factory("GK-A", status="active")
    await project_factory("OSB-1", status="on_hold")
    await project_factory("SAHIL-2", status="completed")
    user = await user_factory(email="patron@t.co", password="parola1234", role_key="patron")
    user.all_projects = True
    await seeded_db.flush()

    summary = await build_summary(seeded_db, user)

    assert len(summary.projects) == 3
    assert summary.active_project_count == 1


async def test_summary_uses_role_display_name(seeded_db, user_factory):
    user = await user_factory(email="patron2@t.co", password="parola1234", role_key="patron")

    summary = await build_summary(seeded_db, user)

    assert summary.role_name == "Patron"


async def test_summary_placeholders_are_unavailable(seeded_db, user_factory):
    """P-YT2 denetimi: BEŞ karttan İKİSİ hâlâ yer tutucudur, ÜÇÜ BAĞLANDI.

    🔴 `pending_approvals`, (DASH-1 ile) `portfolio` ve (RISK-1 ile) `risks` bu
    kümeden bilerek ÇIKARILDI. Kalan ikisinin gerekçeleri
    `dashboard/service.py`de kartların yanındadır; `average_margin`ınki
    DASH-1'de üç ölçülmüş engelle büyüdü.

    Anahtar STRINGLERİ değişmedi: yanıt gövdesindedir ve frontend onlara
    dallanabilir.
    """
    user = await user_factory(email="patron3@t.co", password="parola1234", role_key="patron")

    summary = await build_summary(seeded_db, user)

    assert summary.receivables.pending_module == "invoicing"
    assert summary.average_margin.pending_module == "progress_payments"
    assert summary.pending_approvals.pending_module == "approvals"
    assert not any(
        card.available
        for card in (
            summary.receivables,
            summary.average_margin,
        )
    ), "iki kart hâlâ (C) TUZAK — bağlanmaları ürün/izin kararı bekliyor"
    # 🔴 RISK-1 BAĞLANDI: `patron` üç kaynağın üçünü de okur, yani kart
    # `available=true`dur ve kaynak listesi ÜÇ satırdır — görünür projesi
    # olmayan bu aktörde uyarı satırı çıkmasa bile. Ayrıntılı bekçiler
    # `test_risk1_uyari_akisi.py`dedir.
    assert summary.risks.available is True
    assert [source.state.value for source in summary.risks.sources] == ["ok", "ok", "ok"]
    assert summary.pending_approvals.available is True, (
        "onay rozeti BAĞLANDI: canlı motorun verdiği sıfır 'bilinmiyor' değildir (K2)"
    )
    assert summary.pending_approvals.count == 0
    # 🔴 PORTFOY BAĞLANDI ama bu aktörün GÖRÜNÜR PROJESİ YOK: boş zarf, kaynağını
    # bildirir (2. hâl). Uydurma bir `0.00` DEĞİL — ayrıntılı beş bekçi
    # `test_dash1_portfolio.py`dedir.
    assert summary.portfolio.available is False
    assert summary.portfolio.pending_module == "progress_payments"


async def test_summary_empty_when_no_project_access(seeded_db, user_factory, project_factory):
    await project_factory("GK-A")
    user = await user_factory(email="patron4@t.co", password="parola1234", role_key="patron")

    summary = await build_summary(seeded_db, user)

    assert summary.projects == []
    assert summary.active_project_count == 0
