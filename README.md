# 工业传感器时序采集与降采样平台

面向工业传感器高频时序数据的测点接入、边缘缓冲上送、单位标定、时间对齐、降采样归并与分层归档的采集平台。

这是一个前后端分离的管理平台：前端 Vue 3 + Vite + TypeScript，后端 FastAPI（Python）。
两边各自独立启动，前端 dev server 已关掉自动打开页面，启动后按终端打印的地址手工打开。

## 目录结构

```text
.
├── frontend/                 Vue 3 + Vite + TypeScript 前端
│   ├── src/views/WorkbenchView.vue|tsx  全部模块共享工作台
│   ├── src/data/modules.ts              18个业务模块配置
│   ├── src/api/              统一请求封装
│   └── vite.config.ts        dev server 配置（open: false）
├── backend/                  FastAPI（Python） 后端
│   ├── app/routers/resource.py  通用查询/导出/登记/动作接口
│   ├── app/services/<key>.py    每个模块独立领域逻辑
│   ├── app/services/base.py     共享服务骨架
│   └── app/store.py          内存数据仓库与示例数据
├── .gitignore
└── docker-compose.yml
```

## 启动

### 后端

```bash
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
./run.sh
```

健康检查：`curl http://127.0.0.1:8000/api/health`

### 前端

```bash
cd frontend
npm ci
npm run dev
```

前端默认监听 `http://127.0.0.1:5173/`，dev server 不会自动打开浏览器，
需要自己访问。`/api` 由 vite 代理到后端 `http://127.0.0.1:8000`。

## 业务模块

| 模块 | 目录 | 业务对象 | 主要字段 |
| --- | --- | --- | --- |
| 测点台账 | `sensor_point` | 测点档案 | 测点编号、所属产线、传感器型号 |
| 边缘缓冲 | `edge_buffer` | 缓冲分片 | 分片编号、测点编号、起始时刻 |
| 批量上送 | `upload_batch` | 上送批次 | 批次编号、分片范围、测点数量 |
| 单位标定 | `unit_calibrate` | 标定记录 | 标定编号、测点编号、原始单位 |
| 时间对齐 | `time_align` | 对齐规则 | 规则编号、对齐粒度、时区基准 |
| 断点填补 | `gap_fill` | 填补区间 | 区间编号、测点编号、缺失起点 |
| 尖峰剔除 | `spike_filter` | 异常采样 | 异常编号、测点编号、采样时刻 |
| 降采样 | `downsample` | 降采样任务 | 任务编号、源序列、源采样频率 |
| 窗口归并 | `rollup_window` | 归并窗口 | 窗口编号、测点编号、窗口类型 |
| 序列索引 | `series_index` | 索引分区 | 索引编号、测点编号、时间分区 |
| 冷热分层 | `tier_storage` | 分层策略 | 策略编号、时间边界、热区范围 |
| 采集心跳 | `device_health` | 心跳记录 | 心跳编号、采集终端、上报时刻 |
| 接入水位 | `ingest_watermark` | 水位标记 | 水位编号、测点编号、事件时间水位 |
| 区间重采 | `range_replay` | 重采任务 | 重采编号、测点编号、起始时刻 |
| 采集血缘 | `series_lineage` | 血缘链路 | 链路编号、测点编号、上送批次 |
| 序列导出 | `series_export` | 导出任务 | 导出编号、测点范围、时间范围 |
| 时序质量 | `series_quality` | 质量报告 | 报告编号、测点编号、统计窗口 |
| 序列快照 | `series_snapshot` | 版本快照 | 快照编号、口径说明、测点范围 |

## 约定

- 全部模块共享`frontend/src/views/WorkbenchView.vue|tsx`和一套资源路由。
- 每个模块仍保留独立`backend/app/services/<模块>.py`；纯前端保留
  `frontend/src/domain/<模块>.ts`，用于承载真实业务差异并维持36题位容量。
- 列表接口统一返回 `{ items, total, page, size }`，动作接口统一返回 `{ ok, message }`。
- 状态流转只允许在 `app/services` 里改，路由层不做业务判断。

## 区间重采复核领取

区间重采在通用资源接口之外，还提供把获批片段交给质量复核人员的专用接口
（`backend/app/routers/replay_review.py`，挂在 `/api/range_replay/review` 下）：

- `POST /permissions/grant|revoke`、`GET /permissions`：维护复核员的负责测点，
  按实际复核权限核验数据归属；越权人员只能查看自己的申请状态，不能改动他人的领取结果。
- `POST /{replay_id}/publish`、`POST /{replay_id}/correct`、`GET /{replay_id}/scope`：
  复核授权发布与多次更正，历史版本全部留痕；授权变化不会把未提交的区间误计作已完成。
- `GET /data?point=&start=&end=&reviewer=`：按负责测点和时段读取重采数据，
  结果区分「正式质检 / 需申请复核 / 未批准」三类并附带领取归属。
- `POST /{replay_id}/claim`、`POST /sheets/{sheet_id}/claim-next`：领取获批片段、
  同一张复核单继续取下一批；已提交或已被他人领取的点不能再领。
- `POST /sheets/{sheet_id}/submit`：提交已完成的那部分区间，系统据此生成尚待复核的
  剩余时段，并出具质量报告按提交时的实际领用范围关联。
- `GET /reports/{report_id}/scope`：既有质检报告按当时的领用范围回查。

领域：数据工程
子方向：数据采集、爬虫与RPA
