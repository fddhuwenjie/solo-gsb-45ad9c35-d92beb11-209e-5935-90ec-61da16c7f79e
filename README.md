# 微孔板样本编排 API (Microplate Sample Arrangement API)

基于 **Python + FastAPI + OR-Tools (CP-SAT)** 的微孔板样本布局优化服务。

提交板型、样本（含实验组与批次）、固定孔位、禁用孔位与 seed 后，服务返回一份
**字典序最优**的布局：

1. **第一级（组间平衡）**：最小化各实验组在行、列方向上的最大数量差
   （每组分别统计其在每一行/列的样本数，取行极差与列极差中的较大者，
   再对所有组取最大，记为 `D`；空行/空列按 0 计入）。
2. **第二级（批次聚集）**：在 `D` 最优的前提下，最小化同批次样本
   正交相邻（上下左右，不含对角）的孔位对数，记为 `A`。
3. **确定性决胜**：在前两级最优解集合中，以 seed 控制的确定性搜索
   固定唯一布局 —— **相同输入与相同 seed 必然得到完全相同的布局**。

## 快速启动（单条命令）

```bash
docker compose up --build
```

服务启动后：

- API 文档（Swagger UI）: http://localhost:8000/docs
- 健康检查： http://localhost:8000/health
- 编排接口： `POST http://localhost:8000/api/v1/arrangements`
- 纠位接口： `POST http://localhost:8000/api/v1/arrangements/reconcile`
- 空间质控接口： `POST http://localhost:8000/api/v1/arrangements/spatial-qc`
- 批次放行核验： `POST http://localhost:8000/api/v1/arrangements/batch-release`
- 稀释梯度配制计划： `POST http://localhost:8000/api/v1/arrangements/dilution-plan`
- 双人协作盲化： `POST http://localhost:8000/api/v1/blinding/blind`
- 双人协作解盲： `POST http://localhost:8000/api/v1/blinding/unblind`
- 批次证书建立： `POST http://localhost:8000/api/v1/certificates`
- 证书重核验： `POST http://localhost:8000/api/v1/certificates/reverify`

不使用 Docker 时（Python 3.11+):

```bash
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

## 请求示例

```bash
curl -X POST http://localhost:8000/api/v1/arrangements \
  -H "Content-Type: application/json" \
  -d '{
    "plate": {"type": "96"},
    "samples": [
      {"id": "S01", "group": "treated", "batch": "B1"},
      {"id": "S02", "group": "treated", "batch": "B1"},
      {"id": "S03", "group": "control", "batch": "B2"},
      {"id": "S04", "group": "control", "batch": "B2"},
      {"id": "S05", "group": "control", "batch": "B1"}
    ],
    "fixed": [{"sample_id": "S01", "well": "C5"}],
    "blocked_wells": ["A1", "H12"],
    "seed": 42
  }'
```

### 请求字段

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `plate.type` | string | 板型：`"24"`(4×6)、`"48"`(6×8)、`"96"`(8×12)、`"384"`(16×24) 或 `"custom"`（需同时给 `rows`/`columns`，行列乘积 ≤ 400) |
| `samples[]` | array | 样本列表，每项 `{id, group, batch}`;`id` 必须唯一，`group`/`batch` 必填 |
| `fixed[]` | array | 固定孔位 `{sample_id, well}`，求解器不得移动；固定孔与禁用孔冲突、多样本固定同一孔都会判为无解 |
| `blocked_wells[]` | array | 禁用孔位（如 `"A1"`)，不可放置任何样本 |
| `seed` | int | 随机种子（≥0)，控制确定性决胜与求解搜索 |

孔位命名：行字母 + 列数字（`A1`…`H12` 等），大小写不敏感。

### 响应（200)

```json
{
  "seed": 42,
  "plate": {"type": "96", "rows": 8, "columns": 12,
            "blocked_wells": ["A1", "H12"], "available_wells": ["A2", "..."]},
  "layout": {"S01": "C5", "S02": "D3", "...": "..."},
  "metrics": {
    "level1_group_balance": {
      "global_max_spread": 1,
      "groups": [{"group": "control", "row_counts": [0,1,1,0,1,0,0,0],
                  "row_spread": 1, "col_spread": 1, "...": "..."}]
    },
    "level2_batch_adjacency": {
      "same_batch_adjacent_pairs_count": 0,
      "same_batch_adjacent_pairs": [],
      "total_orthogonal_edges": 172
    }
  },
  "validation": {
    "all_passed": true,
    "checks": {
      "one_well_per_sample": {"passed": true},
      "blocked_wells_respected": {"passed": true},
      "fixed_assignments_respected": {"passed": true},
      "wells_in_range": {"passed": true},
      "adjacency_metric_consistent": {"passed": true}
    }
  },
  "solver": {
    "status": "OPTIMAL",
    "stages": [{"stage": "1_balance", "objective_value": 1, "status": "OPTIMAL"},
               {"stage": "2_adjacency", "objective_value": 0, "status": "OPTIMAL"},
               {"stage": "3_tiebreak", "status": "OPTIMAL"}]
  }
}
```

- `layout`：孔位映射（样本 id → 孔位）。
- `metrics.level1_group_balance`：第一级指标（每组行/列计数与极差、全局最大极差 `D`)。
- `metrics.level2_batch_adjacency`：第二级指标（同批次正交相邻对数 `A` 及具体孔位对）。
- `validation`：对返回布局的独立约束校验（每样本恰好一孔、禁用孔未被占用、
  固定孔未被改写、孔位在板内、邻接指标一致）。
- `solver.status`:`OPTIMAL` 表示两级目标均已证明最优；`FEASIBLE` 表示
  在确定性时间预算内返回了当前最优可行解（大实例可能出现，见下文配置）。

### 无解（422)

无解时**不返回任何部分布局**，而是返回导致不可行的约束类别：

```json
{
  "detail": "No feasible layout exists for the submitted constraints.",
  "infeasibility_categories": ["CAPACITY_EXCEEDED"],
  "messages": ["25 samples but only 24 available wells (24 total, 0 blocked)"]
}
```

可能的类别：

| 类别 | 含义 |
| --- | --- |
| `SAMPLE_IDS_DUPLICATED` | 样本 id 重复 |
| `GROUP_BATCH_INVALID` | 样本缺少实验组或批次 |
| `PLATE_DIMENSION_INVALID` | 板型尺寸非法（自定义缺尺寸/超上限/与预设不符） |
| `WELL_REFERENCE_INVALID` | 孔位名称非法、越界，或固定孔引用了不存在的样本 |
| `FIXED_CONFLICT` | 同一样本被固定到多个孔 / 多个样本固定到同一孔 |
| `FIXED_ON_BLOCKED` | 固定孔落在禁用孔上 |
| `CAPACITY_EXCEEDED` | 样本数超过可用孔数 |
| `SOLVER_INFEASIBLE` | 求解器证明约束组合不可满足 |
| `REQUEST_VALIDATION_ERROR` | 请求体结构不合法（字段缺失/类型错误等） |

请求体结构错误同样返回 422（类别 `REQUEST_VALIDATION_ERROR`)。

## 扫描纠位接口 `POST /api/v1/arrangements/reconcile`

把编排接口返回的 `layout` 作为目标映射，与整板扫描结果核对，并生成
**最少步数**的逐孔纠位指令。每步只把一份样本移入**当前为空**的可用孔
（绝不直接交换两个非空孔）。

```bash
curl -X POST http://localhost:8000/api/v1/arrangements/reconcile \
  -H "Content-Type: application/json" \
  -d '{
    "plate": {"type": "24"},
    "target": {"S1": "A1", "S2": "A2", "S3": "A3", "S4": "B1"},
    "scan":   {"A1": "S2", "A2": "S1", "A3": "S3", "C2": "S4"},
    "blocked_wells": ["D6"],
    "staging_well": "D5"
  }'
```

### 请求字段

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `plate` | object | 板型，与编排接口相同（默认 `"96"`) |
| `target` | object | 目标映射：样本 id → 目标孔位，即编排接口响应中的 `layout` |
| `scan` | object | 完整扫描得到的孔位 → 样本 id 映射，**空孔不列出** |
| `blocked_wells[]` | array | 禁用孔位，不可放置任何样本 |
| `staging_well` | string | 可选。空闲、非目标、非禁用的暂存孔，用于解开闭环错位；结束时恢复为空 |

### 核对（任一不满足即 422，不生成任何搬移指令）

目标孔位重复（`TARGET_WELLS_DUPLICATED`)、孔位名称非法或越界
（`WELL_REFERENCE_INVALID`)、目标孔落在禁用孔上（`TARGET_ON_BLOCKED`)、
扫描孔位重复（`SCAN_WELLS_DUPLICATED`，如 `"a1"` 与 `"A1"`)、扫描样本重复
（`SCAN_SAMPLES_DUPLICATED`)、计划样本缺失（`SAMPLES_MISSING`)、计划外样本
（`UNPLANNED_SAMPLES`)、禁用孔被占用（`BLOCKED_WELL_OCCUPIED`)。
响应在 `infeasibility_categories` 与 `messages` 中列出全部问题。

### 响应（200)

```json
{
  "misplaced_wells": [
    {"well": "A1", "expected": "S1", "actual": "S2"},
    {"well": "A2", "expected": "S2", "actual": "S1"},
    {"well": "B1", "expected": "S4", "actual": null},
    {"well": "C2", "expected": null, "actual": "S4"}
  ],
  "move_count": 4,
  "cycles": [["A1", "A2"]],
  "staging_well": "D5",
  "instructions": [
    {"step": 1, "sample_id": "S4", "from": "C2", "to": "B1"},
    {"step": 2, "sample_id": "S2", "from": "A1", "to": "D5"},
    {"step": 3, "sample_id": "S1", "from": "A2", "to": "A1"},
    {"step": 4, "sample_id": "S2", "from": "D5", "to": "A2"}
  ],
  "final_mapping": {"S1": "A1", "S2": "A2", "S3": "A3", "S4": "B1"},
  "verification": {
    "final_matches_target": true,
    "every_destination_empty_at_move": true,
    "staging_well_restored_empty": true
  }
}
```

- `misplaced_wells`：错孔清单（`expected`/`actual` 为 `null` 表示该孔应为空/当前为空）。
- `instructions`：确定性的最少步数指令。错位分解为**空孔链**与**闭环**:
  空孔链先行，链上每份样本直接移入已腾空的最终目标孔（k 份样本 k 步）;
  每个闭环借用 `staging_well` 暂存一份样本后逐个归位，最后把暂存样本移回
  目标孔（k 份样本 k+1 步，暂存孔恢复为空）。总步数
  `错位样本数 + 闭环数` 为理论最小值；相同输入必然得到相同指令序列。
- `final_mapping`：指令重放后的最终映射（样本 id → 孔位），与 `target` 一致。
- `verification`：对指令序列的独立重放校验（终态与目标一致、每步目的孔
  均为空、暂存孔最终恢复为空）。

### 闭环缺暂存孔（409)

存在闭环错位但未提供可用暂存孔（未提供、被占用、是目标孔、被禁用或非法）
时返回 409，列出全部闭环，**不输出部分指令**:

```json
{
  "detail": "Closed-loop misplacements require a usable staging well; no move instructions were generated.",
  "cycles": [["A1", "A2"]],
  "staging_well": null,
  "problems": ["no staging well provided"]
}
```

无闭环时 `staging_well` 可省略（即使提供也不使用）。

## 空间质控接口 `POST /api/v1/arrangements/spatial-qc`

对已完成编排的板做空间效应质控：先按**实验组中位数中心化**读数（避免组间
基线差异混入空间效应），再用**行列中位数交替剥离（median polish)**
累计总体、逐行、逐列效应，并以 seed 控制的**组内置换**给出最大行列效应的
**加一修正经验 p 值**。读数允许缺失（孔位省略或显式 `null`)。

端口 `8000` 调用示例：

```bash
curl -X POST http://localhost:8000/api/v1/arrangements/spatial-qc \
  -H "Content-Type: application/json" \
  -d '{
    "plate": {"type": "24"},
    "layout": {
      "S1": "A1", "S2": "A2", "S3": "B1", "S4": "B2",
      "S5": "C1", "S6": "C2", "S7": "D1", "S8": "D2"
    },
    "samples": [
      {"id": "S1", "group": "treated"}, {"id": "S2", "group": "control"},
      {"id": "S3", "group": "treated"}, {"id": "S4", "group": "control"},
      {"id": "S5", "group": "treated"}, {"id": "S6", "group": "control"},
      {"id": "S7", "group": "treated"}, {"id": "S8", "group": "control"}
    ],
    "readings": {
      "A1": 1.05, "A2": 0.98, "B1": 1.02, "B2": 1.01,
      "C1": 1.04, "C2": 0.99, "D1": 1.03,
      "D2": null
    },
    "seed": 42,
    "permutations": 1000,
    "tolerance": 0.000001,
    "max_iterations": 1000
  }'
```

### 请求字段

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `plate` | object | 板型，与编排接口相同（默认 `"96"`) |
| `layout` | object | 布局映射：样本 id → 孔位（编排接口响应中的 `layout`) |
| `samples[]` | array | 样本列表，每项 `{id, group}`;`id` 必须与 `layout` 一一对应 |
| `readings` | object | 孔位 → 数值读数；**孔位省略或显式 `null` 表示读数缺失**;`NaN`/`Infinity` 不允许 |
| `seed` | int | 随机种子（≥0)，控制组内置换；相同输入与 seed 结果完全一致 |
| `permutations` | int | 组内置换次数，默认 `1000`，范围 0–1,000,000;`0` 表示跳过置换（`p_value` 为 `null`) |
| `tolerance` | float | 收敛阈值（>0)，默认 `1e-6`；本轮最大行列校正量 ≤ tolerance 即收敛 |
| `max_iterations` | int | 行列交替剥离的最大轮数，默认 `1000`(≥1)；未达 tolerance 即返回 422 |

### 算法

1. **组中位数中心化**：每个有效读数减去其所在 `group` 的读数中位数。
2. **行列中位数交替剥离**：每轮先逐行减去行内（非缺失）中位数并累加到行
   效应，再逐列减去列内中位数并累加到列效应；记录本轮行、列及最大校正量。
   当本轮**最大校正量 ≤ `tolerance`** 时收敛，否则继续，最多 `max_iterations`
   轮。收敛后将行、列效应中位数依次归零（偏移量最终并入总体效应），使效应
   分解与剥离顺序无关。
3. **校正读数**：`校正读数 = 组中位数 + 残差 = 原读数 −（总体 + 行 + 列）`,
   即剥离拟合空间面但保留组基线；缺失孔不出现在输出中。
4. **置换检验**：在**每个 group 内部**，以 seed 驱动的确定性随机数把有效
   读数在该组有效孔位间重新分配（缺失孔位置与每组有效孔数保持不变），
   对每次置换重复同一校正流程；统计量为**全部行、列效应绝对值的最大值**。
   经验 p 值采用加一修正：`p = (1 + #{T_perm ≥ T_obs}) / (1 + permutations)`,
   因此取值范围恒为 `(0, 1]`，永远不会为 0。

### 响应（200)

```json
{
  "seed": 42,
  "plate": {"type": "24", "rows": 4, "columns": 6},
  "parameters": {"permutations": 1000, "tolerance": 1e-6, "max_iterations": 1000},
  "groups": [
    {"group": "control", "valid_readings": 3, "valid_rows": 3,
     "valid_columns": 2, "median": 0.99},
    {"group": "treated", "valid_readings": 4, "valid_rows": 4,
     "valid_columns": 2, "median": 1.035}
  ],
  "effects": {
    "overall": 0.0,
    "row_effects": [0.0, 0.0, 0.0, 0.0],
    "col_effects": [0.02, -0.02, 0.0, 0.0, 0.0, 0.0],
    "max_abs_row_col_effect": 0.02
  },
  "readings": [
    {"well": "A1", "sample_id": "S1", "group": "treated",
     "reading": 1.05, "centred_reading": 0.015,
     "corrected_reading": 1.015}
  ],
  "convergence": {
    "converged": true,
    "iterations": 2,
    "tolerance": 1e-6,
    "trajectory": [
      {"iteration": 1, "row_correction": 0.02, "col_correction": 0.02,
       "max_correction": 0.02}
    ]
  },
  "permutation_test": {
    "permutations": 1000,
    "observed_statistic": 0.02,
    "exceedances": 24,
    "p_value": 0.024975,
    "method": "within-group well permutation; p=(1+#{T_perm>=T_obs})/(1+n)"
  }
}
```

- `groups`：各组有效读数数、覆盖的行数/列数与组中位数（中心化基准）。
- `effects`：总体效应、逐行/逐列效应（长度等于板行数/列数）及统计量
  `max_abs_row_col_effect`。
- `readings`：逐孔给出原始读数、组内中心化读数与校正读数（按行优先顺序，
  仅含有效孔）。
- `convergence.trajectory`：逐轮行校正量、列校正量与最大校正量。
- `permutation_test`：观测统计量、达到或超过它的置换次数与加一修正 p 值；
  `permutations = 0` 时 `p_value` 为 `null`。

### 不可分析（422，不输出部分结论）

满足以下任一条件即返回 422，**不返回任何效应、校正读数或 p 值**，响应在
`infeasibility_categories` 与 `messages` 中列出全部问题：

| 类别 | 含义 |
| --- | --- |
| `SPATIAL_GROUP_INSUFFICIENT` | 任一组有效读数少于 2 |
| `SPATIAL_GRID_INSUFFICIENT` | 有效孔未覆盖至少 2 个不同行或 2 个不同列 |
| `SPATIAL_NOT_CONVERGED` | 观测数据（或任一次置换重复）在 `max_iterations` 内未收敛 |
| `SPATIAL_SAMPLE_DUPLICATED` | `samples` 中样本 id 重复 |
| `SPATIAL_SAMPLE_UNKNOWN` | `layout` 引用了 `samples` 中不存在的样本 |
| `SPATIAL_LAYOUT_CONFLICT` | 多样本占用同一孔，或样本未在 `layout` 中安排孔位 |
| `SPATIAL_READING_INVALID` | 读数对应孔位在 `layout` 中无人占用 |
| `SPATIAL_READING_DUPLICATED` | 同一孔位（大小写不敏感）出现多个读数 |
| `WELL_REFERENCE_INVALID` | 孔位名称非法或越界 |
| `PLATE_DIMENSION_INVALID` | 板型尺寸非法 |
| `REQUEST_VALIDATION_ERROR` | 请求体结构不合法（含 `NaN`/`Infinity` 读数等） |

## 批次放行核验 `POST /api/v1/arrangements/batch-release`

批次放行是一道**两级串联门控**,任何一级失败都不给放行结论:

1. **扫描吻合核验**:按纠位接口的现有规则,把完整扫描与计划 layout 逐孔核对。
   存在缺孔（计划样本未扫到）、计划外样本、扫描样本/孔位重复、禁用孔占用、
   **空孔链错位或闭环错位**时,返回 422,给出问题类别与**错孔清单**(及闭环),
   **不进入空间质控、不给出任何放行结论**。
2. **空间质控门控**:仅当扫描与计划**完全一致**(零错孔)时才复用空间质控流程。
   只有①median polish 在 `max_iterations` 内**收敛**、②最大行列效应绝对值
   ≤ `max_spatial_effect`、③置换 p 值 ≥ `min_p_value` **三个条件同时满足**,
   才返回 `verdict = "RELEASED"` 及扫描摘要与完整质控摘要;任一条件失败返回
   `verdict = "NOT_RELEASED"` 及失败原因,**不输出空间质控明细等部分结论**。
   空间质控请求本身不可分析（样本/布局/读数结构问题）仍返回 422。

端口 `8000` 调用示例:

```bash
curl -X POST http://localhost:8000/api/v1/arrangements/batch-release \
  -H "Content-Type: application/json" \
  -d '{
    "plate": {"type": "24"},
    "target": {
      "S1": "A1", "S2": "A2", "S3": "B1", "S4": "B2",
      "S5": "C1", "S6": "C2", "S7": "D1", "S8": "D2"
    },
    "scan": {
      "A1": "S1", "A2": "S2", "B1": "S3", "B2": "S4",
      "C1": "S5", "C2": "S6", "D1": "S7", "D2": "S8"
    },
    "blocked_wells": ["D6"],
    "samples": [
      {"id": "S1", "group": "treated"}, {"id": "S2", "group": "control"},
      {"id": "S3", "group": "treated"}, {"id": "S4", "group": "control"},
      {"id": "S5", "group": "treated"}, {"id": "S6", "group": "control"},
      {"id": "S7", "group": "treated"}, {"id": "S8", "group": "control"}
    ],
    "readings": {
      "A1": 1.05, "A2": 0.98, "B1": 1.02, "B2": 1.01,
      "C1": 1.04, "C2": 0.99, "D1": 1.03, "D2": 1.00
    },
    "seed": 42,
    "max_spatial_effect": 0.5,
    "min_p_value": 0.05,
    "permutations": 1000,
    "tolerance": 0.000001,
    "max_iterations": 1000
  }'
```

### 请求字段

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `plate` | object | 板型,与编排接口相同（默认 `"96"`) |
| `target` | object | 计划映射:样本 id → 计划孔位（编排接口响应中的 `layout`) |
| `scan` | object | 完整扫描:孔位 → 样本 id,**空孔不列出**;必须与计划完全一致 |
| `blocked_wells[]` | array | 禁用孔位,被扫描占用即拒绝 |
| `samples[]` | array | 样本分组,每项 `{id, group}`,供空间质中心组内中心化与置换 |
| `readings` | object | 孔位 → 数值读数;省略孔位或显式 `null` 表示读数缺失;`NaN`/`Infinity` 不允许 |
| `seed` | int | 随机种子（≥0),控制组内置换;相同输入与 seed 结果完全一致 |
| `max_spatial_effect` | float | **空间效应上限（>0,必填)**:最大行列效应绝对值必须 ≤ 该值 |
| `min_p_value` | float | **最低置换 p 值（0–1,必填)**;`permutations=0` 时无 p 值,该门必失败 |
| `permutations` | int | 组内置换次数,默认 `1000`,范围 0–1,000,000;`0` 跳过置换（无法放行） |
| `tolerance` | float | 收敛阈值（>0),默认 `1e-6`;本轮最大行列校正量 ≤ tolerance 即收敛 |
| `max_iterations` | int | 行列交替剥离最大轮数,默认 `1000`(≥1);未收敛即不放行 |

### 放行成功（200,`verdict = "RELEASED"`)

三个门全部通过才返回该状态,同时给出各门观测值、扫描摘要与完整质控摘要
（质控摘要结构与空间质控接口的 200 响应完全一致):

```json
{
  "verdict": "RELEASED",
  "seed": 42,
  "plate": {"type": "24", "rows": 4, "columns": 6},
  "thresholds": {
    "max_spatial_effect": 0.5, "min_p_value": 0.05,
    "permutations": 1000, "tolerance": 1e-6, "max_iterations": 1000
  },
  "gates": {
    "converged": true,
    "iterations": 2,
    "max_abs_row_col_effect": 0.02,
    "max_spatial_effect": 0.5,
    "effect_within_limit": true,
    "observed_p_value": 0.42,
    "min_p_value": 0.05,
    "p_value_available": true,
    "p_value_meets_threshold": true
  },
  "scan_summary": {
    "scanned_wells": 8,
    "planned_samples": 8,
    "misplaced_wells": [],
    "blocked_wells": ["D6"],
    "blocked_wells_occupied": 0,
    "matches_plan": true
  },
  "spatial_qc_summary": { "...": "与 /spatial-qc 的 200 响应结构相同" }
}
```

### 门控失败（200,`verdict = "NOT_RELEASED"`,不输出部分结论)

收敛失败、效应超限、p 值不达标（或 `permutations=0` 导致 p 值不可用）时,
返回未放行及全部失败原因;**不附带 `scan_summary` 与 `spatial_qc_summary`**,
各门观测值（不可用为 `null`)仍随 `gates` 返回以便复核:

```json
{
  "verdict": "NOT_RELEASED",
  "seed": 42,
  "plate": {"type": "24", "rows": 4, "columns": 6},
  "reasons": ["SPATIAL_EFFECT_EXCEEDED"],
  "messages": ["max absolute row/column effect 0.8 exceeds the limit 0.5"],
  "thresholds": {"...": "..."},
  "gates": {
    "converged": true,
    "iterations": 2,
    "max_abs_row_col_effect": 0.8,
    "max_spatial_effect": 0.5,
    "effect_within_limit": false,
    "observed_p_value": 0.01,
    "min_p_value": 0.05,
    "p_value_available": true,
    "p_value_meets_threshold": false
  }
}
```

| `reasons` 取值 | 含义 |
| --- | --- |
| `SPATIAL_NOT_CONVERGED` | median polish 在 `max_iterations` 内未收敛 |
| `SPATIAL_EFFECT_EXCEEDED` | 最大行列效应绝对值超过 `max_spatial_effect` |
| `P_VALUE_BELOW_THRESHOLD` | 置换 p 值低于 `min_p_value` |
| `P_VALUE_UNAVAILABLE` | `permutations=0`,无 p 值,p 值门无法通过 |

### 扫描不一致（422,不给放行结论)

扫描未与计划完全一致,或请求结构非法时返回 422,响应在
`infeasibility_categories`、`messages` 中列出全部问题,并给出 `misplaced_wells`
错孔清单（`expected`/`actual` 为 `null` 表示该孔应为空/实际为空）与 `cycles`
闭环清单;**不含 `verdict`、门控结果或任何质控输出**:

| 类别 | 含义 |
| --- | --- |
| `CLOSED_LOOP_MISPLACEMENT` | 扫描存在闭环错位（全部相关孔被占用的循环移位）,`cycles` 列出闭环 |
| `SCAN_LAYOUT_MISMATCH` | 扫描与计划布局不一致（含空孔链错位等非闭环错孔）,`misplaced_wells` 列出错孔 |
| `SAMPLES_MISSING` | 计划样本未在扫描中出现（缺孔） |
| `UNPLANNED_SAMPLES` | 扫描出现计划外样本 |
| `SCAN_WELLS_DUPLICATED` / `SCAN_SAMPLES_DUPLICATED` | 扫描孔位（大小写归一后）/样本重复 |
| `TARGET_WELLS_DUPLICATED` / `TARGET_ON_BLOCKED` | 计划孔位重复 / 计划孔落在禁用孔上 |
| `BLOCKED_WELL_OCCUPIED` | 禁用孔被扫描占用 |
| `WELL_REFERENCE_INVALID` / `PLATE_DIMENSION_INVALID` | 孔位名称非法或越界 / 板型尺寸非法 |
| `SPATIAL_*` | 扫描吻合但空间质控请求本身不可分析（如 `SPATIAL_GROUP_INSUFFICIENT`、`SPATIAL_LAYOUT_CONFLICT` 等,类别同空间质控接口） |
| `REQUEST_VALIDATION_ERROR` | 请求体结构不合法（缺字段、越界、`NaN`/`Infinity` 读数等） |

```json
{
  "detail": "The batch cannot be released; ... no release conclusion was produced.",
  "infeasibility_categories": ["CLOSED_LOOP_MISPLACEMENT", "SCAN_LAYOUT_MISMATCH"],
  "messages": ["scan contains 1 closed-loop misplacement cycle(s): A1 -> A2",
               "scan disagrees with the planned layout at 2 well(s); ..."],
  "misplaced_wells": [
    {"well": "A1", "expected": "S1", "actual": "S2"},
    {"well": "A2", "expected": "S2", "actual": "S1"}
  ],
  "cycles": [["A1", "A2"]]
}
```

## 批次证书 `POST /api/v1/certificates`

为**通过批次放行核验**的批次建立证书。请求由 `batch_id`、`request_key`、完整的
放行请求 `release`（结构与批次放行核验接口完全相同）与可选的盲化操作视图 `view`
（盲化接口返回的 well + blind_id 操作视图）组成。服务**复用纠位规则与空间质控
门控**,**只有 `verdict = "RELEASED"` 才会建立证书**：计划 layout(`target`)、scan、
readings、样本分组、seed、全部阈值（`max_spatial_effect`/`min_p_value`/
`permutations`/`tolerance`/`max_iterations`）与**视图摘要**在**同一 SQLite 事务**
中原子写入冻结快照，返回 `certificate_id`、`status`、`revision`（恒为 1）与覆盖
整个快照的内容 `digest`(SHA-256)。每个批次至多存在一份证书。

端口 `8000` 调用示例：

```bash
curl -X POST http://localhost:8000/api/v1/certificates \
  -H "Content-Type: application/json" \
  -d '{
    "batch_id": "B2026-0042",
    "request_key": "crt-9f3a-0001",
    "release": {
      "plate": {"type": "24"},
      "target": {
        "S1": "A1", "S2": "A2", "S3": "B1", "S4": "B2",
        "S5": "C1", "S6": "C2", "S7": "D1", "S8": "D2"
      },
      "scan": {
        "A1": "S1", "A2": "S2", "B1": "S3", "B2": "S4",
        "C1": "S5", "C2": "S6", "D1": "S7", "D2": "S8"
      },
      "samples": [
        {"id": "S1", "group": "treated"}, {"id": "S2", "group": "control"},
        {"id": "S3", "group": "treated"}, {"id": "S4", "group": "control"},
        {"id": "S5", "group": "treated"}, {"id": "S6", "group": "control"},
        {"id": "S7", "group": "treated"}, {"id": "S8", "group": "control"}
      ],
      "readings": {
        "A1": 1.05, "A2": 0.98, "B1": 1.02, "B2": 1.01,
        "C1": 1.04, "C2": 0.99, "D1": 1.03, "D2": 1.00
      },
      "seed": 42,
      "max_spatial_effect": 0.5,
      "min_p_value": 0.05,
      "permutations": 1000
    },
    "view": [
      {"well": "A1", "blind_id": "BL-9f2c01ab77e4"},
      {"well": "A2", "blind_id": "BL-51bd0e8a2c90"}
    ]
  }'
```

### 建立证书请求字段

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `batch_id` | string | 批次标识；每个批次至多存在一份证书（吊销后也不可重建） |
| `request_key` | string | **幂等键**：同键同内容重放已记录响应，同键不同内容返回 409 |
| `release` | object | 批次放行核验请求，字段与 `POST /api/v1/arrangements/batch-release` 完全相同 |
| `view[]` | array | 可选的盲化操作视图，每项 `{well, blind_id}`;**只保存其 SHA-256 摘要，不保存视图本身** |

### 建立成功（201,`status = "ACTIVE"`)

```json
{
  "certificate_id": "cert-21c5d55aee34528c617180cf",
  "batch_id": "B2026-0042",
  "status": "ACTIVE",
  "revision": 1,
  "digest": "237699a01001fa68...",
  "view_digest": "8c4f2ab19d..."
}
```

未携带 `view` 时 `view_digest` 为 `null`。其余结果：

| 状态码 | 情形 |
| --- | --- |
| 200 | 放行门控未通过：`status = "NOT_CERTIFIED"`，附 `verdict`/`reasons`/`gates`/`thresholds`,**不建立证书** |
| 409 | 批次已有证书（`error = "CERTIFICATE_ALREADY_EXISTS"`，含现有 `certificate_id` 与 `status`)，或 `request_key` 曾被不同内容使用（`error = "REQUEST_KEY_CONFLICT"`) |
| 422 | 扫描与计划不一致或请求非法（响应包络与批次放行核验接口的 422 完全一致），或视图结构非法（`CERTIFICATE_VIEW_INVALID` / `WELL_REFERENCE_INVALID`);**不建立证书** |

## 证书重核验 `POST /api/v1/certificates/reverify`

凭 `batch_id`、`expected_revision` 与**新的** `scan`/`readings`（可带视图）复核已获证
批次：板型、计划 layout、样本分组、seed 与全部阈值从**冻结快照**恢复，重新执行与
建立时完全相同的两级放行核验（纠位规则 + 空间质控门控）。复核通过时在**同一
SQLite 事务**中追加一条包含本次 scan/readings 的**不可变 revision 快照**，重算覆盖
该快照的 `digest` 并返回；每个 revision 的快照逐版本保留，旧 revision 可随时审计
比对。

```bash
curl -X POST http://localhost:8000/api/v1/certificates/reverify \
  -H "Content-Type: application/json" \
  -d '{
    "batch_id": "B2026-0042",
    "request_key": "rv-9f3a-0007",
    "expected_revision": 1,
    "scan": {
      "A1": "S1", "A2": "S2", "B1": "S3", "B2": "S4",
      "C1": "S5", "C2": "S6", "D1": "S7", "D2": "S8"
    },
    "readings": {
      "A1": 1.06, "A2": 0.97, "B1": 1.03, "B2": 1.00,
      "C1": 1.04, "C2": 0.99, "D1": 1.02, "D2": 1.01
    },
    "view": [
      {"well": "A1", "blind_id": "BL-9f2c01ab77e4"},
      {"well": "A2", "blind_id": "BL-51bd0e8a2c90"}
    ]
  }'
```

### 重核验请求字段

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `batch_id` | string | 批次标识 |
| `request_key` | string | **幂等键**，规则同建立接口 |
| `expected_revision` | int | 期望的当前 revision(≥1)；与证书当前值不符返回 409,**旧 revision 不可覆盖** |
| `scan` | object | 新的完整扫描：孔位 → 样本 id，空孔不列出 |
| `readings` | object | 新的孔位 → 数值读数；省略或 `null` 表示缺失；`NaN`/`Infinity` 不允许 |
| `view[]` | array | 可选的盲化操作视图；携带时其摘要必须与证书冻结摘要一致 |

### 复核通过（200，追加 revision 快照）

```json
{
  "certificate_id": "cert-21c5d55aee34528c617180cf",
  "batch_id": "B2026-0042",
  "status": "ACTIVE",
  "revision": 2,
  "digest": "9d1c0e4b7a2f...",
  "view_digest": "8c4f2ab19d..."
}
```

每个 revision 都有独立的不可变证据快照（计划参数不变，`scan`/`readings` 为本次
提交）;`digest` 覆盖**本次 revision** 的快照，因此成功重核验后随新证据更新（相同
证据内容得到相同 digest);`view_digest` 仍是建立时冻结的视图摘要。旧 revision 的
快照与 digest 在证书库中逐版本保留，可独立审计。

### 复核失败（200,`status = "REVOKED"`，返回首个失效原因）

**任何错位、质控失败或视图摘要变化**都会把证书立即标为 `REVOKED` 并返回**首个
失效原因**;revision 不递增：

```json
{
  "certificate_id": "cert-21c5d55aee34528c617180cf",
  "batch_id": "B2026-0042",
  "status": "REVOKED",
  "revision": 1,
  "digest": "237699a01001fa68...",
  "revocation": {
    "reason": "CLOSED_LOOP_MISPLACEMENT",
    "messages": ["scan contains 1 closed-loop misplacement cycle(s): A1 -> A2", "..."],
    "misplaced_wells": [
      {"well": "A1", "expected": "S1", "actual": "S2"},
      {"well": "A2", "expected": "S2", "actual": "S1"}
    ],
    "cycles": [["A1", "A2"]]
  }
}
```

`revocation.reason` 取值为放行核验的首个失败类别/原因（如 `SAMPLES_MISSING`、
`UNPLANNED_SAMPLES`、`CLOSED_LOOP_MISPLACEMENT`、`SCAN_LAYOUT_MISMATCH`、
`SPATIAL_EFFECT_EXCEEDED`、`P_VALUE_BELOW_THRESHOLD`、`P_VALUE_UNAVAILABLE`、
`SPATIAL_NOT_CONVERGED` 等）或 `VIEW_DIGEST_MISMATCH`（携带视图的摘要与证书
冻结摘要不符）。

### 其余重核验结果

| 状态码 | 情形 |
| --- | --- |
| 404 | 批次无证书（`error = "CERTIFICATE_NOT_FOUND"`) |
| 409 | 证书已吊销（`error = "CERTIFICATE_REVOKED"`,**吊销不可恢复**，附 `revoked_reason`);`expected_revision` 与当前 revision 不符（`error = "CERTIFICATE_REVISION_MISMATCH"`，附 `current_revision`);`request_key` 曾被不同内容使用（`error = "REQUEST_KEY_CONFLICT"`) |
| 422 | 请求结构非法（`REQUEST_VALIDATION_ERROR`）或视图结构非法（`CERTIFICATE_VIEW_INVALID` / `WELL_REFERENCE_INVALID`);**证书保持不变** |

### 幂等与 `request_key`

两个证书接口都以 `request_key` 为幂等键：

- **同键同内容重放**：返回已记录的响应（状态码与响应体完全一致）——网络重试
  不会造成重复建证、重复递增 revision 或重复吊销；
- **同键不同内容**：返回 409 `REQUEST_KEY_CONFLICT`，原记录不受影响；
- 证书写入、吊销、revision 递增与幂等记录都在**同一 SQLite 事务**中提交；
- 与批次状态相关的拒绝（404 无证书、409 已吊销/已有证书/revision 不符）**不占用**
  `request_key`，修正状态后可用同键重试。

### 证书安全

- 证书**只保存盲化操作视图的 SHA-256 摘要**，不保存视图本身；**绝不保存密钥
  份额或明文映射**（份额与 blind_id ↔ sample_id 对应关系从不进入证书存储）;
- `digest` 覆盖当前 revision 快照的全部内容（批次、板型、layout、scan、
  readings、样本分组、seed、阈值与视图摘要）；每次成功重核验都在同一事务中追加
  一个不可变 revision 快照（各自的 `digest` 覆盖各自内容），旧版本可独立审计比对;
- 数据库路径由 `CERT_DB_PATH` 配置（默认 `./certificates.db`;Docker Compose
  将其设为 `/data/certificates.db` 并挂载命名卷持久化）。

## 稀释梯度配制计划 `POST /api/v1/arrangements/dilution-plan`

为**既有编排结果**（编排接口响应中的 `layout`，即 sample_id → well 映射）
生成确定性的逐步移液指令。调用方提供：

- 目标 `layout` 与每个**来源样本**的浓度 `concentration`、可用体积
  `available_volume_ul`；
- 每个目标孔的目标浓度 `target_concentration` 与终体积 `final_volume_ul`；
- 目标孔容量上限 `well_capacity_ul`、稀释液 `diluent.id`、移液器
  `resolution_ul`（分辨率）与 `min_aspirate_ul`（单次最小吸取量）、
  允许的相对浓度误差 `concentration_tolerance`（如 `0.01` = ±1%）。

每个目标孔的来源**默认为该孔在 layout 中的占孔样本**;稀释梯度/标准曲线/
复孔场景中同一来源常需供给多个孔,此时在该目标上显式给出 `sample_id` 即可;
**同一来源被多个目标共享时只共用一份库存**（各孔来源液抽取量之和不得超过
`available_volume_ul`)。

### 移液与取整规则

目标孔按行主序（A1、A2…)逐孔处理,每孔至多两条、按固定顺序编号的指令:

1. `DISPENSE_DILUENT`:先加稀释液（体积为 0 时该指令整步省略）;
2. `TRANSFER_STOCK`:再转移来源液（体积恒 > 0)。

两体积之和严格等于 `final_volume_ul`,且:

- 来源液体积、稀释液体积与终体积都必须是 `resolution_ul` 的整数倍;
- 每条非零移液都不得小于 `min_aspirate_ul`（纯原液孔不加稀释液,因此不受
  稀释液最小吸取量限制）;
- 实际浓度 `C_actual = C_stock · V_stock / V_final`,要求
  `|C_actual − C_target| / C_target ≤ concentration_tolerance`;
- 在容差内的所有网格解中,选取**最接近理想抽取量**的整数格点;恰好居中时
  取**较小**的来源液体积（最节省库存),保证计划唯一确定。

任一孔因来源不足、终体积超容量、目标浓度高于原液、终体积不在分辨率网格、
最小吸取量无法满足,或取整后无格点能进入容差而无法达标时,**整次请求返回
422 并逐项列出全部原因,不输出任何部分计划**。相同输入必然得到逐字节相同的
结果（指令按孔位行主序、来源按 id 排序,与请求数组顺序无关)。

端口 `8000` 调用示例:

```bash
curl -X POST http://localhost:8000/api/v1/arrangements/dilution-plan \
  -H "Content-Type: application/json" \
  -d '{
    "plate": {"type": "96"},
    "layout": {"S1": "A1", "S2": "A2", "S3": "A3", "S4": "A4"},
    "sources": [
      {"sample_id": "S1", "concentration": 100.0, "available_volume_ul": 500.0},
      {"sample_id": "S2", "concentration": 100.0, "available_volume_ul": 500.0}
    ],
    "targets": [
      {"well": "A1", "sample_id": "S1", "target_concentration": 100.0, "final_volume_ul": 100.0},
      {"well": "A2", "sample_id": "S1", "target_concentration": 50.0,  "final_volume_ul": 100.0},
      {"well": "A3", "sample_id": "S1", "target_concentration": 25.0,  "final_volume_ul": 100.0},
      {"well": "A4", "sample_id": "S2", "target_concentration": 10.0,  "final_volume_ul": 200.0}
    ],
    "well_capacity_ul": 300.0,
    "diluent": {"id": "water"},
    "pipette": {"resolution_ul": 1.0, "min_aspirate_ul": 5.0},
    "concentration_tolerance": 0.01
  }'
```

### 请求字段

| 字段 | 说明 |
| --- | --- |
| `plate` | 板型,同编排接口（默认 96 孔) |
| `layout` | 目标 layout:sample_id → 孔位（编排接口响应中的 `layout`) |
| `sources[]` | `sample_id`、`concentration`(>0)、`available_volume_ul`(≥0);同一样本不得重复 |
| `targets[]` | 每项 `well`(必须在 layout 中)、可选 `sample_id`(省略取占孔样本)、`target_concentration`(>0)、`final_volume_ul`(>0);同孔不得重复 |
| `well_capacity_ul` | 每个目标孔的容量上限(>0) |
| `diluent.id` | 稀释液标识,默认 `"diluent"` |
| `pipette.resolution_ul` | 移液分辨率(>0);所有移液体积必须为其整数倍 |
| `pipette.min_aspirate_ul` | 单次最小吸取量(≥0,默认 0 表示无下限);0 µL 移液直接省略 |
| `concentration_tolerance` | 允许的相对浓度误差(>0) |

### 响应（200)

```json
{
  "plate": {"type": "96", "rows": 8, "columns": 12},
  "diluent": {"id": "water"},
  "pipette": {"resolution_ul": 1.0, "min_aspirate_ul": 5.0},
  "concentration_tolerance": 0.01,
  "well_capacity_ul": 300.0,
  "wells": [
    {
      "well": "A1",
      "sample_id": "S1",
      "layout_sample_id": "S1",
      "target_concentration": 100.0,
      "actual_concentration": 100.0,
      "relative_concentration_error": 0.0,
      "final_volume_ul": 100.0,
      "stock_volume_ul": 100.0,
      "diluent_volume_ul": 0.0,
      "within_tolerance": true,
      "steps": [1]
    },
    {
      "well": "A2",
      "sample_id": "S1",
      "layout_sample_id": "S2",
      "target_concentration": 50.0,
      "actual_concentration": 50.0,
      "relative_concentration_error": 0.0,
      "final_volume_ul": 100.0,
      "stock_volume_ul": 50.0,
      "diluent_volume_ul": 50.0,
      "within_tolerance": true,
      "steps": [2, 3]
    }
  ],
  "sources": [
    {"sample_id": "S1", "concentration": 100.0,
     "available_volume_ul": 500.0, "used_volume_ul": 175.0,
     "remaining_volume_ul": 325.0, "feeds_wells": ["A1", "A2", "A3"]},
    {"sample_id": "S2", "concentration": 100.0,
     "available_volume_ul": 500.0, "used_volume_ul": 20.0,
     "remaining_volume_ul": 480.0, "feeds_wells": ["A4"]}
  ],
  "instructions": [
    {"step": 1, "action": "TRANSFER_STOCK", "well": "A1",
     "sample_id": "S1", "source_id": "S1", "volume_ul": 100.0},
    {"step": 2, "action": "DISPENSE_DILUENT", "well": "A2",
     "source_id": "water", "volume_ul": 50.0},
    {"step": 3, "action": "TRANSFER_STOCK", "well": "A2",
     "sample_id": "S1", "source_id": "S1", "volume_ul": 50.0}
  ],
  "verification": {
    "all_wells_within_tolerance": true,
    "all_volumes_on_resolution_grid": true,
    "all_final_volumes_within_capacity": true,
    "all_sources_within_inventory": true
  }
}
```

- `wells[]`:每孔的目标/实际浓度、相对误差、终体积、来源液与稀释液体积及
  对应的全局步骤号（按孔位行主序);
- `sources[]`:每个来源的总用量、**剩余量**与供给孔清单;
- `instructions[]`:确定性逐步指令（全局连续编号;先稀释液后来源液);
- `verification`:四项自检全部为 `true` 时才可能返回 200。

### 无法配制（422,不输出部分计划)

任一孔无法达标即整次拒绝,`infeasibility_categories` 与 `messages` 逐项列出
**所有**问题孔/来源:

| 类别 | 含义 |
| --- | --- |
| `DILUTION_SOURCE_STOCK_INSUFFICIENT` | 共享同一来源的各孔抽取量之和超过其可用体积（给出缺口 µL) |
| `DILUTION_TARGET_CAPACITY_EXCEEDED` | 终体积超过孔容量上限 |
| `DILUTION_CONCENTRATION_UNACHIEVABLE` | 目标浓度高于来源原液浓度（稀释只能降浓度） |
| `DILUTION_ROUNDING_NO_GRID_SOLUTION` | 终体积不在分辨率网格上,或最小吸取量导致不存在合法拆分 |
| `DILUTION_ROUNDING_TOLERANCE_EXCEEDED` | 分辨率取整后没有任何格点能进入浓度容差（消息给出可达到的最佳浓度与误差） |
| `DILUTION_SOURCE_UNKNOWN` | 目标来源样本未在 `sources` 中给出浓度与体积 |
| `DILUTION_TARGET_WELLS_DUPLICATED` / `DILUTION_SOURCES_DUPLICATED` | 目标孔重复 / 来源样本重复 |
| `WELL_REFERENCE_INVALID` | 孔位非法、越界或不在提交的 layout 中 |
| `REQUEST_VALIDATION_ERROR` | 请求体结构不合法（缺字段、非正数、`NaN`/`Infinity` 等） |

```json
{
  "detail": "No dilution plan can satisfy every target well; no pipetting instructions were generated.",
  "infeasibility_categories": ["DILUTION_SOURCE_STOCK_INSUFFICIENT"],
  "messages": ["source sample 'S1': the 3 target well(s) fed from it require 175.0 µL of stock but only 100.0 µL is available (short by 75.0 µL)"]
}
```

## 双人协作盲化 `POST /api/v1/blinding/blind`

为**既有编排结果**（编排接口响应中的 `layout`，即 sample_id → well 映射）
提供双人协作盲化。提交板型、批次标识和 layout，服务先校验**样本与孔位一一
对应且孔位合法**；任一不满足即返回 422，**不生成盲码、映射包或份额**。

校验通过后：

1. 为每个样本生成由 CSPRNG 产生、**不可从原 sample_id 推断**、**批内唯一**
   的 `blind_id`（形如 `BL-` + 12 位十六进制）；
2. 返回仅含 `well` 与 `blind_id` 的**操作视图**（不含任何 sample_id)；
3. 用 **AES-256-GCM 认证加密**生成映射包，GCM 的 AAD 绑定**批次标识、板型
   与完整操作视图**——映射包无法被重新关联到其他批次、板型，或被截断/
   篡改的视图；
4. 随机 256 位内容密钥以**一次性填充（XOR）拆分**为两份独立份额
   (`share_2 = key XOR share_1`，两份份额均独立随机）。**单份份额本身是
   均匀随机串，与密钥统计独立，不能还原任何对应关系**；必须两份同时到场
   才能重建密钥。

端口 `8000` 调用示例：

```bash
curl -X POST http://localhost:8000/api/v1/blinding/blind \
  -H "Content-Type: application/json" \
  -d '{
    "plate": {"type": "24"},
    "batch_id": "LOT-2026-09-30",
    "layout": {"S1": "A1", "S2": "B2", "S3": "C1", "S4": "C2"}
  }'
```

### 盲化请求字段

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `plate` | object | 板型，与编排接口相同（默认 `"96"`)，必须与编排该 layout 时一致 |
| `batch_id` | string | 批次标识（非空），会被认证绑定进映射包 |
| `layout` | object | sample_id → 孔位映射（编排接口响应中的 `layout`)；非空 |

### 盲化响应（200)

```json
{
  "batch_id": "LOT-2026-09-30",
  "plate": {"type": "24", "rows": 4, "columns": 6},
  "view": [
    {"well": "A1", "blind_id": "BL-cfb60940cddb"},
    {"well": "B2", "blind_id": "BL-59e00ba4f98f"},
    {"well": "C1", "blind_id": "BL-4b2d5344f730"},
    {"well": "C2", "blind_id": "BL-91af33b0e5d2"}
  ],
  "mapping_package": "AfFCe2GrfcuvZ7J08v1oEl37mva6dUFQ...（urlsafe-base64）",
  "key_shares": [
    {"holder": 1, "share": "7YzmRydLc0wC...（urlsafe-base64，分发给持有人1）"},
    {"holder": 2, "share": "tLB1j6_l4fbK...（urlsafe-base64，分发给持有人2）"}
  ],
  "security": {
    "cipher": "AES-256-GCM",
    "key_splitting": "random 256-bit key split into two XOR one-time-pad shares; either single share is uniform and reveals nothing",
    "package_binds": ["batch_id", "plate", "complete_operational_view"],
    "unblinding_requires": ["batch_id", "complete_operational_view",
                           "mapping_package", "two_distinct_key_shares"]
  }
}
```

- `view`：给操作人员的视图，**只有孔位与盲码**，按行优先排序；每次盲化
  都会重新随机生成盲码，重复调用不产生相同盲码。
- `mapping_package`：自包含的 urlsafe-base64 密文包
  （版本字节 ‖ 12 字节 nonce ‖ GCM 密文‖16 字节认证标签）；包内密文不
  出现明文 sample_id。
- `key_shares`：两份份额应**分别交给两名持有人独立保管**。

### 盲化失败（422，不产出任何盲码/映射包/份额）

| 类别 | 含义 |
| --- | --- |
| `WELL_REFERENCE_INVALID` | 孔位名称非法或超出板型范围 |
| `BLIND_WELLS_DUPLICATED` | 多个样本对应同一孔位（大小写不敏感，如 `a1` 与 `A1`) |
| `BLIND_SAMPLE_WELL_MISMATCH` | 样本—孔位一一对应关系不成立（如空样本 id) |
| `PLATE_DIMENSION_INVALID` | 板型尺寸非法 |
| `REQUEST_VALIDATION_ERROR` | 请求体结构不合法（字段缺失/类型错误、`layout` 为空等） |

## 双人协作解盲 `POST /api/v1/blinding/unblind`

解盲必须**同时**提交：批次标识、**原完整操作视图**、映射包和**两份不同
份额**。完整性（密文/AAD 认证）、份额或批次任一不符即返回 422，
**不泄露任何部分映射**（错误信息不区分究竟是批次、板型、视图、包还是
份额出错，也绝不回显份额/密钥/映射内容）。成功时**一次性**返回
`blind_id`、`sample_id`、`well` 全量对应。视图与份额的提交顺序不敏感，
孔位大小写不敏感。

端口 `8000` 调用示例（实际使用时把上一接口返回的视图、包与两份份额原样
填入；两份份额由两名持有人分别提供）：

```bash
curl -X POST http://localhost:8000/api/v1/blinding/unblind \
  -H "Content-Type: application/json" \
  -d '{
    "batch_id": "LOT-2026-09-30",
    "plate": {"type": "24"},
    "view": [
      {"well": "A1", "blind_id": "BL-cfb60940cddb"},
      {"well": "B2", "blind_id": "BL-59e00ba4f98f"},
      {"well": "C1", "blind_id": "BL-4b2d5344f730"},
      {"well": "C2", "blind_id": "BL-91af33b0e5d2"}
    ],
    "mapping_package": "AfFCe2GrfcuvZ7J08v1oEl37mva6dUFQ...",
    "key_shares": ["7YzmRydLc0wC...", "tLB1j6_l4fbK..."]
  }'
```

### 解盲响应（200)

```json
{
  "batch_id": "LOT-2026-09-30",
  "plate": {"type": "24", "rows": 4, "columns": 6},
  "revealed": [
    {"blind_id": "BL-cfb60940cddb", "sample_id": "S1", "well": "A1"},
    {"blind_id": "BL-59e00ba4f98f", "sample_id": "S2", "well": "B2"},
    {"blind_id": "BL-4b2d5344f730", "sample_id": "S3", "well": "C1"},
    {"blind_id": "BL-91af33b0e5d2", "sample_id": "S4", "well": "C2"}
  ]
}
```

### 解盲失败（422，不泄露部分映射）

| 类别 | 含义 |
| --- | --- |
| `BLIND_INTEGRITY_FAILED` | GCM 认证失败：批次、板型、完整视图、映射包或重建密钥任一不符（含篡改包/视图、批次不符、板型不符、份额组合错误） |
| `BLIND_PACKAGE_INVALID` | 映射包不是合法 base64、版本或结构不受支持 |
| `BLIND_SHARE_INVALID` | 份额不是合法 base64、长度不是 32 字节，或两份份额相同 |
| `BLIND_VIEW_INVALID` | 视图为空、孔位非法、视图内孔位或 blind_id 重复 |
| `BLIND_VIEW_INTEGRITY_FAILED` | 解密成功但包内绑定的批次/板型/视图与提交项不一致（防御性二次校验） |
| `REQUEST_VALIDATION_ERROR` | 请求体结构不合法（缺少份额、字段缺失/类型错误等） |

### 密钥与日志安全

- 内容密钥仅存在于盲化请求处理过程中，响应只返回两份份额，服务不保存密钥；
- **请求与错误日志均不记录明文份额、密钥与明文映射**：失败响应只含通用
  说明与公开标识（孔位名、blind_id)，绝不回显份额、映射包或对应关系；
- 盲码、nonce、密钥与两份份额均来自 `secrets` CSPRNG；
- 盲化是**非确定性**操作（每次盲码/份额/包都不同），这与编排接口的
  确定性保证互不影响：同一 layout 可多次盲化，解盲时须使用配对的那次
  视图、包与份额。

## 确定性保证

- 求解器固定为**单 worker**、固定 `random_seed`，并以**确定性时间预算**
  (`max_deterministic_time`）而非墙钟时间作为主要停止条件；
- 第三阶段把第二阶段的最优解作为完整 hint 回注并取首个解，
  因此相同（输入， seed）在相同依赖版本下必然产生逐孔一致的布局；
- 响应中的 `layout` 可直接作为回归测试基准；
- 空间质控接口的组内置换使用 seed 初始化的独立确定性随机流，相同
  （输入， seed)（含 `permutations`/`tolerance`/`max_iterations`)
  必然得到逐位一致的效应、校正读数、收敛轨迹与 p 值。
- 批次放行核验完全复用上述纠位规则与空间质控流程，相同
  （输入， seed)（含 `max_spatial_effect`/`min_p_value` 等门控参数）
  必然得到相同的错孔清单、放行结论、门控观测值与质控摘要。
- 稀释梯度配制计划是纯算术规划(无随机源):目标孔按行主序处理,容差内
  网格点以"最接近理想抽取量、居中取较小值"唯一决胜,来源按 id 排序,
  相同输入必然得到逐字节相同的指令、实际浓度与来源余量。
- 批次证书完全复用上述放行核验:相同（放行请求， 视图）必然得到相同的
  冻结快照与内容 `digest`;成功重核验在同一事务中追加新 revision 的不可变
  证据快照并重算 `digest`，相同（新 scan/readings）必然得到相同的新
  `digest`;证书状态（`revision`/`REVOKED`）由 SQLite 事务保证单调推进——
  revision 只增不减，吊销不可逆。

## 配置（环境变量）

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `CP_NUM_WORKERS` | `1` | CP-SAT 搜索线程数（>1 会破坏确定性，不建议修改） |
| `CP_DETERMINISTIC_TIME_SECONDS` | `4` | 每个优化阶段的确定性时间预算 |
| `CP_WALL_TIME_SECONDS` | `12` | 每个优化阶段的墙钟时间硬上限（安全阀） |
| `CP_FAST_DETERMINISTIC_TIME_SECONDS` | `1` | 快速保底/决胜阶段的确定性预算 |
| `CP_FAST_WALL_TIME_SECONDS` | `8` | 快速保底/决胜阶段的墙钟上限 |
| `CERT_DB_PATH` | `./certificates.db` | 批次证书 SQLite 数据库路径（Docker Compose 中为 `/data/certificates.db`，挂载命名卷 `microplate-data` 持久化） |

预算耗尽时返回当前最优可行解（`solver.status = FEASIBLE`)，两级指标仍是
字典序意义下预算内最优；找不到任何可行解时返回 503（不返回部分布局）。

## 测试

```bash
pip install -r requirements-dev.txt
pytest tests -q
```

## 项目结构

```
app/
  main.py      FastAPI 应用与异常处理（200/201/404/409/422/503)
  schemas.py   请求/响应模型与不可行类别常量
  planner.py   三阶段字典序 CP-SAT 求解（平衡 → 邻接 → 确定性决胜）
  reconciler.py 扫描核对与最少步数纠位指令生成（空孔链 → 闭环暂存）
  spatial_qc.py 组中位数中心化 + 行列中位数剥离（median polish）与组内置换检验
  release.py   批次放行两级门控:扫描吻合核验（复用纠位规则）→ 空间质控放行门
  certificates.py 批次证书:放行结果冻结快照（SQLite 单事务）、按 revision 追加不可变证据快照的重核验（通过则重算 digest,失败则吊销）、request_key 幂等
  dilution.py  稀释梯度配制:共享库存核算 + 分辨率取整 + 容差校验,确定性逐步移液指令
  blinding.py  双人协作盲化/解盲:AES-256-GCM 认证加密映射包 + XOR 双份额
  metrics.py   指标独立重算与布局约束校验
  plates.py    板型预设与孔位命名解析
tests/         pytest 测试（解析、求解、API、纠位、空间质控、批次放行、批次证书、稀释计划、盲化解盲）
Dockerfile     单容器构建
docker-compose.yml  单命令启动（含证书数据库命名卷）
```
