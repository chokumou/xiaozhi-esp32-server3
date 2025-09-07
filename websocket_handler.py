import asyncio
import json
import struct
import uuid
import io
from typing import Dict, Any, Optional
from collections import deque
from aiohttp import web

from config import Config
from utils.logger import setup_logger
from utils.auth import AuthManager, AuthError
from audio.asr import ASRService
from audio.tts import TTSService
from ai.llm import LLMService
from ai.memory import MemoryService

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
        self.stop_event = asyncio.Event() # For graceful shutdown
        self.session_id = str(uuid.uuid4())
        self.audio_format = "opus"  # Default format
        self.features = {}
        
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
            await self.handle_text_message(message)
        elif isinstance(message, bytes):
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
            if len(message) <= 12:  # Skip very small packets (DTX/keepalive)
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
            else:
                # Protocol v1: raw audio data
                audio_data = message

            await self.process_audio_binary(audio_data)
            
        except Exception as e:
            logger.error(f"Error handling binary message from {self.device_id}: {e}")

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

    async def process_audio_binary(self, audio_data: bytes):
        """Process binary audio data"""
        try:
            # Create file-like object for OpenAI Whisper API
            audio_file = io.BytesIO(audio_data)
            audio_file.name = "audio.opus" if self.audio_format == "opus" else "audio.wav"
            
            # Convert audio to text using ASR
            transcribed_text = await self.asr_service.transcribe(audio_file)
            if transcribed_text and transcribed_text.strip():
                logger.info(f"ASR result for {self.device_id}: {transcribed_text}")
                await self.process_text(transcribed_text)
            else:
                logger.debug(f"No ASR result for {self.device_id}")
                
        except Exception as e:
            logger.error(f"Error processing audio from {self.device_id}: {e}")

    async def process_text(self, text: str):
        """Process text input through LLM and generate response"""
        try:
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

            # Generate LLM response
            llm_response = await self.llm_service.chat_completion(llm_messages)
            if llm_response and llm_response.strip():
                logger.info(f"LLM response for {self.device_id}: {llm_response}")
                self.chat_history.append({"role": "assistant", "content": llm_response})
                
                # Send text response first
                await self.send_text_response(llm_response)
                
                # Generate and send audio response
                await self.send_audio_response(llm_response)
            else:
                logger.warning(f"No LLM response for {self.device_id}")
                
        except Exception as e:
            logger.error(f"Error processing text from {self.device_id}: {e}")

    async def send_text_response(self, text: str):
        """Send text response to client"""
        try:
            response = {"type": "text", "data": text}
            await self.websocket.send_str(json.dumps(response))
        except Exception as e:
            logger.error(f"Error sending text response to {self.device_id}: {e}")

    async def send_audio_response(self, text: str):
        """Generate and send audio response"""
        try:
            self.client_is_speaking = True
            
            # Generate audio using TTS
            audio_bytes = await self.tts_service.generate_speech(text)
            if audio_bytes:
                # Send binary audio data based on protocol version
                if self.protocol_version == 2:
                    # Protocol v2: version(2) + type(2) + reserved(2) + timestamp(4) + payload_size(4) + payload
                    header = struct.pack('>HHHII', 2, 1, 0, 0, len(audio_bytes))
                    message = header + audio_bytes
                elif self.protocol_version == 3:
                    # Protocol v3: type(1) + reserved(1) + payload_size(2) + payload
                    header = struct.pack('>BBH', 1, 0, len(audio_bytes))
                    message = header + audio_bytes
                else:
                    # Protocol v1: raw audio data
                    message = audio_bytes
                    
                await self.websocket.send_bytes(message)
                logger.info(f"Sent audio response to {self.device_id} ({len(audio_bytes)} bytes)")
            else:
                logger.warning(f"Failed to generate audio for {self.device_id}")
                
        except Exception as e:
            logger.error(f"Error sending audio response to {self.device_id}: {e}")
        finally:
            self.client_is_speaking = False

    async def run(self):
        """Main connection loop"""
        try:
            async for msg in self.websocket:
                if msg.type == web.WSMsgType.TEXT:
                    await self.handle_message(msg.data)
                elif msg.type == web.WSMsgType.BINARY:
                    await self.handle_message(msg.data)
                elif msg.type == web.WSMsgType.ERROR:
                    logger.error(f"WebSocket error for {self.device_id}: {self.websocket.exception()}")
                    break
                elif msg.type == web.WSMsgType.CLOSE:
                    logger.info(f"WebSocket closed for {self.device_id}")
                    break
        except Exception as e:
            logger.error(f"Unhandled error in connection handler for {self.device_id}: {e}")
        finally:
            self.stop_event.set()
            logger.info(f"Connection handler stopped for {self.device_id}")