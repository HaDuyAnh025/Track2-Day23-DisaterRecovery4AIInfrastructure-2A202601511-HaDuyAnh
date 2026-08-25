"""BƯỚC 3c — SINH VIÊN VIẾT. Tự động hoá runbook §4 "Runbook: Region Chính Down".

7 bước trên slide, mỗi bước 1 dòng log có ts. Log này CHÍNH LÀ timeline của postmortem.
  1 xac_nhan_outage          — probe cả 2 region, đừng tin 1 lần fail (dùng nhiều lần
                              hoặc gọi health_checker.probe nếu đã viết xong 3a)
  2 thong_bao_incident       — ts của dòng này là mốc "operator biết tin", LUÔN LUÔN
                              SAU t_outage trong chaos-events (không thể trùng — operator
                              không thể biết ngay giây outage xảy ra). Ghi cả 2 ts vào
                              log để postmortem tính được "độ trễ thông báo".
  3 scale_gpu_pool           — gọi HÀM `failover.failover(...)` MỘT LẦN DUY NHẤT. Hàm
                              đó tự làm đủ 5 bước con (verify/restore/scale/wait/cutover)
                              và tự ghi log riêng vào reports/failover-events.jsonl.
  4 verify_state_replica     — KHÔNG gọi lại failover — chỉ ĐỌC kết quả (vector count +
                              weights ở region phụ) từ dict mà bước 3 trả về, để log vào
                              runbook-run.jsonl cho postmortem đọc 1 chỗ duy nhất.
  5 dns_cutover              — cũng chỉ đọc lại: kết quả cutover có ok hay không.
  6 verify_golden_signals    — 10 request thật vào region phụ: p95 latency + error rate
  7 post_incident            — elapsed_s + lệnh đo RTO

BÁN TỰ ĐỘNG, KHÔNG FULL-AUTO (§4: "failover đầu tiên nên là bán tự động — alert +
1-click confirm — tránh flapping gây failover 2 chiều liên tục"). Mặc định phải hỏi
người vận hành confirm; --auto chỉ dùng trong CI/khi chấm điểm.

Chạy:  python dr/runbook.py --primary a --target b --backend fs
"""
import argparse
import json
import pathlib
import sys
import time

import httpx

sys.path.insert(0, ".")
from dr import failover as fo  # noqa: E402
from dr import health_checker as hc  # noqa: E402

LOG = pathlib.Path("reports/runbook-run.jsonl")
CHAOS_EVENTS = pathlib.Path("chaos/chaos-events.jsonl")
URL = {"a": "http://127.0.0.1:8001", "b": "http://127.0.0.1:8002"}


def step(n, name, **kw):
    LOG.parent.mkdir(parents=True, exist_ok=True)
    rec = {"ts": time.time(), "iso": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()),
           "step": n, "name": name, **kw}
    with LOG.open("a") as f:
        f.write(json.dumps(rec) + "\n")
    print("RUNBOOK", json.dumps(rec))
    return rec


def confirm(auto: bool, msg: str) -> bool:
    if auto:
        return True
    ans = input(f"{msg} [y/N] ").strip().lower()
    return ans in ("y", "yes")


def _last_outage_ts(region: str):
    if not CHAOS_EVENTS.exists():
        return None
    kills = [json.loads(l) for l in CHAOS_EVENTS.read_text().splitlines() if l.strip()]
    kills = [e for e in kills if e.get("action") == "kill" and e.get("region") == region]
    return kills[-1]["ts"] if kills else None


def _wait_for_health_unhealthy(region: str, since_ts: float, poll_timeout: float = 30.0,
                                poll_interval: float = 1.0):
    """Cho dr/health_checker.py (chạy song song) đủ thời gian tự phát hiện outage.

    Đọc reports/health-events.jsonl thay vì tự probe nhanh riêng — nếu không, runbook
    sẽ cutover TRƯỚC khi health checker kịp ghi UNHEALTHY, và
    tools/measure_rto.py sẽ cảnh báo "t_cutover < t_detect" (drill bị đánh dấu INVALID).
    """
    hc_log = pathlib.Path("reports/health-events.jsonl")
    deadline = time.time() + poll_timeout
    while time.time() < deadline:
        if hc_log.exists():
            for line in hc_log.read_text().splitlines():
                if not line.strip():
                    continue
                try:
                    e = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if (e.get("event") == "state_change" and e.get("region") == region
                        and e.get("to") == "UNHEALTHY" and e.get("ts", 0) >= since_ts):
                    return e
        time.sleep(poll_interval)
    return None


def _confirm_outage(region: str, t_outage, tries: int = 3, interval: float = 0.5):
    """Ưu tiên đọc xác nhận từ dr/health_checker.py (nếu đang chạy song song — Bước 4).

    Không tin 1 lần fail: nếu không có health checker song song (t_outage=None hoặc
    không thấy log trong thời gian chờ), fallback về tự probe nhiều lần liên tiếp.
    """
    if t_outage is not None:
        ev = _wait_for_health_unhealthy(region, since_ts=t_outage)
        if ev is not None:
            return True, {"source": "reports/health-events.jsonl", "event": ev}
    probes = []
    for _ in range(tries):
        ready, reason = hc.probe(region, timeout=2.0)
        probes.append({"ready": ready, "reason": reason})
        time.sleep(interval)
    confirmed = all(not p["ready"] for p in probes)
    return confirmed, {"source": "direct_probe", "probes": probes}


def run(primary: str, target: str, backend: str, auto: bool) -> dict:
    t_start = time.time()
    t_outage = _last_outage_ts(primary)

    # 1: xac_nhan_outage — khong tin 1 lan fail
    confirmed, evidence = _confirm_outage(primary, t_outage)
    step(1, "xac_nhan_outage", primary=primary, confirmed_down=confirmed, evidence=evidence)
    if not confirmed:
        step(7, "post_incident", ok=False, reason="outage_khong_xac_nhan_duoc",
             elapsed_s=round(time.time() - t_start, 2))
        return {"ok": False, "reason": "outage_khong_xac_nhan_duoc"}

    # 2: thong_bao_incident — bam gio RTO, ghi ca t_outage va luc operator biet tin
    notified_at = time.time()
    step(2, "thong_bao_incident", primary=primary, target=target, t_outage_ts=t_outage,
         notified_at=notified_at,
         notify_delay_s=None if t_outage is None else round(notified_at - t_outage, 2))

    if not confirm(auto, f"Xac nhan outage region {primary}, failover sang {target}?"):
        step(2, "aborted_by_operator", primary=primary, target=target)
        return {"ok": False, "reason": "operator_declined"}

    # 3: scale_gpu_pool — goi HAM failover.failover(...) DUNG MOT LAN
    fo_result = fo.failover(target, backend, wait=60)
    step(3, "scale_gpu_pool", target=target, ok=fo_result.get("ok"))

    if not fo_result.get("ok"):
        step(7, "post_incident", ok=False, reason=fo_result.get("reason"),
             elapsed_s=round(time.time() - t_start, 2))
        return {"ok": False, "reason": fo_result.get("reason"), "failover": fo_result}

    # 4: verify_state_replica — CHI doc lai ket qua tu buoc 3, khong goi lai failover
    state_after = fo_result.get("state_after", {})
    step(4, "verify_state_replica", target=target,
         vectors=state_after.get("count"), weights=state_after.get("weights"),
         pool_state=state_after.get("pool_state"),
         rpo_seconds=fo_result.get("rpo_seconds"), docs_lost=fo_result.get("docs_lost"))

    # 5: dns_cutover — cung chi doc lai
    step(5, "dns_cutover", target=target, ok=fo_result.get("ok"))

    # 6: verify_golden_signals — 10 request that vao region phu
    latencies_ms, errors = [], 0
    for i in range(10):
        t0 = time.time()
        try:
            r = httpx.get(f"{URL[target]}/v1/infer", params={"q": "hoa don thang 7"}, timeout=5.0)
            ok = r.status_code == 200 and r.json().get("error") is None
        except Exception:
            ok = False
        latencies_ms.append((time.time() - t0) * 1000)
        if not ok:
            errors += 1
    latencies_ms.sort()
    p95 = latencies_ms[max(0, int(len(latencies_ms) * 0.95) - 1)] if latencies_ms else None
    step(6, "verify_golden_signals", target=target, num_requests=len(latencies_ms),
         p95_latency_ms=None if p95 is None else round(p95, 1),
         error_rate=round(errors / len(latencies_ms), 2) if latencies_ms else None)

    # 7: post_incident
    elapsed = round(time.time() - t_start, 2)
    step(7, "post_incident", ok=True, elapsed_s=elapsed,
         measure_cmd="python tools/measure_rto.py --loadgen reports/drill-2-withdr.jsonl "
                     "--target-rto 300")

    return {"ok": True, "elapsed_s": elapsed, "failover": fo_result}


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--primary", default="a")
    p.add_argument("--target", default="b")
    p.add_argument("--backend", default="fs", choices=["fs", "minio"])
    p.add_argument("--auto", action="store_true")
    a = p.parse_args()
    print(json.dumps(run(a.primary, a.target, a.backend, a.auto), indent=2))
