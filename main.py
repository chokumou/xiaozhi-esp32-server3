import asyncio
import signal
import sys
import json
from aiohttp import web

from config import Config
from utils.logger import setup_logger
from utils.auth import AuthManager, AuthError
from websocket_handler import ConnectionHandler, connected_devices

logger = setup_logger()
auth_manager = AuthManager()

async def ota_endpoint(request):
    """OTA version check endpoint - ESP32 compatible response"""
    try:
        # ESP32からのリクエストデータを取得
        try:
            data = await request.json()
            mac_address = data.get("mac", "")
            device_number = data.get("device_number", "")  # 端末番号を直接取得
            logger.info(f"🔍 [OTA_DEBUG] JSON data received: {data}")
            logger.info(f"🔍 [OTA_DEBUG] MAC from JSON: '{mac_address}'")
            logger.info(f"🔍 [OTA_DEBUG] Device number from JSON: '{device_number}'")
        except Exception as e:
            # JSONでない場合はヘッダーから取得を試行
            mac_address = request.headers.get("Device-Id", "")
            device_number = ""
            logger.info(f"🔍 [OTA_DEBUG] JSON parse failed: {e}")
            logger.info(f"🔍 [OTA_DEBUG] MAC from header: '{mac_address}'")
        
        # 端末番号が直接送信されている場合はそれを使用
        device_info = {}
        # データベース接続を初期化
        try:
            import os
            import requests
            
            # 環境変数から直接取得
            supabase_url = os.getenv("SUPABASE_URL", "https://xsglqqywodyqhzktkygq.supabase.co")
            supabase_key = os.getenv("SUPABASE_KEY", "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6InhzZ2xxcXl3b2R5cWh6a3RreWdxIiwicm9sZSI6InNlcnZpY2Vfcm9sZSIsImlhdCI6MTc0OTAyODEyNywiZXhwIjoyMDY0NjA0MTI3fQ.tmNU7T5N5qe7i2jraods8TD9bdGVhDQAIj0TgcnzQpI")
            
            # Supabaseクライアントの代わりにrequestsを使用
            supabase = {"url": supabase_url, "key": supabase_key}
            logger.info(f"🔍 [OTA_DEVICE] Database connection initialized: {supabase_url}")
        except Exception as e:
            logger.error(f"🔍 [OTA_DEVICE] Database connection failed: {e}")
            supabase = None
        
        if device_number:
            # 端末番号からUUIDを取得（データベース照合）
            if supabase:
                try:
                    # requestsを使用してSupabase REST APIを直接呼び出し
                    headers = {
                        "apikey": supabase["key"],
                        "Authorization": f"Bearer {supabase['key']}",
                        "Content-Type": "application/json"
                    }
                    url = f"{supabase['url']}/rest/v1/devices?device_number=eq.{device_number}"
                    
                    logger.info(f"🔍 [OTA_DEVICE] Database query: {url}")
                    response = requests.get(url, headers=headers)
                    logger.info(f"🔍 [OTA_DEVICE] Database response: {response.status_code}")
                    
                    if response.status_code == 200:
                        data = response.json()
                        logger.info(f"🔍 [OTA_DEVICE] Database data: {data}")
                        if data and len(data) > 0:
                            device_data = data[0]
                            device_info = {
                                "uuid": device_data.get('id', ''),
                                "device_number": device_number
                            }
                            logger.info(f"🔍 [OTA_DEVICE] Device number {device_number} → UUID {device_data.get('id', '')}")
                        else:
                            logger.warning(f"🔍 [OTA_DEVICE] Device number {device_number} not found in database")
                    else:
                        logger.error(f"🔍 [OTA_DEVICE] Database request failed: {response.status_code}")
                        logger.error(f"🔍 [OTA_DEVICE] Response content: {response.text}")
                except Exception as e:
                    logger.error(f"🔍 [OTA_DEVICE] Database lookup failed: {e}")
        else:
            # フォールバック: MACアドレスから端末情報を自動取得
            # MACアドレスの最後の5文字を取得（例: 98:3d:ae:61:68:dc → 68:dc）
            if len(mac_address) >= 5:
                # 最後の5文字を取得（コロンを含む）
                mac_suffix = mac_address[-5:]
            else:
                mac_suffix = ""
            logger.info(f"🔍 [OTA_DEVICE] MAC suffix extracted: {mac_suffix} from {mac_address}")
            
            # 動的マッピング: データベースからMAC Suffixで検索
            if supabase:
                try:
                    # MAC Suffixからデバイス番号を生成（コロン除去、大文字変換）
                    device_number = mac_suffix.replace(':', '').upper()
                    
                    # requestsを使用してSupabase REST APIを直接呼び出し
                    headers = {
                        "apikey": supabase["key"],
                        "Authorization": f"Bearer {supabase['key']}",
                        "Content-Type": "application/json"
                    }
                    url = f"{supabase['url']}/rest/v1/devices?device_number=eq.{device_number}"
                    
                    logger.info(f"🔍 [OTA_DEVICE] Database query: {url}")
                    response = requests.get(url, headers=headers)
                    logger.info(f"🔍 [OTA_DEVICE] Database response: {response.status_code}")
                    
                    if response.status_code == 200:
                        data = response.json()
                        logger.info(f"🔍 [OTA_DEVICE] Database data: {data}")
                        if data and len(data) > 0:
                            device_data = data[0]
                            device_info = {
                                "uuid": device_data.get('id', ''),
                                "device_number": device_number
                            }
                            logger.info(f"🔍 [OTA_DEVICE] MAC {mac_suffix} → Device {device_number} (UUID: {device_data.get('id', '')})")
                        else:
                            # レガシーマッピング（既存の固定マッピング）
                            if mac_suffix == "8:44":
                                device_info = {
                                    "uuid": "405fc146-3a70-4c35-9ed4-a245dd5a9ee0", 
                                    "device_number": "467731"
                                }
                                logger.info(f"🔍 [OTA_DEVICE] MAC {mac_suffix} → Device 467731 (Legacy)")
                            elif mac_suffix == "9:58":
                                device_info = {
                                    "uuid": "92b63e50-4f65-49dc-a259-35fe14bea832", 
                                    "device_number": "327546"
                                }
                                logger.info(f"🔍 [OTA_DEVICE] MAC {mac_suffix} → Device 327546 (Legacy)")
                            else:
                                logger.warning(f"🔍 [OTA_DEVICE] Unknown MAC suffix: {mac_suffix} (full: {mac_address})")
                    else:
                        logger.error(f"🔍 [OTA_DEVICE] Database request failed: {response.status_code}")
                        logger.error(f"🔍 [OTA_DEVICE] Response content: {response.text}")
                        logger.warning(f"🔍 [OTA_DEVICE] Unknown MAC suffix: {mac_suffix} (full: {mac_address})")
                except Exception as e:
                    logger.error(f"🔍 [OTA_DEVICE] Database lookup failed: {e}")
                    logger.warning(f"🔍 [OTA_DEVICE] Unknown MAC suffix: {mac_suffix} (full: {mac_address})")
        
        # Return ESP32-compatible response with websocket configuration
        version_info = {
            "version": "1.6.8",
            "update_available": False,
            "download_url": "",
            "changelog": "No updates available",
            "device_info": device_info,  # デバイス情報を追加
            "websocket": {
                "url": "wss://xiaozhi-esp32-server3-production.up.railway.app/xiaozhi/v1/",
                "token": "",
                "version": 3
            },
            "protocol": "websocket"
        }
        logger.info(f"OTA response: {version_info}")
        return web.json_response(version_info)
    except Exception as e:
        logger.error(f"OTA endpoint error: {e}")
        return web.json_response({"error": "Internal server error"}, status=500)

async def authenticate_websocket(websocket, path):
    try:
        # Extract headers (case insensitive)
        headers = {k.lower(): v for k, v in websocket.request_headers.items()}
        
        # Get device ID and client ID from headers or URL query params
        device_id = headers.get("device-id")
        client_id = headers.get("client-id")
        protocol_version = headers.get("protocol-version", "1")
        
        # ヘッダーにdevice-idがない場合、URLのクエリパラメータから取得
        if not device_id:
            import urllib.parse
            url_path = websocket.path_qs
            parsed = urllib.parse.urlparse(url_path)
            query_params = urllib.parse.parse_qs(parsed.query)
            device_id = query_params.get('device-id', [None])[0]
            logger.info(f"🔍 [URL_DEVICE_ID] Retrieved device_id from URL: {device_id}")
        
        logger.info(f"🔍 [DEVICE_ID_DEBUG] Final device_id: {device_id}")
        
        # Optional JWT authentication
        auth_header = headers.get("authorization")
        if auth_header and auth_header.startswith("Bearer "):
            try:
                token = auth_header.split(" ")[1]
                authenticated_device_id = auth_manager.decode_token(token)
                logger.info(f"Device {authenticated_device_id} authenticated via JWT.")
                if device_id and device_id != authenticated_device_id:
                    logger.warning(f"Device ID mismatch: header={device_id}, token={authenticated_device_id}")
                device_id = authenticated_device_id
            except AuthError as e:
                logger.warning(f"JWT authentication failed: {e}")
                # Continue without authentication for development
                
        if not device_id:
            device_id = client_id or f"device_{websocket.remote_address[0]}"
            
        logger.info(f"Device {device_id} connected (protocol v{protocol_version})")

        handler = ConnectionHandler(websocket, headers)
        await handler.run()

    except Exception as e:
        logger.error(f"Unhandled error during WebSocket connection for {websocket.remote_address}: {e}")
        await websocket.close(code=1011, reason="Internal server error.")

async def main():
    Config.validate() # Validate environment variables
    logger.info("Configuration validated successfully.")

    # Add WebSocket handler function
    async def websocket_handler(request):
        # ESP32 Ping/Pong対応: heartbeat設定（環境変数で調整可能、TTS中の切断防止）
        ws = web.WebSocketResponse(protocols=["v1", "xiaozhi-v1"], heartbeat=Config.WEBSOCKET_HEARTBEAT_SECONDS)
        await ws.prepare(request)
        
        # Get device info from headers
        headers = {k.lower(): v for k, v in request.headers.items()}
        device_id = headers.get("device-id")
        client_id = headers.get("client-id") 
        protocol_version = headers.get("protocol-version", "1")
        
        if not device_id:
            device_id = client_id or f"device_{request.remote}"
            
        logger.info(f"Device {device_id} connected via WebSocket (protocol v{protocol_version})")
        
        # Create connection handler (websocket, headers)
        logger.info(f"🐛 ConnectionHandler作成前: device_id={device_id}")
        logger.info(f"🐛 headers: {headers}")
        handler = ConnectionHandler(ws, headers)
        logger.info(f"🐛 ConnectionHandler作成後")
        await handler.run()
        return ws

    async def device_connected_check(request):
        """
        デバイス接続状態をチェック
        """
        try:
            user_id = request.query.get('user_id')
            if not user_id:
                return web.json_response({"error": "user_id required"}, status=400)
            
            # user_idからdevice_idを取得する必要があるが、
            # 現在は簡易実装：接続中のデバイス一覧をチェック
            connected = len(connected_devices) > 0
            
            logger.info(f"📱 接続チェック: user_id={user_id}, connected_devices={list(connected_devices.keys())}")
            
            return web.json_response({
                "connected": connected,
                "connected_devices": list(connected_devices.keys())
            })
            
        except Exception as e:
            logger.error(f"デバイス接続チェックエラー: {e}")
            return web.json_response({"error": str(e)}, status=500)

    async def device_set_timer(request):
        """
        接続中のデバイスにタイマー設定（一時的に無効化）
        """
        try:
            # 犯人特定のため詳細ログ（ブロックは一時解除）
            data = await request.json()
            user_id = data.get('user_id')
            seconds = data.get('seconds')
            message = data.get('message', '')
            
            logger.error(f"🔥🔥🔥 DEVICE_SET_TIMER_CALL 🔥🔥🔥")
            logger.error(f"🚨 [CULPRIT_DEBUG] IP: {request.remote}")
            logger.error(f"🚨 [CULPRIT_DEBUG] User-Agent: {request.headers.get('User-Agent', 'Unknown')}")
            logger.error(f"🚨 [CULPRIT_DEBUG] Referer: {request.headers.get('Referer', 'None')}")
            logger.error(f"🚨 [CULPRIT_DEBUG] X-Forwarded-For: {request.headers.get('X-Forwarded-For', 'None')}")
            logger.error(f"🚨 [CULPRIT_DEBUG] X-Railway-Request-Id: {request.headers.get('X-Railway-Request-Id', 'None')}")
            logger.error(f"🚨 [CULPRIT_DEBUG] Content: user_id={user_id}, seconds={seconds}, message='{message}'")
            
            # 呼び出し間隔を調査
            if not hasattr(device_set_timer, 'call_times'):
                device_set_timer.call_times = []
            
            import time
            current_time = time.time()
            device_set_timer.call_times.append(current_time)
            
            # 最近10回の呼び出し間隔を分析
            if len(device_set_timer.call_times) > 1:
                intervals = []
                for i in range(1, min(len(device_set_timer.call_times), 10)):
                    interval = device_set_timer.call_times[i] - device_set_timer.call_times[i-1]
                    intervals.append(f"{interval:.2f}s")
                logger.error(f"🚨 [CALL_INTERVAL] 呼び出し間隔: {', '.join(intervals)}")
            
            # 詳細調査：1回の指令で6回実行される原因を特定
            
            # スタックトレース取得
            import traceback
            stack_trace = traceback.format_stack()
            logger.error(f"🚨 [DETAILED_STACK] 詳細スタックトレース:")
            for i, line in enumerate(stack_trace[-10:]):  # 最後の10行
                logger.error(f"🔥🔥🔥 STACK_{i} 🔥🔥🔥 {line.strip()}")
            
            # 処理を続行（詳細調査のため）
            
            # 重複防止チェック（同じメッセージの重複実行を防止）
            cache_key = f"{user_id}_{message}_{seconds//60}"  # 分単位でキャッシュ
            
            # 簡易キャッシュ（グローバル変数使用）
            if not hasattr(device_set_timer, 'recent_requests'):
                device_set_timer.recent_requests = {}
            
            import time
            current_time = time.time()
            
            # 30秒以内の同じリクエストはブロック
            if cache_key in device_set_timer.recent_requests:
                last_time = device_set_timer.recent_requests[cache_key]
                if current_time - last_time < 30:
                    logger.error(f"🚨 [DUPLICATE_BLOCK] 重複リクエストをブロック: {cache_key}")
                    return web.json_response({"status": "duplicate_blocked"}, status=409)
            
            # キャッシュに記録
            device_set_timer.recent_requests[cache_key] = current_time
            
            if not user_id or not seconds:
                return web.json_response({"error": "user_id and seconds required"}, status=400)
            
            logger.info(f"📱 タイマー設定リクエスト: user_id={user_id}, seconds={seconds}, message='{message}'")
            
            # 接続中のデバイスを確認（デバッグログ付き）
            logger.info(f"📱 接続デバイス一覧: {list(connected_devices.keys())}")
            logger.info(f"📱 接続デバイス数: {len(connected_devices)}")
            
            if not connected_devices:
                logger.error(f"📱 接続デバイスなし")
                return web.json_response({"error": "No devices connected"}, status=400)
            
            # 最初の接続デバイスにタイマー設定（簡易実装）
            device_id = list(connected_devices.keys())[0]
            handler = connected_devices[device_id]
            logger.info(f"📱 タイマー送信先デバイス: {device_id}")
            
            logger.info(f"📱 send_timer_set_command呼び出し開始")
            await handler.send_timer_set_command(device_id, seconds, message)
            logger.info(f"📱 send_timer_set_command呼び出し完了")
            
            logger.info(f"📱 タイマー設定成功: device_id={device_id}")
            
            return web.json_response({
                "success": True,
                "device_id": device_id,
                "seconds": seconds,
                "message": message
            })
            
        except Exception as e:
            logger.error(f"デバイスタイマー設定エラー: {e}")
            return web.json_response({"error": str(e)}, status=500)

    async def device_check_alarms(request):
        """
        ESP32からの未発火アラームチェック要求（一時的に無効化）
        """
        logger.info(f"⏰ [ALARM_DISABLED] Alarm check disabled, returning empty response")
        return web.json_response({"alarms": [], "letters": []})
        
        # アラーム機能を一時的に停止中
        # try:
        #     data = await request.json()
        #     request_device_id = data.get('device_id')
        #     
        #     if not request_device_id:
        #         return web.json_response({"error": "device_id required"}, status=400)
    #                 # 未読レター取得（友達リストから個別に取得）
    #                 # まず友達リストを取得
    #                 friends_response = await session.get(
    #                     f"{nekota_server_url}/api/friend/list",
    #                     headers=headers
    #                 )
    #                 
    #                 pending_letters = []
    #                 if friends_response.status == 200:
    #                     friends_data = await friends_response.json()
    #                     friends = friends_data.get("friends", [])
    #                     logger.info(f"📮 友達リスト取得: {len(friends)}人")
    #                     
    #                     # 各友達から未読メッセージを取得
    #                     for friend in friends:
    #                         friend_id = friend.get("id")
    #                         friend_name = friend.get("name", "不明")
    #                         logger.info(f"📮 友達チェック: {friend_name} (ID: {friend_id})")
    #                         
    #                         if friend_id:
    #                             api_url = f"{nekota_server_url}/api/message/list?friend_id={friend_id}&unread_only=true"
    #                             logger.info(f"📮 API呼び出し: {api_url}")
    #                             
    #                             letter_response = await session.get(api_url, headers=headers)
    #                             logger.info(f"📮 API応答: {letter_response.status}")
    #                             
    #                             if letter_response.status == 200:
    #                                 letter_data = await letter_response.json()
    #                                 letters = letter_data.get("messages", [])
    #                                 logger.info(f"📮 {friend_name}からの未読メッセージ: {len(letters)}件")
    #                                 
    #                                 for letter in letters:
    #                                     pending_letters.append({
    #                                         "id": letter["id"],
    #                                         "from_user_name": letter.get("from_user_name", friend.get("name", "誰か")),
    #                                         "message": letter.get("transcribed_text", letter.get("message", ""))
    #                                     })
    #                                     logger.info(f"📮 メッセージ追加: {letter.get('transcribed_text', 'なし')}")
    #                             else:
    #                                 logger.error(f"📮 {friend_name}のメッセージ取得失敗: {letter_response.status}")
    #                 else:
    #                     logger.error(f"📮 友達リスト取得失敗: {friends_response.status}")
    #                 
    #                 logger.info(f"📮 未読レター取得: {len(pending_letters)}件")
    #                 
    #                 # デバイス別にレター情報を保存
    #                 from websocket_handler import device_pending_letters
    #                 device_pending_letters[actual_device_id] = pending_letters
    #                 logger.info(f"📮 デバイス別レター保存完了: {actual_device_id} = {len(pending_letters)}件")
    #                 logger.info(f"🔍🔍🔍 [DEBUG_LETTER_SAVE] デバイス別レター保存: {pending_letters} 🔍🔍🔍")
                    
    #                 return web.json_response({
    #                     "alarms": pending_alarms,
    #                     "letters": pending_letters
    #                 })
    #             else:
    #                 logger.error(f"📱 アラーム取得失敗: {alarm_response.status}")
    #                 return web.json_response({"alarms": [], "letters": []})
    #                 
    #     except Exception as e:
    #         logger.error(f"📱 アラームチェックエラー: {e}")
    #         return web.json_response({"alarms": []})

    # Create HTTP server with all endpoints BEFORE starting
    app = web.Application()
    app.router.add_post('/xiaozhi/ota/', ota_endpoint)
    app.router.add_get('/xiaozhi/ota/', ota_endpoint)
    app.router.add_get('/xiaozhi/v1/', websocket_handler)
    
    # Web画面からのアラーム設定用APIエンドポイント
    app.router.add_get('/api/device/connected', device_connected_check)
    app.router.add_post('/api/device/set_timer', device_set_timer)
    
    # ESP32起動時アラームチェック用API
    app.router.add_post('/api/device/check_alarms', device_check_alarms)
    
    stop_event = asyncio.Event()
    if sys.platform != "win32":
        for sig in (signal.SIGINT, signal.SIGTERM):
            asyncio.get_event_loop().add_signal_handler(sig, stop_event.set)

    # Start unified server
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, Config.HOST, Config.PORT)
    await site.start()
    
    logger.info(f"Unified server starting on {Config.HOST}:{Config.PORT}")
    logger.info(f"OTA endpoint: http://{Config.HOST}:{Config.PORT}/xiaozhi/ota/")
    logger.info(f"WebSocket endpoint: ws://{Config.HOST}:{Config.PORT}/xiaozhi/v1/")
    
    await stop_event.wait()
    logger.info("Server stopped.")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Server interrupted by user (Ctrl+C). Shutting down.")
    except Exception as e:
        logger.critical(f"Fatal server error: {e}")