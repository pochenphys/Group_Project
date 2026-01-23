# Google Cloud Run 部署指南

以下是將您的 API (`dify_retrieval_app.py`) 部署到 Google Cloud Run 的步驟。

## 1. 前置準備

確認您已經安裝並登入 Google Cloud SDK (`gcloud`)：

```bash
gcloud auth login
gcloud config set project [您的-User-Project-ID]
```

## 2. 啟用必要的服務

```bash
gcloud services enable cloudbuild.googleapis.com run.googleapis.com
```

## 3. 建置並部署 (一步完成)

在我們剛剛建立 Dockerfile 的目錄下 (`The big change`) 執行：

```bash
gcloud run deploy dify-retrieval-api --source . --region asia-northeast1 --allow-unauthenticated
```

*   `--source .`：會自動將目錄下的程式碼打包並上傳到 Cloud Build 進行建置。
*   `--region asia-east1`：部署到台灣 (彰化) 機房，速度最快。
*   `--allow-unauthenticated`：允許公開存取 (Dify 才能呼叫)。如果您希望保護 API，需設定 Dify 的驗證 Header。

## 3.5. 未來更新程式碼

如果您修改了程式碼（例如修改了 `dify_retrieval_app.py`），只需要**再次執行相同的指令**即可：

```bash
gcloud run deploy dify-retrieval-api --source . --region asia-northeast1 --allow-unauthenticated
```

Cloud Run 會自動建立新的版本 (Revision) 並將流量導向新版。

## 4. 設定環境變數

部署成功後，您需要將 `.env` 裡面的 Key 設定到 Cloud Run 上：

1.  前往 [Google Cloud Console - Cloud Run](https://console.cloud.google.com/run)
2.  點選 `dify-retrieval-api` 服務。
3.  點選上方 **「編輯與部署新版本」 (Edit & Deploy New Revision)**。
4.  切換到 **「變數與密鑰」 (Variables & Secrets)** 分頁。
5.  新增環境變數：
    *   `GOOGLE_API_KEY`: 您的 Google API Key
    *   `PINECONE_API_KEY`: 您的 Pinecone API Key
6.  點選 **「部署」 (Deploy)**。

## 5. 更新 Dify 設定

取得 Cloud Run 的 URL (例如 `https://dify-retrieval-api-xxxx.a.run.app`)，並更新您的 `dify_openapi.json`：

```json
"servers": [
  {
    "url": "https://dify-retrieval-api-xxxx.a.run.app"
  }
]
```

然後在 Dify 中重新匯入工具即可！
