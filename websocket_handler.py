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
        import time
        self.last_activity_time = time.time() * 1000
        
        # Server2準拠: タイムアウト監視（元の180秒に戻して詳細調査）
        self.timeout_seconds = 180  # 120 + 60秒のバッファ（ESP32エラー原因を特定するため）
        self.timeout_task = None
        
        # Initialize server2-style audio handler
        self.audio_handler = AudioHandlerServer2(self)
        
        # Welcome message compatible with ESP32
        self.welcome_msg = {
            "type": "hello",
            "transport": "websocket", 
            "session_id": self.session_id,
            "audio_params": {
                "format": "opus",
                "sample_rate": 16000,
                "channels": 1,
                "frame_duration": 20
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
            # Server2準拠: 小パケットでも活動時間を更新（ESP32からの継続通信を認識）
            self.last_activity_time = time.time() * 1000
            
            # logger.info(f"🔧 [DEBUG] Processing binary message: {len(message)} bytes, protocol v{self.protocol_version}")  # レート制限対策で削除
            if len(message) <= 12:  # Skip very small packets (DTX/keepalive) but keep activity alive
                logger.info(f"⏭️ [DEBUG] Skipping small packet: {len(message)} bytes (activity updated)")
                return
                
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
                logger.info(f"📋 [PROTO] v3: type={msg_type}, payload_size={payload_size}, extracted_audio={len(audio_data)} bytes")
            else:
                # Protocol v1: raw audio data
                audio_data = message

                # logger.info(f"🚀 [DEBUG] Calling server2-style audio handler with {len(audio_data)} bytes")  # レート制限対策で削除
            await self.audio_handler.handle_audio_frame(audio_data)
            # logger.info(f"✅ [DEBUG] server2-style audio processing completed")  # レート制限対策で削除
            
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
        logger.info(f"Sent welcome message to {self.device_id}")
        
        # Server2準拠: タイムアウト監視タスク起動
        self.timeout_task = asyncio.create_task(self._check_timeout())
        logger.info(f"Started timeout monitoring task for {self.device_id}")

    async def handle_listen_message(self, msg_json: Dict[str, Any]):
        """Handle listen state changes"""
        state = msg_json.get("state")
        mode = msg_json.get("mode")
        
        if state == "start":
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


    async def process_text(self, text: str):
        """Process text input through LLM and generate response"""
        try:
            logger.info(f"🧠 [LLM_START] ===== Processing text input: '{text}' =====")
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
                logger.info(f"🤖 [LLM_RESULT] ===== LLM response for {self.device_id}: '{llm_response}' =====")
                self.chat_history.append({"role": "assistant", "content": llm_response})
                
                # Send STT message to display user input (server2 style)
                await self.send_stt_message(text)
                
                # Generate and send audio response
                await self.send_audio_response(llm_response)
            else:
                logger.warning(f"No LLM response for {self.device_id}")
                
        except Exception as e:
            logger.error(f"Error processing text from {self.device_id}: {e}")

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

    async def send_audio_response(self, text: str):
        """Generate and send audio response"""
        try:
            self.client_is_speaking = True
            # TTS中は音声検知一時停止
            if hasattr(self, 'audio_handler'):
                self.audio_handler.tts_in_progress = True
            
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
            
            # Generate TTS audio (server2 style - individual frames)
            opus_frames_list = await self.tts_service.generate_speech(text)
            logger.info(f"🎶 [TTS_RESULT] ===== TTS generated: {len(opus_frames_list) if opus_frames_list else 0} individual Opus frames =====")
            
            # Server2準拠: 全フレーム一括送信（sendAudioHandle.py line 45準拠）
            if opus_frames_list:
                try:
                    # Server2準拠: 個別フレームを結合して一括送信
                    all_opus_data = b''.join(opus_frames_list)
                    total_frames = len(opus_frames_list)
                    
                    logger.info(f"🎵 [AUDIO_SENDING] Server2-style batch transmission to {self.device_id}")
                    logger.info(f"🔗 [BATCH_STRATEGY] Sending {total_frames} Opus frames as single payload ({len(all_opus_data)} total bytes)")
                    logger.info(f"🎵 [BATCH_DETAIL] First 40 bytes: {all_opus_data[:40].hex() if len(all_opus_data) >= 40 else all_opus_data.hex()}")
                    
                    # Server2準拠: await conn.websocket.send(audios) 
                    await self.websocket.send_bytes(all_opus_data)
                    
                    logger.info(f"🔵XIAOZHI_AUDIO_SENT🔵 ※ここを送ってver2_AUDIO※ 🎵 [AUDIO_SENT] ===== Sent batch Opus data to {self.device_id} ({len(all_opus_data)} total bytes) =====")
                    logger.info(f"🔍 [DEBUG_SEND] WebSocket state after audio send: closed={self.websocket.closed}")

                    # Send TTS stop message (server2 style)
                    tts_stop_msg = {"type": "tts", "state": "stop", "session_id": self.session_id}
                    logger.info(f"🔍 [DEBUG_SEND] About to send TTS stop message: {tts_stop_msg}")
                    await self.websocket.send_str(json.dumps(tts_stop_msg))
                    logger.info(f"🟡XIAOZHI_TTS_STOP🟡 ※ここを送ってver2_TTS_STOP※ 📢 [TTS] Sent TTS stop message")
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
            self.client_is_speaking = False
            # TTS完了後は音声検知を停止状態のまま（次の有音で自動再開）
            # if hasattr(self, 'audio_handler'):
            #     self.audio_handler.tts_in_progress = False  # 削除：即座に再開しない

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
                    logger.info(f"🔍 [LOOP_MONITOR] Received message in async for loop")
                    
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
                    
                    logger.info(f"🔍 [DEBUG_LOOP] Message {msg_count}: type={msg.type}({msg.type.value}), closed={self.websocket.closed}, data_len={len(msg.data) if hasattr(msg, 'data') and msg.data else 'None'}, gap={time_since_last:.1f}s")
                    
                    # 🚨 処理前のWebSocket状態を記録
                    logger.info(f"🔍 [LOOP_MONITOR] Before message processing: websocket.closed={self.websocket.closed}")
                    
                    # Server2準拠: メッセージタイプ別処理
                    if msg.type == web.WSMsgType.TEXT:
                        logger.info(f"🔍 [DEBUG_LOOP] Processing TEXT message: {msg.data[:100]}...")
                        await self.handle_message(msg.data)
                        logger.info(f"🔍 [DEBUG_LOOP] TEXT message processed, continuing loop, closed={self.websocket.closed}")
                    elif msg.type == web.WSMsgType.BINARY:
                        logger.info(f"🔍 [DEBUG_LOOP] Processing BINARY message: {len(msg.data)} bytes")
                        await self.handle_message(msg.data)
                        logger.info(f"🔍 [DEBUG_LOOP] BINARY message processed, continuing loop, closed={self.websocket.closed}")
                    else:
                        logger.warning(f"🔍 [DEBUG_LOOP] Unknown message type: {msg.type}({msg.type.value}), ignoring and continuing")
                    
                    # 🚨 処理後のWebSocket状態を記録
                    logger.info(f"🔍 [LOOP_MONITOR] After message processing: websocket.closed={self.websocket.closed}")
                    
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
                    current_time = time.time() * 1000
                    inactive_time = current_time - self.last_activity_time
                    
                    if inactive_time > self.timeout_seconds * 1000:
                        if not self.stop_event.is_set():
                            logger.info(f"🕐 [TIMEOUT] ESP32 connection timeout after {inactive_time/1000:.1f}s for {self.device_id}")
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
            