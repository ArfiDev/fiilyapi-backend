"""Bütçe uçları — revizyon yaşam döngüsü, yazmalar, katalog, önizleme, dondurma, fark, izin.

Fikstür: `conftest.py` docstring'i. Beklenen bütçeler elle:
  I1 oran 2 → S1 60×2=120 · S2 30×2=60 · Bölümsüz 10×2=20 → 200
  I2 oran 0,5 → S1 50×0,5=25
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from app.modules.audit.models import AuditLog
from app.modules.earned_value.models import (
    EvBaselineCurve,
    EvBaselineLeaf,
    EvRevision,
    RevisionStatus,
)

D = Decimal


def _url(site, tail: str = "") -> str:  # noqa: ANN001
    return f"/sites/{site.id}/earned-value/budget{tail}"


def _leaf_id(item, section) -> str:  # noqa: ANN001
    return f"l:{item.id}:{section.id if section else 'none'}"


def _leaves(view: dict) -> dict[str, dict]:
    return {
        lf["id"]: lf
        for d in view["disciplines"]
        for g in d["groups"]
        for i in g["items"]
        for lf in i["leaves"]
    }


async def _map(client, site, headers, boq, disciplines, *, g2: bool = True) -> dict:  # noqa: ANN001
    kab, duv = disciplines
    items = [{"boq_group_id": str(boq["g1"].id), "discipline_id": str(kab.id)}]
    if g2:
        items.append({"boq_group_id": str(boq["g2"].id), "discipline_id": str(duv.id)})
    resp = await client.put(
        _url(site, "/group-disciplines"), headers=headers, json={"items": items}
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _rates(client, site, headers, boq) -> dict:  # noqa: ANN001
    i1, i2, s1, s2 = boq["i1"], boq["i2"], boq["s1"], boq["s2"]
    body = {
        "leaves": [
            {"boq_item_id": str(i1.id), "section_id": str(s1.id), "unit_mhr": "2"},
            {"boq_item_id": str(i1.id), "section_id": str(s2.id), "unit_mhr": "2"},
            {"boq_item_id": str(i1.id), "section_id": None, "unit_mhr": "2"},
            {"boq_item_id": str(i2.id), "section_id": str(s1.id), "unit_mhr": "0.5"},
        ]
    }
    resp = await client.patch(_url(site, "/leaves"), headers=headers, json=body)
    assert resp.status_code == 200, resp.text
    return resp.json()


# ------------------------------------------------------------------ ilk durum


async def test_empty_site_returns_virtual_draft_without_revision(
    client, admin, santiye, boq, disiplinler
) -> None:
    resp = await client.get(_url(santiye), headers=admin)
    assert resp.status_code == 200, resp.text
    view = resp.json()
    assert view["revision"] is None and view["editable"] is True
    assert [d["id"] for d in view["disciplines"]] == ["d:none"]  # henüz eşleme yok
    leaves = _leaves(view)
    assert leaves[_leaf_id(boq["i1"], None)]["planned_qty"] == "10.000"  # 100 − 90
    assert view["freeze_blockers"] == []  # bütçe yok → disiplinsiz grup yalnız uyarı
    codes = {w["code"] for w in view["freeze_warnings"]}
    assert codes == {"empty_rate", "disciplineless_group_without_budget"}


async def test_first_write_opens_rev0_draft_and_audits(
    client, admin, seeded_db, santiye, boq, disiplinler
) -> None:
    view = await _map(client, santiye, admin, boq, disiplinler)
    assert view["revision"]["number"] == 0
    assert view["revision"]["status"] == "draft"
    assert [d["code"] for d in view["disciplines"]] == ["KAB", "DUV"]
    assert view["disciplines"][0]["color"] == "#2563eb"
    count = await seeded_db.scalar(
        select(func.count()).select_from(AuditLog).where(AuditLog.detail.like("BOQ grubu%"))
    )
    assert count == 1


# ------------------------------------------------------------------ oranlar ve miras


async def test_rates_budget_share_and_default_source(
    client, admin, santiye, boq, disiplinler
) -> None:
    await _map(client, santiye, admin, boq, disiplinler)
    view = await _rates(client, santiye, admin, boq)
    lv = _leaves(view)
    s1_leaf = lv[_leaf_id(boq["i1"], boq["s1"])]
    assert (D(s1_leaf["budget_mhr"]), s1_leaf["rate_source"]) == (D(120), "manual")
    assert D(view["totals"]["direct_budget_mhr"]) == 225
    kab = view["disciplines"][0]
    assert D(kab["direct_budget_mhr"]) == 200
    assert D(kab["share"]) == D(200) / D(225)
    assert view["totals"]["empty_rate_leaf_count"] == 0


async def test_rate_null_clears_rate_and_source(client, admin, santiye, boq, disiplinler) -> None:
    await _map(client, santiye, admin, boq, disiplinler)
    await _rates(client, santiye, admin, boq)
    body = {
        "leaves": [
            {"boq_item_id": str(boq["i2"].id), "section_id": str(boq["s1"].id), "unit_mhr": None}
        ]
    }
    view = (await client.patch(_url(santiye, "/leaves"), headers=admin, json=body)).json()
    leaf = _leaves(view)[_leaf_id(boq["i2"], boq["s1"])]
    assert (leaf["unit_mhr"], leaf["rate_source"], leaf["budget_mhr"]) == (None, None, "0")


async def test_leaf_outside_live_tree_is_422(client, admin, santiye, boq, disiplinler) -> None:
    # I2'nin S2 tahsisi YOK ve kalanı da yok (50 = S1 50) → yaprak yok
    body = {
        "leaves": [
            {"boq_item_id": str(boq["i2"].id), "section_id": str(boq["s2"].id), "unit_mhr": "1"}
        ]
    }
    resp = await client.patch(_url(santiye, "/leaves"), headers=admin, json=body)
    assert resp.status_code == 422, resp.text


async def test_contractor_inheritance_item_and_leaf_override(
    client, admin, santiye, boq, disiplinler
) -> None:
    await _map(client, santiye, admin, boq, disiplinler)
    view = (await client.get(_url(santiye), headers=admin)).json()
    duv_item = view["disciplines"][1]["groups"][0]["items"][0]
    assert (duv_item["contractor_type"], duv_item["contractor_source"]) == ("subcon", "inherited")
    resp = await client.patch(
        _url(santiye, f"/items/{boq['i2'].id}"), headers=admin, json={"contractor_type": "own"}
    )
    item = resp.json()["disciplines"][1]["groups"][0]["items"][0]
    assert (item["contractor_type"], item["contractor_source"]) == ("own", "item")
    body = {
        "leaves": [
            {
                "boq_item_id": str(boq["i2"].id),
                "section_id": str(boq["s1"].id),
                "contractor_type": "subcon",
            }
        ]
    }
    view = (await client.patch(_url(santiye, "/leaves"), headers=admin, json=body)).json()
    leaf = _leaves(view)[_leaf_id(boq["i2"], boq["s1"])]
    assert (leaf["contractor_type"], leaf["contractor_source"]) == ("subcon", "override")
    back = await client.patch(
        _url(santiye, f"/items/{boq['i2'].id}"), headers=admin, json={"contractor_type": None}
    )
    item = back.json()["disciplines"][1]["groups"][0]["items"][0]
    assert item["contractor_source"] == "inherited"  # null = mirasa dön


async def test_item_patch_explicit_null_is_direct_is_422(client, admin, santiye, boq) -> None:
    resp = await client.patch(
        _url(santiye, f"/items/{boq['i1'].id}"), headers=admin, json={"is_direct": None}
    )
    assert resp.status_code == 422
    assert "is_direct" in resp.text


# ------------------------------------------------------------------ katalog


async def test_fill_from_catalog_exact_unique_and_ambiguous(
    client, admin, santiye, boq, disiplinler, katalog
) -> None:
    # G2 disiplinsiz: I2 disiplin süzgecinden geçmez (KATALOG-UQ: aynı disiplinde iki
    # normalize-eş kalem artık DB'de duramaz, belirsizlik yalnız disiplinler arası doğar).
    await _map(client, santiye, admin, boq, disiplinler, g2=False)
    resp = await client.post(_url(santiye, "/fill-from-catalog"), headers=admin)
    assert resp.status_code == 200, resp.text
    out = resp.json()
    # I1 "Beton" m3 ↔ katalog "Beton" m³ (üst simge normalize) TEK tam eşleşme → 3 yaprak
    assert (out["filled_item_count"], out["filled_leaf_count"]) == (1, 3)
    # I2 "Tuğla" ↔ DUV "Tuğla" + KAB "TUĞLA" (Türkçe büyük harf normalize) → belirsiz
    assert out["ambiguous_count"] == 1 and out["unmatched_count"] == 0
    assert {c["name"] for c in out["ambiguous"][0]["candidates"]} == {"Tuğla", "TUĞLA"}
    view = (await client.get(_url(santiye), headers=admin)).json()
    leaf = _leaves(view)[_leaf_id(boq["i1"], boq["s1"])]
    assert (D(leaf["unit_mhr"]), leaf["rate_source"]) == (D("1.80"), "catalog")
    item = view["disciplines"][0]["groups"][0]["items"][0]
    assert item["catalog_item_id"] == str(katalog["beton"].id)  # bağ kaydedildi


async def test_suggestions_rank_linked_exact(
    client, admin, santiye, boq, disiplinler, katalog
) -> None:
    await _map(client, santiye, admin, boq, disiplinler, g2=False)  # iki disiplinden tam eş
    resp = await client.get(_url(santiye, f"/items/{boq['i2'].id}/suggestions"), headers=admin)
    assert resp.status_code == 200
    out = resp.json()
    assert [c["match"] for c in out["catalog"]] == ["exact", "exact"]
    assert out["history"] == []


# ------------------------------------------------------------------ zamanlama + önizleme


async def test_windows_replace_semantics_and_schedule(
    client, admin, santiye, boq, disiplinler
) -> None:
    kab, _ = disiplinler
    await _map(client, santiye, admin, boq, disiplinler)
    body = {
        "windows": [
            {
                "discipline_id": str(kab.id),
                "section_id": str(boq["s2"].id),
                "start_date": "2026-05-20",
                "end_date": "2026-05-27",
            }
        ]
    }
    view = (await client.put(_url(santiye, "/windows"), headers=admin, json=body)).json()
    leaf = _leaves(view)[_leaf_id(boq["i1"], boq["s2"])]
    assert (leaf["window_start"], leaf["window_source"]) == ("2026-05-20", "override")
    sched = (await client.get(_url(santiye, "/schedule"), headers=admin)).json()
    assert {s["name"] for s in sched["sections"]} == {"A Blok", "B Blok"}
    assert sched["weekly_off_days"] == [6]
    view = (await client.put(_url(santiye, "/windows"), headers=admin, json={"windows": []})).json()
    leaf = _leaves(view)[_leaf_id(boq["i1"], boq["s2"])]
    assert (leaf["window_start"], leaf["window_source"]) == ("2026-05-18", "section")


async def test_preview_is_not_persistent_and_sums_to_budget(
    client, admin, santiye, boq, disiplinler
) -> None:
    kab, _ = disiplinler
    await _map(client, santiye, admin, boq, disiplinler)
    await _rates(client, santiye, admin, boq)
    body = {"distributions": [{"discipline_id": str(kab.id), "distribution": "bell"}]}
    resp = await client.post(_url(santiye, "/preview"), headers=admin, json=body)
    assert resp.status_code == 200, resp.text
    out = resp.json()
    assert D(out["total"]["budget_mhr"]) == 225
    assert sum(D(d["mhr"]) for d in out["total"]["days"]) == 225
    kab_out = next(d for d in out["disciplines"] if d["code"] == "KAB")
    assert kab_out["distribution"] == "bell"
    assert out["total"]["peak_week"] is not None
    # K10: gereken kişi = hafta a-s ÷ (iş günü × 9)
    w = out["total"]["weeks"][0]
    assert D(w["required_people"]) == D(w["mhr"]) / (w["working_days"] * D(9))
    assert w["planned_people"] == 12  # hafta ortası S1 aktif
    assert out["standard_daily_hours"] == "9.00"  # CEO B3 eki (K10 lejantı)
    view = (await client.get(_url(santiye), headers=admin)).json()
    assert view["disciplines"][0]["distribution"] == "linear"  # KAYDEDİLMEDİ


# ------------------------------------------------------------------ dondurma + revizyon


async def test_freeze_blocked_by_disciplineless_group_with_budget(
    client, admin, santiye, boq, disiplinler
) -> None:
    await _rates(client, santiye, admin, boq)  # eşleme yok, bütçe var
    resp = await client.post(_url(santiye, "/freeze"), headers=admin, json={})
    assert resp.status_code == 422
    assert "disciplineless_group" in resp.json()["detail"]


async def test_freeze_snapshot_curve_sums_exactly_to_leaf_budget(
    client, admin, seeded_db, santiye, boq, disiplinler
) -> None:
    await _map(client, santiye, admin, boq, disiplinler)
    await _rates(client, santiye, admin, boq)
    resp = await client.post(
        _url(santiye, "/freeze"), headers=admin, json={"name": "Baseline", "description": "ilk"}
    )
    assert resp.status_code == 200, resp.text
    rev = resp.json()
    assert (rev["number"], rev["status"], rev["name"]) == (0, "active", "Baseline")
    assert rev["frozen_by"]["full_name"] and rev["frozen_at"]
    leaves = (
        (
            await seeded_db.execute(
                select(EvBaselineLeaf).where(EvBaselineLeaf.revision_id == rev["id"])
            )
        )
        .scalars()
        .all()
    )
    assert len(leaves) == 4
    for lf in leaves:
        total = await seeded_db.scalar(
            select(func.sum(EvBaselineCurve.mhr)).where(EvBaselineCurve.leaf_id == lf.id)
        )
        assert total == lf.budget_mhr, (lf.item_code, lf.section_name)  # DB'den == (K8)
    sundays = await seeded_db.scalar(
        select(func.count())
        .select_from(EvBaselineCurve)
        .where(func.extract("isodow", EvBaselineCurve.day) == 7)
    )
    assert sundays == 0  # tatil = 0


async def test_after_freeze_writes_need_new_draft_and_diff(
    client, admin, santiye, boq, disiplinler
) -> None:
    await _map(client, santiye, admin, boq, disiplinler)
    await _rates(client, santiye, admin, boq)
    assert (await client.post(_url(santiye, "/freeze"), headers=admin, json={})).status_code == 200
    body = {
        "leaves": [
            {"boq_item_id": str(boq["i2"].id), "section_id": str(boq["s1"].id), "unit_mhr": "0.6"}
        ]
    }
    assert (
        await client.patch(_url(santiye, "/leaves"), headers=admin, json=body)
    ).status_code == 409
    opened = await client.post(_url(santiye, "/revisions"), headers=admin)
    assert opened.status_code == 201 and opened.json()["number"] == 1
    assert (await client.post(_url(santiye, "/revisions"), headers=admin)).status_code == 409
    view = (await client.patch(_url(santiye, "/leaves"), headers=admin, json=body)).json()
    assert view["disciplines"][0]["code"] == "KAB"  # eşleme taslağa kopyalandı
    diff = (
        await client.get(_url(santiye, f"/revisions/{opened.json()['id']}/diff"), headers=admin)
    ).json()
    assert diff["against"]["number"] == 0
    assert [(x["reason"], x["prev_unit_mhr"], x["unit_mhr"]) for x in diff["leaves"]] == [
        ("rate_changed", "0.5000", "0.6000")
    ]
    assert D(diff["direct_delta_mhr"]) == D(5)  # 50 × 0,1
    assert (await client.post(_url(santiye, "/freeze"), headers=admin, json={})).status_code == 200
    revs = (await client.get(_url(santiye, "/revisions"), headers=admin)).json()
    assert [(r["number"], r["status"]) for r in revs] == [(0, "archived"), (1, "active")]


async def test_frozen_revision_shows_snapshot_not_live_boq(
    client, admin, seeded_db, santiye, boq, disiplinler
) -> None:
    await _map(client, santiye, admin, boq, disiplinler)
    await _rates(client, santiye, admin, boq)
    rev = (await client.post(_url(santiye, "/freeze"), headers=admin, json={})).json()
    boq["i1"].quantity = D(140)  # BOQ sonradan büyüdü → canlı Bölümsüz 50 olurdu
    await seeded_db.flush()
    view = (
        await client.get(_url(santiye), headers=admin, params={"revision_id": rev["id"]})
    ).json()
    assert view["editable"] is False
    leaf = _leaves(view)[_leaf_id(boq["i1"], None)]
    assert (leaf["planned_qty"], leaf["window_source"]) == ("10.000", "snapshot")
    pv = await client.post(
        _url(santiye, "/preview"), headers=admin, json={"revision_id": rev["id"]}
    )
    assert pv.status_code == 200, pv.text
    assert D(pv.json()["total"]["budget_mhr"]) == 225


# ------------------------------------------------------------------ izin + kapsam


async def test_permission_matrix(
    client, sef, saha, muhasebe, ik, santiye, boq, disiplinler
) -> None:
    assert (await client.get(_url(santiye), headers=muhasebe)).status_code == 200
    assert (await client.get(_url(santiye), headers=ik)).status_code == 403
    body = {"leaves": [{"boq_item_id": str(boq["i1"].id), "section_id": None, "unit_mhr": "1"}]}
    assert (
        await client.patch(_url(santiye, "/leaves"), headers=muhasebe, json=body)
    ).status_code == 403
    assert (
        await client.patch(_url(santiye, "/leaves"), headers=saha, json=body)
    ).status_code == 200
    # saha mühendisi (draft) donduramaz; şef (approve) dondurur — BİLİNÇLİ (§3.9 B1-8)
    await _map(client, santiye, saha, boq, disiplinler)
    assert (await client.post(_url(santiye, "/freeze"), headers=saha, json={})).status_code == 403
    assert (await client.post(_url(santiye, "/freeze"), headers=sef, json={})).status_code == 200


@pytest.mark.xfail(
    strict=True,
    reason="IZN-B2: DELETE bütçe taslağı yalnız Sistem Yöneticisi (SIL hattında)",
)
async def test_delete_draft_requires_approve_and_only_draft(
    client, sef, saha, seeded_db, santiye, boq, disiplinler
) -> None:
    view = await _map(client, santiye, saha, boq, disiplinler)
    rev_id = view["revision"]["id"]
    assert (
        await client.delete(_url(santiye, f"/revisions/{rev_id}"), headers=saha)
    ).status_code == 403
    assert (
        await client.delete(_url(santiye, f"/revisions/{rev_id}"), headers=sef)
    ).status_code == 204
    assert await seeded_db.get(EvRevision, rev_id) is None


async def test_invisible_site_is_404(client, sef, gorunmeyen_santiye) -> None:
    assert (await client.get(_url(gorunmeyen_santiye), headers=sef)).status_code == 404
    resp = await client.post(_url(gorunmeyen_santiye, "/freeze"), headers=sef, json={})
    assert resp.status_code == 404


async def test_one_draft_per_site_is_enforced_by_db(seeded_db, santiye) -> None:
    seeded_db.add(EvRevision(site_id=santiye.id, number=0, status=RevisionStatus.DRAFT))
    await seeded_db.flush()
    seeded_db.add(EvRevision(site_id=santiye.id, number=1, status=RevisionStatus.DRAFT))
    import pytest
    from sqlalchemy.exc import IntegrityError

    with pytest.raises(IntegrityError):
        await seeded_db.flush()
    await seeded_db.rollback()


def test_week_day_constants() -> None:
    assert date(2026, 5, 17).isoweekday() == 7  # fikstür takvimi: 17.05.2026 Pazar


def test_normalize_label_handles_turkish_dotted_and_dotless_i() -> None:
    """🔴 `"İ".lower()` "i̇" (i + birleşik nokta) verir; `"I".lower()` "i" verir, "ı" değil."""
    from app.modules.earned_value.budget_ops import normalize_label

    assert normalize_label("KİREÇ SIVA") == normalize_label("kireç sıva")
    assert normalize_label("IŞIK") == normalize_label("ışık")
    assert normalize_label("m³") == normalize_label("M3")
    assert normalize_label("  Beton   döküm ") == "beton döküm"


async def test_preview_counts_indirect_budget_outside_the_curve(
    client, admin, santiye, boq, disiplinler
) -> None:
    await _map(client, santiye, admin, boq, disiplinler)
    await _rates(client, santiye, admin, boq)
    resp = await client.patch(
        _url(santiye, f"/items/{boq['i2'].id}"), headers=admin, json={"is_direct": False}
    )
    assert resp.status_code == 200
    assert D(resp.json()["totals"]["indirect_budget_mhr"]) == 25
    out = (await client.post(_url(santiye, "/preview"), headers=admin, json={})).json()
    assert D(out["indirect_budget_mhr"]) == 25  # S3: eğriye girmez, ayrı döner
    assert D(out["total"]["budget_mhr"]) == 200


async def test_F0_4_override_outside_section_dates_is_flagged_not_blocking(
    client, admin, santiye, boq, disiplinler
) -> None:
    kab, _ = disiplinler
    await _map(client, santiye, admin, boq, disiplinler)
    await _rates(client, santiye, admin, boq)
    body = {
        "windows": [
            {
                "discipline_id": str(kab.id),
                "section_id": str(boq["s1"].id),
                "start_date": "2026-05-01",  # S1 04.05'te başlar → taşıyor
                "end_date": "2026-05-15",
            }
        ]
    }
    view = (await client.put(_url(santiye, "/windows"), headers=admin, json=body)).json()
    lv = _leaves(view)
    assert lv[_leaf_id(boq["i1"], boq["s1"])]["outside_section_dates"] is True
    assert lv[_leaf_id(boq["i1"], boq["s2"])]["outside_section_dates"] is False
    assert view["freeze_blockers"] == []  # engel DEĞİL
    sched = (await client.get(_url(santiye, "/schedule"), headers=admin)).json()
    flagged = [b for b in sched["bars"] if b["outside_section_dates"]]
    assert [b["section_id"] for b in flagged] == [str(boq["s1"].id)]


async def test_F0_2_disciplineless_group_with_only_indirect_items_can_freeze(
    client, admin, seeded_db, santiye, boq, disiplinler
) -> None:
    kab, _ = disiplinler
    resp = await client.put(
        _url(santiye, "/group-disciplines"),
        headers=admin,
        json={"items": [{"boq_group_id": str(boq["g1"].id), "discipline_id": str(kab.id)}]},
    )
    assert resp.status_code == 200
    await _rates(client, santiye, admin, boq)
    # G2 disiplinsiz; I2 bütçeli ama DOLAYLI → engel değil (F0-2)
    await client.patch(
        _url(santiye, f"/items/{boq['i2'].id}"), headers=admin, json={"is_direct": False}
    )
    view = (await client.get(_url(santiye), headers=admin)).json()
    assert view["freeze_blockers"] == []
    resp = await client.post(_url(santiye, "/freeze"), headers=admin, json={})
    assert resp.status_code == 200, resp.text
    rows = (
        (
            await seeded_db.execute(
                select(EvBaselineLeaf).where(EvBaselineLeaf.boq_item_id == boq["i2"].id)
            )
        )
        .scalars()
        .all()
    )
    assert [(r.discipline_id, r.is_direct) for r in rows] == [(None, False)]


async def test_F0_5_deleted_draft_number_is_reused(
    client, admin, sef, santiye, boq, disiplinler
) -> None:
    await _map(client, santiye, admin, boq, disiplinler)
    await _rates(client, santiye, admin, boq)
    assert (await client.post(_url(santiye, "/freeze"), headers=admin, json={})).status_code == 200
    rev1 = (await client.post(_url(santiye, "/revisions"), headers=admin)).json()
    assert rev1["number"] == 1
    assert (
        await client.delete(_url(santiye, f"/revisions/{rev1['id']}"), headers=admin)
    ).status_code == 204
    again = (await client.post(_url(santiye, "/revisions"), headers=admin)).json()
    assert again["number"] == 1


async def test_B3_ek_group_code_section_code_and_no_draft_blocker(
    client, admin, seeded_db, santiye, boq, disiplinler
) -> None:
    boq["s1"].code = "A"
    await seeded_db.flush()
    view = await _map(client, santiye, admin, boq, disiplinler)
    assert [g["code"] for d in view["disciplines"] for g in d["groups"]] == ["1", "2"]
    assert _leaves(view)[_leaf_id(boq["i1"], boq["s1"])]["section_code"] == "A"
    assert _leaves(view)[_leaf_id(boq["i1"], None)]["section_code"] is None
    await _rates(client, santiye, admin, boq)
    assert (await client.post(_url(santiye, "/freeze"), headers=admin, json={})).status_code == 200
    frozen = (await client.get(_url(santiye), headers=admin)).json()
    assert frozen["editable"] is False
    assert [b["code"] for b in frozen["freeze_blockers"]] == ["no_draft"]
