"""
LINE Bot 中間層程式 (middle.py)
作為 LINE Bot 與後端功能的中繼器，提供主選單和功能路由
"""

import os
import json
import hmac
import hashlib
import base64
import requests
from typing import Dict, Optional, List
import concurrent.futures
from flask import Flask, request, abort, jsonify
from urllib.parse import parse_qsl
from dotenv import load_dotenv

# 載入環境變數
load_dotenv('LINE.env', override=False)

# LINE API 端點
LINE_REPLY_URL = 'https://api.line.me/v2/bot/message/reply'
LINE_PUSH_URL = 'https://api.line.me/v2/bot/message/push'
LINE_CONTENT_URL = 'https://api-data.line.me/v2/bot/message/{message_id}/content'


class LINEWebhookHandler:
    """處理 LINE Webhook 請求"""
    
    def __init__(self, channel_secret: str):
        """
        初始化 Webhook 處理器
        
        Args:
            channel_secret: LINE Channel Secret
        """
        self.channel_secret = channel_secret.encode('utf-8') if channel_secret else None
    
    def verify_signature(self, request_body: bytes, signature: str) -> bool:
        """
        驗證請求簽名
        
        公式: signature = base64(hmac-sha256(channel_secret, request_body))
        
        Args:
            request_body: 請求主體（bytes）
            signature: 請求標頭中的簽名
            
        Returns:
            bool: 簽名是否有效
        """
        if not self.channel_secret:
            print("警告: Channel Secret 未設定，跳過簽名驗證")
            return True
        
        try:
            # 計算簽名
            hash_value = hmac.new(
                self.channel_secret,
                request_body,
                hashlib.sha256
            ).digest()
            expected_signature = base64.b64encode(hash_value).decode('utf-8')
            
            # 比較簽名
            return hmac.compare_digest(expected_signature, signature)
        except Exception as e:
            print(f"簽名驗證錯誤: {str(e)}")
            return False
    
    def parse_webhook_event(self, request_data: Dict) -> List[Dict]:
        """
        解析 Webhook 事件
        
        Args:
            request_data: Webhook 請求資料
            
        Returns:
            List[Dict]: 事件列表
        """
        events = request_data.get('events', [])
        return events


class LINEAPIClient:
    """與 LINE API 通訊"""
    
    def __init__(self, access_token: str):
        """
        初始化 LINE API 客戶端
        
        Args:
            access_token: LINE Channel Access Token
        """
        self.access_token = access_token
        self.headers = {
            'Authorization': f'Bearer {self.access_token}',
            'Content-Type': 'application/json'
        }
        # 使用 Session 复用连接，提高性能
        self.session = requests.Session()
        self.session.headers.update(self.headers)
    
    def download_image(self, message_id: str) -> Optional[bytes]:
        """
        從 LINE 下載圖片
        
        API: GET https://api-data.line.me/v2/bot/message/{message_id}/content
        
        Args:
            message_id: 圖片訊息 ID
            
        Returns:
            Optional[bytes]: 圖片資料，失敗返回 None
        """
        try:
            url = LINE_CONTENT_URL.format(message_id=message_id)
            headers = {
                'Authorization': f'Bearer {self.access_token}'
            }
            
            # 使用 session 复用连接
            response = self.session.get(url, headers=headers, timeout=30)
            response.raise_for_status()
            
            return response.content
        except Exception as e:
            print(f"下載圖片失敗: {str(e)}")
            return None
    
    def send_text_message(self, user_id: str, text: str) -> bool:
        """
        發送文字訊息到 LINE
        
        API: POST https://api.line.me/v2/bot/message/push
        
        Args:
            user_id: LINE 使用者 ID
            text: 訊息內容
            
        Returns:
            bool: 是否成功發送
        """
        try:
            url = LINE_PUSH_URL
            payload = {
                'to': user_id,
                'messages': [
                    {
                        'type': 'text',
                        'text': text
                    }
                ]
            }
            
            # 使用 session 复用连接
            response = self.session.post(url, json=payload, timeout=10)
            response.raise_for_status()
            return True
        except Exception as e:
            print(f"發送訊息失敗: {str(e)}")
            return False
    
    def reply_messages(self, reply_token: str, messages: List[Dict]) -> bool:
        """
        回覆多則訊息
        """
        if not messages:
            print(f"[DEBUG] reply_messages: 沒有訊息需要發送")
            return True

        # 調試：打印要發送的訊息
        print(f"[DEBUG] reply_messages: 準備發送 {len(messages)} 條訊息給 LINE")
        print(f"[DEBUG] reply_token: {reply_token[:10]}...")

        for i, msg in enumerate(messages[:5]):  # 只顯示前5條，因為 LINE 限制最多5條
            print(f"[DEBUG]   訊息 {i+1}: type={msg.get('type', 'unknown')}")
            if msg.get('type') == 'text':
                text_content = msg.get('text', '')
                print(f"[DEBUG]     文字內容: {text_content[:100]}{'...' if len(text_content) > 100 else ''}")
            elif msg.get('type') == 'image':
                print(f"[DEBUG]     圖片URL: {msg.get('originalContentUrl', 'N/A')}")
            elif msg.get('type') == 'flex':
                print(f"[DEBUG]     Flex訊息: alt_text={msg.get('altText', 'N/A')}")
            else:
                print(f"[DEBUG]     其他類型: {msg}")

        try:
            url = LINE_REPLY_URL
            payload = {
                'replyToken': reply_token,
                'messages': messages[:5]  # Line 限制最多 5 則
            }

            print(f"[DEBUG] 發送 LINE API 請求到: {url}")
            response = self.session.post(url, json=payload, timeout=30)

            print(f"[DEBUG] LINE API 回應狀態碼: {response.status_code}")
            if response.status_code == 200:
                print(f"[DEBUG] LINE API 回應成功")
                return True
            else:
                print(f"[DEBUG] LINE API 回應失敗，內容: {response.text[:200]}")
                response.raise_for_status()
                return True

        except Exception as e:
            print(f"[ERROR] 回覆多則訊息失敗: {str(e)}")
            import traceback
            traceback.print_exc()
            return False

app = Flask(__name__)

# 從環境變數讀取設定
LINE_CHANNEL_ACCESS_TOKEN = os.getenv('LINE_CHANNEL_ACCESS_TOKEN')
LINE_CHANNEL_SECRET = os.getenv('LINE_CHANNEL_SECRET')

# Cloud Run URL（後端功能服務 - AI 功能）
CLOUD_RUN_URL = os.getenv('CLOUD_RUN_URL', 'https://line-bot-router-1081425514180.asia-northeast1.run.app')

# 自製食譜後端 URL (Tibame line-service)
CUSTOM_RECIPE_URL = os.getenv('CUSTOM_RECIPE_URL', 'https://line-service-1081425514180.asia-northeast1.run.app')

# RAG Service URL (用於偏好紀錄)
RAG_API_URL = os.getenv('RAG_API_URL', 'https://rag-imagen4-service-1081425514180.asia-northeast1.run.app')

# 初始化 LINE 客戶端
webhook_handler = LINEWebhookHandler(LINE_CHANNEL_SECRET)
line_client = LINEAPIClient(LINE_CHANNEL_ACCESS_TOKEN)

# 用戶狀態管理
# 格式: {user_id: 'main' | 'ai' | 'custom' | 'ai_recipe' | 'ai_record' | 'ai_view' | 'ai_delete'}
user_state = {}

# AI 功能映射
AI_FUNCTIONS = {
    'recipe': '食譜',
    'record': '紀錄',
    'view': '查看',
    'delete': '刪除'
}


def create_ai_carousel_menu() -> List[Dict]:
    """
    創建 AI 功能選擇的 Carousel Template Message
    
    Returns:
        List[Dict]: Carousel columns 列表
    """
    columns = [
        {
            'thumbnailImageUrl': 'https://via.placeholder.com/300x200/FF6B6B/FFFFFF?text=食譜',
            'title': '食譜功能',
            'text': '上傳食物圖片，獲得詳細食譜和烹飪建議',
            'actions': [
                {
                    'type': 'postback',
                    'label': '選擇食譜',
                    'data': 'ai_function=recipe'
                }
            ]
        },
        {
            'thumbnailImageUrl': 'https://via.placeholder.com/300x200/4ECDC4/FFFFFF?text=紀錄',
            'title': '紀錄功能',
            'text': '上傳食物圖片，記錄食物名稱和入庫時間',
            'actions': [
                {
                    'type': 'postback',
                    'label': '選擇紀錄',
                    'data': 'ai_function=record'
                }
            ]
        },
        {
            'thumbnailImageUrl': 'https://via.placeholder.com/300x200/95E1D3/FFFFFF?text=查看',
            'title': '查看功能',
            'text': '查看您的食物記錄列表',
            'actions': [
                {
                    'type': 'postback',
                    'label': '選擇查看',
                    'data': 'ai_function=view'
                }
            ]
        },
        {
            'thumbnailImageUrl': 'https://via.placeholder.com/300x200/F38181/FFFFFF?text=刪除',
            'title': '刪除功能',
            'text': '記錄食品消耗，從最舊的記錄開始扣除',
            'actions': [
                {
                    'type': 'postback',
                    'label': '選擇刪除',
                    'data': 'ai_function=delete'
                }
            ]
        }
    ]
    return columns


def send_carousel_template(reply_token: str, columns: List[Dict], alt_text: str = '功能選擇') -> bool:
    """
    發送 Carousel Template Message
    
    Args:
        reply_token: 回覆 Token
        columns: Carousel columns 列表
        alt_text: 替代文字
        
    Returns:
        bool: 是否成功發送
    """
    try:
        url = 'https://api.line.me/v2/bot/message/reply'
        headers = {
            'Authorization': f'Bearer {LINE_CHANNEL_ACCESS_TOKEN}',
            'Content-Type': 'application/json'
        }
        
        payload = {
            'replyToken': reply_token,
            'messages': [
                {
                    'type': 'template',
                    'altText': alt_text,
                    'template': {
                        'type': 'carousel',
                        'columns': columns
                    }
                }
            ]
        }
        
        response = requests.post(url, json=payload, headers=headers, timeout=10)
        response.raise_for_status()
        return True
    except Exception as e:
        print(f"發送 Carousel Template 失敗: {e}")
        return False


def call_process_api(url_base: str, user_id: str, event: Dict) -> List[Dict]:
    """呼叫後端 API 取得訊息"""
    if not url_base:
        return []
    try:
        url = f"{url_base.rstrip('/')}/api/process_message"
        response = requests.post(url, json={'user_id': user_id, 'event': event}, timeout=110)
        if response.status_code == 200:
            return response.json().get('messages', [])
    except Exception as e:
        print(f"呼叫 API {url} 失敗: {e}")
    return []

def send_like_feedback(user_id: str, recipe_id: str):
    """傳送正向回饋到 RAG 服務"""
    if not RAG_API_URL: return
    try:
        requests.post(f"{RAG_API_URL}/api/like", json={"user_id": user_id, "recipe_id": recipe_id}, timeout=5)
    except Exception as e:
        print(f"❌ Like Error: {e}")

def forward_to_cloud_run(user_id: str, function_name: str, message_data: Optional[Dict] = None, reply_token: Optional[str] = None) -> bool:
    # 保留舊有邏輯或導向新邏輯
    # 這裡我們讓 middle.py 主動呼叫
    pass


def handle_ai_command(user_id: str, reply_token: str):
    """
    處理 'AI' 命令：顯示功能選擇 carousel
    
    Args:
        user_id: 用戶 ID
        reply_token: 回覆 Token
    """
    # 設置用戶狀態為 AI 模式
    user_state[user_id] = 'ai'
    
    # 創建 carousel menu
    columns = create_ai_carousel_menu()
    
    # 發送 carousel
    send_carousel_template(reply_token, columns, 'AI 功能選擇')


def handle_custom_command(user_id: str, reply_token: str):
    """
    處理 '自製' 命令：啟用自製食譜功能
    
    Args:
        user_id: 用戶 ID
        reply_token: 回覆 Token
    """
    # 設置用戶狀態為自製模式
    user_state[user_id] = 'custom'
    
    guide_message = (
        "🍳 自製食譜功能已啟用！\n\n"
        "📸 您可以：\n"
        "• 上傳食物圖片，獲得食譜建議\n"
        "• 輸入文字查詢相關食譜\n\n"
        "請上傳圖片或輸入文字開始使用！\n\n"
        "💡 提示：\n"
        "• 輸入「主頁」可返回主選單"
    )
    
    line_client.reply_message(reply_token, guide_message)


def handle_home_command(user_id: str, reply_token: str):
    """
    處理 '主頁' 命令：返回主選單
    
    Args:
        user_id: 用戶 ID
        reply_token: 回覆 Token
    """
    # 清除用戶狀態，返回主頁
    if user_id in user_state:
        del user_state[user_id]
    
    welcome_message = (
        "🏠 歡迎使用 LINE Bot！\n\n"
        "請選擇功能：\n"
        "• 輸入「AI」- 使用 AI 功能（食譜、紀錄、查看、刪除）\n"
        "• 輸入「自製」- 使用自製食譜功能\n\n"
        "💡 提示：\n"
        "• 輸入「主頁」隨時返回此選單"
    )
    
    line_client.reply_message(reply_token, welcome_message)


def handle_ai_function_selection(user_id: str, function_name: str, reply_token: Optional[str] = None):
    """
    處理 AI 功能選擇（從 carousel postback）
    
    Args:
        user_id: 用戶 ID
        function_name: 功能名稱 (recipe, record, view, delete)
        reply_token: 可選的回覆 Token
    """
    # 設置用戶狀態
    user_state[user_id] = f'ai_{function_name}'
    
    # 轉發到 Cloud Run（包含 replyToken，讓 LINE_Bot_Router.py 可以回覆）
    # 注意：我們不在此處使用 replyToken，而是讓後端服務使用
    success = forward_to_cloud_run(user_id, function_name, reply_token=reply_token)
    
    if not success:
        # 只有轉發失敗時才由 middle.py 回覆錯誤訊息
        error_message = f"❌ 啟動 {AI_FUNCTIONS.get(function_name, function_name)} 功能失敗，請稍後再試。"
        
        if reply_token:
            line_client.reply_message(reply_token, error_message)
        else:
            line_client.send_text_message(user_id, error_message)


def handle_custom_recipe(user_id: str, text: str, reply_token: Optional[str] = None):
    """
    處理自製食譜功能的文字查詢
    
    Args:
        user_id: 用戶 ID
        text: 用戶輸入的文字
        reply_token: 可選的回覆 Token
    """
    # 檢查是否為退出命令
    if text.strip() in ['主頁', 'home', '退出', 'exit']:
        handle_home_command(user_id, reply_token if reply_token else '')
        return
    
    # 如果有設定自製食譜後端 URL，轉發請求
    if CUSTOM_RECIPE_URL:
        # 轉發文字查詢到自製食譜後端
        try:
            # 創建模擬的 webhook 事件
            mock_event = {
                'type': 'message',
                'source': {'userId': user_id},
                'message': {
                    'type': 'text',
                    'text': text
                },
                'timestamp': int(os.urandom(4).hex(), 16)
            }
            
            # 如果有 replyToken，加入事件中
            if reply_token:
                mock_event['replyToken'] = reply_token
            
            webhook_payload = {'events': [mock_event]}
            
            # 生成簽名（如果後端需要驗證）
            request_body_bytes = json.dumps(webhook_payload).encode('utf-8')
            if LINE_CHANNEL_SECRET:
                hash_value = hmac.new(
                    LINE_CHANNEL_SECRET.encode('utf-8'),
                    request_body_bytes,
                    hashlib.sha256
                ).digest()
                signature = base64.b64encode(hash_value).decode('utf-8')
            else:
                signature = ''
            
            headers = {
                'Content-Type': 'application/json',
                'X-Line-Signature': signature
            }
            
            print(f"[DEBUG] 轉發自製食譜文字查詢到後端 URL: {CUSTOM_RECIPE_URL}/callback")
            print(f"[DEBUG] 查詢內容: {text}")
            
            response = requests.post(
                f"{CUSTOM_RECIPE_URL}/callback",
                json=webhook_payload,
                headers=headers,
                timeout=30
            )
            
            if response.status_code == 200:
                print(f"✓ 成功轉發自製食譜查詢到後端")
                return
            else:
                print(f"✗ 轉發失敗: {response.status_code} - {response.text}")
                # 轉發失敗時，返回錯誤訊息
                error_message = (
                    f"🔍 您查詢：{text}\n\n"
                    "❌ 連接自製食譜後端服務失敗，請稍後再試。\n\n"
                    "💡 提示：\n"
                    "• 輸入「主頁」可返回主選單"
                )
                if reply_token:
                    line_client.reply_message(reply_token, error_message)
                else:
                    line_client.send_text_message(user_id, error_message)
                return
                
        except Exception as e:
            print(f"轉發文字查詢到自製食譜後端失敗: {e}")
            import traceback
            traceback.print_exc()
            # 發生異常時，返回錯誤訊息
            error_message = (
                f"🔍 您查詢：{text}\n\n"
                "❌ 連接自製食譜後端服務失敗，請稍後再試。\n\n"
                "💡 提示：\n"
                "• 輸入「主頁」可返回主選單"
            )
            if reply_token:
                line_client.reply_message(reply_token, error_message)
            else:
                line_client.send_text_message(user_id, error_message)
            return
    
    # 如果沒有設定 CUSTOM_RECIPE_URL，返回提示訊息
    message = (
        f"🔍 您查詢：{text}\n\n"
        "📝 正在為您搜尋相關食譜...\n\n"
        "⚠️ 注意：自製食譜後端服務暫未連接\n"
        "此功能將類似食譜功能，並支援文字查詢。\n\n"
        "💡 提示：\n"
        "• 上傳圖片也可以查詢食譜\n"
        "• 輸入「主頁」可返回主選單"
    )
    
    if reply_token:
        line_client.reply_message(reply_token, message)
    else:
        line_client.send_text_message(user_id, message)


def handle_custom_image(user_id: str, image_event: Dict, reply_token: Optional[str] = None):
    """
    處理自製食譜功能的圖片
    
    Args:
        user_id: 用戶 ID
        image_event: 圖片事件資料
        reply_token: 可選的回覆 Token
    """
    # 這裡應該實現類似 LINE_Bot_Router.py 的食譜功能
    # 處理圖片並生成食譜
    # 由於後端"暫定沒有"，這裡實現基本框架
    
    message_id = image_event.get('message_id')
    
    if not message_id:
        error_msg = "無法取得圖片訊息 ID"
        if reply_token:
            line_client.reply_message(reply_token, error_msg)
        else:
            line_client.send_text_message(user_id, error_msg)
        return
    
    # 下載圖片
    image_data = line_client.download_image(message_id)
    
    if not image_data:
        error_msg = "無法下載圖片，請稍後再試"
        if reply_token:
            line_client.reply_message(reply_token, error_msg)
        else:
            line_client.send_text_message(user_id, error_msg)
        return
    
    # 如果有設定自製食譜後端 URL，轉發圖片請求
    if CUSTOM_RECIPE_URL:
        # 轉發圖片到自製食譜後端
        try:
            # 創建模擬的 webhook 事件
            mock_event = {
                'type': 'message',
                'source': {'userId': user_id},
                'message': {
                    'type': 'image',
                    'id': message_id
                },
                'timestamp': int(os.urandom(4).hex(), 16)
            }
            
            # 如果有 replyToken，加入事件中
            if reply_token:
                mock_event['replyToken'] = reply_token
            
            webhook_payload = {'events': [mock_event]}
            
            # 生成簽名（如果後端需要驗證）
            request_body_bytes = json.dumps(webhook_payload).encode('utf-8')
            if LINE_CHANNEL_SECRET:
                hash_value = hmac.new(
                    LINE_CHANNEL_SECRET.encode('utf-8'),
                    request_body_bytes,
                    hashlib.sha256
                ).digest()
                signature = base64.b64encode(hash_value).decode('utf-8')
            else:
                signature = ''
            
            headers = {
                'Content-Type': 'application/json',
                'X-Line-Signature': signature
            }
            
            print(f"[DEBUG] 轉發自製食譜圖片到後端 URL: {CUSTOM_RECIPE_URL}/callback")
            print(f"[DEBUG] 圖片訊息 ID: {message_id}")
            
            response = requests.post(
                f"{CUSTOM_RECIPE_URL}/callback",
                json=webhook_payload,
                headers=headers,
                timeout=30
            )
            
            if response.status_code == 200:
                message = "📸 已將您的圖片發送到自製食譜服務進行處理..."
            else:
                message = (
                    "📸 收到您的圖片！\n\n"
                    "❌ 連接自製食譜後端服務失敗，請稍後再試。\n\n"
                    "💡 提示：\n"
                    "• 輸入「主頁」可返回主選單"
                )
        except Exception as e:
            print(f"轉發圖片到自製食譜後端失敗: {e}")
            message = (
                "📸 收到您的圖片！\n\n"
                "❌ 連接自製食譜後端服務失敗，請稍後再試。\n\n"
                "💡 提示：\n"
                "• 輸入「主頁」可返回主選單"
            )
    else:
        # 後端服務暫未設定
        message = (
            "📸 收到您的圖片！\n\n"
            "🔍 正在分析圖片並生成食譜...\n\n"
            "⚠️ 注意：自製食譜後端服務暫未連接\n"
            "請在環境變數中設定 CUSTOM_RECIPE_URL。\n\n"
            "💡 提示：\n"
            "• 輸入文字也可以查詢食譜\n"
            "• 輸入「主頁」可返回主選單"
        )
    
    if reply_token:
        line_client.reply_message(reply_token, message)
    else:
        line_client.send_text_message(user_id, message)


def forward_webhook_to_cloud_run(request_body: bytes, signature: str, user_id: str = None) -> bool:
    """
    轉發 webhook 請求到 Cloud Run 後端
    
    Args:
        request_body: 原始請求主體
        signature: LINE 簽名
        user_id: 可選的用戶 ID（用於日誌）
        
    Returns:
        bool: 是否成功轉發
    """
    try:
        cloud_run_webhook_url = f"{CLOUD_RUN_URL}/webhook"
        
        # 將 bytes 解碼為 JSON 對象（僅用於調試）
        import json
        try:
            json_data = json.loads(request_body.decode('utf-8'))
            # 調試：打印請求信息
            print(f"[DEBUG] 轉發到後端 URL: {cloud_run_webhook_url}")
            print(f"[DEBUG] 請求體內容: {json.dumps(json_data, ensure_ascii=False, indent=2)}")
            print(f"[DEBUG] 簽名: {signature[:20]}...")
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            print(f"解析請求體失敗: {e}")
            return False
        
        headers = {
            'Content-Type': 'application/json',
            'X-Line-Signature': signature
        }
        
        # 直接使用原始 bytes 發送（後端需要原始請求體來驗證簽名）
        response = requests.post(
            cloud_run_webhook_url,
            data=request_body,
            headers=headers,
            timeout=30
        )
        
        if response.status_code == 200:
            if user_id:
                print(f"✓ 成功轉發 webhook 到 Cloud Run (用戶: {user_id})")
            return True
        else:
            print(f"✗ 轉發失敗: {response.status_code} - {response.text}")
            return False
    except Exception as e:
        print(f"✗ 轉發 webhook 到 Cloud Run 失敗: {e}")
        import traceback
        traceback.print_exc()
        return False


def forward_webhook_to_custom_recipe(request_body: bytes, signature: str, user_id: str = None) -> bool:
    """
    轉發 webhook 請求到自製食譜後端
    
    Args:
        request_body: 原始請求主體
        signature: LINE 簽名
        user_id: 可選的用戶 ID（用於日誌）
        
    Returns:
        bool: 是否成功轉發
    """
    if not CUSTOM_RECIPE_URL:
        print("警告: CUSTOM_RECIPE_URL 未設定，無法轉發到自製食譜後端")
        return False
    
    try:
        custom_recipe_webhook_url = f"{CUSTOM_RECIPE_URL}/callback"
        
        # 將 bytes 解碼為 JSON 對象（僅用於調試）
        import json
        try:
            json_data = json.loads(request_body.decode('utf-8'))
            # 調試：打印請求信息
            print(f"[DEBUG] 轉發到自製食譜後端 URL: {custom_recipe_webhook_url}")
            print(f"[DEBUG] 請求體內容: {json.dumps(json_data, ensure_ascii=False, indent=2)}")
            print(f"[DEBUG] 簽名: {signature[:20]}...")
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            print(f"解析請求體失敗: {e}")
            return False
        
        headers = {
            'Content-Type': 'application/json',
            'X-Line-Signature': signature
        }
        
        # 直接使用原始 bytes 發送（後端需要原始請求體來驗證簽名）
        response = requests.post(
            custom_recipe_webhook_url,
            data=request_body,
            headers=headers,
            timeout=30
        )
        
        if response.status_code == 200:
            if user_id:
                print(f"✓ 成功轉發 webhook 到自製食譜後端 (用戶: {user_id})")
            return True
        else:
            print(f"✗ 轉發失敗: {response.status_code} - {response.text}")
            return False
    except Exception as e:
        print(f"✗ 轉發 webhook 到自製食譜後端失敗: {e}")
        import traceback
        traceback.print_exc()
        return False


@app.route('/webhook', methods=['POST'])
def webhook():
    """
    LINE Webhook 端點（主入口）
    """
    # 取得請求簽名
    signature = request.headers.get('X-Line-Signature', '')
    if not signature:
        print("警告: 缺少簽名")
        abort(400)
    
    # 取得請求主體
    request_body = request.get_data()
    
    # 驗證簽名
    if not webhook_handler.verify_signature(request_body, signature):
        print("錯誤: 簽名驗證失敗")
        abort(401)
    
    # 解析事件
    try:
        request_data = request.get_json()
        events = webhook_handler.parse_webhook_event(request_data)
        
        for event in events:
            user_id = event.get('source', {}).get('userId', '')
            reply_token = event.get('replyToken', '')
            if not user_id or not reply_token: continue

            # 1. 處理中間層自身狀態切換
            event_type = event.get('type')
            if event_type == 'postback':
                postback_data = event.get('postback', {}).get('data', '')
                params = dict(parse_qsl(postback_data))
                
                # 處理偏好紀錄 (想煮/不想煮)
                action = params.get('action')
                recipe_id = params.get('id')
                
                if action == 'cook':
                    send_like_feedback(user_id, recipe_id)
                    line_client.reply_messages(reply_token, [{'type': 'text', 'text': "👨‍🍳 太棒了！已將您的偏好記錄下來！"}])
                    continue
                elif action == 'dislike':
                    # 不想煮 -> 這裡可以選擇是否也要紀錄 negative feedback，目前 RAG 只有 api/like
                    # 我們先回覆確認訊息
                    # 為了之後能從各後端拿推薦，這裡我們讓它繼續往下走，讓後端處理推薦
                    pass

                if postback_data.startswith('ai_function='):
                    function_name = postback_data.split('=')[1]
                    user_state[user_id] = f'ai_{function_name}'
                    # 模擬一個文字訊息給後端來啟動功能
                    event = {
                        'type': 'message',
                        'message': {'type': 'text', 'text': AI_FUNCTIONS[function_name] + '功能'},
                        'source': event['source']
                    }
            elif event_type == 'message' and event['message']['type'] == 'text':
                text = event['message']['text'].strip()
                if text == '主頁' or text.lower() == 'home':
                    handle_home_command(user_id, reply_token)
                    continue
                elif text == 'AI' or text.lower() == 'ai':
                    handle_ai_command(user_id, reply_token)
                    continue
                elif text == '自製' or text.lower() == 'custom':
                    handle_custom_command(user_id, reply_token)
                    continue

            # 2. 並行呼叫與路由策略
            event_type = event.get('type')
            is_image = (event_type == 'image' or (event_type == 'message' and event.get('message', {}).get('type') == 'image'))
            
            backends = []
            if is_image:
                # 圖片預設進入食譜流
                user_state[user_id] = 'ai_recipe'
                backends = [CLOUD_RUN_URL, CUSTOM_RECIPE_URL]
            elif event_type == 'message' and event['message']['type'] == 'text':
                text = event['message']['text'].strip()
                # 專屬指令檢查 (紀錄、查看、查詢、刪除)
                exclusive_keywords = ['紀錄', '查看', '查詢', '刪除']
                if any(k in text for k in exclusive_keywords):
                    print(f"[Routing] Exclusive route to Router for command: {text}")
                    backends = [CLOUD_RUN_URL]
                elif '食譜功能' in text:
                    # 食譜功能請求
                    backends = [CLOUD_RUN_URL, CUSTOM_RECIPE_URL]
                    print(f"[Routing] Recipe function -> {len(backends)} 個服務")
                else:
                    # 其他文字訊息，根據用戶狀態決定
                    current_state = user_state.get(user_id, 'main')
                    backends = [CLOUD_RUN_URL]
                    if current_state == 'ai_recipe':
                        backends.append(CUSTOM_RECIPE_URL)
                        print(f"[Routing] Recipe state -> {len(backends)} 個服務")
            elif event_type == 'postback':
                # 處理 postback 事件
                postback_data = event.get('postback', {}).get('data', '')
                print(f"[DEBUG] 處理 postback 事件: {postback_data}")

                if 'action=recommend' in postback_data:
                    # 推薦請求：發送到兩個服務
                    backends = [CLOUD_RUN_URL, CUSTOM_RECIPE_URL]
                    print(f"[Routing] Postback recommend -> {len(backends)} 個服務")
                elif 'action=cook' in postback_data or 'action=dislike' in postback_data:
                    # 回饋動作：主要由 middle 處理，但也可以發送到後端
                    backends = [CLOUD_RUN_URL, CUSTOM_RECIPE_URL]
                    print(f"[Routing] Postback feedback -> {len(backends)} 個服務")
                else:
                    # 其他 postback：發送到 Router
                    backends = [CLOUD_RUN_URL]
                    print(f"[Routing] Other postback -> 1 個服務")

                print(f"[DEBUG] 選擇的後端服務: {[url.split('/')[-1] for url in backends]}")
            else:
                # 其他事件類型，根據用戶狀態決定
                current_state = user_state.get(user_id, 'main')
                backends = [CLOUD_RUN_URL]
                if current_state == 'ai_recipe':
                    backends.append(CUSTOM_RECIPE_URL)

            # 檢查是否需要先發送"請稍等"訊息（食譜功能相關）
            should_send_wait = False
            if is_image or (event_type == 'postback' and 'action=recommend' in event.get('postback', {}).get('data', '')):
                should_send_wait = True

            # 如果是食譜相關，立即用 push 發送"請稍等"
            if should_send_wait:
                wait_message = "請稍等"
                push_success = line_client.send_text_message(user_id, wait_message)
                print(f"[DEBUG] Push '請稍等' 訊息: {'成功' if push_success else '失敗'}")

            all_messages = []
            results_data = {} # 暫存 API 回傳原始數據
            
            with concurrent.futures.ThreadPoolExecutor(max_workers=len(backends)) as executor:
                # 調用 API 並獲取回應對象
                def call_and_return_all(url, uid, ev):
                    if not url: return [], None
                    try:
                        print(f"[Parallel] Calling {url}/api/process_message...")
                        resp = requests.post(f"{url.rstrip('/')}/api/process_message", json={'user_id': uid, 'event': ev}, timeout=110)
                        if resp.status_code == 200:
                            data = resp.json()
                            msgs = data.get('messages', [])
                            print(f"[Parallel] {url} returned {len(msgs)} messages")
                            return msgs, data
                        else:
                            print(f"[Parallel] {url} failed with status {resp.status_code}")
                    except Exception as e:
                        print(f"[Parallel] {url} error: {e}")
                    return [], None

                future_to_url = {executor.submit(call_and_return_all, url, user_id, event): url for url in backends if url}
                for future in concurrent.futures.as_completed(future_to_url):
                    url = future_to_url[future]
                    try:
                        msgs, raw_data = future.result()
                        print(f"[DEBUG] {url.split('/')[-1]} 返回 {len(msgs)} 條訊息")
                        for i, msg in enumerate(msgs):
                            print(f"[DEBUG]   訊息 {i+1}: type={msg.get('type', 'unknown')}")
                        all_messages.extend(msgs)
                        if raw_data:
                            results_data[url] = raw_data
                    except Exception as e:
                        print(f"[ERROR] 處理 {url} 的結果時出錯: {e}")

            # --- 自動儲存 Dify 食譜到 RAG 向量庫 ---
            if CLOUD_RUN_URL in results_data:
                router_data = results_data[CLOUD_RUN_URL]
                # 檢查是否有生成的食譜文字需要儲存
                gen_recipe = router_data.get('generated_recipe_to_store') 
                if gen_recipe:
                    recipe_id = gen_recipe.get('id')
                    recipe_text = gen_recipe.get('text')
                    recipe_title = gen_recipe.get('title', 'Dify Recipe')
                    if recipe_id and recipe_text:
                        print(f"[Storage] Storing Dify recipe {recipe_id} to RAG...")
                        try:
                            requests.post(f"{RAG_API_URL}/api/store_recipe", json={
                                "recipe_id": recipe_id,
                                "text": recipe_text,
                                "title": recipe_title
                            }, timeout=10)
                        except Exception as e:
                            print(f"❌ Storage failed: {e}")

            # 3. 集中回覆
            print(f"[DEBUG] 總共收集到 {len(all_messages)} 條訊息")

            if all_messages:
                print(f"[DEBUG] 開始用 reply token 發送訊息給用戶 {user_id}")
                success = line_client.reply_messages(reply_token, all_messages)
                print(f"[DEBUG] Reply 發送結果: {'成功' if success else '失敗'}")
            else:
                print(f"[DEBUG] 沒有訊息，執行 fallback 邏輯")
                # Fallback: 如果都沒回傳，且沒進入主選單，顯示當前狀態提示
                current_state = user_state.get(user_id, 'main')
                if current_state == 'main':
                    handle_home_command(user_id, reply_token)
        
        return 'OK', 200
        
    except Exception as e:
        print(f"處理 Webhook 失敗: {str(e)}")
        import traceback
        traceback.print_exc()
        abort(500)


@app.route('/health', methods=['GET'])
def health():
    """健康檢查端點"""
    return {'status': 'ok', 'service': 'LINE Bot Middleware'}, 200


@app.route('/', methods=['GET'])
def index():
    """首頁"""
    return '''
    <h1>LINE Bot 中間層系統</h1>
    <p>Webhook 端點: /webhook</p>
    <p>健康檢查: /health</p>
    <p>狀態: 運行中</p>
    <h2>功能：</h2>
    <ul>
        <li>🏠 主選單</li>
        <li>🤖 AI 功能（食譜、紀錄、查看、刪除）</li>
        <li>🍳 自製食譜功能</li>
    </ul>
    '''


def main():
    """主函數"""
    import argparse
    
    parser = argparse.ArgumentParser(description='LINE Bot 中間層系統')
    port = int(os.getenv('PORT', 5000))
    parser.add_argument('--host', type=str, default='0.0.0.0',
                       help='伺服器主機 (預設: 0.0.0.0)')
    parser.add_argument('--port', type=int, default=port,
                       help='伺服器埠號 (預設: 從 PORT 環境變數或 5000)')
    parser.add_argument('--debug', action='store_true',
                       help='啟用除錯模式')
    
    args = parser.parse_args()
    
    print("=" * 60)
    print("LINE Bot 中間層系統")
    print("=" * 60)
    print(f"LINE Channel Secret: {LINE_CHANNEL_SECRET[:20] if LINE_CHANNEL_SECRET else '未設定'}...")
    print(f"Webhook URL: http://{args.host}:{args.port}/webhook")
    print(f"Cloud Run URL: {CLOUD_RUN_URL}")
    print("=" * 60)
    print("\n伺服器啟動中...")
    print("注意: LINE Webhook 需要 HTTPS，本地測試請使用 ngrok")
    print("\n")
    
    app.run(host=args.host, port=args.port, debug=args.debug)


if __name__ == '__main__':
    main()
