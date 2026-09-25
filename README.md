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
| 证据封存包 | 封存建立含全部照片/文件摘要/元数据/候选判定/整改/当前处罚版本的**不可变清单**；补拍/整改/升级/更正不改旧包，只产生显式关联的补充包/替代包；同一处罚至多一个活动包；文件摘要不符只标校验异常；可导出 tar 离线校验并还原唯一处罚 |

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
| `GET /api/penalties/{id}/` | 完整追溯：事件、承包商、版本链、升级、复核、证据 |
| `POST /api/seals/` | 封存（传 `event` 或 `penalty`；`client_token` 幂等；`finalize=false` 只建待封存包） |
| `POST /api/seals/{id}/finalize/` | 完成待封存(pending)包，固化不可变清单 |
| `POST /api/seals/{id}/supplement/` / `replace/` | 新增补拍/整改/升级/更正后建立补充包/替代包（显式关联旧包，旧包不可变） |
| `POST /api/seals/{id}/verify/` | 在线校验：复算清单与文件摘要，失败只标 `verification_failed` |
| `POST /api/seals/{id}/exports/` | 创建/续作导出任务（`client_token` 幂等，可中断续作），产出离线 tar |
| `GET /api/seals/{id}/exports/{job_id}/download/` | 下载离线封存 tar（内含纯标准库 `verify_offline.py`） |
| `POST /api/seals/offline-verify/` | 上传 tar 离线校验并还原唯一处罚与完整证据视图 |
| `GET /api/seals/{id}/lineage/` | 封存谱系追溯（首包 → … → 当前活动包） |
| `POST /api/seals/migrate/` | 历史数据迁移（也可 `python manage.py migrate_seals`），幂等可续跑 |
| `GET/POST /api/seal-exports/{id}/run/` | 导出任务查询与中断后续作 |
| `/api/grids/` `/api/contracts/` `/api/events/` `/api/penalty-versions/` `/api/rectifications/` | 基础数据只读/维护 |

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

封存相关用例（`assessment/tests/test_seals.py`）覆盖验收点：
正常封存→导出→**离线校验（含脱离 Django 的纯标准库校验器）**并还原唯一处罚与完整证据；
更正/整改/复核只产生显式关联的**补充包/替代包**，旧包摘要与内容不变；
重复封存与 6 线程并发只产生一个活动包；
文件篡改→`verification_failed`、业务链/候选不受影响，文件恢复后复校回 `sealed`，文件缺失原链仍可追溯；
封存时文件已缺失也可封存并留痕；
历史迁移分批/幂等、不产生第二个活动包；
导出中断（`fail_after`）后同 token 续作只产出一个 bundle；
跨地点同图候选（照片分属不同事件）不混入同一封存包；pending/finalize 流程；OpenAPI 端点。

## 封存包不可变语义

```
SealPackage(initial, sealed, is_active=True)
   │  补拍/整改/升级/更正后再封存
   ▼
旧包 status=supplemented（内容/摘要原样保留, is_active=False）
SealPackage(supplement, sealed, is_active=True, parent=旧包)
   │  显式替代
   ▼
旧包 status=superseded（replaces 指针）
```

* 状态：`pending`（待封存）/ `sealed`（已封存）/ `supplemented`（已补充,历史包）/
  `verification_failed`（校验失败,仍是活动包）/ `superseded`（已替代,历史包）。
* 并发安全：`select_for_update` 串行化 + `WHERE is_active` 的部分唯一索引兜底。
* 离线包：`manifest.json`（canonical JSON，sort_keys）+ `manifest.json.sha256` + `files/photos/` +
  无依赖的 `verify_offline.py`，退出码 0/1 即监督复核结论。

## 目录

```
sanitation/settings.py          # PostGIS、drf-spectacular、业务阈值
assessment/
  models.py                     # 网格/合同/事件/照片/候选/整改/处罚/版本/升级/复核/封存包
  services/
    phash.py                    # Pillow 感知哈希
    duplicates.py               # 仅按 pHash 生成候选
    attribution.py              # 发生时合同归属（PostGIS 空间查询）
    events.py / rectification.py / penalties.py / escalation.py / decisions.py
    seal_manifest.py            # canonical JSON + sha256（封存/离线共用同一算法）
    seal_snapshot.py            # 不可变清单快照装配（照片/元数据/候选/整改/版本/处罚依据）
    sealing.py                  # 封存、补充/替代、并发去重、历史迁移
    seal_verify.py              # 在线校验、离线 tar 校验、唯一处罚还原
    seal_export.py              # 可中断续作的 tar 导出（内含离线校验器）
    clock.py                    # SystemClock / FixedClock / OffsetClock
  mockimages.py                 # 5 张确定性模拟图片
  management/commands/          # generate_mock_images / seed_demo / migrate_seals
  tests/test_api.py             # 端到端测试
  tests/test_seals.py           # 封存/离线校验/迁移/导出中断/并发测试
docs/openapi.{json,yml}
```
