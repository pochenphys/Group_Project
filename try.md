請幫我看 @middle.py、@line-service.py、@Line_bot_Router.py的架構
對於食譜部分我想讓使用者輸入訊息後統一送到 @middle.py的cloud run， middle.py同時打兩個請求道 @line-service.py和 @Line_bot_Router.py的cloud run， 兩個伺服器處理完後把回傳的訊息回給 @middle.py， @middle.py再統一回傳訊息給使用者。
回傳的訊息因如下，並使用同一個reply token進行回復: 第一行:AI 第二行:AI回傳訊息的圖像輪盤 第三行:自製 第四行:@line-service.py的圖像輪盤
圖像輪盤的回應方式則如舊，並同一於回覆後進行詢問是否喜愛，若喜愛則加進pinecone向量資料庫