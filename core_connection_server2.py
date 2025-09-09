"""
Server2-style Connection Handler for Server3
完全なServer2互換の接続・メッセージルーティング制御
"""
import os
import time
import asyncio
from typing import Dict, Any
from utils.logger import setup_logger

logger = setup_logger()
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
            if isinstance(message, bytes):
                result = await self._handle_binary_message(message, audio_handler)
                return result
            else:
                logger.warning(f"⚠️ [CONNECTION_ROUTE] Non-bytes message: {type(message)}")
                return None
        except Exception as e:
            logger.error(f"🚨S2🚨 ★TEST★ [CONNECTION_ERROR] route_message failed: {e}")
            import traceback
            logger.error(f"🚨S2🚨 ★TEST★ [CONNECTION_ERROR] Traceback: {traceback.format_exc()}")
            # フォールバック: 直接audio_handlerを呼び出し
            if hasattr(audio_handler, 'handle_audio_frame'):
                await audio_handler.handle_audio_frame(message)
            return None
        
    async def _handle_binary_message(self, message: bytes, audio_handler):
        """Server2準拠のバイナリメッセージ処理"""
        
        # Step 1: Server2準拠エコー防止フィルタ（最優先）
        try:
            # AI発話中は全音声を完全無視（マイクオフ状態）
            client_is_speaking = getattr(audio_handler, 'client_is_speaking', False)
            # デバッグ: client_is_speaking状態を5フレームに1回確認
            if hasattr(self, '_debug_counter'):
                self._debug_counter += 1
            else:
                self._debug_counter = 1
            
            if self._debug_counter % 5 == 0:
                handler_id = id(audio_handler)
                has_attr = hasattr(audio_handler, 'client_is_speaking')
                logger.info(f"🔍 [MIC_DEBUG] client_is_speaking={client_is_speaking}, msg_size={len(message)}B, handler_id={handler_id}, has_attr={has_attr}")
            
            if client_is_speaking:
                # AI発話中は全ての音声フレームを無視（完全マイクオフ）
                logger.info(f"🔇 [MIC_OFF] AI発話中マイクオフ: {len(message)}B - 全音声破棄（エコー完全防止）")
                return  # 全音声完全破棄
        except Exception as e:
            logger.error(f"🚨 [MIC_OFF_ERROR] マイクオフエラー: {e}")
            pass
            
        # Step 2: Connection層DTXフィルタ (Server2 connection.py:375)
        try:
            dtx_threshold = int(os.getenv("DTX_THRESHOLD", "12"))
            
            if len(message) <= dtx_threshold:
                self.dtx_drop_count += 1
                if self.dtx_drop_count % 100 == 0:
                    logger.info(
                        f"🛡️ [CONNECTION_DTX] DTX小パケット破棄: {self.dtx_drop_count}回 "
                        f"UTT#{self.utt_seq} bytes={len(message)} (likely DTX/keepalive) threshold={dtx_threshold}"
                    )
                return  # 完全破棄
        except Exception as e:
            logger.error(f"🚨 [BINARY_ERROR] DTX filter error: {e}")
            pass
            
        # Step 3: 統計更新 (Server2準拠)
        self._rx_frame_count += 1
        self._rx_bytes_total += len(message)
        self.rx_frames_since_listen += 1
        self.rx_bytes_since_listen += len(message)
        
        # Step 4: 統計ログ (Server2準拠)
        if (self.rx_frames_since_listen % 100) == 0:
            logger.info(
                f"📊 [AUDIO_TRACE] UTT#{self.utt_seq} recv frames={self.rx_frames_since_listen}, bytes={self.rx_bytes_since_listen}"
            )
        
        if (self._rx_frame_count % 50) == 0:
            logger.info(
                f"📈 [CONNECTION_STATS] 音声フレーム受信統計: {self._rx_frame_count} フレーム, {self._rx_bytes_total} バイト"
            )
            
        # Step 5: receiveAudioHandle層への転送
        await self._forward_to_audio_handler(message, audio_handler)
        
    async def _forward_to_audio_handler(self, audio: bytes, audio_handler):
        """Server2 receiveAudioHandle.py準拠の処理"""
        
        # receiveAudioHandle DTXフィルタ (line 22) - より厳格に
        try:
            dtx_thr = int(os.getenv("DTX_THRESHOLD_HANDLER", "8"))  # より大きな閾値で二重防御
        except Exception:
            dtx_thr = 8
            
        if audio and len(audio) <= dtx_thr:
            try:
                logger.debug(f"🚫 [AUDIO_DTX] DROP_DTX pkt={len(audio)}B")
            except Exception:
                pass
            return  # DTX破棄
            
        # Server2準拠の音声処理へ
        if hasattr(audio_handler, 'handle_audio_frame'):
            await audio_handler.handle_audio_frame(audio)
