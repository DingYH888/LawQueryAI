"""文书数据模型"""
from tortoise import fields
from tortoise.models import Model


class GeneratedDocument(Model):
  # 主键，使用 UUID 类型
  id = fields.UUIDField(pk=True)
  # 外键，关联 User 模型，反向查询名为 documents
  user = fields.ForeignKeyField("models.User", related_name="documents")
  # 文档标题，最大长度 200，允许为空
  title = fields.CharField(max_length=200, null=True)
  # 文档类型，最大长度 50，允许为空
  document_type = fields.CharField(max_length=50, null=True)
  # 文档内容，长文本，允许为空
  content = fields.TextField(null=True)
  # 生成文档时使用的参数，JSON 格式，允许为空
  params = fields.JSONField(null=True)
  # 文档引用的其他资源信息，JSON 格式，允许为空
  doc_references = fields.JSONField(null=True)
  # 创建时间，自动填充
  created_at = fields.DatetimeField(auto_now_add=True)
  # 更新时间，自动更新
  updated_at = fields.DatetimeField(auto_now=True)

  class Meta:
    table = "generated_documents"
