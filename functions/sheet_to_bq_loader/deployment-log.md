# Deployment log — sheet-to-bq-loader trên elt-drive-to-bq

Ghi lại toàn bộ quá trình deploy thực tế: đã làm gì, gặp lỗi gì, nguyên nhân và cách
fix — để tái sử dụng khi deploy sang project khác hoặc khi function này cần sửa lại.

| | |
|---|---|
| **Project** | `elt-drive-to-bq` (project number `722092646648`) |
| **Function** | `sheet-to-bq-loader`, region `asia-southeast1`, gen2, `python312` |
| **Nguồn** | Google Sheet `1ClGSnw5kawxRCr43qNUJr4IdiWe3ItH28tBq6CVgcfk`, tab `Sheet1`, range `A1:B` |
| **Đích** | `elt-drive-to-bq:bronze.brz_customer` (cột: `id INTEGER, name STRING, _load_id STRING, _ingested_at TIMESTAMP`) |
| **Service account chạy function** | `sheets-to-bq-loader@elt-drive-to-bq.iam.gserviceaccount.com` |
| **Kết quả cuối** | Chạy thành công thủ công (`rows_loaded: 1`), đang setup lịch tự động qua Cloud Scheduler |

---

## Những gì đã build

- `main.py` — đọc 1 tab Google Sheet, ép kiểu cột `id` (INTEGER) và `name` (STRING), bỏ qua (và log warning) dòng thiếu giá trị bắt buộc hoặc sai kiểu, ghi `_load_id`/`_ingested_at` vào mỗi dòng, load vào BigQuery bằng `WRITE_TRUNCATE` (mỗi lần chạy ghi đè toàn bộ bảng).
- `requirements.txt` — `functions-framework`, `google-cloud-bigquery`, `google-api-python-client`, `google-auth`.
- `deploy.sh` — toàn bộ lệnh `gcloud`/`bq` theo thứ tự, đã cập nhật theo mọi lỗi thực tế bên dưới.
- Auth: dùng Application Default Credentials qua service account gắn ở `--run-service-account`, không có key file nào được tạo/tải về.

---

## Các lỗi gặp phải, theo thứ tự thời gian

### 1. Script không chạy được khi thử từ máy local

**Hiện tượng:** `deploy.sh` "làm terminal break" khi chạy từ máy Windows.

**Nguyên nhân thật:** không xác định được chính xác vì thiếu log lỗi cụ thể — nghi vấn hàng đầu là chạy nhầm trong PowerShell (không hiểu cú pháp bash) thay vì Git Bash/WSL.

**Fix:** chuyển hẳn sang làm việc trong **Cloud Shell** — môi trường Linux có sẵn, đã auth sẵn với đúng project.

---

### 2. Cloud Shell là máy riêng biệt — file local không tự động có ở đó

**Hiện tượng:** `bash functions/sheet_to_bq_loader/cmd: No such file or directory` — trong khi file này tồn tại trên máy local.

**Nguyên nhân:** Cloud Shell không đồng bộ gì với IDE/máy local. `ls` trong Cloud Shell chỉ thấy đúng những gì được tạo/upload thủ công vào đó.

**Fix:** tạo lại `main.py` và `requirements.txt` trực tiếp trong Cloud Shell bằng `cat > file << 'EOF' ... EOF`, đảm bảo đúng nội dung mới nhất.

---

### 3. Build fail — thiếu quyền trên Cloud Build service account (lần 1)

**Lỗi:**
```
OperationError: code=3, message=Build failed with status: FAILURE.
Could not build the function due to a missing permission on the build service account.
```

**Nguyên nhân:** từ ~04/2023, Google không tự cấp quyền Editor cho Cloud Build service account (`{PROJECT_NUMBER}@cloudbuild.gserviceaccount.com`) trên project mới nữa — phải cấp tay `roles/cloudbuild.builds.builder`.

**Fix:**
```bash
PROJECT_NUMBER=$(gcloud projects describe "$PROJECT_ID" --format='value(projectNumber)')
gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:${PROJECT_NUMBER}@cloudbuild.gserviceaccount.com" \
  --role="roles/cloudbuild.builds.builder" --condition=None
gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:${PROJECT_NUMBER}@cloudbuild.gserviceaccount.com" \
  --role="roles/artifactregistry.writer" --condition=None
```

---

### 4. Build fail — thiếu quyền trên Compute default service account (lần 2)

**Lỗi:** giống hệt lỗi #3, nhưng gcloud lần này tự phát hiện và hỏi:
```
The default build service account [722092646648-compute@developer.gserviceaccount.com]
is missing the [roles/cloudbuild.builds.builder] role.
Would you like to continue? (y/N)?
```

**Nguyên nhân:** project này thực tế build bằng **Compute Engine default service account**
(`{PROJECT_NUMBER}-compute@developer.gserviceaccount.com`), không phải Cloud Build SA
riêng ở #3. Trả lời `y` chỉ tự cấp đúng 1 role đó — build vẫn fail tiếp vì còn thiếu
quyền push image lên Artifact Registry và ghi log.

**Fix:**
```bash
COMPUTE_SA="${PROJECT_NUMBER}-compute@developer.gserviceaccount.com"
gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:${COMPUTE_SA}" --role="roles/cloudbuild.builds.builder" --condition=None
gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:${COMPUTE_SA}" --role="roles/artifactregistry.writer" --condition=None
gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:${COMPUTE_SA}" --role="roles/logging.logWriter" --condition=None
gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:${COMPUTE_SA}" --role="roles/storage.objectViewer" --condition=None
```

**Bài học:** khi build fail vì thiếu quyền, luôn kiểm tra gcloud có gợi ý đúng tên SA
không — dự án khác nhau có thể dùng SA build khác nhau.

---

### 5. Bash history expansion nuốt mất `!` trong `SHEET_RANGE`

**Lỗi:** `-bash: !A1: event not found`

**Nguyên nhân:** `SHEET_RANGE=Sheet1!A1:B` được đặt trong dấu **nháy kép** (`"..."`)
khi paste trực tiếp vào **interactive shell**. Bash coi `!` là ký tự đặc biệt (history
expansion), và nháy kép không chặn được nó — chỉ nháy đơn (`'...'`) mới chặn được.

**Lưu ý quan trọng:** chạy qua `bash deploy.sh` (non-interactive) thì KHÔNG bị lỗi này,
vì history expansion mặc định tắt ở shell không tương tác. Lỗi chỉ xảy ra khi paste
lệnh trực tiếp vào prompt.

**Fix:** luôn dùng nháy đơn quanh toàn bộ giá trị `--set-env-vars` khi gõ/paste trực
tiếp vào Cloud Shell:
```bash
--set-env-vars='SPREADSHEET_ID=...,SHEET_RANGE=Sheet1!A1:B,BQ_DATASET=...,BQ_TABLE=...'
```

---

### 6. Sheets API 400 — sai tên tab

**Lỗi:**
```
HttpError 400 ... "Unable to parse range: Sheet1!A1:B"
```

**Nguyên nhân:** tab đầu tiên trong sheet không thực sự tên "Sheet1" — Google Sheets
đặt tên tab mặc định theo ngôn ngữ tài khoản (tiếng Việt thường ra "Trang tính1").

**Fix:** đổi tên tab trong sheet thành đúng "Sheet1" để khớp với `SHEET_RANGE` đã
deploy (thay vì sửa code/env var — đơn giản hơn cho trường hợp này).

**Bài học:** trước khi deploy, luôn xác nhận tên tab thật bằng cách nhìn trực tiếp
trong UI (góc dưới trái) hoặc gọi API:
```bash
curl -s -H "Authorization: Bearer $(gcloud auth print-access-token)" \
  "https://sheets.googleapis.com/v4/spreadsheets/<ID>?fields=sheets.properties.title"
```

---

### 7. BigQuery 403 — thiếu `bigquery.jobs.create`

**Lỗi:**
```
403 ... User does not have bigquery.jobs.create permission in project elt-drive-to-bq
```

**Nguyên nhân:** `roles/bigquery.dataEditor` (dù đã gán) chỉ cho phép đọc/ghi **data**
trong dataset, KHÔNG bao gồm quyền tạo **job** (query job/load job). Đây là 2 khái
niệm tách biệt trong BigQuery IAM. Quyền tạo job phải gán ở cấp **project**.

**Fix:**
```bash
gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:${SA_EMAIL}" --role="roles/bigquery.jobUser" --condition=None
```

---

### 8. BigQuery 403 — thiếu `bigquery.tables.create`

**Lỗi:**
```
403 ... Permission bigquery.tables.create denied on dataset elt-drive-to-bq:bronze
```

**Nguyên nhân:** service account chưa thực sự có `dataEditor` trên dataset `bronze` —
bước gán quyền này (trong `deploy.sh` gốc) chưa được chạy khi làm từng lệnh lẻ.

**Fix (lần thử đầu, KHÔNG thành công):**
```bash
bq add-iam-policy-binding --member="serviceAccount:${SA_EMAIL}" \
  --role="roles/bigquery.dataEditor" "${PROJECT_ID}:${BQ_DATASET}"
```
→ Lỗi: `BigQuery error in add-iam-policy-binding operation: This feature requires allowlisting.`
(giới hạn của `bq` CLI cho IAM binding cấp dataset, không phải lỗi thao tác.)

**Fix thực sự dùng:** gán `dataEditor` ở cấp **project** qua Resource Manager IAM API
(không bị giới hạn allowlist như lệnh `bq` ở trên):
```bash
gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:${SA_EMAIL}" --role="roles/bigquery.dataEditor" --condition=None
```

**Đánh đổi:** rộng hơn dự định ban đầu (dataEditor toàn project thay vì chỉ dataset
`bronze`). Muốn siết lại phạm vi: vào Console → BigQuery → dataset `bronze` → Sharing
→ Permissions (UI không bị giới hạn allowlist như `bq` CLI).

---

## Sau bước 8: chạy thành công

```json
{"load_id":"ecd13d53a87b48128a45db4592d61ae2","rows_loaded":1,"status":"ok"}
```

Xác nhận bằng:
```bash
bq query --use_legacy_sql=false \
  "SELECT * FROM \`elt-drive-to-bq.bronze.brz_customer\` LIMIT 10"
```

---

## Trạng thái IAM hiện tại (đã xác nhận qua `gcloud projects get-iam-policy`)

| Service account | Roles đã gán |
|---|---|
| `sheets-to-bq-loader@elt-drive-to-bq.iam.gserviceaccount.com` | `roles/bigquery.jobUser`, `roles/bigquery.dataEditor` (project-level) |
| `722092646648@cloudbuild.gserviceaccount.com` | `roles/cloudbuild.builds.builder`, `roles/artifactregistry.writer` |
| `722092646648-compute@developer.gserviceaccount.com` | `roles/cloudbuild.builds.builder`, `roles/artifactregistry.writer`, `roles/logging.logWriter`, `roles/storage.objectViewer` |

## Cấu hình function hiện tại

```
name:            sheet-to-bq-loader
region:          asia-southeast1
runtime:         python312 (gen2)
entry-point:     load_sheet_to_bq
trigger:         HTTP, --no-allow-unauthenticated
run-service-account: sheets-to-bq-loader@elt-drive-to-bq.iam.gserviceaccount.com
env vars:
  SPREADSHEET_ID = 1ClGSnw5kawxRCr43qNUJr4IdiWe3ItH28tBq6CVgcfk
  SHEET_RANGE    = Sheet1!A1:B
  BQ_DATASET     = bronze
  BQ_TABLE       = brz_customer
```

---

## Việc còn lại (chưa xác nhận hoàn tất)

- [ ] `gcloud functions add-invoker-policy-binding` — cho phép Cloud Scheduler gọi function
- [ ] `gcloud scheduler jobs create http` — tạo lịch chạy tự động
- [ ] `gcloud scheduler jobs run` — test lịch chạy qua Scheduler (khác cơ chế auth với curl thủ công ở trên)
- [ ] Monitoring/alerting khi function fail — hiện tại nếu lỗi thì chỉ log lại, chưa có ai được báo
- [ ] Sheet hiện có 1 dòng test (`id=1, name=...`) — cân nhắc dọn/thay bằng data thật trước khi đưa vào dùng chính thức
- [ ] Quyết định có cần giữ lịch sử data không — hiện tại `WRITE_TRUNCATE` nghĩa là không có lịch sử, mỗi lần chạy ghi đè hoàn toàn

---

## Bài học tổng quát cho lần deploy sau (project khác, function khác)

1. **Luôn chạy trong môi trường Linux thật** (Cloud Shell/WSL/Git Bash) — không chạy script bash trong PowerShell.
2. **Xác nhận tên tab Google Sheet thật** trước khi set `SHEET_RANGE`, đừng đoán theo tên mặc định tiếng Anh.
3. **Dùng nháy đơn** cho mọi giá trị `--set-env-vars`/flag có chứa `!` khi paste trực tiếp vào interactive shell.
4. **Build service account và runtime service account là 2 identity khác nhau** — build fail không có nghĩa là code sai, mà là quyền build thiếu; kiểm tra đúng SA gcloud báo lỗi (có thể là `@cloudbuild.gserviceaccount.com` hoặc `-compute@developer.gserviceaccount.com` tùy project).
5. **BigQuery có 3 tầng quyền khác nhau, hay bị nhầm lẫn:** đọc/ghi data (`dataEditor`), tạo job (`jobUser`, cấp project), tạo bảng (nằm trong `dataEditor` nhưng phải gán đúng chỗ). Thiếu 1 trong 3 là fail, và mỗi cái lỗi 1 kiểu message khác nhau.
6. **`bq` CLI có giới hạn allowlist ở 1 số lệnh** (`add-iam-policy-binding` cấp dataset) mà `gcloud`/Console không bị — khi gặp "requires allowlisting", chuyển sang cách khác thay vì cố tìm cách bật allowlist.
