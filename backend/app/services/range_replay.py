"""区间重采领域服务：维护传感器故障或标定变更引起的历史区间重采与覆盖方式。

复核领取子域：把获批的重采片段交给质量复核人员。
- 按实际复核权限核验数据归属，越权人员只能查看自己的申请状态，不能改动他人的领取结果；
- 复核授权发布后支持多次更正，授权变化不会把未提交的区间误计作已完成；
- 复核员可提交已经完成的那部分区间，系统据此生成尚待复核的剩余时段；
- 质检报告按提交时的实际领用范围关联，既有报告可随时按当时范围回查。
"""
from __future__ import annotations

import time
from datetime import datetime
from typing import Any

from app.services.base import BaseModuleService
from app.store import store

# 重采数据复核分类
CATEGORY_FORMAL = "正式质检"  # 处于当前获批时段内，可进入正式质检
CATEGORY_NEEDS_APPLICATION = "需申请复核"  # 在重采申请时段内但从未获批，需要申请复核
CATEGORY_UNAPPROVED = "未批准"  # 曾获批但已被授权更正撤回，不能进入正式质检


def _ts_key(value: Any) -> float | None:
    """把时刻归一化为可比较的分钟数，支持 ISO 时间与 HH:MM(:SS)。"""
    text = str(value or "").strip()
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00")).replace(tzinfo=None)
        return (dt - datetime(1970, 1, 1)).total_seconds() / 60
    except ValueError:
        pass
    for fmt in ("%H:%M:%S", "%H:%M"):
        try:
            parsed = datetime.strptime(text, fmt)
            return parsed.hour * 60 + parsed.minute + parsed.second / 60
        except ValueError:
            continue
    return None


def _sort_key(value: Any) -> float:
    key = _ts_key(value)
    return key if key is not None else float("inf")


class RangeReplayService(BaseModuleService):
    MODULE = "range_replay"
    ENTITY = "重采任务"
    OPERATION_TERM = "水位"
    LIST_FIELDS = ["重采编号", "测点编号", "起始时刻", "截止时刻", "重采原因", "覆盖方式", "重采点数", "重采水位"]
    REQUIRED_FIELDS = ["重采编号", "测点编号", "起始时刻"]
    STATUS_ORDER = ["草稿", "待重采", "重采中", "已完成", "已取消"]
    ACTION_RULES = {"启动重采": "重采中", "标记完成": "已完成", "取消重采": "已取消"}
    NEGATIVE_ACTIONS = ["取消重采"]

    def __init__(self) -> None:
        # 复核员 -> 负责测点集合（实际复核权限）
        self._review_permissions: dict[str, set[str]] = {}
        # 重采申请 id -> 复核授权（含更正历史）
        self._review_auth: dict[int, dict[str, Any]] = {}
        # 复核单号 -> 复核单（领取结果）
        self._review_sheets: dict[str, dict[str, Any]] = {}
        self._sheet_seq = 0

    # 数据回填操作：对传感器历史区间执行重采样计算写入重采覆盖结果数据
    def replay_range(self, replay_id: int) -> dict[str, Any]:
        """对传感器历史区间执行重采样计算并写入重采覆盖结果数据文件。"""
        replay = store.find(self.MODULE, replay_id)
        if replay is None:
            return {"error": f"重采任务 {replay_id} 不存在"}
        self.run_action(replay_id, "启动重采")
        point_key = replay["测点编号"]
        reason = replay.get("重采原因", "传感器故障")
        overwrite = replay.get("覆盖方式", "追加")

        # 读取原始数据重采
        cal_file = self._data_dir("calibrated") / f"{point_key}_calibrated.jsonl"
        records = self._read_jsonl(cal_file)
        if not records:
            edge_file = self._data_dir("edge_samples") / f"{point_key}.jsonl"
            records = self._read_jsonl(edge_file)

        # 模拟按时间区间重采样
        replay_records = []
        for r in records:
            replay_records.append({
                "replay_id": replay["重采编号"],
                "point": point_key,
                "reason": reason,
                "overwrite_mode": overwrite,
                "original_timestamp": r.get("timestamp", ""),
                "replay_value": r.get("calibrated_value_V", r.get("raw_value_mV", 0)),
                "replayed_at": time.time(),
            })
        # 将重采结果写入区间重采数据文件
        output_file = self._data_dir("replayed_ranges") / f"{replay['重采编号']}_replay.jsonl"
        written = self._write_jsonl(output_file, replay_records)
        replay["重采点数"] = str(written)
        replay["重采水位"] = str(written)
        self.run_action(replay_id, "标记完成")
        return {"replay_id": replay["重采编号"], "point": point_key,
                "reason": reason, "records_replayed": written}

    def batch_replay_faulty(self, reason: str = "传感器故障校准") -> dict[str, Any]:
        """批量重采所有因传感器故障导致异常的测点历史区间数据。"""
        all_replays = store.rows(self.MODULE)
        faulty = [r for r in all_replays if "故障" in (r.get("重采原因", ""))]
        results = []
        for r in faulty:
            res = self.replay_range(r["id"])
            results.append(res)
        # 汇总批量重采记录
        self._write_jsonl(
            self._data_dir("replayed_ranges") / "batch_replay_summary.jsonl",
            [{"reason": reason, "total_replayed": len(results), "at": time.time()}]
        )
        return {"reason": reason, "total_replayed": len(results)}

    # ------------------------------------------------------------------
    # 复核权限：按实际复核权限核验数据归属
    # ------------------------------------------------------------------

    def grant_review_permission(self, reviewer: str, points: list[str]) -> dict[str, Any]:
        """把负责测点的复核权限授予复核员。"""
        reviewer = str(reviewer or "").strip()
        if not reviewer:
            return {"error": "复核员不能为空", "code": "invalid"}
        owned = self._review_permissions.setdefault(reviewer, set())
        for point in points:
            point = str(point or "").strip()
            if point:
                owned.add(point)
        return {"reviewer": reviewer, "points": sorted(owned)}

    def revoke_review_permission(self, reviewer: str, points: list[str]) -> dict[str, Any]:
        """收回复核员对指定测点的复核权限。"""
        reviewer = str(reviewer or "").strip()
        owned = self._review_permissions.get(reviewer, set())
        for point in points:
            owned.discard(str(point or "").strip())
        return {"reviewer": reviewer, "points": sorted(owned)}

    def review_permissions(self, reviewer: str | None = None) -> dict[str, Any]:
        """查询复核权限；指定复核员时只返回其负责测点。"""
        if reviewer is not None:
            return {"reviewer": reviewer,
                    "points": sorted(self._review_permissions.get(reviewer, set()))}
        return {"items": [
            {"reviewer": name, "points": sorted(points)}
            for name, points in sorted(self._review_permissions.items())
        ]}

    def _permission_error(self, reviewer: str, point: str) -> dict[str, Any] | None:
        """越权人员只能查看自己的申请状态，不能读取或改动他人的复核数据。"""
        if point not in self._review_permissions.get(reviewer, set()):
            return {
                "error": f"复核员 {reviewer or '（空）'} 对测点 {point} 无复核权限，仅可查看自己的申请状态",
                "code": "forbidden",
                "own_sheets": self.reviewer_sheets(reviewer),
            }
        return None

    # ------------------------------------------------------------------
    # 复核授权：发布后支持多次更正，历史版本全部留痕
    # ------------------------------------------------------------------

    def publish_review_scope(self, replay_id: int, approved_start: Any, approved_end: Any,
                             note: str = "") -> dict[str, Any]:
        """发布重采申请的复核授权，首次划定可复核时段。"""
        replay = store.find(self.MODULE, replay_id)
        if replay is None:
            return {"error": f"重采任务 {replay_id} 不存在", "code": "not_found"}
        if replay_id in self._review_auth:
            return {"error": f"重采任务 {replay_id} 的复核授权已发布，请改用授权更正", "code": "invalid"}
        start_key, end_key = _ts_key(approved_start), _ts_key(approved_end)
        if start_key is None or end_key is None or start_key > end_key:
            return {"error": "获批时段不合法", "code": "invalid"}
        auth = {
            "replay_id": replay_id,
            "重采编号": replay["重采编号"],
            "测点编号": replay["测点编号"],
            "approved_start": str(approved_start),
            "approved_end": str(approved_end),
            "version": 1,
            "history": [{
                "version": 1,
                "approved_start": str(approved_start),
                "approved_end": str(approved_end),
                "note": note or "首次发布",
                "changed_at": time.time(),
            }],
        }
        self._review_auth[replay_id] = auth
        return self._auth_view(auth)

    def correct_review_scope(self, replay_id: int, approved_start: Any, approved_end: Any,
                             note: str = "") -> dict[str, Any]:
        """授权更正：扩大或收窄可复核时段。

        只调整授权范围与历史，不触碰任何复核单的提交记录，
        未提交的区间不会因为授权变化被误计作已完成。
        """
        auth = self._review_auth.get(replay_id)
        if auth is None:
            return {"error": f"重采任务 {replay_id} 尚未发布复核授权", "code": "not_found"}
        start_key, end_key = _ts_key(approved_start), _ts_key(approved_end)
        if start_key is None or end_key is None or start_key > end_key:
            return {"error": "获批时段不合法", "code": "invalid"}
        prev_start, prev_end = _ts_key(auth["approved_start"]), _ts_key(auth["approved_end"])
        version = auth["version"] + 1
        auth["approved_start"] = str(approved_start)
        auth["approved_end"] = str(approved_end)
        auth["version"] = version
        auth["history"].append({
            "version": version,
            "approved_start": str(approved_start),
            "approved_end": str(approved_end),
            "note": note or f"第{version}次授权更正",
            "changed_at": time.time(),
        })
        # 基于重采数据点给出本次更正的差异，便于核对新增获批与被撤回区间
        replay = store.find(self.MODULE, replay_id)
        newly, withdrawn = [], []
        for rec in self._load_replay_records(replay):
            key = rec["ts_key"]
            in_prev = prev_start <= key <= prev_end
            in_now = start_key <= key <= end_key
            if in_now and not in_prev:
                newly.append(rec["timestamp"])
            elif in_prev and not in_now:
                withdrawn.append(rec["timestamp"])
        view = self._auth_view(auth)
        view["newly_approved"] = newly
        view["withdrawn"] = withdrawn
        return view

    def current_review_scope(self, replay_id: int) -> dict[str, Any]:
        """查看当前复核授权与全部更正历史。"""
        auth = self._review_auth.get(replay_id)
        if auth is None:
            return {"error": f"重采任务 {replay_id} 尚未发布复核授权", "code": "not_found"}
        return self._auth_view(auth)

    def _auth_view(self, auth: dict[str, Any]) -> dict[str, Any]:
        return {
            "replay_id": auth["replay_id"],
            "重采编号": auth["重采编号"],
            "测点编号": auth["测点编号"],
            "approved_start": auth["approved_start"],
            "approved_end": auth["approved_end"],
            "version": auth["version"],
            "history": [dict(item) for item in auth["history"]],
        }

    @staticmethod
    def _approved_now(auth: dict[str, Any], key: float) -> bool:
        return _ts_key(auth["approved_start"]) <= key <= _ts_key(auth["approved_end"])

    @staticmethod
    def _approved_ever(auth: dict[str, Any], key: float) -> bool:
        return any(
            _ts_key(item["approved_start"]) <= key <= _ts_key(item["approved_end"])
            for item in auth["history"]
        )

    # ------------------------------------------------------------------
    # 重采数据读取：按负责测点和时段读取，并区分复核分类与归属
    # ------------------------------------------------------------------

    def _load_replay_records(self, replay: dict[str, Any] | None) -> list[dict[str, Any]]:
        """读取某个重采申请的重采结果数据，归一化时刻与取值。"""
        if replay is None:
            return []
        output_file = self._data_dir("replayed_ranges") / f"{replay['重采编号']}_replay.jsonl"
        records = []
        for raw in self._read_jsonl(output_file):
            ts = raw.get("original_timestamp") or raw.get("timestamp") or ""
            key = _ts_key(ts)
            if key is None:
                continue
            records.append({
                "replay_id": replay["重采编号"],
                "timestamp": ts,
                "ts_key": key,
                "value": raw.get("replay_value", raw.get("calibrated_value_V", raw.get("raw_value_mV", 0))),
            })
        return records

    def read_replay_data(self, point: str, start: Any, end: Any, reviewer: str) -> dict[str, Any]:
        """按负责测点和时段读取重采数据。

        结果区分三类：能进入正式质检的数据、需要申请复核的区间、未批准记录；
        每条记录附带领取归属，越权人员只能查看自己的申请状态。
        """
        reviewer = str(reviewer or "").strip()
        err = self._permission_error(reviewer, point)
        if err is not None:
            return err
        start_key, end_key = _ts_key(start), _ts_key(end)
        if start_key is None or end_key is None or start_key > end_key:
            return {"error": "读取时段不合法", "code": "invalid"}
        records = []
        for replay in store.rows(self.MODULE):
            if replay.get("测点编号") != point:
                continue
            auth = self._review_auth.get(replay["id"])
            for rec in self._load_replay_records(replay):
                key = rec["ts_key"]
                if not (start_key <= key <= end_key):
                    continue
                if auth is not None and self._approved_now(auth, key):
                    category = CATEGORY_FORMAL
                elif auth is not None and self._approved_ever(auth, key):
                    category = CATEGORY_UNAPPROVED
                else:
                    category = CATEGORY_NEEDS_APPLICATION
                owner = self._point_owner(replay["id"], key)
                records.append({
                    "replay_id": rec["replay_id"],
                    "timestamp": rec["timestamp"],
                    "value": rec["value"],
                    "category": category,
                    "claimed_by": owner["sheet_id"] if owner else None,
                    "reviewer": owner["reviewer"] if owner else None,
                    "submitted": owner["submitted"] if owner else False,
                })
        records.sort(key=lambda item: _sort_key(item["timestamp"]))
        summary = {
            CATEGORY_FORMAL: sum(1 for r in records if r["category"] == CATEGORY_FORMAL),
            CATEGORY_NEEDS_APPLICATION: sum(1 for r in records if r["category"] == CATEGORY_NEEDS_APPLICATION),
            CATEGORY_UNAPPROVED: sum(1 for r in records if r["category"] == CATEGORY_UNAPPROVED),
        }
        return {
            "point": point,
            "start": str(start),
            "end": str(end),
            "reviewer": reviewer,
            "records": records,
            "summary": summary,
        }

    # ------------------------------------------------------------------
    # 复核单：领取获批片段、部分提交、剩余时段生成
    # ------------------------------------------------------------------

    def claim_review_segment(self, replay_id: int, reviewer: str,
                             start: Any, end: Any) -> dict[str, Any]:
        """复核员领取获批片段，生成复核单；已被领取或已提交的点不能再领。"""
        replay = store.find(self.MODULE, replay_id)
        if replay is None:
            return {"error": f"重采任务 {replay_id} 不存在", "code": "not_found"}
        reviewer = str(reviewer or "").strip()
        err = self._permission_error(reviewer, replay["测点编号"])
        if err is not None:
            return err
        auth = self._review_auth.get(replay_id)
        if auth is None:
            return {"error": f"重采任务 {replay_id} 尚未发布复核授权", "code": "invalid"}
        start_key, end_key = _ts_key(start), _ts_key(end)
        if start_key is None or end_key is None or start_key > end_key:
            return {"error": "领取时段不合法", "code": "invalid"}
        taken = self._taken_points(replay_id)
        claimable = [
            rec for rec in self._load_replay_records(replay)
            if start_key <= rec["ts_key"] <= end_key
            and self._approved_now(auth, rec["ts_key"])
            and rec["ts_key"] not in taken
        ]
        if not claimable:
            return {
                "error": "该时段内没有可领取的获批片段（可能未获批、已被领取或已提交）",
                "code": "invalid",
            }
        self._sheet_seq += 1
        sheet_id = f"REVIEW-{replay['重采编号']}-{self._sheet_seq:02d}"
        sheet = {
            "sheet_id": sheet_id,
            "replay_id": replay_id,
            "重采编号": replay["重采编号"],
            "测点编号": replay["测点编号"],
            "reviewer": reviewer,
            "claimed": [rec["timestamp"] for rec in claimable],
            "submitted": [],
            "reports": [],
            "created_at": time.time(),
        }
        self._review_sheets[sheet_id] = sheet
        return self._sheet_view(sheet)

    def claim_next_batch(self, sheet_id: str, reviewer: str) -> dict[str, Any]:
        """同一张复核单继续取下一批：把当前获批且无人领取的点补进本单。"""
        sheet = self._review_sheets.get(sheet_id)
        if sheet is None:
            return {"error": f"复核单 {sheet_id} 不存在", "code": "not_found"}
        if sheet["reviewer"] != str(reviewer or "").strip():
            return {"error": f"复核员 {reviewer} 无权改动他人的领取结果", "code": "forbidden"}
        replay = store.find(self.MODULE, sheet["replay_id"])
        auth = self._review_auth.get(sheet["replay_id"])
        if replay is None or auth is None:
            return {"error": "复核单对应的重采申请或授权已失效", "code": "not_found"}
        taken = self._taken_points(sheet["replay_id"])
        own = {_ts_key(t) for t in sheet["claimed"]}
        newly = []
        for rec in self._load_replay_records(replay):
            key = rec["ts_key"]
            if key in own or key in taken:
                continue
            if not self._approved_now(auth, key):
                continue
            newly.append(rec)
        for rec in newly:
            sheet["claimed"].append(rec["timestamp"])
        view = self._sheet_view(sheet)
        view["newly_claimed"] = [rec["timestamp"] for rec in newly]
        if not newly:
            view["message"] = "当前没有更多可领取的获批片段"
        return view

    def submit_review_points(self, sheet_id: str, reviewer: str,
                             points: list[Any]) -> dict[str, Any]:
        """提交已经完成的那部分区间，系统据此生成尚待复核的剩余时段。

        提交成功后出具质量报告，并按提交时的实际领用范围关联，
        后续授权更正不影响已生成的报告快照。
        """
        sheet = self._review_sheets.get(sheet_id)
        if sheet is None:
            return {"error": f"复核单 {sheet_id} 不存在", "code": "not_found"}
        if sheet["reviewer"] != str(reviewer or "").strip():
            return {"error": f"复核员 {reviewer} 无权改动他人的领取结果", "code": "forbidden"}
        auth = self._review_auth.get(sheet["replay_id"])
        claimed = {_ts_key(t): t for t in sheet["claimed"]}
        submitted = {_ts_key(t) for t in sheet["submitted"]}
        accepted, rejected = [], []
        for point in points:
            key = _ts_key(point)
            if key is None or key not in claimed:
                rejected.append({"point": str(point), "reason": "不在本复核单领取范围内"})
                continue
            if key in submitted:
                rejected.append({"point": claimed[key], "reason": "已提交，不能重复提交"})
                continue
            if auth is None or not self._approved_now(auth, key):
                rejected.append({"point": claimed[key], "reason": "授权已撤回，不能进入正式质检"})
                continue
            accepted.append(claimed[key])
        if not accepted:
            return {"error": "没有可提交的已完成区间", "code": "invalid", "rejected": rejected}
        sheet["submitted"].extend(accepted)
        report = self._create_quality_report(sheet, accepted)
        sheet["reports"].append(report["报告编号"])
        view = self._sheet_view(sheet)
        view["submitted_now"] = accepted
        view["rejected"] = rejected
        view["report"] = report
        return view

    def review_sheet_detail(self, sheet_id: str) -> dict[str, Any]:
        """查看复核单详情：领取、已提交、尚待复核与被撤回区间。"""
        sheet = self._review_sheets.get(sheet_id)
        if sheet is None:
            return {"error": f"复核单 {sheet_id} 不存在", "code": "not_found"}
        return self._sheet_view(sheet)

    def reviewer_sheets(self, reviewer: str) -> list[dict[str, Any]]:
        """列出复核员自己的复核单，越权人员只能查看自己的申请状态。"""
        reviewer = str(reviewer or "").strip()
        return [
            self._sheet_view(sheet)
            for sheet in self._review_sheets.values()
            if sheet["reviewer"] == reviewer
        ]

    def segment_ownership(self, replay_id: int, points: list[Any]) -> dict[str, Any]:
        """核对指定时刻点的实际归属：被哪张复核单领取、是否已提交。"""
        if store.find(self.MODULE, replay_id) is None:
            return {"error": f"重采任务 {replay_id} 不存在", "code": "not_found"}
        result = []
        for point in points:
            key = _ts_key(point)
            owner = self._point_owner(replay_id, key) if key is not None else None
            result.append({
                "point": str(point),
                "claimed": owner is not None,
                "sheet_id": owner["sheet_id"] if owner else None,
                "reviewer": owner["reviewer"] if owner else None,
                "submitted": owner["submitted"] if owner else False,
            })
        return {"replay_id": replay_id, "points": result}

    def report_claim_scope(self, report_id: int) -> dict[str, Any]:
        """既有质检报告按当时的领用范围回查，后续授权更正不影响该快照。"""
        report = store.find("series_quality", report_id)
        if report is None:
            return {"error": f"质量报告 {report_id} 不存在", "code": "not_found"}
        linked = bool(report.get("复核单号"))
        return {
            "报告编号": report.get("报告编号"),
            "测点编号": report.get("测点编号"),
            "质检结论": report.get("质检结论"),
            "关联复核": linked,
            "复核单号": report.get("复核单号"),
            "重采编号": report.get("重采编号"),
            "领用范围": report.get("领用范围") or report.get("统计窗口"),
            "领用点数": report.get("领用点数"),
            "领用明细": report.get("领用明细", []),
            "复核员": report.get("复核员"),
        }

    # ------------------------------------------------------------------
    # 复核单内部工具
    # ------------------------------------------------------------------

    def _taken_points(self, replay_id: int) -> set[float]:
        """该申请下已被任意复核单领取（含已提交）的时刻点。"""
        keys: set[float] = set()
        for sheet in self._review_sheets.values():
            if sheet["replay_id"] == replay_id:
                keys.update(_ts_key(t) for t in sheet["claimed"])
        keys.discard(None)
        return keys

    def _point_owner(self, replay_id: int, ts_key: float | None) -> dict[str, Any] | None:
        if ts_key is None:
            return None
        for sheet in self._review_sheets.values():
            if sheet["replay_id"] != replay_id:
                continue
            if ts_key in {_ts_key(t) for t in sheet["claimed"]}:
                return {
                    "sheet_id": sheet["sheet_id"],
                    "reviewer": sheet["reviewer"],
                    "submitted": ts_key in {_ts_key(t) for t in sheet["submitted"]},
                }
        return None

    def _sheet_view(self, sheet: dict[str, Any]) -> dict[str, Any]:
        replay = store.find(self.MODULE, sheet["replay_id"])
        auth = self._review_auth.get(sheet["replay_id"])
        claimed_sorted = sorted(sheet["claimed"], key=_sort_key)
        submitted_sorted = sorted(sheet["submitted"], key=_sort_key)
        submitted_keys = {_ts_key(t) for t in sheet["submitted"]}
        pending, withdrawn = [], []
        for ts in claimed_sorted:
            key = _ts_key(ts)
            if key in submitted_keys:
                continue
            if auth is not None and self._approved_now(auth, key):
                pending.append(ts)
            else:
                # 授权被撤回但未提交：单独列出，不能误计作已完成
                withdrawn.append(ts)
        if not sheet["submitted"]:
            status = "复核中"
        elif pending or withdrawn:
            status = "部分提交"
        else:
            status = "已完成"
        return {
            "sheet_id": sheet["sheet_id"],
            "replay_id": sheet["replay_id"],
            "重采编号": sheet["重采编号"],
            "测点编号": sheet["测点编号"],
            "reviewer": sheet["reviewer"],
            "status": status,
            "claimed": claimed_sorted,
            "submitted": submitted_sorted,
            "pending": pending,
            "withdrawn_unsubmitted": withdrawn,
            "remaining_ranges": self._remaining_ranges(replay, pending),
            "reports": list(sheet["reports"]),
        }

    def _remaining_ranges(self, replay: dict[str, Any] | None,
                          pending: list[str]) -> list[dict[str, Any]]:
        """把尚待复核的时刻点按数据点相邻关系归并为剩余时段。"""
        if not pending:
            return []
        records = self._load_replay_records(replay)
        sequence = sorted({rec["ts_key"] for rec in records})
        index = {key: pos for pos, key in enumerate(sequence)}
        label = {rec["ts_key"]: rec["timestamp"] for rec in records}
        for ts in pending:
            label.setdefault(_ts_key(ts), ts)
        ranges: list[list[float]] = []
        for key in sorted({_ts_key(t) for t in pending}):
            if ranges and index.get(key, -1) == index.get(ranges[-1][-1], -2) + 1:
                ranges[-1].append(key)
            else:
                ranges.append([key])
        return [
            {
                "start": label[group[0]],
                "end": label[group[-1]],
                "points": [label[key] for key in group],
            }
            for group in ranges
        ]

    def _create_quality_report(self, sheet: dict[str, Any],
                               points: list[str]) -> dict[str, Any]:
        """按实际领用范围出具质量报告，并写入时序质量模块与报告数据文件。"""
        replay = store.find(self.MODULE, sheet["replay_id"])
        point_set = set(points)
        records = [
            rec for rec in self._load_replay_records(replay)
            if rec["timestamp"] in point_set
        ]
        seen: set[str] = set()
        duplicates = 0
        out_of_range = 0
        for rec in records:
            if rec["timestamp"] in seen:
                duplicates += 1
            seen.add(rec["timestamp"])
            try:
                value = float(rec["value"])
            except (TypeError, ValueError):
                value = 0.0
            if abs(value) > 1000:
                out_of_range += 1
        completeness = round(len(seen) / max(len(points), 1), 2)
        if completeness > 0.95 and duplicates == 0 and out_of_range == 0:
            conclusion = "合格"
        elif completeness > 0.7:
            conclusion = "待优化"
        else:
            conclusion = "不合格"
        sorted_points = sorted(points, key=_sort_key)
        scope = f"{sorted_points[0]}~{sorted_points[-1]}"
        rows = store.rows("series_quality")
        report_id = max((int(r.get("id", 0)) for r in rows), default=0) + 1
        entry = {
            "id": report_id,
            "报告编号": f"QUALITY-{sheet['测点编号']}-{report_id}",
            "测点编号": sheet["测点编号"],
            "统计窗口": scope,
            "完整率": f"{completeness:.2f}",
            "重复点数": str(duplicates),
            "越界点数": str(out_of_range),
            "校验规则": "完整性+重复+越界",
            "质检结论": conclusion,
            "status": "已出报告",
            "pending": False,
            "abnormal": False,
            # 领用范围快照：按提交时的实际领用范围关联，后续授权更正不影响
            "复核单号": sheet["sheet_id"],
            "重采编号": sheet["重采编号"],
            "领用范围": scope,
            "领用点数": str(len(points)),
            "领用明细": sorted_points,
            "复核员": sheet["reviewer"],
        }
        rows.append(entry)
        self._write_jsonl(
            self._data_dir("quality_reports") / f"{entry['报告编号']}_quality.jsonl",
            [{
                "report_id": entry["报告编号"],
                "point": sheet["测点编号"],
                "sheet_id": sheet["sheet_id"],
                "replay_id": sheet["重采编号"],
                "claim_scope": scope,
                "claimed_points": sorted_points,
                "completeness": completeness,
                "duplicates": duplicates,
                "out_of_range": out_of_range,
                "conclusion": conclusion,
                "checked_at": time.time(),
            }]
        )
        return entry
