import json
import asyncio
import io
from typing import Dict, Any, Optional
import websockets
from websockets.server import WebSocketServerProtocol

from utils.logger import log
from utils.auth import AuthManager
from audio.asr import ASRProvider
from audio.tts import TTSProvider
from ai.llm import LLMProvider
from ai.memory import MemoryManager

class WebSocketHandler:
    """WebSocket接続ハンドラー"""
    
    def __init__(self):
        self.asr_provider = ASRProvider()
        self.tts_provider = TTSProvider()
        self.llm_provider = LLMProvider()
        self.memory_manager = MemoryManager()
        self.auth_manager = AuthManager()
    
    async def handle_connection(self, websocket: WebSocketServerProtocol, path: str):
        """WebSocket接続を処理"""
        client_ip = websocket.remote_address[0] if websocket.remote_address else "unknown"
        log.info(f"New WebSocket connection from {client_ip}")
        
        device_id = None
        conversation_history = []
        
        try:
            async for message in websocket:
                try:
                    if isinstance(message, bytes):
                        # 音声データの場合
                        await self._handle_audio_message(
                            websocket, message, device_id, conversation_history
                        )
                    else:
                        # テキストメッセージの場合
                        data = json.loads(message)
                        result = await self._handle_text_message(
                            websocket, data, conversation_history
                        )
                        if result and "device_id" in result:
                            device_id = result["device_id"]
                            
                except json.JSONDecodeError:
                    log.warning("Invalid JSON message received")
                    await self._send_error(websocket, "Invalid JSON format")
                except Exception as e:
                    log.error(f"Message handling error: {e}")
                    await self._send_error(websocket, "Message processing failed")
                    
        except websockets.exceptions.ConnectionClosed:
            log.info(f"WebSocket connection closed for {client_ip}")
        except Exception as e:
            log.error(f"WebSocket connection error: {e}")
        finally:
            log.info(f"WebSocket connection ended for {client_ip}")
    
    async def _handle_text_message(self, websocket: WebSocketServerProtocol, data: Dict[str, Any], conversation_history: list) -> Optional[Dict[str, Any]]:
        """テキストメッセージを処理"""
        message_type = data.get("type")
        
        if message_type == "hello":
            return await self._handle_hello(websocket, data)
        elif message_type == "text":
            return await self._handle_text_input(websocket, data, conversation_history)
        else:
            log.warning(f"Unknown message type: {message_type}")
            return None
    
    async def _handle_hello(self, websocket: WebSocketServerProtocol, data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Hello メッセージを処理"""
        log.info("Received hello message")
        
        # 認証情報を抽出
        headers = data.get("headers", {})
        device_id = self.auth_manager.extract_device_id(headers)
        
        if not device_id:
            log.warning("No device ID found in hello message")
            await self._send_error(websocket, "Device ID required")
            return None
        
        # Welcome メッセージを送信
        welcome_msg = {
            "type": "welcome",
            "message": "xiaozhi-server3 connected",
            "device_id": device_id,
            "features": {
                "asr": True,
                "tts": True,
                "memory": True
            }
        }
        
        await websocket.send(json.dumps(welcome_msg))
        log.info(f"Welcome message sent to device {device_id}")
        
        return {"device_id": device_id}
    
    async def _handle_audio_message(self, websocket: WebSocketServerProtocol, audio_data: bytes, device_id: Optional[str], conversation_history: list):
        """音声メッセージを処理"""
        if not device_id:
            log.warning("Audio message received but no device ID")
            return
        
        log.debug(f"Received audio data: {len(audio_data)} bytes from device {device_id}")
        
        # 音声認識
        text = await self.asr_provider.transcribe(audio_data, "wav")
        if not text:
            log.warning("ASR failed or returned empty text")
            return
        
        # テキスト入力として処理
        await self._process_text_input(websocket, text, device_id, conversation_history)
    
    async def _handle_text_input(self, websocket: WebSocketServerProtocol, data: Dict[str, Any], conversation_history: list) -> None:
        """テキスト入力を処理"""
        text = data.get("text", "").strip()
        device_id = data.get("device_id")
        
        if not text or not device_id:
            log.warning("Text input missing text or device_id")
            return
        
        await self._process_text_input(websocket, text, device_id, conversation_history)
    
    async def _process_text_input(self, websocket: WebSocketServerProtocol, text: str, device_id: str, conversation_history: list):
        """テキスト入力を処理"""
        log.info(f"Processing text from device {device_id}: '{text}'")
        
        # 会話履歴に追加
        conversation_history.append({"role": "user", "content": text})
        
        # 記憶検索
        memory_context = ""
        if any(keyword in text for keyword in ["覚えてる", "思い出", "何だっけ", "教えて"]):
            log.debug("Memory search triggered")
            memory_context = await self.memory_manager.search_memory(device_id, text)
            if memory_context:
                log.info(f"Memory found for context: {memory_context[:100]}...")
        
        # LLM応答生成
        response = await self.llm_provider.chat(conversation_history[-5:], memory_context)  # 最新5件のみ使用
        if not response:
            response = "すみません、うまく聞き取れませんでした。"
        
        # 会話履歴に追加
        conversation_history.append({"role": "assistant", "content": response})
        
        # 記憶すべき情報がある場合は保存
        if any(keyword in text for keyword in ["覚えて", "覚えておいて", "記憶して"]):
            log.debug("Memory save triggered")
            memory_text = await self.llm_provider.summarize_memory(f"ユーザー: {text}")
            if memory_text:
                await self.memory_manager.save_memory(device_id, memory_text)
        
        # テキスト応答を送信
        text_response = {
            "type": "text",
            "text": response,
            "device_id": device_id
        }
        await websocket.send(json.dumps(text_response))
        
        # 音声合成
        audio_data = await self.tts_provider.synthesize(response)
        if audio_data:
            # 音声応答を送信
            audio_response = {
                "type": "audio",
                "format": "opus",
                "device_id": device_id
            }
            await websocket.send(json.dumps(audio_response))
            await websocket.send(audio_data)
            log.info(f"Audio response sent to device {device_id}")
        else:
            log.warning("TTS failed, sending text only")
    
    async def _send_error(self, websocket: WebSocketServerProtocol, message: str):
        """エラーメッセージを送信"""
        error_msg = {
            "type": "error",
            "message": message
        }
        try:
            await websocket.send(json.dumps(error_msg))
        except Exception as e:
            log.error(f"Failed to send error message: {e}")
