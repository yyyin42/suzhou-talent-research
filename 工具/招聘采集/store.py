# -*- coding: utf-8 -*-
"""存储层：基于 CSV 的轻量状态库。

设计取舍：不用数据库。CSV 可直接 git 版本化、Excel 打开人工核对、
任何成员零门槛维护——对本团队规模这是最稳的选择。

库文件（data/state/ 下）：
  master.csv     全量记录（唯一事实来源，只追加不删除）
  dedup_index.csv  uid 与 dedup_key 的索引（加速增量判断）
  batches.csv    批次登记（对应交接报告的批次登记表）
"""

import csv
import os
from datetime import datetime

from .model import FIELDS, make_uid

STATE_DIR = "state"
DATA_DIR = "data"


class Store:
    def __init__(self, root):
        """root：项目数据根目录（含 state/ 与导出）。"""
        self.root = root
        self.state_dir = os.path.join(root, STATE_DIR)
        self.master_path = os.path.join(self.state_dir, "master.csv")
        self.dedup_path = os.path.join(self.state_dir, "dedup_index.csv")
        self.batches_path = os.path.join(self.state_dir, "batches.csv")
        os.makedirs(self.state_dir, exist_ok=True)
        self._uid_index = {}       # uid -> row dict
        self._dedup_keys = {}      # dedup_key -> uid
        self._load()

    # ---------------------------------------------------------------- 读取
    def _load(self):
        if os.path.exists(self.master_path):
            with open(self.master_path, "r", encoding="utf-8-sig", newline="") as f:
                for row in csv.DictReader(f):
                    uid = row.get("uid")
                    if uid:
                        self._uid_index[uid] = row
                        dk = row.get("dedup_key")
                        if dk:
                            self._dedup_keys.setdefault(dk, uid)

    def has_uid(self, uid):
        return uid in self._uid_index

    def find_by_dedup_key(self, dedup_key):
        uid = self._dedup_keys.get(dedup_key)
        return self._uid_index.get(uid) if uid else None

    def all_rows(self):
        return list(self._uid_index.values())

    def count(self):
        return len(self._uid_index)

    # ---------------------------------------------------------------- 写入
    def upsert(self, record):
        """增量写入一条记录。

        返回 status：
          new       —— 首次出现，直接入库；
          duplicate —— uid 或内容去重键已存在，跳过（但记录来源平台）；
          updated   —— 同 uid 但发布/刷新信息变化，更新字段并追加新批次标记。
        """
        row = record.to_row()
        uid = row["uid"]

        if uid in self._uid_index:
            old = self._uid_index[uid]
            changed = (old.get("job_description") != row["job_description"]
                       or old.get("salary_raw") != row["salary_raw"])
            if changed:
                old.update({
                    "job_description": row["job_description"],
                    "description_len": row["description_len"],
                    "quality_flag": row["quality_flag"],
                    "salary_raw": row["salary_raw"],
                    "publish_date": row["publish_date"] or old.get("publish_date", ""),
                    "status": "updated",
                    "batch_id": row["batch_id"],
                })
                self._rewrite_master()
                return "updated"
            return "duplicate"

        # 跨渠道内容重复：同企业+岗位+地点但不同 uid
        if row["dedup_key"] and row["dedup_key"] in self._dedup_keys:
            return "duplicate"

        self._append_master(row)
        self._uid_index[uid] = row
        if row["dedup_key"]:
            self._dedup_keys.setdefault(row["dedup_key"], uid)
        return "new"

    def _append_master(self, row):
        is_new_file = not os.path.exists(self.master_path)
        with open(self.master_path, "a", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=FIELDS)
            if is_new_file:
                w.writeheader()
            w.writerow({k: row.get(k, "") for k in FIELDS})

    def _rewrite_master(self):
        tmp = self.master_path + ".tmp"
        with open(tmp, "w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=FIELDS)
            w.writeheader()
            for row in self.all_rows():
                w.writerow({k: row.get(k, "") for k in FIELDS})
        os.replace(tmp, self.master_path)

    # ---------------------------------------------------------------- 批次
    def register_batch(self, batch_id, channels, stats, collector, notes=""):
        is_new_file = not os.path.exists(self.batches_path)
        header = ["batch_id", "datetime", "channels", "collector",
                  "new", "duplicate", "updated", "failed", "total_in_store", "notes"]
        with open(self.batches_path, "a", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=header)
            if is_new_file:
                w.writeheader()
            w.writerow({
                "batch_id": batch_id,
                "datetime": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "channels": ";".join(channels),
                "collector": collector,
                "new": stats.get("new", 0),
                "duplicate": stats.get("duplicate", 0),
                "updated": stats.get("updated", 0),
                "failed": stats.get("failed", 0),
                "total_in_store": self.count(),
                "notes": notes,
            })

    def next_batch_id(self):
        """生成批次号：B20260930-01 形式，同日自增。"""
        today = datetime.now().strftime("%Y%m%d")
        n = 1
        if os.path.exists(self.batches_path):
            with open(self.batches_path, "r", encoding="utf-8-sig", newline="") as f:
                for row in csv.DictReader(f):
                    if row.get("batch_id", "").startswith(f"B{today}-"):
                        n += 1
        return f"B{today}-{n:02d}"

    # ---------------------------------------------------------------- 统计
    def stats(self):
        rows = self.all_rows()
        by_flag = {"valid": 0, "reference": 0, "minimal": 0}
        by_family = {}
        by_platform = {}
        for r in rows:
            by_flag[r.get("quality_flag") or "minimal"] = by_flag.get(r.get("quality_flag") or "minimal", 0) + 1
            fam = r.get("job_family_guess") or "未匹配"
            by_family[fam] = by_family.get(fam, 0) + 1
            plat = r.get("source_platform") or "?"
            by_platform[plat] = by_platform.get(plat, 0) + 1
        return {
            "total": len(rows),
            "by_quality": by_flag,
            "by_family": by_family,
            "by_platform": by_platform,
        }
