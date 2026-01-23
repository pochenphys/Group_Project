# 使用 Python 3.11 作為基礎映像
FROM python:3.11-slim

# 設置工作目錄
WORKDIR /app

# 複製 requirements.txt 並安裝依賴
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 複製應用程式碼
COPY middle.py .

# 設置環境變數（Cloud Run 會覆蓋 PORT）
ENV PORT=8080
ENV PYTHONUNBUFFERED=1

# 暴露端口（Cloud Run 會自動映射）
EXPOSE 8080

# 啟動應用程式
CMD ["python", "middle.py", "--host", "0.0.0.0", "--port", "8080"]
