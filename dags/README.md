# Cloud Composer — điều phối luồng Sheets → BigQuery → SCD2

> ## ⚠️ Cảnh báo chi phí
> Composer tính tiền **theo thời gian environment tồn tại, không theo số lần chạy**.
> Environment nhỏ nhất khoảng **300–500 USD/tháng** kể cả khi không chạy DAG nào.
> Phần còn lại của hệ thống này gần 0 USD.
>
> **Tạm dừng DAG không dừng được tiền. Chỉ xoá environment mới dừng.**
> Lệnh xoá ở cuối file. Nếu dựng để học Airflow, hãy xoá ngay trong ngày.

---

## Vì sao dùng Airflow ở đây

Function đã tự nạp được mọi tab trong một lần gọi. Thứ nó **không** cho được là
khả năng nhìn từng tab: một lời gọi HTTP chỉ là một chấm xanh hoặc đỏ, và một tab
bị chặn vì trùng khoá trông y hệt một lỗi mạng thoáng qua.

DAG này tách thành **một task cho mỗi tab**, nên Airflow UI chỉ rõ sheet nào hỏng,
retry đúng sheet đó, và để phần còn lại chạy tiếp.

Quan trọng hơn, nó tách ba thứ hỏng vì ba lý do khác nhau:

| Task | Hỏng nghĩa là | Xử lý |
|---|---|---|
| `preflight` | **Dữ liệu sai** — trùng khoá, khoá rỗng | Fail ngay, không retry |
| `load_<tab>` | **Máy móc hỏng** — lỗi API, timeout, IAM | Retry 2 lần, cách 5 phút |
| `verify_<tab>` | **Kết quả sai** — một khoá có 2 dòng hiện hành | Fail ngay, không retry |

Sự phân biệt giữa *fail ngay* và *retry* là phần đáng giá nhất. Trùng khoá là lỗi
bảng tính — retry không sửa được, mà 3 lần retry cách nhau 5 phút làm chậm việc
báo cho người ta nửa tiếng.

---

## Sơ đồ DAG

```
start → preflight ─┬→ load_customer → verify_customer ─┐
                   ├→ load_orders   → verify_orders   ─┼→ report → finish
                   └→ load_notes  ───────────────────  ┘
```

`preflight` chạy dry-run trước toàn bộ: đọc, ép kiểu, kiểm tra mọi tab mà **không
ghi gì**. Nhờ vậy trùng khoá lộ ra trước khi bảng nào bị động tới. Không có nó,
tab 1 nạp xong, tab 7 bị từ chối, và lần chạy kết thúc ở trạng thái nửa vời —
đúng về mặt kỹ thuật với SCD2, nhưng khó giải thích vào sáng hôm sau.

`verify_<tab>` kiểm tra bất biến SCD2 **sau khi nạp**: mỗi khoá nghiệp vụ đúng một
dòng `_is_current`. Đây là thứ kiểm tra *kết quả* chứ không phải *ý định* — nếu
merge có lúc nào hồi quy, DAG đỏ thay vì dashboard âm thầm nhân đôi số liệu.

`report` chạy với `ALL_DONE` chứ không phải `ALL_SUCCESS`: bản tóm tắt có giá trị
nhất đúng vào lúc có thứ gì đó hỏng.

---

## Cài đặt

```bash
bash dags/composer-setup.sh
```

9 bước, script tự dừng ở chỗ cần xác nhận. Mất **20–30 phút** ở bước tạo
environment.

Những gì script làm khác với deploy function:

- Tạo service account **riêng** `composer-runner` — Composer cần *gọi* function và
  *đọc* BigQuery, không cần ghi. Dùng chung danh tính với function là trao cho bộ
  điều phối quyền ghi mà nó không bao giờ dùng.
- `run.invoker` cấp **trên đúng function đó**, không phải toàn project.
- Chọn image version mới nhất đang có thay vì ghim cứng một phiên bản có thể đã
  ngừng cung cấp ở region này.

### Cấu hình tab

DAG đọc `sources.yaml` — **cùng file** function dùng. Script tự copy từ
`functions/sheets_multi_scd2/` sang thư mục DAG, nên không có chuyện hai bên khai
báo lệch nhau.

Thêm tab → sửa file đó → upload lại:

```bash
gcloud composer environments storage dags import \
  --environment=etl-orchestrator --location=asia-southeast1 \
  --source=dags/sources.yaml
```

DAG tự sinh task mới ở lần parse sau, không cần sửa code.

---

## Vận hành

```bash
ENV=etl-orchestrator; LOC=asia-southeast1

# Chạy tay
gcloud composer environments run $ENV --location=$LOC dags trigger -- sheets_to_bq_scd2

# Xem các lần chạy
gcloud composer environments run $ENV --location=$LOC dags list-runs -- -d sheets_to_bq_scd2

# Mở UI
gcloud composer environments describe $ENV --location=$LOC --format="value(config.airflowUri)"
```

Lịch mặc định: `0 6 * * 1-5`, múi giờ `Asia/Ho_Chi_Minh`.

---

## Kiểm thử

```bash
python -m pytest tests/test_scd2_dag_helpers.py
```

25 test, **không cần cài Airflow**. Phần logic điều phối nằm trong
[`scd2_dag_helpers.py`](scd2_dag_helpers.py) tách khỏi file DAG chính vì lý do này
— quyết định *retry hay fail hay skip* là thứ đáng test nhất và không liên quan gì
tới API của Airflow.

Một ca test đáng chú ý: dry-run báo **mọi tab là `skipped`** theo thiết kế. Nếu
`preflight` coi đó là skip thật, nó sẽ tự đánh dấu skipped và **kéo theo toàn bộ
task phía sau**. Tham số `allow_skip=False` xử lý đúng ca này, và có test riêng.

### Chưa test được ở đây

DAG chưa từng chạy trên Airflow thật. Test phủ phần logic thuần; phần còn lại —
DAG parse được, task id hợp lệ, OIDC token lấy được từ metadata server — chỉ xác
nhận được sau khi upload lên environment.

---

## Xoá khi xong

```bash
gcloud composer environments delete etl-orchestrator --location=asia-southeast1

# Bucket không tự xoá theo environment
gsutil ls | grep composer
gsutil -m rm -r gs://<bucket-vua-liet-ke>
```

Sau khi xoá, luồng vẫn chạy được bằng Cloud Scheduler gọi thẳng function (đã cấu
hình sẵn trong `functions/sheets_multi_scd2/deploy.sh` bước 8) — mất phần nhìn
theo từng tab, nhưng chi phí về lại gần 0.
