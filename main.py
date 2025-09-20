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
            logger.info(f"🔍 [OTA_DEBUG] JSON data received: {data}")
            logger.info(f"🔍 [OTA_DEBUG] MAC from JSON: '{mac_address}'")
        except Exception as e:
            # JSONでない場合はヘッダーから取得を試行
            mac_address = request.headers.get("Device-Id", "")
            logger.info(f"🔍 [OTA_DEBUG] JSON parse failed: {e}")
            logger.info(f"🔍 [OTA_DEBUG] MAC from header: '{mac_address}'")
        
        # MACアドレスから端末情報を自動取得
        mac_suffix = mac_address[-4:] if len(mac_address) >= 4 else ""
        
        device_info = {}
        if mac_suffix == "8:44":
            device_info = {
                "uuid": "405fc146-3a70-4c35-9ed4-a245dd5a9ee0", 
                "device_number": "467731"
            }
            logger.info(f"🔍 [OTA_DEVICE] MAC {mac_suffix} → Device 467731")
        elif mac_suffix == "9:58":
            device_info = {
                "uuid": "92b63e50-4f65-49dc-a259-35fe14bea832", 
                "device_number": "327546"
            }
            logger.info(f"🔍 [OTA_DEVICE] MAC {mac_suffix} → Device 327546")
        else:
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
        ESP32からの未発火アラームチェック要求
        """
        try:
            data = await request.json()
            device_id = data.get('device_id')
            
            if not device_id:
                return web.json_response({"error": "device_id required"}, status=400)
            
            logger.info(f"📱 アラームチェック要求: device_id={device_id}")
            
            # nekota-serverから未発火アラーム取得
            import aiohttp
            nekota_server_url = "https://nekota-server-production.up.railway.app"
            
            # デバイス認証でuser_idを取得
            # device_idをdevice_numberに変換（マッピング）
            device_mapping = {
                "ESP32_8:44": "467731",  # 現在テスト中の端末
                "ESP32_9:58": "327546"   # もう一方の端末
            }
            device_number = device_mapping.get(device_id, device_id)
            logger.info(f"📱 デバイスマッピング: {device_id} → {device_number}")
            
            async with aiohttp.ClientSession() as session:
                # デバイス認証
                auth_response = await session.post(
                    f"{nekota_server_url}/api/device/exists",
                    json={"device_number": device_number}  # マッピング後のdevice_number
                )
                
                if auth_response.status != 200:
                    error_text = await auth_response.text()
                    logger.error(f"📱 デバイス認証失敗: {auth_response.status} - {error_text}")
                    return web.json_response({"alarms": []})
                
                auth_data = await auth_response.json()
                logger.info(f"📱 認証レスポンス: {auth_data}")
                
                # 正しい構造でuser_idを取得
                user_data = auth_data.get("user")
                if user_data is None:
                    logger.error(f"📱 認証失敗: ユーザーデータが見つかりません (device_id={device_id})")
                    return web.json_response({"alarms": []})
                
                user_id = user_data.get("id")
                jwt_token = auth_data.get("token")
                
                if not user_id or not jwt_token:
                    logger.error(f"📱 認証情報取得失敗: user_id={user_id}, token={'あり' if jwt_token else 'なし'}")
                    return web.json_response({"alarms": []})
                
                # 未発火アラーム取得
                headers = {"Authorization": f"Bearer {jwt_token}"}
                # ESP32未通知のアラームのみ取得（重複防止）
                alarm_response = await session.get(
                    f"{nekota_server_url}/api/alarm/?user_id={user_id}&fired=false&esp32_notified=false",
                    headers=headers
                )
                
                if alarm_response.status == 200:
                    alarm_data = await alarm_response.json()
                    alarms = alarm_data.get("alarms", [])
                    
                    logger.info(f"📱 未発火アラーム取得: {len(alarms)}件")
                    
                    # 現在時刻より未来のアラームのみ処理
                    import datetime
                    now_utc = datetime.datetime.now(datetime.timezone.utc)
                    
                    pending_alarms = []
                    for alarm in alarms:
                        try:
                            # アラームデータの構造をログ出力
                            logger.info(f"📱 アラームデータ: {alarm}")
                            
                            # 日付と時刻を組み合わせてUTC時刻を作成
                            alarm_date = alarm.get('alarm_date')     # YYYY-MM-DD
                            alarm_time_str = alarm.get('alarm_time') # HH:MM:SS
                            
                            if alarm_date and alarm_time_str:
                                # 日付と時刻を組み合わせ
                                alarm_datetime_str = f"{alarm_date}T{alarm_time_str}"
                                alarm_time = datetime.datetime.fromisoformat(alarm_datetime_str)
                                
                                # DBに保存されている時刻は既にUTC時刻なので、そのままUTCとして解釈
                                alarm_time = alarm_time.replace(tzinfo=datetime.timezone.utc)
                                logger.info(f"📱 DB時刻をUTCとして解釈: {alarm_time}")
                                
                                logger.info(f"📱 アラーム時刻: {alarm_time}, 現在時刻: {now_utc}")
                                
                                if alarm_time > now_utc:
                                    seconds_until = int((alarm_time - now_utc).total_seconds())
                                    
                                    # メッセージとテキストを統合
                                    message = alarm.get("message", "")
                                    text = alarm.get("text", "")
                                    
                                    # 両方ある場合は統合、片方だけの場合はそのまま
                                    if message and text:
                                        combined_message = f"{message}　{text}"
                                    elif text:
                                        combined_message = text
                                    else:
                                        combined_message = message or "アラームの時間だにゃん！"
                                    
                                    pending_alarms.append({
                                        "id": alarm["id"],
                                        "seconds": seconds_until,
                                        "message": combined_message
                                    })
                                    logger.info(f"📱 有効アラーム追加: {seconds_until}秒後, メッセージ: {combined_message}")
                                else:
                                    logger.info(f"📱 過去のアラーム（スキップ）: {alarm_time}")
                            else:
                                logger.error(f"📱 アラーム時刻データ不正: date={alarm_date}, time={alarm_time_str}")
                                
                        except Exception as e:
                            logger.error(f"📱 アラーム処理エラー: {e}, データ: {alarm}")
                    
                    logger.info(f"📱 有効アラーム: {len(pending_alarms)}件")
                    
                    # ESP32に送信するアラームを通知済みに更新
                    if pending_alarms:
                        alarm_ids = [alarm["id"] for alarm in pending_alarms]
                        for alarm_id in alarm_ids:
                            try:
                                update_response = await session.patch(
                                    f"{nekota_server_url}/api/alarm/{alarm_id}",
                                    json={"esp32_notified": True},
                                    headers=headers
                                )
                                if update_response.status == 200:
                                    logger.info(f"📱 アラーム通知済み更新: {alarm_id}")
                                else:
                                    logger.warning(f"📱 アラーム更新失敗: {alarm_id}")
                            except Exception as e:
                                logger.error(f"📱 アラーム更新エラー: {alarm_id} - {e}")
                    
                    # 未読レター取得
                    letter_response = await session.get(
                        f"{nekota_server_url}/api/message/list?friend_id=all&unread_only=true",
                        headers=headers
                    )
                    
                    pending_letters = []
                    if letter_response.status == 200:
                        letter_data = await letter_response.json()
                        letters = letter_data.get("messages", [])
                        
                        logger.info(f"📮 未読レター取得: {len(letters)}件")
                        
                        for letter in letters:
                            pending_letters.append({
                                "id": letter["id"],
                                "from_user_name": letter.get("from_user_name", "誰か"),
                                "message": letter.get("transcribed_text", letter.get("message", ""))
                            })
                    else:
                        logger.error(f"📮 レター取得失敗: {letter_response.status}")
                    
                    return web.json_response({
                        "alarms": pending_alarms,
                        "letters": pending_letters
                    })
                else:
                    logger.error(f"📱 アラーム取得失敗: {alarm_response.status}")
                    return web.json_response({"alarms": [], "letters": []})
                    
        except Exception as e:
            logger.error(f"📱 アラームチェックエラー: {e}")
            return web.json_response({"alarms": []})

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