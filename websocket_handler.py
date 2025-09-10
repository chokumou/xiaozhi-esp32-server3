import asyncio
import json
import struct
import uuid
import io
import threading
import time
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
        
        # Server2準拠: タイムアウト監視（元の180秒に戻して詳細調査）
        self.timeout_seconds = 180  # 120 + 60秒のバッファ（ESP32エラー原因を特定するため）
        self.timeout_task = None
        
        # Initialize server2-style audio handler
        self.audio_handler = AudioHandlerServer2(self)
        
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
            else:
                logger.warning(f"Unknown message type from {self.device_id}: {msg_type}")

        except json.JSONDecodeError:
            logger.error(f"Invalid JSON from {self.device_id}: {message[:100]}...")
        except Exception as e:
            logger.error(f"Error handling text message from {self.device_id}: {e}")

    async def handle_binary_message(self, message: bytes):
        """Handle binary audio data based on protocol version"""
        try:
            # A. 入口で落とす（最重要）- AI発話中+クールダウン中完全ブロック
            now_ms = time.time() * 1000
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
                    logger.info(f"🚪 [WS_ENTRANCE_BLOCK] {block_reason}入口ブロック: 過去30フレーム完全破棄 (累計={self.ws_gate_drops})")
                return  # 即座に破棄
            
            # Server2準拠: 小パケットでも活動時間を更新（ESP32からの継続通信を認識）
            self.last_activity_time = time.time()
            
            # デバッグ: パケットサイズをログ（★入口ガード通過★ - AI非発話＆クールダウン外）
            if not hasattr(self, '_packet_log_count'):
                self._packet_log_count = 0
            self._packet_log_count += 1
            # 通常時も20フレームに1回に制限（ログ軽減）
            if self._packet_log_count % 20 == 0:
                logger.info(f"🔧 [PACKET_DEBUG] ★入口ガード通過★ 通常処理: 過去20フレーム (最新: {len(message)}B), protocol v{self.protocol_version}")
            
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
            now_ms = time.time() * 1000
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
        elif state == "stop":
            logger.info(f"Client {self.device_id} stopped listening")
            
        if mode:
            logger.debug(f"Client listen mode: {mode}")


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

            # Check for memory-related keywords
            memory_query = None
            if "覚えて" in text or "記憶して" in text:
                # Extract what to remember
                memory_to_save = text.replace("覚えて", "").replace("記憶して", "").strip()
                if memory_to_save:
                    success = await self.memory_service.save_memory(self.device_id, memory_to_save)
                    if success:
                        await self.send_text_response("はい、覚えました。")
                    else:
                        await self.send_text_response("すみません、記憶できませんでした。")
                    return
            elif "覚えてる" in text or "何が好き" in text or "誕生日はいつ" in text:
                memory_query = text

            # Prepare messages for LLM
            llm_messages = list(self.chat_history)
            if memory_query:
                retrieved_memory = await self.memory_service.query_memory(self.device_id, memory_query)
                if retrieved_memory:
                    llm_messages.insert(0, {"role": "system", "content": f"ユーザーの記憶: {retrieved_memory}"})
                    logger.info(f"Retrieved memory for LLM: {retrieved_memory[:50]}...")

            # Generate LLM response (server2 style - no extra keepalive)
            llm_response = await self.llm_service.chat_completion(llm_messages)
            
            if llm_response and llm_response.strip():
                logger.info(f"🔥 RID[{rid}] LLM_RESULT: '{llm_response}'")
                self.chat_history.append({"role": "assistant", "content": llm_response})
                
                # Send STT message to display user input (server2 style)
                await self.send_stt_message(text)
                
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
                
                # Server2準拠: 端末にマイクオフ指示（フルデュプレックス衝突防止）
                mic_control_message = {
                    "type": "audio_control", 
                    "action": "mic_off", 
                    "reason": "tts_speaking"
                }
                try:
                    await self.websocket.send(json.dumps(mic_control_message))
                    logger.info(f"📡 [DEVICE_CONTROL] 端末にマイクオフ指示送信: {mic_control_message}")
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
                await self.websocket.send(json.dumps(tts_start_message))
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
            
            # Send TTS start message (server2 style)
            try:
                tts_start_msg = {
                    "type": "tts", 
                    "state": "start", 
                    "session_id": self.session_id
                }
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
            opus_frames_list = await self.tts_service.generate_speech(text)
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
                    
                    # Server2準拠: audios = 全フレーム結合bytes
                    audios = b''.join(opus_frames_list)
                    total_frames = len(opus_frames_list)
                    
                    # ESP32準拠: BinaryProtocol3ヘッダー追加
                    import struct
                    type_field = 0  # ESP32期待値：type=0
                    reserved = 0    # ESP32必須：reserved=0
                    payload_size = len(audios)
                    header = struct.pack('>BBH', type_field, reserved, payload_size)  # type(1) + reserved(1) + size(2) big-endian
                    v3_data = header + audios
                    
                    logger.info(f"🎵 [V3_PROTOCOL] BinaryProtocol3: type={type_field}, size={payload_size}, total={len(v3_data)} bytes")
                    logger.info(f"🔍 [CONNECTION_CHECK] Just before send_bytes: closed={self.websocket.closed}")
                    
                    if hasattr(self, 'websocket') and self.websocket and not self.websocket.closed:
                        # ESP32のOpusDecoder対応: 個別フレーム送信 (server2準拠)
                        frame_count = len(opus_frames_list)
                        logger.info(f"🎵 [INDIVIDUAL_FRAMES] Sending {frame_count} individual Opus frames")
                        
                        # デバッグ：最初のフレーム詳細解析
                        if frame_count > 0:
                            first_frame = opus_frames_list[0]
                            logger.info(f"🔬 [OPUS_DEBUG] First frame: size={len(first_frame)}bytes, hex_header={first_frame[:8].hex() if len(first_frame)>=8 else first_frame.hex()}")
                        
                        for i, opus_frame in enumerate(opus_frames_list):
                            # 極小フレーム（音質劣化の原因）をスキップ
                            if len(opus_frame) < 10:
                                logger.warning(f"🚨 [FRAME_SKIP] Skipping tiny frame {i+1}: {len(opus_frame)}bytes")
                                continue
                                
                            # 各フレームに個別のBinaryProtocol3ヘッダーを追加
                            frame_header = struct.pack('>BBH', 0, 0, len(opus_frame))  # type=0, reserved=0, size
                            frame_data = frame_header + opus_frame
                            
                            # TTS送信中の中断検知
                            if i % 50 == 0:  # 50フレームごとにチェック
                                logger.info(f"🎵 [FRAME_PROGRESS] Frame {i+1}/{frame_count}: opus={len(opus_frame)}bytes, connection_ok={not self.websocket.closed}")
                                
                            # TTS中断要因チェック
                            if hasattr(self, '_processing_text') and not self._processing_text:
                                logger.warning(f"🚨 [TTS_INTERRUPT] _processing_text became False during TTS at frame {i+1}")
                            if hasattr(self.audio_handler, 'is_processing') and not self.audio_handler.is_processing:
                                logger.warning(f"🚨 [TTS_INTERRUPT] audio_handler.is_processing became False during TTS at frame {i+1}")
                                # TTS送信中は強制的に is_processing を維持
                                self.audio_handler.is_processing = True
                                logger.warning(f"🛡️ [TTS_PROTECTION] Forcing is_processing=True during TTS frame {i+1}")
                            
                            # ログ削減：10フレームごとまたは最初/最後のみ  
                            elif i == 0 or i == frame_count-1 or (i+1) % 10 == 0:
                                logger.info(f"🎵 [FRAME_SEND] Frame {i+1}/{frame_count}: opus={len(opus_frame)}bytes")
                            
                            await self.websocket.send_bytes(frame_data)
                            await asyncio.sleep(0.010)  # 10ms delay - 音質とTLS負荷のバランス
                            
                            # TLS接続状態詳細チェック
                            if self.websocket.closed:
                                logger.error(f"🚨 [TLS_ERROR] WebSocket closed during frame {i+1}, close_code={getattr(self.websocket, 'close_code', 'None')}")
                            elif getattr(self.websocket, '_writer', None) is None:
                                logger.error(f"🚨 [TLS_ERROR] Writer lost during frame {i+1}")
                                break
                            
                            if self.websocket.closed:
                                logger.error(f"🚨 [FRAME_SEND] Connection closed after frame {i+1}, stopping transmission")
                                break
                                
                        if not self.websocket.closed:
                            logger.info(f"✅ [INDIVIDUAL_FRAMES] All {frame_count} frames sent successfully")
                    else:
                        logger.error(f"❌ [V3_PROTOCOL] WebSocket disconnected before send")
                    
                    logger.info(f"🔵XIAOZHI_AUDIO_SENT🔵 ※ここを送ってver2_AUDIO※ 🎵 [AUDIO_SENT] ===== Sent {total_frames} Opus frames to {self.device_id} ({len(audios)} total bytes) =====")
                    logger.info(f"🔍 [DEBUG_SEND] WebSocket state after audio send: closed={self.websocket.closed}")

                    # Send TTS stop message with cooldown info (server2 style + 回り込み防止)
                    tts_stop_msg = {"type": "tts", "state": "stop", "session_id": self.session_id, "cooldown_ms": 1200}  # 残響も含めた完全エコー除去のため1200msに延長
                    logger.info(f"🔍 [DEBUG_SEND] About to send TTS stop message: {tts_stop_msg}")
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
                    cooldown_until = time.time() * 1000 + cooldown_ms
                    
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
                            # 1. TTS停止メッセージ（Server2準拠）
                            await self.websocket.send(json.dumps(tts_stop_message))
                            
                            # 2. マイクオン指示（拡張）
                            await self.websocket.send(json.dumps(mic_on_message))
                            
                            # 3. 録音再開指示（重要！ESP32が自動再開しない場合の保険）
                            listen_start_message = {
                                "type": "listen", 
                                "state": "start", 
                                "mode": "continuous"
                            }
                            await self.websocket.send(json.dumps(listen_start_message))
                            
                            logger.info(f"📡 [DEVICE_CONTROL] 端末制御送信完了: TTS停止→マイクON→録音再開")
                            logger.info(f"📡 [DEVICE_CONTROL] Messages: {tts_stop_message}, {mic_on_message}, {listen_start_message}")
                        except Exception as e:
                            logger.warning(f"📡 [DEVICE_CONTROL] 端末制御送信失敗: {e}")
                        
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
            