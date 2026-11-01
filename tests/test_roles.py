from datetime import date, timedelta

import pytest

from catalog import changelog
from catalog import properties as props
from catalog import repository as repo
from catalog import roles
from catalog.db import connect
from catalog.ingest import ColumnSchema, FileSchema

S = FileSchema("csv", 1, [ColumnSchema("x", "int64")])
TODAY = date(2026, 11, 1)


def owner(**kw):
    base = dict(role_type="owner", org="営業部", position="営業部長", scopes=roles.OWNER_SCOPES, last_confirmed=TODAY)
    return {**base, **kw}


def steward(kind="steward_business", **kw):
    base = dict(role_type=kind, org="営業管理課", person="佐藤", contact="sato@example.com",
                escalation="営業部長", last_confirmed=TODAY)
    return {**base, **kw}


@pytest.fixture
def sales(conn, admin):
    """docs §6.1 の例: ドメイン「販売管理」と、受注明細・売上実績"""
    d = roles.create_domain(conn, admin, "販売管理", "営業部のデータ")
    orders = repo.register_asset(conn, admin, "受注明細", "o.csv", S, domain_id=d).asset_id
    sales = repo.register_asset(conn, admin, "売上実績", "s.csv", S, domain_id=d).asset_id
    roles.add_assignment(conn, admin, domain_id=d, **owner())
    return {"domain": d, "orders": orders, "sales": sales}


def labels(eff):
    return [(a["role_label"], roles.assignment_label(a), a["scopes"], a["source"]) for a in eff]


def test_validation(conn, admin, sales):
    with pytest.raises(ValueError, match="役職"):
        roles.add_assignment(conn, admin, asset_id=sales["orders"], **owner(position=" "))
    with pytest.raises(ValueError, match="責任の範囲"):
        roles.add_assignment(conn, admin, asset_id=sales["orders"], **owner(scopes=[]))
    with pytest.raises(ValueError, match="未対応の責任範囲"):
        roles.add_assignment(conn, admin, asset_id=sales["orders"], **owner(scopes=["なんでも"]))
    with pytest.raises(ValueError, match="担当者.*問い合わせ窓口"):
        roles.add_assignment(conn, admin, asset_id=sales["orders"], **steward(person="", contact=""))
    with pytest.raises(ValueError, match="最終確認日"):
        roles.add_assignment(conn, admin, asset_id=sales["orders"], **steward(last_confirmed="2026/11/01"))
    with pytest.raises(ValueError, match="未対応の役割"):
        roles.add_assignment(conn, admin, asset_id=sales["orders"], **steward(kind="manager"))
    with pytest.raises(ValueError, match="どちらか一方"):
        roles.add_assignment(conn, admin, domain_id=sales["domain"], asset_id=sales["orders"], **steward())
    with pytest.raises(ValueError, match="既にあります"):  # ドメインに同じ組織・役職のオーナー
        roles.add_assignment(conn, admin, domain_id=sales["domain"], **owner(scopes=["定義"]))
    # 種類に関係ない項目は捨てる
    aid = roles.add_assignment(conn, admin, asset_id=sales["orders"], **steward(position="無関係", scopes=["定義"]))
    a = roles.get_assignment(conn, aid)
    assert (a["position"], a["scopes"], a["last_confirmed"]) == ("", [], "2026-11-01")


def test_permissions(conn, admin, power, editor, sales):
    with pytest.raises(PermissionError):
        roles.add_assignment(conn, editor, asset_id=sales["orders"], **steward())
    with pytest.raises(PermissionError):
        roles.add_assignment(conn, power, domain_id=sales["domain"], **steward())
    with pytest.raises(PermissionError):
        roles.create_domain(conn, power, "x")
    with pytest.raises(PermissionError):
        roles.set_asset_domain(conn, editor, sales["orders"], None)
    aid = roles.add_assignment(conn, power, asset_id=sales["orders"], **steward())
    roles.update_assignment(conn, power, aid, contact="#sales-data")
    domain_owner = roles.assignments_for(conn, domain_id=sales["domain"])[0]["id"]
    with pytest.raises(PermissionError):
        roles.confirm_assignment(conn, power, domain_owner)
    with pytest.raises(PermissionError):
        roles.delete_assignment(conn, editor, aid)


def test_inheritance_matches_docs_example(conn, admin, sales):
    # 受注明細: オーナーはドメインから継承、スチュワードは業務・技術を個別設定
    roles.add_assignment(conn, admin, asset_id=sales["orders"], **steward())
    roles.add_assignment(conn, admin, asset_id=sales["orders"],
                         **steward("steward_technical", org="情報システム部", person="鈴木"))
    eff = roles.effective_assignments(conn, sales["orders"], TODAY)
    assert labels(eff) == [
        ("オーナー", "営業部 / 営業部長", roles.OWNER_SCOPES, "ドメインから継承"),
        ("スチュワード（業務）", "営業管理課 / 佐藤", [], "個別設定"),
        ("スチュワード（技術）", "情報システム部 / 鈴木", [], "個別設定"),
    ]
    assert roles.uncovered_owner_scopes(eff) == [] and roles.has_steward(eff)

    # 売上実績: 計上時点の定義だけ経理部長が上書き → 営業部長からは残りの範囲を継承
    roles.add_assignment(conn, admin, asset_id=sales["sales"], **owner(org="経理部", position="経理部長", scopes=["定義"]))
    eff = roles.effective_assignments(conn, sales["sales"], TODAY)
    assert labels(eff) == [
        ("オーナー", "経理部 / 経理部長", ["定義"], "個別設定"),
        ("オーナー", "営業部 / 営業部長", ["問いの受諾", "品質基準", "アクセス承認"], "ドメインから継承"),
    ]
    assert not roles.has_steward(eff)


def test_steward_inherited_per_type(conn, admin, sales):
    d = sales["domain"]
    roles.add_assignment(conn, admin, domain_id=d, **steward())
    roles.add_assignment(conn, admin, domain_id=d, **steward("steward_technical", org="情報システム部", person="鈴木"))
    roles.add_assignment(conn, admin, asset_id=sales["orders"],
                         **steward("steward_technical", org="外部委託", person="田中"))
    eff = roles.effective_assignments(conn, sales["orders"], TODAY)
    stewards = [(a["role_label"], a["person"], a["source"]) for a in eff if a["kind"] == "steward"]
    assert stewards == [
        ("スチュワード（業務）", "佐藤", "ドメインから継承"),
        ("スチュワード（技術）", "田中", "個別設定"),  # 技術はアセット側で上書き
    ]


def test_asset_owner_covering_all_scopes_hides_domain_owner(conn, admin, sales):
    roles.add_assignment(conn, admin, asset_id=sales["orders"], **owner(org="物流部", position="物流部長"))
    eff = roles.effective_assignments(conn, sales["orders"], TODAY)
    assert [roles.assignment_label(a) for a in eff] == ["物流部 / 物流部長"]


def test_no_domain_and_domain_change(conn, admin, power, sales):
    lone = repo.register_asset(conn, admin, "単独", "x.csv", S).asset_id
    eff = roles.effective_assignments(conn, lone, TODAY)
    assert eff == [] and roles.uncovered_owner_scopes(eff) == roles.OWNER_SCOPES
    roles.set_asset_domain(conn, power, lone, sales["domain"])
    assert [a["source"] for a in roles.effective_assignments(conn, lone, TODAY)] == ["ドメインから継承"]
    roles.set_asset_domain(conn, power, lone, None)
    assert roles.effective_assignments(conn, lone, TODAY) == []


def test_delete_domain(conn, admin, sales):
    roles.delete_domain(conn, admin, sales["domain"])
    assert repo.get_asset(conn, sales["orders"])["domain_id"] is None
    assert conn.execute("SELECT COUNT(*) FROM role_assignments").fetchone()[0] == 0
    assert roles.list_domains(conn) == []


def test_domain_crud(conn, admin, sales):
    with pytest.raises(ValueError):
        roles.create_domain(conn, admin, "販売管理")
    with pytest.raises(ValueError):
        roles.create_domain(conn, admin, " ")
    roles.update_domain(conn, admin, sales["domain"], name="販売", description="説明")
    d = roles.list_domains(conn)[0]
    assert (d["name"], d["description"], d["asset_count"]) == ("販売", "説明", 2)


def test_stale_and_confirm(conn, admin, power, sales):
    old = TODAY - timedelta(days=roles.STALE_DAYS + 1)
    aid = roles.add_assignment(conn, admin, asset_id=sales["orders"], **steward(last_confirmed=old))
    fresh = roles.add_assignment(conn, admin, asset_id=sales["orders"],
                                 **steward("steward_technical", person="鈴木",
                                           last_confirmed=TODAY - timedelta(days=roles.STALE_DAYS)))
    stale = roles.list_all_assignments(conn, stale_only=True, today=TODAY)
    assert [a["id"] for a in stale] == [aid] and stale[0]["asset_name"] == "受注明細"
    assert fresh not in [a["id"] for a in stale]
    roles.confirm_assignment(conn, power, aid, today=TODAY)
    assert roles.list_all_assignments(conn, stale_only=True, today=TODAY) == []
    assert len(roles.list_all_assignments(conn, person="佐藤")) == 1
    assert len(roles.list_all_assignments(conn, person="営業部長")) == 1  # 役職でも探せる
    assert roles.list_all_assignments(conn, org="営業部")[0]["domain_name"] == "販売管理"


def test_partial_update_keeps_concurrent_changes(conn, db_path, admin, power, sales):
    aid = roles.add_assignment(conn, admin, asset_id=sales["orders"], **steward())
    c1, c2 = connect(db_path), connect(db_path)
    roles.update_assignment(c1, admin, aid, contact="#sales-data")
    roles.update_assignment(c2, power, aid, escalation="営業部 次長")
    a = roles.get_assignment(conn, aid)
    assert (a["contact"], a["escalation"]) == ("#sales-data", "営業部 次長")
    with pytest.raises(ValueError, match="変更できない"):
        roles.update_assignment(conn, admin, aid, role_type="owner")
    with pytest.raises(ValueError, match="必須"):
        roles.update_assignment(conn, admin, aid, person="")


def test_changelog_and_suggestions(conn, admin, sales):
    aid = roles.add_assignment(conn, admin, asset_id=sales["orders"], **steward())
    roles.update_assignment(conn, admin, aid, contact="#sales")
    roles.delete_assignment(conn, admin, aid)
    rows = [(c["entity"], c["action"], c["target"]) for c in reversed(changelog.list_changes(conn, asset_id=sales["orders"]))]
    assert rows[-3:] == [
        ("role", "add", "スチュワード（業務）"),
        ("role", "update", "スチュワード（業務）（営業管理課 / 佐藤） / 問い合わせ窓口"),
        ("role", "remove", "スチュワード（業務）"),
    ]
    domain_logs = [c["target"] for c in changelog.list_changes(conn) if c["entity"] == "role" and c["asset_id"] is None]
    assert domain_logs == ["ドメイン 販売管理 / オーナー"]
    assert roles.suggestions(conn, "position") == ["営業部長"]
    assert roles.suggestions(conn, "org") == ["営業部"]


def test_list_assets_shows_roles(conn, admin, sales):
    roles.add_assignment(conn, admin, asset_id=sales["orders"],
                         **steward(last_confirmed=TODAY - timedelta(days=400)))
    res = {a["name"]: a for a in repo.list_assets(conn)}
    assert res["受注明細"]["domain_name"] == "販売管理"
    assert res["受注明細"]["owners"] == ["営業部 / 営業部長"]
    assert res["受注明細"]["stewards"] == ["営業管理課 / 佐藤（スチュワード（業務））"]
    assert res["受注明細"]["stale_roles"] == 1
    names = lambda **kw: [a["name"] for a in repo.list_assets(conn, **kw)]
    assert names(query="営業部長") == ["受注明細", "売上実績"]  # ドメインからの継承分でもヒット
    assert names(query="佐藤") == ["受注明細"]
    assert names(stale_roles_only=True) == ["受注明細"]
    lone = repo.register_asset(conn, admin, "単独", "x.csv", S).asset_id
    assert names(domain_ids=[None]) == ["単独"]
    assert names(domain_ids=[sales["domain"], None]) == ["単独", "受注明細", "売上実績"]
    comp = props.completeness(conn, lone)
    assert comp.missing[-2:] == ["オーナー", "スチュワード"]
    roles.add_assignment(conn, admin, asset_id=lone, **owner(scopes=["定義"]))
    assert "オーナー（問いの受諾・品質基準・アクセス承認）" in props.completeness(conn, lone).missing
