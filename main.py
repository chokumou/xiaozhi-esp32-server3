import asyncio
import signal
import sys
import json
from aiohttp import web

from config import Config
from utils.logger import setup_logger
from utils.auth import AuthManager, AuthError
from websocket_handler import ConnectionHandler, connected_devices

# 制御チャネル用の接続管理
control_connections = {}

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
            
            # 制御チャネル経由でタイマー設定コマンドを送信
            device_id = list(control_connections.keys())[0] if control_connections else None
            
            if not device_id:
                logger.error(f"📱 制御チャネル接続デバイスなし")
                return web.json_response({"error": "No control channel connected"}, status=400)
                
            logger.info(f"📱 制御チャネル経由タイマー設定: {device_id}")
            
            # 制御コマンド作成
            command = {
                "cmd": "SET_TIMER",
                "seconds": seconds,
                "message": message,
                "timestamp": asyncio.get_event_loop().time()
            }
            
            # 制御チャネル経由で送信
            success = await send_control_command(device_id, command)
            if not success:
                return web.json_response({"error": "Failed to send control command"}, status=500)
            
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

    async def control_websocket_handler(request):
        """
        制御チャネル用WebSocket（常時接続、60秒ハートビート）
        """
        ws = web.WebSocketResponse(heartbeat=60)
        await ws.prepare(request)
        
        # デバイスIDを取得
        headers = {k.lower(): v for k, v in request.headers.items()}
        device_id = headers.get("device-id") or f"device_{request.remote}"
        
        logger.info(f"🔧 制御チャネル接続: device_id={device_id}")
        
        try:
            # 制御チャネルに登録
            control_connections[device_id] = ws
            logger.info(f"🔧 制御チャネル登録完了: {device_id}")
            
            # 接続確認メッセージ送信
            await ws.send_str('{"type":"control_hello","message":"Control channel established"}')
            
            # メッセージ受信ループ
            async for msg in ws:
                if msg.type == web.WSMsgType.TEXT:
                    try:
                        data = msg.json()
                        logger.info(f"🔧 制御メッセージ受信: {device_id} -> {data}")
                        
                        # ハートビート応答
                        if data.get("type") == "ping":
                            await ws.send_str('{"type":"pong"}')
                            
                    except Exception as e:
                        logger.error(f"🔧 制御メッセージ処理エラー: {e}")
                        
                elif msg.type == web.WSMsgType.ERROR:
                    logger.error(f"🔧 制御チャネルエラー: {ws.exception()}")
                    break
                    
        except Exception as e:
            logger.error(f"🔧 制御チャネル例外: {e}")
        finally:
            # 接続解除時にクリーンアップ
            if device_id in control_connections:
                del control_connections[device_id]
                logger.info(f"🔧 制御チャネル解除: {device_id}")
        
        return ws

    async def send_control_command(device_id: str, command: dict):
        """制御チャネル経由でコマンド送信"""
        if device_id in control_connections:
            try:
                await control_connections[device_id].send_str(json.dumps(command))
                logger.info(f"🔧 制御コマンド送信成功: {device_id} -> {command}")
                return True
            except Exception as e:
                logger.error(f"🔧 制御コマンド送信失敗: {device_id} -> {e}")
                return False
        else:
            logger.warning(f"🔧 制御チャネル未接続: {device_id}")
            return False

    # Create HTTP server with all endpoints BEFORE starting
    app = web.Application()
    app.router.add_post('/xiaozhi/ota/', ota_endpoint)
    app.router.add_get('/xiaozhi/ota/', ota_endpoint)
    app.router.add_get('/xiaozhi/v1/', websocket_handler)
    
    # Web画面からのアラーム設定用APIエンドポイント
    app.router.add_get('/api/device/connected', device_connected_check)
    app.router.add_post('/api/device/set_timer', device_set_timer)
    
    # 制御チャネル用エンドポイント
    app.router.add_get('/control', control_websocket_handler)
    
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