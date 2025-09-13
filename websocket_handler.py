import asyncio
import json
import struct
import uuid
import io
import threading
import time
from typing import Dict, Any, Optional
from datetime import datetime, timedelta
import pytz
from collections import deque
from aiohttp import web

class ConnectionClosedError(Exception):
    """WebSocket connection closed exception"""
    pass

from config import Config
from utils.logger import setup_logger
from utils.auth import AuthManager, AuthError
from audio.asr import ASRService
from audio.tts import TTSService
from ai.llm import LLMService
from ai.memory import MemoryService
from audio_handler_server2 import AudioHandlerServer2

logger = setup_logger()

class ConnectionHandler:
    def __init__(self, websocket: web.WebSocketResponse, headers: Dict[str, str]):
        self.websocket = websocket
        self.headers = headers
        self.device_id = headers.get("device-id") or "unknown"
        self.client_id = headers.get("client-id") or str(uuid.uuid4())
        self.protocol_version = int(headers.get("protocol-version", "1"))
        import time as time_module  # スコープエラー回避
        self.session_id = f"session_{int(time_module.time())}"  # Server2準拠のセッションID
        
        self.asr_service = ASRService()
        self.tts_service = TTSService()
        self.llm_service = LLMService()
        self.memory_service = MemoryService()

        self.chat_history = deque(maxlen=10) # Store last 10 messages
        self.client_is_speaking = False
        self.stop_event = threading.Event() # For graceful shutdown (server2 style)
        self.session_id = str(uuid.uuid4())
        self.audio_format = "opus"  # Default format (ESP32 sends Opus like server2)
        self.features = {}
        self.close_after_chat = False  # Server2準拠: チャット後の接続制御
        
        # Audio buffering (server2 style)
        self.asr_audio = []  # List of Opus frames (server2 style)
        self.client_have_voice = False
        self.client_voice_stop = False
        self.last_activity_time = time.time()
        
        # Server2準拠: タイムアウト監視（環境変数で調整可能）
        self.timeout_seconds = Config.WEBSOCKET_TIMEOUT_SECONDS
        
        # 🎯 3. ACK + 再送キュー機能
        self.pending_alarms = {}  # {message_id: alarm_data}
        self.alarm_ack_timeouts = {}  # {message_id: timeout_task}
        logger.info(f"🕐 [TIMEOUT_CONFIG] WebSocket timeout set to: {self.timeout_seconds} seconds")
        
        self.timeout_task = None
        
        # Initialize server2-style audio handler
        self.audio_handler = AudioHandlerServer2(self)
        # デバッグ用: per-frame Δt ログ出力を制御するフラグ（False: 無効）
        self.debug_tts_timing = False
        # 累積バースト検出カウンタ
        self._tts_burst_total = 0
        
        # Welcome message compatible with ESP32 (Server2準拠)
        self.welcome_msg = {
            "type": "hello",
            "version": 1,  # ★重要★ESP32が期待するversionフィールド
            "transport": "websocket", 
            "session_id": self.session_id,
            "audio_params": {
                "format": "opus",
                "sample_rate": 16000,
                "channels": 1,
                "frame_duration": 60  # Server2準拠の60ms
            }
        }

        logger.info(f"ConnectionHandler initialized for device: {self.device_id}, protocol v{self.protocol_version}")

    async def handle_message(self, message):
        """Handle both text (JSON) and binary (audio) messages"""
        if isinstance(message, str):
            logger.info(f"📨 [DEBUG] Received TEXT message: {message[:100]}... from {self.device_id}")
            await self.handle_text_message(message)
        elif isinstance(message, bytes):
            # logger.info(f"🎤 [DEBUG] Received BINARY audio data: {len(message)} bytes from {self.device_id}")  # レート制限対策で削除
            await self.handle_binary_message(message)

    async def handle_text_message(self, message: str):
        try:
            msg_json = json.loads(message)
            msg_type = msg_json.get("type")

            if msg_type == "hello":
                await self.handle_hello_message(msg_json)
            elif msg_type == "abort":
                logger.info(f"Abort message received from {self.device_id}")
                self.client_is_speaking = False
            elif msg_type == "listen":
                await self.handle_listen_message(msg_json)
            elif msg_type == "text":
                text_input = msg_json.get("data", "")
                if text_input:
                    await self.process_text(text_input)
            elif msg_type == "ack":
                # 🎯 [ACK_HANDLER] ESP32からのACK受信処理
                await self.handle_ack_message(msg_json)
            else:
                logger.warning(f"Unknown message type from {self.device_id}: {msg_type}")

        except json.JSONDecodeError:
            logger.error(f"Invalid JSON from {self.device_id}: {message[:100]}...")
        except Exception as e:
            logger.error(f"Error handling text message from {self.device_id}: {e}")

    async def handle_binary_message(self, message: bytes):
        """Handle binary audio data based on protocol version"""
        try:
            # 📊 [DATA_TRACKER] 受信データ完全追跡
            msg_size = len(message)
            current_time = time.monotonic()

            # 🛑 [DTX_ABSOLUTE_DROP_EARLY] 1-5ByteのDTXフレームを入口で即座に破棄（サーバ負荷軽減）
            if msg_size <= 5:
                if not hasattr(self, '_dtx_drop_count'):
                    self._dtx_drop_count = 0
                self._dtx_drop_count += 1
                if self._dtx_drop_count % 50 == 0:
                    logger.info(f"🛑 [DTX_ABSOLUTE_DROP] Early entrance DTX drop: {self._dtx_drop_count} total")
                return  # 入口で完全破棄
            
            # 🔍 [FLOOD_DETECTION] 大量送信検知
            if not hasattr(self, '_last_msg_time'):
                self._last_msg_time = current_time
                self._msg_count_1sec = 0
                self._total_bytes_1sec = 0
            
            time_diff = current_time - self._last_msg_time
            if time_diff < 1.0:  # 1秒以内
                self._msg_count_1sec += 1
                self._total_bytes_1sec += msg_size
            else:
                # 1秒経過: 統計リセット
                if self._msg_count_1sec > 20:  # 1秒に20フレーム以上
                    logger.warning(f"🚨 [FLOOD_ALERT] ESP32大量送信検知: {self._msg_count_1sec}フレーム/秒, {self._total_bytes_1sec}bytes/秒")
                self._last_msg_time = current_time
                self._msg_count_1sec = 1
                self._total_bytes_1sec = msg_size
            
            # 📈 [SIZE_HISTOGRAM] サイズ別分類
            if msg_size == 1:
                size_category = "DTX"
            elif msg_size < 50:
                size_category = "SMALL"
            elif msg_size < 150:
                size_category = "NORMAL"
            else:
                size_category = "LARGE"
            
            # 🔍 [SOURCE_TRACE] 送信元プログラム推定
            if not hasattr(self, '_size_stats'):
                self._size_stats = {"DTX": 0, "SMALL": 0, "NORMAL": 0, "LARGE": 0}
            self._size_stats[size_category] += 1
            
            # 🎯 [ROOT_CAUSE] 根本原因推定ログ
            total_frames = sum(self._size_stats.values())
            if total_frames % 50 == 0:  # 50フレーム毎に分析
                dtx_ratio = self._size_stats["DTX"] / total_frames * 100
                normal_ratio = self._size_stats["NORMAL"] / total_frames * 100
                logger.info(f"🔍 [ROOT_CAUSE] フレーム構成分析: DTX={dtx_ratio:.1f}% NORMAL={normal_ratio:.1f}% (total={total_frames})")
                
                # 根本原因推定
                if dtx_ratio > 60:
                    logger.warning(f"🎯 [CAUSE_DTX] DTX大量送信: おそらく無音検知の誤動作またはマイク感度過敏")
                elif normal_ratio > 50:
                    logger.warning(f"🎯 [CAUSE_VOICE] 音声フレーム大量送信: おそらくVAD異常またはマイク回り込み")
                else:
                    logger.warning(f"🎯 [CAUSE_MIXED] 混合送信: マイク制御異常の可能性")
            
            # A. 入口で落とす（最重要）- AI発話中+クールダウン中完全ブロック
            # 🎯 [MONOTONIC_TIME] 単一時基統一: monotonic使用でシステム時刻変更に耐性
            now_ms = time.monotonic() * 1000
            is_ai_speaking = hasattr(self, 'audio_handler') and getattr(self.audio_handler, 'client_is_speaking', False)
            is_cooldown = hasattr(self, 'audio_handler') and now_ms < getattr(self.audio_handler, 'tts_cooldown_until', 0)
            
            if is_ai_speaking or is_cooldown:
                # B. WebSocket入口で必ず落とす（最重要）
                # 同一の時基でガード（ユーザー指摘の通り）
                if not hasattr(self, 'ws_gate_drops'):
                    self.ws_gate_drops = 0
                if not hasattr(self, '_ws_block_count'):
                    self._ws_block_count = 0
                    
                self.ws_gate_drops += 1
                self._ws_block_count += 1
                
                # 統計・デバッグ情報
                block_reason = "AI発話中" if is_ai_speaking else f"クールダウン中(残り{int(getattr(self.audio_handler, 'tts_cooldown_until', 0) - now_ms)}ms)"
                
                # ログは30フレームに1回（詳細確認のため頻度上げ）
                if self._ws_block_count % 30 == 0:
                    logger.info(f"🚪 [WS_ENTRANCE_BLOCK] {block_reason}入口ブロック: {size_category}({msg_size}B) 過去30フレーム完全破棄 (累計={self.ws_gate_drops})")
                return  # 即座に破棄
            
            # Server2準拠: 小パケットでも活動時間を更新（ESP32からの継続通信を認識）
            self.last_activity_time = time.time()
            
            # 📊 [TRAFFIC_LOG] 送信データ詳細ログ（★入口ガード通過★ - AI非発話＆クールダウン外）
            if not hasattr(self, '_packet_log_count'):
                self._packet_log_count = 0
            self._packet_log_count += 1
            
            # (DTX は入口で既に破棄済み)
            
            # 🚨 [ESP32_DEBUG] ESP32修正後のフレーム詳細分析
            logger.info(f"📊 [FRAME_DETAIL] ★Server受信★ {size_category}({msg_size}B) hex={message[:min(8, len(message))].hex()} count/sec={self._msg_count_1sec} bytes/sec={self._total_bytes_1sec} protocol=v{self.protocol_version}")
            
            # 通常時も10フレームに1回に制限（より詳細に）
            if self._packet_log_count % 10 == 0:
                logger.info(f"📊 [TRAFFIC_DETAIL] ★入口ガード通過★ {size_category}({msg_size}B) count/sec={self._msg_count_1sec} bytes/sec={self._total_bytes_1sec} protocol=v{self.protocol_version}")
            
            # 🚨 [IMMEDIATE_FLOOD] リアルタイム洪水警告 + 緊急遮断
            if self._msg_count_1sec > 30:  # 30フレーム/秒超過時の緊急対策
                avg_size = self._total_bytes_1sec / self._msg_count_1sec if self._msg_count_1sec > 0 else 0
                logger.error(f"🚨 [CRITICAL_FLOOD] ESP32からの異常大量送信: {self._msg_count_1sec}フレーム/秒, {self._total_bytes_1sec}bytes/秒 (平均{avg_size:.1f}B/フレーム) → WebSocket切断リスク")
                
                # 🔍 [DEBUG_THRESHOLD] 閾値デバッグ
                logger.error(f"🔍 [THRESHOLD_DEBUG] 現在: {self._msg_count_1sec}フレーム/秒, 閾値: 25フレーム/秒, 超過: {self._msg_count_1sec > 25}")
                
                # 緊急遮断: 高頻度フレームを強制破棄
                if self._msg_count_1sec > 10:  # 10フレーム/秒超過で強制破棄（ESP32ファームウェア未更新対策）
                    logger.error(f"🛑 [EMERGENCY_DROP] 緊急フレーム破棄: {self._msg_count_1sec}フレーム/秒, {size_category}({msg_size}B) → 接続保護のため破棄")
                    
                    # 🔍 [DROP_ANALYSIS] 破棄理由分析
                    if not hasattr(self, '_drop_stats'):
                        self._drop_stats = {"DTX": 0, "SMALL": 0, "NORMAL": 0, "LARGE": 0}
                    self._drop_stats[size_category] += 1
                    logger.error(f"🔍 [DROP_STATS] 破棄統計: DTX={self._drop_stats['DTX']} NORMAL={self._drop_stats['NORMAL']} SMALL={self._drop_stats['SMALL']}")
                    
                    return  # 強制破棄して接続を保護
                else:
                    logger.error(f"🔍 [NO_DROP] 破棄条件未満: {self._msg_count_1sec}フレーム/秒 <= 10 → 処理継続")
            
            # 旧来の小パケットスキップを一時無効化（Server2 Connection Handlerで処理）
            # if len(message) <= 12:  # Skip very small packets (DTX/keepalive) but keep activity alive
            #     logger.info(f"⏭️ [DEBUG] Skipping small packet: {len(message)} bytes (activity updated)")
            #     return
                
            if self.protocol_version == 2:
                # Protocol v2: version(2) + type(2) + reserved(2) + timestamp(4) + payload_size(4) + payload
                if len(message) < 14:
                    return
                version, msg_type, reserved, timestamp, payload_size = struct.unpack('>HHHII', message[:14])
                audio_data = message[14:14+payload_size]
            elif self.protocol_version == 3:
                # Protocol v3: type(1) + reserved(1) + payload_size(2) + payload
                if len(message) < 4:
                    return
                msg_type, reserved, payload_size = struct.unpack('>BBH', message[:4])
                audio_data = message[4:4+payload_size]
                # logger.info(f"📋 [PROTO] v3: type={msg_type}, payload_size={payload_size}, extracted_audio={len(audio_data)} bytes")  # ログ削減
            else:
                # Protocol v1: raw audio data
                audio_data = message

            # Server2完全準拠: Connection Handlerを使用（全プロトコル共通）
            if not hasattr(self, 'connection_handler'):
                from core_connection_server2 import Server2StyleConnectionHandler
                self.connection_handler = Server2StyleConnectionHandler()
                logger.info("🎯 [CONNECTION_INIT] Server2StyleConnectionHandler initialized")
                
            # Server2準拠のメッセージルーティング
            try:
                await self.connection_handler.route_message(audio_data, self.audio_handler)
            except Exception as route_error:
                logger.error(f"🚨S2🚨 ★TEST★ [ROUTE_ERROR] route_message failed: {route_error}")
                import traceback
                logger.error(f"🚨S2🚨 ★TEST★ [ROUTE_ERROR] Traceback: {traceback.format_exc()}")
                # フォールバック: 直接audio_handlerを呼び出し
                await self.audio_handler.handle_audio_frame(audio_data)
            
            # 注意: 活動時間更新は既にメソッド冒頭で実行済み
            
        except Exception as e:
            logger.error(f"🚨 [CRITICAL_ERROR] Binary message processing failed for {self.device_id}: {e}")
            logger.error(f"🚨 [CRITICAL_ERROR] Message details: len={len(message)}, protocol_v={self.protocol_version}")
            logger.error(f"🚨 [CRITICAL_ERROR] Message hex: {message.hex() if len(message) <= 100 else message[:100].hex()}")
            import traceback
            logger.error(f"🚨 [CRITICAL_ERROR] Full traceback: {traceback.format_exc()}")
            # Continue processing despite error to avoid connection drop
            raise  # Re-raise to trigger WebSocket disconnect investigation

    async def handle_hello_message(self, msg_json: Dict[str, Any]):
        """Handle ESP32 hello message"""
        logger.info(f"Received hello from {self.device_id}")
        
        # Store client audio parameters
        audio_params = msg_json.get("audio_params")
        if audio_params:
            self.audio_format = audio_params.get("format", "opus")
            self.welcome_msg["audio_params"] = audio_params
            logger.info(f"Client audio format: {self.audio_format}")
            
        # Store client features  
        features = msg_json.get("features")
        if features:
            self.features = features
            logger.info(f"Client features: {features}")
            
        # Send welcome response
        if self.websocket.closed or getattr(self.websocket, '_writer', None) is None:
            logger.error(f"💀 [WEBSOCKET_DEAD] Cannot send welcome message - connection dead")
            return
        await self.websocket.send_str(json.dumps(self.welcome_msg))
        logger.info(f"✅ [HELLO_RESPONSE] Sent welcome message to {self.device_id}: {self.welcome_msg}")
        logger.info(f"🤝 [HANDSHAKE] WebSocket handshake completed successfully for {self.device_id}")
        
        # Server2準拠: タイムアウト監視タスク起動
        self.timeout_task = asyncio.create_task(self._check_timeout())
        logger.info(f"Started timeout monitoring task for {self.device_id}")
        
        # WebSocket再接続時の未送信アラーム再送チェック
        await self._check_pending_alarms()

    async def handle_listen_message(self, msg_json: Dict[str, Any]):
        """Handle listen state changes"""
        state = msg_json.get("state")
        mode = msg_json.get("mode")
        
        if state == "start":
            # 3) 「listen:start」も無視（TTS中/クールダウン中）
            # 🎯 [MONOTONIC_TIME] 単一時基統一
            now_ms = time.monotonic() * 1000
            is_ai_speaking = hasattr(self, 'audio_handler') and getattr(self.audio_handler, 'client_is_speaking', False)
            is_cooldown = hasattr(self, 'audio_handler') and now_ms < getattr(self.audio_handler, 'tts_cooldown_until', 0)
            
            if is_ai_speaking or is_cooldown:
                if not hasattr(self, '_ignored_listen_count'):
                    self._ignored_listen_count = 0
                self._ignored_listen_count += 1
                
                block_reason = "AI発話中" if is_ai_speaking else f"クールダウン中"
                logger.info(f"🎤 [LISTEN_IGNORE] {block_reason}のlisten:start無視 (計{self._ignored_listen_count}回)")
                return  # listen:start を無視
            
            # Server2準拠: listen start時の完全バッファクリア
            logger.info(f"🧹 [LISTEN_START_CLEAR] Listen開始: バッファ完全クリア実行")
            if hasattr(self, 'audio_handler'):
                # ASRバッファクリア
                if hasattr(self.audio_handler, 'audio_frames'):
                    cleared_frames = len(self.audio_handler.audio_frames)
                    self.audio_handler.audio_frames.clear()
                    if cleared_frames > 0:
                        logger.info(f"🧹 [LISTEN_ASR_CLEAR] Listen開始時ASRバッファクリア: {cleared_frames}フレーム")
                
                # VAD状態リセット
                if hasattr(self.audio_handler, 'silence_count'):
                    self.audio_handler.silence_count = 0
                if hasattr(self.audio_handler, 'last_voice_time'):
                    self.audio_handler.last_voice_time = 0
                if hasattr(self.audio_handler, 'wake_until'):
                    self.audio_handler.wake_until = 0
                    
            logger.info(f"Client {self.device_id} started listening")

    async def handle_ack_message(self, msg_json: dict):
        """🎯 [ACK_HANDLER] ESP32からのACK受信処理"""
        original_type = msg_json.get("original_type")
        action = msg_json.get("action")
        
        logger.info(f"🔍 [ACK_DEBUG] Received ACK: original_type={original_type}, action={action}, full_json={msg_json}")
        
        if original_type == "audio_control" and action == "mic_off":
            self._mic_ack_received = True
            logger.info(f"✅ [ACK_RECEIVED] ESP32 confirmed mic_off: {msg_json}")
        elif original_type == "audio_control" and action == "mic_on":
            logger.info(f"✅ [ACK_RECEIVED] ESP32 confirmed mic_on: {msg_json}")
        elif original_type == "alarm_set":
            # 🎯 alarm_set ACK処理
            message_id = msg_json.get("message_id")
            if message_id:
                self._handle_alarm_ack(message_id)
            else:
                logger.warning(f"⚠️ [ALARM_ACK_NO_ID] alarm_set ACK without message_id: {msg_json}")
        else:
            logger.info(f"✅ [ACK_RECEIVED] Unknown ACK: {msg_json}")


    async def process_accumulated_audio(self):
        """Process accumulated voice audio data"""
        try:
            logger.info(f"🎯 [AUDIO_START] ===== Processing accumulated audio: {len(self.audio_buffer)} bytes =====")
            
            # Convert Opus to WAV using server2 method
            logger.info(f"🔍 [ASR] Audio format: {self.audio_format}, buffer size: {len(self.audio_buffer)}")
            
            if self.audio_format == "opus":
                logger.info(f"🔄 [WEBSOCKET] Processing as Opus format")
                # Convert Opus to WAV using server2 method
                try:
                    import wave
                    import opuslib_next
                    
                    logger.info(f"🔄 [WEBSOCKET] Converting Opus buffer to WAV (server2 method)")
                    
                    # For debugging: save original data
                    logger.info(f"🔍 [OPUS_DEBUG] ===== First 20 bytes: {bytes(self.audio_buffer[:20]).hex()} =====")
                    
                    # Method 1: Try as single packet
                    try:
                        decoder = opuslib_next.Decoder(16000, 1)  # 16kHz, mono
                        pcm_data = decoder.decode(bytes(self.audio_buffer), 960)  # 60ms frame
                        logger.info(f"✅ [WEBSOCKET] Single packet decode success: {len(pcm_data)} bytes PCM")
                    except Exception as e1:
                        logger.warning(f"⚠️ [WEBSOCKET] Single packet failed: {e1}")
                        
                        # Method 2: Just try as raw PCM data instead of Opus
                        logger.warning(f"⚠️ [WEBSOCKET] Trying as raw PCM data instead")
                        # Assume it's already PCM 16-bit mono at 16kHz
                        pcm_data = bytes(self.audio_buffer)
                        logger.info(f"✅ [WEBSOCKET] Using raw data as PCM: {len(pcm_data)} bytes")
                    
                    # Create WAV file from PCM
                    wav_buffer = io.BytesIO()
                    with wave.open(wav_buffer, 'wb') as wav_file:
                        wav_file.setnchannels(1)  # mono
                        wav_file.setsampwidth(2)  # 16-bit
                        wav_file.setframerate(16000)  # 16kHz
                        wav_file.writeframes(pcm_data)
                    
                    wav_buffer.seek(0)
                    audio_file = wav_buffer
                    audio_file.name = "audio.wav"
                    logger.info(f"🎉 [WEBSOCKET] Converted Opus to WAV: {len(self.audio_buffer)} -> {len(pcm_data)} bytes PCM")
                    
                except Exception as e:
                    logger.error(f"❌ [WEBSOCKET] Opus conversion failed: {e}")
                    # Fallback: Create empty WAV (better than Opus for OpenAI)
                    wav_buffer = io.BytesIO()
                    with wave.open(wav_buffer, 'wb') as wav_file:
                        wav_file.setnchannels(1)
                        wav_file.setsampwidth(2) 
                        wav_file.setframerate(16000)
                        wav_file.writeframes(b'\x00' * 1600)  # 100ms of silence
                    wav_buffer.seek(0)
                    audio_file = wav_buffer
                    audio_file.name = "audio.wav"
                    logger.info(f"⚠️ [WEBSOCKET] Fallback: sending silent WAV")
            else:
                # Process as PCM data (ESP32 default)
                logger.info(f"🔄 [WEBSOCKET] Processing as PCM format")
                try:
                    import wave
                    
                    # Create WAV file from raw PCM data
                    wav_buffer = io.BytesIO()
                    with wave.open(wav_buffer, 'wb') as wav_file:
                        wav_file.setnchannels(1)  # mono
                        wav_file.setsampwidth(2)  # 16-bit
                        wav_file.setframerate(16000)  # 16kHz
                        wav_file.writeframes(bytes(self.audio_buffer))
                    
                    wav_buffer.seek(0)
                    audio_file = wav_buffer
                    audio_file.name = "audio.wav"
                    logger.info(f"✅ [WEBSOCKET] Created WAV from PCM: {len(self.audio_buffer)} bytes")
                    
                except Exception as e:
                    logger.error(f"❌ [WEBSOCKET] PCM to WAV conversion failed: {e}")
                    # Fallback: raw data
                    audio_file = io.BytesIO(bytes(self.audio_buffer))
                    audio_file.name = "audio.wav"
            
            # Convert audio to text using ASR
            logger.info(f"🎤 [ASR_START] ===== Calling OpenAI Whisper API =====")
            transcribed_text = await self.asr_service.transcribe(audio_file)
            logger.info(f"📝 [ASR_RESULT] ===== ASR Result: '{transcribed_text}' (length: {len(transcribed_text) if transcribed_text else 0}) =====")
            
            if transcribed_text and transcribed_text.strip():
                logger.info(f"✅ [ASR] Processing transcription: {transcribed_text}")
                await self.process_text(transcribed_text)
            else:
                logger.warning(f"❌ [ASR] No valid result for {self.device_id}")
                
        except Exception as e:
            logger.error(f"❌ [AUDIO_ERROR] ===== Error processing accumulated audio from {self.device_id}: {e} =====")


    async def process_text(self, text: str, rid: str = None):
        """Process text input through LLM and generate response"""
        try:
            if not rid:
                import uuid
                rid = str(uuid.uuid4())[:8]
            
            # 🎯 検索可能ログ: START_TO_CHAT
            logger.info(f"🔥 RID[{rid}] START_TO_CHAT: '{text}' (tts_active={getattr(self, 'tts_active', False)})")

            # TTS中は新しいテキスト処理を拒否
            if hasattr(self, 'tts_active') and self.tts_active:
                logger.warning(f"🔥 RID[{rid}] START_TO_CHAT_BLOCKED: TTS中のため拒否")
                return

            # 重複実行防止
            if hasattr(self, '_processing_text') and self._processing_text:
                logger.warning(f"🔥 RID[{rid}] START_TO_CHAT_DUPLICATE: 既に処理中のため拒否")
                return

            self._processing_text = True
            
            # アクティブTTS RIDをセット（後でAbort判定に使用）
            if hasattr(self.audio_handler, 'active_tts_rid'):
                self.audio_handler.active_tts_rid = rid
            
            logger.info(f"🔥 RID[{rid}] LLM_START: Processing '{text}'")
            self.chat_history.append({"role": "user", "content": text})

            # Check for alarm-related keywords first (highest priority)
            if any(keyword in text for keyword in ["起こして", "アラーム", "目覚まし", "時に鳴らして"]):
                logger.info(f"⏰ [ALARM_TRIGGER] Alarm request detected: '{text}'")
                
                # 🎯 シンプル確実: アラーム設定のみ、AI応答なし
                alarm_result = await self._process_alarm_request_simple(text)
                return
            
            # Check for alarm stop keywords
            elif any(keyword in text for keyword in ["アラーム止めて", "止めて", "アラーム停止", "もういい", "起きた"]):
                logger.info(f"⏰ [ALARM_STOP] Alarm stop request detected: '{text}'")
                await self.send_audio_response("はい、アラームを止めましたにゃん！おはようございます！", rid)
                return
            
            # Check for memory-related keywords
            memory_query = None
            logger.info(f"🧠 [MEMORY_CHECK] Checking text for memory keywords: '{text}'")
            
            # 先に呼び出しキーワードをチェック（優先度高）
            if ("覚えてる" in text or "記憶ある" in text or "教えて" in text or 
                "何が好き" in text or "誕生日はいつ" in text or "知ってる" in text or "記憶してる" in text):
                memory_query = text
                logger.info(f"🧠 [MEMORY_QUERY_TRIGGER] Memory query triggered! Query: '{text}'")
            elif "覚えて" in text or "覚えといて" in text or "記憶して" in text:
                # Extract what to remember
                memory_to_save = text.replace("覚えて", "").replace("覚えといて", "").replace("記憶して", "").strip()
                logger.info(f"🧠 [MEMORY_TRIGGER] Memory save triggered! Content: '{memory_to_save}'")
                
                if memory_to_save:
                    success = await self.memory_service.save_memory(self.device_id, memory_to_save)
                    if success:
                        logger.info(f"🧠 [MEMORY_SUCCESS] Memory saved successfully!")
                        await self.send_audio_response("はい、覚えました。")
                    else:
                        logger.error(f"🧠 [MEMORY_FAILED] Memory save failed!")
                        await self.send_audio_response("すみません、記憶できませんでした。")
                    return
                else:
                    logger.warning(f"🧠 [MEMORY_EMPTY] No content to save after keyword removal")

            # Prepare messages for LLM
            llm_messages = list(self.chat_history)
            if memory_query:
                logger.info(f"🔍 [MEMORY_SEARCH] Starting memory search for query: '{memory_query}'")
                retrieved_memory = await self.memory_service.query_memory(self.device_id, memory_query)
                if retrieved_memory:
                    llm_messages.insert(0, {"role": "system", "content": f"ユーザーの記憶: {retrieved_memory}"})
                    logger.info(f"✅ [MEMORY_FOUND] Retrieved memory for LLM: {retrieved_memory[:50]}...")
                else:
                    logger.info(f"❌ [MEMORY_NOT_FOUND] No memory found for query: '{memory_query}'")

            # Generate LLM response (server2 style - no extra keepalive)
            llm_response = await self.llm_service.chat_completion(llm_messages)
            
            if llm_response and llm_response.strip():
                logger.info(f"🔥 RID[{rid}] LLM_RESULT: '{llm_response}'")
                self.chat_history.append({"role": "assistant", "content": llm_response})
                
                # STT message already sent at ASR completion for fast display
                # (LLM完了後の重複送信を避けるためコメントアウト)
                
                # Generate and send audio response
                logger.info(f"🔥 RID[{rid}] TTS_START: Starting audio generation")
                await self.send_audio_response(llm_response, rid)
            else:
                logger.warning(f"🔥 RID[{rid}] LLM_NO_RESPONSE: No response from LLM")
                
        except Exception as e:
            logger.error(f"Error processing text from {self.device_id}: {e}")
        finally:
            self._processing_text = False

    async def handle_abort_message(self, rid: str, source: str = "unknown"):
        """Server2のhandleAbortMessage相当処理 - RID追跡対応"""
        try:
            logger.warning(f"🔥 RID[{rid}] HANDLE_ABORT_MESSAGE: source={source}, active_tts_rid={getattr(self.audio_handler, 'active_tts_rid', 'None')}")
            
            # TTS停止状態設定
            self.tts_active = False
            self._processing_text = False
            
            # Server2準拠: Abort時もマイク制御リセット
            if hasattr(self, 'audio_handler'):
                self.audio_handler.client_is_speaking = False
                logger.info(f"🎤 [MIC_CONTROL] Abort時AI発話停止: client_is_speaking=False")
            
            # ESP32にTTS停止メッセージ送信 (server2準拠)
            abort_message = {
                "type": "tts", 
                "state": "stop", 
                "session_id": getattr(self, 'session_id', 'unknown')
            }
            if self.websocket.closed or getattr(self.websocket, '_writer', None) is None:
                logger.error(f"💀 [WEBSOCKET_DEAD] Cannot send abort message - connection dead")
                return
            await self.websocket.send_str(json.dumps(abort_message))
            logger.info(f"🔥 RID[{rid}] TTS_ABORT_SENT: Sent TTS stop message to ESP32")
            
            # Abort後の録音再開制御（重要！）
            mic_on_message = {
                "type": "audio_control", 
                "action": "mic_on", 
                "reason": "abort_recovery"
            }
            listen_start_message = {
                "type": "listen", 
                "state": "start", 
                "mode": "continuous"
            }
            try:
                if self.websocket.closed or getattr(self.websocket, '_writer', None) is None:
                    logger.error(f"💀 [WEBSOCKET_DEAD] Cannot send recovery messages - connection dead")
                    return
                await self.websocket.send_str(json.dumps(mic_on_message))
                await self.websocket.send_str(json.dumps(listen_start_message))
                logger.info(f"🔥 RID[{rid}] ABORT_RECOVERY: マイクON+録音再開指示送信完了")
            except Exception as e:
                logger.warning(f"🔥 RID[{rid}] ABORT_RECOVERY_FAILED: {e}")
            
            # 音声処理状態クリア
            if hasattr(self.audio_handler, 'asr_audio'):
                self.audio_handler.asr_audio.clear()
            if hasattr(self.audio_handler, 'is_processing'):
                logger.warning(f"🔥 RID[{rid}] IS_PROCESSING_ABORT: Setting is_processing=False")
                self.audio_handler.is_processing = False
                
            logger.info(f"🔥 RID[{rid}] HANDLE_ABORT_MESSAGE_END: TTS interruption handled")
            
        except Exception as e:
            logger.error(f"🔥 RID[{rid}] HANDLE_ABORT_MESSAGE_ERROR: {e}")

    async def handle_barge_in_abort(self):
        """Server2のhandleAbortMessage相当処理"""
        try:
            # 呼び出し元を詳細追跡
            import traceback
            full_stack = traceback.format_stack()
            caller_details = []
            for i, frame in enumerate(full_stack[-4:-1]):
                caller_details.append(f"Level{i}: {frame.strip()}")
            
            logger.warning("🚨 [BARGE_IN_ABORT] Handling TTS interruption - server2 style")
            logger.warning(f"🔍 [ABORT_CALL_STACK] {' | '.join(caller_details)}")
            
            # TTS停止状態設定
            self.tts_active = False
            self._processing_text = False
            
            # ESP32にTTS停止メッセージ送信 (server2準拠)
            abort_message = {
                "type": "tts", 
                "state": "stop", 
                "session_id": getattr(self, 'session_id', 'unknown')
            }
            await self.websocket.send_str(json.dumps(abort_message))
            logger.info("📱 [TTS_ABORT] Sent TTS stop message to ESP32")
            
            # 音声処理状態クリア
            if hasattr(self.audio_handler, 'asr_audio'):
                self.audio_handler.asr_audio.clear()
            if hasattr(self.audio_handler, 'is_processing'):
                logger.warning(f"🚨 [IS_PROCESSING_ABORT] Setting is_processing=False in handle_barge_in_abort")
                self.audio_handler.is_processing = False
                
            logger.info("✅ [BARGE_IN_ABORT] TTS interruption handled successfully")
            
        except Exception as e:
            logger.error(f"❌ [BARGE_IN_ABORT] Error handling TTS interruption: {e}")

    async def send_stt_message(self, text: str):
        """Send STT message to display user input (server2 style)"""
        try:
            # Enhanced connection check
            if self.websocket.closed or getattr(self.websocket, '_writer', None) is None:
                logger.warning(f"⚠️ [WEBSOCKET] Connection closed/invalid, cannot send STT to {self.device_id}")
                return
                
            # Send STT message (server2 style) - テキストから句読点・絵文字除去
            cleaned_text = self._clean_text_for_display(text)
            stt_message = {"type": "stt", "text": cleaned_text, "session_id": self.session_id}
            if self.websocket.closed or getattr(self.websocket, '_writer', None) is None:
                logger.error(f"💀 [WEBSOCKET_DEAD] Cannot send STT message - connection dead")
                return
            await self.websocket.send_str(json.dumps(stt_message))
            logger.info(f"🟢XIAOZHI_STT_SENT🟢 📱 [STT] Sent user text to display: '{text}'")
        except Exception as e:
            logger.error(f"🔴XIAOZHI_STT_ERROR🔴 Error sending STT message to {self.device_id}: {e}")
    
    def _clean_text_for_display(self, text: str) -> str:
        """Server2準拠: テキストから句読点・絵文字を除去"""
        if not text:
            return text
        
        # 基本的な句読点・記号除去
        punctuation_chars = "，。！？、；：（）【】「」『』〈〉《》,.!?;:()[]<>{}"
        cleaned = text
        
        # 先頭・末尾の句読点・空白除去
        start = 0
        while start < len(cleaned) and (cleaned[start].isspace() or cleaned[start] in punctuation_chars):
            start += 1
            
        end = len(cleaned) - 1
        while end >= start and (cleaned[end].isspace() or cleaned[end] in punctuation_chars):
            end -= 1
            
        return cleaned[start:end + 1] if start <= end else text
    
    def _fix_pronunciation_for_tts(self, text: str) -> str:
        """TTS用の発音修正"""
        if not text:
            return text
        
        # 発音修正辞書（ハードコード）
        pronunciation_fixes = {
            "ネコ太": "ネコタ",
            "君": "きみ",
            "君は": "きみは", 
            "君が": "きみが",
            "君の": "きみの",
            "君を": "きみを",
            "君と": "きみと",
            "君に": "きみに",
            "君で": "きみで",
            "君も": "きみも"
        }
        
        fixed_text = text
        for wrong, correct in pronunciation_fixes.items():
            fixed_text = fixed_text.replace(wrong, correct)
        
        return fixed_text
    
    async def _process_alarm_request(self, text: str) -> str:
        """音声からアラーム設定を処理"""
        import re
        import datetime
        
        try:
            # 相対時刻パターンを先にチェック
            relative_patterns = [
                r"(\d{1,2})分後",                 # "5分後", "30分後"
                r"(\d{1,2})時間後",               # "1時間後", "2時間後"
                r"(\d{1,2})時間(\d{1,2})分後"     # "1時間30分後"
            ]
            
            hour, minute = None, 0
            is_relative = False
            
            # 相対時刻の処理
            for pattern in relative_patterns:
                match = re.search(pattern, text)
                if match:
                    is_relative = True
                    now = datetime.datetime.now()
                    
                    if "分後" in pattern and "時間" not in pattern:
                        # N分後
                        minutes_later = int(match.group(1))
                        target_time = now + datetime.timedelta(minutes=minutes_later)
                    elif "時間後" in pattern and "分後" not in pattern:
                        # N時間後
                        hours_later = int(match.group(1))
                        target_time = now + datetime.timedelta(hours=hours_later)
                    elif "時間" in pattern and "分後" in pattern:
                        # N時間M分後
                        hours_later = int(match.group(1))
                        minutes_later = int(match.group(2))
                        target_time = now + datetime.timedelta(hours=hours_later, minutes=minutes_later)
                    
                    hour = target_time.hour
                    minute = target_time.minute
                    logger.info(f"⏰ [RELATIVE_TIME] {text} → {target_time.strftime('%H:%M')}")
                    break
            
            # 絶対時刻パターン（相対時刻が見つからなかった場合）
            if not is_relative:
                time_patterns = [
                    r"(\d{1,2})時(\d{1,2}?)分?",      # "7時30分", "7時"
                    r"(\d{1,2}):(\d{2})",             # "7:30"  
                    r"(\d{1,2})時半",                 # "7時半"
                    r"午前(\d{1,2})時",               # "午前7時"
                    r"午後(\d{1,2})時"                # "午後7時"
                ]
                
                for pattern in time_patterns:
                    match = re.search(pattern, text)
                    if match:
                        if "時半" in pattern:
                            hour = int(match.group(1))
                            minute = 30
                        elif "午前" in pattern:
                            hour = int(match.group(1))
                        elif "午後" in pattern:
                            hour = int(match.group(1)) + 12
                        else:
                            hour = int(match.group(1))
                            if match.group(2):
                                minute = int(match.group(2))
                        break
            
            if hour is None:
                logger.warning(f"⏰ [ALARM_PARSE] Could not extract time from: '{text}'")
                return None
            
            # 日付の判定
            if is_relative:
                # 相対時刻の場合は既に計算済み
                target_date = target_time.date()
            else:
                # 絶対時刻の場合の日付判定
                target_date = datetime.date.today()
                if "明日" in text:
                    target_date += datetime.timedelta(days=1)
                elif "今日" in text:
                    target_date = datetime.date.today()
                else:
                    # 現在時刻より前なら明日に設定
                    now = datetime.datetime.now()
                    if hour < now.hour or (hour == now.hour and minute <= now.minute):
                        target_date += datetime.timedelta(days=1)
            
            # アラームメッセージの生成
            alarm_message = f"ネコ太からのお知らせにゃん！"
            if "起きて" in text or "起こして" in text:
                alarm_message = "起きる時間だにゃん！おはようございます！"
            
            # アラーム設定API呼び出し
            alarm_success = await self._create_alarm_via_api(
                date=target_date.strftime("%Y-%m-%d"),
                time=f"{hour:02d}:{minute:02d}",
                message=alarm_message
            )
            
            if alarm_success:
                date_str = "今日" if target_date == datetime.date.today() else "明日"
                logger.info(f"⏰ [ALARM_SUCCESS] Alarm set for {target_date} {hour:02d}:{minute:02d}")
                
                # ESP32にアラーム設定通知を送信
                await self._send_alarm_notification(target_date, hour, minute)
                
                return f"はい！{date_str}の{hour}時{minute:02d}分にアラームを設定しましたにゃん！電源管理を調整するので、アラーム時刻になったら自動で起こしますよ！"
            else:
                logger.error(f"⏰ [ALARM_FAILED] Failed to create alarm")
                # エラーの種類に応じたユーザーフレンドリーなメッセージを返す
                return self._get_alarm_error_message()
                
        except Exception as e:
            logger.error(f"⏰ [ALARM_ERROR] Error processing alarm request: {e}")
            return None
    
    async def _process_alarm_request_optimized(self, text: str, rid: str):
        """最適化されたアラーム処理: 通知→ACK→TTS別スレッド"""
        try:
            # 1. アラーム作成 + 軽量通知送信
            alarm_result = await self._process_alarm_request(text)
            
            if alarm_result:
                logger.info(f"⏰ [OPTIMIZED_FLOW] Phase 1: Alarm notification sent, waiting for ACK...")
                
                # 2. ACK確認待機（最大2秒）
                ack_received = await self._wait_for_latest_alarm_ack(timeout=2.0)
                
                # asyncioインポートを先頭で実行
                import asyncio
                
                if ack_received:
                    logger.info(f"✅ [OPTIMIZED_FLOW] Phase 2: ACK confirmed, waiting for WebSocket to stabilize...")
                    
                    # 🎯 WebSocket安定化待機（フレーム処理を落ち着かせる）
                    await asyncio.sleep(0.5)  # 500ms待機でフレーム処理安定化
                    logger.info(f"🌊 [WEBSOCKET_STABLE] WebSocket stabilized, starting TTS in background")
                    
                    # 3. TTS を別スレッドで開始（ブロックしない）
                    audio_task = asyncio.create_task(self.send_audio_response(alarm_result, rid))
                    logger.info(f"🎵 [BACKGROUND_TTS] TTS started in background after stabilization")
                else:
                    logger.warning(f"⚠️ [OPTIMIZED_FLOW] ACK timeout, proceeding with TTS anyway")
                    # ACKタイムアウトでもTTSは実行
                    audio_task = asyncio.create_task(self.send_audio_response(alarm_result, rid))
            else:
                # アラーム作成失敗
                await self.send_audio_response("アラームの設定に失敗しました。時間を教えてくださいにゃん。", rid)
                
        except Exception as e:
            logger.error(f"⏰ [OPTIMIZED_ERROR] Error in optimized alarm flow: {e}")
            await self.send_audio_response("アラームの設定でエラーが発生しました。", rid)
    
    async def _wait_for_latest_alarm_ack(self, timeout: float) -> bool:
        """最新のアラームACKを待機"""
        import asyncio
        
        # 最新のpending alarmのmessage_idを取得
        if not self.pending_alarms:
            return False
            
        latest_message_id = list(self.pending_alarms.keys())[-1]
        
        # ACK待機ループ
        start_time = asyncio.get_event_loop().time()
        while (asyncio.get_event_loop().time() - start_time) < timeout:
            if latest_message_id not in self.pending_alarms:
                # ACK受信済み（pendingから削除された）
                logger.info(f"🎯 [ACK_WAIT] ACK received for message: {latest_message_id}")
                return True
            await asyncio.sleep(0.1)  # 100ms間隔でチェック
        
        logger.warning(f"⏰ [ACK_WAIT] Timeout waiting for ACK: {latest_message_id}")
        return False
    
    async def _process_alarm_setting_only(self, text: str) -> bool:
        """アラーム設定のみを処理（TTS応答なし）"""
        import re
        import datetime
        
        try:
            # 相対時刻パターンを先にチェック
            relative_patterns = [
                r"(\d{1,2})分後",                 # "5分後", "30分後"
                r"(\d{1,2})時間後",               # "1時間後", "2時間後"
                r"(\d{1,2})時間(\d{1,2})分後"     # "1時間30分後"
            ]
            
            hour, minute = None, 0
            is_relative = False
            
            # 相対時刻の処理
            for pattern in relative_patterns:
                match = re.search(pattern, text)
                if match:
                    is_relative = True
                    now = datetime.datetime.now()
                    
                    if "分後" in pattern and "時間" not in pattern:
                        # N分後
                        minutes_later = int(match.group(1))
                        target_time = now + datetime.timedelta(minutes=minutes_later)
                    elif "時間後" in pattern and "分後" not in pattern:
                        # N時間後
                        hours_later = int(match.group(1))
                        target_time = now + datetime.timedelta(hours=hours_later)
                    elif "時間" in pattern and "分後" in pattern:
                        # N時間M分後
                        hours_later = int(match.group(1))
                        minutes_later = int(match.group(2))
                        target_time = now + datetime.timedelta(hours=hours_later, minutes=minutes_later)
                    
                    hour = target_time.hour
                    minute = target_time.minute
                    logger.info(f"⏰ [RELATIVE_TIME] {text} → {target_time.strftime('%H:%M')}")
                    break
            
            # 絶対時刻パターン（相対時刻が見つからなかった場合）
            if not is_relative:
                time_patterns = [
                    r"(\d{1,2})時(\d{1,2}?)分?",      # "7時30分", "7時"
                    r"(\d{1,2}):(\d{2})",             # "7:30"  
                    r"(\d{1,2})時半",                 # "7時半"
                ]
                
                for pattern in time_patterns:
                    match = re.search(pattern, text)
                    if match:
                        if "時半" in pattern:
                            hour = int(match.group(1))
                            minute = 30
                        else:
                            hour = int(match.group(1))
                            minute = int(match.group(2)) if match.group(2) else 0
                        break
            
            if hour is None:
                logger.warning(f"⏰ [TIME_PARSE_FAILED] Could not extract time from: '{text}'")
                return False
            
            # 24時間形式に変換
            if 0 <= hour <= 23:
                if hour < 12 and any(keyword in text for keyword in ["午後", "夜", "夕方"]):
                    hour += 12
                elif hour == 12 and any(keyword in text for keyword in ["午前", "朝"]):
                    hour = 0
            
            # 明日のアラームか今日のアラームか判定（ユーザーのタイムゾーンで計算）
            import pytz
            
            # ユーザーのタイムゾーンを取得（デフォルトはJST）
            user_timezone = self._get_user_timezone()
            user_tz = pytz.timezone(user_timezone)
            current_time_user = datetime.datetime.now(user_tz)
            target_date = current_time_user.date()
            
            # アラーム時刻をユーザータイムゾーンで作成
            alarm_datetime_user = datetime.datetime.combine(target_date, datetime.time(hour, minute))
            alarm_datetime_user = user_tz.localize(alarm_datetime_user)
            
            # もし設定時刻が現在時刻より前なら、明日に設定
            if alarm_datetime_user <= current_time_user:
                target_date = target_date + datetime.timedelta(days=1)
                logger.info(f"⏰ [TOMORROW_ALARM] Setting alarm for tomorrow ({user_timezone}): {target_date} {hour:02d}:{minute:02d}")
            else:
                logger.info(f"⏰ [TODAY_ALARM] Setting alarm for today ({user_timezone}): {target_date} {hour:02d}:{minute:02d}")
            
            # アラームメッセージ
            alarm_message = f"アラーム: {hour:02d}:{minute:02d}"
            
            # アラーム設定API呼び出し
            alarm_success = await self._create_alarm_via_api(
                date=target_date.strftime("%Y-%m-%d"),
                time=f"{hour:02d}:{minute:02d}",
                message=alarm_message
            )
            
            if alarm_success:
                logger.info(f"⏰ [ALARM_SUCCESS] Alarm set for {target_date} {hour:02d}:{minute:02d}")
                # ESP32にアラーム設定通知を送信
                await self._send_alarm_notification(target_date, hour, minute)
                
                # ディスプレイにアラーム設定完了メッセージを表示
                display_msg = {
                    "type": "display_text",
                    "text": "アラーム設定完了",
                    "duration": 3000
                }
                await self.websocket.send_str(json.dumps(display_msg))
                logger.info(f"📱 [DISPLAY] Sent alarm setting completion message")
                
                return True
            else:
                logger.error(f"⏰ [ALARM_FAILED] Failed to create alarm")
                return False
                
        except Exception as e:
            logger.error(f"⏰ [ALARM_ERROR] Error processing alarm request: {e}")
            return False

    async def _process_alarm_request_simple(self, text: str):
        """シンプルなアラーム処理: 設定のみ、AI応答なし"""
        try:
            # 1. アラーム設定処理（TTS応答なし）
            alarm_result = await self._process_alarm_setting_only(text)
            
            if alarm_result:
                logger.info(f"⏰ [SIMPLE_ALARM] Alarm set successfully, no TTS response")
                
                # 2. 固定の「アラーム設定完了」メッセージを画面表示のみ
                display_msg = {
                    "type": "display_text",
                    "text": "アラーム設定完了",
                    "duration": 3000  # 3秒表示
                }
                
                import json
                await self.websocket.send_str(json.dumps(display_msg))
                logger.info(f"📱 [FIXED_DISPLAY] Sent fixed alarm setting message to display")
                
            else:
                # 設定失敗時も固定メッセージ
                error_msg = {
                    "type": "display_text", 
                    "text": "アラーム設定失敗",
                    "duration": 3000
                }
                
                import json
                await self.websocket.send_str(json.dumps(error_msg))
                logger.info(f"📱 [FIXED_ERROR] Sent fixed error message to display")
                
        except Exception as e:
            logger.error(f"⏰ [SIMPLE_ERROR] Error in simple alarm flow: {e}")
    
    def _get_user_timezone(self):
        """ユーザーのタイムゾーンを取得（デフォルトはJST）"""
        # TODO: ユーザー設定からタイムゾーンを取得
        # 現在はデフォルトでJSTを使用
        # 将来的には以下のように実装：
        # user_settings = self.get_user_settings()
        # return user_settings.get('timezone', 'Asia/Tokyo')
        
        # 世界中対応のタイムゾーンマップ
        region_timezone_map = {
            'JP': 'Asia/Tokyo',
            'US': 'America/New_York',
            'GB': 'Europe/London',
            'DE': 'Europe/Berlin',
            'FR': 'Europe/Paris',
            'AU': 'Australia/Sydney',
            'CN': 'Asia/Shanghai',
            'KR': 'Asia/Seoul',
            'HK': 'Asia/Hong_Kong',
            'SG': 'Asia/Singapore'
        }
        
        # デフォルトはJST
        return 'Asia/Tokyo'
    
    async def _create_alarm_via_api(self, date: str, time: str, message: str) -> bool:
        """nekota-server APIを使ってアラームを作成"""
        try:
            import httpx
            
            # デバイス番号からuser_idを取得
            device_number = "327546"  # 登録済みデバイス番号
            jwt_token, user_id = await self.memory_service._get_valid_jwt_and_user(device_number)
            
            if not jwt_token or not user_id:
                logger.error(f"⏰ [ALARM_API] Failed to get valid JWT for device {device_number}")
                return False
            
            # アラーム作成API呼び出し
            async with httpx.AsyncClient(timeout=10) as client:
                headers = {"Authorization": f"Bearer {jwt_token}"}
                payload = {
                    "user_id": user_id,
                    "date": date,
                    "time": time,
                    "timezone": "Asia/Tokyo",
                    "text": message
                }
                
                response = await client.post(
                    f"{Config.MANAGER_API_URL}/api/alarm",
                    headers=headers,
                    json=payload
                )
                
                if response.status_code in [200, 201]:
                    logger.info(f"⏰ [ALARM_API] Successfully created alarm: {date} {time}")
                    return True
                elif response.status_code == 403:
                    # アラーム制限エラーの詳細を保存
                    self.last_alarm_error = {
                        "type": "limit_reached",
                        "status_code": 403,
                        "message": response.text
                    }
                    logger.error(f"⏰ [ALARM_API] Alarm limit reached: {response.text}")
                    return False
                else:
                    # その他のエラー
                    self.last_alarm_error = {
                        "type": "api_error",
                        "status_code": response.status_code,
                        "message": response.text
                    }
                    logger.error(f"⏰ [ALARM_API] Failed to create alarm: {response.status_code} - {response.text}")
                    return False
                    
        except Exception as e:
            logger.error(f"⏰ [ALARM_API] Error calling alarm API: {e}")
            # ネットワークエラーなど
            self.last_alarm_error = {
                "type": "network_error",
                "message": str(e)
            }
            return False
    
    def _get_alarm_error_message(self) -> str:
        """アラーム作成失敗時のユーザーフレンドリーなメッセージを生成"""
        if not hasattr(self, 'last_alarm_error'):
            return "アラームの設定に失敗しましたにゃん。もう一度お試しくださいにゃ。"
        
        error = self.last_alarm_error
        error_type = error.get("type", "unknown")
        
        if error_type == "limit_reached":
            # アラーム制限到達時の丁寧な説明
            return ("申し訳ございませんにゃ！現在のプランでは、アラームは3個までしか設定できませんにゃん。"
                   "既存のアラームを削除するか、プレミアムプランにアップグレードすると無制限でアラームが使えますにゃ！"
                   "管理画面でアラームの管理ができますよ〜")
        elif error_type == "api_error":
            return "アラームの設定でエラーが発生しましたにゃん。少し時間をおいてから再度お試しくださいにゃ。"
        elif error_type == "network_error":
            return "ネットワークエラーでアラームが設定できませんでしたにゃん。インターネット接続を確認してくださいにゃ。"
        else:
            return "アラームの設定に失敗しましたにゃん。もう一度お試しくださいにゃ。"
    
    async def _send_alarm_notification(self, date, hour, minute):
        """ESP32にアラーム設定を通知＋電源管理制御"""
        try:
            # アラーム時刻までの秒数を計算
            import datetime
            target_datetime = datetime.datetime.combine(date, datetime.time(hour, minute))
            now = datetime.datetime.now()
            seconds_until_alarm = int((target_datetime - now).total_seconds())
            
            # 1. 優先送信: alarm_setメッセージ（ESP32のAlarmManagerに登録）
            import uuid
            message_id = str(uuid.uuid4())
            alarm_id = int(datetime.datetime.now().timestamp())
            
            # ユーザータイムゾーンでの現在時刻を取得
            user_timezone = self._get_user_timezone()
            user_tz = pytz.timezone(user_timezone)
            server_now_user = datetime.datetime.now(user_tz)
            
            # UTC時刻も取得（デバッグ用）
            utc_now = datetime.datetime.now(pytz.UTC)
            
            alarm_set_msg = {
                "type": "alarm_set",
                "message_id": message_id,  # 🎯 ACK追跡用ID
                "alarm_id": alarm_id,
                "alarm_date": date.strftime("%Y-%m-%d"),
                "alarm_time": f"{hour:02d}:{minute:02d}",
                "message": f"{hour:02d}:{minute:02d}のアラーム",
                "timezone": user_timezone,  # ユーザータイムゾーン
                "server_time": server_now_user.strftime("%Y-%m-%d %H:%M:%S"),  # サーバー現在時刻（ユーザータイムゾーン）
                "server_timestamp": int(server_now_user.timestamp())  # Unix timestamp（ユーザータイムゾーン）
            }
            
            # デバッグログ：送信する時刻情報を確認
            logger.info(f"🕐 [TIME_DEBUG] Server time (UTC): {utc_now.strftime('%Y-%m-%d %H:%M:%S')}")
            logger.info(f"🕐 [TIME_DEBUG] Server time (User TZ): {server_now_user.strftime('%Y-%m-%d %H:%M:%S')}")
            logger.info(f"🕐 [TIME_DEBUG] Server timestamp (User TZ): {int(server_now_user.timestamp())}")
            
            # 🎯 4. 再送キューに登録
            self.pending_alarms[message_id] = alarm_set_msg
            
            import json
            await self.websocket.send_str(json.dumps(alarm_set_msg))
            logger.info(f"🔔 [ALARM_SET] Sent alarm_set to ESP32: {date.strftime('%Y-%m-%d')} {hour:02d}:{minute:02d}, msg_id={message_id}")
            
            # 🎯 ACKタイムアウト設定（5秒）
            import asyncio
            timeout_task = asyncio.create_task(self._alarm_ack_timeout(message_id, 5.0))
            self.alarm_ack_timeouts[message_id] = timeout_task
            
            # 少し待機してから次のメッセージ送信
            await asyncio.sleep(0.1)
            
            # 2. 電源管理メッセージ（既存のpower_wakeup）
            power_wakeup_msg = {
                "type": "power_wakeup",
                "reason": "alarm_scheduled", 
                "seconds_until_alarm": seconds_until_alarm,
                "alarm_time": f"{hour:02d}:{minute:02d}",
                "alarm_date": date.strftime("%Y-%m-%d"),
                "message": f"アラーム設定: PowerSaveTimer WakeUp() - {seconds_until_alarm}秒後にアラーム"
            }
            
            await self.websocket.send_str(json.dumps(power_wakeup_msg))
            logger.info(f"⚡ [POWER_WAKEUP] Sent power_wakeup to ESP32: WakeUp() for alarm in {seconds_until_alarm}s")
            
            # サーバー側のタイムアウトも延長
            if seconds_until_alarm > 0:
                self.timeout_seconds = max(self.timeout_seconds, seconds_until_alarm + 60)  # アラーム時刻+1分
                logger.info(f"⏰ [SERVER_TIMEOUT] Extended server timeout to {self.timeout_seconds}s for alarm")
            
        except Exception as e:
            logger.error(f"⏰ [ALARM_NOTIFICATION] Failed to send alarm messages: {e}")
    
    async def _check_pending_alarms(self):
        """WebSocket再接続時に未送信アラームをチェック・再送"""
        try:
            import datetime
            import requests
            
            # デバイスIDを使ってアラーム情報を取得
            response = requests.get(
                f"https://nekota-server-production.up.railway.app/alarm/check?device_id={self.device_id}",
                timeout=5
            )
            
            if response.status_code == 200:
                alarms = response.json()
                logger.info(f"🔍 [ALARM_RESEND] Found {len(alarms)} pending alarms for {self.device_id}")
                
                # 未来のアラームのみ再送
                now = datetime.datetime.now()
                for alarm in alarms:
                    try:
                        alarm_datetime = datetime.datetime.fromisoformat(alarm['alarm_datetime'].replace('Z', '+00:00'))
                        if alarm_datetime > now:
                            # 再送実行
                            await self._send_alarm_notification(
                                alarm_datetime.date(),
                                alarm_datetime.hour,
                                alarm_datetime.minute
                            )
                            logger.info(f"🔄 [ALARM_RESENT] Resent alarm: {alarm['alarm_datetime']}")
                    except Exception as alarm_error:
                        logger.error(f"❌ [ALARM_RESEND_ERROR] Failed to resend alarm: {alarm_error}")
                        
        except Exception as e:
            logger.error(f"⏰ [ALARM_RESEND] Failed to check pending alarms: {e}")
    
    async def _alarm_ack_timeout(self, message_id: str, timeout_seconds: float):
        """ACKタイムアウト監視"""
        await asyncio.sleep(timeout_seconds)
        
        if message_id in self.pending_alarms:
            logger.warning(f"⏰ [ACK_TIMEOUT] No ACK received for alarm message: {message_id}")
            # 再送実行（最大3回）
            alarm_msg = self.pending_alarms[message_id]
            await self._resend_alarm(message_id, alarm_msg)
    
    async def _resend_alarm(self, message_id: str, alarm_msg: dict, retry_count: int = 0):
        """アラーム再送機能"""
        max_retries = 3
        if retry_count >= max_retries:
            logger.error(f"❌ [ALARM_RESEND_FAILED] Max retries exceeded for message: {message_id}")
            # 失敗時はペンディングから削除
            self.pending_alarms.pop(message_id, None)
            return
        
        try:
            import json
            await self.websocket.send_str(json.dumps(alarm_msg))
            logger.info(f"🔄 [ALARM_RESEND] Retry {retry_count + 1}/{max_retries} for message: {message_id}")
            
            # 次回タイムアウト設定
            timeout_task = asyncio.create_task(
                self._alarm_resend_timeout(message_id, alarm_msg, retry_count + 1, 5.0)
            )
            self.alarm_ack_timeouts[message_id] = timeout_task
            
        except Exception as e:
            logger.error(f"❌ [ALARM_RESEND_ERROR] Failed to resend alarm: {e}")
    
    async def _alarm_resend_timeout(self, message_id: str, alarm_msg: dict, retry_count: int, timeout_seconds: float):
        """再送タイムアウト監視"""
        await asyncio.sleep(timeout_seconds)
        
        if message_id in self.pending_alarms:
            await self._resend_alarm(message_id, alarm_msg, retry_count)
    
    def _handle_alarm_ack(self, message_id: str):
        """ACK受信処理"""
        if message_id in self.pending_alarms:
            logger.info(f"✅ [ALARM_ACK] Received ACK for message: {message_id}")
            
            # ペンディングから削除
            self.pending_alarms.pop(message_id, None)
            
            # タイムアウトタスクをキャンセル
            if message_id in self.alarm_ack_timeouts:
                self.alarm_ack_timeouts[message_id].cancel()
                self.alarm_ack_timeouts.pop(message_id, None)
        else:
            logger.warning(f"⚠️ [ALARM_ACK_UNKNOWN] Received ACK for unknown message: {message_id}")
    
    def _start_keepalive_for_alarm(self, date, hour, minute):
        """アラーム時刻までキープアライブを送信"""
        import asyncio
        import datetime
        
        async def keepalive_task():
            try:
                target_datetime = datetime.datetime.combine(date, datetime.time(hour, minute))
                logger.info(f"⏰ [KEEPALIVE] Starting keepalive until {target_datetime}")
                
                while datetime.datetime.now() < target_datetime:
                    # 25秒間隔でキープアライブ（30秒スリープより短く）
                    await asyncio.sleep(25)
                    
                    if hasattr(self, 'websocket') and self.websocket:
                        keepalive_msg = {
                            "type": "keepalive",
                            "timestamp": datetime.datetime.now().isoformat(),
                            "message": "アラーム待機中..."
                        }
                        import json
                        await self.websocket.send_str(json.dumps(keepalive_msg))
                        logger.debug(f"⏰ [KEEPALIVE] Sent keepalive message")
                    else:
                        logger.warning(f"⏰ [KEEPALIVE] WebSocket connection lost")
                        break
                        
                logger.info(f"⏰ [KEEPALIVE] Reached alarm time, stopping keepalive")
                
            except Exception as e:
                logger.error(f"⏰ [KEEPALIVE] Error in keepalive task: {e}")
        
        # バックグラウンドタスクとして実行
        asyncio.create_task(keepalive_task())

    async def send_audio_response(self, text: str, rid: str = None):
        """Generate and send audio response"""
        try:
            if not rid:
                import uuid
                rid = str(uuid.uuid4())[:8]
            
            # 🎯 検索可能ログ: TTS開始
            logger.info(f"🔥 RID[{rid}] TTS_GENERATION_START: '{text[:50]}...'")
            
            # 並行TTS検知
            if hasattr(self, 'tts_active') and self.tts_active:
                logger.warning(f"🔥 RID[{rid}] HANDLE_ABORT_MESSAGE: 並行TTS検知 - 前のTTSを中断")
                await self.handle_abort_message(rid, "parallel_tts")
            
            # 🔇 CRITICAL: TTS生成前に即座にマイクオフ（エコー予防）
            self.client_is_speaking = True
            if hasattr(self, 'audio_handler'):
                self.audio_handler.client_is_speaking = True  # 最優先でマイクオフ
                
                # Server2準拠: TTS開始保護期間設定（1200ms）
                tts_lock_ms = 1200
                self.audio_handler.speak_lock_until = time.time() * 1000 + tts_lock_ms
                logger.info(f"🛡️ [TTS_PROTECTION] TTS開始保護期間設定: {tts_lock_ms}ms")
                
                # 🎯 [HALF_DUPLEX] ハーフデュプレックス制御: mic_mute → ACK受領 → TTS送信
                mic_control_message = {
                    "type": "audio_control", 
                    "action": "mic_off", 
                    "reason": "tts_speaking"
                }
                try:
                    # 🔍 [CONNECTION_GUARD] 送信前WebSocket状態確認
                    if self.websocket.closed or getattr(self.websocket, '_writer', None) is None:
                        logger.error(f"💀 [WEBSOCKET_DEAD] Cannot send mic_off control - connection dead")
                        return
                        
                    await self.websocket.send_str(json.dumps(mic_control_message))
                    logger.info(f"📡 [DEVICE_CONTROL] 端末にマイクオフ指示送信: {mic_control_message}")
                    
                    # 🎯 [VAD_CONTROL] ESP32のVADバイパス指示（常時送信モード）
                    vad_control_message = {
                        "type": "vad_control", 
                        "action": "disable",  # disable = VADバイパス（常時送信）
                        "reason": "ai_speaking_preroll"  # プリロール対応
                    }
                    await self.websocket.send_str(json.dumps(vad_control_message))
                    logger.info(f"📡 [VAD_CONTROL] 端末にVADバイパス指示送信: {vad_control_message} (常時送信モード)")
                    
                    # 🎯 [ACK_WAIT] ACK待機（100ms短縮）またはフォールバック
                    ack_received = False
                    wait_start = time.monotonic()
                    while time.monotonic() - wait_start < 0.1:  # 100ms短縮待機
                        await asyncio.sleep(0.01)  # 10ms間隔でチェック
                        # ACKはhandle_text_messageで処理される
                        if hasattr(self, '_mic_ack_received') and self._mic_ack_received:
                            ack_received = True
                            self._mic_ack_received = False  # リセット
                            break
                    
                    if ack_received:
                        logger.info(f"✅ [ACK_RECEIVED] MIC_OFF ACK received, starting TTS")
                    else:
                        logger.info(f"⏱️ [ACK_TIMEOUT] MIC_OFF ACK timeout (100ms), but ESP32 firmware has mic control - proceeding with TTS")
                        
                except Exception as e:
                    logger.warning(f"📡 [DEVICE_CONTROL] マイクオフ指示送信失敗: {e}")
                
                # TTS開始時に録音バッファをクリア（溜まったフレーム一斉処理防止）
                if hasattr(self.audio_handler, 'audio_frames'):
                    cleared_frames = len(self.audio_handler.audio_frames)
                    self.audio_handler.audio_frames.clear()
                    if cleared_frames > 0:
                        logger.info(f"🗑️ [BUFFER_CLEAR] TTS開始時バッファクリア: {cleared_frames}フレーム破棄")
                
                logger.info(f"🎯 [CRITICAL_TEST] TTS開始: AI発言フラグON - エコーブロック開始")
                
                # Server2準拠: 端末にTTS開始メッセージ送信（重要！）
                tts_start_message = {
                    "type": "tts", 
                    "state": "start", 
                    "session_id": getattr(self, 'session_id', 'default')
                }
                if self.websocket.closed or getattr(self.websocket, '_writer', None) is None:
                    logger.error(f"💀 [WEBSOCKET_DEAD] Cannot send TTS start message - connection dead")
                    return
                await self.websocket.send_str(json.dumps(tts_start_message))
                logger.info(f"📡 [DEVICE_CONTROL] 端末にTTS開始指示送信: {tts_start_message}")
                
                self.audio_handler.tts_in_progress = True
                # TTS送信中は is_processing を強制維持
                self.audio_handler.is_processing = True
                handler_id = id(self.audio_handler)
                logger.info(f"🎤 [MIC_CONTROL] AI発話開始: client_is_speaking=True (エコー防止), handler_id={handler_id}")
                logger.info(f"🛡️ [TTS_PROTECTION] Set is_processing=True for TTS protection")
            
            # Check if websocket is still open (server2 style)
            # Enhanced connection validation
            if self.websocket.closed or getattr(self.websocket, '_writer', None) is None:
                logger.warning(f"⚠️ [WEBSOCKET] Connection closed/invalid, cannot send audio to {self.device_id}")
                return
            
            # Additional check: ensure websocket is still connected
            if not hasattr(self, 'websocket') or not self.websocket:
                logger.error(f"❌ [WEBSOCKET] WebSocket not connected: websocket={getattr(self, 'websocket', None)}")
                return
            
            # Generate audio using TTS
            logger.info(f"🔊 [TTS_START] ===== Generating TTS for: '{text}' =====")
            
            # TTS用の発音修正
            tts_text = self._fix_pronunciation_for_tts(text)
            if tts_text != text:
                logger.info(f"🗣️ [PRONUNCIATION_FIX] '{text}' → '{tts_text}'")
            
            # Send TTS start message (server2 style)
            try:
                tts_start_msg = {
                    "type": "tts", 
                    "state": "start", 
                    "session_id": self.session_id
                }
                if self.websocket.closed or getattr(self.websocket, '_writer', None) is None:
                    logger.error(f"💀 [WEBSOCKET_DEAD] Cannot send TTS start - connection dead")
                    return
                await self.websocket.send_str(json.dumps(tts_start_msg))
                logger.info(f"📢 [TTS] Sent TTS start message")
                
                # ハンドシェイク待ち: ESP32の音声受信準備完了まで待機
                logger.info(f"⏳ [HANDSHAKE] Waiting 500ms for ESP32 audio readiness")
                await asyncio.sleep(0.5)  # 500ms待機
            except Exception as status_error:
                logger.warning(f"⚠️ [TTS] Failed to send TTS start: {status_error}")
                return
            
            # Send sentence_start message with AI text (server2 critical addition)
            try:
                sentence_msg = {
                    "type": "tts",
                    "state": "sentence_start", 
                    "text": text,
                    "session_id": self.session_id
                }
                if self.websocket.closed or getattr(self.websocket, '_writer', None) is None:
                    logger.error(f"💀 [WEBSOCKET_DEAD] Cannot send TTS display - connection dead")
                    return
                await self.websocket.send_str(json.dumps(sentence_msg))
                logger.info(f"🟢XIAOZHI_TTS_DISPLAY_SENT🟢 📱 [TTS_DISPLAY] Sent AI text to display: '{text}'")
            except Exception as sentence_error:
                logger.error(f"🔴XIAOZHI_TTS_DISPLAY_ERROR🔴 ⚠️ [TTS] Failed to send sentence_start: {sentence_error}")
                return
            
            # Server2準拠: stop_eventチェック削除（TTS中断なし）
            
            # TTS処理前の接続状態チェック
            logger.info(f"🔍 [CONNECTION_CHECK] Before TTS generation: closed={self.websocket.closed}")
            
            # TTS生成中のタイムアウト対策：活動状態更新
            self.last_activity_time = time.time()
            
            # Generate TTS audio (server2 style - individual frames)
            opus_frames_list = await self.tts_service.generate_speech(tts_text)
            logger.info(f"🎶 [TTS_RESULT] ===== TTS generated: {len(opus_frames_list) if opus_frames_list else 0} individual Opus frames =====")
            
            # TTS処理後の活動状態更新とタイムアウト対策
            self.last_activity_time = time.time()
            logger.info(f"🔍 [CONNECTION_CHECK] After TTS generation: closed={self.websocket.closed}")
            
            # Server2完全移植: sendAudioHandle.py line 36-45 直接移植
            if opus_frames_list:
                try:
                    # 送信直前の最終接続確認
                    if self.websocket.closed:
                        logger.error(f"🚨 [CONNECTION_ERROR] WebSocket already closed before audio send")
                        return
                    
                    # 🎯 [CRITICAL_FIX] 二重送信防止: 個別フレーム送信のみに統一
                    total_frames = len(opus_frames_list)
                    logger.info(f"🎵 [UNIFIED_SEND] Unified individual frame sending: {total_frames} frames")
                    
                    if hasattr(self, 'websocket') and self.websocket and not self.websocket.closed:
                        # 🎯 [SERVER2_METHOD] Server2方式: bytes一括送信で安定化
                        frame_count = len(opus_frames_list)
                        
                        # デバッグ：最初のフレーム詳細解析
                        if frame_count > 0:
                            first_frame = opus_frames_list[0]
                            logger.info(f"🔬 [OPUS_DEBUG] First frame: size={len(first_frame)}bytes, hex_header={first_frame[:8].hex() if len(first_frame)>=8 else first_frame.hex()}")
                        
                        # 🚀 [SERVER2_EXACT] Server2完全再現: 60ms間隔個別フレーム送信
                        frame_duration_ms = 60  # Server2と同じ60ms
                        send_start_time = time.monotonic()
                        
                        # 送信前のWebSocket状態詳細チェック
                        logger.info(f"🔍 [WEBSOCKET_STATE] Before send: closed={self.websocket.closed}, state={getattr(self.websocket, 'state', 'unknown')}")
                        
                        if self.websocket.closed:
                            logger.error(f"❌ [SERVER2_EXACT] WebSocket already closed before sending")
                            raise Exception("WebSocket closed before audio send")
                        
                        logger.info(f"🎯 [SERVER2_EXACT] Sending {frame_count} frames individually, 60ms intervals (exactly like Server2)")
                        
                        try:
                            for frame_index, opus_frame in enumerate(opus_frames_list):
                                # WebSocket接続状態を毎フレームチェック
                                if self.websocket.closed:
                                    logger.error(f"❌ [SERVER2_EXACT_ERROR] WebSocket closed at frame {frame_index}/{frame_count}")
                                    break
                                
                                try:
                                    # 各フレームを個別に送信（Server2方式）
                                    await self.websocket.send_bytes(opus_frame)
                                    
                                    # 10フレーム毎に接続状態ログ
                                    if frame_index % 10 == 0:
                                        logger.debug(f"🔄 [SERVER2_PROGRESS] Frame {frame_index}/{frame_count}, WS state: closed={self.websocket.closed}")
                                    
                                except Exception as frame_error:
                                    logger.error(f"❌ [SERVER2_FRAME_ERROR] Frame {frame_index} failed: {frame_error}")
                                    # フレーム送信失敗時は即座に終了
                                    break
                                
                                # 最後のフレーム以外は待機（WebSocket安定性のため少し短めに）
                                if frame_index < len(opus_frames_list) - 1:
                                    await asyncio.sleep(0.040)  # 40ms（60ms→40msで高速化＋安定性向上）
                            
                            send_end_time = time.monotonic()
                            total_send_time = (send_end_time - send_start_time) * 1000  # ms
                            total_bytes = sum(len(frame) for frame in opus_frames_list)
                            
                            logger.info(f"✅ [SERVER2_EXACT_SUCCESS] Sent {frame_count} frames individually: {total_send_time:.1f}ms total")
                            logger.info(f"📊 [SERVER2_EXACT_STATS] Avg interval: {total_send_time/frame_count:.1f}ms, throughput: {total_bytes / total_send_time * 1000:.0f} bytes/sec")
                            
                        except Exception as send_error:
                            logger.error(f"❌ [SERVER2_EXACT_ERROR] Failed to send individual frames: {send_error}")
                            
                            # WebSocket切断が原因の場合は再接続を試行
                            if "closing transport" in str(send_error) or "closed" in str(send_error):
                                logger.warning(f"🔄 [WEBSOCKET_RECONNECT] Attempting reconnection due to transport closure")
                                # WebSocket切断フラグをセット（アプリケーション層で再接続処理）
                                self.websocket.closed = True
                            raise
                    else:
                        logger.error(f"❌ [V3_PROTOCOL] WebSocket disconnected before send")
                    
                    total_bytes = sum(len(frame) for frame in opus_frames_list)
                    logger.info(f"🔵XIAOZHI_AUDIO_SENT🔵 ※ここを送ってver2_AUDIO※ 🎵 [AUDIO_SENT] ===== Sent {total_frames} Opus frames to {self.device_id} ({total_bytes} total bytes) =====")
                    logger.info(f"🔍 [DEBUG_SEND] WebSocket state after audio send: closed={self.websocket.closed}")

                    # Send TTS stop message with cooldown info (server2 style + 回り込み防止)
                    tts_stop_msg = {"type": "tts", "state": "stop", "session_id": self.session_id, "cooldown_ms": 1200}  # 残響も含めた完全エコー除去のため1200msに延長
                    logger.info(f"🔍 [DEBUG_SEND] About to send TTS stop message: {tts_stop_msg}")
                    if self.websocket.closed or getattr(self.websocket, '_writer', None) is None:
                        logger.error(f"💀 [WEBSOCKET_DEAD] Cannot send TTS stop - connection dead")
                        return
                    await self.websocket.send_str(json.dumps(tts_stop_msg))
                    logger.info(f"🟡XIAOZHI_TTS_STOP🟡 ※ここを送ってver2_TTS_STOP※ 📢 [TTS] Sent TTS stop message with cooldown=1200ms")
                    logger.info(f"🔍 [DEBUG_SEND] WebSocket state after TTS stop: closed={self.websocket.closed}")
                    
                    # Server2準拠: TTS完了後の接続制御
                    if self.close_after_chat:
                        logger.info(f"🔴XIAOZHI_CLOSE_AFTER_CHAT🔴 Closing connection after chat completion for {self.device_id}")
                        await self.websocket.close()
                        return
                    else:
                        logger.info(f"🔵XIAOZHI_CONTINUE_CONNECTION🔵 Maintaining connection after TTS completion for {self.device_id}")
                        logger.info(f"🔍 [DEBUG_SEND] WebSocket final state: closed={self.websocket.closed}")

                except Exception as send_error:
                    logger.error(f"❌ [WEBSOCKET] Audio send failed to {self.device_id}: {send_error}")
                    logger.error(f"🔍 [DEBUG_SEND] WebSocket state after error: closed={self.websocket.closed}")
            else:
                logger.warning(f"Failed to generate audio for {self.device_id}")
                
        except Exception as e:
            logger.error(f"Error sending audio response to {self.device_id}: {e}")
        finally:
            # A. フラグOFFのタイミングをクールダウン後に一本化
            # ★重要★ TTS終了直後にはフラグOFFしない（WebSocket入口ガード維持）
            
            async def delayed_flag_off():
                try:
                    cooldown_ms = 1200  # ユーザー指摘の通り
                    # 🎯 [MONOTONIC_TIME] 単一時基統一
                    cooldown_until = time.monotonic() * 1000 + cooldown_ms
                    
                    # TTS終了直後にクールダウン期間設定（★フラグは維持★）
                    if hasattr(self, 'audio_handler'):
                        self.audio_handler.tts_cooldown_until = cooldown_until
                        
                        # Server2準拠: TTS終了時の完全バッファクリア（重要）
                        logger.info(f"🧹 [BUFFER_CLEAR_TTS_END] TTS終了時バッファクリア開始")
                        
                        # 1. ASR音声バッファクリア（クールダウン明けの流入防止）
                        if hasattr(self.audio_handler, 'audio_frames'):
                            cleared_frames = len(self.audio_handler.audio_frames)
                            self.audio_handler.audio_frames.clear()
                            logger.info(f"🧹 [ASR_BUFFER_CLEAR] ASRフレームバッファクリア: {cleared_frames}フレーム")
                        
                        # 2. VAD状態リセット（server2のreset_vad_states準拠）
                        if hasattr(self.audio_handler, 'silence_count'):
                            self.audio_handler.silence_count = 0
                        if hasattr(self.audio_handler, 'last_voice_time'):
                            self.audio_handler.last_voice_time = 0
                        if hasattr(self.audio_handler, 'wake_until'):
                            self.audio_handler.wake_until = 0
                        logger.info(f"🧹 [VAD_RESET] VAD状態リセット完了")
                        
                        # 3. RMSアキュムレータクリア
                        if hasattr(self.audio_handler, '_rms_buffer'):
                            self.audio_handler._rms_buffer = []
                        logger.info(f"🧹 [RMS_RESET] RMSバッファリセット完了")
                    
                    logger.info(f"🎯 [CRITICAL_TEST] TTS送信完了: フラグ維持中、クールダウン{cooldown_ms}ms開始、バッファ完全クリア")
                    
                    # クールダウン期間中はフラグ維持（WebSocket入口ガード維持）
                    cooldown_seconds = cooldown_ms / 1000.0
                    await asyncio.sleep(cooldown_seconds)
                    
                    # ★ここで初めてフラグOFF★（クールダウン満了後）
                    self.client_is_speaking = False
                    if hasattr(self, 'audio_handler'):
                        self.audio_handler.client_is_speaking = False  # AI発話確実終了
                        
                        # Server2準拠: 端末にTTS終了 + マイクオン指示送信
                        tts_stop_message = {
                            "type": "tts", 
                            "state": "stop", 
                            "session_id": getattr(self, 'session_id', 'default')
                        }
                        mic_on_message = {
                            "type": "audio_control", 
                            "action": "mic_on", 
                            "reason": "tts_finished"
                        }
                        try:
                            # 🔍 [CONNECTION_GUARD] WebSocket状態確認（最重要）
                            if self.websocket.closed or getattr(self.websocket, '_writer', None) is None:
                                logger.error(f"💀 [WEBSOCKET_DEAD] Connection closed during cooldown, cannot send control messages")
                                return
                                
                            # 1. TTS停止メッセージ（Server2準拠）
                            await self.websocket.send_str(json.dumps(tts_stop_message))
                            
                            # 2. マイクオン指示（拡張）
                            await self.websocket.send_str(json.dumps(mic_on_message))
                            
                            # 3. VAD判定復帰指示（ハングオーバ対応）
                            vad_enable_message = {
                                "type": "vad_control",
                                "action": "enable",  # enable = VAD判定復帰
                                "reason": "ai_finished_hangover"  # ハングオーバー対応
                            }
                            await self.websocket.send_str(json.dumps(vad_enable_message))
                            
                            # 4. 録音再開指示（重要！ESP32が自動再開しない場合の保険）
                            listen_start_message = {
                                "type": "listen", 
                                "state": "start", 
                                "mode": "continuous"
                            }
                            await self.websocket.send_str(json.dumps(listen_start_message))
                            
                            logger.info(f"📡 [DEVICE_CONTROL] 端末制御送信完了: TTS停止→マイクON→VAD判定復帰→録音再開")
                            logger.info(f"📡 [DEVICE_CONTROL] Messages: {tts_stop_message}, {mic_on_message}, {vad_enable_message}, {listen_start_message}")
                            logger.info(f"🎯 [VAD_STRATEGY] VADバイパス→通常判定復帰でプリロール/ハングオーバー対応")
                        except Exception as e:
                            logger.warning(f"📡 [DEVICE_CONTROL] 端末制御送信失敗: {e}")
                            logger.error(f"💀 [WEBSOCKET_ERROR] WebSocket状態: closed={getattr(self.websocket, 'closed', 'unknown')}, writer={getattr(self.websocket, '_writer', 'unknown')}")
                        
                        # D. 可視化（デバッグ）- TTS区間統計出力
                        ws_blocked = getattr(self, '_ws_block_count', 0)
                        ws_gate_total = getattr(self, 'ws_gate_drops', 0)
                        audio_blocked = getattr(self.audio_handler.handler if hasattr(self.audio_handler, 'handler') else None, 'blocked_frames', 0) if hasattr(self, 'audio_handler') else 0
                        cooldown_blocked = getattr(self.audio_handler, '_cooldown_log_count', 0) if hasattr(self, 'audio_handler') else 0
                        
                        logger.info(f"🎯 [CRITICAL_TEST] クールダウン満了: AI発言フラグOFF - WebSocket入口ガード解除")
                        logger.info(f"📊 [TTS_GUARD] WS入口blocked={ws_blocked} (累計={ws_gate_total}) Audio層blocked={audio_blocked} Cooldown期間blocked={cooldown_blocked}")
                        
                        # カウンターリセット（累計は維持）
                        if hasattr(self, '_ws_block_count'):
                            self._ws_block_count = 0
                            
                except Exception as e:
                    logger.error(f"🚨 [FLAG_OFF_ERROR] 遅延フラグOFFエラー: {e}")
                    # エラー時も確実にフラグOFF（安全弁）
                    self.client_is_speaking = False
                    if hasattr(self, 'audio_handler'):
                        self.audio_handler.client_is_speaking = False
            
            # ★TTS終了直後はフラグOFFしない★（従来の即座リセットを削除）
            # 唯一の例外対策として is_processing のみリセット
            if hasattr(self, 'audio_handler'):
                self.audio_handler.tts_in_progress = False
                self.audio_handler.is_processing = False
                
            # 非同期でクールダウン後フラグOFF実行
            asyncio.create_task(delayed_flag_off())
            
            logger.info(f"🔥 RID[{rid if 'rid' in locals() else 'unknown'}] TTS_COMPLETE: is_processing=False, フラグ維持中({1200}ms後OFF)")

    async def run(self):
        """Main connection loop - Server2 style with audio sync"""
        try:
            logger.info(f"🟢XIAOZHI_LOOP_START🟢 🚀 [WEBSOCKET_LOOP] Starting message loop for {self.device_id}")
            msg_count = 0
            connection_ended = False
            
            # アラーム時刻チェックタスクを開始
            alarm_task = asyncio.create_task(self.start_alarm_checker())
            timeout_task = asyncio.create_task(self._check_timeout())
            
            # 接続開始時に待機中のアラームがないかチェック
            await self._check_pending_alarms()
            
            # 詳細デバッグ: WebSocketメッセージ受信完全トレース
            try:
                logger.info(f"🔍 [DEBUG_LOOP] Starting async for loop for {self.device_id}, websocket.closed={self.websocket.closed}")
                last_msg_time = time.time()
                
                # 🚨 重要: Server2準拠のWebSocketメッセージ処理ループ
                logger.info(f"🔍 [LOOP_MONITOR] About to enter async for msg in self.websocket")
                async for msg in self.websocket:
                        # logger.info(f"🔍 [LOOP_MONITOR] Received message in async for loop")  # ログ削減
                    
                    # Server2準拠: ESP32切断メッセージの事前検知
                    if msg.type in (web.WSMsgType.CLOSE, web.WSMsgType.CLOSED, web.WSMsgType.ERROR):
                        logger.warning(f"🟣XIAOZHI_ESP32_CLOSE🟣 ESP32 initiated close: type={msg.type}, code={getattr(msg, 'extra', 'None')}")
                        connection_ended = True
                        break
                    msg_count += 1
                    current_time = time.time()
                    time_since_last = current_time - last_msg_time
                    last_msg_time = current_time
                    
                    # メッセージ間隔も監視
                    if time_since_last > 1.0:  # 1秒以上の間隔
                        logger.info(f"🔍 [DEBUG_LOOP] Long gap detected: {time_since_last:.1f}s since last message")
                    
                    # logger.info(f"🔍 [DEBUG_LOOP] Message {msg_count}: type={msg.type}({msg.type.value}), closed={self.websocket.closed}, data_len={len(msg.data) if hasattr(msg, 'data') and msg.data else 'None'}, gap={time_since_last:.1f}s")  # ログ削減
                    
                    # 🚨 処理前のWebSocket状態を記録
                    # logger.info(f"🔍 [LOOP_MONITOR] Before message processing: websocket.closed={self.websocket.closed}")  # ログ削減
                    
                    # Server2準拠: メッセージタイプ別処理
                    if msg.type == web.WSMsgType.TEXT:
                        logger.info(f"🔍 [DEBUG_LOOP] Processing TEXT message: {msg.data[:100]}...")
                        await self.handle_message(msg.data)
                        logger.info(f"🔍 [DEBUG_LOOP] TEXT message processed, continuing loop, closed={self.websocket.closed}")
                    elif msg.type == web.WSMsgType.BINARY:
                        # logger.info(f"🔍 [DEBUG_LOOP] Processing BINARY message: {len(msg.data)} bytes")  # ログ削減
                        await self.handle_message(msg.data)
                        # logger.info(f"🔍 [DEBUG_LOOP] BINARY message processed, continuing loop, closed={self.websocket.closed}")  # ログ削減
                    else:
                        logger.warning(f"🔍 [DEBUG_LOOP] Unknown message type: {msg.type}({msg.type.value}), ignoring and continuing")
                    
                    # 🚨 処理後のWebSocket状態を記録
                    # logger.info(f"🔍 [LOOP_MONITOR] After message processing: websocket.closed={self.websocket.closed}")  # ログ削減
                    
                    # ループ継続確認
                    logger.debug(f"🔍 [DEBUG_LOOP] Loop iteration {msg_count} complete, about to continue async for")
                    
                # 🚨 async for が終了した直後の詳細ログ
                logger.info(f"🔍 [LOOP_MONITOR] async for loop exited - investigating why")
                logger.info(f"🔍 [DEBUG_LOOP] async for loop ended naturally for {self.device_id}, final msg_count={msg_count}")
                logger.info(f"🔍 [DEBUG_LOOP] Time since last message when loop ended: {time.time() - last_msg_time:.1f}s")
                logger.info(f"🔍 [DEBUG_LOOP] WebSocket state: closed={self.websocket.closed}, close_code={getattr(self.websocket, 'close_code', 'None')}")
                
                # ESP32側切断詳細調査
                try:
                    # WebSocket状態詳細ログ
                    logger.info(f"🔍 [DEBUG_LOOP] WebSocket exception: {self.websocket.exception()}")
                except:
                    logger.info(f"🔍 [DEBUG_LOOP] No WebSocket exception")
                    
            except Exception as loop_error:
                logger.error(f"🔥XIAOZHI_ERROR🔥 ❌ [WEBSOCKET] Loop error for {self.device_id}: {loop_error}")
                connection_ended = True
                
            # 音声送信待機: WebSocketが正常で音声送信待ちの場合は継続
            if not connection_ended and not self.websocket.closed:
                logger.info(f"🎵 [WEBSOCKET_LOOP] Waiting for pending audio transmissions for {self.device_id}")
                # 最大3秒まで音声送信完了を待機
                wait_start = time.time()
                while not self.websocket.closed and (time.time() - wait_start) < 3.0:
                    await asyncio.sleep(0.1)
                    # 実際の音声送信完了チェックはここで実装可能
                    
            logger.info(f"🔵XIAOZHI_LOOP_COMPLETE🔵 ✅ [WEBSOCKET_LOOP] Loop completed for {self.device_id} after {msg_count} messages")
            logger.info(f"🔍 [DEBUG_LOOP] Final WebSocket state: closed={self.websocket.closed}, close_code={getattr(self.websocket, 'close_code', 'None')}")
        except Exception as e:
            logger.error(f"❌ [WEBSOCKET] Unhandled error in connection handler for {self.device_id}: {e}")
        finally:
            # Server2準拠: タイムアウト監視タスク終了
            if self.timeout_task and not self.timeout_task.done():
                self.timeout_task.cancel()
                try:
                    await self.timeout_task
                except asyncio.CancelledError:
                    pass
                    
            logger.info(f"🔍 [DEBUG] WebSocket loop ended for {self.device_id}, entering cleanup")
            
    async def _check_timeout(self):
        """Server2準拠: 接続タイムアウト監視"""
        try:
            while not self.stop_event.is_set():
                # 活動時間初期化チェック
                if self.last_activity_time > 0.0:
                    current_time = time.time()
                    inactive_time = current_time - self.last_activity_time
                    
                    if inactive_time > self.timeout_seconds:
                        if not self.stop_event.is_set():
                            logger.info(f"🕐 [TIMEOUT] ESP32 connection timeout after {inactive_time:.1f}s for {self.device_id}")
                            self.stop_event.set()
                            try:
                                await self.websocket.close()
                            except Exception as close_error:
                                logger.error(f"Error closing timeout connection: {close_error}")
                        break
                        
                # 1秒間隔でチェック
                await asyncio.sleep(1.0)
                
        except Exception as e:
            logger.error(f"Error in timeout check for {self.device_id}: {e}")
    
    async def start_alarm_checker(self):
        """アラーム時刻チェックタスクを開始"""
        try:
            while not self.stop_event.is_set():
                try:
                    await self._check_alarm_time()
                except Exception as e:
                    logger.error(f"⏰ [ALARM_CHECK] Error checking alarm for {self.device_id}: {e}")
                
                # 10秒間隔でチェック
                await asyncio.sleep(10.0)
                
        except Exception as e:
            logger.error(f"Error in alarm checker for {self.device_id}: {e}")
    
    async def _check_alarm_time(self):
        """現在時刻でアラームが発火すべきかチェック"""
        try:
            # JWTトークンが必要
            if not hasattr(self, 'user_id') or not self.user_id:
                logger.debug(f"⏰ [ALARM_CHECK] Skipping - no user_id for {self.device_id}")
                return
            
            logger.debug(f"⏰ [ALARM_CHECK] Checking alarms for user_id={self.user_id}, device={self.device_id}")
            
            # 現在時刻（JST）
            jst = pytz.timezone('Asia/Tokyo')
            now_jst = datetime.now(jst)
            current_date = now_jst.strftime('%Y-%m-%d')
            current_time = now_jst.strftime('%H:%M')
            
            logger.info(f"⏰ [ALARM_CHECK] Current time: {current_date} {current_time} (JST)")
            logger.info(f"⏰ [ALARM_CHECK] Full datetime: {now_jst.strftime('%Y-%m-%d %H:%M:%S %Z')}")
            
            # アラームAPIでチェック
            import httpx
            async with httpx.AsyncClient() as client:
                response = await client.get(
                    f"{Config.MANAGER_API_URL}/api/alarm/check",
                    params={
                        "user_id": self.user_id,
                        "timezone": "Asia/Tokyo"
                    },
                    headers={
                        "Authorization": f"Bearer {Config.MANAGER_API_SECRET}"
                    }
                )
                
                if response.status_code == 200:
                    result = response.json()
                    alarms = result.get('alarms', [])
                    
                    logger.info(f"⏰ [ALARM_CHECK] Found {len(alarms)} total alarms for user")
                    
                    for alarm in alarms:
                        alarm_date = alarm.get('alarm_date')
                        alarm_time = alarm.get('alarm_time') 
                        message = alarm.get('message', '').strip()
                        alarm_id = alarm.get('id')
                        is_fired = alarm.get('is_fired', False)
                        
                        logger.info(f"⏰ [ALARM_DETAIL] ID:{alarm_id} Date:{alarm_date} Time:{alarm_time} Fired:{is_fired} Msg:'{message}'")
                        
                        # 現在の日付・時刻と一致するかチェック
                        if alarm_date == current_date and alarm_time == current_time:
                            logger.info(f"🎯 [ALARM_MATCH] EXACT TIME MATCH! {alarm_date} {alarm_time}")
                        else:
                            logger.debug(f"⏰ [ALARM_NO_MATCH] {alarm_date} {alarm_time} != {current_date} {current_time}")
                        
                        # 現在の日付・時刻と一致するかチェック（かつ未発火）
                        if alarm_date == current_date and alarm_time == current_time and not is_fired:
                            logger.info(f"⏰ [ALARM_FIRED] Alarm triggered: {alarm_time} - {message}")
                            
                            # WebSocket接続確認 + 切断時は再接続不要（グローバル送信）
                            if self.websocket.closed:
                                logger.warning(f"🔌 [ALARM_DISCONNECT] WebSocket disconnected, attempting global alarm send")
                                await self._send_alarm_global(self.device_id, alarm_time, message, alarm_id)
                            else:
                                # 正常接続時は通常送信
                                await self._send_alarm_notification_fired(alarm_time, message, alarm_id)
                
        except Exception as e:
            logger.error(f"⏰ [ALARM_CHECK] Error: {e}")
    
    async def _send_alarm_notification_fired(self, alarm_time: str, message: str, alarm_id: str):
        """アラーム発火時の通知をESP32に送信"""
        try:
            # カスタムメッセージがあれば使用、なければデフォルト
            if message and message != "ネコ太からのお知らせにゃん！":
                notification_text = f"{message}ですにゃ"
            else:
                # 時刻を日本語で読み上げ
                hour, minute = alarm_time.split(':')
                notification_text = f"{hour}時{minute}分ですにゃ"
            
            # ESP32にアラーム通知送信
            alarm_notification = {
                "type": "alarm_notification",
                "message": notification_text,
                "alarm_time": alarm_time,
                "alarm_id": alarm_id,
                "timestamp": datetime.now().isoformat()
            }
            
            await self.websocket.send_text(json.dumps(alarm_notification))
            logger.info(f"🔔 [ALARM_NOTIFICATION] Sent to ESP32: '{notification_text}'")
            
            # アラームを発火済みにマーク
            await self._mark_alarm_as_fired(alarm_id)
            
        except Exception as e:
            logger.error(f"🔔 [ALARM_NOTIFICATION] Failed to send: {e}")
    
    async def _mark_alarm_as_fired(self, alarm_id: str):
        """アラームを発火済みにマーク"""
        try:
            import httpx
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    f"{Config.MANAGER_API_URL}/api/alarm/mark_fired",
                    json={"alarm_id": alarm_id},
                    headers={
                        "Authorization": f"Bearer {Config.MANAGER_API_SECRET}"
                    }
                )
                
                if response.status_code == 200:
                    logger.info(f"⏰ [ALARM_FIRED] Marked alarm as fired: {alarm_id}")
                else:
                    logger.error(f"⏰ [ALARM_FIRED] Failed to mark fired: {response.status_code}")
                    
        except Exception as e:
            logger.error(f"⏰ [ALARM_FIRED] Error marking as fired: {e}")
    
    async def _send_alarm_global(self, target_device_id: str, alarm_time: str, message: str, alarm_id: str):
        """WebSocket切断時のグローバルアラーム送信（他の接続やHTTP経由）"""
        try:
            logger.info(f"🌐 [ALARM_GLOBAL] Attempting global alarm send to device {target_device_id}")
            
            # アラーム発火を記録（重複防止）
            await self._mark_alarm_as_fired(alarm_id)
            
            # アラーム通知テキスト生成
            if message and message != "ネコ太からのお知らせにゃん！":
                notification_text = f"{message}ですにゃ"
            else:
                hour, minute = alarm_time.split(':')
                notification_text = f"{hour}時{minute}分ですにゃ"
            
            logger.info(f"🔔 [ALARM_GLOBAL] Alarm notification: '{notification_text}' for device {target_device_id}")
            
            # 方法1: ESP32への再接続トリガー信号（Light Sleepから復帰）
            logger.info(f"🔔 [ALARM_WAKE] Device {target_device_id} should wake up and reconnect for alarm")
            
            # 方法2: デバイスが再接続してきたときのためにアラーム状態を保持
            # (実装はconnection_managerに依存)
            logger.info(f"🔄 [ALARM_PENDING] Alarm ready for when device {target_device_id} reconnects")
            
        except Exception as e:
            logger.error(f"🌐 [ALARM_GLOBAL] Error in global alarm send: {e}")
    
    async def _check_pending_alarms(self):
        """接続開始時に待機中のアラームをチェック（再接続後の即座配信）"""
        try:
            if not hasattr(self, 'user_id') or not self.user_id:
                logger.debug(f"🔄 [PENDING_ALARM] Skipping - no user_id for {self.device_id}")
                return
            
            logger.info(f"🔄 [PENDING_ALARM] Checking for pending alarms on reconnect for {self.device_id}")
            
            # 現在時刻前後5分以内の未発火アラームをチェック
            jst = pytz.timezone('Asia/Tokyo')
            now_jst = datetime.now(jst)
            
            # 5分前から現在時刻までのアラームを取得
            start_time = (now_jst - timedelta(minutes=5)).strftime('%H:%M')
            current_time = now_jst.strftime('%H:%M')
            current_date = now_jst.strftime('%Y-%m-%d')
            
            import httpx
            async with httpx.AsyncClient() as client:
                response = await client.get(
                    f"{Config.MANAGER_API_URL}/api/alarm/check",
                    params={
                        "user_id": self.user_id,
                        "timezone": "Asia/Tokyo"
                    },
                    headers={
                        "Authorization": f"Bearer {Config.MANAGER_API_SECRET}"
                    }
                )
                
                if response.status_code == 200:
                    result = response.json()
                    alarms = result.get('alarms', [])
                    
                    for alarm in alarms:
                        alarm_date = alarm.get('alarm_date')
                        alarm_time = alarm.get('alarm_time')
                        message = alarm.get('message', '').strip()
                        alarm_id = alarm.get('id')
                        
                        # 今日の過去5分以内のアラームをチェック
                        if alarm_date == current_date and start_time <= alarm_time <= current_time:
                            logger.info(f"🔄 [PENDING_ALARM] Found recent alarm on reconnect: {alarm_time} - {message}")
                            await self._send_alarm_notification_fired(alarm_time, message, alarm_id)
                            
        except Exception as e:
            logger.error(f"🔄 [PENDING_ALARM] Error checking pending alarms: {e}")
