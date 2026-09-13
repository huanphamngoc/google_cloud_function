# sheets-multi-scd2 — nạp nhiều tab, versioning bằng SCD2

Một Cloud Function nạp **toàn bộ các tab** của một Google Spreadsheet vào
BigQuery, mỗi tab một bảng, có **lịch sử thay đổi đầy đủ theo SCD Type 2**.

Thêm tab mới vào file → nhiều nhất là thêm một khối trong `sources.yaml`.
**Không sửa code, không deploy lại.**

```
POST /                        nạp mọi tab được phép
POST /?tabs=Sheet1,Orders     chỉ nạp các tab này
POST /?dry_run=1              đọc, ép kiểu, kiểm tra — không ghi gì
```

---

## Bố cục trong BigQuery

```
core.<bảng>              lịch sử SCD2, chỉ append, không bao giờ truncate
core.v_<bảng>            chỉ bản hiện hành  ← đây là thứ người dùng/BI đọc
core.v_<bảng>_history    mọi phiên bản kèm khoảng hiệu lực
staging.<bảng>__stg      dữ liệu của lần chạy này, tự hết hạn sau 1 ngày
ops.load_runs            mỗi tab mỗi lần chạy một dòng
ops.schema_changes       mỗi cột được thêm hoặc nới kiểu một dòng
```

### Vì sao không có bronze/silver/gold

Bạn yêu cầu bỏ medallion, và với hệ thống này nó thực sự không cần:

- **Tầng bronze tồn tại để giữ dữ liệu thô, phòng khi cần đối chiếu.** Ở đây mỗi
  dòng đã mang sẵn cột `_raw` chứa nguyên văn dòng gốc dạng JSON — kể cả những ô
  nằm ngoài các cột đã khai báo. Bronze chỉ là một bản sao kém hơn của thứ đã có.
- **Tầng silver tồn tại để giữ lịch sử và làm sạch.** SCD2 đã là lịch sử, ở mức
  chi tiết hơn snapshot theo ngày. Việc ép kiểu diễn ra ngay lúc nạp.
- **Tầng gold tồn tại để che chi tiết kỹ thuật khỏi người đọc.** `v_<bảng>` làm
  đúng việc đó với chi phí bằng 0 — view không tốn lưu trữ.

Đổi lại: khi cần bảng tổng hợp (join nhiều nguồn, tính chỉ số), hãy tạo chúng
như những view/bảng riêng đọc từ `v_<bảng>`. Đó là lúc thêm một tầng có ý
nghĩa — không phải ngay từ đầu.

---

## SCD2 hoạt động thế nào

Mỗi bảng mang thêm 8 cột quản trị:

| Cột | Ý nghĩa |
|---|---|
| `_raw` | nguyên văn dòng gốc (JSON), gồm cả ô ngoài cột đã khai báo |
| `_row_hash` | sha256 của phần dữ liệu nghiệp vụ — cơ chế phát hiện thay đổi |
| `_valid_from` | phiên bản này bắt đầu đúng từ lúc nào |
| `_valid_to` | ngừng đúng từ lúc nào (`9999-12-31` khi còn hiện hành) |
| `_is_current` | đúng một dòng TRUE cho mỗi business key |
| `_is_deleted` | TRUE khi key biến mất khỏi nguồn |
| `_load_id` | lần chạy nào tạo ra phiên bản này |
| `_source_tab` | đến từ tab nào |

### Ba câu lệnh, một transaction

```
1. UPDATE  đóng phiên bản có hash khác  → _is_current = FALSE
2. UPDATE  đóng phiên bản có key biến mất → thêm _is_deleted = TRUE
3. INSERT  mở phiên bản mới cho mọi key không còn dòng hiện hành khớp hash
```

Thứ tự bắt buộc: hai lệnh UPDATE chạy trước, nên đến lúc INSERT đánh giá
`NOT EXISTS (... _is_current ...)` thì key vừa thay đổi đã không còn dòng hiện
hành — và được mở phiên bản mới. Key không đổi vẫn còn dòng hiện hành cùng hash
nên bị bỏ qua.

Không dùng một câu `MERGE` duy nhất vì MERGE không thể vừa đóng dòng cũ vừa chèn
dòng mới cho **cùng một key** trong một lượt.

Bọc trong transaction để một lỗi giữa chừng không thể để lại key có **0 dòng hiện
hành** — tình trạng làm dòng đó biến mất khỏi mọi báo cáo cho tới lần chạy sau.

### Vì sao `_valid_to` dùng `9999-12-31` thay vì NULL

Truy vấn tại một thời điểm trở thành `BETWEEN` thuần:

```sql
SELECT * FROM `core.v_customer_history`
WHERE TIMESTAMP '2026-06-30' BETWEEN _valid_from AND _valid_to
```

Nếu dùng NULL, mọi truy vấn đều phải nhớ thêm `OR _valid_to IS NULL` — quên một
lần là mất đúng dòng hiện hành, và không có thông báo lỗi nào.

---

## Truy vấn thường dùng

```sql
-- Dữ liệu hiện tại (dùng cho báo cáo, dashboard)
SELECT * FROM `core.v_customer`;

-- Trạng thái tại một thời điểm trong quá khứ
SELECT * FROM `core.v_customer_history`
WHERE TIMESTAMP '2026-06-30 23:59:59' BETWEEN _valid_from AND _valid_to
  AND NOT _is_deleted;

-- Lịch sử thay đổi của một bản ghi
SELECT _valid_from, _valid_to, _is_current, _is_deleted, name
FROM `core.customer` WHERE id = 1 ORDER BY _valid_from;

-- Những gì đã đổi trong một lần chạy
SELECT * FROM `core.customer` WHERE _load_id = '<load_id>';

-- Lần chạy gần nhất làm gì
SELECT tab, status, rows_inserted, rows_closed, rows_deleted, message
FROM `ops.load_runs` ORDER BY run_ts DESC LIMIT 20;

-- Cột nào mới xuất hiện gần đây
SELECT * FROM `ops.schema_changes` ORDER BY run_ts DESC LIMIT 20;
```

---

## Cấu hình

`sources.yaml`:

```yaml
spreadsheet_id: "..."
dataset: core
discover_tabs: true          # tab chưa khai báo vẫn nạp, ở chế độ snapshot
exclude_tabs: ["^_", "(?i)readme"]

tabs:
  Sheet1:
    table: customer
    mode: scd2
    business_key: [id]       # BẮT BUỘC với scd2
    types:
      ngay_tao: DATE         # ép kiểu mà suy luận cố tình không đoán
    exclude_columns:
      - nguoi_sua_cuoi       # vẫn giữ trong _raw, nhưng không tính vào hash
    on_duplicate_key: block  # hoặc keep_last
```

| Tuỳ chọn | Tác dụng |
|---|---|
| `business_key` | Khoá nghiệp vụ. Nhiều cột = khoá phức hợp |
| `mode: snapshot` | Không versioning, chỉ append snapshot kèm `_load_id` |
| `types` | Ép kiểu khi suy luận trả về STRING |
| `exclude_columns` | Bỏ khỏi bảng và khỏi hash — cột nhiễu không tạo phiên bản mới |
| `on_duplicate_key` | `block` (mặc định) hoặc `keep_last` |
| `header_row` | Khi tiêu đề không nằm ở dòng 1 |

Biến môi trường ghi đè file: `SPREADSHEET_ID`, `BQ_DATASET`,
`BQ_STAGING_DATASET`, `BQ_OPS_DATASET`, `BQ_LOCATION`, `CONFIG_PATH`.

---

## Những chỗ hệ thống **từ chối** chạy, và vì sao

Đây là phần đáng đọc nhất. Mỗi trường hợp dưới đây có một cách "xử lý cho xong"
rất cám dỗ, và cả bốn đều dẫn tới hỏng dữ liệu âm thầm.

| Tình huống | Xử lý | Lý do |
|---|---|---|
| **Key trùng trong một lần nạp** | Chặn tab, báo số dòng cụ thể trong sheet | Giữ dòng cuối tức là tin rằng thứ tự trong sheet phản ánh thời gian — không gì đảm bảo điều đó. Đặt `keep_last` nếu bạn xác nhận là đúng |
| **Key rỗng** | Chặn tab | Key NULL không khớp được ở lần sau → mỗi lần chạy đẻ một phiên bản mới vĩnh viễn |
| **Tab còn tiêu đề nhưng hết dữ liệu** | Chặn tab | "Đã xoá hết" và "ai đó lỡ tay xoá" trông giống hệt nhau. Đóng toàn bộ bản ghi là thao tác không hoàn tác được |
| **Ngày dạng `01/02/2026`** | Giữ nguyên STRING | Không phân biệt được ngày-trước hay tháng-trước. Đoán sai là đảo lộn cả năm dữ liệu. Dùng `types:` khi đã có người xác nhận |
| **Kiểu xung đột không nới được** (STRING ← INT64) | Giữ kiểu cũ, đếm số ô ép kiểu lỗi | Bảng đã STRING sáu tháng không bị thu hẹp vì mẫu tuần này toàn chữ số |
| **Cột biến mất khỏi sheet** | Giữ nguyên cột, chỉ ghi nhận | Xoá cột là phá lịch sử vẫn còn đúng với các phiên bản cũ |

Một tab bị chặn **không làm hỏng các tab khác** — mỗi tab chạy độc lập, kết quả
tổng hợp trả về là `partial` với HTTP 207.

---

## Chạy thử

```bash
# Xem sẽ nạp gì mà không ghi gì
curl -X POST -H "Authorization: Bearer $(gcloud auth print-identity-token)" \
  "${URL}?dry_run=1"

# Nạp thật
curl -X POST -H "Authorization: Bearer $(gcloud auth print-identity-token)" "$URL"
```

Kết quả trả về:

```json
{
  "load_id": "…",
  "status": "partial",
  "tabs": [
    {"tab":"Sheet1","table":"customer","mode":"scd2","status":"ok",
     "rows_read":3,"inserted":1,"closed":1,"deleted":0},
    {"tab":"Orders","table":"orders","mode":"scd2","status":"blocked",
     "rows_read":50,"message":"2 duplicate business key(s): [1001] at rows [5, 17]…"}
  ]
}
```

Mã HTTP phản ánh trạng thái: `200` ok, `207` partial, `500` error — để Scheduler
và Monitoring nhìn thấy lần gọi thất bại, thay vì dấu tích xanh với tin xấu nằm
trong body.

---

## Kiểm thử

```bash
python -m pytest tests/test_scd2_*.py
```

147 test, chạy khoảng 1 giây, **không cần tài khoản cloud**:

| File | Phạm vi |
|---|---|
| `test_scd2_schema.py` | Chuẩn hoá tiêu đề (có tiếng Việt có dấu), suy luận kiểu, ép kiểu, hash |
| `test_scd2_sql.py` | Chính xác câu SQL sinh ra: thứ tự 3 lệnh, transaction, partition/cluster |
| `test_scd2_config.py` | Những cấu hình sai bị từ chối |
| `test_scd2_transform.py` | Dựng bản ghi, phát hiện key trùng/rỗng |
| `test_scd2_pipeline.py` | Điều phối với BigQuery và Sheets giả lập |

### Điều test **chưa** phủ — nói rõ để không hiểu nhầm

Câu SQL merge được **ghi lại, không được thực thi**. Các test khẳng định cấu trúc
và thứ tự câu lệnh, không khẳng định BigQuery chạy ra đúng số dòng. Xác nhận điều
đó cần một dataset thật — đó là việc đầu tiên nên làm sau khi deploy:

```sql
-- Sau lần chạy 1: mỗi key đúng 1 dòng hiện hành
SELECT id, COUNTIF(_is_current) AS n FROM `core.customer`
GROUP BY id HAVING n != 1;   -- phải trả về 0 dòng

-- Sửa 1 ô trong sheet, chạy lại: dòng cũ đóng, dòng mới mở
SELECT _valid_from, _valid_to, _is_current, name
FROM `core.customer` WHERE id = 1 ORDER BY _valid_from;

-- Chạy lại mà không sửa gì: không phiên bản nào mới
SELECT rows_inserted FROM `ops.load_runs`
ORDER BY run_ts DESC LIMIT 1;   -- phải bằng 0
```

Ba truy vấn trên là bộ nghiệm thu SCD2 tối thiểu.

---

## Giới hạn đã biết

- **Suy luận kiểu theo mẫu toàn bộ tab.** Tab rất lớn sẽ tốn bộ nhớ; ngưỡng
  chuyển sang Cloud Run Job nằm ở [decisions §1](../../design/elt-drive-to-bq-decisions.md).
- **Số bắt đầu bằng 0** (số điện thoại, mã sản phẩm) bị suy luận thành INT64 và
  mất số 0 đầu. Dùng `types: { so_dien_thoai: STRING }`. Đã ghi nhận trong test.
- **Tách nghìn chỉ hiểu kiểu Mỹ** (`1,234.56`). Sheet dùng `1.234,56` sẽ ra STRING.
- **Tất cả tab chạy tuần tự trong một lần gọi.** Nhiều tab lớn có thể chạm timeout
  540s — chia bằng `?tabs=` hoặc chuyển sang Cloud Run Job.
- **Chưa có cảnh báo khi fail.** Function trả 500 nhưng chưa ai được báo.

## Quan hệ với `sheet_to_bq_loader`

Function cũ ([sheet_to_bq_loader](../sheet_to_bq_loader/)) nạp **một tab** vào
`bronze.brz_customer` bằng `WRITE_TRUNCATE` — mỗi lần chạy xoá sạch lịch sử.

Package này thay thế nó hoàn toàn. Giữ function cũ chạy song song cho tới khi ba
truy vấn nghiệm thu ở trên cho kết quả đúng, rồi mới xoá:

```bash
gcloud functions delete sheet-to-bq-loader --region=asia-southeast1 --gen2
gcloud scheduler jobs delete sheet-to-bq-loader-schedule --location=asia-southeast1
```
