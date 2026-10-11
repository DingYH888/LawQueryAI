"""重排序器 — 对检索结果进行重排序"""
from typing import List, Dict

class Reranker:
  """基于关键词匹配度的重排序器"""
  async def rerank(self, query: str, documents: List[Dict], top_k: int = 5) -> List[Dict]:
    """query:查询文本
        documents:待排序的文档列表
        top_k:返回数量
    """
    query_terms = set(query.lower().split())
    scored_docs = []

    for doc in documents:
      content = doc.get("content", "").lower()#提取的内容
      title = doc.get("title", "").lower()#提取的标题
      score = sum(1 for term in query_terms if term in content or term in title)#匹配度分数
      scored_docs.append((score, doc))

    scored_docs.sort(key=lambda x: x[0], reverse=True)#根据相关性分数进行排序
    return [doc for _, doc in scored_docs[:top_k]]
