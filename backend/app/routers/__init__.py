"""业务路由汇总：复核领取等专用路由先于通用资源路由注册，避免动态路径抢先匹配。"""
from __future__ import annotations

from app.routers import replay_review, resource

ROUTERS = [replay_review, resource]
