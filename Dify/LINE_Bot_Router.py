"""
LINE Bot 中繼器程式
作為功能路由中轉站，根據用戶輸入路由到不同的功能模組
"""

import os
import re
import time
import base64
import threading
from typing import Dict, Optional, List
from urllib.parse import parse_qsl
from flask import Flask, request, abort, jsonify
from dotenv import load_dotenv
from datetime import datetime, timedelta, timezone

# 載入環境變數
# override=False: Cloud Run 環境變數優先，本地開發時如果 .env 文件存在也會載入
load_dotenv('LINE.env', override=False)

# 導入 Line2Dify 模組的類和函數
from Line2Dify import (
    LINEWebhookHandler,
    LINEAPIClient,
    DifyAPIClient,
    MessageFlowController,
    ImageProcessor,
    temp_image_storage,
    temp_image_lock,
    user_recipe_storage,
    user_text_storage,
    user_food_storage,
    recipe_storage_lock
)

# 導入 record.py 模組的函數和類（用於記錄功能和查看功能）
from record import add_image_to_buffer, DatabaseManager, MYSQL_CONFIG, get_user_profile, db_manager

app = Flask(__name__)

# 1x1 透明 PNG（最小的有效PNG，用於404響應以減少資源消耗）
# Base64編碼的1x1透明PNG（約70字節）
EMPTY_PNG_B64 = 'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=='
EMPTY_PNG_DATA = base64.b64decode(EMPTY_PNG_B64)

# 台灣時區（UTC+8）
TAIWAN_TZ = timezone(timedelta(hours=8))

# 從環境變數讀取設定
LINE_CHANNEL_ACCESS_TOKEN = os.getenv('LINE_CHANNEL_ACCESS_TOKEN')
LINE_CHANNEL_SECRET = os.getenv('LINE_CHANNEL_SECRET')
DIFY_API_KEY = os.getenv('DIFY_API_KEY')
DIFY_API_ENDPOINT = os.getenv('DIFY_API_ENDPOINT', 'https://api.dify.ai')
# 第二個 DIFY 設定（用於按鈕推薦功能）
DIFY_API_KEY_SECOND = os.getenv('DIFY_API_KEY_SECOND')
DIFY_API_ENDPOINT_SECOND = os.getenv('DIFY_API_ENDPOINT_SECOND', 'https://api.dify.ai')

# 初始化 LINE 和 Dify 客戶端
webhook_handler = LINEWebhookHandler(LINE_CHANNEL_SECRET)
line_client = LINEAPIClient(LINE_CHANNEL_ACCESS_TOKEN)
dify_client = DifyAPIClient(DIFY_API_KEY, DIFY_API_ENDPOINT)
dify_client_second = DifyAPIClient(DIFY_API_KEY_SECOND, DIFY_API_ENDPOINT_SECOND)

# 初始化食譜功能控制器
recipe_flow_controller = MessageFlowController(dify_client, line_client)

# 使用 record.py 中的 db_manager 實例
# 注意：現在使用 get_connection() context manager，每次操作都會建立新連線
# 不再維護持久連線，避免 "MySQL server has gone away" 錯誤

# 用戶功能狀態管理（追蹤每個用戶當前使用的功能）
# 格式: {user_id: 'function_name'}
user_function_state = {}

# 追蹤用戶是否已發送"請稍等"消息（避免重複發送）
# 格式: {user_id: timestamp} - 記錄發送時間，10秒內不再發送
user_wait_message_sent = {}

# 用戶刪除記錄映射（追蹤每個用戶的記錄編號對應的記錄ID）
# 格式: {user_id: {編號: {'id': record_id, 'food_name': ..., 'quantity': ..., 'storage_time': ...}}}
user_delete_records_mapping = {}

# ===== Flex message builders =====
def _format_storage_time(storage_time) -> str:
    """Format storage_time consistently for UI."""
    if not storage_time:
        return "未指定"
    try:
        if isinstance(storage_time, datetime):
            return storage_time.strftime("%Y-%m-%d %H:%M:%S")
        return str(storage_time)
    except Exception:
        return "未指定"


def build_delete_records_flex(username: str, records: List[Dict], *, limit: int = 5, page: int = 1) -> Dict:
    """
    Build a Flex message listing records with one-click delete buttons.
    Supports pagination.
    """
    total = len(records or [])
    page = max(1, page)
    start = (page - 1) * limit
    end = start + limit
    shown = records[start:end] if records else []

    items = []
    for record in shown:
        food_name = record.get('food_name', '未知')
        quantity = record.get('quantity')
        storage_time = _format_storage_time(record.get('storage_time', ''))
        record_id = record.get('id')

        qty_text = f"{quantity}" if quantity is not None else "未指定"
        right = {
            "type": "text",
            "text": f"x {qty_text}",
            "size": "sm",
            "color": "#666666",
            "align": "end",
            "gravity": "center",
            "flex": 0
        }

        # If id is missing, show disabled-looking label instead of a button.
        if record_id is not None:
            right = {
                "type": "button",
                "style": "secondary",
                "height": "sm",
                "action": {
                    "type": "postback",
                    "label": "刪除",
                    "data": f"action=delete_record&id={record_id}&page={page}"
                }
            }

        items.append({
            "type": "box",
            "layout": "vertical",
            "spacing": "xs",
            "margin": "md",
            "contents": [
                {
                    "type": "box",
                    "layout": "horizontal",
                    "contents": [
                        {
                            "type": "text",
                            "text": food_name,
                            "weight": "bold",
                            "size": "md",
                            "wrap": True,
                            "flex": 1
                        },
                        right
                    ]
                },
                {
                    "type": "text",
                    "text": f"入庫時間：{storage_time}",
                    "size": "xs",
                    "color": "#999999",
                    "wrap": True
                },
                {"type": "separator", "margin": "md"}
            ]
        })

    footer_note = ""
    max_page = max(1, (total + limit - 1) // limit)
    if total > limit:
        footer_note = f"（第 {page}/{max_page} 頁，共 {total} 筆）"
    else:
        footer_note = f"（共 {total} 筆）"

    pager_buttons = []
    if page > 1:
        pager_buttons.append({
            "type": "button",
            "style": "secondary",
            "height": "sm",
            "action": {
                "type": "postback",
                "label": "上一頁",
                "data": f"action=delete_page&page={page-1}"
            }
        })
    if end < total:
        pager_buttons.append({
            "type": "button",
            "style": "primary",
            "height": "sm",
            "action": {
                "type": "postback",
                "label": "下一頁",
                "data": f"action=delete_page&page={page+1}"
            }
        })

    return {
        "type": "flex",
        "altText": "刪除記錄",
        "contents": {
            "type": "bubble",
            "size": "giga",
            "body": {
                "type": "box",
                "layout": "vertical",
                "spacing": "md",
                "contents": [
                    {
                        "type": "text",
                        "text": "🗑️ 刪除功能",
                        "weight": "bold",
                        "size": "lg"
                    },
                    {
                        "type": "text",
                        "text": f"📋 {username} 的記錄（共 {total} 筆）",
                        "size": "sm",
                        "color": "#666666",
                        "wrap": True
                    },
                    {"type": "separator", "margin": "md"},
                    *items,
                    *([
                        {
                            "type": "text",
                            "text": footer_note,
                            "size": "xs",
                            "color": "#999999",
                            "wrap": True
                        }
                    ] if footer_note else []),
                    *([
                        {
                            "type": "box",
                            "layout": "horizontal",
                            "spacing": "md",
                            "contents": pager_buttons
                        }
                    ] if pager_buttons else []),
                    {
                        "type": "text",
                        "text": "提示：也可直接輸入「退出」結束刪除模式",
                        "size": "xs",
                        "color": "#999999",
                        "wrap": True
                    }
                ]
            }
        }
    }


def _format_elapsed(storage_time) -> str:
    """Human-friendly elapsed time string (Taiwan time)."""
    if not storage_time:
        return ""
    try:
        dt = None
        if isinstance(storage_time, datetime):
            dt = storage_time
        else:
            # Try parse common formats
            raw = str(storage_time)
            for fmt in ["%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d"]:
                try:
                    dt = datetime.strptime(raw, fmt)
                    break
                except Exception:
                    continue
            if dt is None:
                try:
                    dt = datetime.fromisoformat(raw.replace('Z', '+00:00'))
                except Exception:
                    return ""

        # Normalize timezone
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=TAIWAN_TZ)
        now = datetime.now(TAIWAN_TZ)
        delta = now - dt.astimezone(TAIWAN_TZ)
        if delta.total_seconds() < 0:
            delta = timedelta(0)

        if delta.total_seconds() < 60:
            return "剛剛"
        minutes = int(delta.total_seconds() // 60)
        if minutes < 60:
            return f"{minutes} 分鐘前"
        hours = minutes // 60
        if hours < 24:
            return f"{hours} 小時前"
        days = hours // 24
        return f"{days} 天前"
    except Exception:
        return ""


def build_view_records_flex(username: str, records: List[Dict], *, limit: int = 5, page: int = 1) -> Dict:
    """Build a Flex message listing records for view mode with pagination."""
    total = len(records or [])
    page = max(1, page)
    start = (page - 1) * limit
    end = start + limit
    shown = records[start:end] if records else []

    items = []
    for record in shown:
        food_name = record.get('food_name', '未知')
        quantity = record.get('quantity')
        storage_time_raw = record.get('storage_time', '')
        storage_time = _format_storage_time(storage_time_raw)
        elapsed = _format_elapsed(storage_time_raw)

        qty_text = f"{quantity}" if quantity is not None else "未指定"
        subtitle = f"入庫時間：{storage_time}"
        if elapsed:
            subtitle += f"（{elapsed}）"

        items.append({
            "type": "box",
            "layout": "vertical",
            "spacing": "xs",
            "margin": "md",
            "contents": [
                {
                    "type": "box",
                    "layout": "horizontal",
                    "contents": [
                        {
                            "type": "text",
                            "text": food_name,
                            "weight": "bold",
                            "size": "md",
                            "wrap": True,
                            "flex": 1
                        },
                        {
                            "type": "text",
                            "text": f"x {qty_text}",
                            "size": "sm",
                            "color": "#666666",
                            "align": "end",
                            "gravity": "center",
                            "flex": 0
                        }
                    ]
                },
                {
                    "type": "text",
                    "text": subtitle,
                    "size": "xs",
                    "color": "#999999",
                    "wrap": True
                },
                {"type": "separator", "margin": "md"}
            ]
        })

    footer_note = ""
    max_page = max(1, (total + limit - 1) // limit)
    if total > limit:
        footer_note = f"（第 {page}/{max_page} 頁，共 {total} 筆）"
    else:
        footer_note = f"（共 {total} 筆）"

    # Pagination buttons
    pager_buttons = []
    if page > 1:
        pager_buttons.append({
            "type": "button",
            "style": "secondary",
            "height": "sm",
            "action": {
                "type": "postback",
                "label": "上一頁",
                "data": f"action=view_page&page={page-1}"
            }
        })
    if end < total:
        pager_buttons.append({
            "type": "button",
            "style": "primary",
            "height": "sm",
            "action": {
                "type": "postback",
                "label": "下一頁",
                "data": f"action=view_page&page={page+1}"
            }
        })

    return {
        "type": "flex",
        "altText": "查看記錄",
        "contents": {
            "type": "bubble",
            "size": "giga",
            "body": {
                "type": "box",
                "layout": "vertical",
                "spacing": "md",
                "contents": [
                    {
                        "type": "text",
                        "text": "📋 查看功能",
                        "weight": "bold",
                        "size": "lg"
                    },
                    {
                        "type": "text",
                        "text": f"{username} 的記錄（共 {total} 筆）",
                        "size": "sm",
                        "color": "#666666",
                        "wrap": True
                    },
                    {"type": "separator", "margin": "md"},
                    *items,
                    *([
                        {
                            "type": "text",
                            "text": footer_note,
                            "size": "xs",
                            "color": "#999999",
                            "wrap": True
                        }
                    ] if footer_note else []),
                    *([
                        {
                            "type": "box",
                            "layout": "horizontal",
                            "spacing": "md",
                            "contents": pager_buttons
                        }
                    ] if pager_buttons else []),
                    {
                        "type": "text",
                        "text": "提示：輸入「記錄功能 / 刪除功能」可切換模式",
                        "size": "xs",
                        "color": "#999999",
                        "wrap": True
                    }
                ]
            }
        }
    }


# 功能關鍵字映射
FUNCTION_KEYWORDS = {
    'recipe': ['食譜功能', '食譜', 'recipe', 'Recipe', 'RECIPE', '開始食譜', '使用食譜', '食譜模式'],
    'record': ['記錄功能', '記錄', 'record', 'Record', 'RECORD', '開始記錄', '使用記錄', '記錄模式'],
    'view': ['查看功能', '查看', 'view', 'View', 'VIEW', '查詢', '查詢功能', '我的記錄', '記錄查詢'],
    'delete': ['刪除功能', '刪除', 'delete', 'Delete', 'DELETE', '消耗', '消耗功能', '使用'],
    'help': ['幫助', 'help', 'Help', '功能', '選單', 'menu', 'Menu', '說明'],
    'exit': ['退出', 'exit', 'Exit', '結束', '取消', 'cancel', 'Cancel']
}


def query_user_food_records(user_id: str) -> List[Dict]:
    """
    查詢指定用戶的食物記錄（使用 user_id）
    
    Args:
        user_id: 使用者 ID（LINE user_id）
        
    Returns:
        List[Dict]: 食物記錄列表，每個記錄包含 food_name, quantity, storage_time
    """
    try:
        # 使用 context manager 每次建立新連線（避免連線過期問題）
        with db_manager.get_connection() as conn:
            with conn.cursor() as cursor:
                # 查詢該用戶的所有記錄（使用 user_id）
                sql = """
                SELECT 
                    id,
                    food_name AS 食品,
                    quantity AS 數量,
                    storage_time AS 購買時間
                FROM foods
                WHERE username = %s
                ORDER BY storage_time ASC
                """
                cursor.execute(sql, (user_id,))
                results = cursor.fetchall()
                
                # 轉換為字典列表
                records = []
                for row in results:
                    records.append({
                        'id': row[0],  # id
                        'food_name': row[1],  # 食品
                        'quantity': row[2],   # 數量
                        'storage_time': row[3]  # 購買時間
                    })
                
                return records
            
    except Exception as e:
        print(f"✗ 查詢資料庫失敗: {e}")
        import traceback
        traceback.print_exc()
        return []


def query_food_records_by_name(username: str, food_name: str) -> List[Dict]:
    """
    查詢指定用戶的特定食品記錄（按時間升序，最舊的在前）
    
    Args:
        username: 使用者名稱
        food_name: 食品名稱
        
    Returns:
        List[Dict]: 食物記錄列表，每個記錄包含 id, food_name, quantity, storage_time
    """
    try:
        # 使用 context manager 每次建立新連線（避免連線過期問題）
        with db_manager.get_connection() as conn:
            with conn.cursor() as cursor:
                # 查詢該用戶的特定食品記錄，按時間升序（最舊的在前）
                sql = """
                SELECT 
                    id,
                    food_name,
                    quantity,
                    storage_time
                FROM foods
                WHERE username = %s AND food_name = %s
                ORDER BY storage_time ASC
                """
                cursor.execute(sql, (username, food_name))
                results = cursor.fetchall()
                
                # 轉換為字典列表
                records = []
                for row in results:
                    records.append({
                        'id': row[0],
                        'food_name': row[1],
                        'quantity': row[2],
                        'storage_time': row[3]
                    })
                
                return records
            
    except Exception as e:
        print(f"✗ 查詢資料庫失敗: {e}")
        import traceback
        traceback.print_exc()
        return []


def deduct_food_quantity(username: str, food_name: str, deduct_amount: float) -> Dict:
    """
    扣除食品數量（從最舊的記錄開始）
    
    Args:
        username: 使用者名稱
        food_name: 食品名稱
        deduct_amount: 要扣除的數量
        
    Returns:
        Dict: 包含 success, remaining_amount, updated_records, deleted_records
    """
    try:
        # 查詢該食品的所有記錄（按時間升序）
        records = query_food_records_by_name(username, food_name)
        
        if not records:
            return {
                'success': False,
                'message': f'找不到 {food_name} 的記錄',
                'remaining_amount': deduct_amount,
                'updated_records': [],
                'deleted_records': []
            }
        
        remaining_amount = deduct_amount
        updated_records = []
        deleted_records = []
        
        # 使用 context manager 每次建立新連線（避免連線過期問題）
        with db_manager.get_connection() as conn:
            with conn.cursor() as cursor:
                for record in records:
                    if remaining_amount <= 0:
                        break
                    
                    record_id = record['id']
                    # 確保數量轉換為浮點數
                    current_quantity = float(record['quantity']) if record['quantity'] is not None else None
                    
                    # 如果數量為 NULL，跳過
                    if current_quantity is None:
                        continue
                    
                    if current_quantity <= remaining_amount:
                        # 當前記錄的數量不足或剛好，刪除這筆記錄
                        delete_sql = "DELETE FROM foods WHERE id = %s"
                        cursor.execute(delete_sql, (record_id,))
                        deleted_records.append({
                            'id': record_id,
                            'food_name': food_name,
                            'quantity': float(current_quantity)
                        })
                        remaining_amount -= current_quantity
                    else:
                        # 當前記錄的數量足夠，更新數量
                        new_quantity = float(current_quantity - remaining_amount)
                        update_sql = "UPDATE foods SET quantity = %s WHERE id = %s"
                        cursor.execute(update_sql, (new_quantity, record_id))
                        updated_records.append({
                            'id': record_id,
                            'food_name': food_name,
                            'old_quantity': float(current_quantity),
                            'new_quantity': float(new_quantity),
                            'deducted': float(remaining_amount)
                        })
                        remaining_amount = 0
                
                conn.commit()
        
        return {
            'success': True,
            'remaining_amount': remaining_amount,
            'updated_records': updated_records,
            'deleted_records': deleted_records,
            'message': f'成功處理 {food_name} 的扣除'
        }
        
    except Exception as e:
        print(f"✗ 扣除數量失敗: {e}")
        import traceback
        traceback.print_exc()
        return {
            'success': False,
            'message': f'扣除數量時發生錯誤: {str(e)}',
            'remaining_amount': deduct_amount
        }


def delete_food_record_by_id(record_id: int) -> Dict:
    """
    根據記錄 ID 刪除食物記錄
    
    Args:
        record_id: 記錄 ID
    
    Returns:
        Dict: 包含 success, message, deleted_record
    """
    try:
        # 使用 context manager 每次建立新連線（避免連線過期問題）
        with db_manager.get_connection() as conn:
            with conn.cursor() as cursor:
                # 先查詢記錄信息（用於返回）
                select_sql = "SELECT id, food_name, quantity, storage_time FROM foods WHERE id = %s"
                cursor.execute(select_sql, (record_id,))
                record = cursor.fetchone()
                
                if not record:
                    return {
                        'success': False,
                        'message': f'找不到 ID {record_id} 的記錄'
                    }
                
                # 刪除記錄
                delete_sql = "DELETE FROM foods WHERE id = %s"
                cursor.execute(delete_sql, (record_id,))
                conn.commit()
                
                deleted_record = {
                    'id': record[0],
                    'food_name': record[1],
                    'quantity': record[2],
                    'storage_time': record[3]
                }
                
                return {
                    'success': True,
                    'message': f'成功刪除記錄 ID {record_id}',
                    'deleted_record': deleted_record
                }
            
    except Exception as e:
        print(f"✗ 刪除記錄失敗: {e}")
        import traceback
        traceback.print_exc()
        return {
            'success': False,
            'message': f'刪除記錄時發生錯誤: {str(e)}'
        }


def remove_markdown_headers(text: str) -> str:
    """
    移除 Markdown 標題標記（# 符號）
    
    Args:
        text: 包含 Markdown 標記的文字
        
    Returns:
        str: 清理後的文字
    """
    # 使用正則表達式移除行首的 # 符號
    return re.sub(r'^#+\s*', '', text, flags=re.MULTILINE)


def parse_consumption_input(text: str) -> List[Dict]:
    """
    解析用戶輸入的消耗信息
    
    Args:
        text: 用戶輸入的文字，例如："蘋果 2個\n橘子 1個"
        
    Returns:
        List[Dict]: 消耗項目列表，每個項目包含 'food_name' 和 'quantity'
    """
    consumption_items = []
    
    # 按換行符分割
    lines = text.strip().split('\n')
    
    for line in lines:
        line = line.strip()
        if not line:
            continue
        
        # 使用正則表達式匹配：食物名稱 + 數量
        # 匹配模式：食物名稱（中文、英文、數字）+ 數量（數字 + 單位）
        pattern = r'([^\d\s]+?)\s*(\d+(?:\.\d+)?)\s*(個|件|包|盒|瓶|罐|條|根|片|塊|斤|公斤|克|kg|g)?'
        match = re.search(pattern, line)
        
        if match:
            food_name = match.group(1).strip()
            quantity_str = match.group(2)
            
            try:
                quantity = float(quantity_str)
                consumption_items.append({
                    'food_name': food_name,
                    'quantity': quantity
                })
            except ValueError:
                print(f"警告: 無法解析數量 '{quantity_str}'")
    
    return consumption_items


class FunctionRouter:
    """功能路由器"""
    
    def __init__(self):
        self.functions = {
            'recipe': self.handle_recipe_function,
            'record': self.handle_record_function,
            'view': self.handle_view_function,
            'delete': self.handle_delete_function,
            'help': self.handle_help_function,
            'exit': self.handle_exit_function
        }
    
    def detect_function(self, text: str) -> Optional[str]:
        """
        檢測文字訊息中的功能關鍵字
        
        Args:
            text: 用戶輸入的文字
            
        Returns:
            Optional[str]: 功能名稱，如果未檢測到則返回 None
        """
        text_lower = text.strip().lower()
        
        for func_name, keywords in FUNCTION_KEYWORDS.items():
            for keyword in keywords:
                if keyword.lower() in text_lower:
                    return func_name
        
        return None
    
    def handle_recipe_function(self, user_id: str, reply_token: Optional[str], client=None) -> bool:
        """
        處理食譜功能啟用
        
        Args:
            user_id: 用戶 ID
            reply_token: 回覆 Token
            client: LINE API 客戶端
            
        Returns:
            bool: 是否成功處理
        """
        # 設定用戶功能狀態為食譜模式
        # 提示消息已移至 middle.py 處理
        user_function_state[user_id] = 'recipe'
        return True
    
    def handle_record_function(self, user_id: str, reply_token: Optional[str], client=None) -> bool:
        """
        處理記錄功能啟用
        
        Args:
            user_id: 用戶 ID
            reply_token: 回覆 Token
            client: LINE API 客戶端
            
        Returns:
            bool: 是否成功處理
        """
        c = client or line_client
        # 設定用戶功能狀態為記錄模式
        user_function_state[user_id] = 'record'
        
        guide_message = (
            "📝 記錄功能已啟用！\n\n"
            "📸 請上傳您想要記錄的食物圖片，我會為您：\n"
            "• 記錄食物名稱\n"
            "• 記錄入庫時間\n"
            "• 保存到資料庫\n\n"
            "請直接上傳食物圖片即可開始記錄！\n\n"
            "💡 提示：\n"
            "• 輸入其他功能關鍵字可切換功能\n"
            "• 輸入「退出」可結束記錄功能"
        )
        
        if reply_token:
            return c.reply_message(reply_token, guide_message)
        else:
            return c.send_text_message(user_id, guide_message)
    
    def handle_view_function(self, user_id: str, reply_token: Optional[str], client=None) -> bool:
        """
        處理查看功能
        
        Args:
            user_id: 用戶 ID
            reply_token: 回覆 Token
            client: LINE API 客戶端
            
        Returns:
            bool: 是否成功處理
        """
        c = client or line_client
        try:
            # 獲取用戶資訊（用於顯示）
            user_profile = get_user_profile(user_id)
            username = user_profile.get('displayName', '未知用戶') if user_profile else '未知用戶'
            
            # 查詢資料庫（使用 user_id）
            records = query_user_food_records(user_id)
            
            if not records:
                # 沒有記錄
                message = (
                    f"📋 {username} 的記錄\n\n"
                    "目前沒有任何記錄。\n"
                    "使用「記錄功能」來記錄食物吧！"
                )
            else:
                # 有記錄：改用 Flex message 讓清單更好讀（含分頁）
                message = build_view_records_flex(username, records, limit=5, page=1)
            
            # 查看功能執行完後，清除用戶狀態（回到初始狀態）
            if user_id in user_function_state:
                del user_function_state[user_id]
            
            # 發送訊息
            if reply_token:
                return c.reply_message(reply_token, message)
            else:
                return c.send_text_message(user_id, message)
                
        except Exception as e:
            print(f"處理查看功能失敗: {e}")
            import traceback
            traceback.print_exc()
            # 即使出錯，也清除用戶狀態
            if user_id in user_function_state:
                del user_function_state[user_id]
            error_msg = "查詢記錄時發生錯誤，請稍後再試。"
            if reply_token:
                return c.reply_message(reply_token, error_msg)
            else:
                return c.send_text_message(user_id, error_msg)
    
    def handle_delete_function(self, user_id: str, reply_token: Optional[str], client=None) -> bool:
        """
        處理刪除功能啟用
        
        Args:
            user_id: 用戶 ID
            reply_token: 回覆 Token
            client: LINE API 客戶端
            
        Returns:
            bool: 是否成功處理
        """
        c = client or line_client
        try:
            # 獲取用戶資訊（用於顯示）
            user_profile = get_user_profile(user_id)
            username = user_profile.get('displayName', '未知用戶') if user_profile else '未知用戶'
            
            # 設定用戶功能狀態為刪除模式
            user_function_state[user_id] = 'delete'
            
            # 查詢資料庫，顯示記錄清單（使用 user_id）
            records = query_user_food_records(user_id)
            
            if not records:
                # 沒有記錄
                message = (
                    f"🗑️ 刪除功能已啟用！\n\n"
                    f"📋 {username} 的記錄\n\n"
                    "目前沒有任何記錄。\n"
                    "使用「記錄功能」來記錄食物吧！"
                )
            else:
                # 有記錄：改用 Flex message + 一鍵刪除按鈕（含分頁）
                message = build_delete_records_flex(username, records, limit=5, page=1)
            
            # 發送訊息
            if reply_token:
                return c.reply_message(reply_token, message)
            else:
                return c.send_text_message(user_id, message)
                
        except Exception as e:
            print(f"處理刪除功能失敗: {e}")
            import traceback
            traceback.print_exc()
            error_msg = "啟用刪除功能時發生錯誤，請稍後再試。"
            if reply_token:
                return c.reply_message(reply_token, error_msg)
            else:
                return c.send_text_message(user_id, error_msg)
    
    def handle_help_function(self, user_id: str, reply_token: Optional[str], client=None) -> bool:
        """
        處理幫助功能
        
        Args:
            user_id: 用戶 ID
            reply_token: 回覆 Token
            client: LINE API 客戶端
            
        Returns:
            bool: 是否成功處理
        """
        c = client or line_client
        help_message = (
            "📋 可用功能列表：\n\n"
            "🍳 食譜功能 - 輸入「食譜功能」或「食譜」\n"
            "   上傳食物圖片，獲得詳細食譜和烹飪建議\n"
            "   （持續模式：可持續上傳圖片）\n\n"
            "📝 記錄功能 - 輸入「記錄功能」或「記錄」\n"
            "   上傳食物圖片，記錄食物名稱和入庫時間\n"
            "   （持續模式：可持續上傳圖片）\n\n"
            "🔍 查看功能 - 輸入「查看功能」或「查看」\n"
            "   查看您的食物記錄列表\n"
            "   （執行完後自動返回初始狀態）\n\n"
            "🗑️ 刪除功能 - 輸入「刪除功能」或「刪除」\n"
            "   記錄食品消耗，從最舊的記錄開始扣除\n"
            "   （持續模式：可持續輸入消耗信息）\n\n"
            "💡 功能切換：\n"
            "   在任何持續模式下，輸入其他功能關鍵字即可切換功能\n\n"
            "❓ 幫助 - 輸入「幫助」或「help」\n"
            "   查看此功能列表\n\n"
            "❌ 退出 - 輸入「退出」或「exit」\n"
            "   結束當前功能模式，返回初始狀態"
        )
        
        if reply_token:
            return c.reply_message(reply_token, help_message)
        else:
            return c.send_text_message(user_id, help_message)
    
    def handle_exit_function(self, user_id: str, reply_token: Optional[str], client=None) -> bool:
        """
        處理退出功能
        
        Args:
            user_id: 用戶 ID
            reply_token: 回覆 Token
            client: LINE API 客戶端
            
        Returns:
            bool: 是否成功處理
        """
        c = client or line_client
        if user_id in user_function_state:
            current_function = user_function_state[user_id]
            del user_function_state[user_id]
            
            # 清除記錄映射（如果是在刪除模式下）
            if current_function == 'delete' and user_id in user_delete_records_mapping:
                del user_delete_records_mapping[user_id]
            
            function_name_map = {
                'recipe': '食譜',
                'record': '記錄',
                'view': '查看',
                'delete': '刪除'
            }
            function_name = function_name_map.get(current_function, current_function)
            exit_message = f"已退出 {function_name} 功能模式。\n\n輸入「幫助」查看可用功能。"
        else:
            exit_message = "您目前沒有啟用任何功能模式。\n\n輸入「幫助」查看可用功能。"
        
        if reply_token:
            return c.reply_message(reply_token, exit_message)
        else:
            return c.send_text_message(user_id, exit_message)
    
    def route_message(self, user_id: str, text: str, reply_token: Optional[str], client=None) -> bool:
        """
        路由文字訊息到對應功能
        
        Args:
            user_id: 用戶 ID
            text: 文字訊息
            reply_token: 回覆 Token
            client: LINE API 客戶端
            
        Returns:
            bool: 是否成功處理
        """
        c = client or line_client
        # 檢測功能關鍵字（優先檢查是否要切換功能）
        function_name = self.detect_function(text)
        
        # 檢查用戶當前功能狀態
        current_function = user_function_state.get(user_id)
        
        # 如果檢測到退出功能，優先處理退出
        if function_name == 'exit':
            return self.functions['exit'](user_id, reply_token, client=client)
        
        # 如果檢測到功能關鍵字，則切換功能
        if function_name and function_name in self.functions:
            # 如果用戶在持續模式下，且輸入的是其他功能關鍵字，則切換功能
            if current_function and current_function != function_name:
                # 切換到新功能
                return self.functions[function_name](user_id, reply_token, client=client)
            elif not current_function:
                # 用戶不在任何模式下，啟用新功能
                return self.functions[function_name](user_id, reply_token, client=client)
            else:
                # 用戶已在該模式下，重新顯示提示（可選）
                return self.functions[function_name](user_id, reply_token, client=client)
        
        # 如果用戶在持續模式下，處理該模式的輸入
        if current_function == 'delete':
            # 用戶在刪除模式下，處理消耗輸入
            return self.handle_delete_consumption(user_id, text, reply_token, client=client)
        elif current_function in ['recipe', 'record']:
            # 食譜和記錄功能持續模式，但文字輸入應該提示上傳圖片
            # 如果輸入的是功能關鍵字，上面已經處理了
            # 這裡處理其他文字輸入
            guide_message = (
                f"您目前在「{self._get_function_name(current_function)}」模式下。\n\n"
                "請上傳圖片以使用該功能，或輸入其他功能關鍵字切換功能。\n"
                "輸入「退出」可結束當前功能模式。"
            )
            if reply_token:
                return c.reply_message(reply_token, guide_message)
            else:
                return c.send_text_message(user_id, guide_message)
        
        # 未識別的功能，顯示幫助訊息
        unknown_message = (
            "❓ 未識別的功能指令。\n\n"
            "請輸入「幫助」查看可用功能列表。"
        )
        if reply_token:
            return c.reply_message(reply_token, unknown_message)
        else:
            return c.send_text_message(user_id, unknown_message)
    
    def _get_function_name(self, function_key: str) -> str:
        """獲取功能的中文名稱"""
        function_name_map = {
            'recipe': '食譜',
            'record': '記錄',
            'view': '查看',
            'delete': '刪除'
        }
        return function_name_map.get(function_key, function_key)
    
    def handle_delete_consumption(self, user_id: str, text: str, reply_token: Optional[str], client=None) -> bool:
        """
        處理刪除模式下的消耗輸入
        
        Args:
            user_id: 用戶 ID
            text: 用戶輸入的消耗信息
            reply_token: 回覆 Token
            client: LINE API 客戶端
            
        Returns:
            bool: 是否成功處理
        """
        c = client or line_client
        # 先檢查是否輸入功能關鍵字（允許在刪除模式下切換功能）
        function_name = self.detect_function(text)
        if function_name and function_name in self.functions:
            # 如果是退出功能，直接處理
            if function_name == 'exit':
                return self.functions['exit'](user_id, reply_token, client=client)
            # 如果是其他功能關鍵字，切換功能
            elif function_name != 'delete':
                return self.functions[function_name](user_id, reply_token, client=client)
        
        try:
            # 獲取用戶資訊（USERNAME）
            user_profile = get_user_profile(user_id)
            username = user_profile.get('displayName', '未知用戶') if user_profile else '未知用戶'
            
            # 檢查是否為按編號刪除（格式：純數字 或 "數字 數字"）
            text_stripped = text.strip()
            
            # 匹配純數字（例如：3）或 "數字 數字"（例如：3 1）
            number_pattern = r'^(\d+)(?:\s+(\d+(?:\.\d+)?))?$'
            number_match = re.match(number_pattern, text_stripped)
            
            if number_match:
                # 按編號刪除
                record_number = int(number_match.group(1))
                deduct_amount = float(number_match.group(2)) if number_match.group(2) else None
                
                # 檢查用戶是否有記錄映射
                if user_id not in user_delete_records_mapping:
                    error_msg = (
                        "❌ 找不到記錄映射。\n\n"
                        "請重新輸入「刪除功能」查看記錄列表。"
                    )
                    if reply_token:
                        return c.reply_message(reply_token, error_msg)
                    else:
                        return c.send_text_message(user_id, error_msg)
                
                # 獲取記錄映射
                record_mapping = user_delete_records_mapping[user_id]
                
                if record_number not in record_mapping:
                    error_msg = f"❌ 找不到編號 {record_number} 的記錄。\n\n請重新輸入「刪除功能」查看記錄列表。"
                    if reply_token:
                        return c.reply_message(reply_token, error_msg)
                    else:
                        return c.send_text_message(user_id, error_msg)
                
                # 獲取記錄信息
                record_info = record_mapping[record_number]
                record_id = record_info['id']
                food_name = record_info['food_name']
                current_quantity = record_info.get('quantity')
                
                # 如果指定了數量，檢查是否可以部分刪除
                if deduct_amount is not None and current_quantity is not None:
                    current_quantity_float = float(current_quantity)
                    
                    if deduct_amount >= current_quantity_float:
                        # 完全刪除記錄
                        result = delete_food_record_by_id(record_id)
                        if result['success']:
                            # 從映射中移除
                            del record_mapping[record_number]
                            success_msg = f"✅ 已刪除編號 {record_number} 的記錄：{food_name}\n"
                            if reply_token:
                                return c.reply_message(reply_token, success_msg)
                            else:
                                return c.send_text_message(user_id, success_msg)
                        else:
                            error_msg = f"❌ 刪除失敗：{result.get('message', '未知錯誤')}"
                            if reply_token:
                                return c.reply_message(reply_token, error_msg)
                            else:
                                return c.send_text_message(user_id, error_msg)
                    else:
                        # 部分扣除：更新數量
                        new_quantity = current_quantity_float - deduct_amount
                        try:
                            # 使用 context manager 每次建立新連線（避免連線過期問題）
                            with db_manager.get_connection() as conn:
                                with conn.cursor() as cursor:
                                    update_sql = "UPDATE foods SET quantity = %s WHERE id = %s"
                                    cursor.execute(update_sql, (new_quantity, record_id))
                                    conn.commit()
                                    
                                    # 更新映射中的數量
                                    record_info['quantity'] = new_quantity
                                    
                                    success_msg = (
                                        f"✅ 已更新編號 {record_number} 的記錄：{food_name}\n"
                                        f"   數量：{current_quantity} -> {new_quantity} (扣除 {deduct_amount})"
                                    )
                                    if reply_token:
                                        return c.reply_message(reply_token, success_msg)
                                    else:
                                        return c.send_text_message(user_id, success_msg)
                        except Exception as e:
                            print(f"更新記錄失敗: {e}")
                            error_msg = f"❌ 更新記錄失敗：{str(e)}"
                            if reply_token:
                                return c.reply_message(reply_token, error_msg)
                            else:
                                return c.send_text_message(user_id, error_msg)
                else:
                    # 沒有指定數量，完全刪除記錄
                    result = delete_food_record_by_id(record_id)
                    if result['success']:
                        # 從映射中移除
                        del record_mapping[record_number]
                        success_msg = f"✅ 已刪除編號 {record_number} 的記錄：{food_name}"
                        if reply_token:
                            return c.reply_message(reply_token, success_msg)
                        else:
                            return c.send_text_message(user_id, success_msg)
                    else:
                        error_msg = f"❌ 刪除失敗：{result.get('message', '未知錯誤')}"
                        if reply_token:
                            return c.reply_message(reply_token, error_msg)
                        else:
                            return c.send_text_message(user_id, error_msg)
            
            # 如果不是編號格式，按原來的邏輯處理（食品名稱 + 數量）
            # 解析消耗信息
            consumption_items = parse_consumption_input(text)
            
            if not consumption_items:
                # 無法解析消耗信息
                error_msg = (
                    "❌ 無法解析消耗信息。\n\n"
                    "刪除方式：\n"
                    "1️⃣ 按編號刪除：輸入編號（例如：3）\n"
                    "2️⃣ 按食品名稱刪除：輸入食品名稱 數量（例如：蘋果 2個）"
                )
                if reply_token:
                    return c.reply_message(reply_token, error_msg)
                else:
                    return c.send_text_message(user_id, error_msg)
            
            # 處理每個消耗項目
            result_messages = []
            all_success = True
            
            for item in consumption_items:
                food_name = item['food_name']
                deduct_amount = item['quantity']
                
                # 扣除數量
                result = deduct_food_quantity(username, food_name, deduct_amount)
                
                if result['success']:
                    # 構建結果訊息
                    item_message = f"✅ {food_name} - 扣除 {float(deduct_amount)}\n"
                    
                    # 顯示更新的記錄
                    if result['updated_records']:
                        for record in result['updated_records']:
                            old_qty = float(record['old_quantity'])
                            new_qty = float(record['new_quantity'])
                            item_message += f"  更新：記錄 ID {record['id']} ({old_qty} -> {new_qty})\n"
                    
                    # 顯示刪除的記錄
                    if result['deleted_records']:
                        for record in result['deleted_records']:
                            qty = float(record['quantity'])
                            item_message += f"  刪除：記錄 ID {record['id']} (數量: {qty})\n"
                    
                    # 如果還有剩餘數量無法扣除
                    if result['remaining_amount'] > 0:
                        remaining = float(result['remaining_amount'])
                        item_message += f"  ⚠️ 警告：還需要扣除 {remaining}，但庫存不足\n"
                        all_success = False
                    
                    result_messages.append(item_message)
                else:
                    # 處理失敗
                    error_msg = f"❌ {food_name} - {result.get('message', '處理失敗')}\n"
                    result_messages.append(error_msg)
                    all_success = False
            
            # 組合所有結果訊息
            if all_success:
                final_message = "✅ 消耗記錄完成！\n\n"
            else:
                final_message = "⚠️ 消耗記錄處理完成（部分項目可能有問題）\n\n"
            
            final_message += "\n".join(result_messages)
            final_message += "\n\n輸入「查看功能」查看更新後的記錄。"
            
            # 發送訊息
            if reply_token:
                return c.reply_message(reply_token, final_message)
            else:
                return c.send_text_message(user_id, final_message)
                
        except Exception as e:
            print(f"處理消耗輸入失敗: {e}")
            import traceback
            traceback.print_exc()
            error_msg = "處理消耗信息時發生錯誤，請稍後再試。"
            if reply_token:
                return c.reply_message(reply_token, error_msg)
            else:
                return c.send_text_message(user_id, error_msg)


# 初始化路由器
router = FunctionRouter()


@app.route('/webhook', methods=['POST'])
def webhook():
    """
    LINE Webhook 端點（主入口）
    
    處理流程：
    1. 驗證 Webhook 簽名
    2. 解析事件
    3. 根據事件類型和用戶狀態路由到對應功能
    4. 回覆 200 OK
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
        
        # 收集圖片事件和其他事件
        image_events_by_user = {}
        text_events = []
        postback_events = []
        other_events = []
        
        for event in events:
            # 處理 Postback 事件
            if event.get('type') == 'postback':
                postback_data = event.get('postback', {}).get('data', '')
                user_id = event.get('source', {}).get('userId', '')
                reply_token = event.get('replyToken', '')
                
                if postback_data.startswith('recipe_select='):
                    postback_events.append({
                        'user_id': user_id,
                        'reply_token': reply_token,
                        'data': postback_data
                    })
                    continue
                elif postback_data.startswith('action=recommend'):
                    postback_events.append({
                        'user_id': user_id,
                        'reply_token': reply_token,
                        'data': postback_data,
                        'type': 'recommend'
                    })
                    continue
            
            # 處理圖片事件
            image_event = webhook_handler.handle_image_event(event)
            if image_event:
                user_id = image_event.get('user_id')
                if user_id:
                    if user_id not in image_events_by_user:
                        image_events_by_user[user_id] = []
                    image_events_by_user[user_id].append(image_event)
            else:
                # 處理文字訊息
                message_event = webhook_handler.handle_message_event(event)
                if message_event:
                    message_type = message_event['message_type']
                    if message_type == 'text':
                        text_events.append(message_event)
                    else:
                        other_events.append(message_event)
                else:
                    other_events.append(event)
        
        # 處理 Postback 事件
        for postback_event in postback_events:
            user_id = postback_event['user_id']
            reply_token = postback_event.get('reply_token')  # 獲取 reply_token
            data = postback_event['data']
            event_type = postback_event.get('type', 'recipe_select')

            print(f"收到 Postback 事件（用戶: {user_id}, data: {data}, type: {event_type}, reply_token: {'有' if reply_token else '無'}）")

            try:
                if event_type == 'recommend':
                    # 推薦功能應該通過 process_message_api 處理，不在 webhook 中直接處理
                    print(f"[資訊] 收到推薦請求，但跳過 webhook 直接處理（應通過 process_message_api）")
                    continue
                else:
                    # 處理食譜選擇
                    # 解析選擇的食譜編號
                    recipe_num = int(data.split('=')[1])
                
                with recipe_storage_lock:
                    if user_id in user_recipe_storage:
                        recipes = user_recipe_storage[user_id]
                        recipe_key = f'dish_{recipe_num}'
                        
                        if recipe_key in recipes:
                            recipe_content = recipes[recipe_key]
                            
                            # 去除食譜中的 # 標記
                            cleaned_recipe = remove_markdown_headers(recipe_content)
                            
                            # 獲取 text（碳足跡計算結果）
                            text_content = user_text_storage.get(user_id, '')
                            
                            # 組合消息：text + 分隔符 + 清理後的食譜內容
                            if text_content:
                                message = text_content + "\n" + "=" * 25 + "\n" + cleaned_recipe
                            else:
                                message = cleaned_recipe
                            
                            # 使用 reply_token 發送（如果有的話）
                            if reply_token:
                                line_client.reply_message(reply_token, message)
                            else:
                                line_client.send_text_message(user_id, message)
                            print(f"已發送食譜 {recipe_num} 給用戶 {user_id}")
                        else:
                            error_msg = f"找不到編號 {recipe_num} 的食譜"
                            if reply_token:
                                line_client.reply_message(reply_token, error_msg)
                            else:
                                line_client.send_text_message(user_id, error_msg)
                            print(f"[錯誤] {error_msg}")
                    else:
                        error_msg = "食譜數據已過期，請重新上傳圖片"
                        if reply_token:
                            line_client.reply_message(reply_token, error_msg)
                        else:
                            line_client.send_text_message(user_id, error_msg)
                        print(f"[錯誤] 用戶 {user_id} 的食譜數據不存在")
            except (ValueError, IndexError) as e:
                print(f"解析 Postback 數據失敗: {e}")
                error_msg = "處理請求時發生錯誤，請重新上傳圖片"
                if reply_token:
                    line_client.reply_message(reply_token, error_msg)
                else:
                    line_client.send_text_message(user_id, error_msg)
        
        # 處理文字訊息（功能路由）
        for text_event in text_events:
            user_id = text_event.get('user_id')
            reply_token = text_event.get('reply_token')
            text = text_event['message'].get('text', '').strip()
            
            print(f"收到文字訊息（用戶: {user_id}, 內容: {text}）")
            
            # 路由到對應功能
            router.route_message(user_id, text, reply_token)
        
        # 處理圖片事件（根據用戶功能狀態路由）
        for user_id, image_events in image_events_by_user.items():
            current_function = user_function_state.get(user_id)
            
            print(f"收到用戶 {user_id} 的 {len(image_events)} 張圖片（當前功能: {current_function}）")
            
            if current_function == 'recipe':
                # 路由到食譜功能（異步處理，避免 webhook 超時）
                # 只在第一次收到圖片時發送"請稍等"訊息（避免重複發送）
                current_time = time.time()
                last_sent_time = user_wait_message_sent.get(user_id, 0)
                
                # 如果10秒內沒有發送過，則發送
                if current_time - last_sent_time > 10:
                    wait_message = "請稍等，正在處理您的圖片..."
                    line_client.send_text_message(user_id, wait_message)
                    user_wait_message_sent[user_id] = current_time
                    print(f"已發送「請稍等」訊息給用戶 {user_id}")
                
                # 異步處理圖片（在背景線程中執行，避免阻塞 webhook）
                def process_images_async(events):
                    """異步處理圖片事件"""
                    try:
                        if len(events) == 1:
                            recipe_flow_controller.process_line_image(events[0])
                        else:
                            recipe_flow_controller.process_line_images(events)
                    except Exception as e:
                        print(f"[錯誤] 異步處理圖片失敗: {e}")
                        import traceback
                        traceback.print_exc()
                        error_msg = "處理圖片時發生錯誤，請稍後再試。"
                        line_client.send_text_message(user_id, error_msg)
                
                # 在背景線程中處理圖片
                threading.Thread(target=process_images_async, args=(image_events,), daemon=True).start()
                print(f"已啟動異步處理任務（用戶: {user_id}, 圖片數: {len(image_events)}）")
            elif current_function == 'record':
                # 路由到記錄功能（調用 record.py）
                for image_event in image_events:
                    # 調用 record.py 的 add_image_to_buffer 函數
                    # 該函數會自動處理緩衝和變數設定（freshrecord="True"）
                    add_image_to_buffer(image_event)
            else:
                # 未啟用功能，提示用戶
                guide_message = (
                    "📸 您上傳了圖片，但尚未啟用任何功能。\n\n"
                    "請先輸入「食譜功能」或「記錄功能」來啟用對應功能，\n"
                    "或輸入「幫助」查看所有可用功能。"
                )
                reply_token = image_events[0].get('reply_token')
                if reply_token:
                    line_client.reply_message(reply_token, guide_message)
                else:
                    line_client.send_text_message(user_id, guide_message)
        
        # 處理其他事件（影片、文件等）
        for event in other_events:
            # 如果 event 已經是處理過的 message_event（字典格式），直接使用
            # 否則嘗試解析原始事件
            if isinstance(event, dict) and 'message_type' in event:
                message_event = event
            else:
                message_event = webhook_handler.handle_message_event(event)
            
            if message_event:
                message_type = message_event.get('message_type')
                user_id = message_event.get('user_id')
                reply_token = message_event.get('reply_token')
                
                print(f"[除錯] 處理其他事件：類型={message_type}, 用戶={user_id}, reply_token={'有' if reply_token else '無'}")
                
                unsupported_types = ['video', 'file', 'audio']
                if message_type in unsupported_types:
                    error_msg = "目前不支援此格式，請上傳圖片。"
                    print(f"[除錯] 發送錯誤訊息給用戶 {user_id}: {error_msg}")
                    if reply_token:
                        success = line_client.reply_message(reply_token, error_msg)
                        print(f"[除錯] 使用 reply_token 發送結果: {success}")
                    elif user_id:
                        success = line_client.send_text_message(user_id, error_msg)
                        print(f"[除錯] 使用 push 訊息發送結果: {success}")
                    else:
                        print(f"[警告] 無法發送訊息：缺少 user_id 和 reply_token")
            else:
                print(f"[警告] 無法解析事件: {type(event)}")
        
        return 'OK', 200
        
    except Exception as e:
        print(f"處理 Webhook 失敗: {str(e)}")
        import traceback
        traceback.print_exc()
        abort(500)


@app.route('/temp_image/<image_id>', methods=['GET'])
def get_temp_image(image_id: str):
    """
    提供臨時圖片訪問
    
    Args:
        image_id: 圖片 ID
    
    Returns:
        圖片內容或輕量級404響應（減少資源消耗）
    """
    with temp_image_lock:
        if image_id in temp_image_storage:
            image_data = temp_image_storage[image_id]
            # 返回圖片（根據實際格式設置 Content-Type）
            return app.response_class(
                image_data,
                mimetype='image/png',
                headers={
                    'Content-Disposition': f'inline; filename="generated_image_{image_id}.png"'
                }
            )
        else:
            # 返回輕量級1x1透明PNG，並設置長期緩存頭
            # 這樣可以減少資源消耗，客戶端會緩存這個響應
            # 減少 Cloud Run 的 CPU、內存和帶寬使用
            return app.response_class(
                EMPTY_PNG_DATA,
                mimetype='image/png',
                headers={
                    'Cache-Control': 'public, max-age=31536000, immutable',  # 緩存1年
                    'Content-Length': str(len(EMPTY_PNG_DATA))
                }
            )


@app.route('/api/process_message', methods=['POST'])
def process_message_api():
    """集中式處理 API，回傳訊息對象而不直接發送"""
    data = request.json
    event = data.get('event', {})
    user_id = data.get('user_id')

    # DEBUG: Print request content
    print(f"[DEBUG] LINE_Bot_Router received request: user_id={user_id}")
    print(f"[DEBUG] Event type: {event.get('type', 'unknown')}")
    if event.get('type') == 'postback':
        pb_data = event.get('postback', {}).get('data', '')
        print(f"[DEBUG] Postback data: {pb_data}")

    if not user_id or not event:
        print(f"[DEBUG] Request validation failed: user_id={user_id}, event={bool(event)}")
        return jsonify({'error': 'Missing user_id or event'}), 400
    
    # 解析事件類型並標準化內部 event 對象
    # 這裡的 event 是原始 LINE webhook event，我們需要將其標準化
    normalized_event = webhook_handler.handle_message_event(event) if event.get('type') == 'message' else event
    if event.get('type') == 'postback':
        # Postback 也需要簡單標準化以符合內部邏輯
        normalized_event = {
            'type': 'postback',
            'user_id': user_id,
            'reply_token': event.get('replyToken'),
            'data': event.get('postback', {}).get('data', ''),
            'postback': event.get('postback', {})
        }
    
    event = normalized_event # 更新為標準化後的事件
    event_type = event.get('type') or event.get('event_type')
    messages = []
    
    try:
        if event_type == 'message':
            message = event.get('message', {})
            msg_type = message.get('type')
            
            if msg_type == 'text':
                text = message.get('text', '').strip()
                # 使用 Mock Client 攔截 route_message 的發送
                collected = []
                reply_tok = event.get('replyToken')
                class MockClient:
                    def reply_message(self, token, content):
                        if isinstance(content, str): collected.append({'type': 'text', 'text': content})
                        else: collected.append(content)
                        return True
                    def send_text_message(self, uid, content):
                        # 若 content 已是訊息物件（Flex/template 等），直接加入，避免被包成 type=text
                        if isinstance(content, dict) and content.get('type') in ('flex', 'template', 'image'):
                            collected.append(content)
                        else:
                            collected.append({'type': 'text', 'text': content if isinstance(content, str) else str(content)})
                        return True
                
                router.route_message(user_id, text, reply_tok, client=MockClient())
                messages = collected
            
            elif msg_type == 'image':
                current_function = user_function_state.get(user_id)
                if current_function == 'record':
                    # 只有在明確處於紀錄狀態時才執行紀錄；僅在「第一張」時回覆一次，避免連傳多張時重複回覆
                    ok, buffer_size = add_image_to_buffer(event)
                    if ok and buffer_size == 1:
                        messages.append({'type': 'text', 'text': '📸 已收到圖片，正在為您記錄到資料庫...'})
                else:
                    # 其他所有情況都預設執行食譜流 (Dify 分析)
                    user_function_state[user_id] = 'recipe'
                    messages = recipe_flow_controller.process_line_images_and_return_json([event])
        
        elif event_type == 'postback':
            postback_data = event.get('postback', {}).get('data', '')
            if postback_data.startswith('recipe_select='):
                try:
                    recipe_num = int(postback_data.split('=')[1])
                    with recipe_storage_lock:
                        if user_id in user_recipe_storage:
                            recipes = user_recipe_storage[user_id]
                            recipe_key = f'dish_{recipe_num}'
                            if recipe_key in recipes:
                                recipe_content = recipes[recipe_key]
                                cleaned_recipe = remove_markdown_headers(recipe_content)
                                text_content = user_text_storage.get(user_id, '')
                                if text_content:
                                    full_text = text_content + "\n" + "=" * 25 + "\n" + cleaned_recipe
                                else:
                                    full_text = cleaned_recipe
                                messages.append({'type': 'text', 'text': full_text})

                                # 建立回饋按鈕
                                dify_recipe_id = f"dify_{user_id}_{recipe_num}"
                                # [新增] 提供主程式儲存使用的數據
                                generated_recipe_to_store = {
                                    'id': dify_recipe_id,
                                    'text': full_text,
                                    'title': f"Dify Recipe {recipe_num}"
                                }

                                messages.append({
                                    "type": "flex",
                                    "altText": "請給予回饋",
                                    "contents": {
                                        "type": "bubble",
                                        "body": {
                                            "type": "box",
                                            "layout": "vertical",
                                            "contents": [
                                                {
                                                    "type": "text",
                                                    "text": "🤔 覺得這道菜如何？",
                                                    "weight": "bold",
                                                    "size": "md",
                                                    "align": "center"
                                                },
                                                {
                                                    "type": "box",
                                                    "layout": "vertical",
                                                    "margin": "xl",
                                                    "spacing": "sm",
                                                    "contents": [
                                                        {
                                                            "type": "box",
                                                            "layout": "horizontal",
                                                            "spacing": "md",
                                                            "contents": [
                                                                {
                                                                    "type": "button",
                                                                    "style": "primary",
                                                                    "color": "#22cc44",
                                                                    "action": {
                                                                        "type": "postback",
                                                                        "label": "想煮這道菜",
                                                                        "data": f"action=cook&id={dify_recipe_id}"
                                                                    }
                                                                },
                                                                {
                                                                    "type": "button",
                                                                    "style": "secondary",
                                                                    "color": "#e0e0e0",
                                                                    "action": {
                                                                        "type": "postback",
                                                                        "label": "不想煮這...",
                                                                        "data": f"action=dislike&id={dify_recipe_id}"
                                                                    }
                                                                }
                                                            ]
                                                        },
                                                        {
                                                            "type": "button",
                                                            "style": "link",
                                                            "height": "sm",
                                                            "margin": "md",
                                                            "action": {
                                                                "type": "postback",
                                                                "label": "🎲 再推薦一道菜",
                                                                # 只帶 user_id；推薦時會從 user_text_storage 取回第一次的 text
                                                                # 避免把大段文字塞進 querystring 導致長度/編碼問題
                                                                "data": f"action=recommend&user_id={user_id}"
                                                            }
                                                        }
                                                    ]
                                                }
                                            ]
                                        }
                                    }
                                })
                            else:
                                messages.append({'type': 'text', 'text': f"找不到編號 {recipe_num} 的食譜"})
                        else:
                            messages.append({'type': 'text', 'text': "食譜數據已過期，請重新上傳圖片"})
                except:
                    messages.append({'type': 'text', 'text': "處理請求時發生錯誤"})

            # 處理回饋動作
            elif postback_data.startswith('action='):
                params = dict(parse_qsl(postback_data))
                action = params.get('action')
                recipe_id = params.get('id')
                ingredients = params.get('ingr')
                user_id_param = params.get('user_id', user_id)
                retry = params.get('retry', 'false').lower() == 'true'

                if action == 'recommend':
                    # 使用第二個 DIFY 處理推薦請求，不使用 retry 參數
                    print(f"[DEBUG] Processing action=recommend for user: {user_id_param}")
                    try:
                        # 優先用第一個 Dify 回傳並暫存的 food 當作第二個 Dify 的 text 變數
                        food_text = user_food_storage.get(user_id_param, '')
                        recommend_messages = recipe_flow_controller.process_recommend_request_second_dify(
                            user_id_param, # 不使用 retry 參數
                            dify_client_second=dify_client_second,
                            text=food_text
                        )
                        print(f"[DEBUG] Second DIFY returned {len(recommend_messages)} messages")
                        messages.extend(recommend_messages)
                        print(f"[DEBUG] Total messages after merge: {len(messages)}")
                    except Exception as e:
                        print(f"DIFY 推薦錯誤: {e}")
                        import traceback
                        traceback.print_exc()
                        messages.append({'type': 'text', 'text': '推薦服務暫時無法使用，請稍後再試！'})

                elif action == 'cook':
                    # 處理正面回饋
                    messages.append({'type': 'text', 'text': '👨‍🍳 太棒了！已記錄您的喜好！'})

                elif action == 'delete_record':
                    # 一鍵刪除記錄：刪除後回傳更新後的 Flex 清單（保留當前頁）
                    record_id = params.get('id')
                    try:
                        current_page = int(params.get('page', '1'))
                    except Exception:
                        current_page = 1
                    try:
                        rid = int(record_id) if record_id is not None else None
                    except Exception:
                        rid = None

                    if not rid:
                        messages.append({'type': 'text', 'text': '❌ 刪除失敗：缺少或無效的記錄 ID'})
                    else:
                        result = delete_food_record_by_id(rid)
                        if result.get('success'):
                            messages.append({'type': 'text', 'text': '✅ 已刪除該筆記錄'})
                        else:
                            msg = result.get('message', '')
                            # 已刪除過的按鈕再點：友善提示，不顯示錯誤
                            if msg and '找不到' in msg and '記錄' in msg:
                                messages.append({'type': 'text', 'text': '該筆記錄已刪除，請參考下方最新清單。'})
                            else:
                                messages.append({'type': 'text', 'text': f"❌ 刪除失敗：{msg or '未知錯誤'}"})

                        # 保持在刪除模式（讓使用者可繼續刪）
                        user_function_state[user_id] = 'delete'

                        # 回傳更新後清單（同一頁，若頁數超出則回前一頁）
                        try:
                            user_profile = get_user_profile(user_id)
                            username = user_profile.get('displayName', '未知用戶') if user_profile else '未知用戶'
                            records = query_user_food_records(user_id)
                            if not records:
                                messages.append({'type': 'text', 'text': f"📋 {username} 的記錄\n\n目前沒有任何記錄。"})
                            else:
                                max_page = max(1, (len(records) + 5 - 1) // 5)
                                current_page = min(current_page, max_page)
                                messages.append(build_delete_records_flex(username, records, limit=5, page=current_page))
                        except Exception as e:
                            print(f"[ERROR] Failed to build updated delete list: {e}")

                elif action == 'delete_page':
                    # 刪除清單分頁（舊泡泡的上一頁/下一頁：頁數過期時顯示最新清單）
                    try:
                        page = int(params.get('page', '1'))
                    except Exception:
                        page = 1
                    try:
                        user_profile = get_user_profile(user_id)
                        username = user_profile.get('displayName', '未知用戶') if user_profile else '未知用戶'
                        records = query_user_food_records(user_id)
                        limit = 5
                        max_page = max(1, (len(records) + limit - 1) // limit) if records else 1
                        if page < 1 or page > max_page:
                            messages.append({'type': 'text', 'text': '請參考下方最新清單。'})
                            page = min(max(page, 1), max_page)
                        messages.append(build_delete_records_flex(username, records, limit=limit, page=page))
                    except Exception as e:
                        print(f"[ERROR] Failed to build delete page: {e}")
                        messages.append({'type': 'text', 'text': '❌ 分頁失敗，請稍後再試'})

                elif action == 'view_page':
                    # 查看清單分頁（舊泡泡的上一頁/下一頁：頁數過期時顯示最新清單）
                    try:
                        page = int(params.get('page', '1'))
                    except Exception:
                        page = 1
                    try:
                        user_profile = get_user_profile(user_id)
                        username = user_profile.get('displayName', '未知用戶') if user_profile else '未知用戶'
                        records = query_user_food_records(user_id)
                        limit = 5
                        max_page = max(1, (len(records) + limit - 1) // limit) if records else 1
                        if page < 1 or page > max_page:
                            messages.append({'type': 'text', 'text': '請參考下方最新清單。'})
                            page = min(max(page, 1), max_page)
                        messages.append(build_view_records_flex(username, records, limit=limit, page=page))
                    except Exception as e:
                        print(f"[ERROR] Failed to build view page: {e}")
                        messages.append({'type': 'text', 'text': '❌ 分頁失敗，請稍後再試'})

                # elif action == 'dislike':
                #     # 處理負面回饋
                #     messages.append({'type': 'text', 'text': '收到，下次會避免推薦類似菜色！'})

        # DEBUG: Print final messages
        print(f"[DEBUG] LINE_Bot_Router returns {len(messages)} messages")
        for i, msg in enumerate(messages):
            print(f"[DEBUG] Message {i+1}: type={msg.get('type', 'unknown')}")
            if msg.get('type') == 'text':
                print(f"[DEBUG]   Text content: {str(msg.get('text', ''))[:100]}...")
            elif msg.get('type') == 'image':
                print(f"[DEBUG]   Image URL: {msg.get('originalContentUrl', 'N/A')}")
            elif msg.get('type') == 'flex':
                print(f"[DEBUG]   Flex message: {msg.get('altText', 'N/A')}")

        # 構建包含額外數據的響應
        response_data = {'messages': messages}
        # 如果有食譜需要儲存，這是在 postback 處理中設置的
        if 'generated_recipe_to_store' in locals():
            response_data['generated_recipe_to_store'] = generated_recipe_to_store

        print(f"[DEBUG] Final response contains {len(response_data['messages'])} messages")
        return jsonify(response_data)
        
    except Exception as e:
        print(f"API 處理失敗: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({'messages': [{'type': 'text', 'text': '系統繁忙'}]})


@app.route('/health', methods=['GET'])
def health():
    """健康檢查端點"""
    return {'status': 'ok', 'service': 'LINE Bot Router', 'functions': list(router.functions.keys())}, 200


@app.route('/', methods=['GET'])
def index():
    """首頁"""
    return '''
    <h1>LINE Bot 中繼器系統</h1>
    <p>Webhook 端點: /webhook</p>
    <p>健康檢查: /health</p>
    <p>狀態: 運行中</p>
    <h2>已註冊功能：</h2>
    <ul>
        <li>🍳 食譜功能 (recipe)</li>
    </ul>
    '''


def main():
    """主函數"""
    import argparse
    
    parser = argparse.ArgumentParser(description='LINE Bot 中繼器系統')
    # Cloud Run 會設置 PORT 環境變數，優先使用它
    port = int(os.getenv('PORT', 5000))
    parser.add_argument('--host', type=str, default='0.0.0.0',
                       help='伺服器主機 (預設: 0.0.0.0)')
    parser.add_argument('--port', type=int, default=port,
                       help='伺服器埠號 (預設: 從 PORT 環境變數或 5000)')
    parser.add_argument('--debug', action='store_true',
                       help='啟用除錯模式')
    
    args = parser.parse_args()
    
    print("=" * 60)
    print("LINE Bot 中繼器系統")
    print("=" * 60)
    print(f"LINE Channel Secret: {LINE_CHANNEL_SECRET[:20]}...")
    print(f"Webhook URL: http://{args.host}:{args.port}/webhook")
    print(f"已註冊功能: {', '.join(router.functions.keys())}")
    print("=" * 60)
    print("\n伺服器啟動中...")
    print("注意: LINE Webhook 需要 HTTPS，本地測試請使用 ngrok")
    print("\n")
    
    app.run(host=args.host, port=args.port, debug=args.debug)


if __name__ == '__main__':
    main()
