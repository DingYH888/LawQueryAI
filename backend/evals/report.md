# LawQueryAI 弱标注回归基线报告

> ⚠️ **自动弱标注、非人工、仅作相对回归基线。**
> 固定小样本：cases=3，query=6，top_k=5。
> **不得据此宣称 Recall@5/MRR 提升，不得写入简历/对外文档。**

## 数据来源与口径
- 来源：MySQL legal_db.cases
- 标签规则：title 查询→search；summary 首句→qa；正例=来源案例 id（构造式弱标签）
- 生成时间：2026-11-12T10:11:07+00:00

## 召回对比（均值）
| 组 | Hit@1 | Hit@3 | Hit@5 | MRR@5 | 平均耗时(ms) | 最大耗时(ms) |
|---|---:|---:|---:|---:|---:|---:|
| vector_only | 1.0 | 1.0 | 1.0 | 1.0 | 12.625 | 24.624 |
| bm25_only | 0.6667 | 1.0 | 1.0 | 0.8056 | 0.106 | 0.268 |
| hybrid | 1.0 | 1.0 | 1.0 | 1.0 | 10.226 | 11.501 |
| hybrid_rerank | 1.0 | 1.0 | 1.0 | 1.0 | 10.045 | 11.936 |

## 意图识别（结构化输出）
- 与构造标签一致率：0.8333
- 降级率：0.0
- 延迟：平均 5168.556 ms，最大 7642.06 ms
- token：input=1738，output=1039，total=2777（available=True）

| query | expected | predicted | correct | fallback | latency_ms |
|---|---|---|---|---|---:|
| 张三与李四离婚财产纠纷案 | search | qa | False | None | 7642.06 |
| 夫妻双方因感情破裂协议离婚，对婚后共同购买的房产分割产生争议 | qa | qa | True | None | 2274.28 |
| 李某交通事故赔偿案 | search | search | True | None | 5759.004 |
| 交通事故受害人起诉肇事方及保险公司，要求赔偿医疗费、误工费、残疾赔偿金等 | qa | qa | True | None | 5810.418 |
| 王某劳动仲裁案 | search | search | True | None | 4508.343 |
| 劳动者因公司拖欠工资被迫离职，要求支付经济补偿金 | qa | qa | True | None | 5017.231 |

## 已知局限
- 自动弱标注、非人工，仅作相对回归基线
- 固定小样本（cases=3、query=6），不得宣称 Recall@5/MRR 提升，不得写入简历/对外文档
- query 由来源案例 title/summary 构造，与案例文本高度重合，召回指标偏乐观
- top_k=5 大于案例数时 Recall@3/5 必然退化，不能当作效果证明
- 意图集只覆盖 qa/search（title→search、summary→qa），未覆盖 document；该一致率仅代表与构造规则一致，不代表线上表现
- Reranker 为关键词命中计数，中文无空格时可能全 0；本次 hybrid_rerank 与 hybrid 的 6/6 条排序完全相同，不主张重排收益

> 逐条检索明细见同目录 `report.json`。
