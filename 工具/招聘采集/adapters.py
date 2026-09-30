# -*- coding: utf-8 -*-
"""渠道适配器：每个平台一个类，把平台原始数据映射成统一 JobRecord。

适配器契约：
  - fetch() 产出 JobRecord 生成器（已 build 标准化）；
  - 只访问公开、无需登录的端点；
  - 平台规则变化导致失败时抛明确异常，不做任何绕过尝试。

新增渠道时：在 ADAPTERS 里注册类，并在 config.yaml 加对应配置段。
"""

import csv
import io
from datetime import datetime

from .httpclient import HttpClient, default_headers
from .model import JobRecord


class BaseAdapter:
    """渠道基类。子类实现 fetch_records()。"""

    name = "base"

    def __init__(self, cfg, logger, batch_id, collector):
        self.cfg = cfg
        self.logger = logger
        self.batch_id = batch_id
        self.collector = collector
        self.client = HttpClient(
            delay=cfg.get("delay_seconds", 2.0),
            timeout=cfg.get("timeout_seconds", 30),
            retry=cfg.get("retry", 3),
            logger=logger,
        )

    def fetch(self):
        yield from ()

    def make_record(self, **kw):
        rec = JobRecord(
            source_platform=self.name,
            batch_id=self.batch_id,
            collector=self.collector,
            **kw,
        )
        return rec.build()


class SNDHRAdapter(BaseAdapter):
    """苏高新人才网（sndhr.com）——2026-09-29 已完成技术验证的渠道。

    端点均为未登录访客可见的公开接口。两个实现要点：
      1) 请求头需带 token 字段（未登录时前端固定传 null）；
      2) 分页字段名是 page，传其他名字会被静默忽略、永远返回第一页。
    """

    name = "sndhr"

    BASE = "https://apiweb.sndhr.com/web-api"

    EDU_MAP = {
        "elementarySchool": "大专及以下", "juniorHighSchool": "大专及以下",
        "seniorHighSchool": "大专及以下", "technicalSchool": "大专及以下",
        "vocationalHighSchool": "大专及以下", "associates": "大专",
        "bachelor": "本科", "master": "硕士", "doctorate": "博士", "unLimit": "不限",
    }
    EXP_MAP = {
        "zero": "不限", "oneToThree": "1-3年", "threeToFive": "3-5年",
        "fiveToTen": "5-10年", "tenUp": "10年以上", "unLimit": "不限",
    }
    NATURE_MAP = {
        "stateEnterprise": "国有企业", "privateEnterprise": "民营企业",
        "cooperativelimitedEnterprise": "股份制企业", "limitedEnterprise": "有限责任公司",
        "listedEnterprise": "上市公司", "foreignHongKong": "港澳台资",
        "jointVentureHongKong": "港澳台合资", "foreignJapan": "日资",
        "foreignAmerican": "美资", "other": "其他",
    }
    SIZE_MAP = {
        "less49": "50人以下", "less99": "100人以下", "less199": "200人以下",
        "less499": "500人以下", "less999": "1000人以下", "less1999": "2000人以下",
        "less4999": "5000人以下", "more5000": "5000人以上",
    }
    AREA_MAP = {
        "320505": "苏州高新区", "320506": "苏州吴中区", "320507": "苏州相城区",
        "320508": "苏州姑苏区", "320509": "苏州吴江区", "320581": "常熟市",
        "320582": "张家港市", "320583": "昆山市", "320585": "太仓市",
    }
    WORKNATURE_MAP = {"fullTime": "全职", "partTime": "兼职", "practice": "实习", "intern": "实习"}

    def _headers(self):
        return default_headers(
            origin="https://www.sndhr.com",
            referer="https://www.sndhr.com/",
            extra={"Content-Type": "application/json", "token": "null"},
        )

    def _fetch_list_page(self, page, size):
        data = self.client.post_json(
            self.BASE + "/find-job/page-advanced",
            payload={"page": page, "pageSize": size},
            headers=self._headers(),
        )
        if data.get("code") not in (0, None):
            raise RuntimeError(f"sndhr 列表接口返回异常：{data.get('msg')}")
        return data.get("data") or []

    def _fetch_detail(self, job_id):
        data = self.client.get_json(
            self.BASE + "/find-job/job-detail",
            headers=self._headers(),
            params={"id": job_id, "isPersonal": "true"},
        )
        if data.get("code") not in (0, None):
            raise RuntimeError(f"sndhr 详情接口返回异常：{data.get('msg')}")
        return (data.get("data") or {}).get("job") or {}

    def fetch(self):
        page_size = self.cfg.get("page_size", 50)
        limit = self.cfg.get("limit")
        with_detail = self.cfg.get("with_detail", True)

        rows, page = [], 1
        while True:
            batch = self._fetch_list_page(page, page_size)
            if not batch:
                break
            rows.extend(batch)
            self.logger.log(f"列表第 {page} 页：{len(batch)} 条（累计 {len(rows)}）")
            if limit and len(rows) >= limit:
                rows = rows[:limit]
                break
            page += 1

        # 列表内按 id 去重（分页参数错误会造成跨页重复，这里兜底并告警）
        seen, uniq = set(), []
        for r in rows:
            if r["id"] in seen:
                continue
            seen.add(r["id"])
            uniq.append(r)
        if len(uniq) != len(rows):
            self.logger.log(
                f"列表存在跨页重复：{len(rows)} -> {len(uniq)}，请核对分页参数",
                level="WARN")
        self.logger.log(f"列表完成：{len(uniq)} 条（去重后）")

        for i, r in enumerate(uniq, 1):
            desc = ""
            if with_detail:
                try:
                    detail = self._fetch_detail(r["id"])
                    desc = (detail.get("description") or "").strip()
                except Exception as e:
                    self.logger.log(
                        f"详情失败 {r.get('name')}（id={r['id']}）：{e}", level="WARN")
                if i % 20 == 0 or i == len(uniq):
                    self.logger.log(f"详情进度 {i}/{len(uniq)}")

            area = self.AREA_MAP.get(str(r.get("area", "")), str(r.get("area") or ""))
            yield self.make_record(
                source_id=str(r["id"]),
                source_url=f"https://www.sndhr.com/findJob/positionDetail?id={r['id']}",
                job_title=r.get("name", ""),
                company=r.get("enterpriseName", ""),
                location=f"{area} {r.get('address') or ''}".strip(),
                salary_raw=f"{r.get('salaryFrom') or ''}-{r.get('salaryTo') or ''}".strip("-"),
                education=self.EDU_MAP.get(r.get("educationDegree"), r.get("educationDegree") or ""),
                experience=self.EXP_MAP.get(r.get("workExperience"), r.get("workExperience") or ""),
                publish_date=(r.get("startDate") or "")[:10],
                job_description=desc,
                raw={
                    "id": r.get("id"), "code": r.get("code"), "name": r.get("name"),
                    "enterpriseId": r.get("enterpriseId"),
                    "educationDegree": r.get("educationDegree"),
                    "workExperience": r.get("workExperience"),
                    "salaryFrom": r.get("salaryFrom"), "salaryTo": r.get("salaryTo"),
                    "area": r.get("area"), "address": r.get("address"),
                    "industry": r.get("industry"), "nature": r.get("nature"),
                    "natureText": self.NATURE_MAP.get(r.get("nature"), ""),
                    "enterpriseSize": r.get("enterpriseSize"),
                    "enterpriseSizeText": self.SIZE_MAP.get(r.get("enterpriseSize"), ""),
                    "workNature": self.WORKNATURE_MAP.get(r.get("workNature"), ""),
                    "headCount": r.get("headCount"),
                    "startDate": r.get("startDate"), "refreshDate": r.get("refreshDate"),
                    "targetList": r.get("targetList") or [],
                },
            )


class GenericJSONAPIAdapter(BaseAdapter):
    """通用公开 JSON API 适配器（评估新渠道用，默认不启用）。

    适用于：无需登录、GET 返回 JSON、分页参数明确的公开端点。
    配置示例（config.yaml 的 channels 段）：

    demo_channel:
      adapter: generic_json
      enabled: false          # 评估确认合规后才能改 true
      list_url: "https://example.com/api/jobs"
      page_param: "page"
      size_param: "pageSize"
      page_size: 20
      max_pages: 5            # 评估期限制抓取规模
      records_path: "data.list"        # 记录数组在返回 JSON 中的路径
      field_map:              # 平台字段 -> 统一字段
        job_title: "positionName"
        company: "companyName"
        source_id: "id"
      url_template: "https://example.com/job/{source_id}"

    注意：评估期 max_pages 必须保守；正式启用前需完成渠道合规评估表。
    """

    name = "generic_json"

    def _dig(self, obj, path):
        cur = obj
        for part in path.split("."):
            if isinstance(cur, dict):
                cur = cur.get(part)
            else:
                return None
        return cur

    def fetch(self):
        url = self.cfg["list_url"]
        fm = self.cfg.get("field_map", {})
        max_pages = self.cfg.get("max_pages", 5)
        page_size = self.cfg.get("page_size", 20)
        headers = default_headers(
            origin=self.cfg.get("origin"),
            referer=self.cfg.get("referer"),
            extra=self.cfg.get("headers"),
        )

        for page in range(1, max_pages + 1):
            params = {
                self.cfg.get("page_param", "page"): page,
                self.cfg.get("size_param", "pageSize"): page_size,
            }
            data = self.client.get_json(url, headers=headers, params=params)
            records = self._dig(data, self.cfg.get("records_path", "data")) or []
            if not records:
                break
            for r in records:
                kw = {}
                for unified, platform_field in fm.items():
                    val = r.get(platform_field)
                    if val is not None:
                        kw[unified] = val
                if not kw.get("source_id"):
                    continue
                sid = str(kw["source_id"])
                url_tpl = self.cfg.get("url_template", "")
                kw.setdefault("source_url", url_tpl.format(source_id=sid) if url_tpl else url)
                kw.setdefault("job_description", "")
                kw.setdefault("raw", r)
                yield self.make_record(**kw)
            self.logger.log(f"通用适配器第 {page} 页：{len(records)} 条")


class CSVImportAdapter(BaseAdapter):
    """CSV/Excel 导入适配器：把人工收集或其他来源导出的表格并入统一库。

    用于：
      - 商业平台"人工抽样"的复制粘贴数据（唯一合规路径）；
      - CnOpenData 等授权数据切片的导入；
      - 历史批次 Excel 的重放。

    配置示例：
    manual_import:
      adapter: csv_import
      enabled: true
      files:
        - path: "data/人工抽样_20261005.csv"
          platform: "boss_manual"     # 记录里 source_platform 用这个
        - path: "data/cnopendata_slice.csv"
          platform: "cnopendata"
      field_map:
        job_title: "职位名称"
        company: "公司"
        ...
    """

    name = "csv_import"

    def fetch(self):
        for file_cfg in self.cfg.get("files", []):
            path = file_cfg["path"]
            platform = file_cfg.get("platform", "manual")
            fm = file_cfg.get("field_map", {})
            try:
                with open(path, "r", encoding="utf-8-sig") as f:
                    reader = csv.DictReader(f)
                    n = 0
                    for row in reader:
                        kw = {}
                        for unified, col in fm.items():
                            val = row.get(col)
                            if val not in (None, ""):
                                kw[unified] = val
                        if not kw.get("source_id"):
                            # 人工抽样无平台 id 时，用行内容指纹
                            import hashlib
                            fp = hashlib.md5(
                                f"{kw.get('company','')}|{kw.get('job_title','')}".encode("utf-8")
                            ).hexdigest()[:12]
                            kw["source_id"] = fp
                        kw.setdefault("job_description", "")
                        kw.setdefault("raw", dict(row))
                        rec = self.make_record(**kw)
                        rec.source_platform = platform
                        yield rec
                        n += 1
                    self.logger.log(f"导入 {path}：{n} 条")
            except FileNotFoundError:
                self.logger.log(f"导入文件不存在：{path}", level="WARN")


ADAPTERS = {
    "sndhr": SNDHRAdapter,
    "generic_json": GenericJSONAPIAdapter,
    "csv_import": CSVImportAdapter,
}


def build_adapter(channel_name, cfg, logger, batch_id, collector):
    adapter_kind = cfg.get("adapter")
    cls = ADAPTERS.get(adapter_kind)
    if not cls:
        raise ValueError(
            f"渠道 {channel_name} 配置了未知适配器 {adapter_kind}，"
            f"可选：{list(ADAPTERS)}")
    adapter = cls(cfg, logger, batch_id, collector)
    adapter.name = cfg.get("platform_name", channel_name)
    return adapter
