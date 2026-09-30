#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
苏高新人才网（www.sndhr.com）公开招聘信息采集脚本
=================================================
课题：苏州数智产业岗位技能需求与高校人才培养匹配调研（苏链智绘队）

用途：从苏高新人才网（苏州高新区官方人才网）的公开接口采集岗位列表与岗位详情，
      输出为 Excel，直接对接团队的企业端底表。

合规说明（务必阅读）：
  - 本脚本只访问该网站对未登录访客公开的接口，不做登录/验证码/反爬绕过；
  - 每次请求间隔默认 2 秒（--delay 可调，不建议低于 1 秒）；
  - 全站当前在挂岗位约 255 条，全量采集约需 6 分钟，对服务器压力极小；
  - 采集结果仅用于本课题研究，不传播岗位中的联系方式，不针对单个企业做评价。

使用方法（在团队共享文件夹内运行）：
  # 1) 先试跑 5 条，确认输出正常
  python sndhr_collect.py --limit 5

  # 2) 全量采集（列表 + 详情），输出到指定文件
  python sndhr_collect.py --out 企业端岗位样本_苏高新人才网_20261005_v01.xlsx

  # 3) 只采列表、不采详情（更快，但拿不到岗位描述原文）
  python sndhr_collect.py --no-detail

依赖：openpyxl（团队已配好）

作者备注：接口地址与参数由前端代码公开可见的调用还原，非破解；
         若网站改版导致失效，报错信息会明确提示，请勿反复重试。
"""

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime

try:
    from openpyxl import Workbook
    from openpyxl.styles import Font, Alignment
except ImportError:
    print("缺少 openpyxl，请先安装：pip install openpyxl")
    sys.exit(1)

# ---------------------------------------------------------------- 基础配置
BASE = "https://apiweb.sndhr.com/web-api"
SITE = "www.sndhr.com"

HEADERS = {
    "Content-Type": "application/json",
    # 前端对所有请求都会带上 token 头（未登录时为空），缺少该头会被网关拒绝（403）
    "token": "null",
    "Origin": "https://www.sndhr.com",
    "Referer": "https://www.sndhr.com/",
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"),
}

# 码值 → 中文（来自站点字典，未知码值原样保留便于人工核对）
EDU_MAP = {
    "elementarySchool": "小学", "juniorHighSchool": "初中", "seniorHighSchool": "高中",
    "technicalSchool": "技校", "vocationalHighSchool": "职高", "associates": "大专",
    "bachelor": "本科", "master": "硕士", "doctorate": "博士", "unLimit": "不限",
}
EXP_MAP = {
    "zero": "不限经验", "oneToThree": "1-3年", "threeToFive": "3-5年",
    "fiveToTen": "5-10年", "tenUp": "10年以上", "unLimit": "不限",
}
NATURE_MAP = {
    "stateEnterprise": "国有企业", "privateEnterprise": "民营企业",
    "cooperativelimitedEnterprise": "股份制企业", "limitedEnterprise": "有限责任公司",
    "listedEnterprise": "上市公司", "foreignHongKong": "港澳台资", "jointVentureHongKong": "港澳台合资",
    "foreignJapan": "日资", "foreignAmerican": "美资", "foreign": "外资", "other": "其他",
}
SIZE_MAP = {
    "less49": "50人以下", "less99": "100人以下", "less199": "200人以下",
    "less499": "500人以下", "less999": "1000人以下", "less1999": "2000人以下",
    "less4999": "5000人以下", "more5000": "5000人以上",
}
WORKNATURE_MAP = {"fullTime": "全职", "partTime": "兼职", "practice": "实习", "intern": "实习"}

# 行政区划码（苏州）→ 中文
AREA_MAP = {
    "320505": "虎丘区（高新区）", "320506": "吴中区", "320507": "相城区",
    "320508": "姑苏区", "320509": "吴江区", "320581": "常熟市",
    "320582": "张家港市", "320583": "昆山市", "320585": "太仓市",
}


def log(msg):
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


def api(path, payload=None, params=None, retry=3):
    """调用接口；payload 非空走 POST，否则走 GET。带退避重试。"""
    url = BASE + path
    if params:
        url += "?" + "&".join(f"{k}={v}" for k, v in params.items())
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    method = "POST" if payload is not None else "GET"
    last_err = None
    for attempt in range(1, retry + 1):
        try:
            req = urllib.request.Request(url, data=data, headers=HEADERS, method=method)
            with urllib.request.urlopen(req, timeout=30) as resp:
                body = json.loads(resp.read().decode("utf-8"))
            if body.get("code") not in (0, None):
                raise RuntimeError(f"接口返回异常：{body.get('msg')}")
            return body.get("data")
        except (urllib.error.URLError, TimeoutError, RuntimeError, json.JSONDecodeError) as e:
            last_err = e
            if attempt < retry:
                wait = 3 * attempt
                log(f"  请求失败（{e}），{wait} 秒后第 {attempt + 1} 次重试…")
                time.sleep(wait)
    raise RuntimeError(f"接口请求最终失败：{last_err}")


def fetch_list(page, size):
    return api("/find-job/page-advanced", {"page": page, "pageSize": size}) or []


def fetch_detail(job_id):
    d = api("/find-job/job-detail", params={"id": job_id, "isPersonal": "true"}) or {}
    return d.get("job") or {}


def code2text(mapping, code):
    if code is None:
        return ""
    return mapping.get(str(code), f"{code}（未映射）")


def collect(limit=None, with_detail=True, delay=2.0, page_size=50):
    log("开始拉取岗位列表…")
    rows, page = [], 1
    while True:
        batch = fetch_list(page, page_size)
        if not batch:
            break
        rows.extend(batch)
        log(f"  列表第 {page} 页：{len(batch)} 条（累计 {len(rows)}）")
        if limit and len(rows) >= limit:
            rows = rows[:limit]
            break
        page += 1
        time.sleep(delay)

    # 列表内按 id 去重（同一岗位跨页重复时保留首次出现）
    seen, uniq = set(), []
    for r in rows:
        if r["id"] not in seen:
            seen.add(r["id"])
            uniq.append(r)
    log(f"列表完成：{len(uniq)} 条（去重后）")

    if with_detail:
        log(f"开始拉取岗位详情（共 {len(uniq)} 条，每条间隔 {delay} 秒）…")
        for i, r in enumerate(uniq, 1):
            try:
                detail = fetch_detail(r["id"])
                r["description"] = (detail.get("description") or "").strip()
                r["endDate"] = detail.get("endDate")
                r["publisher"] = detail.get("publisher")
            except Exception as e:
                r["description"] = ""
                log(f"  [{i}/{len(uniq)}] 详情失败 {r['name']}：{e}")
            else:
                if i % 10 == 0 or i == len(uniq):
                    log(f"  详情进度 {i}/{len(uniq)}")
            time.sleep(delay)
    return uniq


def write_excel(rows, path, with_detail):
    wb = Workbook()
    ws = wb.active
    ws.title = "岗位原始表"
    header = ["岗位编号", "来源平台", "来源网址", "接口标识(id)", "采集日期", "采集人",
              "岗位名称", "企业名称", "工作地点", "学历要求", "经验要求", "薪资",
              "招聘人数", "工作性质", "企业性质", "企业规模", "福利标签",
              "发布日期", "刷新日期", "岗位描述原文", "描述字数"]
    ws.append(header)
    for c in ws[1]:
        c.font = Font(bold=True)
        c.alignment = Alignment(horizontal="center")

    today = datetime.now().strftime("%Y-%m-%d")
    for i, r in enumerate(rows, 1):
        desc = r.get("description") or ""
        ws.append([
            f"GQ{i:04d}",
            "苏高新人才网",
            f"https://{SITE}/findJob/positionDetail?id={r['id']}",
            r["id"],
            today,
            "",  # 采集人：请团队成员自行填写
            r.get("name", ""),
            r.get("enterpriseName", ""),
            f"{code2text(AREA_MAP, r.get('area'))} {r.get('address') or ''}".strip(),
            code2text(EDU_MAP, r.get("educationDegree")),
            code2text(EXP_MAP, r.get("workExperience")),
            f"{r.get('salaryFrom') or ''}-{r.get('salaryTo') or ''}".strip("-"),
            r.get("headCount"),
            code2text(WORKNATURE_MAP, r.get("workNature")),
            code2text(NATURE_MAP, r.get("nature")),
            code2text(SIZE_MAP, r.get("enterpriseSize")),
            "、".join([x for x in (r.get("targetList") or []) if x]),
            r.get("startDate", ""),
            r.get("refreshDate", ""),
            desc,
            len(desc),
        ])

    ws.freeze_panes = "A2"
    ws.column_dimensions["T"].width = 80

    # 第二张表：原始码值留档，便于复核与追溯
    ws2 = wb.create_sheet("原始字段留档")
    keys = ["id", "code", "name", "enterpriseName", "enterpriseId", "educationDegree",
            "workExperience", "salaryFrom", "salaryTo", "area", "address", "industry",
            "nature", "enterpriseSize", "workNature", "headCount", "startDate", "refreshDate"]
    ws2.append(keys)
    for r in rows:
        ws2.append([r.get(k) for k in keys])

    wb.save(path)
    log(f"已输出：{os.path.abspath(path)}（{len(rows)} 条）")


def main():
    ap = argparse.ArgumentParser(description="苏高新人才网公开招聘信息采集")
    ap.add_argument("--out", default=None, help="输出 Excel 文件名")
    ap.add_argument("--limit", type=int, default=None, help="只采前 N 条（试跑用）")
    ap.add_argument("--delay", type=float, default=2.0, help="每次请求间隔秒数（默认 2）")
    ap.add_argument("--page-size", type=int, default=50, help="每页条数（默认 50）")
    ap.add_argument("--no-detail", action="store_true", help="不采集岗位详情（无描述原文）")
    args = ap.parse_args()

    out = args.out or f"企业端岗位样本_苏高新人才网_{datetime.now():%Y%m%d_%H%M}.xlsx"
    rows = collect(limit=args.limit, with_detail=not args.no_detail,
                   delay=args.delay, page_size=args.page_size)
    write_excel(rows, out, with_detail=not args.no_detail)

    if not args.no_detail:
        ok = sum(1 for r in rows if len(r.get("description") or "") > 20)
        log(f"描述有效性：{ok}/{len(rows)} 条有实质描述（>20字），"
            f"其余为极简岗位，后续分析时需标记")


if __name__ == "__main__":
    main()
