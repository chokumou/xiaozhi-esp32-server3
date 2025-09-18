import asyncio
import json
import struct
import uuid
import io
import threading
import time
import aiohttp
from typing import Dict, Any, Optional
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

# 接続中のデバイス管理（グローバル）
connected_devices: Dict[str, 'ConnectionHandler'] = {}

class ConnectionHandler:
    def __init__(self, websocket: web.WebSocketResponse, headers: Dict[str, str]):
        logger.info(f"🐛 ConnectionHandler.__init__ 開始")
        self.websocket = websocket
        self.headers = headers
        self.device_id = headers.get("device-id") or "unknown"
        logger.info(f"🐛 device_id設定: {self.device_id}")
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
        
        # 接続時にデバイスを登録
        connected_devices[self.device_id] = self
        logger.info(f"📱 RID[{self.device_id}] デバイス接続登録完了")
        logger.info(f"🐛 現在の接続デバイス一覧: {list(connected_devices.keys())}")
        logger.info(f"🐛 接続デバイス数: {len(connected_devices)}")
        self.features = {}
        self.close_after_chat = False  # Server2準拠: チャット後の接続制御
        
        # Audio buffering (server2 style)
        self.asr_audio = []  # List of Opus frames (server2 style)
        self.client_have_voice = False
        self.client_voice_stop = False
        self.last_activity_time = time.time()
        
        # Server2準拠: タイムアウト監視（環境変数で調整可能）
        self.timeout_seconds = Config.WEBSOCKET_TIMEOUT_SECONDS
        
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
            elif msg_type == "timer_expired":
                # タイマー完了通知の処理
                timer_message = msg_json.get("message", "")
                logger.info(f"⏰ タイマー完了通知を受信: '{timer_message}'")
                
                # タイマー完了をユーザーに通知
                response_text = f"時間だよ！{timer_message}にゃん"
                import uuid
                rid = str(uuid.uuid4())[:8]
                await self.send_audio_response(response_text, rid)
                logger.info(f"⏰ タイマー完了通知を送信: {response_text}")
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
            
            # タイマー機能の自然言語処理
            timer_processed = await self.process_timer_command(text, rid)
            if timer_processed:
                # タイマー処理が成功した場合は、LLM処理をスキップ
                self._processing_text = False
                return
            
            # レター機能の自然言語処理
            logger.info(f"📮 RID[{rid}] レター処理チェック開始: '{text}'")
            letter_processed = await self.process_letter_command(text, rid)
            logger.info(f"📮 RID[{rid}] レター処理結果: {letter_processed}")
            if letter_processed:
                # レター処理が成功した場合は、LLM処理をスキップ
                logger.info(f"📮 RID[{rid}] レター処理完了、LLM処理をスキップ")
                self._processing_text = False
                return
            
            self.chat_history.append({"role": "user", "content": text})

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
                        
                        logger.info(f"🎯 [SERVER2_EXACT] Sending {frame_count} frames individually, 60ms intervals (exactly like Server2)")
                        
                        try:
                            for frame_index, opus_frame in enumerate(opus_frames_list):
                                # 各フレームを個別に送信（Server2方式）
                                await self.websocket.send_bytes(opus_frame)
                                
                                # 最後のフレーム以外は60ms待機
                                if frame_index < len(opus_frames_list) - 1:
                                    await asyncio.sleep(frame_duration_ms / 1000.0)  # 60ms = 0.06s
                            
                            send_end_time = time.monotonic()
                            total_send_time = (send_end_time - send_start_time) * 1000  # ms
                            total_bytes = sum(len(frame) for frame in opus_frames_list)
                            
                            logger.info(f"✅ [SERVER2_EXACT_SUCCESS] Sent {frame_count} frames individually: {total_send_time:.1f}ms total")
                            logger.info(f"📊 [SERVER2_EXACT_STATS] Avg interval: {total_send_time/frame_count:.1f}ms, throughput: {total_bytes / total_send_time * 1000:.0f} bytes/sec")
                            
                        except Exception as send_error:
                            logger.error(f"❌ [SERVER2_EXACT_ERROR] Failed to send individual frames: {send_error}")
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
            # 切断時にデバイスを削除
            if self.device_id in connected_devices:
                del connected_devices[self.device_id]
                logger.info(f"📱 RID[{self.device_id}] デバイス接続削除完了")
                logger.info(f"🐛 残りの接続デバイス一覧: {list(connected_devices.keys())}")
                logger.info(f"🐛 残りの接続デバイス数: {len(connected_devices)}")
            else:
                logger.warning(f"📱 RID[{self.device_id}] デバイスが接続リストに存在しません")
            
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

    async def process_timer_command(self, text: str, rid: str) -> bool:
        """
        自然言語からタイマー設定を解析し、ESP32に送信する
        戻り値: タイマー処理が成功した場合True、そうでなければFalse
        """
        try:
            import re
            from datetime import datetime, timedelta
            
            # タイマー設定のパターンマッチング（アラーム関連キーワードも含める）
            timer_patterns = [
                # "X秒後" パターン（アラーム関連キーワード付き）
                (r'(\d+)秒後.*(?:アラーム|タイマー|お知らせ)', lambda m: int(m.group(1))),
                # "X分後" パターン（アラーム関連キーワード付き）
                (r'(\d+)分後.*(?:アラーム|タイマー|お知らせ)', lambda m: int(m.group(1)) * 60),
                # "X時間後" パターン（アラーム関連キーワード付き）
                (r'(\d+)時間後.*(?:アラーム|タイマー|お知らせ)', lambda m: int(m.group(1)) * 3600),
                # "X時Y分" パターン（今日の時刻、アラーム関連キーワード付き）
                (r'(\d+)時(\d+)分.*(?:アラーム|タイマー|お知らせ)', lambda m: self.calculate_time_until_today(int(m.group(1)), int(m.group(2)))),
                # "X時" パターン（今日の時刻、分は0、アラーム関連キーワード付き）
                (r'(\d+)時.*(?:アラーム|タイマー|お知らせ)', lambda m: self.calculate_time_until_today(int(m.group(1)), 0)),
                # 従来のパターン（後方互換性のため）
                (r'(\d+)秒後', lambda m: int(m.group(1))),
                (r'(\d+)分後', lambda m: int(m.group(1)) * 60),
                (r'(\d+)時間後', lambda m: int(m.group(1)) * 3600),
                (r'(\d+)時(\d+)分', lambda m: self.calculate_time_until_today(int(m.group(1)), int(m.group(2)))),
                (r'(\d+)時', lambda m: self.calculate_time_until_today(int(m.group(1)), 0)),
            ]
            
            # タイマー停止のパターン
            stop_patterns = [
                r'タイマー.*停止',
                r'タイマー.*キャンセル', 
                r'タイマー.*やめる',
                r'アラーム.*停止',
                r'アラーム.*キャンセル',
            ]
            
            # 停止コマンドのチェック
            for pattern in stop_patterns:
                if re.search(pattern, text):
                    logger.info(f"⏹️ RID[{rid}] タイマー停止コマンドを検出: {text}")
                    await self.send_timer_stop_command(rid)
                    return True
            
            # タイマー設定コマンドのチェック（2つのキーワード分離方式）
            logger.info(f"🐛 RID[{rid}] タイマーパターンマッチング開始: '{text}'")
            
            # 1. アラーム/タイマー関連キーワードがあるかチェック
            has_alarm_keyword = re.search(r'(?:アラーム|タイマー|お知らせ)', text)
            logger.debug(f"🐛 RID[{rid}] アラーム関連キーワード: {has_alarm_keyword is not None}")
            
            # 2. 時間表現があるかチェック
            time_patterns = [
                (r'(\d+)秒後', lambda m: int(m.group(1))),
                (r'(\d+)分後', lambda m: int(m.group(1)) * 60),
                (r'(\d+)時間後', lambda m: int(m.group(1)) * 3600),
                (r'(\d+)時(\d+)分', lambda m: self.calculate_time_until_today(int(m.group(1)), int(m.group(2)))),
                (r'(\d+)時', lambda m: self.calculate_time_until_today(int(m.group(1)), 0)),
            ]
            
            time_match = None
            matched_pattern = None
            for pattern, time_calculator in time_patterns:
                match = re.search(pattern, text)
                logger.debug(f"🐛 RID[{rid}] 時間パターン '{pattern}' チェック: {match is not None}")
                if match:
                    time_match = match
                    matched_pattern = pattern
                    matched_calculator = time_calculator
                    break
            
            # 3. 両方のキーワードがある場合のみタイマー設定
            if has_alarm_keyword and time_match:
                try:
                    logger.info(f"🎯 RID[{rid}] タイマー条件マッチ: アラーム関連=True, 時間表現='{matched_pattern}'")
                    
                    # 時刻指定の場合はタイムゾーンを考慮
                    if "時" in matched_pattern:
                        seconds = matched_calculator(time_match)
                    else:
                        seconds = matched_calculator(time_match)
                    
                    if seconds > 0:
                        # メッセージを元のテキストに設定（抽出処理を削除）
                        message = text
                        logger.debug(f"🐛 RID[{rid}] メッセージ設定: '{message}'")
                        
                        logger.info(f"⏰ RID[{rid}] タイマー設定コマンドを検出: {text} -> {seconds}秒, メッセージ: '{message}'")
                        await self.send_timer_set_command(rid, seconds, message)
                        return True
                except Exception as e:
                    logger.error(f"RID[{rid}] タイマー時間計算エラー: {e}")
            else:
                logger.debug(f"🐛 RID[{rid}] タイマー条件不一致: アラーム関連={has_alarm_keyword is not None}, 時間表現={time_match is not None}")
            
            return False
            
        except Exception as e:
            logger.error(f"RID[{rid}] タイマーコマンド処理エラー: {e}")
            return False

    def calculate_time_until_today(self, hour: int, minute: int) -> int:
        """
        今日の指定時刻までの秒数を計算
        """
        try:
            now = datetime.now()
            target_time = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            
            # 今日の時刻が既に過ぎている場合は明日の時刻にする
            if target_time <= now:
                target_time += timedelta(days=1)
            
            delta = target_time - now
            return int(delta.total_seconds())
        except Exception as e:
            logger.error(f"時刻計算エラー: {e}")
            return 0

    async def send_timer_set_command(self, rid: str, seconds: int, message: str):
        """
        ESP32にタイマー設定コマンドを送信 + nekota-serverのDBに保存
        """
        try:
            logger.info(f"🐛 RID[{rid}] send_timer_set_command開始: seconds={seconds}, message='{message}'")
            # ESP32に送信するメッセージ
            timer_command = {
                "type": "set_timer",
                "seconds": seconds,
                "message": message
            }
            
            # WebSocketでESP32に送信
            logger.info(f"🐛 RID[{rid}] WebSocket送信前: websocket.closed={self.websocket.closed}")
            await self.websocket.send_str(json.dumps(timer_command))
            logger.info(f"⏰ RID[{rid}] ESP32にタイマー設定コマンドを送信: {json.dumps(timer_command)}")
            logger.info(f"🐛 RID[{rid}] WebSocket送信後: websocket.closed={self.websocket.closed}")
            
            # nekota-serverのDBにアラームを保存
            await self.save_alarm_to_nekota_server(rid, seconds, message)
            
            # ユーザーに確認メッセージを送信（現地時間で表示）
            from datetime import datetime, timedelta, timezone, timedelta as td
            
            # 現地時間（日本時間）で計算
            jst = timezone(td(hours=9))  # JST = UTC+9
            now_jst = datetime.now(jst)
            target_time_jst = now_jst + timedelta(seconds=seconds)
            time_str = target_time_jst.strftime("%H時%M分")
            response_text = f"わかったよ！{time_str}にお知らせするにゃん"
            await self.send_audio_response(response_text, rid)
            logger.info(f"⏰ RID[{rid}] タイマー設定確認メッセージを送信: {response_text}")
            
        except Exception as e:
            logger.error(f"RID[{rid}] タイマー設定コマンド送信エラー: {e}")

    async def send_timer_stop_command(self, rid: str):
        """
        ESP32にタイマー停止コマンドを送信
        """
        try:
            # ESP32に送信するメッセージ
            stop_command = {
                "type": "stop_timer"
            }
            
            # WebSocketでESP32に送信
            await self.websocket.send_str(json.dumps(stop_command))
            logger.info(f"⏹️ RID[{rid}] ESP32にタイマー停止コマンドを送信: {json.dumps(stop_command)}")
            
            # ユーザーに確認メッセージを送信
            response_text = "わかったよ！タイマーをやめたにゃん"
            await self.send_audio_response(response_text, rid)
            logger.info(f"⏹️ RID[{rid}] タイマー停止確認メッセージを送信: {response_text}")
            
        except Exception as e:
            logger.error(f"RID[{rid}] タイマー停止コマンド送信エラー: {e}")

    async def save_alarm_to_nekota_server(self, rid: str, seconds: int, message: str):
        """
        nekota-serverのDBにアラームを保存（MemoryServiceのパターンを使用）
        """
        try:
            from datetime import datetime, timedelta
            
            logger.info(f"🐛 RID[{rid}] アラーム保存開始: seconds={seconds}, message='{message}'")
            
            # タイマー完了時刻を計算
            target_time = datetime.now() + timedelta(seconds=seconds)
            
            # 日本時間で計算（標準ライブラリのみ使用）
            from datetime import timezone, timedelta as td
            jst = timezone(td(hours=9))  # JST = UTC+9
            target_time_jst = target_time.replace(tzinfo=timezone.utc).astimezone(jst)
            
            logger.info(f"🐛 RID[{rid}] 計算された時刻: {target_time_jst.strftime('%Y-%m-%d %H:%M')}")
            
            # MemoryServiceと同じ方法で端末認証（既存の仕組みを使用）
            device_number = "327546"  # 登録済みデバイス番号（MemoryServiceと同じ）
            logger.info(f"🐛 RID[{rid}] 端末番号を使用: {device_number}")
            
            # MemoryServiceの認証方法を使用
            jwt_token, user_id = await self.memory_service._get_valid_jwt_and_user(device_number)
            
            if not jwt_token or not user_id:
                logger.error(f"🐛 RID[{rid}] 認証失敗: device_number={device_number}")
                return
            
            logger.info(f"🐛 RID[{rid}] 認証成功: user_id={user_id}, token={jwt_token[:20]}...")
            logger.info(f"🐛 RID[{rid}] device_id={rid}, user_id={user_id} の関係を確認")
            
            # アラームデータを準備
            alarm_data = {
                "user_id": user_id,
                "date": target_time_jst.strftime("%Y-%m-%d"),
                "time": target_time_jst.strftime("%H:%M"),
                "timezone": "Asia/Tokyo",
                "text": message
            }
            
            logger.info(f"🐛 RID[{rid}] アラームデータ: {alarm_data}")
            
            # MemoryServiceと同じhttpxクライアントを使用
            headers = {"Authorization": f"Bearer {jwt_token}"}
            
            response = await self.memory_service.client.post(
                "/api/alarm",
                json=alarm_data,
                headers=headers
            )
            
            logger.info(f"🐛 RID[{rid}] アラーム保存レスポンス: {response.status_code}")
            
            if response.status_code == 201:
                result = response.json()
                logger.info(f"💾 RID[{rid}] アラームをnekota-serverのDBに保存成功: {result}")
            else:
                error_text = response.text
                logger.error(f"💾 RID[{rid}] アラーム保存失敗: {response.status_code} - {error_text}")
                        
        except Exception as e:
            logger.warning(f"💾 RID[{rid}] nekota-serverアラーム保存エラー（動作は継続）: {e}")
            # DB保存に失敗してもタイマー機能は正常動作

    async def process_letter_command(self, text: str, rid: str) -> bool:
        """レター送信コマンドの処理"""
        try:
            logger.info(f"📮 RID[{rid}] レター処理開始: '{text}'")
            
            # 柔軟なレター送信キーワードチェック
            letter_keywords = ["メッセージ", "レター", "手紙", "送って", "送る", "伝えて", "連絡"]
            has_letter_keyword = any(keyword in text for keyword in letter_keywords)
            
            if has_letter_keyword:
                logger.info(f"📮 RID[{rid}] レター送信開始（キーワード検出）")
                
                # 「○○に送って」形式の場合、名前を抽出して直接処理
                if ("に送って" in text or "に送る" in text or "へ送って" in text or "へ送る" in text):
                    extracted_name = self._extract_name_from_text(text)
                    logger.info(f"📮 RID[{rid}] 文章から名前抽出: '{extracted_name}' (元: '{text}')")
                    
                    if extracted_name and extracted_name != text:  # 名前が抽出できた場合
                        # メッセージ内容を質問
                        response_text = f"{extracted_name}になんのメッセージを送るにゃ？"
                        await self.send_audio_response(response_text, rid)
                        
                        # 事前に友達名を保存
                        self.letter_target_friend = extracted_name
                        self.letter_state = "waiting_message_with_friend"
                        self.letter_rid = rid
                        return True
                
                # 通常のレター送信フロー
                response_text = "なんのメッセージを送るにゃ？"
                await self.send_audio_response(response_text, rid)
                
                # 次の音声入力を待機（メッセージ内容）
                self.letter_state = "waiting_message"
                self.letter_rid = rid
                return True
                
            # メッセージ内容受信状態
            elif hasattr(self, 'letter_state') and self.letter_state == "waiting_message":
                logger.info(f"📮 RID[{rid}] メッセージ内容受信: '{text}'")
                
                self.letter_message = text
                
                # 友達選択を質問
                response_text = "誰に送るにゃ？"
                await self.send_audio_response(response_text, rid)
                
                # 次の音声入力を待機（友達選択）
                self.letter_state = "waiting_friend"
                return True
                
            # 友達名付きメッセージ内容受信状態
            elif hasattr(self, 'letter_state') and self.letter_state == "waiting_message_with_friend":
                logger.info(f"📮 RID[{rid}] 友達名付きメッセージ内容受信: '{text}'")
                
                # メッセージが既に設定されている場合は上書きしない
                if not hasattr(self, 'letter_message') or not self.letter_message:
                    self.letter_message = text
                    logger.info(f"📮 RID[{rid}] メッセージ設定: '{text}'")
                else:
                    logger.info(f"📮 RID[{rid}] メッセージ既存のため維持: '{self.letter_message}' (新規入力: '{text}')")
                
                friend_name = self.letter_target_friend
                
                # 友達を検索して送信
                result = await self.find_and_send_letter(friend_name, self.letter_message, rid)
                
                if result["success"]:
                    response_text = f"わかったよ！{result['friend_name']}にお手紙を送ったにゃん"
                    # 状態をリセット
                    self.letter_state = None
                    self.letter_message = None
                    self.letter_rid = None
                    self.letter_target_friend = None
                elif result["suggestion"]:
                    response_text = f"もしかして{result['suggestion']}？"
                    self.letter_suggested_friend = result['suggestion']
                    self.letter_state = "confirming_friend"
                else:
                    # システムエラーまたは友達が見つからない場合は完全リセット
                    response_text = f"ごめん、{friend_name}への送信に失敗したにゃん。もう一度最初からお願いします"
                    # 状態を完全にリセット
                    self.letter_state = None
                    self.letter_message = None
                    self.letter_rid = None
                    self.letter_target_friend = None
                    self.letter_suggested_friend = None
                
                await self.send_audio_response(response_text, rid)
                return True
                
            # 友達選択受信状態
            elif hasattr(self, 'letter_state') and self.letter_state == "waiting_friend":
                logger.info(f"📮 RID[{rid}] 友達選択受信: '{text}'")
                logger.info(f"📮 RID[{rid}] 現在の状態: letter_state={getattr(self, 'letter_state', None)}, letter_message={getattr(self, 'letter_message', None)}")
                
                # 文章から名前を抽出
                friend_name = self._extract_name_from_text(text)
                logger.info(f"📮 RID[{rid}] 抽出された名前: '{friend_name}' (元テキスト: '{text}')")
                
                result = await self.find_and_send_letter(friend_name, self.letter_message, rid)
                
                if result["success"]:
                    response_text = f"わかったよ！{result['friend_name']}にお手紙を送ったにゃん"
                    # 状態をリセット
                    self.letter_state = None
                    self.letter_message = None
                    self.letter_rid = None
                elif result["suggestion"]:
                    response_text = f"もしかして{result['suggestion']}？"
                    self.letter_suggested_friend = result['suggestion']
                    self.letter_state = "confirming_friend"
                else:
                    # システムエラーまたは友達が見つからない場合は完全リセット
                    response_text = f"ごめん、{friend_name}への送信に失敗したにゃん。もう一度最初からお願いします"
                    # 状態を完全にリセット
                    self.letter_state = None
                    self.letter_message = None
                    self.letter_rid = None
                    self.letter_target_friend = None
                    self.letter_suggested_friend = None
                
                await self.send_audio_response(response_text, rid)
                return True
                
            # 友達確認状態
            elif hasattr(self, 'letter_state') and self.letter_state == "confirming_friend":
                logger.info(f"📮 RID[{rid}] 友達確認受信: '{text}'")
                
                if "はい" in text or "そう" in text or "うん" in text or "はい" in text:
                    # 提案された友達に送信
                    success = await self.send_letter_to_friend_direct(self.letter_suggested_friend, self.letter_message, rid)
                    if success:
                        response_text = f"わかったよ！{self.letter_suggested_friend}にお手紙を送ったにゃん"
                    else:
                        response_text = "ごめん、送信に失敗したにゃん"
                    
                    # 状態をリセット
                    self.letter_state = None
                    self.letter_message = None
                    self.letter_rid = None
                    self.letter_suggested_friend = None
                else:
                    # 他の友達を再質問
                    response_text = "じゃあ、誰に送るにゃ？"
                    self.letter_state = "waiting_friend"
                
                await self.send_audio_response(response_text, rid)
                return True
                
            # 友達名リトライ状態
            elif hasattr(self, 'letter_state') and self.letter_state == "waiting_friend_retry":
                logger.info(f"📮 RID[{rid}] 友達名リトライ受信: '{text}'")
                
                friend_name = text.strip()
                result = await self.find_and_send_letter(friend_name, self.letter_message, rid)
                
                if result["success"]:
                    response_text = f"わかったよ！{result['friend_name']}にお手紙を送ったにゃん"
                    # 状態をリセット
                    self.letter_state = None
                    self.letter_message = None
                    self.letter_rid = None
                elif result["suggestion"]:
                    response_text = f"もしかして{result['suggestion']}？"
                    self.letter_suggested_friend = result['suggestion']
                    self.letter_state = "confirming_friend"
                else:
                    # システムエラーまたは友達が見つからない場合は完全リセット
                    response_text = "やっぱり見つからないにゃん。もう一度最初からお願いします"
                    # 状態を完全にリセット
                    self.letter_state = None
                    self.letter_message = None
                    self.letter_rid = None
                    self.letter_target_friend = None
                    self.letter_suggested_friend = None
                
                await self.send_audio_response(response_text, rid)
                return True
                
            return False
            
        except Exception as e:
            logger.error(f"📮 RID[{rid}] レター処理エラー: {e}")
            return False

    async def find_and_send_letter(self, friend_name: str, message: str, rid: str) -> dict:
        """友達をあいまい検索してレターを送信"""
        try:
            logger.info(f"📮 RID[{rid}] あいまい検索開始: '{friend_name}' へ '{message}'")
            
            # nekota-serverから友達リストを取得
            device_number = "327546"  # 固定デバイス番号
            jwt_token, user_id = await self.memory_service._get_valid_jwt_and_user(device_number)
            if not jwt_token or not user_id:
                logger.error(f"📮 RID[{rid}] 認証失敗")
                return {"success": False, "suggestion": None}
            
            import aiohttp
            nekota_server_url = "https://nekota-server-production.up.railway.app"
            
            async with aiohttp.ClientSession() as session:
                # 友達リスト取得
                headers = {"Authorization": f"Bearer {jwt_token}"}
                friend_response = await session.get(
                    f"{nekota_server_url}/api/friend/list?user_id={user_id}",
                    headers=headers
                )
                
                if friend_response.status == 200:
                    friend_data = await friend_response.json()
                    friends = friend_data.get("friends", [])
                    
                    logger.info(f"📮 RID[{rid}] 友達リスト取得成功: {len(friends)}人")
                    for i, friend in enumerate(friends):
                        logger.info(f"📮 RID[{rid}] 友達{i+1}: {friend.get('name', 'Unknown')}")
                    
                    # 完全一致検索
                    target_friend = None
                    for friend in friends:
                        if friend.get("name", "").lower() == friend_name.lower():
                            target_friend = friend
                            break
                    
                    # 完全一致した場合は送信
                    if target_friend:
                        success = await self._send_letter_api(target_friend, message, user_id, headers, session, rid)
                        if success:
                            return {"success": True, "friend_name": target_friend["name"], "suggestion": None}
                    
                    # あいまい検索（部分一致）
                    logger.info(f"📮 RID[{rid}] あいまい検索開始: 友達数={len(friends)}")
                    suggestions = []
                    for friend in friends:
                        friend_name_lower = friend.get("name", "").lower()
                        input_name_lower = friend_name.lower()
                        
                        logger.info(f"📮 RID[{rid}] 検索比較: '{input_name_lower}' vs '{friend_name_lower}'")
                        
                        # 部分一致または含む関係
                        is_partial_match = (input_name_lower in friend_name_lower or 
                                          friend_name_lower in input_name_lower)
                        similarity = self._calculate_similarity(input_name_lower, friend_name_lower)
                        
                        logger.info(f"📮 RID[{rid}] マッチ結果: partial={is_partial_match}, similarity={similarity}")
                        
                        if is_partial_match or similarity > 0.3:  # 類似度閾値を下げる
                            suggestions.append({
                                "friend": friend,
                                "similarity": similarity,
                                "partial_match": is_partial_match
                            })
                            logger.info(f"📮 RID[{rid}] 候補追加: {friend['name']}")
                    
                    # 類似度でソート（部分一致を優先）
                    suggestions.sort(key=lambda x: (x["partial_match"], x["similarity"]), reverse=True)
                    
                    # 最も類似度の高い友達を提案
                    if suggestions:
                        best_match = suggestions[0]["friend"]
                        logger.info(f"📮 RID[{rid}] 最適候補: {best_match['name']}")
                        return {"success": False, "suggestion": best_match["name"]}
                    
                    logger.info(f"📮 RID[{rid}] 候補なし")
                    
                    return {"success": False, "suggestion": None}
                else:
                    logger.error(f"📮 RID[{rid}] 友達リスト取得失敗: {friend_response.status}")
                    return {"success": False, "suggestion": None}
                    
        except Exception as e:
            logger.error(f"📮 RID[{rid}] あいまい検索エラー: {e}")
            return {"success": False, "suggestion": None}

    async def send_letter_to_friend_direct(self, friend_name: str, message: str, rid: str) -> bool:
        """友達名で直接レター送信（確認済み）"""
        try:
            device_number = "327546"  # 固定デバイス番号
            jwt_token, user_id = await self.memory_service._get_valid_jwt_and_user(device_number)
            if not jwt_token or not user_id:
                return False
            
            import aiohttp
            nekota_server_url = "https://nekota-server-production.up.railway.app"
            
            async with aiohttp.ClientSession() as session:
                headers = {"Authorization": f"Bearer {jwt_token}"}
                friend_response = await session.get(
                    f"{nekota_server_url}/api/friend/list?user_id={user_id}",
                    headers=headers
                )
                
                if friend_response.status == 200:
                    friend_data = await friend_response.json()
                    friends = friend_data.get("friends", [])
                    
                    target_friend = None
                    for friend in friends:
                        if friend.get("name", "").lower() == friend_name.lower():
                            target_friend = friend
                            break
                    
                    if target_friend:
                        return await self._send_letter_api(target_friend, message, user_id, headers, session, rid)
                        
            return False
        except Exception as e:
            logger.error(f"📮 RID[{rid}] 直接送信エラー: {e}")
            return False

    async def _send_letter_api(self, target_friend: dict, message: str, user_id: str, headers: dict, session, rid: str) -> bool:
        """レター送信API呼び出し"""
        try:
            nekota_server_url = "https://nekota-server-production.up.railway.app"
            
            letter_data = {
                "from_user_id": user_id,
                "to_user_id": target_friend["user_id"],
                "message": message,
                "type": "letter"
            }
            
            logger.info(f"📮 RID[{rid}] レター送信開始: URL={nekota_server_url}/api/message/send_letter")
            logger.info(f"📮 RID[{rid}] 送信データ: {letter_data}")
            
            message_response = await session.post(
                f"{nekota_server_url}/api/message/send_letter",
                json=letter_data,
                headers=headers
            )
            
            logger.info(f"📮 RID[{rid}] レスポンス受信: status={message_response.status}")
            
            if message_response.status == 201:
                logger.info(f"📮 RID[{rid}] レター送信成功: {target_friend['name']}")
                return True
            else:
                error_text = await message_response.text()
                logger.error(f"📮 RID[{rid}] レター送信失敗: {message_response.status} - {error_text}")
                logger.error(f"📮 RID[{rid}] 送信データ: {letter_data}")
                logger.error(f"📮 RID[{rid}] リクエストURL: {nekota_server_url}/api/message/send_letter")
                logger.error(f"📮 RID[{rid}] リクエストヘッダー: {headers}")
                return False
                
        except Exception as e:
            logger.error(f"📮 RID[{rid}] API送信エラー: {e}")
            return False

    def _normalize_japanese_text(self, text: str) -> list:
        """日本語テキストを正規化（ひらがな・カタカナ・漢字変換）"""
        import unicodedata
        
        normalized_variants = [text.lower()]
        
        # ひらがな→カタカナ変換
        hiragana_to_katakana = ""
        for char in text:
            if 'ひ' <= char <= 'ゖ':  # ひらがな範囲
                hiragana_to_katakana += chr(ord(char) + 0x60)
            else:
                hiragana_to_katakana += char
        if hiragana_to_katakana != text:
            normalized_variants.append(hiragana_to_katakana.lower())
        
        # カタカナ→ひらがな変換
        katakana_to_hiragana = ""
        for char in text:
            if 'ア' <= char <= 'ヶ':  # カタカナ範囲
                katakana_to_hiragana += chr(ord(char) - 0x60)
            else:
                katakana_to_hiragana += char
        if katakana_to_hiragana != text:
            normalized_variants.append(katakana_to_hiragana.lower())
        
        # 全角→半角変換
        half_width = unicodedata.normalize('NFKC', text).lower()
        if half_width != text.lower():
            normalized_variants.append(half_width)
        
        return list(set(normalized_variants))  # 重複除去

    def _extract_name_from_text(self, text: str) -> str:
        """文章から名前を抽出"""
        import re
        
        # 不要な語句を除去するパターン
        noise_patterns = [
            r'に送って$',
            r'に送る$', 
            r'を探して$',
            r'に連絡$',
            r'にメッセージ$',
            r'にレター$',
            r'に手紙$',
            r'へ送って$',
            r'へ送る$',
            r'に伝えて$',
            r'に教えて$'
        ]
        
        extracted_name = text.strip()
        
        # 各パターンで不要部分を除去
        for pattern in noise_patterns:
            extracted_name = re.sub(pattern, '', extracted_name, flags=re.IGNORECASE)
        
        # 前後の空白を除去
        extracted_name = extracted_name.strip()
        
        # 空文字列の場合は元のテキストを返す
        if not extracted_name:
            extracted_name = text.strip()
        
        return extracted_name

    def _calculate_similarity(self, str1: str, str2: str) -> float:
        """文字列の類似度を計算（日本語対応改良版）"""
        if not str1 or not str2:
            return 0.0
        
        # 正規化バリアントを生成
        str1_variants = self._normalize_japanese_text(str1)
        str2_variants = self._normalize_japanese_text(str2)
        
        max_similarity = 0.0
        
        # 全組み合わせで最高類似度を計算
        for v1 in str1_variants:
            for v2 in str2_variants:
                # 完全一致
                if v1 == v2:
                    return 1.0
                
                # 部分一致（含まれる関係）
                if v1 in v2 or v2 in v1:
                    max_similarity = max(max_similarity, 0.8)
                    continue
                
                # 共通文字数を計算
                len1, len2 = len(v1), len(v2)
                common = 0
                v2_chars = list(v2)
                
                for char in v1:
                    if char in v2_chars:
                        v2_chars.remove(char)  # 重複カウントを防ぐ
                        common += 1
                
                # ジャッカード係数的な計算
                union_size = len1 + len2 - common
                if union_size > 0:
                    similarity = common / union_size
                    max_similarity = max(max_similarity, similarity)
        
        return max_similarity

# デバイス接続チェック関数
def is_device_connected(device_id: str) -> bool:
    """
    指定されたデバイスが接続中かチェック
    """
    return device_id in connected_devices

async def send_timer_to_connected_device(device_id: str, seconds: int, message: str) -> bool:
    """
    接続中のデバイスにタイマー設定コマンドを送信
    """
    if device_id not in connected_devices:
        logger.warning(f"📱 デバイス {device_id} は接続されていません")
        return False
    
    try:
        handler = connected_devices[device_id]
        await handler.send_timer_set_command(device_id, seconds, message)
        logger.info(f"📱 デバイス {device_id} にタイマー設定コマンドを送信成功")
        return True
    except Exception as e:
        logger.error(f"📱 デバイス {device_id} へのタイマー送信エラー: {e}")
        return False