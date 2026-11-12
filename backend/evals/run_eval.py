"""Phase 3 弱标注回归评测。

- 召回四组：vector-only / BM25-only / hybrid / hybrid+rerank
- 意图：与构造标签一致率（构造式弱标签）+ 降级率 + 延迟/token
- 数据：evals/golden.jsonl（自动弱标注、非人工、仅作相对回归基线）
- 输出：evals/report.json + evals/report.md（脚本真跑后落盘）

注意：这是固定小样本回归基线，不是效果评测；不得宣称 Recall@5/MRR 提升。
"""
import asyncio
import json
import logging
import statistics
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[1]
if str(BASE_DIR) not in sys.path:
  sys.path.insert(0, str(BASE_DIR))

EVAL_DIR = Path(__file__).resolve().parent
GOLDEN = EVAL_DIR / "golden.jsonl"
TOP_K = 5

from app.agents.intent_agent import IntentAgent
from app.agents.retrieval_agent import RetrievalAgent
from app.observability.spans import LOGGER_NAME
from app.rag.retriever import HybridRetriever

CAVEATS = [
  "自动弱标注、非人工，仅作相对回归基线",
  "固定小样本（cases=3、query=6），不得宣称 Recall@5/MRR 提升，不得写入简历/对外文档",
  "query 由来源案例 title/summary 构造，与案例文本高度重合，召回指标偏乐观",
  "top_k=5 大于案例数时 Recall@3/5 必然退化，不能当作效果证明",
  "意图集只覆盖 qa/search（title→search、summary→qa），未覆盖 document；该一致率仅代表与构造规则一致，不代表线上表现",
  "Reranker 为关键词命中计数，中文无空格时可能全 0；本次 hybrid_rerank 与 hybrid 的 6/6 条排序完全相同，不主张重排收益",
]


class _EventCapture(logging.Handler):
  def __init__(self):
    super().__init__(level=logging.DEBUG)
    self.events: list[dict] = []

  def emit(self, record):
    payload = getattr(record, "observability", None)
    if isinstance(payload, dict):
      self.events.append(dict(payload))


def load_golden() -> tuple[dict, list[dict]]:
  meta = None
  records: list[dict] = []
  for raw in GOLDEN.read_text(encoding="utf-8").splitlines():
    raw = raw.strip()
    if not raw:
      continue
    obj = json.loads(raw)
    if "_meta" in obj:
      meta = obj
    else:
      records.append(obj)
  if meta is None:
    raise SystemExit("golden.jsonl 缺少 _meta 文件头")
  return meta, records


def _dedupe_ids(items, top_k: int) -> list[str]:
  out: list[str] = []
  seen: set[str] = set()
  for item in items:
    if isinstance(item, dict):
      case_id = item.get("id")
    else:
      case_id = (getattr(item, "metadata", None) or {}).get("id")
    if case_id and case_id not in seen:
      seen.add(case_id)
      out.append(str(case_id))
    if len(out) >= top_k:
      break
  return out


def _score(ids: list[str], relevant: list[str]) -> dict:
  rel = set(relevant)
  return {
    "hit@1": 1.0 if ids[:1] and ids[0] in rel else 0.0,
    "hit@3": 1.0 if any(i in rel for i in ids[:3]) else 0.0,
    "hit@5": 1.0 if any(i in rel for i in ids[:5]) else 0.0,
    "mrr@5": next(
      (1.0 / (rank + 1) for rank, case_id in enumerate(ids[:5]) if case_id in rel),
      0.0,
     ),
   }


def _aggregate(per_query: list[dict]) -> dict:
  if not per_query:
    return {}
  n = len(per_query)
  return {
    key: round(sum(row[key] for row in per_query) / n, 4)
    for key in ("hit@1", "hit@3", "hit@5", "mrr@5")
   }


def _latency_summary(rows: list[dict]) -> dict:
  values = [row["latency_ms"] for row in rows]
  if not values:
    return {}
  return {
    "total_ms": round(sum(values), 3),
    "mean_ms": round(statistics.mean(values), 3),
    "max_ms": round(max(values), 3),
   }


async def eval_retrieval(records: list[dict]) -> dict:
  hybrid = HybridRetriever()
  agent = RetrievalAgent()

  async def vector_only(query: str) -> list[str]:
    docs = await hybrid.vector_retriever.ainvoke(query)
    return _dedupe_ids(docs, TOP_K)

  async def bm25_only(query: str) -> list[str]:
    if hybrid.bm25_retriever is None:
      return []
    return _dedupe_ids(hybrid.bm25_retriever.invoke(query), TOP_K)

  async def hybrid_only(query: str) -> list[str]:
    return _dedupe_ids(await hybrid.retrieve(query, top_k=TOP_K), TOP_K)

  async def hybrid_rerank(query: str) -> list[str]:
    return _dedupe_ids(await agent.retrieve(query, top_k=TOP_K), TOP_K)

  runners = {
    "vector_only": vector_only,
    "bm25_only": bm25_only,
    "hybrid": hybrid_only,
    "hybrid_rerank": hybrid_rerank,
   }

  results: dict = {}
  for name, runner in runners.items():
    per_query = []
    for rec in records:
      started = time.perf_counter()
      ids = await runner(rec["query"])
      latency = round((time.perf_counter() - started) * 1000, 3)
      per_query.append({
        "query": rec["query"],
        "source": rec.get("source"),
        "relevant_ids": rec["relevant_ids"],
        "retrieved_ids": ids,
        "latency_ms": latency,
        **_score(ids, rec["relevant_ids"]),
       })
    results[name] = {
      "per_query": per_query,
      "aggregate": _aggregate(per_query),
      "latency_ms": _latency_summary(per_query),
     }
  return results


async def eval_intent(records: list[dict]) -> dict:
  agent = IntentAgent()
  logger = logging.getLogger(LOGGER_NAME)
  capture = _EventCapture()
  old_level = logger.level
  logger.setLevel(logging.DEBUG)
  logger.addHandler(capture)
  per_query = []
  try:
    for rec in records:
      before = len(capture.events)
      started = time.perf_counter()
      out = await agent.recognize_structured(rec["query"])
      latency = round((time.perf_counter() - started) * 1000, 3)
      new_events = capture.events[before:]
      usage = next(
        (e.get("usage") for e in new_events
         if e.get("event") == "intent.structured" and e.get("usage")),
        None,
       )
      per_query.append({
        "query": rec["query"],
        "expected_intent": rec["expected_intent"],
        "predicted_intent": out["intent"],
        "correct": out["intent"] == rec["expected_intent"],
        "fallback_reason": out.get("fallback_reason"),
        "latency_ms": latency,
        "usage": usage,
       })
  finally:
    logger.removeHandler(capture)
    logger.setLevel(old_level)

  n = len(per_query)
  usage_rows = [row["usage"] for row in per_query if row.get("usage")]
  return {
    "accuracy": round(sum(1 for row in per_query if row["correct"]) / n, 4) if n else None,
    "fallback_rate": round(sum(1 for row in per_query if row["fallback_reason"]) / n, 4) if n else None,
    "latency_ms": _latency_summary(per_query),
    "tokens": {
      "available": bool(usage_rows),
      "input_tokens": sum(u.get("input_tokens") or 0 for u in usage_rows),
      "output_tokens": sum(u.get("output_tokens") or 0 for u in usage_rows),
      "total_tokens": sum(u.get("total_tokens") or 0 for u in usage_rows),
     },
    "per_query": per_query,
   }


def _render_md(report: dict) -> str:
  meta = report["meta"]
  golden = meta["golden_meta"]
  lines = [
    "# LawQueryAI 弱标注回归基线报告",
    "",
    "> ⚠️ **自动弱标注、非人工、仅作相对回归基线。**",
    f"> 固定小样本：cases={meta['case_count']}，query={meta['query_count']}，top_k={meta['top_k']}。",
    "> **不得据此宣称 Recall@5/MRR 提升，不得写入简历/对外文档。**",
    "",
    "## 数据来源与口径",
    f"- 来源：{golden.get('db')}",
    f"- 标签规则：{golden.get('label_rule')}",
    f"- 生成时间：{meta['generated_at']}",
    "",
    "## 召回对比（均值）",
    "| 组 | Hit@1 | Hit@3 | Hit@5 | MRR@5 | 平均耗时(ms) | 最大耗时(ms) |",
    "|---|---:|---:|---:|---:|---:|---:|",
   ]
  for name, result in report["retrieval"].items():
    agg = result["aggregate"]
    lat = result["latency_ms"]
    lines.append(
      f"| {name} | {agg.get('hit@1')} | {agg.get('hit@3')} | {agg.get('hit@5')} "
      f"| {agg.get('mrr@5')} | {lat.get('mean_ms')} | {lat.get('max_ms')} |"
     )

  intent = report["intent"]
  tokens = intent["tokens"]
  lines += [
    "",
    "## 意图识别（结构化输出）",
    f"- 与构造标签一致率：{intent['accuracy']}",
    f"- 降级率：{intent['fallback_rate']}",
    f"- 延迟：平均 {intent['latency_ms'].get('mean_ms')} ms，最大 {intent['latency_ms'].get('max_ms')} ms",
    f"- token：input={tokens['input_tokens']}，output={tokens['output_tokens']}，"
    f"total={tokens['total_tokens']}（available={tokens['available']}）",
    "",
    "| query | expected | predicted | correct | fallback | latency_ms |",
    "|---|---|---|---|---|---:|",
   ]
  for row in intent["per_query"]:
    lines.append(
      f"| {row['query']} | {row['expected_intent']} | {row['predicted_intent']} "
      f"| {row['correct']} | {row['fallback_reason']} | {row['latency_ms']} |"
     )

  lines += ["", "## 已知局限"]
  lines += [f"- {item}" for item in meta["caveats"]]
  lines += ["", "> 逐条检索明细见同目录 `report.json`。", ""]
  return "\n".join(lines)


async def main():
  meta, records = load_golden()
  retrieval = await eval_retrieval(records)
  intent = await eval_intent(records)
  report = {
    "meta": {
      "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
      "golden_meta": meta,
      "case_count": meta.get("case_count"),
      "query_count": len(records),
      "top_k": TOP_K,
      "caveats": CAVEATS,
     },
    "retrieval": retrieval,
    "intent": intent,
   }
  (EVAL_DIR / "report.json").write_text(
    json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
   )
  (EVAL_DIR / "report.md").write_text(_render_md(report), encoding="utf-8")

  print(json.dumps({
    "retrieval_aggregate": {k: v["aggregate"] for k, v in retrieval.items()},
    "intent": {
      "accuracy": intent["accuracy"],
      "fallback_rate": intent["fallback_rate"],
      "latency_ms": intent["latency_ms"],
      "tokens": intent["tokens"],
     },
   }, ensure_ascii=True, indent=2))


def render_only():
  """用已有 report.json 重渲染 report.md（不重跑 LLM，指标保持不变）。"""
  report = json.loads((EVAL_DIR / "report.json").read_text(encoding="utf-8"))
  report["meta"]["caveats"] = CAVEATS
  (EVAL_DIR / "report.json").write_text(
    json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
   )
  (EVAL_DIR / "report.md").write_text(_render_md(report), encoding="utf-8")
  print("report re-rendered from report.json (no re-eval)")


if __name__ == "__main__":
  if "--render-only" in sys.argv:
    render_only()
  else:
    asyncio.run(main())
