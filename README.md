# Web语义抽取与内容理解中心

面向Web结构化数据采集场景，提供页面DOM解析、实体识别、关系抽取、内容分类和知识图谱构建的语义抽取平台

这是一个前后端分离的管理平台：前端 Vue 3 + Vite + TypeScript，后端 FastAPI（Python）。
两边各自独立启动，前端 dev server 已关掉自动打开页面，启动后按终端打印的地址手工打开。

## 目录结构

```text
.
├── frontend/                 Vue 3 + Vite + TypeScript 前端
│   ├── src/views/WorkbenchView.vue|tsx  全部模块共享工作台
│   ├── src/views/ProbeTrialView.vue     探针试跑（数据包/试跑/核销）
│   ├── src/data/modules.ts              18个业务模块配置
│   ├── src/api/              统一请求封装
│   └── vite.config.ts        dev server 配置（open: false）
├── backend/                  FastAPI（Python） 后端
│   ├── app/routers/resource.py  通用查询/导出/登记/动作接口
│   ├── app/routers/probe_trial.py 探针试跑数据包与核销接口
│   ├── app/services/<key>.py    每个模块独立领域逻辑
│   ├── app/services/probe_package.py     数据包编解码/校验/原子存储
│   ├── app/services/probe_trial.py       候选、采样额度、试跑与回写
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
| DOM解析 | `dom_parser` | 解析配置 | 配置名称、目标URI、解析引擎 |
| 实体抽取 | `entity_extract` | 实体条目 | 实体ID、实体类型、实体名称 |
| 关系图谱 | `relation_graph` | 关系边 | 边ID、源实体、目标实体 |
| 内容分类 | `content_classifier` | 分类标签 | 标签ID、页面URI、一级分类 |
| 模板规则 | `template_rule` | 规则模板 | 模板名称、适用站点、提取字段 |
| 文本分块 | `text_segment` | 文本块 | 块ID、来源页面、块类型 |
| 情感标注 | `sentiment_tag` | 情感记录 | 记录ID、文本片段、情感倾向 |
| Schema对齐 | `schema_align` | 对齐规则 | 规则名称、源Schema、目标Schema |
| 种子调度 | `feed_scheduler` | 调度任务 | 任务名称、URL种子、抓取深度 |
| 质量检查 | `quality_check` | 检查规则 | 规则名称、检查字段、检查类型 |
| 探针选择 | `probe_selector` | 探针配置 | 探针名称、目标页面、选择器列表 |
| 探针试跑 | `/probe-trial` | 试跑数据包 | 数据包标识、模板快照、候选清单 |
| 页面快照 | `page_snapshot` | 快照记录 | 快照ID、页面URI、DOM哈希 |
| 抽取指标 | `extract_metric` | 指标记录 | 记录时间、所属任务、成功页面数 |
| 语义结果 | `nlu_result` | 语义记录 | 记录ID、输入文本、意图标签 |
| 知识链接 | `knowledge_link` | 链接记录 | 链接ID、源实体、外部URI |
| 输出目标 | `output_target` | 输出配置 | 配置名称、输出类型、目标地址 |
| 抽取工坊 | `extract_studio` | 交互任务 | 任务名称、示例页面、标注区域 |
| 聚合视图 | `aggregate_view` | 视图配置 | 视图名称、统计维度、聚合字段 |

## 探针试跑数据包

模板规则只负责“建议探针”，采集端要真正试跑还需要一份可下载、可校验的交付物。
`/api/probe-trial` 与前端「探针试跑」页面负责这条链路：

1. **生成**：勾选模板 → `POST /api/probe-trial/packages` 生成 ZIP 并原子落盘，
   `GET /api/probe-trial/packages/{id}/download` 下载。包内文件：

   | 文件 | 内容 |
   | --- | --- |
   | `manifest.json` | 本轮交付声明：模板快照、可试跑/待验证候选、采样额度冲突、文件校验和 |
   | `template.json` | 打包时刻的模板快照，可还原当时模板与每个选择器 |
   | `candidates.json` | 字段候选清单，`candidate_id` 跨包稳定 |
   | `probes.json` | 同一批候选的旧版探针写法，兼容历史消费方 |
   | `SHA256SUMS` | 除 `manifest.json` 外每个文件的 SHA-256 |

2. **额度**：同一站点同一样本页每轮只有一个采样额度。额度按模板状态
   （已发布 > 已验证 > 草稿 > 已失效）分配，未拿到额度的模板候选仍写进包里，
   状态为「待验证」并注明原因，下一轮再试。
3. **试跑**：`POST /api/probe-trial/runs`（可传 `package_id` 或 `package_base64`）
   读取包 → 按样本页触发 `page_snapshot` → 结论回写字段候选清单
   （`GET /api/probe-trial/field-candidates`）。快照带 `provenance`，
   `GET /api/probe-trial/snapshots/{id}/provenance` 会重算哈希并回查包文件，
   可在独立试跑中核对快照来源。
4. **核销**：`GET /api/probe-trial/templates/{id}/verification` 给出每个选择器的
   试跑状态，全部通过后 `POST /api/probe-trial/templates/{id}/reconcile` 把
   「已核销」写回模板规则。旧包试跑不会核销新版本模板，报告中会提示
   “只有旧版数据包的试跑结论”。

**完整性**：包在内存里编码完成后才写临时文件并 `os.replace` 到正式路径，
另写一份校验和旁车文件；读取时必须两者都存在且 SHA-256、大小一致。
打包中断后按同一 `package_id` 调 `POST /api/probe-trial/packages/{id}/rebuild`
重试，半成品永远不会被读取。

**兼容**：解码器同时接受当前 ZIP 包、1.0 版 ZIP 包和早期直接交付的 JSON 文档；
历史中文字段名（`探针名称/目标页面/选择器列表/采样页面` 等）通过别名表映射到
`field/selector/sample_page`，没有状态的旧候选按「可试跑」处理并打上 `legacy` 标记。

## 约定

- 全部模块共享`frontend/src/views/WorkbenchView.vue|tsx`和一套资源路由。
- 每个模块仍保留独立`backend/app/services/<模块>.py`；纯前端保留
  `frontend/src/domain/<模块>.ts`，用于承载真实业务差异并维持36题位容量。
- 列表接口统一返回 `{ items, total, page, size }`，动作接口统一返回 `{ ok, message }`。
- 状态流转只允许在 `app/services` 里改，路由层不做业务判断。

领域：数据工程
子方向：数据采集、爬虫与RPA
