"""BƯỚC 3b — SINH VIÊN VIẾT. Cutover sang region phụ.

5 bước, THỨ TỰ QUAN TRỌNG (§2 Kiến Trúc Tham Chiếu: DNS/LB, compute, state là 3 lớp riêng):
  1_verify_target    — /v1/state của region phụ: weights? vector count? pool_state?
  2_restore_snapshot — gọi state/snapshot.py get + state/snapshot.py rpo()
                       Log BẮT BUỘC: rpo_seconds, docs_lost, embed_model_version.
                       (§3: "backup index nhưng quên backup embedding model version
                        -> index không tương thích khi restore")
  3_scale_pool       — ghi "full" vào state/region-<t>/pool_state (warm -> full)
  4_wait_ready       — POLL /readyz tới khi 200. Region phụ có WARMUP_SECONDS —
                       đây là GPU pool warm-up của §4, nó nằm trong RTO của bạn.
  5_dns_cutover      — ghi region đích vào edge/active_region

BẪY: nếu bạn đổi edge/active_region TRƯỚC bước 4, user sẽ nhận 503 từ CẢ HAI region
và RTO của bạn dài hơn, không ngắn hơn. Nếu bước 4 timeout -> ABORT, KHÔNG cutover.

Mỗi bước ghi 1 dòng vào reports/failover-events.jsonl với ts + step.
Không có dòng 5_dns_cutover = tools/measure_rto.py không tìm được t_cutover = mất điểm.

Chạy:  python dr/failover.py --target b --backend fs
"""
import argparse
import json
import pathlib
import sys
import time

import httpx

sys.path.insert(0, ".")
from state import snapshot  # noqa: E402

URL = {"a": "http://127.0.0.1:8001", "b": "http://127.0.0.1:8002"}
LOG = pathlib.Path("reports/failover-events.jsonl")


def emit(**kw):
    LOG.parent.mkdir(parents=True, exist_ok=True)
    rec = {"ts": time.time(), "iso": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()), **kw}
    with LOG.open("a") as f:
        f.write(json.dumps(rec) + "\n")
    print("FAILOVER", json.dumps(rec))
    return rec


def state_of(region: str) -> dict:
    """/v1/state của region — không raise, target chết thì trả lỗi thay vì crash cả runbook."""
    try:
        r = httpx.get(f"{URL[region]}/v1/state", timeout=5.0)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        return {"region": region, "error": type(e).__name__}


def failover(target: str, backend: str, wait: float) -> dict:
    t_start = time.time()
    primary = "b" if target == "a" else "a"

    # 1_verify_target
    verify = state_of(target)
    emit(step="1_verify_target", region=target,
         **{k: v for k, v in verify.items() if k != "region"})

    # 2_restore_snapshot
    restore_meta = snapshot.get(target, backend)
    primary_db = pathlib.Path(f"state/region-{primary}/vectors.sqlite")
    restored_db = pathlib.Path(f"state/region-{target}/vectors.sqlite")
    rpo_info = snapshot.rpo(primary_db, restored_db)
    emit(step="2_restore_snapshot", region=target, backend=backend,
         rpo_seconds=rpo_info["rpo_seconds"], docs_lost=rpo_info["docs_lost"],
         embed_model_version=restore_meta.get("embed_model_version"))

    # 3_scale_pool
    pool_file = pathlib.Path(f"state/region-{target}/pool_state")
    pool_file.parent.mkdir(parents=True, exist_ok=True)
    pool_file.write_text("full")
    emit(step="3_scale_pool", region=target, pool_state="full")

    # 4_wait_ready
    t4 = time.time()
    deadline = t4 + wait
    ready = False
    while time.time() < deadline:
        try:
            r = httpx.get(f"{URL[target]}/readyz", timeout=2.0)
            if r.status_code == 200:
                ready = True
                break
        except Exception:
            pass
        time.sleep(0.5)
    waited_s = round(time.time() - t4, 2)
    emit(step="4_wait_ready", region=target, ready=ready, waited_s=waited_s)

    if not ready:
        emit(step="abort", region=target, reason="target_not_ready_after_wait", waited_s=waited_s)
        return {"ok": False, "target": target, "primary": primary,
                "reason": "wait_ready_timeout",
                "rpo_seconds": rpo_info["rpo_seconds"], "docs_lost": rpo_info["docs_lost"]}

    # 5_dns_cutover — chỉ chạy sau khi target đã ready
    pathlib.Path("edge/active_region").write_text(target)
    emit(step="5_dns_cutover", region=target)

    state_after = state_of(target)
    return {"ok": True, "target": target, "primary": primary,
            "rpo_seconds": rpo_info["rpo_seconds"], "docs_lost": rpo_info["docs_lost"],
            "embed_model_version": restore_meta.get("embed_model_version"),
            "waited_s": waited_s, "state_after": state_after,
            "elapsed_s": round(time.time() - t_start, 2)}


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--target", default="b", choices=["a", "b"])
    p.add_argument("--backend", default="fs", choices=["fs", "minio"])
    p.add_argument("--wait", type=float, default=60)
    a = p.parse_args()
    print(json.dumps(failover(a.target, a.backend, a.wait), indent=2))
