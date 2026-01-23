# 使用 Python 3.11 作為基礎映像
FROM python:3.11-slim

# 設置工作目錄
WORKDIR /app

# 複製 requirements.txt 並安裝依賴
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 複製應用程式碼
COPY . .

# 設置環境變數
ENV PORT=8080
ENV PYTHONUNBUFFERED=1

# 暴露端口
EXPOSE 8080

# 啟動應用程式 (使用 gunicorn 以提高並發能力)
CMD ["gunicorn", "--bind", "0.0.0.0:8080", "--workers", "1", "--threads", "8", "--timeout", "120", "LINE_Bot_Router:app"]
