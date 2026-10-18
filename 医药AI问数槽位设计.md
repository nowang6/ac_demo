# 医药公司 AI 问数的槽位类型设计

> 整理日期：2026-10-18
> 背景：基于本仓库 `demo.py`（AC 自动机问数快路径演示）的槽位设计，讨论迁移到医药行业场景时的设计思路。

## 一、有没有行业通用模板？

**没有强制的行业通用模板，但这件事可以分两层看，每层都有事实上的参照物：**

1. **槽位语法层（slot schema）——跨行业高度通用**。`demo.py` 里的 7 类槽位（metric / dimension / dim_value / time_expr / order / limit / intent）本质就是语义层（Semantic Layer）查询语法的槽位化，和 dbt MetricFlow 的 `metrics + group_by + where + time`、阿里 OneData 指标体系的"原子指标 + 修饰词（业务限定）+ 时间周期 + 派生指标"四要素是同一套东西。业界的共识是：**查询、筛选、聚合、对比、排序这些"算子"和槽位类型是通用的，具体填什么词是每家企业自己的词典**。所以换到医药行业，槽位**骨架基本不用动**。

2. **词典内容层——医药有行业标准，但规范的是"词"不是"槽位"**：ATC 药物分类、国家医保药品代码、MedDRA（不良事件术语）、ICD-10（诊断）、CDISC（临床提交数据标准）、OMOP CDM（真实世界研究通用数据模型）。这些直接决定维度值表和层级怎么建。

## 二、建议的医药槽位设计

保留 7 类通用槽位，新增 4 类医药特有的：

| 槽位类型 | 通用/医药特有 | 示例词条 → 归一目标 | 说明 |
|---|---|---|---|
| `metric` | 通用 | 纯销、流向、库存周转、达成率、单产 → `pure_sales` 等 | 商业化域指标，口径在 REGISTRY 声明 |
| `dimension` | 通用 | 各医院、按科室、按产品线、按渠道 → `hospital` / `department` / `channel` | 分组维度 |
| `dim_value` | 通用 | 三甲、DTP、PD-1、华东 → `hospital_grade=3A` | 单值限定 → filters |
| `time_expr` | 通用 | 本财年、Q3、环比 → `fy:0` / `quarter:0` | 药企普遍用财年（FY），指标按季度下达 |
| `order` / `limit` / `intent` | 通用 | 降序、前10、排名、趋势 | 不变 |
| **`caliber`（口径）** | **医药新增** | 含税/不含税、纯销口径/购进口径、盒/最小单位 | 流向数据同一指标多种口径，同名不同算是医药问数最常见的坑 |
| **`comparator`（对标）** | **医药新增** | "和达伯舒比"、"对比K药" → `benchmark=pembrolizumab` | 市场份额/竞品分析是药企高频问法，和 filters 语义不同 |
| **`cohort`（人群）** | **医药新增** | "2型糖尿病老年患者" | 医学部/RWE 场景，值是条件树（AND/OR），不是单值——建议命中后直接升级 LLM 解析，不走快路径 |
| **`product_group`（药品组）** | **医药新增** | "免疫治疗线"、"肿瘤线产品" → 产品集合引用 | 药品维度天然多层（治疗领域→ATC→通用名→商品名→规格），集合引用解决"看整个PD-1类"这类问法 |

## 三、医药场景的三个关键设计点

1. **药品维度要支持层级和集合**。"PD-1 的销售额"里 `PD-1` 不是单值，是一个产品集合的引用。`dim_value` 的 target 需要允许 `product_group=xxx` 这种指向一组主数据 ID 的形式，AC 词典只负责触发，展开交给注册表。

2. **别名运营在医药价值极高**。商品名/通用名/英文缩写/俗称（"K药""O药"）极多，且医生和销售的说法完全不同。`demo.py` 里 `NEW_ALIASES` 的"日志挖未命中 → 评审入库"闭环，在医药就是核心机制而不是锦上添花。

3. **REGISTRY 兼容矩阵之外还需要权限矩阵**。流向数据涉及商业合规（两票制、医生个人信息），同一个问句不同大区销售能看到的边界不同。这层不适合放词典里，建议放在校验层（`validate`）之后、SQL 生成之前做行级/列级裁剪。

## 四、落地建议

从**商业流向域**起步（指标字典最成熟、问数需求最高频），把公司现有指标字典/维度值表打平成 LEXICON——就是 `demo.py` 现在的思路，换一本词典而已；医学/临床域（cohort 类问法）后置，走 LLM 兜底路径。

## 参考来源

- [AI用数 - NL2Semantic2SQL 与语义层实践（龙石数据）](https://www.longshidata.com/blog/article/category/aiusage.html)
- [本体化语义层的五层能力拆解与 NL2LF2SQL 实践 - SegmentFault](https://segmentfault.com/a/1190000047774818)
- [Build and centralize metrics with the dbt Semantic Layer](https://www.getdbt.com/blog/build-centralize-and-deliver-consistent-metrics-with-the-dbt-semantic-layer)
