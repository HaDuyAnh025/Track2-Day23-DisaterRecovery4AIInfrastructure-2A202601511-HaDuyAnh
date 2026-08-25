# Postmortem — DR Drill Lab 23

Theo đúng template §4 "Sau Failover: Blameless Postmortem". Blameless: câu hỏi là
"hệ thống/process nào cho phép chuyện này", không phải "ai làm sai".

## 1. Timeline (mọi dòng phải có evidence path:line)

| ISO time | Sự kiện | Evidence |
|---|---|---|
| 2026-08-25T04:59:33 | outage bắt đầu (`chaos kill_region.py --region a --mode netblock --mock`) | `chaos/chaos-events.jsonl:3` |
| 2026-08-25T04:59:33 (+0.1s) | user đầu tiên bị ảnh hưởng — request đầu tiên trả 503/ReadTimeout | `reports/drill-2-withdr.jsonl:25` |
| 2026-08-25T04:59:54 (+21.0s) | health check alert — `region:a, to:UNHEALTHY, consecutive_fails:3` | `reports/health-events.jsonl:2` |
| 2026-08-25T04:59:54 (+21.7s) | operator (runbook `--auto`) confirm outage, bắt đầu cutover — đọc lại chính xác dòng health-events ở trên | `reports/runbook-run.jsonl:1` |
| 2026-08-25T05:00:03 (+30.2s) | DNS/LB cutover — `edge/active_region` ghi `"b"` | `reports/failover-events.jsonl:5` |
| 2026-08-25T05:00:04 (+31.2s) | resolved — request đầu tiên OK từ region phụ (`served_by:"b"`) | `reports/drill-2-withdr.jsonl:39` |

## 2. RTO/RPO đo được vs mục tiêu — gap ở bước nào?

- RTO mục tiêu: 300s · đo được: `31.2s` · gap (margin còn lại): `268.8s` — **PASS thoải mái**, còn nhiều dư địa trước khi chạm ngưỡng 300s.
- RPO mục tiêu: 300s · đo được: `12.0s` (`6` doc bị mất) · gap (margin còn lại): `288.0s` — **PASS**.
- **Bước tốn nhiều giây nhất:** `health-check detection` (21.0s / 31.2s tổng, ~67%) — vì sao? `interval_s=5 × threshold=3 = 15.0s` là sàn cấu trúc (`reports/health-events.jsonl:2`), nhưng outage rơi giữa một chu kỳ poll (health checker khởi động trước outage, không đồng bộ pha), nên phải chờ gần trọn thêm 1 chu kỳ mới đủ 3 lần fail liên tiếp — thực đo 21.0s thay vì 15.0s lý thuyết. Đây cũng là bước duy nhất trong RTO breakdown mà scale được bằng cấu hình (`interval`) mà không cần đổi kiến trúc.

## 3. Root cause (5 whys)

Không phải "vì tôi chạy chaos script". Câu hỏi: *nếu đây là outage thật, bước nào trong runbook của tôi sẽ thất bại?*

1. **Vì sao user thấy lỗi?** → Region A là region duy nhất được `edge/active_region` trỏ tới; không có traffic splitting hay standby nóng nào phục vụ song song.
2. **Vì sao phải mất 21s mới biết?** → Health checker chỉ coi là outage sau `threshold=3` lần fail liên tiếp cách nhau `interval=5s` — đây là đánh đổi có chủ đích để chống flapping (§4 Anti-Patterns), không phải lỗi.
3. **Vì sao mất thêm ~2.4s sau khi biết mới bắt đầu warm-up?** → `dr/runbook.py` phải tự xác nhận lại qua `reports/health-events.jsonl` (tránh cutover trước khi có bằng chứng — bài học từ chính bug đã sửa trong session này: cutover sớm hơn health checker sẽ bị `tools/measure_rto.py` gắn cờ `t_cutover < t_detect` và đánh dấu drill INVALID), rồi mới gọi `state/snapshot.py get` + ghi `pool_state=full`.
4. **Vì sao mất thêm 6.8s ở bước warm-up?** → Region B khởi động ở trạng thái `cold/warm`, chỉ được chuyển `full` LÚC failover (đúng theo `serving/app.py::pool_state()` — lần đọc đầu tiên lúc boot không tính, chỉ transition lúc runtime mới tính là GPU pool warm-up thật). Đây là chi phí cấu trúc của việc không giữ pool phụ luôn nóng.
5. **Vì sao nếu outage thật kéo dài, docs_lost có thể lớn hơn 6?** → `state/replicate.py` chạy mỗi 30s; nếu ingest rate cao hơn hoặc outage xảy ra ngay sau một chu kỳ replicate vừa xong, cửa sổ mất dữ liệu (RPO) tiệm cận tới gần hết chu kỳ 30s, không phải hằng số 12s đo được lần này.

## 4. Action items (có owner + deadline)

| # | Action | Owner | Deadline | Giảm RTO/RPO bao nhiêu giây |
|---|---|---|---|---|
| 1 | Hạ `interval` health-check từ 5s → 2s (giữ `threshold=3`, sàn còn 6s thay vì 15s) | SRE lead | 2026-09-08 | ~ -9s RTO (đổi lại tăng rủi ro flapping — cần theo dõi số lần transition/giờ sau khi đổi) |
| 2 | Giữ pool region B luôn ở `warm` sẵn (pre-warm liên tục) thay vì chỉ scale lúc failover | Platform team | 2026-09-15 | ~ -6.8s RTO (bỏ hẳn bước GPU pool warm-up khỏi đường găng) |
| 3 | Hạ chu kỳ `state/replicate.py --every` từ 30s → 10s | Data team | 2026-09-08 | RPO trần lý thuyết giảm từ ~30s → ~10s (docs_lost kỳ vọng giảm tương ứng) |

## 5. Ba câu hỏi bắt buộc trả lời

1. **`interval × threshold` = 5s × 3 = 15.0s.** Đo thực tế detection mất 21.0s/31.2s tổng RTO — chiếm **~67%** RTO, là thành phần lớn nhất.
2. **Nếu hạ `interval` xuống 1s:** sàn detect floor còn `1×3=3s` (giảm được tối đa ~12s lý thuyết so với sàn 15s hiện tại). Cái giá phải trả (§4 flapping): health checker gọi `/readyz` dày hơn 5x, tăng tải lên cả hai region liên tục; quan trọng hơn, một đợt tắc nghẽn mạng/GC ngắn (không phải outage thật) dễ tạo đủ 3 lần fail liên tiếp trong 3s hơn nhiều so với 15s, làm tăng false-positive failover (2 region đá qua đá lại traffic — đúng anti-pattern §4 cảnh báo).
3. **Nếu outage kéo dài 6 giờ và region chính mất dữ liệu vĩnh viễn:** `docs_lost` không còn là "6 document trong 12 giây" nữa — nó trở thành *toàn bộ* dữ liệu ingest được từ lần `replicate.py put` cuối cùng trước khi mất vĩnh viễn cho tới thời điểm mất, tức có thể là hàng giờ dữ liệu khách hàng (hóa đơn, giao dịch, hội thoại...) biến mất không thể khôi phục — với khách hàng, đây không còn là một con số kỹ thuật mà là dữ liệu thật của họ không bao giờ lấy lại được, kéo theo nghĩa vụ thông báo vi phạm dữ liệu (breach notification) tuỳ ngành.
