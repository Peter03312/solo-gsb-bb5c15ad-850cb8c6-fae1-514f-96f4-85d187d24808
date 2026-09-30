# 食品工厂批次追溯服务

面向小型食品加工厂的**本地运行**批次追溯服务。登记原料批次、记录一次生产的多批次投入与单一产出、
查询批次余量与上下游谱系，并支持疑似污染批次的后代追踪与整批隔离。

- 技术栈：Python 3.11 + FastAPI + SQLAlchemy 2 + PostgreSQL（关系数据库）
- 数量使用 `NUMERIC`/`Decimal`（最多 3 位小数），序列化为字符串，杜绝浮点误差
- 生产提交在**单事务**内完成，投入批次排序加行锁（PostgreSQL）+ 条件原子扣减，
  并发提交不会超用同一批次
- 生产接口支持**幂等键**：同一请求标识与内容重试返回原结果；同标识异内容冲突拒绝
- 数据卷持久化，服务重启数据不丢

---

## 一、一键启动（Docker Compose，推荐）

前置条件：已安装 Docker 与 Docker Compose v2。

```bash
docker compose up -d --build
```

启动时会自动：

1. 拉取并启动 PostgreSQL 16；
2. 在应用镜像中安装 Python 依赖；
3. 等待数据库健康就绪，**自动建库建表**（应用启动时幂等执行）。

查看状态 / 日志 / 停止：

```bash
docker compose ps
docker compose logs -f api
docker compose down            # 停止（保留数据卷）
docker compose down -v         # 停止并删除数据卷（清空全部数据）
```

> 首次启动若数据库初始化较慢，API 会以 2 秒间隔重试等待最多约 60 秒。

## 二、不使用 Docker 本地运行

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 方式 A：连接已有的 PostgreSQL（需先建库或让启动逻辑自动建）
export TRACE_DATABASE_URL='postgresql+psycopg2://trace:trace@localhost:5432/trace'

# 方式 B：零外部依赖，直接用本地 SQLite 试用
export TRACE_DATABASE_URL='sqlite+pysqlite:///./trace.db'

uvicorn app.main:app --host 0.0.0.0 --port 8080
```

---

## 三、服务地址与端口

| 项目 | 值 |
| --- | --- |
| 服务地址 | `http://localhost:8080` |
| 健康检查 | `GET http://localhost:8080/health` |
| 交互式 API 文档（Swagger） | `http://localhost:8080/docs` |
| OpenAPI 描述 | `http://localhost:8080/openapi.json` |

## 四、配置（环境变量，前缀 `TRACE_`）

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `TRACE_DATABASE_URL` | `postgresql+psycopg2://trace:trace@db:5432/trace` | SQLAlchemy 数据库连接串 |
| `TRACE_HOST` | `0.0.0.0` | 监听地址 |
| `TRACE_PORT` | `8080` | 监听端口 |
| `TRACE_MAX_INPUTS` | `500` | 单笔生产允许的最大投入批次数 |

可复制 `.env.example` 为 `.env` 后修改。Compose 中的数据库账号/密码在 `docker-compose.yml`
的 `db.environment` 与 `TRACE_DATABASE_URL` 中保持一致，按需一起修改。

---

## 五、HTTP 调用样例

以下 `curl` 可直接执行。数量以**字符串**传 Decimal（也接受 JSON 数字），单位全链路必须一致；
错误响应统一为 `{"error":{"code": "...", "message": "..."}}`。

### 1. 登记原料批次

```bash
curl -s -X POST http://localhost:8080/api/v1/batches \
  -H 'Content-Type: application/json' \
  -d '{"batch_no":"RM-A","qty":"100.000","unit":"kg","note":"面粉"}'

curl -s -X POST http://localhost:8080/api/v1/batches \
  -H 'Content-Type: application/json' \
  -d '{"batch_no":"RM-B","qty":"50.000","unit":"kg","note":"糖"}'
```

批次编号全局唯一、不可复用（原料号与产出号共用同一编号空间），重复登记返回 `409 BATCH_NO_EXISTS`。

### 2. 提交生产（多投入批次 -> 一个产出批次）

```bash
curl -s -X POST http://localhost:8080/api/v1/productions \
  -H 'Content-Type: application/json' \
  -d '{
    "request_id": "REQ-20260930-001",
    "output_batch_no": "FG-1",
    "output_qty": "80.000",
    "unit": "kg",
    "inputs": [
      {"batch_no": "RM-A", "used_qty": "60.000"},
      {"batch_no": "RM-B", "used_qty": "30.000"}
    ]
  }'
```

业务规则（任一不满足则**整笔拒绝、余量不变**，HTTP 422）：

- 所有数量为正数，投入批次在同一请求中不重复；
- 各投入批次与产出的**单位一致**（`UNIT_MISMATCH`）；
- 投入批次必须存在（`SOURCE_NOT_FOUND`）；
- 各用量不得超过该批次余量（`INSUFFICIENT_BALANCE`）；
- 产出量不得超过投入总量（`OUTPUT_EXCEEDS_INPUT`）；
- 产出批次编号不得与已有编号复用（`409 BATCH_NO_EXISTS`）；
- 被隔离批次不得再投料（`BATCH_QUARANTINED`）。

**幂等重试**：用完全相同的 `request_id` 与请求体重放，返回首次结果且
`idempotent_replayed=true`，余量只扣一次；同一 `request_id` 但内容不同，返回
`409 IDEMPOTENCY_CONFLICT`。

```bash
# 同标识同内容重试：返回原结果
curl -s -X POST http://localhost:8080/api/v1/productions \
  -H 'Content-Type: application/json' \
  -d '{"request_id":"REQ-20260930-001","output_batch_no":"FG-1","output_qty":"80.000","unit":"kg","inputs":[{"batch_no":"RM-A","used_qty":"60.000"},{"batch_no":"RM-B","used_qty":"30.000"}]}'
```

### 3. 查询批次余量 / 隔离状态

```bash
curl -s http://localhost:8080/api/v1/batches/RM-A
# {"batch_no":"RM-A","kind":"raw","unit":"kg","initial_qty":"100.000",
#  "remaining_qty":"40.000","quarantined":false, ...}
```

### 4. 查询上下游谱系

```bash
curl -s http://localhost:8080/api/v1/batches/FG-1/lineage
```

返回：

- `parents` / `ancestors`：直接上游（投入料）/ 全部上游（沿生产链向上递归）；
- `children` / `descendants`：直接下游 / **全部后代产出批次**；
- `edges`：谱系边（每次生产中 `input_batch_no -> output_batch_no` 及用量）。

### 5. 标记疑似污染并隔离全部后代

```bash
curl -s -X POST http://localhost:8080/api/v1/batches/RM-B/quarantine \
  -H 'Content-Type: application/json' \
  -d '{"reason":"异物"}'
# {"batch_no":"RM-B","quarantined":true,"reason":"异物",
#  "affected_descendants":["FG-1", ...]}
```

发生混料时，受影响的**产出批次整体隔离**（递归找出全部后代）；隔离后的批次
（含其下游产出）再出现在生产投入中会被拒绝。重复调用是幂等的，已隔离批次不会重复标记。

---

## 六、API 一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/v1/batches` | 登记原料批次 |
| GET  | `/api/v1/batches/{batch_no}` | 查询批次余量与隔离状态 |
| POST | `/api/v1/productions` | 提交生产（幂等） |
| GET  | `/api/v1/batches/{batch_no}/lineage` | 上下游谱系 |
| POST | `/api/v1/batches/{batch_no}/quarantine` | 标记污染并隔离全部后代 |
| GET  | `/health` | 健康检查 |

错误码：`BATCH_NO_EXISTS`(409)、`IDEMPOTENCY_CONFLICT`(409)、`SOURCE_NOT_FOUND`(422)、
`INSUFFICIENT_BALANCE`(422)、`UNIT_MISMATCH`(422)、`OUTPUT_EXCEEDS_INPUT`(422)、
`BATCH_QUARANTINED`(422)、`BATCH_NOT_FOUND`(404)。

## 七、并发与一致性

- 生产事务对投入批次**按编号排序后 `SELECT ... FOR UPDATE`** 加锁，多笔提交串行化且避免互锁；
- 余量扣减使用带 `remaining_qty >= used_qty` 条件的原子 `UPDATE`，并配有
  `CHECK (remaining_qty >= 0)` 约束作为最终兜底，数据库层面杜绝超用；
- 幂等键与余量扣减在**同一事务**中提交，不存在"键已存但未扣料"或反之的中间态；
- SQLite（本地试用）下使用 WAL + busy_timeout 排队写入；生产部署建议使用 Compose 的 PostgreSQL。

## 八、运行测试

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
pytest          # 自动使用临时 SQLite 库，共 26 项，含并发超用、幂等、隔离传播等
```

## 项目结构

```
app/
  config.py     # 环境变量配置
  db.py         # 模型、引擎、建库（含等待就绪/自动建库）
  schemas.py    # 请求/响应模型与 Decimal 处理
  service.py    # 业务逻辑：登记/生产事务/谱系/隔离
  main.py       # FastAPI 路由与错误处理
tests/          # pytest 用例
Dockerfile
docker-compose.yml
requirements.txt
```
