"""
Server2-style Connection Handler for Server3
完全なServer2互換の接続・メッセージルーティング制御
"""
import os
import time
import asyncio
import logging
from typing import Dict, Any

logger = logging.getLogger(__name__)
TAG = "Connection"


class Server2StyleConnectionHandler:
    """Server2準拠の接続・メッセージ処理ハンドラ"""
    
    def __init__(self):
        # フレーム統計
        self._rx_frame_count = 0
        self._rx_bytes_total = 0
        self.rx_frames_since_listen = 0
        self.rx_bytes_since_listen = 0
        self.utt_seq = 0
        
        # DTX制御
        self.dtx_drop_count = 0
        
    async def route_message(self, message: bytes, audio_handler):
        """Server2準拠のメッセージルーティング"""
        try:
            logger.info(f"🎯 [CONNECTION_ROUTE] Start route_message: {len(message)}B, type={type(message)}")
            
            if isinstance(message, bytes):
                logger.info(f"🎯 [CONNECTION_ROUTE] Processing {len(message)}B bytes message")
                result = await self._handle_binary_message(message, audio_handler)
                logger.info(f"🎯 [CONNECTION_ROUTE] _handle_binary_message completed")
                return result
            else:
                logger.warning(f"⚠️ [CONNECTION_ROUTE] Non-bytes message: {type(message)}")
                return None
        except Exception as e:
            logger.error(f"🚨 [CONNECTION_ERROR] route_message failed: {e}")
            import traceback
            logger.error(f"🚨 [CONNECTION_ERROR] Traceback: {traceback.format_exc()}")
            # フォールバック: 直接audio_handlerを呼び出し
            if hasattr(audio_handler, 'handle_audio_frame'):
                await audio_handler.handle_audio_frame(message)
            return None
        
    async def _handle_binary_message(self, message: bytes, audio_handler):
        """Server2準拠のバイナリメッセージ処理"""
        
        logger.info(f"🎯 [BINARY_DEBUG] _handle_binary_message start: {len(message)}B")
        
        # Step 1: Connection層DTXフィルタ (Server2 connection.py:375)
        try:
            dtx_threshold = int(os.getenv("DTX_THRESHOLD", "12"))
            logger.info(f"🎯 [BINARY_DEBUG] DTX check: {len(message)}B vs threshold {dtx_threshold}")
            
            if len(message) <= dtx_threshold:
                self.dtx_drop_count += 1
                if self.dtx_drop_count % 50 == 0:
                    logger.info(
                        f"🛡️ [CONNECTION_DTX] DTX小パケット破棄: {self.dtx_drop_count}回 "
                        f"UTT#{self.utt_seq} bytes={len(message)} (likely DTX/keepalive) threshold={dtx_threshold}"
                    )
                logger.info(f"🎯 [BINARY_DEBUG] Message dropped by DTX filter")
                return  # 完全破棄
            
            logger.info(f"🎯 [BINARY_DEBUG] Message passed DTX filter, proceeding to stats")
        except Exception as e:
            logger.error(f"🚨 [BINARY_ERROR] DTX filter error: {e}")
            pass
            
        # Step 2: 統計更新 (Server2準拠)
        self._rx_frame_count += 1
        self._rx_bytes_total += len(message)
        self.rx_frames_since_listen += 1
        self.rx_bytes_since_listen += len(message)
        
        # Step 3: 統計ログ (Server2準拠)
        if (self.rx_frames_since_listen % 50) == 0:
            logger.info(
                f"📊 [AUDIO_TRACE] UTT#{self.utt_seq} recv frames={self.rx_frames_since_listen}, bytes={self.rx_bytes_since_listen}"
            )
        
        if (self._rx_frame_count % 25) == 0:
            logger.info(
                f"📈 [CONNECTION_STATS] 音声フレーム受信統計: {self._rx_frame_count} フレーム, {self._rx_bytes_total} バイト"
            )
            
        # Step 4: receiveAudioHandle層への転送
        await self._forward_to_audio_handler(message, audio_handler)
        
    async def _forward_to_audio_handler(self, audio: bytes, audio_handler):
        """Server2 receiveAudioHandle.py準拠の処理"""
        
        # receiveAudioHandle DTXフィルタ (line 22)
        try:
            dtx_thr = int(os.getenv("DTX_THRESHOLD", "3"))
        except Exception:
            dtx_thr = 3
            
        if audio and len(audio) <= dtx_thr:
            try:
                logger.debug(f"🚫 [AUDIO_DTX] DROP_DTX pkt={len(audio)}B")
            except Exception:
                pass
            return  # DTX破棄
            
        # Server2準拠の音声処理へ
        if hasattr(audio_handler, 'handle_audio_frame'):
            await audio_handler.handle_audio_frame(audio)
