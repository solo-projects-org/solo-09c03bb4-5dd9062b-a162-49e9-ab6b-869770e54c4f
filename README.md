# 泵站振动频带能量核对系统

给泵站振动分析员在一次试车中**对照不同时段的频带能量**用的小型 Web 工具。
Flask + SQLite + NumPy 单体应用，Docker Compose 一键启动，附跨段样例。

- 采样率固定 **256 Hz**，采样段（float64 BLOB）存入 SQLite；
- 按**起始样点**上传**单列采样 CSV**，可**整段换报**、**撤段**；
- 段间样点区间重叠一律 **409 拒收**（相邻端点相接允许）；
- 窗长 **256 点**，从样点 0 起每隔 **128 点**一窗；窗可跨段拼接，**缺一样都不计**；
- NumPy `np.fft.rfft` 计算**矩形窗单边谱**，谱线按 Parseval 归一化为均方贡献；
- 网页选择**样点闭区间**与**频带**，返回**完整落入**时段的**有效窗数**和这些窗的
  **平均带内均方值**（附 RMS、逐窗明细、多时段对照表、覆盖时间线）；
- 无有效窗时结果明确标 **缺测**；
- 段增 / 换 / 撤时，**只重算与该段旧范围、新范围各自相交的窗的并集**，
  变残缺的窗立即从谱表撤下；段与窗谱在**同一事务**提交，任何失败整体回滚、旧状态不变；
- 能量查询**只读取 `window_spectrum` 谱表**，不回读段内原始采样
  （有 SQLite authorizer 测试强制验证）。

---

## 1. Docker Compose 启动（推荐）

```bash
docker compose up --build
```

打开 <http://localhost:8000>。SQLite 数据库持久化在宿主机 `./data/vibration.db`。

容器等价执行：

```bash
gunicorn --bind 0.0.0.0:8000 --workers 1 --threads 4 --timeout 60 wsgi:app
```

健康检查：`GET /api/health`。

> 本仓库在交付环境中没有 Docker 守护进程，因此镜像构建 / `compose up` 未能在本机实跑；
> 但 `docker-compose.yml` 已通过 YAML 解析核对，且完全相同的 gunicorn 启动命令已在本地
> 用 gunicorn 23 实跑验证（见文末「实测记录」）。

## 2. 不用 Docker 的本地运行

需要 Python 3.11+，并能安装 Flask / NumPy：

```bash
python3 -m pip install -r requirements.txt
python wsgi.py                 # 等价于 python -m flask run，监听 0.0.0.0:8000
# 或：gunicorn --bind 0.0.0.0:8000 wsgi:app
```

数据库路径可用环境变量 `DB_PATH` 覆盖（默认 `data/vibration.db`），
示例目录可用 `SAMPLE_DIR` 覆盖（默认仓库内 `samples/`）。

运行测试：

```bash
python3 -m pip install pytest
PYTHONPATH=. python3 -m pytest
```

当前 **42 个测试全部通过**，覆盖窗格边界、谱公式、重叠拒收、跨段数值、
增量范围、撤段、事务回滚、「查询不读 segment 表」与全部主要 API。

---

## 3. 网页试用（30 秒路径）

1. 打开首页，点右上角 **「载入跨段示例」**（已有数据时会确认并整体重置）。
2. 页面给出三个段与窗覆盖时间线：绿色有效窗、红色斜纹残缺窗、橙色查询区间。
3. 已预填 `样点 0–1023`、`频带 15–25 Hz`，直接点 **查询能量**：
   **有效窗 5 个，平均带内均方 = 2.000000，RMS ≈ 1.414214**。
4. 点时段快选 **「跨段窗 128–511」**：窗 1、2 由 A/B 两段拼成，计数 2。
5. 点 **「缺口时段 512–895（缺测）」**：窗 4、5 完整落入区间但因 50 点缺口残缺，
   结果显示醒目的 **缺测**。
6. 用频带快选核对 55 Hz（≈0.18）、90 Hz（≈0.045）、全频带（≈2.225）。
7. 点 **「把本次结果加入对照表」**，再换一个时段 / 频带查询并加入，即可横向对照。
8. 在段清单每行可 **整段换报**（弹层中可改起始样点）或 **撤段**；
   操作响应里带本次触及 / 入库 / 撤下的窗编号，时间线随即刷新。
9. 也可以直接点时间线上的任意窗块，把查询区间设为该窗。

### 附带的跨段样例

`samples/` 下三个单列 CSV 由 `samples/generate_samples.py` 确定性生成：

| 文件 | 全局样点区间 | 点数 | 作用 |
|---|---|---:|---|
| `demo_seg1_start0.csv` | `[0,300)` | 300 | 段 A |
| `demo_seg2_start300.csv` | `[300,650)` | 350 | 段 B，与 A 相邻；窗 1、2 跨 A/B |
| `demo_seg3_start700.csv` | `[700,1024)` | 324 | 段 C；`[650,700)` 留 50 点缺口 |

信号为 `2.0·sin(2π20t) + 0.6·sin(2π55t+0.7) + 0.3·sin(2π90t)`，
三个频率在 256 点窗内都是整周期，因此无数谱泄漏，数值可精确核对。
窗格明细见 [`samples/README.md`](samples/README.md)。

手动 curl 上传：

```bash
curl -F start_sample=0   -F file=@samples/demo_seg1_start0.csv  http://localhost:8000/api/segments
curl -F start_sample=300 -F file=@samples/demo_seg2_start300.csv http://localhost:8000/api/segments
curl -F start_sample=700 -F file=@samples/demo_seg3_start700.csv http://localhost:8000/api/segments
```

---

## 4. 计算口径（与页面 / API 完全一致）

### 4.1 窗

- 第 `k` 个窗（`k=0,1,2,…`）覆盖半开区间 `[128k, 128k+256)`，
  即闭区间样点 `[128k, 128k+255]`，对应时间 `[k/2, k/2+1)` 秒。
- 与半开段范围 `[a,b)` 相交的窗编号范围：

  ```
  k_lo = max(0, floor((a-256)/128) + 1)
  k_hi = floor((b-1)/128)
  ```

- 组装时按全局样点下标从所有相交段拷贝；用覆盖掩码检查 256 点是否齐全。
  **缺任意一点 → 该窗无效**：不计算 / 不保存谱；若此前有谱则删除（撤下）。

### 4.2 矩形窗单边谱与均方值

```python
X = np.fft.rfft(x * np.ones(256))      # 矩形窗 = 不锥化
p[0]     = |X[0]|² / 256²               # DC
p[128]   = |X[128]|² / 256²             # Nyquist
p[1:128] = 2 |X[1:128]|² / 256²         # 其余单边谱线加倍
```

于是 `sum(p) == mean(x²)`（测试按 Parseval 验证到 1e-12）。
谱线间隔恰为 1 Hz（`fs/N = 256/256`），频带 `[f_low, f_high]` Hz（闭区间）
按**谱线中心频率**选线，窗的带内均方值 = 所选谱线 `p[k]` 之和。

### 4.3 时段统计

网页结束样点按**含端点**理解。窗 `[w0,w0+256)` 完整落入闭区间 `[s,e]` 当且仅当

```
s ≤ w0 且 w0 ≤ e + 1 − 256
```

在 `window_spectrum` 中按 `window_start` 区间取出这些**有效窗**，
计数 `W`，返回

```
avg_band_ms = (1/W) Σ_w band_ms_w
band_rms    = sqrt(avg_band_ms)
```

`W=0` 时 `status="missing"`、`avg_band_ms=null"`，网页显示「缺测」。
注意：查询区间可以比数据范围大；只要某有效窗完整落在区间内就会计数。

---

## 5. HTTP API 速览

| 方法 / 路径 | 说明 |
|---|---|
| `GET /` | 网页 |
| `GET /api/health` / `GET /api/config` | 健康检查 / 运行配置 |
| `GET /api/overview` | 段清单、有效窗起点、期望窗起点（供时间线） |
| `POST /api/segments` | 新段。multipart：`start_sample`、`file`、可选 `name`；或 JSON `{start_sample, csv_text, name}`。重叠 → 409 |
| `POST /api/segments?replace=1` | 同一入口换报：用 `segment_id` 或 `start_sample` 定位旧段 |
| `POST/PUT /api/segments/<id>/replace` | 整段换报（可在表单另给 `start_sample` 改范围） |
| `PUT/POST /api/segments/<id>` | 同上（REST 风格） |
| `PUT/POST /api/segments/by-start/<start>/replace` | 以起始样点定位并换报 |
| `DELETE /api/segments/<id>` | 撤段 |
| `DELETE /api/segments` | 清空全部 |
| `GET /api/energy?start_sample=&end_sample=&f_low=0&f_high=128` | **只查谱表**的能量结果 |
| `POST /api/demo/load` | 载入跨段示例，JSON `{"reset": true}` 先清空 |
| `GET /api/demo/files` / `/api/demo/raw/<file>` | 示例文件清单 / 原文下载 |

所有错误返回 JSON：`{"error": "中文原因", "details": {...}}` 与对应 4xx/5xx。
成功的段写操作额外返回 `reindex`：`window_indices / stored_indices / withdrawn_indices`
（含对应 `*_starts` 与计数），可直接核对增量范围。

### 数据库结构

```sql
segment(id, start_sample, n_samples, fs=256, name, source_file,
        samples BLOB float64, created_at, updated_at)
window_spectrum(window_start INTEGER PRIMARY KEY,   -- 必为 128 的倍数
                n_samples=256, fs=256, n_bins=129,
                power BLOB float64[129],             -- 单边均方谱线
                total_ms REAL, updated_at)
```

---

## 6. 关键正确性保证是怎么落地的

1. **重叠拒收**：写入前用
   `start_sample < :end AND start_sample+n_samples > :start`
   查冲突段；换报带 `id != 自身` 排除。冲突返回 409 和冲突段信息。
2. **只动该动的窗**：换报计算「旧范围相交窗 ∪ 新范围相交窗」，
   两段相距很远时取并集而**不是包围盒**（有测试断言中间窗不被触及、
   未触及窗的 `updated_at` 原样不变）。
3. **事务原子性**：段 INSERT/UPDATE/DELETE 与所有窗谱 upsert/delete
   都在同一个 `with conn:` 事务里；谱计算注入异常的测试证明段插入会回滚、
   旧谱 BLOB 不变。
4. **查询不重读采样**：`query_band_energy()` 的 SQL 只出现
   `window_spectrum`；测试给连接装上 SQLite authorizer，
   一旦读取 `segment` 表立即 `SQLITE_DENY`，查询仍正确通过。
5. **CSV 健壮性**：UTF-8-BOM、空行、首行表头（即使含逗号）、
   尾随空字段可容忍；真正的多列、中途非数字、NaN/Inf 给出行号级 400。

---

## 7. 目录结构

```
/workspace
├── app/
│   ├── __init__.py        # create_app、配置、建表
│   ├── dsp.py             # 窗格/覆盖判定、rfft 单边均方谱、频带选线
│   ├── csvio.py           # 单列 CSV 解析
│   ├── db.py              # SQLite 连接、schema
│   ├── services.py        # 段增换撤、增量索引、能量查询、总览
│   ├── demo.py            # 跨段示例的原子载入
│   ├── web.py             # Flask 蓝图：页面 + JSON API
│   ├── templates/index.html
│   └── static/{style.css,app.js}
├── samples/               # 跨段 CSV + 生成脚本 + 说明
├── tests/                 # 42 个 pytest 用例
├── wsgi.py                # gunicorn wsgi:app
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
└── pytest.ini
```

---

## 8. 实测记录（交付前）

- `python3 -m pytest`：**42 passed**。
- 用 gunicorn 23 实跑 `wsgi:app`，curl 实测：
  - `/`、`/static/app.js`、`/static/style.css` 均 200；
  - 示例一键载入：三段 id/范围正确，三批增量窗分别为
    `[0,1,2]`、`[1,2,3,4,5]`、`[4,5,6,7]`；
  - `0–1023 / 15–25 Hz` → `ok, count=5, avg=2.000000`；
  - `128–511` → 2 个跨段窗 `[1,2]`；
  - `512–895 / 0–128 Hz` → `missing, 0, null`；
  - 重叠上传 → `409` 且错误信息指出冲突段；
  - multipart 整段换报、按起点换报、撤段均正确；撤 B 后有效窗只剩 `{0,768}`；
  - 直接查 SQLite：`window_spectrum` 每行 129 个 float64，`total_ms≈2.225`。
- 用 jsdom 加载真实页面与 `app.js`、对接运行中的服务：
  **19/19 项 DOM 交互检查通过、无页面脚本错误**
  （段表 3 行、有效/残缺窗 5/3、查询数值、缺测徽标、频带切换、
  时间线点窗、对照表加行、换报弹层开关）。
- `node --check app/static/app.js` 通过。

## 9. 还没做完 / 已知边界

- **未在本环境真正执行 `docker compose up --build`**：环境中没有 docker
  可执行文件与守护进程；只做了 Compose YAML 解析核对 + 同命令本地 gunicorn 验证。
  镜像拉取 / 构建链路（PyPI、`python:3.12-slim`）需在有 Docker 的机器上首验。
- UI 自动化用的是 **jsdom 而非真实 Chromium**：验证了 DOM 逻辑与事件，
  但没有真实像素布局 / 截图；CSS 在窄屏下做了响应式规则，未做浏览器兼容性实测。
- 频带边界按 **FFT 整数 Hz 谱线中心包含**处理；不支持把非整数 Hz 边界按
  功率比例劈分到相邻谱线（当前 fs=N=256 使谱线恰为整数 Hz，常规选带不受影响）。
- 定位为试车核对级单机工具，**无登录 / 多用户 / 审计权限**；
  SQLite 单 worker 多线程部署。超大规模（数十万段 / 窗）下能量查询需按
  `window_start` 范围解码 BLOB，可再加工整表或聚合表，目前规模无压力。
- 未提供报告导出（CSV/PDF）与波形 / 频谱曲线图；已有逐窗明细表、
  时段对照表和覆盖时间线，原始数据都可经 API 取到。
