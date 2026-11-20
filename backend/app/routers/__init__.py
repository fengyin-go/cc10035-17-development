"""业务路由汇总：所有模块共用一套资源路由，由领域服务完成分派。"""
from __future__ import annotations

from app.routers import probe_trial, resource

# 固定前缀的探针试跑路由必须先注册，避免被 /api/{module}/{entry_id} 误匹配。
ROUTERS = [probe_trial, resource]
