# -*- coding: utf-8 -*-
"""
AI问数快路径演示：词典构建 → 文本归一化 → AC自动机扫描 → 冲突消解 → 槽位装配 → 时间归一化
词典 = 销售分析域（缩略版，真实系统来自指标字典/维度值表的打平视图）
"""
import ahocorasick
import hashlib
import json
import re
import unicodedata
from datetime import date, timedelta

TODAY = date(2026, 10, 17)   # 生产环境取当前日期；此处固定便于演示可复现

# ============================================================
# 1. 词典：每条词条 = (别名字符串, 槽位类型, 归一目标)
#    别名统一小写登记；payload 的"槽位类型"决定命中后流到哪个槽位
# ============================================================
LEXICON = [
    # ---- 指标别名（来自指标字典）----
    ("销售额",    "metric",    "sales_amount"),
    ("营收",      "metric",    "sales_amount"),
    ("gmv",       "metric",    "sales_amount"),
    ("卖了多少",  "metric",    "sales_amount"),
    ("利润率",    "metric",    "profit_rate"),
    ("利润占比",  "metric",    "profit_rate"),
    ("毛利率",    "metric",    "profit_rate"),
    ("订单数",    "metric",    "order_cnt"),
    ("订单量",    "metric",    "order_cnt"),
    ("销量",      "metric",    "sales_qty"),
    ("卖了多少件","metric",    "sales_qty"),
    ("客单价",    "metric",    "avg_order_value"),
    ("退款率",    "metric",    "refund_rate"),
    # ---- 维度词（分组语义 → dimensions）----
    ("各城市",    "dimension", "city"),
    ("每个城市",  "dimension", "city"),
    ("按城市",    "dimension", "city"),
    ("城市",      "dimension", "city"),
    ("大区",      "dimension", "region"),
    ("区域",      "dimension", "region"),
    ("渠道",      "dimension", "channel"),
    ("产品线",    "dimension", "product_line"),
    ("品牌",      "dimension", "brand"),
    ("门店",      "dimension", "store"),
    # ---- 维度值（单值限定 → filters）----
    ("北京市",    "dim_value", "city=110000"),
    ("北京",      "dim_value", "city=110000"),
    ("帝都",      "dim_value", "city=110000"),
    ("上海",      "dim_value", "city=310000"),
    ("魔都",      "dim_value", "city=310000"),
    ("深圳市",    "dim_value", "city=440000"),
    ("深圳",      "dim_value", "city=440000"),
    ("广州",      "dim_value", "city=440100"),
    ("线上",      "dim_value", "channel=online"),
    ("线下",      "dim_value", "channel=offline"),
    ("分销",      "dim_value", "channel=distribution"),
    ("华东",      "dim_value", "region=east"),
    ("华南",      "dim_value", "region=south"),
    ("华北",      "dim_value", "region=north"),
    # ---- 时间触发词（带参数语义，参数交给时间规则）----
    ("上季度",    "time_expr", "quarter:-1"),
    ("上个季度",  "time_expr", "quarter:-1"),
    ("本季度",    "time_expr", "quarter:0"),
    ("这个季度",  "time_expr", "quarter:0"),
    ("上个月",    "time_expr", "month:-1"),
    ("昨天",      "time_expr", "day:-1"),
    ("今天",      "time_expr", "day:0"),
    ("最近",      "time_expr", "lastn:?"),
    ("上周",      "time_expr", "week:-1"),
    ("本周",      "time_expr", "week:0"),
    ("今年以来",  "time_expr", "ytd:0"),
    ("今年",      "time_expr", "ytd:0"),
    ("去年",      "time_expr", "year:-1"),
    # ---- 排序方向（→ order_by，字段默认取主指标）----
    ("降序",      "order",     "desc"),
    ("从高到低",  "order",     "desc"),
    ("从大到小",  "order",     "desc"),
    ("倒序",      "order",     "desc"),
    ("升序",      "order",     "asc"),
    ("从低到高",  "order",     "asc"),
    ("从小到大",  "order",     "asc"),
    ("正序",      "order",     "asc"),
    # ---- limit 触发词（N 由正则提取：top N / 前N）----
    ("top",       "limit",     "topn:?"),
    # ---- 意图关键词（命中即覆盖默认 intent）----
    ("趋势",      "intent",    "trend"),
    ("环比",      "intent",    "trend"),
    ("同比",      "intent",    "trend"),
    ("排名",      "intent",    "rank"),
    ("最高",      "intent",    "rank"),
    ("最低",      "intent",    "rank"),
    ("对比",      "intent",    "compare"),
    ("明细",      "intent",    "detail"),
]

# ---- 别名运营的产物：来自抽取日志的未命中词挖掘，人工评审后入库 ----
# 每条都过两道评审：①确实是该概念的常见说法 ②无歧义冲突（如"流水"在
# 零售语境=营业额，且日志中验证无"流水线"类误命中 case 才放行）
NEW_ALIASES = [
    ("营业额",   "metric",    "sales_amount"),
    ("流水",     "metric",    "sales_amount"),
    ("净利率",   "metric",    "profit_rate"),     # 简化：并入 profit_rate 口径
    ("羊城",     "dim_value", "city=440100"),     # 广州
    ("鹏城",     "dim_value", "city=440000"),     # 深圳
    ("由高到低", "order",     "desc"),
    ("由低到高", "order",     "asc"),
]

# 注册表：口径唯一事实源（默认聚合 + 兼容矩阵都在这里声明，不从句子里猜）
REGISTRY = {
    "sales_amount":    {"default_agg": "sum",   "compatible_dims": "*"},
    "profit_rate":     {"default_agg": "avg",   # 比率型 → avg
                        # 假设业务口径：利润率仅支持空间/渠道轴下钻，不支持产品轴（品牌内成本分摊口径未定）
                        "compatible_dims": ["city", "region", "channel", "time"]},
    "order_cnt":       {"default_agg": "count", "compatible_dims": "*"},
    "sales_qty":       {"default_agg": "sum",   "compatible_dims": "*"},
    "avg_order_value": {"default_agg": "avg",
                        "compatible_dims": ["city", "region", "channel", "time", "product_line"]},
    "refund_rate":     {"default_agg": "avg",
                        "compatible_dims": ["channel", "region", "time"]},
}

# ============================================================
# 2. 文本归一化：NFKC(全角→半角) → 小写 → 标点→空格 → 空白压缩
# ============================================================
def normalize(text: str) -> str:
    text = unicodedata.normalize("NFKC", text)
    text = text.lower()
    text = re.sub(r"[，。？！、；：,.?!;:()\[\]（）]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text

# ============================================================
# 3. 构建 AC 自动机（词条统一小写注册；万级词条此步毫秒级）
# ============================================================
def build_automaton(extra=None):
    A = ahocorasick.Automaton()
    for term, slot, target in LEXICON + list(extra or []):
        A.add_word(term.lower(), (term.lower(), slot, target))
    A.make_automaton()
    return A

# ============================================================
# 4. 扫描 + 冲突消解（起点升序、同起点取最长、跳过重叠）
# ============================================================
def scan(A, text):
    raw = []
    for end_idx, (term, slot, target) in A.iter(text):
        start = end_idx - len(term) + 1
        raw.append({"s": start, "e": end_idx + 1, "term": term,
                    "slot": slot, "target": target})
    raw.sort(key=lambda h: (h["s"], -(h["e"] - h["s"])))
    chosen, pos = [], -1
    for h in raw:
        if h["s"] > pos:
            chosen.append(h)
            pos = h["e"] - 1
    chosen.sort(key=lambda h: h["s"])
    return raw, chosen

# ============================================================
# 5. 时间归一化：触发词绑定 + 正则兜底（绝对季度 / 最近N天）
# ============================================================
def quarter_bounds(year, q):
    start = date(year, 3 * (q - 1) + 1, 1)
    end = date(year + 1, 1, 1) if q == 4 else date(year, 3 * q + 1, 1)
    return start, end

def resolve_time(hit, text, today):
    if hit is None:
        # 兜底：绝对季度 "2026q3"
        if m := re.search(r"(\d{4})\s*q\s*([1-4])", text):
            s, e = quarter_bounds(int(m.group(1)), int(m.group(2)))
            return {"start": str(s), "end": str(e), "granularity": "quarter"}
        return None
    kind, _, param = hit["target"].partition(":")
    if kind == "quarter":
        cur_q = (today.month - 1) // 3 + 1
        y, q = divmod(today.year * 4 + (cur_q - 1) + int(param), 4)
        s, e = quarter_bounds(y, q + 1)
        return {"start": str(s), "end": str(e), "granularity": "quarter"}
    if kind == "day":
        d = today + timedelta(days=int(param))
        return {"start": str(d), "end": str(d + timedelta(days=1)), "granularity": "day"}
    if kind == "week":
        monday = today - timedelta(days=today.weekday()) + timedelta(weeks=int(param))
        return {"start": str(monday), "end": str(monday + timedelta(days=7)),
                "granularity": "week"}
    if kind == "ytd":
        return {"start": str(date(today.year, 1, 1)),
                "end": str(today + timedelta(days=1)), "granularity": "year"}
    if kind == "year":
        y = today.year + int(param)
        return {"start": str(date(y, 1, 1)), "end": str(date(y + 1, 1, 1)),
                "granularity": "year"}
    if kind == "lastn":
        m = re.search(r"最?近\s*(\d+)\s*天", text)
        n = int(m.group(1)) if m else 7
        return {"start": str(today - timedelta(days=n)),
                "end": str(today + timedelta(days=1)), "granularity": "day"}
    return None

# ============================================================
# 6. 槽位装配：按词条类型分流；agg 从注册表反填
# ============================================================
def assemble(chosen, text):
    slots = {"intent": "aggregate", "metrics": [], "dimensions": [],
             "filters": {}, "time": None, "order_by": None, "limit": None}
    time_hit, order_dir = None, None
    for h in chosen:
        if h["slot"] == "metric":
            if h["target"] not in [m["id"] for m in slots["metrics"]]:
                slots["metrics"].append(
                    {"id": h["target"], "agg": REGISTRY[h["target"]]["default_agg"]})
        elif h["slot"] == "dimension":
            if h["target"] not in slots["dimensions"]:
                slots["dimensions"].append(h["target"])
        elif h["slot"] == "dim_value":
            dim, _, val = h["target"].partition("=")
            slots["filters"][dim] = val
        elif h["slot"] == "time_expr":
            time_hit = h
        elif h["slot"] == "intent":
            slots["intent"] = h["target"]
        elif h["slot"] == "order":
            order_dir = h["target"]
        elif h["slot"] == "limit":
            pass   # top 触发词，N 统一由下方正则提取
    # 排序：显式方向词优先；rank 意图（最高/最低/排名）无方向词时默认降序
    if order_dir is None and slots["intent"] == "rank":
        order_dir = "desc"
    if order_dir and slots["metrics"]:
        slots["order_by"] = {"field": slots["metrics"][0]["id"], "dir": order_dir}
    # limit 兜底正则：top N / 前N（"前"字不进词典，避免误命中"之前"）
    m = re.search(r"top\s*(\d+)|前\s*(\d+)", text)
    if m:
        slots["limit"] = int(m.group(1) or m.group(2))
    slots["time"] = resolve_time(time_hit, text, TODAY)
    return slots

# ============================================================
# 7. 校验层：兼容矩阵（metric × dimension）+ 时间合法性
#    可自动修复的静默修复，不可修复的降级为澄清话术——绝不带病出结果
# ============================================================
def validate(slots):
    problems = []
    for m in slots["metrics"]:
        meta = REGISTRY[m["id"]]
        for d in slots["dimensions"]:
            if meta["compatible_dims"] != "*" and d not in meta["compatible_dims"]:
                problems.append({
                    "type": "INCOMPATIBLE_DIM",
                    "detail": f'{m["id"]} 不支持按 {d} 下钻',
                    "clarify": (f'「{m["id"]}」不支持按「{d}」查看，'
                                f'可按 {" / ".join(meta["compatible_dims"])} 看，要换一个吗？'),
                })
    t = slots.get("time")
    if t and t["start"] >= t["end"]:
        problems.append({"type": "BAD_TIME", "detail": "时间区间 start >= end",
                         "clarify": "时间范围似乎有误，能换个说法吗？"})
    return problems

# ============================================================
# 8. 指纹生成：canonical 序列化 → SHA256
#    规则：集合字段排序（消除顺序差异）、key 排序、紧凑输出，
#    时间只放归一化后的绝对区间（raw_expr 绝不进指纹——
#    "上季度"和"2026Q3"归一化后区间相同，必须是同一个指纹）
# ============================================================
DATASET = "sales_dw"

def canonicalize(slots):
    obj = {
        "dataset": DATASET,
        "intent": slots["intent"],
        "metrics": sorted(slots["metrics"], key=lambda m: (m["id"], m["agg"])),
        "dimensions": sorted(slots["dimensions"]),
        "filters": {k: v for k, v in sorted(slots["filters"].items())},
        "time": slots["time"],
        "order_by": slots["order_by"],
        "limit": slots["limit"],
    }
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)

def fingerprint(slots):
    return hashlib.sha256(canonicalize(slots).encode("utf-8")).hexdigest()[:16]

# ============================================================
# 演示
# ============================================================
def pretty_range(h):
    return f'[{h["s"]},{h["e"]})'

def run(A, query, show_raw=True):
    print("=" * 78)
    print(f"输入: {query!r}")
    norm = normalize(query)
    print(f"归一化: {norm!r}")
    raw, chosen = scan(A, norm)
    if show_raw:
        print(f"-- 原始命中（{len(raw)} 个，含 fail 链顺带命中的嵌套词条）--")
        for h in raw:
            keep = h in chosen
            mark = "✓ 保留" if keep else "✗ 淘汰(嵌套/重叠)"
            print(f'  {pretty_range(h):<10} {h["term"]:<6} {h["slot"]:<10} '
                  f'{h["target"]:<22} {mark}')
    print(f"-- 冲突消解后（{len(chosen)} 个）--")
    for h in chosen:
        print(f'  {pretty_range(h):<10} {h["term"]:<6} {h["slot"]:<10} {h["target"]}')
    print("-- 装配槽位 --")
    slots = assemble(chosen, norm)
    print(json.dumps(slots, ensure_ascii=False, indent=2))
    print("-- 校验层 --")
    problems = validate(slots)
    if not problems:
        print("  ✓ 通过（agg/兼容矩阵/时间均合法）")
        print("-- 指纹 --")
        print(f"  canonical: {canonicalize(slots)}")
        print(f"  hash: {fingerprint(slots)}")
    else:
        for p in problems:
            print(f'  ✗ {p["type"]}: {p["detail"]}')
            print(f'    → 澄清话术: {p["clarify"]}')
        print("  (校验未通过，不生成指纹)")
    print()

if __name__ == "__main__":
    A = build_automaton(NEW_ALIASES)   # 当前线上状态：基础词典 + 已入库的新别名
    print(f"AC 自动机构建完成：{len(LEXICON)} 基础词条 + {len(NEW_ALIASES)} 新别名"
          f" = {len(LEXICON) + len(NEW_ALIASES)} 条\n")

    # 例1：主例句（展示 fail 链顺带命中："城市"嵌在"各城市"里被同时报告）
    run(A, "上季度各城市利润率")

    # 例2：脏输入（全角/大写/标点）—— 归一化前后对比
    print("=" * 78)
    print("【归一化价值对比】输入: '２０２６Ｑ３帝都的GMV，只看线上'")
    dirty = "２０２６Ｑ３帝都的GMV，只看线上"
    raw_before, _ = scan(A, dirty)
    print(f"  不归一化直接扫：命中 {len(raw_before)} 个 → "
          f"{[h['term'] for h in raw_before] or '几乎全miss'}")
    run(A, dirty, show_raw=False)

    # 例3：时间参数 + 维度值筛选 + 口语化指标
    run(A, "最近7天北京卖了多少", show_raw=False)

    # 例4：显式排序 + limit（从高到低取前5）
    run(A, "上季度各城市销售额，从高到低取前5")

    # 例5：排序意图词（"最高"→rank，无显式方向词时默认降序）+ topN
    run(A, "今年上海销售额最高的top3渠道", show_raw=False)

    # 例6：升序 + 新指标销量 + 周时间
    run(A, "上周华南各门店销量，从小到大", show_raw=False)

    # 例7：兼容矩阵拦截——profit_rate × brand 不在兼容维度里
    run(A, "上季度各品牌利润率", show_raw=False)

    # 例8：兼容矩阵拦截——refund_rate 只允许 channel/region/time
    run(A, "今年各城市退款率", show_raw=False)

    # ========================================================
    # 指纹等价性演示：表面写法不同、归一化槽位等价 → 同一 hash
    # ========================================================
    print("=" * 78)
    print("【指纹等价性演示】")
    cases = [
        ("q1", "上季度各城市利润率"),
        ("q2", "上个季度每个城市的利润占比"),      # 别名全换：上个季度/每个城市/利润占比
        ("q3", "２０２６Ｑ３各城市利润率"),         # 全角 + 绝对季度表达（正则兜底解析）
        ("q4", "上季度各城市利润率，降序"),          # 多一个排序 → 应该是不同指纹
    ]
    hashes = {}
    for tag, q in cases:
        norm = normalize(q)
        _, chosen = scan(A, norm)
        slots = assemble(chosen, norm)
        if validate(slots):
            hashes[tag] = None
        else:
            hashes[tag] = fingerprint(slots)
        print(f"  {tag}  {q!r}")
        print(f"      hash = {hashes[tag]}")
    print()
    same = hashes["q1"] == hashes["q2"] == hashes["q3"]
    print(f"  q1 == q2 == q3 : {same}")
    if same:
        print("    → 四种表面写法归一到同一指纹 = 同一任务")
        print("    → 可命中 L1(SQL)/L2(结果) 缓存（L2 另需新鲜度校验）")
    print(f"  q1 == q4       : {hashes['q1'] == hashes['q4']}")
    if hashes["q1"] != hashes["q4"]:
        print("    → q4 多了 order_by（降序），是不同查询")
        print("    → 对应字段级 diff 的 VARIANT：不复用结果，可复用 SQL 骨架")

    # ========================================================
    # 别名运营闭环演示：入库前(miss→升级LLM) vs 入库后(全命中)
    # ========================================================
    print("=" * 78)
    print("【别名运营闭环】日志挖出未命中词 → 评审入库 → 同一问句命中恢复")
    query = "去年羊城营业额，由高到低取前3"
    norm = normalize(query)
    print(f"输入: {query!r}\n归一化: {norm!r}\n")

    print("-- 入库前（基础词典）--")
    _, old_chosen = scan(build_automaton(), norm)
    for h in old_chosen:
        print(f'  {h["s"]}:{h["e"]}  {h["term"]:<6} {h["slot"]:<10} {h["target"]}')
    gaps = "营业额(指标) / 羊城(城市值) / 由高到低(排序方向)"
    print(f"  命中 {len(old_chosen)} 个；缺口: {gaps}")
    print("  → 槽位不完整（无指标），快路径放弃，升级 LLM 兜底\n")

    print("-- 入库后（含新别名）--")
    _, new_chosen = scan(A, norm)
    for h in new_chosen:
        print(f'  {h["s"]}:{h["e"]}  {h["term"]:<6} {h["slot"]:<10} {h["target"]}')
    slots = assemble(new_chosen, norm)
    if not validate(slots):
        print("  → 槽位完整，校验通过，指纹:")
        print(f"  canonical: {canonicalize(slots)}")
        print(f"  hash: {fingerprint(slots)}")
        print("  → 零 LLM，毫秒级，重新回到快路径")
