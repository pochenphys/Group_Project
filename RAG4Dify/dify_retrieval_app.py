
import os
import time
import json
import base64
from flask import Flask, request, jsonify
from langchain_google_genai import GoogleGenerativeAIEmbeddings
from pinecone import Pinecone
from dotenv import load_dotenv

# Load env variables from .env file
load_dotenv()

app = Flask(__name__)
app.config['JSON_AS_ASCII'] = False

# =======================================================
# 配置 & 初始化
# =======================================================
# 請確保環境變數已設定:
# GOOGLE_API_KEY
# PINECONE_API_KEY

GOOGLE_API_KEY = os.environ.get("GOOGLE_API_KEY")
PINECONE_API_KEY = os.environ.get("PINECONE_API_KEY")
INDEX_NAME = "recipe-vector"  # 您的 Pinecone Index 名稱

pc_client = None
embeddings_model = None

def init_services():
    global pc_client, embeddings_model
    if not pc_client and PINECONE_API_KEY:
        print("🔌 連接 Pinecone...")
        pc_client = Pinecone(api_key=PINECONE_API_KEY)
    
    if not embeddings_model and GOOGLE_API_KEY:
        print("🧠 初始化 Google Embeddings...")
        embeddings_model = GoogleGenerativeAIEmbeddings(model="text-embedding-004")

# =======================================================
# 輔助函式
# =======================================================
def parse_metadata_text(meta_text):
    """解析 Pinecone 文字內容，只回傳精簡資訊 (標題 + 食材/摘要)"""
    if not meta_text: return {}
    
    # 預設值
    data = {
        "title": "未知食譜", 
        "summary": "無詳細資料" # 改叫 summary，放食材重點
    }
    
    try:
        lines = meta_text.split('\n')
        ingredients_found = ""
        
        for line in lines:
            line = line.strip()
            if line.startswith("dishname:"):
                data["title"] = line.split(":", 1)[1].strip()
            elif line.startswith("材料:"):
                # 只保留材料部分，不需太長
                raw_ingr = line.split(":", 1)[1].strip()
                # 簡單清理：把 | 換成 , 讓 LLM 好讀
                ingredients_found = raw_ingr.replace(" | ", ", ")
                
        # 組合 Summary
        if ingredients_found:
            # 截斷過長的食材清單，節省 Token
            if len(ingredients_found) > 100:
                ingredients_found = ingredients_found[:100] + "..."
            data["summary"] = f"食材: {ingredients_found}"
        else:
            # 如果沒抓到材料，就用第一行當摘要
            data["summary"] = lines[0][:50] if lines else "未知內容"

        # Fallback title Check
        if data["title"] == "未知食譜" and lines:
             data["title"] = lines[0].split(":", 1)[1].strip() if ":" in lines[0] else lines[0].strip()

    except Exception:
        pass
        
    return data

# =======================================================
# API: Dify 專用檢索接口
# =======================================================
@app.route('/api/retrieve_context', methods=['POST'])
def retrieve_context():
    """
    Dify Tool 呼叫此接口來取得「使用者偏好食譜內容」。
    不進行生成，只回傳純文字資料。
    """
    init_services()
    data = request.json
    user_id = data.get('user_id')
    ingredients = data.get('ingredients', '') # 可選：當下食材
    
    print(f"🔍 Dify Fetching Context for: {user_id}, Ingredients: {ingredients}")
    
    if not pc_client or not embeddings_model:
        return jsonify({"error": "Service not initialized (Missing Keys?)"}), 500
        
    try:
        idx = pc_client.Index(INDEX_NAME)
        query_vector = None
        
        # 1. 決定查詢向量 (Query Vector)
        # 策略：如果有給食材，就用食材查；如果沒給，就用使用者偏好查
        if ingredients:
            print(f"🥦 使用食材搜尋: {ingredients}")
            query_vector = embeddings_model.embed_query(ingredients)
        elif user_id:
            # 嘗試讀取使用者偏好
            print(f"👤 讀取使用者偏好: {user_id}")
            fetch_res = idx.fetch(ids=[user_id], namespace="users")
            if user_id in fetch_res.vectors:
                query_vector = fetch_res.vectors[user_id].values
            else:
                # Cold Start (隨機給一個主題)
                topic = "Taiwanese Cuisine" 
                query_vector = embeddings_model.embed_query(topic)
                print("❄️ 新使用者，使用預設主題搜尋")
        else:
            # [Fallback] 如果沒 ID 也沒食材，明確告訴 Dify "找不到上下文"
            # 讓 Dify LLM 節點自己自由發揮
            print("⚠️ 無參數，回傳空結果供 Dify 自由生成")
            return jsonify({
                "status": "no_context",
                "user_id": None,
                "retrieved_recipes": [],  # 空陣列
                "message": "No context found, please generate freely."
            })
        
        if not query_vector:
             # 這裡應該不會到了，但為了保險起見
             return jsonify({"error": "No query vector generated"}), 400

        # 2. 向量搜尋 (Vector Search)
        query_res = idx.query(
            vector=query_vector,
            top_k=5, 
            include_metadata=True,
            namespace="recipe"
        )
        
        # 3. 整理回傳資料 (精簡版：標題 + 食材摘要)
        results = []
        for match in query_res.matches:
            meta = match.metadata or {}
            raw_text = meta.get('text', '')
            parsed = parse_metadata_text(raw_text)
            
            results.append({
                "title": parsed['title'],
                "context": parsed['summary'], # 這裡只放精簡的食材與風味資訊
                "score": match.score
            })
            
        return jsonify({
            "status": "success",
            "user_id": user_id,
            "retrieved_recipes": results
        })

    except Exception as e:
        print(f"❌ Error: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/', methods=['GET'])
def health():
    return "Dify Retrieval Service is Running!", 200

if __name__ == '__main__':
    # 預設跑在 5001 port，避免跟原本的衝突
    app.run(host='0.0.0.0', port=5001)
