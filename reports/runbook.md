# Runbook 1 trang — Region chính down

Runbook phải chạy được lúc 3h sáng bởi người KHÔNG viết nó. Mỗi bước: lệnh copy-paste
được + cách biết bước đó xong.

| # | Bước | Lệnh | Biết là xong khi | Ai làm |
|---|---|---|---|---|
| 1 | Xác nhận outage — đọc `reports/health-events.jsonl` (health checker chạy song song, KHÔNG tin 1 lần fail: cần đủ `threshold=3` lần liên tiếp); nếu health checker chưa chạy thì fallback tự probe `/readyz` 3 lần cách nhau 0.5s | `python chaos/kill_region.py status` (kiểm tra nhanh bằng tay); tự động bên trong `dr/runbook.py` | `confirmed_down:true` trong dòng `step:1, name:xac_nhan_outage` của `reports/runbook-run.jsonl`, `evidence.source` = `reports/health-events.jsonl` hoặc `direct_probe` | on-call |
| 2 | Mở incident + bấm giờ RTO | tự động (bên trong `python dr/runbook.py --primary a --target b --backend fs`) | dòng `step:2_thong_bao_incident` ghi cả `t_outage_ts` (từ `chaos/chaos-events.jsonl`) và `notified_at` vào `reports/runbook-run.jsonl` | on-call |
| 3 | Restore state ở region phụ + scale pool + đợi ready + cutover DNS (1 lệnh, 5 bước con tự động) | `python dr/failover.py --target b --backend fs` (runbook gọi hàm này 1 lần duy nhất, không chạy tay song song) | `reports/failover-events.jsonl` có đủ 5 dòng `1_verify_target` … `5_dns_cutover`, dòng cuối `ok:true` | on-call → SRE lead xác nhận |
| 4 | Verify state replica (đọc lại kết quả bước 3, KHÔNG restore lần 2) | tự động, đọc `state_after` từ dict mà `failover()` trả về | dòng `step:4_verify_state_replica` có `vectors > 0` và `weights:true` trong `reports/runbook-run.jsonl` | on-call |
| 5 | DNS/LB cutover | tự động, đọc lại kết quả bước 3 | `curl localhost:8080/edge/state` cho `active_region=b` (sau khi TTL `EDGE_TTL_SECONDS` hết hạn) | on-call |
| 6 | Verify golden signals | tự động: 10 request thật vào `http://127.0.0.1:8002/v1/infer` | p95 < 500ms, error rate = 0.0 (lần chạy thật đo được: `p95_latency_ms:283.9, error_rate:0.0` — `reports/runbook-run.jsonl:6`) — xem dòng `step:6, name:verify_golden_signals` | on-call |
| 7 | Đo RTO + postmortem | `python tools/measure_rto.py --loadgen reports/drill-2-withdr.jsonl --target-rto 300` | `rto_verdict` != null (PASS/FAIL), số ghi vào `reports/rto-evidence.md` | on-call → viết `reports/postmortem.md` |

**Chạy toàn bộ runbook (bán tự động, hỏi xác nhận y/N trước khi cutover):**
```bash
python dr/runbook.py --primary a --target b --backend fs
```
Chấm điểm / CI dùng `--auto` để bỏ qua bước hỏi xác nhận:
```bash
python dr/runbook.py --primary a --target b --backend fs --auto
```

**Rollback (failover ngược):** chỉ trigger khi Region A đã `restore` xong VÀ đã đứng
vững ≥ 15 phút liên tục ở `/readyz` 200 (tránh flapping — §4 Anti-Patterns: full-auto
không có circuit breaker sẽ làm 2 region đá qua đá lại traffic liên tục). Người quyết
định là SRE lead trực ca, không phải on-call một mình — rollback cũng đi qua đúng quy
trình `dr/failover.py --target a --backend fs` (không tự tay sửa `edge/active_region`).
