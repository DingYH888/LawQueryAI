"""Phase 4 一键验收（标准 3307 环境）。

流程：docker compose(必要时) → seed_cases → build_index → 启动 uvicorn →
/health + qa/search/document 三类请求 + 两次真实 SSE 录制结构对比 + pytest →
落盘 evals/verify_demo_report.json 与 evals/sse_recording.json。

用法：uv run python scripts/verify_demo.py
可选：VERIFY_PORT=8010 指定端口；--index-twice 额外验证索引幂等。
"""
import json
import os
import re
import socket
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import httpx

BACKEND_DIR = Path(__file__).resolve().parents[1]
REPO_DIR = BACKEND_DIR.parent
EVAL_DIR = BACKEND_DIR / "evals"
EVAL_DIR.mkdir(parents=True, exist_ok=True)
REPORT_PATH = EVAL_DIR / "verify_demo_report.json"
RECORDING_PATH = EVAL_DIR / "sse_recording.json"
SERVER_LOG = EVAL_DIR / "verify_demo_server.log"
PORT = int(os.environ.get("VERIFY_PORT", "8010"))
BASE = f"http://127.0.0.1:{PORT}"
PY = sys.executable

KNOWN_LIMITATIONS = [
  "LangGraph 1.2.14：客户端断开 SSE 后节点任务仍会跑完，本阶段只登记不改框架行为",
  "非流式长输出仅有 ChatOpenAI/httpx 的 request_timeout（单次 I/O 超时），无应用层总墙钟超时",
  "RAG_SCORE_THRESHOLD 未接线、court/case_type 过滤位置未修正（3 条样本无法验证效果）",
  "CORS expose_headers 未配置（当前 Vite 同源代理无影响）",
  "文书未做持久化",
]


def _run(cmd: list[str], cwd: Path, timeout: int = 300) -> dict:
  proc = subprocess.run(
    cmd, cwd=str(cwd), capture_output=True, text=True,
    encoding="utf-8", errors="replace", timeout=timeout,
   )
  return {
    "cmd": " ".join(cmd),
    "exit": proc.returncode,
    "stdout_tail": "\n".join((proc.stdout or "").strip().splitlines()[-3:]),
    "stderr_tail": "\n".join((proc.stderr or "").strip().splitlines()[-3:]),
   }


def _tcp_open(host: str, port: int) -> bool:
  with socket.socket() as sock:
    sock.settimeout(0.8)
    return sock.connect_ex((host, port)) == 0


def _wait_tcp(host: str, port: int, timeout: int = 120) -> bool:
  deadline = time.time() + timeout
  while time.time() < deadline:
    if _tcp_open(host, port):
      return True
    time.sleep(2)
  return False


def _ensure_mysql(db_host: str, db_port: int) -> dict:
  if _tcp_open(db_host, db_port):
    return {"action": "skipped", "reason": f"{db_host}:{db_port} already open"}
  up = _run(["docker", "compose", "up", "-d"], REPO_DIR, timeout=180)
  ready = _wait_tcp(db_host, db_port, timeout=150)
  return {"action": "docker compose up -d", "exit": up["exit"],
          "ready": ready, "tail": up["stdout_tail"] or up["stderr_tail"]}


def _run_script(script: str, timeout: int = 300) -> dict:
  env_note = {"PYTHONIOENCODING": "utf-8"}
  old = os.environ.get("PYTHONIOENCODING")
  os.environ.update(env_note)
  try:
    return _run([PY, f"scripts/{script}"], BACKEND_DIR, timeout=timeout)
  finally:
    if old is None:
      os.environ.pop("PYTHONIOENCODING", None)
    else:
      os.environ["PYTHONIOENCODING"] = old


def _wait_health(client: httpx.Client, timeout: int = 180) -> dict:
  deadline = time.time() + timeout
  last = None
  while time.time() < deadline:
    try:
      resp = client.get("/health", timeout=5)
      if resp.status_code == 200:
        return {"status": resp.status_code,
                "trace_id": resp.headers.get("X-Trace-Id"),
                "body": resp.json()}
      last = f"HTTP {resp.status_code}"
    except Exception as exc:
      last = f"{type(exc).__name__}: {exc}"
    time.sleep(2)
  raise RuntimeError(f"backend not ready: {last}")


def _sse_record(client: httpx.Client, token: str, message: str) -> dict:
  headers = {"Authorization": f"Bearer {token}"}
  frames: list[str] = []
  with client.stream("POST", "/api/chat/stream",
                     json={"message": message}, headers=headers) as resp:
    for line in resp.iter_lines():
      if line.startswith("data: "):
        frames.append(line[6:])
    status = resp.status_code
    trace_id = resp.headers.get("X-Trace-Id")

  parsed = []
  all_json = True
  allowed = True
  allowed_keys = {"session_id", "content", "sources"}
  for frame in frames[:-1]:
    try:
      payload = json.loads(frame)
    except Exception:
      all_json = False
      continue
    parsed.append(payload)
    if isinstance(payload, dict) and not set(payload.keys()).issubset(allowed_keys):
      allowed = False

  first_keys = sorted(parsed[0].keys()) if parsed and isinstance(parsed[0], dict) else []
  record = {
    "status": status,
    "trace_id": trace_id,
    "frame_count": len(frames),
    "first_frame_keys": first_keys,
    "content_present": any(isinstance(p, dict) and "content" in p for p in parsed),
    "sources_present": any(isinstance(p, dict) and "sources" in p for p in parsed),
    "all_payloads_json_objects": all_json,
    "allowed_keys_only": allowed,
    "terminal": frames[-1] if frames else None,
   }
  record["schema"] = _sse_schema(record)
  return record


def _sse_schema(record: dict) -> dict:
  return {
    "first_frame_keys": record["first_frame_keys"],
    "content_present": record["content_present"],
    "terminal": record["terminal"],
    "all_payloads_json_objects": record["all_payloads_json_objects"],
    "allowed_keys_only": record["allowed_keys_only"],
   }


def _check_sse_schema(record: dict) -> list[str]:
  problems = []
  if record["status"] != 200:
    problems.append(f"SSE status={record['status']}")
  if record["first_frame_keys"] != ["session_id"]:
    problems.append(f"first_frame_keys={record['first_frame_keys']}")
  if not record["content_present"]:
    problems.append("no content frame")
  if record["terminal"] != "[DONE]":
    problems.append(f"terminal={record['terminal']}")
  if not record["all_payloads_json_objects"]:
    problems.append("non-json payload")
  if not record["allowed_keys_only"]:
    problems.append("unexpected payload keys")
  return problems


def main() -> int:
  started = time.time()
  report: dict = {
    "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    "mode": "direct",
    "port": PORT,
    "steps": {},
    "known_limitations": KNOWN_LIMITATIONS,
   }
  failures: list[str] = []
  backend = None

  try:
    # 0) 数据库目标（不打印密码）
    sys.path.insert(0, str(BACKEND_DIR))
    from app.config import settings
    parsed = urlparse(settings.DATABASE_URL)
    db_host = parsed.hostname or "localhost"
    db_port = parsed.port or 3306
    report["database"] = {"host": db_host, "port": db_port,
                          "name": (parsed.path or "").lstrip("/")}

    # 1) docker / mysql
    mysql_step = _ensure_mysql(db_host, db_port)
    report["steps"]["mysql"] = mysql_step
    if not mysql_step.get("ready", True):
      failures.append("mysql not ready")

    # 2) seed + build_index
    report["steps"]["seed"] = _run_script("seed_cases.py", timeout=180)
    report["steps"]["build_index"] = _run_script("build_index.py", timeout=300)
    for key in ("seed", "build_index"):
      if report["steps"][key]["exit"] != 0:
        failures.append(f"{key} exit={report['steps'][key]['exit']}")
    if "--index-twice" in sys.argv:
      report["steps"]["build_index_2"] = _run_script("build_index.py", timeout=300)
      if report["steps"]["build_index_2"]["exit"] != 0:
        failures.append("build_index_2 failed")

    # 3) 启动后端
    log_handle = open(SERVER_LOG, "w", encoding="utf-8")
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    backend = subprocess.Popen(
      [PY, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(PORT)],
      cwd=str(BACKEND_DIR), stdout=log_handle, stderr=subprocess.STDOUT, env=env,
     )

    with httpx.Client(base_url=BASE, timeout=300) as client:
      report["steps"]["health"] = _wait_health(client)
      if not report["steps"]["health"].get("trace_id"):
        failures.append("health missing X-Trace-Id")

      # 注册验收用户
      username = "verify_" + uuid.uuid4().hex[:8]
      reg = client.post("/api/auth/register", json={
        "username": username, "email": username + "@example.com",
        "password": "verify123", "nickname": "verify",
       })
      if reg.status_code != 200:
        raise RuntimeError(f"register failed: {reg.status_code} {reg.text[:200]}")
      token = reg.json()["access_token"]
      headers = {"Authorization": f"Bearer {token}"}

      # 4) 三类请求
      requests_result = {}
      for label, message in [
        ("qa", "离婚财产如何分割？"),
        ("search", "查找类似案例：劳动合同纠纷有哪些相关判例？"),
        ("document", "帮我写一份起诉状，原告张三，被告李四，要求返还借款"),
       ]:
        t0 = time.perf_counter()
        resp = client.post("/api/chat/send", json={"message": message}, headers=headers)
        data = resp.json() if resp.status_code == 200 else {}
        requests_result[label] = {
          "status": resp.status_code,
          "intent": data.get("intent"),
          "response_chars": len(data.get("response") or ""),
          "sources": len(data.get("sources") or []),
          "trace_id": resp.headers.get("X-Trace-Id"),
          "elapsed_s": round(time.perf_counter() - t0, 2),
         }
        if resp.status_code != 200:
          failures.append(f"chat/{label} status={resp.status_code}")
      report["steps"]["chat"] = requests_result

      # 鉴权路由抽查
      cases = client.get("/api/cases/search",
                         params={"q": "离婚财产", "limit": 3}, headers=headers)
      templates = client.get("/api/documents/templates", headers=headers)
      report["steps"]["auth_routes"] = {
        "cases_search_status": cases.status_code,
        "cases_total": (cases.json() or {}).get("total") if cases.status_code == 200 else None,
        "templates_status": templates.status_code,
       }
      if cases.status_code != 200:
        failures.append(f"cases/search status={cases.status_code}")
      if templates.status_code != 200:
        failures.append(f"documents/templates status={templates.status_code}")

      # 5) 两次真实 SSE 录制 + 结构对比
      rec1 = _sse_record(client, token, "劳动合同解除如何补偿？")
      rec2 = _sse_record(client, token, "你好")
      structure_equal = rec1["schema"] == rec2["schema"]
      report["steps"]["sse"] = {
        "recording_1": rec1,
        "recording_2": rec2,
        "structure_equal": structure_equal,
        "compare_note": "只比较帧结构（首帧键/是否有 content/终止帧/JSON/允许键），"
                        "不比较帧数与 content 内容（LLM 输出长度不同）",
       }
      failures += _check_sse_schema(rec1)
      failures += _check_sse_schema(rec2)
      if not structure_equal:
        failures.append("SSE structure differs between two recordings")
      RECORDING_PATH.write_text(
        json.dumps({
          "generated_at": report["generated_at"],
          "recording_1": rec1, "recording_2": rec2,
          "structure_equal": structure_equal,
         }, ensure_ascii=False, indent=2), encoding="utf-8"
       )

    # 6) pytest
    pytest_step = _run([PY, "-m", "pytest", "-q"], BACKEND_DIR, timeout=300)
    passed_match = re.search(r"(\d+) passed", pytest_step["stdout_tail"] or "")
    report["steps"]["pytest"] = {
      "exit": pytest_step["exit"],
      "passed": int(passed_match.group(1)) if passed_match else None,
      "tail": pytest_step["stdout_tail"],
     }
    if pytest_step["exit"] != 0:
      failures.append(f"pytest exit={pytest_step['exit']}")

  except Exception as exc:
    failures.append(f"exception: {type(exc).__name__}: {exc}")
  finally:
    if backend is not None:
      backend.terminate()
      try:
        backend.wait(timeout=15)
      except subprocess.TimeoutExpired:
        backend.kill()

  report["duration_s"] = round(time.time() - started, 2)
  report["result"] = "PASS" if not failures else "FAIL"
  report["failures"] = failures
  REPORT_PATH.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

  summary = {
    "result": report["result"],
    "failures": failures,
    "health": report["steps"].get("health"),
    "chat": {k: {"status": v["status"], "intent": v["intent"]}
             for k, v in report["steps"].get("chat", {}).items()},
    "sse_structure_equal": report["steps"].get("sse", {}).get("structure_equal"),
    "pytest_passed": report["steps"].get("pytest", {}).get("passed"),
    "report": str(REPORT_PATH),
    "duration_s": report["duration_s"],
   }
  print(json.dumps(summary, ensure_ascii=True, indent=2))
  return 0 if report["result"] == "PASS" else 1


if __name__ == "__main__":
  raise SystemExit(main())
