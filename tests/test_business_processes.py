import pytest

from catalog import business_processes as bps
from catalog import changelog
from catalog import metrics as ms


def test_crud_and_permissions(conn, power, editor, viewer):
    bid = bps.create_business_process(conn, power, "  受注 ", "注文を受けて確定するまで")
    assert bps.get_business_process(conn, bid)["name"] == "受注"
    for u in (editor, viewer):
        with pytest.raises(PermissionError):
            bps.create_business_process(conn, u, "出荷")
    with pytest.raises(ValueError):
        bps.create_business_process(conn, power, "受注")  # 重複
    with pytest.raises(ValueError):
        bps.create_business_process(conn, power, "  ")

    bps.update_business_process(conn, power, bid, description="受注業務")
    assert bps.get_business_process(conn, bid)["description"] == "受注業務"
    before = len(changelog.list_changes(conn))
    bps.update_business_process(conn, power, bid, description="受注業務")  # 変更なしは記録しない
    assert len(changelog.list_changes(conn)) == before


def test_delete_unsets_metrics(conn, power, editor):
    bid = bps.create_business_process(conn, power, "受注")
    mid = ms.create_metric(conn, editor, "受注金額", business_process_id=bid)
    assert bps.list_business_processes(conn)[0]["metric_count"] == 1
    assert bps.delete_business_process(conn, power, bid) == 1
    m = ms.get_metric(conn, mid)
    assert m["business_process_id"] is None and m["business_process"] is None
