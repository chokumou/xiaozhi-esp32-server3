#!/usr/bin/env python3
"""
xiaozhi-esp32-server3
シンプルで安定したAI音声対話サーバー
"""

import asyncio
import os
import sys
import signal
from pathlib import Path

import websockets
from websockets.server import serve

from config import config, Config
from utils.logger import log
from websocket_handler import WebSocketHandler

class XiaozhiServer:
    """Xiaozhi AI音声対話サーバー"""
    
    def __init__(self):
        self.websocket_handler = WebSocketHandler()
        self.server = None
    
    async def start(self):
        """サーバー開始"""
        try:
            # 設定検証
            Config.validate()
            log.info("Configuration validated successfully")
            
            # ログディレクトリ作成
            Path("logs").mkdir(exist_ok=True)
            
            # WebSocketサーバー起動
            log.info(f"Starting xiaozhi-server3 on {config.HOST}:{config.PORT}")
            
            self.server = await serve(
                self.websocket_handler.handle_connection,
                config.HOST,
                config.PORT,
                ping_interval=30,
                ping_timeout=10,
                close_timeout=10
            )
            
            log.info(f"xiaozhi-server3 is running on ws://{config.HOST}:{config.PORT}")
            log.info("Press Ctrl+C to stop the server")
            
            # サーバー実行継続
            await self.server.wait_closed()
            
        except Exception as e:
            log.error(f"Server startup failed: {e}")
            sys.exit(1)
    
    async def stop(self):
        """サーバー停止"""
        if self.server:
            log.info("Stopping xiaozhi-server3...")
            self.server.close()
            await self.server.wait_closed()
            log.info("Server stopped")

async def main():
    """メイン関数"""
    server = XiaozhiServer()
    
    # シグナルハンドラー設定
    def signal_handler():
        log.info("Shutdown signal received")
        asyncio.create_task(server.stop())
    
    # Unix系OSでのシグナル処理
    if sys.platform != "win32":
        loop = asyncio.get_running_loop()
        for sig in [signal.SIGINT, signal.SIGTERM]:
            loop.add_signal_handler(sig, signal_handler)
    
    try:
        await server.start()
    except KeyboardInterrupt:
        log.info("Keyboard interrupt received")
        await server.stop()
    except Exception as e:
        log.error(f"Unexpected error: {e}")
        await server.stop()
        sys.exit(1)

if __name__ == "__main__":
    # Windowsでイベントループポリシーを設定
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    
    # uvloopを使用（Unix系OSのみ）
    try:
        if sys.platform != "win32":
            import uvloop
            uvloop.install()
            log.info("Using uvloop for better performance")
    except ImportError:
        log.info("uvloop not available, using default event loop")
    
    # サーバー実行
    asyncio.run(main())
