# -*- coding: utf-8 -*-
"""离线自测：不访问任何网站，验证标准化、去重、增量、导出全链路。

运行：python -m 招聘采集.selftest
全部通过输出 PASS；任何一步失败抛 AssertionError。
"""

import os
import shutil
import tempfile

from .model import (JobRecord, parse_salary, quality_of, guess_family,
                    clean_text, make_dedup_key)
from .store import Store


def test_clean_text():
    assert clean_text("<p>Java&nbsp;工程师</p>") == "Java 工程师"
    assert clean_text("  数据分析师（全职） ") == "数据分析师(全职)"
    assert clean_text("") == ""
    assert clean_text(None) == ""


def test_salary():
    assert parse_salary("5k-7k") == (5.0, 7.0)
    assert parse_salary("5000-7000") == (5.0, 7.0)
    assert parse_salary("1.5-3万") == (15.0, 30.0)
    assert parse_salary("面议") == (None, None)
    assert parse_salary("") == (None, None)
    assert parse_salary("8K") == (8.0, 8.0)


def test_quality():
    assert quality_of(100) == "valid"
    assert quality_of(30) == "reference"
    assert quality_of(3) == "minimal"


def test_family():
    assert guess_family("算法工程师") == "人工智能"
    assert guess_family("数据分析师") == "数据分析"
    assert guess_family("行政专员") == ""


def test_dedup_key():
    k1 = make_dedup_key("苏州茂立光电科技有限公司", "Java 工程师", "苏州高新区")
    k2 = make_dedup_key("苏州茂立光电科技", "java工程师", "苏州 高新区")
    assert k1 == k2, f"同企业同岗位应判重：{k1} vs {k2}"


def test_store_incremental():
    tmp = tempfile.mkdtemp(prefix="recruit_selftest_")
    try:
        store = Store(tmp)

        r1 = JobRecord(
            source_platform="sndhr", source_id="111",
            source_url="https://example.com/111",
            job_title="Python 开发工程师", company="苏州某某科技有限公司",
            location="苏州高新区 嵩山路468号", salary_raw="8k-12k",
            education="本科", experience="1-3年", publish_date="2026-09-20",
            job_description="负责后端开发，" * 10,
        ).build()
        assert store.upsert(r1) == "new"

        # 完全相同的记录再次入库 -> duplicate
        assert store.upsert(r1) == "duplicate"

        # 同 uid 但描述变化 -> updated
        r1_changed = JobRecord(
            source_platform="sndhr", source_id="111",
            source_url="https://example.com/111",
            job_title="Python 开发工程师", company="苏州某某科技有限公司",
            location="苏州高新区 嵩山路468号", salary_raw="8k-12k",
            education="本科", experience="1-3年", publish_date="2026-09-20",
            job_description="负责后端开发与运维，" * 10,
        ).build()
        assert store.upsert(r1_changed) == "updated"

        # 不同平台 id 但同企业+岗位+地点 -> duplicate（跨渠道内容去重）
        r2 = JobRecord(
            source_platform="manual", source_id="222",
            source_url="https://example.com/222",
            job_title="Python 开发工程师", company="苏州某某科技有限公司",
            location="苏州高新区 嵩山路468号",
            job_description="另一个平台抄录的同一岗位",
        ).build()
        assert store.upsert(r2) == "duplicate"

        # 新岗位 -> new
        r3 = JobRecord(
            source_platform="sndhr", source_id="333",
            job_title="数据分析师", company="苏州另一家有限公司",
            location="苏州虎丘区", salary_raw="6-9千",
        ).build()
        assert store.upsert(r3) == "new"

        assert store.count() == 2  # 111 和 333，222 被内容去重

        # 派生字段检查
        row = store.find_by_dedup_key(r1.dedup_key)
        assert row is not None
        assert row["salary_min_k"] == "8.0" or row["salary_min_k"] == 8.0
        assert row["district"] == "高新区"
        assert row["quality_flag"] == "valid"

        # 持久化往返：新开 Store 应读到同样数据
        store2 = Store(tmp)
        assert store2.count() == 2
        assert store2.has_uid("sndhr:111")

        # 批次登记
        store2.register_batch("B20260930-01", ["sndhr"],
                              {"new": 2, "duplicate": 2, "updated": 1, "failed": 0},
                              "测试")
        batches_file = os.path.join(tmp, "state", "batches.csv")
        assert os.path.exists(batches_file)
        assert store2.next_batch_id().startswith("B")

        # 统计
        s = store2.stats()
        assert s["total"] == 2
        assert "sndhr" in s["by_platform"]
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"  PASS {t.__name__}")
    print(f"\n全部 {len(tests)} 项自测通过。")


if __name__ == "__main__":
    main()
