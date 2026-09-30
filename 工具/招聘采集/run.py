# -*- coding: utf-8 -*-
"""采集主管道：配置加载 -> 渠道调度 -> 增量入库 -> 筛选 -> 导出。

用法（见 README）：
  python -m 招聘采集.run                    # 按配置采集全部启用渠道
  python -m 招聘采集.run --dry-run          # 只读试跑，不写库
  python -m 招聘采集.run --channels sndhr   # 只跑指定渠道
  python -m 招聘采集.run --export-only      # 不采集，只重导出
"""

import argparse
import json
import os
import sys
from datetime import datetime

import yaml

from .adapters import build_adapter
from .httpclient import AccessDenied, Logger
from .model import FIELDS, JobRecord
from .store import Store

HERE = os.path.dirname(os.path.abspath(__file__))


# ---------------------------------------------------------------- 配置
def load_config(path):
    with open(path, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    _validate_config(cfg)
    return cfg


def _validate_config(cfg):
    channels = cfg.get("channels") or {}
    if not channels:
        return
    for name, ch in channels.items():
        if not ch.get("adapter"):
            raise ValueError(f"渠道 {name} 缺少 adapter 字段")
        if ch.get("delay_seconds", 2.0) < 1.0:
            raise ValueError(f"渠道 {name} 的 delay_seconds 不得低于 1 秒")
        if ch.get("generic_max_pages") and ch["generic_max_pages"] > 50:
            raise ValueError(f"渠道 {name} 评估期页数上限 50")


def apply_filters(records, filters, logger):
    """按配置过滤记录；被过滤的记录标记 excluded 并保留理由（不静默丢数据）。"""
    keywords = filters.get("title_keywords") or []
    districts = filters.get("districts") or []
    platforms = filters.get("platforms") or []

    kept = []
    for rec in records:
        if platforms and rec.source_platform not in platforms:
            rec.status = "excluded"
            rec.exclude_reason = f"平台 {rec.source_platform} 不在筛选范围"
        elif districts and rec.district and rec.district not in districts:
            rec.status = "excluded"
            rec.exclude_reason = f"区县 {rec.district} 不在研究范围"
        elif keywords:
            title = rec.job_title.lower()
            if not any(k.lower() in title for k in keywords):
                rec.status = "excluded"
                rec.exclude_reason = "岗位名未命中关键词表"
        if rec.status != "excluded":
            kept.append(rec)
    if keywords or districts or platforms:
        logger.log(f"筛选：{len(kept)}/{len(records)} 条保留"
                   f"（关键词 {len(keywords)} 个，区县 {len(districts)} 个）")
    return kept


# ---------------------------------------------------------------- 导出
def export_excel(store, out_path, filters=None):
    """把主库导出为 Excel（岗位总表 + 原始字段留档 + 质量透视三张表）。"""
    try:
        from openpyxl import Workbook
        from openpyxl.styles import Font, Alignment
    except ImportError:
        print("缺少 openpyxl：pip install openpyxl（或改用 --format csv）")
        sys.exit(1)

    rows = store.all_rows()
    if filters:
        rows = [r for r in rows if _row_pass(r, filters)]

    wb = Workbook()
    ws = wb.active
    ws.title = "岗位总表"
    ws.append(FIELDS)
    for c in ws[1]:
        c.font = Font(bold=True)
        c.alignment = Alignment(horizontal="center")
    for r in rows:
        ws.append([r.get(k, "") for k in FIELDS])
    ws.freeze_panes = "A2"
    ws.column_dimensions["S"].width = 80

    # 质量统计表
    ws2 = wb.create_sheet("质量统计")
    s = store.stats()
    ws2.append(["指标", "数值"])
    ws2.append(["总记录数", s["total"]])
    ws2.append(["— 按质量"] + [])
    for k, v in s["by_quality"].items():
        ws2.append([f"  {k}", v])
    ws2.append(["— 按岗位族初筛"] + [])
    for k, v in sorted(s["by_family"].items(), key=lambda x: -x[1]):
        ws2.append([f"  {k}", v])
    ws2.append(["— 按来源平台"] + [])
    for k, v in s["by_platform"].items():
        ws2.append([f"  {k}", v])

    # 批次登记表
    ws3 = wb.create_sheet("批次登记")
    ws3.append(["batch_id", "datetime", "channels", "collector",
                "new", "duplicate", "updated", "failed", "total_in_store", "notes"])
    batches_path = os.path.join(store.state_dir, "batches.csv")
    if os.path.exists(batches_path):
        import csv as _csv
        with open(batches_path, "r", encoding="utf-8-sig", newline="") as f:
            for row in _csv.DictReader(f):
                ws3.append([row.get(k, "") for k in
                            ["batch_id", "datetime", "channels", "collector",
                             "new", "duplicate", "updated", "failed",
                             "total_in_store", "notes"]])

    wb.save(out_path)
    return len(rows), out_path


def export_csv(store, out_path, filters=None):
    import csv
    rows = store.all_rows()
    if filters:
        rows = [r for r in rows if _row_pass(r, filters)]
    with open(out_path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in FIELDS})
    return len(rows), out_path


def _row_pass(row, filters):
    if filters.get("platforms") and row.get("source_platform") not in filters["platforms"]:
        return False
    if filters.get("districts") and row.get("district") not in filters["districts"]:
        return False
    kws = filters.get("title_keywords") or []
    if kws:
        t = (row.get("job_title") or "").lower()
        if not any(k.lower() in t for k in kws):
            return False
    return True


# ---------------------------------------------------------------- 主流程
def run(args):
    cfg_path = args.config
    if not os.path.isabs(cfg_path):
        cfg_path = os.path.join(HERE, cfg_path)
    cfg = load_config(cfg_path)

    data_root = os.path.normpath(os.path.join(HERE, cfg.get("data_root", "data")))
    os.makedirs(data_root, exist_ok=True)
    log_dir = os.path.join(data_root, "logs")
    os.makedirs(log_dir, exist_ok=True)
    log_file = os.path.join(log_dir, f"run_{datetime.now():%Y%m%d_%H%M%S}.log")
    logger = Logger(logfile=log_file, quiet=args.quiet)
    logger.log(f"配置：{cfg_path}")
    logger.log(f"数据目录：{data_root}")

    store = Store(data_root)
    collector = cfg.get("collector", "")
    filters = cfg.get("filters") or {}

    if args.export_only:
        _do_export(store, cfg, data_root, args)
        return

    requested = set(args.channels.split(",")) if args.channels else None
    batch_id = store.next_batch_id()
    logger.log(f"批次号：{batch_id}　采集人：{collector or '（未配置）'}")

    stats = {"new": 0, "duplicate": 0, "updated": 0, "failed": 0}
    ran_channels = []
    denied_channels = []

    for name, ch_cfg in (cfg.get("channels") or {}).items():
        if requested and name not in requested:
            continue
        if not ch_cfg.get("enabled", False):
            logger.log(f"渠道 {name}：未启用，跳过（评估通过后在配置中开启）")
            continue
        if ch_cfg.get("compliance_confirmed") is not True:
            logger.log(
                f"渠道 {name}：配置缺少 compliance_confirmed: true，"
                f"视为未完成合规确认，跳过。请先完成渠道合规评估并留存记录。",
                level="WARN")
            continue

        ran_channels.append(name)
        logger.log(f"—— 渠道 {name}（{ch_cfg.get('adapter')}）开始 ——")
        try:
            adapter = build_adapter(name, ch_cfg, logger, batch_id, collector)
            records = list(adapter.fetch())
            records = apply_filters(records, filters, logger)
            for rec in records:
                status = store.upsert(rec)
                stats[status] = stats.get(status, 0) + 1
            logger.log(f"渠道 {name} 完成：{len(records)} 条处理")
        except AccessDenied as e:
            denied_channels.append(name)
            stats["failed"] += 1
            logger.log(f"渠道 {name} 被拒绝访问，已停止：{e}", level="ERROR")
            logger.log("按合规纪律不重试、不绕过；请人工核查平台规则后更新配置或弃用该渠道",
                       level="ERROR")
        except Exception as e:
            stats["failed"] += 1
            logger.log(f"渠道 {name} 失败：{e}", level="ERROR")

    if args.dry_run:
        logger.log("试跑模式：以上结果未写入主库")
    else:
        notes = f"dry_run={args.dry_run}"
        if denied_channels:
            notes += f"；访问受限渠道：{','.join(denied_channels)}"
        store.register_batch(batch_id, ran_channels, stats, collector, notes)
        logger.log(
            f"批次 {batch_id} 入库：新增 {stats['new']}，重复 {stats['duplicate']}，"
            f"更新 {stats['updated']}，失败 {stats['failed']}；主库累计 {store.count()} 条")

    _do_export(store, cfg, data_root, args)
    logger.log(f"日志已存：{log_file}")


def _do_export(store, cfg, data_root, args):
    fmt = args.format or cfg.get("export", {}).get("format", "xlsx")
    stamp = datetime.now().strftime("%Y%m%d")
    default_name = f"企业端岗位汇总_{stamp}.{'xlsx' if fmt == 'xlsx' else 'csv'}"
    out = args.out or os.path.join(data_root, default_name)
    if fmt == "xlsx":
        n, p = export_excel(store, out, cfg.get("filters"))
    else:
        n, p = export_csv(store, out, cfg.get("filters"))
    print(f"导出：{p}（{n} 条）")


def main():
    ap = argparse.ArgumentParser(
        description="招聘信息采集与汇总（合规优先，多渠道，增量）")
    ap.add_argument("--config", default="config.yaml", help="配置文件路径")
    ap.add_argument("--channels", default=None,
                    help="只跑指定渠道，逗号分隔（如 sndhr）")
    ap.add_argument("--dry-run", action="store_true",
                    help="试跑：不写主库、不记批次")
    ap.add_argument("--export-only", action="store_true",
                    help="不采集，只导出当前主库")
    ap.add_argument("--out", default=None, help="导出文件路径")
    ap.add_argument("--format", choices=["xlsx", "csv"], default=None,
                    help="导出格式（默认读配置）")
    ap.add_argument("--quiet", action="store_true", help="只写日志不刷屏")
    args = ap.parse_args()
    run(args)


if __name__ == "__main__":
    main()
