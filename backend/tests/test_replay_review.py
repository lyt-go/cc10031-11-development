"""区间重采复核领取测试：授权更正、部分提交、剩余时段、归属核验与报告回查。

场景对应：第一次复核获准完成 10:10 至 10:20 的三个点，保存前授权先扩到
10:25 再收窄到 10:22，提交已完成的两点后关联质检报告领用范围，同一张复核
单继续取下一批，另一人不能再领已提交或被他人领取的时段，被撤回的
10:23 至 10:25 不能进入正式质检。
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

POINT = "SENS-REV1"
REPLAY_NO = "RANG-REV1"
# 重采数据点：10:10/10:15/10:20 为首次获批的三个点，10:21~10:25 为后续更正涉及的点
DATA_TIMES = ["10:10", "10:15", "10:20", "10:21", "10:22", "10:23", "10:24", "10:25"]


def _ts(hhmm: str) -> str:
    return f"2026-09-01T{hhmm}:00"


@pytest.fixture(autouse=True)
def setup_data_dir():
    """将数据目录指向临时位置，避免污染 real data。"""
    with tempfile.TemporaryDirectory() as tmp:
        with patch("app.services.base.DATA_ROOT", Path(tmp)):
            import app.services.range_replay as rr
            rr.DATA_ROOT = Path(tmp)
            yield Path(tmp)


@pytest.fixture()
def replay_entry():
    """登记时段为 10:10~10:25 的重采申请并写入重采数据文件。"""
    from app.services.range_replay import RangeReplayService

    svc = RangeReplayService()
    entry, missing = svc.create_entry({
        "重采编号": REPLAY_NO,
        "测点编号": POINT,
        "起始时刻": _ts("10:10"),
        "截止时刻": _ts("10:25"),
        "重采原因": "传感器故障",
        "覆盖方式": "追加",
        "重采点数": "8",
        "重采水位": "8",
    })
    assert not missing
    return svc, entry


def _write_replay_data(data_dir: Path) -> None:
    folder = data_dir / "replayed_ranges"
    folder.mkdir(parents=True, exist_ok=True)
    with open(folder / f"{REPLAY_NO}_replay.jsonl", "w", encoding="utf-8") as f:
        for i, hhmm in enumerate(DATA_TIMES):
            f.write(json.dumps({
                "replay_id": REPLAY_NO,
                "point": POINT,
                "original_timestamp": _ts(hhmm),
                "replay_value": 23.5 + i,
            }, ensure_ascii=False) + "\n")


class TestReviewClaimScenario:
    """主场景：两次授权更正 + 部分提交 + 下一批领取 + 归属核验。"""

    def test_full_scenario(self, setup_data_dir, replay_entry):
        svc, replay = replay_entry
        _write_replay_data(setup_data_dir)
        replay_id = replay["id"]

        # 复核权限：reviewer_a 负责该测点
        svc.grant_review_permission("reviewer_a", [POINT])

        # 发布申请：第一次复核获准完成 10:10 至 10:20
        pub = svc.publish_review_scope(replay_id, _ts("10:10"), _ts("10:20"))
        assert pub["version"] == 1
        assert pub["approved_end"] == _ts("10:20")

        # reviewer_a 领取获批片段 → 10:10~10:20 的三个点
        sheet = svc.claim_review_segment(replay_id, "reviewer_a", _ts("10:10"), _ts("10:20"))
        assert sheet["claimed"] == [_ts("10:10"), _ts("10:15"), _ts("10:20")]
        assert sheet["status"] == "复核中"
        sheet_id = sheet["sheet_id"]

        # 更正前读取：申请时段与可复核时段重叠一半，其余为需申请复核
        data = svc.read_replay_data(POINT, _ts("10:10"), _ts("10:25"), "reviewer_a")
        assert data["summary"]["正式质检"] == 3
        assert data["summary"]["需申请复核"] == 5
        assert data["summary"]["未批准"] == 0

        # 保存前两次授权更正：先扩大到 10:25，再收窄到 10:22
        c1 = svc.correct_review_scope(replay_id, _ts("10:10"), _ts("10:25"), note="扩大权限")
        assert c1["version"] == 2
        assert c1["newly_approved"] == [_ts(t) for t in ["10:21", "10:22", "10:23", "10:24", "10:25"]]
        c2 = svc.correct_review_scope(replay_id, _ts("10:10"), _ts("10:22"), note="收窄权限")
        assert c2["version"] == 3
        assert c2["withdrawn"] == [_ts(t) for t in ["10:23", "10:24", "10:25"]]

        # 授权变化不能把未提交的区间误计作已完成
        mid = svc.review_sheet_detail(sheet_id)
        assert mid["submitted"] == []
        assert mid["status"] == "复核中"

        # 两次更正后：原先三点仍有资格，新增的 10:21 正式批准可复核，10:23~10:25 未批准
        data = svc.read_replay_data(POINT, _ts("10:10"), _ts("10:25"), "reviewer_a")
        formal = [r["timestamp"] for r in data["records"] if r["category"] == "正式质检"]
        unapproved = [r["timestamp"] for r in data["records"] if r["category"] == "未批准"]
        assert formal == [_ts(t) for t in ["10:10", "10:15", "10:20", "10:21", "10:22"]]
        assert unapproved == [_ts(t) for t in ["10:23", "10:24", "10:25"]]

        # 提交已完成的两点，质量报告按实际领用范围关联
        sub = svc.submit_review_points(sheet_id, "reviewer_a", [_ts("10:10"), _ts("10:15")])
        assert sub["submitted_now"] == [_ts("10:10"), _ts("10:15")]
        # 系统据此生成尚待复核的剩余时段
        assert sub["pending"] == [_ts("10:20")]
        assert sub["remaining_ranges"] == [{
            "start": _ts("10:20"), "end": _ts("10:20"), "points": [_ts("10:20")],
        }]
        assert sub["status"] == "部分提交"
        report = sub["report"]
        assert report["领用范围"] == f"{_ts('10:10')}~{_ts('10:15')}"
        assert report["领用点数"] == "2"
        assert report["复核单号"] == sheet_id
        assert report["重采编号"] == REPLAY_NO
        assert report["status"] == "已出报告"

        # 既有质检报告能按当时的领用范围回查
        scope = svc.report_claim_scope(report["id"])
        assert scope["关联复核"] is True
        assert scope["领用范围"] == f"{_ts('10:10')}~{_ts('10:15')}"
        assert scope["领用明细"] == [_ts("10:10"), _ts("10:15")]
        assert scope["复核员"] == "reviewer_a"

        # 同一张复核单继续取下一批：新获批的 10:21、10:22 补进本单
        nxt = svc.claim_next_batch(sheet_id, "reviewer_a")
        assert nxt["newly_claimed"] == [_ts("10:21"), _ts("10:22")]
        assert nxt["pending"] == [_ts("10:20"), _ts("10:21"), _ts("10:22")]

        # 另一人（有该测点权限）尝试领取剩余时段 → 已被领取，拿不到
        svc.grant_review_permission("reviewer_b", [POINT])
        attempt = svc.claim_review_segment(replay_id, "reviewer_b", _ts("10:20"), _ts("10:22"))
        assert "error" in attempt

        # 核对第三点和 10:21 的实际归属：仍属于 reviewer_a 的复核单
        ownership = svc.segment_ownership(replay_id, [_ts("10:20"), _ts("10:21")])
        for item in ownership["points"]:
            assert item["claimed"] is True
            assert item["sheet_id"] == sheet_id
            assert item["reviewer"] == "reviewer_a"
            assert item["submitted"] is False

        # 已提交的两点不能再领
        reclaim = svc.claim_review_segment(replay_id, "reviewer_b", _ts("10:10"), _ts("10:15"))
        assert "error" in reclaim

        # 被撤回的 10:23 至 10:25 不能进入正式质检：不能领取、不能提交
        withdrawn_claim = svc.claim_review_segment(replay_id, "reviewer_b", _ts("10:23"), _ts("10:25"))
        assert "error" in withdrawn_claim
        data = svc.read_replay_data(POINT, _ts("10:23"), _ts("10:25"), "reviewer_a")
        assert data["summary"]["正式质检"] == 0
        assert data["summary"]["未批准"] == 3

    def test_authorization_change_never_counts_unsubmitted_as_done(self, setup_data_dir, replay_entry):
        """授权收窄后，未提交的区间列为被撤回，不得误计作已完成。"""
        svc, replay = replay_entry
        _write_replay_data(setup_data_dir)
        replay_id = replay["id"]
        svc.grant_review_permission("reviewer_a", [POINT])

        svc.publish_review_scope(replay_id, _ts("10:10"), _ts("10:25"))
        sheet = svc.claim_review_segment(replay_id, "reviewer_a", _ts("10:10"), _ts("10:25"))
        assert len(sheet["claimed"]) == 8
        sheet_id = sheet["sheet_id"]

        svc.submit_review_points(sheet_id, "reviewer_a", [_ts("10:10"), _ts("10:15")])
        # 授权收窄到 10:22：10:23~10:25 被撤回但从未提交
        svc.correct_review_scope(replay_id, _ts("10:10"), _ts("10:22"), note="收窄")

        view = svc.review_sheet_detail(sheet_id)
        assert view["submitted"] == [_ts("10:10"), _ts("10:15")]
        assert view["pending"] == [_ts("10:20"), _ts("10:21"), _ts("10:22")]
        assert view["withdrawn_unsubmitted"] == [_ts(t) for t in ["10:23", "10:24", "10:25"]]
        assert view["status"] == "部分提交"  # 不能因授权收窄被误计为已完成

        # 被撤回的点不能提交进入正式质检
        ret = svc.submit_review_points(sheet_id, "reviewer_a", [_ts("10:23")])
        assert "error" in ret
        assert ret["rejected"][0]["reason"] == "授权已撤回，不能进入正式质检"

        # 提交完剩余获批区间后，被撤回点依然不算完成
        done = svc.submit_review_points(
            sheet_id, "reviewer_a", [_ts("10:20"), _ts("10:21"), _ts("10:22")])
        assert done["pending"] == []
        assert done["withdrawn_unsubmitted"] == [_ts(t) for t in ["10:23", "10:24", "10:25"]]
        assert done["status"] == "部分提交"

    def test_permission_isolation(self, setup_data_dir, replay_entry):
        """越权人员只能查看自己的申请状态，不能改动他人的领取结果。"""
        svc, replay = replay_entry
        _write_replay_data(setup_data_dir)
        replay_id = replay["id"]
        svc.grant_review_permission("reviewer_a", [POINT])
        svc.publish_review_scope(replay_id, _ts("10:10"), _ts("10:20"))
        sheet = svc.claim_review_segment(replay_id, "reviewer_a", _ts("10:10"), _ts("10:20"))
        sheet_id = sheet["sheet_id"]

        # reviewer_c 无该测点复核权限：读取被驳回，只能看到自己的申请状态（空）
        denied = svc.read_replay_data(POINT, _ts("10:10"), _ts("10:25"), "reviewer_c")
        assert denied["code"] == "forbidden"
        assert denied["own_sheets"] == []

        # 越权领取、越权提交均被拒绝
        claim = svc.claim_review_segment(replay_id, "reviewer_c", _ts("10:10"), _ts("10:20"))
        assert claim["code"] == "forbidden"
        submit = svc.submit_review_points(sheet_id, "reviewer_c", [_ts("10:10")])
        assert submit["code"] == "forbidden"
        assert "无权改动他人的领取结果" in submit["error"]

        # reviewer_b 有测点权限，但也不能改动 reviewer_a 的领取结果
        svc.grant_review_permission("reviewer_b", [POINT])
        submit = svc.submit_review_points(sheet_id, "reviewer_b", [_ts("10:10")])
        assert submit["code"] == "forbidden"
        take_next = svc.claim_next_batch(sheet_id, "reviewer_b")
        assert take_next["code"] == "forbidden"

        # 各自只能查看自己的申请状态
        assert [s["sheet_id"] for s in svc.reviewer_sheets("reviewer_a")] == [sheet_id]
        assert svc.reviewer_sheets("reviewer_b") == []

        # 归属未被越权操作改变
        ownership = svc.segment_ownership(replay_id, [_ts("10:10")])
        assert ownership["points"][0]["reviewer"] == "reviewer_a"
        assert ownership["points"][0]["submitted"] is False


class TestReviewRouterWiring:
    """复核领取接口已注册到应用，且不与通用资源路由冲突。"""

    def test_routes_registered(self):
        from app.main import app

        def _walk(routes):
            for route in routes:
                path = getattr(route, "path", None)
                if path:
                    yield path
                original = getattr(route, "original_router", None)
                if original is not None:
                    yield from _walk(original.routes)
                nested = getattr(route, "routes", None)
                if nested:
                    yield from _walk(nested)

        paths = set(_walk(app.routes))
        assert "/api/range_replay/review/data" in paths
        assert "/api/range_replay/review/{replay_id}/publish" in paths
        assert "/api/range_replay/review/{replay_id}/correct" in paths
        assert "/api/range_replay/review/{replay_id}/claim" in paths
        assert "/api/range_replay/review/sheets/{sheet_id}/submit" in paths
        assert "/api/range_replay/review/sheets/{sheet_id}/claim-next" in paths
        assert "/api/range_replay/review/reports/{report_id}/scope" in paths
        # 通用资源路由仍然可用
        assert "/api/{module}" in paths
        assert "/api/{module}/{entry_id}" in paths
