"""将 MySQL 中的案例数据导入 ChromaDB 向量库"""
import asyncio
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from tortoise import Tortoise
from langchain_core.documents import Document
from app.config import settings


async def build_index():
  # 连接 MySQL（与 app/db/database.py 保持一致）
  await Tortoise.init(
    db_url=settings.DATABASE_URL,
    modules={"models": ["app.models.case"]},
    use_tz=False, # Windows 下不设置时区避免报错
   )
  from app.models.case import Case

  cases = await Case.all()
  if not cases:
    print(" 数据库中没有案例，请先运行 seed_cases.py")
    return

  # 构建 LangChain Document 列表
  documents = []
  for case in cases:
    doc = Document(
      page_content=f"{case.title}\n{case.summary or ''}\n{case.content or ''}",
      metadata={
        "id": str(case.id),
        "title": case.title,
        "case_number": case.case_number,
        "court": case.court,
        "case_type": case.case_type,
        "judgment_date": case.judgment_date.isoformat() if case.judgment_date else None,
        "summary": case.summary,
        "laws": case.laws,
       },
     )
    documents.append(doc)

  # 写入向量库（幂等）：按 metadata.id 去重，清理历史重复后只补新增案例
  from app.rag.vector_store import get_vector_store
  vector_store = get_vector_store()

  existing = vector_store.get(include=["metadatas"])
  existing_vector_ids = existing.get("ids") or []
  existing_metadatas = existing.get("metadatas") or []
  seen_case_ids: dict[str, str] = {}
  duplicate_vector_ids: list[str] = []
  for vector_id, meta in zip(existing_vector_ids, existing_metadatas):
    case_id = (meta or {}).get("id")
    if not case_id: # 无归属脏数据不动，避免误删
      continue
    if case_id in seen_case_ids:
      duplicate_vector_ids.append(vector_id)
    else:
      seen_case_ids[case_id] = vector_id

  if duplicate_vector_ids:
    vector_store.delete(ids=duplicate_vector_ids)

  new_documents = [
    doc for doc in documents
    if doc.metadata.get("id") not in seen_case_ids
   ]
  if new_documents:
    vector_store.add_documents(new_documents)

  final_count = len(vector_store.get()["ids"])
  print(
    f"案例数 {len(documents)}；向量库原有 {len(existing_vector_ids)} 条"
    f"（唯一案例 {len(seen_case_ids)}，重复 {len(duplicate_vector_ids)}）；"
    f"新增 {len(new_documents)}；当前 {final_count} 条"
   )
  await Tortoise.close_connections()


if __name__ == "__main__":
  print("🔨 开始构建向量索引...")
  asyncio.run(build_index())
