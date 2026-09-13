# CI/CD cho sheet-to-bq-loader — hướng dẫn từ A đến Z

Mục tiêu: mỗi lần push code lên GitHub (nhánh `main`), Cloud Build tự động deploy
lại function — không cần gõ `gcloud functions deploy` bằng tay nữa.

## Kiến trúc

```
git push main  →  GitHub  →  Cloud Build trigger  →  cloudbuild.yaml chạy:
                                                        1. syntax check main.py
                                                        2. gcloud functions deploy
```

File liên quan:
- [`cloudbuild.yaml`](cloudbuild.yaml) — định nghĩa các bước build/deploy
- [`main.py`](main.py), [`requirements.txt`](requirements.txt) — code function (không đổi)
- [`deployment-log.md`](deployment-log.md) — lịch sử debug IAM đã làm thủ công trước đây, tất cả role ở đó vẫn cần giữ nguyên

---

## Bước 1 — Đưa code lên GitHub

```bash
cd /path/to/repo
git init
git add functions/sheet_to_bq_loader
git commit -m "Add sheet-to-bq-loader Cloud Function"
git branch -M main
```

Tạo repo trống trên GitHub (qua web, hoặc `gh repo create <name> --private`), rồi:

```bash
git remote add origin https://github.com/<username>/<repo-name>.git
git push -u origin main
```

---

## Bước 2 — Kết nối GitHub với Cloud Build (thủ công, làm 1 lần)

Không script được bước này (giống việc share Google Sheet với service account trước
đây — phải thao tác tay qua UI):

**Console → Cloud Build → Triggers → Connect Repository → GitHub → Authorize → chọn đúng repo**

---

## Bước 3 — Cấp quyền cho Cloud Build để nó được phép DEPLOY

Đây là lớp quyền **mới**, khác với quyền build đã cấp ở `deployment-log.md`. Build
xong không có nghĩa là được phép deploy — deploy cần thêm 2 quyền:

1. `roles/cloudfunctions.developer` — được phép gọi `gcloud functions deploy`
2. `roles/iam.serviceAccountUser` **trên đúng service account** `sheets-to-bq-loader@...`
   — được phép deploy 1 function chạy AS service account đó ("actAs" permission)

```bash
PROJECT_ID="elt-drive-to-bq"
PROJECT_NUMBER=$(gcloud projects describe "$PROJECT_ID" --format='value(projectNumber)')
CLOUDBUILD_SA="${PROJECT_NUMBER}@cloudbuild.gserviceaccount.com"

gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:${CLOUDBUILD_SA}" \
  --role="roles/cloudfunctions.developer" \
  --condition=None

gcloud iam service-accounts add-iam-policy-binding \
  "sheets-to-bq-loader@${PROJECT_ID}.iam.gserviceaccount.com" \
  --member="serviceAccount:${CLOUDBUILD_SA}" \
  --role="roles/iam.serviceAccountUser"
```

> **Lưu ý dựa trên kinh nghiệm thực tế ở `deployment-log.md`:** project này từng build
> bằng **Compute default service account** thay vì Cloud Build SA riêng. Nếu build CI/CD
> báo lỗi thiếu quyền tương tự, cấp thêm 2 role trên cho
> `${PROJECT_NUMBER}-compute@developer.gserviceaccount.com` nữa:
> ```bash
> COMPUTE_SA="${PROJECT_NUMBER}-compute@developer.gserviceaccount.com"
> gcloud projects add-iam-policy-binding "$PROJECT_ID" \
>   --member="serviceAccount:${COMPUTE_SA}" \
>   --role="roles/cloudfunctions.developer" --condition=None
> gcloud iam service-accounts add-iam-policy-binding \
>   "sheets-to-bq-loader@${PROJECT_ID}.iam.gserviceaccount.com" \
>   --member="serviceAccount:${COMPUTE_SA}" \
>   --role="roles/iam.serviceAccountUser"
> ```
> Cách xác định SA nào trigger thực sự dùng: Console → Cloud Build → Triggers →
> mở trigger → mục "Service account".

---

## Bước 4 — File `cloudbuild.yaml` (đã tạo sẵn)

Đã có tại [`functions/sheet_to_bq_loader/cloudbuild.yaml`](cloudbuild.yaml) — 2 bước:

1. **syntax-check**: `python -m py_compile main.py` — bắt lỗi cú pháp trước khi tốn thời gian deploy
2. **deploy**: đúng lệnh `gcloud functions deploy` đã chạy thành công thủ công trước đây, chỉ khác là chạy tự động trong Cloud Build

`$PROJECT_ID` trong file là biến built-in của Cloud Build, tự thay bằng project thật lúc chạy — không cần sửa gì thêm.

---

## Bước 5 — Tạo trigger

```bash
gcloud builds triggers create github \
  --name="deploy-sheet-to-bq-loader" \
  --repo-owner="<GITHUB_USERNAME>" \
  --repo-name="<REPO_NAME>" \
  --branch-pattern="^main$" \
  --build-config="functions/sheet_to_bq_loader/cloudbuild.yaml" \
  --included-files="functions/sheet_to_bq_loader/**"
```

`--included-files` giới hạn: trigger chỉ chạy khi có thay đổi trong đúng thư mục
function này — sửa `design/` hay code khác trong repo sẽ không kích hoạt deploy thừa.

---

## Bước 6 — Test

Sửa 1 dòng bất kỳ trong `main.py` (hoặc chỉ đổi comment), commit, push:

```bash
git add functions/sheet_to_bq_loader/main.py
git commit -m "test CI/CD trigger"
git push
```

Theo dõi build:

```bash
gcloud builds list --limit=5
```

Lấy `BUILD_ID` từ kết quả trên, xem log chi tiết:

```bash
gcloud builds log <BUILD_ID>
```

Hoặc xem trực tiếp trên Console: **Cloud Build → History**.

---

## Bước 7 — Xác nhận deploy thành công

```bash
gcloud functions describe sheet-to-bq-loader --region=asia-southeast1 --gen2 \
  --format="value(updateTime)"
```

Thời gian này phải khớp với lúc build vừa chạy xong — xác nhận function đã được
deploy lại tự động, không phải bản cũ.

---

## Việc có thể làm thêm sau này (không bắt buộc để CI/CD chạy được)

- **PR validation**: thêm 1 trigger thứ 2 chạy `py_compile` trên pull request (không deploy), để bắt lỗi trước khi merge — theo đúng pattern đã mô tả ở `design/architecture.md` §10 cho toàn bộ chương trình migration, thu nhỏ lại cho 1 function.
- **Rollback**: `gcloud functions deploy` mỗi lần tạo 1 revision mới trên Cloud Run bên dưới — có thể `gcloud run services update-traffic` để trỏ về revision cũ nếu bản mới lỗi.
- **Thông báo khi build fail**: Cloud Build hỗ trợ Pub/Sub notification → nối vào Slack/email, tránh phải tự vào Console kiểm tra.
