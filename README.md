# 街道环卫考核 API

Django REST Framework + Pillow(pHash) + PostgreSQL/PostGIS 实现的纯 API 服务（无界面）。

## 设计原则（对应考核规则）

| 考核要求 | 实现方式 |
| --- | --- |
| 同一现场问题不能多次扣分 | 一个事件 `ProblemEvent` 对应唯一处罚单元 `PenaltyUnit`（1:1）；不同角度照片经**人工判定**后挂接同一事件，不新增扣分 |
| 相似照片不能自动合并不同地点 | pHash 只生成 `DuplicateCandidate`（pending 候选）；**候选生成阶段刻意不用位置/时间**；位置、时间只在人工 `decide` 时作为依据。误传同图可判 `different` 后分别立案 |
| 整改后复发是新事件 | 整改后复发照片判定 `create_new` → 新建事件、新建处罚单元；候选标记为 `recurrence`；向已整改事件挂接会被拒绝（400） |
| 扣分归属按发生时合同 | 立案时按 `occurred_at ∈ [valid_from, valid_to)` 且点在网格内解析合同，快照到事件/处罚单；与录入时间无关。无合同 422、区间重叠 409 |
| 逾期升级基于可注入时钟 | `POST /api/escalations/run/` body 传 `now`（或服务层传 `Clock`）；`FixedClock`/`OffsetClock` 支持重放，同刻重放幂等 |
| 复核锁定、更正只能追加 | 复核通过把 `locked_version` 指向当前版本；更正/升级一律 `PenaltyVersion` append-only，历史行不可改，追加后重新待复核 |
| 每笔扣分可追溯 | `penalty_no` 唯一 → 事件、责任合同/承包商、版本链、升级记录、复核记录、全部证据照片（含经纬度/拍摄时间/pHash） |
| 证据保全 | 照片只可上传/查询，不提供修改、删除（405）；处罚/版本只读 + 专用动作端点 |
| 证据封存包 | 一键把某笔扣分的全部证据（照片文件 SHA-256、位置时间、候选判定、整改记录、当前处罚版本）固化为不可变清单；封存后的补拍/整改/升级/更正只生成显式关联的补充/替代包，旧包一字不改；可导出 ZIP 离线校验 |

pHash：Pillow 实现的 64 位 DCT 感知哈希（`assessment/services/phash.py`，仅依赖 Pillow）。

## 快速启动

### 方式 A：docker compose（PostGIS 镜像）

```bash
docker compose up --build
# OpenAPI: http://localhost:8000/api/schema/
```

### 方式 B：本地用户态（无 root，脚本自动装 PostgreSQL+PostGIS+GDAL）

```bash
./run_local.sh        # 起库、迁移、生成模拟图片、runserver
python manage.py seed_demo   # 另开终端：写演示网格/合同/照片
```

### 方式 C：已有 PostgreSQL/PostGIS

```bash
pip install -r requirements.txt
export DB_HOST=... DB_PORT=5432 DB_NAME=... DB_USER=... DB_PASSWORD=...
python manage.py migrate
python manage.py generate_mock_images   # 模拟图片到 media/mock_images/，并打印 pHash 距离矩阵
python manage.py runserver
```

## OpenAPI

* 在线：`GET /api/schema/`（JSON；加 `?format=yaml` 得 YAML）
* 静态导出：[`docs/openapi.json`](docs/openapi.json)、[`docs/openapi.yml`](docs/openapi.yml)
* 重新导出：`python manage.py spectacular --file docs/openapi.yml`

## 主要端点

| 方法 路径 | 说明 |
| --- | --- |
| `POST /api/photos/` | multipart 上传：`image` + `lng/lat` + `captured_at`；服务端算 pHash 并生成疑似候选 |
| `GET /api/candidates/?status=pending` | 疑似重复候选（含双方位置、拍摄时间、汉明距离） |
| `POST /api/candidates/{id}/decide/` | `attach`（同问题挂接，不扣分）/ `create_new`（复发或不同，另立案）/ `different` / `rejected` |
| `POST /api/photos/{id}/create_event/` | 对照片直接立案（无候选或判 different 后） |
| `POST /api/events/{id}/rectify/` | 整改回调（可注入 `now`）；重复回调 409 |
| `POST /api/escalations/run/` | 逾期扫描（可注入 `now`），返回新建升级数；幂等 |
| `POST /api/penalties/{id}/review/` | 复核，`approved=true` 锁定当前版本 |
| `POST /api/penalties/{id}/correct/` | 人工更正：只追加一个 correction 版本 |
| `GET /api/penalties/{id}/` | 完整追溯：事件、承包商、版本链、升级、复核、证据、封存链 |
| `POST /api/penalties/{id}/seal/` 或 `POST /api/events/{id}/seal/` | 证据封存：建立首个封存包；幂等，重复/并发请求只得到一个活动包 |
| `POST /api/packages/{id}/supplement/` `/replace/` | 封存后发生变化时生成补充/替代包（显式关联 parent，旧包置为已补充/已替代） |
| `POST /api/packages/{id}/verify/` | 在线校验：重算清单哈希与文件摘要；不符只标记“校验失败”，不动业务链 |
| `GET /api/packages/{id}/export/` | 导出封存包 ZIP（确定性字节，可重试）；文件缺失/摘要不符 409 |
| `POST /api/packages/verify_offline/` | 离线校验：上传导出的 ZIP，不依赖数据库还原唯一处罚与完整证据 |
| `/api/grids/` `/api/contracts/` `/api/events/` `/api/penalty-versions/` `/api/rectifications/` `/api/packages/` | 基础数据只读/维护 |

## 证据封存包（EvidencePackage）

监督复核需要证明“某笔扣分的照片、位置时间、人工判定、整改和处罚依据未被篡改”。
封存包把某一时刻的完整证据链固化为**不可变清单（manifest）**：

* **清单内容**：全部关联照片（含文件 SHA-256/大小、经纬度、拍摄时间、pHash）、
  事件与处罚关键元数据（含合同/承包商快照）、候选判定、整改记录、当前处罚版本；
  清单规范化 JSON 的 SHA-256 即 `manifest_hash`。
* **取证边界**：只收本事件名下的照片文件；跨地点同图的候选只以 id 引用对方照片，
  绝不把其他事件的文件混入本包。
* **状态机**：`pending` 待封存 → `sealed` 已封存 →（派生后继后）`superseded` 已补充/已替代；
  校验发现文件摘要不符 → `verify_failed` 校验失败（修复后重新校验可恢复）。
* **不变性**：封存后发生补拍、整改、升级、更正，旧包一个字节都不改，
  只能 `supplement`/`replace` 生成显式关联（`parent`）的新包；每个新包都是完整快照，
  可独立离线校验，与父包的摘要差异即变更/篡改痕迹。
* **唯一活动包**：同一处罚单元至多一个 `pending/sealed` 包（数据库部分唯一约束 +
  行锁串行化），重复封存与并发请求只会得到同一个活动包。
* **校验只动包自身**：`verify` 只重写封存包的 `status/verify_report`，
  绝不回写事件、处罚或候选判定；文件损坏/缺失时原链（事件→处罚→封存链）仍可追溯。
* **导出/离线校验**：`export` 产出确定性 ZIP（`manifest.json` + `manifest.sha256` +
  全部证据文件），中断重试字节一致；`verify_offline` API 或纯标准库脚本
  `docs/verify_evidence_package.py` 可在无数据库环境下校验并还原唯一处罚单号与证据清单：

  ```bash
  python3 docs/verify_evidence_package.py EP-XXXXXXXXXXXX.zip   # 退出码 0=通过
  ```
* **旧数据迁移**：迁移 `0004` 自动为既有处罚单元补建原始封存包；
  也可随时重跑 `python manage.py seal_existing_packages`（幂等，已有封存链的跳过）。
  封存时文件不可读的，清单如实记录 `readable=false`，后续校验会标记为校验失败。

## 典型流程（三个关键例子）

模拟图片（`python manage.py generate_mock_images`）及 pHash 距离：

```
scene_a_angle1（首报）
scene_a_angle2    距离 0   —— 不同角度
scene_a_repost    距离 2   —— 整改后复发
scene_a_elsewhere_copy 距离 0 —— 与首报逐像素相同，但坐标在 3km 外
scene_c_bins      距离 28  —— 明显不同现场（不产生候选）
```

1. **同图跨地点误传**：上传远处同图 → 出现候选 → 监督员核对坐标 `121.51,31.26` 与首报不符 →
   `decide=different` → 现场核实属实后 `create_event` → 归远处网格的丙公司，独立处罚单号。
2. **同地点复发**：首报事件 `rectify` → 复发照上传 → 候选 `decide=create_new`
   → 候选变 `recurrence`、产生第二事件与第二处罚单；向已整改事件 attach 会被拒绝。
3. **重复整改回调**：对同一事件第二次 `rectify` → 409，扣分不变。

## 测试

```bash
python manage.py test assessment -v 2
```

15 个用例（真实 PostGIS 测试库，迁移自动 `CREATE EXTENSION postgis`）：
pHash 距离、完整业务流（误传/复发/挂接/重复整改/历史归属/无合同/证据保全/追溯）、
注入时钟升级 + 复核锁定 + 追加更正、合同重叠 409、OpenAPI schema，
以及证据封存包验收（离线校验还原唯一处罚、更正只产补充包、重复/并发封存仅一个活动包、
文件损坏缺失时原链可追溯、旧数据迁移、导出中断重试、跨地点同图候选不混包）。

## 目录

```
sanitation/settings.py          # PostGIS、drf-spectacular、业务阈值
assessment/
  models.py                     # 网格/合同/事件/照片/候选/整改/处罚单元/版本/升级/复核/封存包
  services/
    phash.py                    # Pillow 感知哈希
    duplicates.py               # 仅按 pHash 生成候选
    attribution.py              # 发生时合同归属（PostGIS 空间查询）
    events.py / rectification.py / penalties.py / escalation.py / decisions.py
    sealing.py                  # 证据封存：清单/封存/补充替代/校验/导出/离线校验/回填
    clock.py                    # SystemClock / FixedClock / OffsetClock
  mockimages.py                 # 5 张确定性模拟图片
  management/commands/          # generate_mock_images / seed_demo / seal_existing_packages
  tests/test_api.py             # 业务流端到端测试
  tests/test_sealing.py         # 证据封存包验收测试（含 8 线程并发封存）
docs/openapi.{json,yml}
docs/verify_evidence_package.py # 封存包离线校验脚本（纯标准库）
```
