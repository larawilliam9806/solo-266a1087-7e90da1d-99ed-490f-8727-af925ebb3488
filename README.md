# Census Archive Service

四叉树普查档案的最小读取取数服务。FastAPI + SQLite，`docker compose up` 即可启动。

## 启动

```bash
docker compose up --build
# 文档: http://localhost:8000/docs
```

数据卷 `census-data` 挂在 `/data/archive.sqlite3`；可用 `CENSUS_PORT` 改端口。

本地开发：

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload
pytest -q
```

## 数据模型

网格逐层四分：根格号为空串 `""`，子格在父格后追加一位 `0`–`3`，最多 12 位。
例如 `""` → `"3"` → `"31"` → `"310"`。格号只含 `0`–`3`，长度即层级。

每个统计期只有一批档案。提交整批在**单个事务**内完成（`BEGIN IMMEDIATE`）：

- 不带 `replace` 提交已存在的统计期 → `409`，原批不动；
- 带 `replace=true` → 删旧行 + 写新行在同一事务，失败自动回滚，原批不动；
- 校验失败（重复格号、非法数字、>12 位、负数人数）→ `422`，原批不动；
- 一致性核验失败（见下节）→ `422` 并列出冲突，原批及其核验结论不动。

## 一致性核验

交存或换交时，逐格核对上级与后代的人数：

- 核对某个上级时，只统计它**最浅的已存后代**——已被另一个已存后代覆盖的
  细格跳过，嵌套格不会重复计数；
- 后代合计**不得超过**上级人数；
- 后代把上级**铺满**（每个最细格都被覆盖）时，合计必须**等于**上级人数。

发现矛盾即拒绝该批（`422`），响应列出冲突的上级、参与核对的格号与双方人数：

```json
{
  "detail": "batch for period '2024' is inconsistent: ...",
  "period": "2024",
  "conflicts": [
    {"ancestor": "0", "ancestor_population": 10, "cells": ["01"],
     "descendant_total": 12, "kind": "exceeds"}
  ]
}
```

`kind` 为 `exceeds`（后代合计超过上级）或 `mismatch`（铺满但不相等）。

### 旧库批次

升级前已入库的批次没有核验结论，在**首次取数**时核验，结论按批保存
（以批次 `batch_id` 为键，与 `created_at` 无关，同一秒内的两次换交也是两批）：

- 核验通过 → 按原规则求最简算式，只读入式格的人数；
- 核验失败 → 取数只报冲突（`409`，响应格式同上），换交合格批后恢复取数；
- 核验期间不阻塞该期换交；结论以批次 id 为键，迟到的核验不会覆盖新批的结论。
  一次取数始终在同一 SQLite 读快照内使用同一批的结论、目录与人数。

## 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/archive` | 整批交存/换交 |
| `GET` | `/archive/{period}` | 批次元数据 |
| `POST` | `/census/query` | 求覆盖并集人数 + 最简算式 |
| `GET` | `/healthz` | 健康检查 |

### 交存

```bash
curl -X POST localhost:8000/archive -H 'content-type: application/json' -d '{
  "period": "2024",
  "cells": [{"cell": "0", "population": 120}, {"cell": "01", "population": 45}]
}'
```

换交同一统计期时加 `"replace": true`。

### 取数

```bash
curl -X POST localhost:8000/census/query -H 'content-type: application/json' -d '{
  "period": "2024",
  "cells": ["00", "02", "03"]
}'
```

```json
{
  "period": "2024",
  "cells": ["00", "02", "03"],
  "expression": "pop(\"0\") - pop(\"01\")",
  "terms": [
    {"cell": "0", "coefficient": 1},
    {"cell": "01", "coefficient": -1}
  ],
  "retrieved_cells": 2,
  "total_population": 75
}
```

- 查询是一组格号，结果按**覆盖并集**计算，重叠格不会重复计数。
- 算式系数可正可负，且只对目录中存在的格取数；返回的 `terms` 就是需要读取的格，
  `retrieved_cells` 为其个数。
- 算式**与人口分布无关**：对任意把人数分配到最细格的方案，算式恒等于目标区域人数。
- 核验结论、目录与人数读自同一 SQLite 读事务（`snapshot`），并发换交也不会混批。
- 批次核验结论为冲突时返回 `409` 并列出冲突格，换交合格批后恢复取数。
- 无解（请求区域没有可覆盖的档案）时返回 `422` 并列出 `missing_blocks`，提示缺档。

## 求解器

`app/solver.py` 在格号的**前缀树**上做两状态 DP，只在交存的格号与查询格号及其前缀处展开，
绝不枚举 `4^12` 个细格，也不需要读取任何人数即可确定算式。

设 `X(v)` 为根到 `v` 路径上的系数前缀和，则格 `v` 的系数为 `X(v) - X(parent)`，
细格约束为：目标区域内 `X=1`，区域外 `X=0`。任意整数标号钳到 `{0,1}` 不会增加系数非零的格，
故最优解的系数都在 `{-1,0,1}`，DP 每个节点只需记"进入标号"这一位状态，代价为非零系数个数。
无档案可用的子树会被 O(1) 折叠，并作为缺档块报告。
