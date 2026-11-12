"""构造弱标注评测集 golden.jsonl。

标签来源：MySQL `cases` 的 title / summary 自动构造，**非人工标注**。
文件第一行是 _meta，明确标注「自动弱标注、非人工、仅作相对回归基线」。
正例固定为 query 的来源案例 id。
"""
import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[1]
if str(BASE_DIR) not in sys.path:
  sys.path.insert(0, str(BASE_DIR))

from tortoise import Tortoise

from app.config import settings

OUT = Path(__file__).resolve().parent / "golden.jsonl"
MAX_QUERY_LEN = 120


def _summary_query(summary: str | None) -> str:
  if not summary:
    return ""
  first = summary.split("。")[0].strip()
  return first[:MAX_QUERY_LEN]


async def build():
  await Tortoise.init(
    db_url=settings.DATABASE_URL,
    modules={"models": ["app.models.case"]},
    use_tz=False,
   )
  from app.models.case import Case

  cases = await Case.all().order_by("id")
  records: list[dict] = []
  for case in cases:
    case_id = str(case.id)
    title = (case.title or "").strip()
    if title:
      records.append({
        "query": title,
        "relevant_ids": [case_id],
        "source": "title",
        "expected_intent": "search",
        "case_title": title,
       })
    summary_query = _summary_query(case.summary)
    if summary_query:
      records.append({
        "query": summary_query,
        "relevant_ids": [case_id],
        "source": "summary",
        "expected_intent": "qa",
        "case_title": title,
       })
  await Tortoise.close_connections()

  meta = {
    "_meta": "自动弱标注、非人工、仅作相对回归基线",
    "warning": "固定小样本，不得宣称 Recall@5/MRR 提升，不得写入简历/对外文档",
    "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    "db": "MySQL legal_db.cases",
    "case_count": len(cases),
    "query_count": len(records),
    "label_rule": "title 查询→search；summary 首句→qa；正例=来源案例 id（构造式弱标签）",
   }

  lines = [json.dumps(meta, ensure_ascii=False)]
  lines += [json.dumps(r, ensure_ascii=False) for r in records]
  OUT.write_text("\n".join(lines) + "\n", encoding="utf-8")
  print(f"golden.jsonl written: cases={len(cases)} queries={len(records)} -> {OUT}")


if __name__ == "__main__":
  os.environ.setdefault("PYTHONIOENCODING", "utf-8")
  asyncio.run(build())
