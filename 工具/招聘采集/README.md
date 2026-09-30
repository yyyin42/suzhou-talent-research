# 招聘信息采集与汇总框架

课题五企业端数据采集工具。多渠道、增量更新、合规优先。

> 前身是单平台的 `sndhr_collect.py`（GitHub 仓库中在 `legacy/`，本地工程在 `../`，保留不动）。
> 本框架在其基础上重构：统一字段、多渠道适配、跨批次增量去重、
> 批次登记、质量标记、日志与导出一体化。

## 一、目录结构

```text
工具/招聘采集/
├── README.md          本说明
├── config.yaml        唯一需要日常修改的文件
├── __init__.py
├── model.py           统一字段模型 + 标准化（清洗/薪资解析/质量分档/岗位族初筛）
├── httpclient.py      网络层（限速/退避重试/401/403即停/请求日志）
├── adapters.py        渠道适配器（sndhr / generic_json / csv_import）
├── store.py           CSV 状态库（增量判断/跨渠道去重/批次登记）
├── run.py             主入口
├── selftest.py        离线自测（不访问任何网站）
└── data/              运行后生成：主库、日志、导出
    ├── state/master.csv      全量记录（唯一事实来源，只追加）
    ├── state/batches.csv     批次登记表
    └── logs/                 每次运行的完整日志
```

## 二、快速开始

```bash
# 进入「招聘采集」文件夹的上级目录（本地工程是 数智调研大赛/工具，仓库克隆后是仓库根目录）
cd <上级目录>
PY="C:/Users/超越自我/.workbuddy/binaries/python/envs/default/Scripts/python.exe"
# 其他电脑：PY=python（需已装 pyyaml 和 openpyxl）

# 0) 自测（离线，不访问网络）——改过代码后必跑
$PY -m 招聘采集.selftest

# 1) 试跑 sndhr 前 5 条，不写库
#    （先在 config.yaml 的 sndhr 段解开 limit: 5 注释）
$PY -m 招聘采集.run --dry-run

# 2) 正式采集（按 config.yaml 的启用渠道）
$PY -m 招聘采集.run

# 3) 只跑指定渠道 / 只重新导出
$PY -m 招聘采集.run --channels sndhr
$PY -m 招聘采集.run --export-only
$PY -m 招聘采集.run --export-only --format csv --out 输出文件名.csv
```

依赖：`pyyaml`、`openpyxl`（导出 xlsx 时）。托管环境已装好。

## 三、配置说明（config.yaml）

| 段 | 字段 | 说明 |
| --- | --- | --- |
| `collector` | | 采集人姓名，写进每条记录；**正式批次必填** |
| `data_root` | | 数据目录，相对本文件夹 |
| `filters.title_keywords` | | 岗位名关键词白名单；留空 = 全收，靠人工复核岗位族 |
| `filters.districts` | | 区县白名单（匹配标准化后的 district 字段） |
| `filters.platforms` | | 平台白名单 |
| `channels.<名>` | `adapter` | 适配器类型：`sndhr` / `generic_json` / `csv_import` |
| | `enabled` | 是否参与采集 |
| | `compliance_confirmed` | **合规确认开关**。缺 `true` 时框架拒绝采集该渠道 |
| | `delay_seconds` | 请求间隔，代码强制下限 1 秒 |
| | `with_detail` | 是否采详情（岗位描述原文） |
| | `limit` | 只采前 N 条（试跑用） |
| `export.format` | | `xlsx` 或 `csv` |

### 新增渠道的标准流程

1. **先评估、后配置**：按《数据采集交接报告》6.5 完成渠道合规评估并留档；
2. 在 `config.yaml` 加配置段（`generic_json` 适配器适合"公开 JSON API + 明确分页参数"的站点）；
3. `enabled: false` 状态下用 `--dry-run` 小规模试跑（`max_pages` 设小）；
4. 确认字段映射正确、无访问限制后，把 `enabled` 和 `compliance_confirmed` 都改 `true`；
5. 首次正式采集后核对 `data/state/batches.csv` 里的新增数与预期是否一致。

## 四、数据口径（重要）

- **主键**：`uid = source_platform + source_id`。导出表里的岗位序号不跨批次稳定，跨批次去重以 uid 为准；
- **三层去重**：① 同渠道内接口 id → ② 跨批次 uid → ③ 跨渠道内容键（企业名+岗位名+地点，规范化后）；
- **增量语义**：`new`（首次）/ `duplicate`（uid 或内容重复）/ `updated`（同 uid 但描述或薪资变化）；
- **质量分档**：`valid`（≥50 字，可进技能提取）/ `reference`（20–49 字，只做结构统计）/ `minimal`（<20 字，单独披露）；
- **岗位族初筛**（`job_family_guess`）：关键词匹配的**候选建议**，不是最终分类，最终须人工复核（对应交接报告 S1 阶段的双人试编码）。

## 五、验证逻辑

- `python -m 招聘采集.selftest`：6 项离线测试（文本清洗、薪资解析、质量分档、岗位族初筛、去重键、增量入库全链路），**改代码后必跑**；
- 每次运行结束看两处：控制台的批次统计（新增/重复/更新/失败）和 `data/logs/` 里的完整请求日志；
- `sndhr` 适配器内置跨页重复告警：列表条数与去重后条数不一致会在日志里 `WARN`，提示分页参数可能出错。

## 六、合规红线（代码层强制）

1. 401/403/429 → 立即停止该渠道，不重试、不绕过（`httpclient.AccessDenied`）；
2. `delay_seconds` 下限 1 秒（代码 clamp，配置写 0.5 也会被提到 1.0）；
3. 渠道必须 `compliance_confirmed: true` 才会真正采集；
4. 不采集联系方式（手机号/邮箱不入库；描述原文里的联系方式在清洗阶段仅去 HTML，**人工复核时注意不要外传**）；
5. 商业平台数据只能走 `csv_import` 人工导入路径，不存在商业平台的自动抓取适配器。

## 七、已知边界与待反馈事项

- **24365 平台**：`generic_json` 配置段已预留但字段全空。该站接口结构未评估（评估属 S1 阶段任务），有眉目后按"新增渠道流程"补全；
- **CnOpenData**：走授权文件导入（`csv_import`），等老师申请结果；
- **sndhr 正式首采**：技术验证已通过，但按交接报告的放行标准，正式基线应在 S0/S1（检索词表定稿 + 30–50 条双人试编码 + 老师确认）之后执行。框架就绪 ≠ 现在就该跑全量；
- **描述字数统计**：按字符数（含中英文标点），与交接报告口径一致。
