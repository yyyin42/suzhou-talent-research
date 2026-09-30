# -*- coding: utf-8 -*-
"""统一字段模型与标准化工具。

所有渠道适配器最终都把记录转成这里的统一结构，后续清洗、去重、
质量标记、导出只依赖这一层，不感知任何平台细节。
"""

import re
from dataclasses import dataclass, field, asdict
from datetime import datetime

# 统一表头（导出 Excel/CSV 的列顺序，也是数据字典的权威定义）
FIELDS = [
    "uid",               # 稳定唯一键：source_platform + source_id
    "source_platform",   # 来源平台（配置里的渠道名）
    "source_id",         # 平台内稳定标识（接口 id / 页面唯一参数）
    "source_url",        # 原始链接（可回访复核）
    "batch_id",          # 采集批次号，如 B20260930-01
    "collected_at",      # 采集时间 ISO 格式
    "collector",         # 采集人（配置填写，导出前可覆盖）
    "job_title",         # 职位名称
    "company",           # 公司
    "location",          # 地点（标准化后）
    "district",          # 区县（标准化提取，可空）
    "salary_raw",        # 薪资原文
    "salary_min_k",      # 薪资下限（千元/月，标准化，可空）
    "salary_max_k",      # 薪资上限（千元/月，标准化，可空）
    "education",         # 学历要求（标准五档+不限，可空）
    "experience",        # 经验要求（标准分档，可空）
    "publish_date",      # 发布日期 YYYY-MM-DD（可空）
    "job_description",   # 职位描述原文（清洗后）
    "description_len",   # 描述字数
    "quality_flag",      # 质量标记：valid / reference / minimal
    "job_family_guess",  # 岗位族初筛（关键词匹配，仅作候选，非结论）
    "dedup_key",         # 内容去重键：企业名+岗位名+地点（规范化）
    "status",            # new / duplicate / updated / excluded
    "exclude_reason",    # 排除理由（status=excluded 时填）
]

EDU_LEVELS = ["大专及以下", "大专", "本科", "硕士", "博士", "不限"]
EXP_LEVELS = ["不限", "1-3年", "3-5年", "5-10年", "10年以上"]

# 全半角统一、去 HTML 标签
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"[ \t\u3000]+")


def clean_text(s):
    """文本清洗：去 HTML 标签与常见实体、统一全半角空白、去首尾空白。"""
    if not s:
        return ""
    s = _TAG_RE.sub(" ", str(s))
    # HTML 实体（先于全角替换，避免实体残留在文本里）
    for entity, ch in (("&nbsp;", " "), ("&amp;", "&"), ("&lt;", "<"),
                       ("&gt;", ">"), ("&quot;", '"'), ("&#39;", "'")):
        s = s.replace(entity, ch)
    s = (s.replace("\u00a0", " ").replace("\uff0c", "，")
          .replace("\uff08", "(").replace("\uff09", ")"))
    s = _WS_RE.sub(" ", s)
    return s.strip()


def norm_company(name):
    """企业名规范化，用于内容去重键：去常见后缀、统一大小写与空白。"""
    if not name:
        return ""
    s = clean_text(name).lower()
    for suf in ("股份有限公司", "有限责任公司", "有限公司", "公司"):
        if s.endswith(suf.lower()):
            s = s[: -len(suf)]
            break
    return _WS_RE.sub("", s)


def norm_title(title):
    t = clean_text(title).lower()
    return _WS_RE.sub("", t)


def norm_location(loc):
    return _WS_RE.sub("", clean_text(loc))


def make_dedup_key(company, title, location):
    return "|".join([norm_company(company), norm_title(title), norm_location(location)])


def make_uid(platform, source_id):
    return f"{platform}:{source_id}"


# 岗位族初筛词表（候选建议，不是最终分类；最终须人工复核）
FAMILY_KEYWORDS = {
    "人工智能": ["算法", "机器学习", "深度学习", "自然语言处理", "nlp", "计算机视觉",
                "ai工程师", "人工智能", "大模型", "aigc"],
    "数据分析": ["数据分析", "数据分析师", "商业分析", "bi", "数据挖掘", "统计"],
    "软件开发": ["软件", "开发", "程序员", "java", "python", "前端", "后端", "c++",
                "嵌入式", "测试", "运维", "全栈"],
    "智能制造": ["自动化", "机械", "电气", "工艺", "设备", "智能制造", "工业", "产线",
                "机器人", "plc", "cnc"],
    "数字营销": ["市场", "营销", "推广", "新媒体", "电商", "运营专员（营销）", "品牌",
                "投放", "seo", "sem"],
    "产品运营": ["产品经理", "运营", "用户", "社区", "内容", "活动策划", "项目助理"],
}

# 江苏省苏州市行政区划 → 区县
SUZHOU_DISTRICT_RE = re.compile(
    r"(虎丘区|吴中区|相城区|姑苏区|吴江区|常熟市|张家港市|昆山市|太仓市|高新区|工业园区|姑苏)"
)

EDU_CODE_MAP = {
    "elementarySchool": "大专及以下", "juniorHighSchool": "大专及以下",
    "seniorHighSchool": "大专及以下", "technicalSchool": "大专及以下",
    "vocationalHighSchool": "大专及以下", "associates": "大专",
    "bachelor": "本科", "master": "硕士", "doctorate": "博士",
    "unLimit": "不限",
}

EXP_CODE_MAP = {
    "zero": "不限", "unLimit": "不限", "oneToThree": "1-3年",
    "threeToFive": "3-5年", "fiveToTen": "5-10年", "tenUp": "10年以上",
}


@dataclass
class JobRecord:
    source_platform: str = ""
    source_id: str = ""
    source_url: str = ""
    job_title: str = ""
    company: str = ""
    location: str = ""
    salary_raw: str = ""
    education: str = ""
    experience: str = ""
    publish_date: str = ""
    job_description: str = ""
    raw: dict = field(default_factory=dict)  # 原始字段整包留档
    batch_id: str = ""
    collected_at: str = field(default_factory=lambda: datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    collector: str = ""
    # 派生字段（标准化阶段填充）
    district: str = ""
    salary_min_k: object = None
    salary_max_k: object = None
    description_len: int = 0
    quality_flag: str = ""
    job_family_guess: str = ""
    dedup_key: str = ""
    status: str = "new"
    exclude_reason: str = ""

    def build(self):
        """清洗 + 标准化 + 活生字段填充，返回自身。"""
        self.job_title = clean_text(self.job_title)
        self.company = clean_text(self.company)
        self.location = clean_text(self.location)
        self.job_description = clean_text(self.job_description)
        self.description_len = len(self.job_description)
        self.district = self._extract_district()
        self.salary_min_k, self.salary_max_k = parse_salary(self.salary_raw)
        self.quality_flag = quality_of(self.description_len)
        self.job_family_guess = guess_family(self.job_title)
        self.dedup_key = make_dedup_key(self.company, self.job_title, self.location)
        self.education = normalize_edu(self.education)
        self.experience = normalize_exp(self.experience)
        return self

    def _extract_district(self):
        m = SUZHOU_DISTRICT_RE.search(self.location or "")
        return m.group(1) if m else ""

    def to_row(self):
        d = asdict(self)
        d.pop("raw", None)
        d["uid"] = make_uid(self.source_platform, self.source_id)
        return {k: d.get(k, "") for k in FIELDS}

    def to_raw_row(self):
        return {
            "uid": make_uid(self.source_platform, self.source_id),
            "source_platform": self.source_platform,
            "source_id": self.source_id,
            "raw_json": json_dumps_safe(self.raw),
        }


def json_dumps_safe(obj):
    import json
    def default(o):
        return str(o)
    return json.dumps(obj, ensure_ascii=False, default=default)


# 薪资区间：两个数字各带可选单位（5k-7k / 5000-7000 / 1.5-3万）
_SAL_RANGE_RE = re.compile(
    r"(\d+(?:\.\d+)?)\s*(k|K|千|万)?\s*[-~至到]\s*(\d+(?:\.\d+)?)\s*(k|K|千|万)?")
# 单值：8K / 1.2万
_SAL_SINGLE_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(k|K|千|万)")


def _to_k(v, unit):
    """把数值+单位换算成千元/月。无单位且值>=1000 视为元。"""
    if unit == "万":
        return v * 10
    if unit in ("k", "K", "千"):
        return v
    # 无单位：>=1000 按元，否则按千（招聘文本惯例）
    return v / 1000 if v >= 1000 else v


def parse_salary(raw):
    """把 '5k-7k' / '5000-7000' / '1.5-3万' 解析成 (下限, 上限)，单位千元/月。

    解析失败返回 (None, None)，不猜。
    """
    if not raw:
        return None, None
    s = str(raw).strip().lower().replace("元/月", "").replace("月薪", "")
    m = _SAL_RANGE_RE.search(s)
    if m:
        u1, u2 = m.group(2), m.group(4)
        # 单位只写一次时（如 1.5-3万 / 5-7k），作用于整个区间
        if u1 is None and u2:
            u1 = u2
        elif u2 is None and u1:
            u2 = u1
        lo = _to_k(float(m.group(1)), u1)
        hi = _to_k(float(m.group(3)), u2)
        return round(lo, 1), round(hi, 1)
    m = _SAL_SINGLE_RE.search(s)
    if m:
        v = round(_to_k(float(m.group(1)), m.group(2)), 1)
        return v, v
    return None, None


def quality_of(n):
    if n >= 50:
        return "valid"
    if n >= 20:
        return "reference"
    return "minimal"


def guess_family(title):
    """按关键词返回首个命中的岗位族候选；无命中返回空串。"""
    t = (title or "").lower()
    for fam, words in FAMILY_KEYWORDS.items():
        for w in words:
            if w in t:
                return fam
    return ""


def normalize_edu(edu):
    if not edu:
        return ""
    e = str(edu).strip()
    if e in EDU_CODE_MAP:
        return EDU_CODE_MAP[e]
    for lv in ("博士", "硕士", "本科", "大专"):
        if lv in e:
            return lv
    if "不限" in e or "无" in e:
        return "不限"
    return e  # 未知值原样保留，人工核对


def normalize_exp(exp):
    if not exp:
        return ""
    e = str(exp).strip()
    if e in EXP_CODE_MAP:
        return EXP_CODE_MAP[e]
    for lv in EXP_LEVELS:
        if lv != "不限" and lv in e:
            return lv
    if "不限" in e or "应届" in e or "无" in e:
        return "不限"
    return e
