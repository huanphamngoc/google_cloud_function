# CI/CD — push lên GitHub là tự deploy Cloud Function

Repo: `https://github.com/huanphamngoc/google_cloud_function.git`

> **Đây là quy trình CI/CD đang dùng.** Bản GitHub Actions từng được dựng xong
> nhưng đã tạm dừng vì vấn đề chi phí — xem [GITHUB-ACTIONS.md](GITHUB-ACTIONS.md)
> nếu muốn bật lại sau này.

Mục tiêu: `git push` lên nhánh `main` → Cloud Build tự chạy test → deploy →
smoke test. Không còn gõ `gcloud functions deploy` bằng tay.

```
git push main → GitHub → Cloud Build trigger → cloudbuild.yaml:
                                                 1. chạy 352 test
                                                 2. kiểm tra sources.yaml
                                                 3. gcloud functions deploy
                                                 4. smoke test (dry run)
```

Repo có **hai function**, mỗi function một trigger riêng. `--included-files`
đảm bảo sửa function này không deploy lại function kia:

| Function | Thư mục | Cloud Build config |
|---|---|---|
| `sheet-to-bq-loader` | `functions/sheet_to_bq_loader/` | [cloudbuild.yaml](functions/sheet_to_bq_loader/cloudbuild.yaml) |
| `sheets-multi-scd2` | `functions/sheets_multi_scd2/` | [cloudbuild.yaml](functions/sheets_multi_scd2/cloudbuild.yaml) |

---

## Bước 1 — Đẩy code lên GitHub

`.gitignore` đã tạo sẵn ở thư mục gốc (chặn `*.json` để không bao giờ lỡ commit
file key service account).

```bash
cd /e/Job/gcp_big_data_26272

git init
git add .
git status          # XEM KỸ trước khi commit - không được có file .json nào

git commit -m "ELT Sheets to BigQuery: loaders, SCD2, Composer DAG, docs"
git branch -M main
git remote add origin https://github.com/huanphamngoc/google_cloud_function.git
git push -u origin main
```

> **Dừng lại ở `git status`.** Đây là lần duy nhất bạn nhìn thấy toàn bộ danh sách
> file trước khi chúng lên internet. Một file key service account lọt lên GitHub
> public là phải xoá service account và tạo lại, không phải chỉ xoá commit.

---

## Bước 2 — Kết nối GitHub với Cloud Build

**Không script được bước này** — giống việc share Google Sheet với service
account, phải thao tác tay:

**Console → Cloud Build → Triggers → Connect Repository → GitHub (Cloud Build GitHub App) → Authorize → chọn `huanphamngoc/google_cloud_function`**

Chọn **"Connect repository"**, rồi bấm **"Skip for now"** khi nó hỏi tạo trigger —
trigger sẽ tạo bằng lệnh ở bước 4 để có `--included-files`.

---

## Bước 3 — Cấp quyền cho Cloud Build

Đây là **lớp quyền mới**, khác với quyền build đã cấp trong `deployment-log.md`.
Build được không có nghĩa là được phép deploy.

```bash
PROJECT_ID="elt-drive-to-bq"
REGION="asia-southeast1"
RUNTIME_SA="sheets-to-bq-loader@${PROJECT_ID}.iam.gserviceaccount.com"
PROJECT_NUMBER=$(gcloud projects describe "$PROJECT_ID" --format='value(projectNumber)')

# Project này build bằng COMPUTE default SA, không phải Cloud Build SA riêng
# (đã xác nhận trong deployment-log.md). Cấp cho cả hai để khỏi phải quay lại.
for BUILD_SA in "${PROJECT_NUMBER}@cloudbuild.gserviceaccount.com" \
                "${PROJECT_NUMBER}-compute@developer.gserviceaccount.com"; do

  # 1. Được phép gọi gcloud functions deploy, và gán IAM cho Cloud Run service
  #    bên dưới. --no-allow-unauthenticated cần quyền run.services.setIamPolicy,
  #    thứ mà cloudfunctions.developer KHÔNG bao gồm — thiếu nó thì build xong,
  #    service xong, rồi hỏng ở bước cuối.
  for ROLE in roles/cloudfunctions.developer roles/run.admin; do
    gcloud projects add-iam-policy-binding "$PROJECT_ID" \
      --member="serviceAccount:${BUILD_SA}" \
      --role="$ROLE" --condition=None
  done

  # 2. Được phép deploy một function CHẠY AS service account khác ("actAs").
  #    Cấp TRÊN RESOURCE service account, không phải mức project.
  gcloud iam service-accounts add-iam-policy-binding "$RUNTIME_SA" \
    --member="serviceAccount:${BUILD_SA}" \
    --role="roles/iam.serviceAccountUser"

  # 3. Được phép gọi function ở bước smoke test.
  gcloud functions add-invoker-policy-binding sheets-multi-scd2 \
    --region="$REGION" \
    --member="serviceAccount:${BUILD_SA}" 2>/dev/null || true
done
```

| Quyền | Để làm gì | Thiếu thì lỗi gì |
|---|---|---|
| `cloudfunctions.developer` | Gọi `gcloud functions deploy` | `PERMISSION_DENIED: cloudfunctions.functions.update` |
| `run.admin` | Gán IAM cho Cloud Run service bên dưới | `run.services.setIamPolicy denied` ở **bước cuối**, sau khi build đã xong |
| `iam.serviceAccountUser` **trên SA** | Deploy function chạy as SA khác | `PERMISSION_DENIED: iam.serviceAccounts.actAs` |
| `run.invoker` **trên function** | Bước smoke test gọi được function | Smoke test nhận 403 |

**Cách xác định SA nào trigger thực sự dùng:** Console → Cloud Build → Triggers →
mở trigger → mục "Service account". Vòng lặp trên cấp cho cả hai nên thường không
cần kiểm tra, nhưng khi lỗi thì đây là chỗ nhìn.

---

## Bước 4 — Tạo trigger

```bash
GH_OWNER="huanphamngoc"
GH_REPO="google_cloud_function"

# Trigger cho function SCD2 (bản mới)
gcloud builds triggers create github \
  --name="deploy-sheets-multi-scd2" \
  --repo-owner="$GH_OWNER" \
  --repo-name="$GH_REPO" \
  --branch-pattern="^main$" \
  --build-config="functions/sheets_multi_scd2/cloudbuild.yaml" \
  --included-files="functions/sheets_multi_scd2/**"

# Trigger cho function cũ
gcloud builds triggers create github \
  --name="deploy-sheet-to-bq-loader" \
  --repo-owner="$GH_OWNER" \
  --repo-name="$GH_REPO" \
  --branch-pattern="^main$" \
  --build-config="functions/sheet_to_bq_loader/cloudbuild.yaml" \
  --included-files="functions/sheet_to_bq_loader/**"
```

`--included-files` là chi tiết đáng giá: sửa `design/` hay `dags/` sẽ **không** kích
hoạt deploy nào. Không có nó, mỗi lần sửa tài liệu là hai function bị deploy lại.

Kiểm tra:

```bash
gcloud builds triggers list --format="table(name, github.name, filename)"
```

---

## Bước 5 — Test

```bash
# Sửa một dòng comment bất kỳ trong main.py
git add functions/sheets_multi_scd2/main.py
git commit -m "test CI/CD trigger"
git push
```

Theo dõi:

```bash
gcloud builds list --limit=5
gcloud builds log $(gcloud builds list --limit=1 --format='value(id)')
```

Hoặc Console → **Cloud Build → History**.

---

## Bước 6 — Xác nhận deploy thật sự chạy

```bash
gcloud functions describe sheets-multi-scd2 --region=asia-southeast1 --gen2 \
  --format="value(updateTime)"
```

Thời gian này phải khớp với lúc build vừa xong — xác nhận đây là bản mới, không
phải bản cũ còn sót.

---

## Cloud Build làm gì trong mỗi lần chạy

Bốn bước, xếp theo thứ tự **cái nào fail rẻ nhất thì chạy trước**:

| Bước | Nội dung | Thời gian | Bắt được lỗi gì |
|---|---|---|---|
| 1. `tests` | 352 test | ~30 giây | Logic sai, SQL sinh sai, config validate sai |
| 2. `config-check` | Parse `sources.yaml` thật | ~10 giây | Tab khai báo `scd2` mà quên `business_key` |
| 3. `deploy` | `gcloud functions deploy` | ~2 phút | Lỗi IAM, lỗi build container |
| 4. `smoke-test` | Gọi function với `?dry_run=1` | ~20 giây | Function deploy xong nhưng không đọc được Sheet |

Bước 2 tồn tại vì một `sources.yaml` hỏng vẫn deploy trót lọt rồi mới fail lúc 6h
sáng hôm sau. Bắt ở đây tốn 10 giây; bắt ở kia tốn cả buổi sáng.

Bước 4 là bước nhiều CI/CD bỏ qua: deploy thành công **không** có nghĩa là function
chạy được. Dry run đọc bảng tính thật, kiểm tra mọi tab, **không ghi gì** — chứng
minh bản vừa deploy thực sự với tới được Sheets API.

---

## Lỗi thường gặp

| Lỗi | Nguyên nhân | Xử lý |
|---|---|---|
| `missing permission on the build service account` | Thiếu quyền build | Đã cấp ở `deployment-log.md`; nếu vẫn lỗi, kiểm tra đúng SA nào |
| `iam.serviceAccounts.actAs denied` | Thiếu `serviceAccountUser` **trên SA** | Bước 3 mục 2 — chú ý cấp trên resource SA chứ không phải project |
| `key in the template is not a valid built-in substitution` | Biến shell trong `cloudbuild.yaml` viết `$VAR` | Phải viết `$$VAR` — Cloud Build thay `$VAR` trước khi bash chạy |
| Smoke test `403` | Build SA chưa có `run.invoker` | Bước 3 mục 3 |
| Trigger không chạy khi push | `--included-files` không khớp đường dẫn | `gcloud builds triggers describe <tên>` xem lại |
| Build chạy nhưng deploy bản cũ | Push nhầm nhánh | `git branch --show-current` |

---

## Nên làm thêm sau khi CI/CD chạy được

**Trigger kiểm tra Pull Request** — chạy test nhưng **không** deploy, để bắt lỗi
trước khi merge:

```bash
gcloud builds triggers create github \
  --name="pr-check" \
  --repo-owner="huanphamngoc" \
  --repo-name="google_cloud_function" \
  --pull-request-pattern="^main$" \
  --build-config="cloudbuild-pr.yaml"
```

(cần tạo `cloudbuild-pr.yaml` chỉ có bước `tests` — nói tôi nếu cần.)

**Rollback** — mỗi lần deploy tạo một revision Cloud Run bên dưới:

```bash
gcloud run revisions list --service=sheets-multi-scd2 --region=asia-southeast1
gcloud run services update-traffic sheets-multi-scd2 \
  --region=asia-southeast1 --to-revisions=<revision-cũ>=100
```

**Thông báo khi build fail** — Cloud Build hỗ trợ Pub/Sub notification nối vào
Slack/email. Không có nó, build đỏ chỉ ai vào Console mới biết.
