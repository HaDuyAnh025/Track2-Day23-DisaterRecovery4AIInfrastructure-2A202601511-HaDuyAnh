# RTO/RPO Evidence — Lab 23

Quy tắc duy nhất: mỗi con số ở đây phải trỏ được về **một dòng log thật**
(`đường/dẫn.jsonl:số_dòng`). `pytest tests/test_rto_evidence.py` mở từng file ra kiểm tra.
Mọi số dưới đây lấy từ đúng một cặp drill chạy trong cùng một phiên (WSL Ubuntu, bare mode,
`--mock`), không trộn số từ các lần chạy khác nhau.

## 1. Drill 1 — không có DR (baseline)

| Chỉ số | Giá trị | Cách đo | Evidence |
|---|---|---|---|
| t_outage | `2026-08-25T04:58:24` | chaos kill | `chaos/chaos-events.jsonl:1` |
| Request fail đầu tiên | `+0.2s` | dòng `ok:false` đầu tiên sau t_outage (`ts=1787633904.4558523`, outage `ts=1787633904.296125`) | `reports/drill-1-nodr.jsonl:17` |
| Request thành công sau đó | không có | 13 dòng `ok:false` liên tiếp còn lại, không có dòng `ok:true` nào sau t_outage trong suốt phần còn lại của loadgen (30 dòng, kết thúc `ts≈1787633916`) | `reports/drill-1-nodr.jsonl:18-30` |
| Số request fail | `14` | `tools/measure_rto.py --loadgen reports/drill-1-nodr.jsonl --target-rto 300` → `requests_failed:14` | `reports/drill-1-nodr.jsonl:17-30` |
| RTO | `NO_RECOVERY` | `tools/measure_rto.py` → `"rto_verdict":"NO_RECOVERY"`, `"recovered_by_region":null` | `reports/drill-1-nodr.jsonl` (toàn bộ) |

## 2. Drill 2 — có DR

| Mốc | +giây từ t_outage | Cách đo | Evidence |
|---|---|---|---|
| t_outage (mốc 0) | 0 | `action:kill`, `ts=1787633973.2186396` | `chaos/chaos-events.jsonl:3` |
| User thấy lỗi đầu tiên | +0.1s | dòng `ok:false` đầu, `ts=1787633973.348899` | `reports/drill-2-withdr.jsonl:25` |
| Health check phát hiện | +21.0s | `to:UNHEALTHY, region:a, consecutive_fails:3`, `ts=1787633994.2561693` | `reports/health-events.jsonl:2` |
| Snapshot restore xong | +23.4s | `step:2_restore_snapshot`, `ts=1787633996.59484` | `reports/failover-events.jsonl:2` |
| Region phụ ready | +30.2s | `step:4_wait_ready, ready:true, waited_s:6.79`, `ts=1787634003.4071882` | `reports/failover-events.jsonl:4` |
| DNS cutover | +30.2s | `step:5_dns_cutover`, `ts=1787634003.416369` | `reports/failover-events.jsonl:5` |
| **RTO đo được** | **+31.2s** | dòng `ok:true, served_by:"b"` đầu sau lỗi, `ts=1787634004.3820791` | `reports/drill-2-withdr.jsonl:39` |

| Chỉ số | Đo được | Mục tiêu (slide §1) | Verdict |
|---|---|---|---|
| RTO — Inference API | `31.2s` | 300s (5 phút) | **PASS** |
| RPO — Vector DB | `12.0s` / `6` doc | 300s (5 phút) | **PASS** |

Nguồn số: `python tools/measure_rto.py --loadgen reports/drill-2-withdr.jsonl --target-rto 300`
→ `"valid":true, "warnings":[], "rto_measured_s":31.2, "rto_verdict":"PASS", "rpo_at_restore_s":12.0, "docs_lost":6`.
RPO/docs_lost lấy từ `2_restore_snapshot` — `reports/failover-events.jsonl:2`.

## 3. RTO của tôi gồm những gì

| Thành phần | Giây | Nó đến từ đâu | Giảm được bằng cách nào |
|---|---|---|---|
| Health-check detect floor | `21.0s` | `interval_s=5.0 × threshold=3 = 15.0s` là sàn cấu trúc (`reports/health-events.jsonl:2`); đo thực tế `21.0s` vì lần outage rơi giữa một chu kỳ poll (health checker khởi động ~12s trước outage, không đồng bộ pha với thời điểm kill), nên phải chờ thêm gần 1 chu kỳ trước khi đủ 3 lần fail liên tiếp | Hạ `interval` xuống 2-3s để chu kỳ ngắn hơn, đổi lại chấp nhận rủi ro flapping cao hơn (§4) |
| Snapshot restore | `2.4s` | Từ lúc phát hiện (`+21.0s`) đến khi `3_scale_pool` ghi xong (`+23.4s`) — gồm bước `1_verify_target` + `2_restore_snapshot` (copy `vectors.sqlite` + `model.bin` từ `state/_replica/`) + `3_scale_pool`. Chi tiết: `reports/failover-events.jsonl:1-3` | Snapshot nhỏ (SQLite + 1 file weight) nên đã nhanh; với vector DB lớn hơn cần restore song song / incremental để giữ con số này thấp |
| GPU pool warm-up | `6.8s` | `waited_s:6.79` ở `4_wait_ready` — `reports/failover-events.jsonl:4`, khớp `WARMUP_SECONDS=6` cấu hình trong `scripts/up_bare.sh` | Giữ pool region phụ ở trạng thái `warm` sẵn (thay vì `cold`) để warm-up runtime ngắn hơn — đánh đổi chi phí giữ pool ấm liên tục |
| DNS/LB TTL cache | `1.0s` | Từ `5_dns_cutover` (`+30.2s`) đến request thành công đầu tiên (`+31.2s`) — `reports/drill-2-withdr.jsonl:39` so với `reports/failover-events.jsonl:5` | Hạ `EDGE_TTL_SECONDS` (đang =5s) xuống thấp hơn để cache DNS hết hạn nhanh hơn, đổi lại tăng tải đọc `edge/active_region` mỗi request |

**Tổng:** 21.0 + 2.4 + 6.8 + 1.0 = **31.2s**, khớp `rto_measured_s` đo được ở trên.
