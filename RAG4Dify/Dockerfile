# 使用官方 Python 輕量版映像檔
FROM python:3.11-slim

# 設定工作目錄
WORKDIR /app

# 防止 Python 產生 .pyc 檔案
ENV PYTHONDONTWRITEBYTECODE=1
# 確保 Python 輸出直接顯示在 Log 中
ENV PYTHONUNBUFFERED=1

# 安裝依賴套件
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 複製程式碼到容器中
COPY dify_retrieval_app.py .
# 這裡假設您的 .env 會透過 Cloud Run 的環境變數設定，所以不一定要 COPY .env
# 但為了方便本地測試，我們先 COPY 過去 (Cloud Run 部署時建議在 Console 設定環境變數)
# COPY .env . 

# 設定 Cloud Run 預設 Port
ENV PORT=8080

# 使用 Gunicorn 啟動 Flask 應用
# -w 2: 2 個 Worker 進程
# --threads 8: 每個 Worker 8 個執行緒
# -b 0.0.0.0:$PORT: 監聽 Cloud Run 指定的 Port
CMD exec gunicorn --bind :$PORT --workers 1 --threads 8 --timeout 0 dify_retrieval_app:app
