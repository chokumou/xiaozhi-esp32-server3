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
        # Return ESP32-compatible response with websocket configuration
        version_info = {
            "version": "1.6.8",
            "update_available": False,
            "download_url": "",
            "changelog": "No updates available",
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
        
        # Get device ID and client ID from headers
        device_id = headers.get("device-id")
        client_id = headers.get("client-id")
        protocol_version = headers.get("protocol-version", "1")
        
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
        接続中のデバイスにタイマー設定
        """
        try:
            data = await request.json()
            user_id = data.get('user_id')
            seconds = data.get('seconds')
            message = data.get('message', '')
            
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
            async with aiohttp.ClientSession() as session:
                # デバイス認証
                auth_response = await session.post(
                    f"{nekota_server_url}/api/device/exists",
                    json={"device_number": "327546"}  # 固定デバイス番号
                )
                
                if auth_response.status != 200:
                    error_text = await auth_response.text()
                    logger.error(f"📱 デバイス認証失敗: {auth_response.status} - {error_text}")
                    return web.json_response({"alarms": []})
                
                auth_data = await auth_response.json()
                logger.info(f"📱 認証レスポンス: {auth_data}")
                
                # 正しい構造でuser_idを取得
                user_data = auth_data.get("user", {})
                user_id = user_data.get("id")
                jwt_token = auth_data.get("token")
                
                if not user_id or not jwt_token:
                    logger.error(f"📱 認証情報取得失敗: user_id={user_id}, token={'あり' if jwt_token else 'なし'}")
                    return web.json_response({"alarms": []})
                
                # 未発火アラーム取得
                headers = {"Authorization": f"Bearer {jwt_token}"}
                alarm_response = await session.get(
                    f"{nekota_server_url}/api/alarm/?user_id={user_id}&fired=false",
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
                                    pending_alarms.append({
                                        "id": alarm["id"],
                                        "seconds": seconds_until,
                                        "message": alarm["text"]
                                    })
                                    logger.info(f"📱 有効アラーム追加: {seconds_until}秒後")
                                else:
                                    logger.info(f"📱 過去のアラーム（スキップ）: {alarm_time}")
                            else:
                                logger.error(f"📱 アラーム時刻データ不正: date={alarm_date}, time={alarm_time_str}")
                                
                        except Exception as e:
                            logger.error(f"📱 アラーム処理エラー: {e}, データ: {alarm}")
                    
                    logger.info(f"📱 有効アラーム: {len(pending_alarms)}件")
                    return web.json_response({"alarms": pending_alarms})
                else:
                    logger.error(f"📱 アラーム取得失敗: {alarm_response.status}")
                    return web.json_response({"alarms": []})
                    
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