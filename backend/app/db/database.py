"""数据库连接管理 - Tortoise ORM"""
from tortoise import Tortoise
from app.config import settings

async def init_db():
  db_url = settings.DATABASE_URL  # mysql:// 格式，Tortoise (aiomysql) 直接支持

  await Tortoise.init(
    db_url=db_url,
    modules={"models": [
      "models.user",
      "models.case",
     ]},
    use_tz=False, # Windows 下不设置时区避免报错
   )
  await Tortoise.generate_schemas(safe=True)


#关闭数据库
async def close_db():
  await Tortoise.close_connections()
