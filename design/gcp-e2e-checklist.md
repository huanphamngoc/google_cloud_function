# Cần chuẩn bị gì để dựng một hệ thống ELT end-to-end trên GCP

Checklist chuẩn bị **hạ tầng, tài khoản, công cụ và quy trình** — những thứ phải có
sẵn trước khi gõ dòng lệnh đầu tiên, và những thứ dễ quên nhất.

Tài liệu này **không** lặp lại phần kiến thức — phần đó ở
[`knowledge-prep.md`](knowledge-prep.md). Ở đây là *tài nguyên và thao tác*, không
phải *kỹ năng*.

| Tài liệu | Trả lời câu hỏi |
|---|---|
| [`elt-drive-to-bq.md`](elt-drive-to-bq.md) | Hệ thống trông như thế nào? |
| [`elt-drive-to-bq-decisions.md`](elt-drive-to-bq-decisions.md) | Ở mỗi bước chọn gì, căn cứ số nào? |
| [`knowledge-prep.md`](knowledge-prep.md) | Cần biết những gì? |
| **Tài liệu này** | **Cần chuẩn bị sẵn những gì?** |

> Cột **Đã có?** phản ánh trạng thái project `elt-drive-to-bq` tính đến 2026-09-11.
> ✅ đã xong · ⚠️ có nhưng chưa đủ/chưa xác minh · ❌ chưa có

---

## 0. Bảng tổng — 9 nhóm chuẩn bị

| # | Nhóm | Thời gian | Đã có? |
|---|---|---|---|
| 1 | Tài khoản & thanh toán | 30 phút | ✅ |
| 2 | Cấu trúc project & chọn region | 30 phút | ✅ |
| 3 | Kiểm tra ràng buộc tổ chức (org policy) | 15 phút | ⚠️ chưa kiểm tra |
| 4 | Bật API | 5 phút | ✅ |
| 5 | Danh tính & phân quyền | 1 giờ | ⚠️ quá rộng |
| 6 | Quyền truy cập nguồn dữ liệu | 15 phút/nguồn | ✅ |
| 7 | Công cụ trên máy local | 1 giờ | ✅ |
| 8 | Kho code & quản lý hạ tầng | 1 giờ | ❌ chưa có git |
| 9 | Hàng rào an toàn trước khi chạy thật | 1 giờ | ❌ |

Tổng thời gian nếu làm từ đầu: **khoảng nửa ngày** cho một môi trường sạch.

---

## 1. Tài khoản và thanh toán

### Cần có

| Thứ | Ghi chú |
|---|---|
| Tài khoản Google | Cá nhân (`@gmail.com`) hoặc Workspace đều được — xem mục 3 về khác biệt |
| Billing account đã liên kết với project | **Không có billing thì không bật được hầu hết API**, kể cả khi dùng trong free tier |
| Thẻ thanh toán hợp lệ | GCP giữ tạm ~1 USD để xác thực |

### Free tier — biết để không hoảng khi thấy hoá đơn

| Dịch vụ | Miễn phí hằng tháng |
|---|---|
| BigQuery — truy vấn | 1 TB byte quét đầu tiên |
| BigQuery — lưu trữ | 10 GB đầu tiên |
| Cloud Functions | 2 triệu lần gọi |
| Cloud Run | 180.000 vCPU-giây |
| Cloud Scheduler | 3 job |
| Cloud Build | 120 phút build/ngày |

Hệ thống ở quy mô hiện tại (1 sheet, 1 lần chạy/ngày) **nằm trọn trong free tier** —
chi phí thực tế gần 0 USD. Đừng để điều đó thành lý do bỏ qua mục 9.

### Kiểm tra

```bash
gcloud beta billing projects describe elt-drive-to-bq \
  --format="value(billingEnabled,billingAccountName)"
```

Kết quả phải là `True` và một mã billing account. Nếu `False`, mọi thứ phía sau sẽ
lỗi với thông báo khó hiểu về API chưa bật.

---

## 2. Cấu trúc project và chọn region

### Quyết định số lượng project

| Tình huống | Số project |
|---|---|
| Học / thử nghiệm / 1 người | 1 project, tách bằng tiền tố dataset (`bronze_dev` / `bronze`) |
| Có người dùng thật phụ thuộc dữ liệu | 2 project: `<tên>-dev`, `<tên>-prod` |
| Công ty, nhiều team | 3 project + folder trong organization |

Ngưỡng tách chi tiết ở [`elt-drive-to-bq-decisions.md` §12](elt-drive-to-bq-decisions.md).

### Chọn region — quyết định một lần, khó đổi

Đây là thứ **phải nghĩ kỹ trước khi tạo dataset đầu tiên**. BigQuery **không cho
đổi location của dataset** sau khi tạo; muốn đổi phải tạo dataset mới và copy toàn bộ.

| Thành phần | Yêu cầu về location |
|---|---|
| BigQuery dataset | Chọn 1 lần, cố định vĩnh viễn |
| Cloud Function / Cloud Run | Nên cùng region với dataset (giảm độ trễ, tránh phí egress) |
| Cloud Scheduler | Phải ở region Scheduler hỗ trợ — không phải region nào cũng có |
| GCS bucket (nếu dùng) | Phải **cùng location** với dataset thì mới load được trực tiếp |

Tiêu chí chọn: **gần người dùng nhất**, không phải rẻ nhất. Chênh lệch giá giữa các
region nhỏ hơn nhiều so với chi phí sửa sai về sau.

Project này dùng `asia-southeast1` (Singapore) — hợp lý cho người dùng ở Việt Nam.

```bash
# Kiểm tra location thật của dataset đang có
bq show --format=prettyjson elt-drive-to-bq:bronze | grep -i location
```

### Quy ước đặt tên — thống nhất từ đầu

| Đối tượng | Quy ước | Ví dụ |
|---|---|---|
| Project | `<sản phẩm>-<môi trường>` | `elt-drive-to-bq` |
| Dataset | Tên tầng | `bronze`, `silver`, `gold`, `ops` |
| Bảng | `<tiền tố tầng>_<thực thể>` | `brz_customer`, `slv_customer`, `gld_customer_daily` |
| Service account | `<vai trò>-<hệ thống>` | `sheets-to-bq-loader` |
| Cột kỹ thuật | Tiền tố `_` | `_load_id`, `_ingested_at`, `_raw` |

Đổi tên bảng sau khi có báo cáo trỏ vào là việc tốn thời gian và dễ vỡ. Thống nhất
trước rẻ hơn nhiều.

---

## 3. Kiểm tra ràng buộc tổ chức — bước hay bị bỏ sót nhất

Nếu dùng **tài khoản Google Workspace của công ty**, admin có thể đã bật các org
policy chặn đúng những thứ hệ thống này cần. Phát hiện sớm tiết kiệm nhiều giờ debug.

| Ràng buộc | Hậu quả nếu bị bật | Cách xử lý |
|---|---|---|
| **Domain restricted sharing** | **Không share được Google Sheet với service account** → hệ thống không chạy được | Nhờ admin whitelist, hoặc chuyển sheet sang Shared Drive mà SA được thêm vào |
| `iam.disableServiceAccountKeyCreation` | Không tạo được key JSON | Không ảnh hưởng — thiết kế này dùng ADC, không dùng key. Đây là policy **tốt** |
| `iam.allowedPolicyMemberDomains` | Không cấp quyền cho danh tính ngoài domain | Thường không ảnh hưởng, SA cùng project là nội bộ |
| `gcp.resourceLocations` | Không tạo được tài nguyên ở region đã chọn | Chọn region khác trong danh sách được phép |
| `compute.requireShieldedVm` | Có thể ảnh hưởng Cloud Build | Hiếm, nhưng gây lỗi build khó hiểu |

```bash
# Liệt kê org policy đang áp lên project
gcloud resource-manager org-policies list --project=elt-drive-to-bq
```

**Với tài khoản cá nhân (`@gmail.com`)** thì không có organization, không có policy
nào — bỏ qua mục này. Đổi lại cũng không có Shared Drive, nên sheet phải thuộc sở
hữu của chính tài khoản đó.

---

## 4. Bật API

Bật tất cả một lần từ đầu — rẻ hơn nhiều so với bật lẻ mỗi khi gặp lỗi:

```bash
gcloud services enable \
  cloudfunctions.googleapis.com \
  run.googleapis.com \
  cloudbuild.googleapis.com \
  artifactregistry.googleapis.com \
  sheets.googleapis.com \
  drive.googleapis.com \
  bigquery.googleapis.com \
  bigquerydatatransfer.googleapis.com \
  cloudscheduler.googleapis.com \
  workflows.googleapis.com \
  dataform.googleapis.com \
  secretmanager.googleapis.com \
  monitoring.googleapis.com \
  logging.googleapis.com \
  iam.googleapis.com \
  cloudresourcemanager.googleapis.com
```

| API | Vì sao cần | Bắt buộc? |
|---|---|---|
| `cloudfunctions` + `run` | gen2 chạy trên Cloud Run bên dưới — **cần cả hai** | Có |
| `cloudbuild` + `artifactregistry` | Đóng gói container khi deploy | Có |
| `sheets` | Đọc Google Sheet | Có |
| `drive` | Liệt kê/theo dõi file trên Drive | Khi mở rộng |
| `bigquery` | Kho dữ liệu | Có |
| `cloudscheduler` | Chạy theo lịch | Có |
| `workflows` | Điều phối nhiều bước | Từ nguồn thứ 3 |
| `dataform` | Biến đổi SQL có quản lý phụ thuộc | Từ bảng dẫn xuất thứ 3 |
| `secretmanager` | Lưu bí mật khi có nguồn cần API key | Khi cần |
| `monitoring` + `logging` | Cảnh báo và log | Có |

Bật API **không tốn tiền** — chỉ dùng mới tốn. Không có lý do tiết kiệm ở bước này.

---

## 5. Danh tính và phân quyền

### Bốn danh tính riêng biệt — nhầm lẫn giữa chúng là nguồn lỗi số 1

| Danh tính | Là ai | Cần quyền gì |
|---|---|---|
| **Bạn** (người deploy) | Tài khoản Google cá nhân | `roles/owner` hoặc `editor` + `cloudfunctions.developer` |
| **SA build** | `{NUMBER}-compute@developer...` hoặc `{NUMBER}@cloudbuild...` | `cloudbuild.builds.builder`, `artifactregistry.writer`, `logging.logWriter`, `storage.objectViewer` |
| **SA runtime** | `sheets-to-bq-loader@...` | `bigquery.dataEditor`, `bigquery.jobUser` + **quyền Drive trên sheet** |
| **SA gọi job** | Cloud Scheduler dùng lại SA runtime | `run.invoker` trên chính function |

**Ba điều dễ sai nhất** — rút ra từ [`deployment-log.md`](../functions/sheet_to_bq_loader/deployment-log.md):

1. **SA build ≠ SA runtime.** Build xong không có nghĩa là được phép deploy. Xác
   định SA build thực sự: Console → Cloud Build → Triggers → mở trigger → "Service account".
2. **BigQuery tách "ghi dữ liệu" và "chạy job".** `dataEditor` cho ghi vào bảng,
   `jobUser` cho tạo job thực hiện việc ghi. Thiếu một trong hai đều 403 nhưng
   thông báo khác nhau — đọc kỹ tên permission bị từ chối.
3. **Deploy function chạy AS một SA khác cần `iam.serviceAccountUser` trên chính SA
   đó** (quyền "actAs") — cấp trên **resource SA**, không phải mức project.

### Nguyên tắc không thoả hiệp

**Không tạo, không tải, không commit file key JSON.** Cloud Functions/Run gắn danh
tính lúc chạy, code xác thực qua ADC. Key JSON là bí mật tồn tại vĩnh viễn, không
xoay vòng, rò rỉ là mất quyền — trong kiến trúc này không có chỗ nào cần đến nó.

```bash
# Kiểm tra: lệnh này nên trả về rỗng
gcloud iam service-accounts keys list \
  --iam-account=sheets-to-bq-loader@elt-drive-to-bq.iam.gserviceaccount.com \
  --managed-by=user
```

**Trạng thái hiện tại:** `dataEditor` đang ở **mức project** thay vì mức dataset.
Lý do đã ghi nhận (`bq add-iam-policy-binding` bị chặn allowlisting). Thu hẹp qua
Console → dataset → Sharing → Permissions.

---

## 6. Quyền truy cập nguồn dữ liệu

Bước này **không script được** và là bước hay quên nhất khi thêm nguồn mới.

| Loại nguồn | Cách cấp quyền |
|---|---|
| Google Sheet | Mở sheet → Share → thêm **email service account** làm Viewer |
| File trên Shared Drive | Thêm SA làm thành viên của Shared Drive |
| File trên GCS | IAM thường: `storage.objectViewer` trên bucket |
| Database | Kết nối mạng + credential trong Secret Manager |

**Điểm mấu chốt cần nhớ:** quyền đọc Google Sheet là **Drive sharing, không phải
GCP IAM**. Không có role GCP nào cấp được điều này. Service account đúng, IAM đúng
hoàn toàn, nhưng chưa share sheet thì vẫn 403.

### Checklist cho mỗi nguồn mới

- [ ] Ai sở hữu file? (nếu người đó nghỉ việc, file có mất không?)
- [ ] Đã share với SA chưa?
- [ ] Tên tab chính xác là gì? (locale tiếng Việt mặc định là `Trang tính1`, không
      phải `Sheet1` — đã từng gây lỗi `Unable to parse range`)
- [ ] Dòng đầu có phải header không? Có dòng trống/merge cell phía trên không?
- [ ] Ai được phép sửa cấu trúc cột?

---

## 7. Công cụ trên máy local

| Công cụ | Bắt buộc | Kiểm tra |
|---|---|---|
| Google Cloud SDK (`gcloud`, `bq`, `gsutil`) | Có | `gcloud version` |
| Python 3.12 | Có (khớp runtime function) | `python --version` |
| Git | Có | `git --version` |
| VS Code + extension Python, Draw.io Integration | Nên có | — |
| Terraform | Khi hạ tầng > 20 tài nguyên | `terraform version` |
| `jq` | Tiện xử lý output JSON | `jq --version` |

### Thiết lập lần đầu

```bash
gcloud auth login                 # đăng nhập cho lệnh gcloud
gcloud auth application-default login   # cho code chạy local dùng ADC
gcloud config set project elt-drive-to-bq
gcloud config set functions/region asia-southeast1
```

**Hai lệnh `auth` là hai thứ khác nhau** — `gcloud auth login` cấp quyền cho CLI;
`application-default login` cấp cho *thư viện client Python* chạy trên máy bạn. Test
code local mà quên lệnh thứ hai sẽ báo lỗi thiếu credential dù `gcloud` vẫn chạy tốt.

### Lưu ý cho Windows

Trên Windows, `deploy.sh` cần Git Bash hoặc WSL. **Cloud Shell** (nút terminal trên
Console) là lựa chọn không cần cài gì — `gcloud` và `bq` có sẵn, đã đăng nhập sẵn.

Một bẫy đã gặp: trong shell tương tác, bash diễn giải `!` kể cả trong nháy kép —
`SHEET_RANGE="Sheet1!A1:B"` dán trực tiếp vào terminal sẽ lỗi `event not found`. Chạy
qua `bash deploy.sh` thì không sao (shell không tương tác tắt history expansion).

---

## 8. Kho code và quản lý hạ tầng

### Cấu trúc thư mục

```
<repo>/
├── functions/<tên-function>/
│   ├── main.py              ← buildpack Python BẮT BUỘC tên này
│   ├── requirements.txt
│   ├── deploy.sh
│   └── cloudbuild.yaml
├── design/                  ← tài liệu thiết kế
├── sql/ hoặc dataform/      ← logic biến đổi
├── tests/
└── README.md
```

**`main.py` phải đúng tên đó.** Lệnh `gcloud functions deploy` không có tham số chỉ
định file vì buildpack quy ước sẵn: nó tìm `main.py` trong thư mục `--source`, rồi
tìm hàm tên `--entry-point` bên trong.

### Quản lý hạ tầng: script hay Terraform?

| Số tài nguyên | Cách làm | Lý do |
|---|---|---|
| < 20 | Script `gcloud` như [`deploy.sh`](../functions/sheet_to_bq_loader/deploy.sh) | Đọc hiểu ngay, không thêm công cụ |
| 20–100 | Terraform | Bắt đầu cần biết "hiện đang có gì" |
| > 100, nhiều môi trường | Terraform + module | Bắt buộc |

Điều quan trọng hơn việc chọn công cụ: **hạ tầng phải tái tạo được từ file trong
repo**, không phải từ trí nhớ về những nút đã bấm trên Console. `deploy.sh` đã đáp
ứng điều đó cho quy mô hiện tại.

### Việc nên làm ngay bất kể quy mô

Project này **chưa có git repo** (`git status` → `fatal: not a git repository`).
Version control giải quyết vấn đề độc lập với CI/CD — lịch sử thay đổi và khả năng
khôi phục khi sửa hỏng. Không có ngưỡng nào để chờ.

```bash
git init && git add . && git commit -m "Initial commit"
```

Kèm `.gitignore` tối thiểu:

```
__pycache__/
*.pyc
.venv/
*.json          # chặn vô tình commit key service account
.env
```

---

## 9. Hàng rào an toàn trước khi chạy thật

Bốn thứ này rẻ, nhanh, và là khác biệt giữa "một script chạy được" với "một hệ thống
vận hành được".

| Hàng rào | Thiết lập | Thời gian |
|---|---|---|
| **Budget alert** | Billing → Budgets → ngưỡng 50/90/100% | 10 phút |
| **Cảnh báo job fail** | Logging → Create log-based alert, `severity>=ERROR` | 30 phút |
| **Giới hạn byte truy vấn** | `maximum_bytes_billed` trong QueryJobConfig | 5 phút |
| **Thời hạn lưu log** | Logging → Retention (mặc định 30 ngày) | 5 phút |

Ngưỡng chi tiết cho từng cái ở
[`elt-drive-to-bq-decisions.md` §9–10](elt-drive-to-bq-decisions.md).

**Budget alert nên đặt ngay khi tạo project**, lúc chi phí còn bằng 0 — vì đó chính
là lúc không ai nghĩ đến nó, và nó tồn tại để bảo vệ khỏi sự cố bất ngờ chứ không
phải để theo dõi chi phí đã biết.

---

## 10. Thứ tự thực hiện cho một hệ thống mới

```
NGÀY 0 — nền móng (nửa ngày)
  1. Tạo project, liên kết billing, đặt budget alert
  2. Kiểm tra org policy (mục 3) — LÀM TRƯỚC, không phải sau khi debug
  3. Chọn region, tạo dataset bronze/silver/gold/ops
  4. Bật toàn bộ API
  5. git init + push lên remote

NGÀY 1 — một nguồn chạy được đầu đến cuối (1 ngày)
  6. Tạo SA runtime + cấp quyền BigQuery
  7. Share nguồn dữ liệu với SA
  8. Viết function nạp, deploy thủ công, chạy tay 1 lần
  9. Xác nhận dữ liệu đã vào bảng
 10. Đặt lịch Scheduler

NGÀY 2 — làm nó đáng tin (nửa ngày)
 11. Cảnh báo khi fail + khi dữ liệu cũ
 12. Bảng ops ghi lại mỗi lần chạy
 13. Bronze append-only + phân vùng theo ngày

SAU ĐÓ — chỉ khi chạm ngưỡng (xem elt-drive-to-bq-decisions.md)
 14. Nguồn thứ 2  → tách schema ra config
 15. Bảng thứ 3   → Dataform
 16. Nguồn thứ 3  → Workflows
 17. Người thứ 2  → CI/CD
```

**Nguyên tắc xuyên suốt:** đi *một nguồn* từ đầu đến cuối trước, rồi mới mở rộng
ngang. Dựng sẵn framework cho 10 nguồn trước khi có nguồn nào chạy được là cách
chắc chắn nhất để xây nhầm thứ.

---

## 11. Mười thứ dễ quên nhất

Rút từ quá trình debug thực tế của chính project này:

| # | Thứ bị quên | Triệu chứng |
|---|---|---|
| 1 | Share sheet với service account | 403 từ Sheets API dù IAM đúng hoàn toàn |
| 2 | Tên tab thật của sheet | `Unable to parse range: Sheet1!A1:B` |
| 3 | Cấp quyền cho SA **build** (khác SA runtime) | Build fail "missing permission on the build service account" |
| 4 | `bigquery.jobUser` (chỉ cấp `dataEditor`) | 403 `bigquery.jobs.create denied` |
| 5 | Bật cả `run.googleapis.com` cho function gen2 | Deploy lỗi khó hiểu |
| 6 | Location dataset khớp region function | Lỗi cross-region khi load |
| 7 | File phải tên `main.py` | Deploy xong nhưng không tìm thấy entry point |
| 8 | `application-default login` khi test local | Thiếu credential dù `gcloud` chạy tốt |
| 9 | `run.invoker` cho Scheduler | Lịch chạy nhưng nhận 403 |
| 10 | Kiểm tra log **mới**, không phải log cũ | Debug nhầm vào lỗi đã sửa xong |

Điểm số 10 đáng nhấn mạnh: luôn đối chiếu `execution_id` và timestamp trước khi kết
luận. Đã từng mất thời gian vì đọc lại log của lần chạy cũ.

---

## 12. Trạng thái project hiện tại

| Nhóm chuẩn bị | Trạng thái | Việc còn lại |
|---|---|---|
| 1. Tài khoản & billing | ✅ | Đặt budget alert |
| 2. Project & region | ✅ | `asia-southeast1`, nhất quán |
| 3. Org policy | ⚠️ | Chưa kiểm tra (tài khoản cá nhân → có thể bỏ qua) |
| 4. API | ✅ | Bổ sung `monitoring`, `dataform` khi cần |
| 5. IAM | ⚠️ | Thu hẹp `dataEditor` về mức dataset |
| 6. Nguồn dữ liệu | ✅ | Sheet đã share, tab đã đổi tên đúng |
| 7. Công cụ local | ✅ | — |
| 8. Repo & hạ tầng | ❌ | **`git init` — làm ngay** |
| 9. Hàng rào an toàn | ❌ | **Cảnh báo fail + budget alert — làm ngay** |

Ba việc gạch đầu dòng đậm cộng lại mất khoảng **2 giờ** và là khoảng cách còn lại
giữa hiện trạng với một hệ thống đủ điều kiện gọi là vận hành được.
