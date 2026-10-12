"""下载 Swagger UI 静态资源到本地，解决 /docs 页面依赖 CDN（jsdelivr 被墙）导致空白的问题。

用法（在 backend 目录下）：
    .venv\\Scripts\\python.exe scripts\\fetch_swagger_ui.py

脚本会依次尝试多个镜像源，把 swagger-ui.css 和 swagger-ui-bundle.js
下载到 backend/static/swagger-ui/ 目录；应用启动时若检测到本地文件存在，
/docs 便完全走本地资源、不再依赖外部 CDN。
"""
import ssl
import sys
import urllib.request
from pathlib import Path

# 项目根目录：本脚本位于 <backend>/scripts/ 下，取其上一级即为 backend 根
BACKEND_DIR = Path(__file__).resolve().parent.parent
OUT_DIR = BACKEND_DIR / "static" / "swagger-ui"

# 每个目标文件对应多个候选下载源（按优先级排列，任一成功即可）
# 优先国内可达的 bootcdn，其次 unpkg、fastly(jsdelivr 镜像)、jsdelivr 原生
TARGETS = [
    {
        "filename": "swagger-ui.css",
        "urls": [
            "https://cdn.bootcdn.net/ajax/libs/swagger-ui/5.17.14/swagger-ui.css",
            "https://cdn.bootcdn.net/ajax/libs/swagger-ui/5.10.3/swagger-ui.css",
            "https://unpkg.com/swagger-ui-dist@5/swagger-ui.css",
            "https://fastly.jsdelivr.net/npm/swagger-ui-dist@5/swagger-ui.css",
            "https://cdn.jsdelivr.net/npm/swagger-ui-dist@5/swagger-ui.css",
        ],
    },
    {
        "filename": "swagger-ui-bundle.js",
        "urls": [
            "https://cdn.bootcdn.net/ajax/libs/swagger-ui/5.17.14/swagger-ui-bundle.js",
            "https://cdn.bootcdn.net/ajax/libs/swagger-ui/5.10.3/swagger-ui-bundle.js",
            "https://unpkg.com/swagger-ui-dist@5/swagger-ui-bundle.js",
            "https://fastly.jsdelivr.net/npm/swagger-ui-dist@5/swagger-ui-bundle.js",
            "https://cdn.jsdelivr.net/npm/swagger-ui-dist@5/swagger-ui-bundle.js",
        ],
    },
]

# 最小合法文件大小（字节），用于过滤 404/报错页面等无效响应
MIN_SIZE = {"swagger-ui.css": 1000, "swagger-ui-bundle.js": 50_000}


def download(url: str) -> bytes | None:
    """尝试从单个 url 下载，成功返回内容，失败返回 None。"""
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
            "Accept": "*/*",
        },
    )
    # 忽略证书校验，规避部分镜像站证书链不完整导致的失败
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    try:
        # 显式关闭代理，避免走本地代理导致下载失败
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(req, timeout=30, context=ctx) as resp:
            if resp.status != 200:
                return None
            data = resp.read()
            return data
    except Exception:
        return None


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    ok = True
    for target in TARGETS:
        filename = target["filename"]
        dest = OUT_DIR / filename
        print(f"[下载] {filename} ...")
        success = False
        for url in target["urls"]:
            data = download(url)
            if data is not None and len(data) >= MIN_SIZE[filename]:
                dest.write_bytes(data)
                print(f"    成功 <- {url}  ({len(data)} 字节)")
                success = True
                break
            elif data is not None:
                print(f"    内容过小({len(data)}B)，跳过 <- {url}")
        if not success:
            ok = False
            print(f"    失败：所有镜像源均不可用！")
    print("\n=== 完成 ===")
    if ok:
        print(f"资源已就绪于: {OUT_DIR}")
        print("请重启 uvicorn，/docs 将使用本地静态资源。")
        return 0
    print("部分资源下载失败，请检查网络或手动放置文件。")
    return 1


if __name__ == "__main__":
    sys.exit(main())