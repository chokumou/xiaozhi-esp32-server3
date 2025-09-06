import asyncio
from typing import Optional
from openai import AsyncOpenAI
from config import config
from utils.logger import log

class TTSProvider:
    """OpenAI TTS音声合成プロバイダー"""
    
    def __init__(self):
        self.client = AsyncOpenAI(api_key=config.OPENAI_API_KEY)
    
    async def synthesize(self, text: str) -> Optional[bytes]:
        """テキストを音声に変換"""
        try:
            log.debug(f"TTS synthesis start: '{text[:50]}...'")
            
            # OpenAI TTS APIで音声合成
            response = await self.client.audio.speech.create(
                model=config.OPENAI_TTS_MODEL,
                voice=config.OPENAI_TTS_VOICE,
                input=text,
                response_format="opus"  # ESP32用にOPUS形式
            )
            
            # 音声データを取得
            audio_data = response.content
            
            if audio_data:
                log.info(f"TTS success: {len(audio_data)} bytes")
                return audio_data
            else:
                log.warning("TTS returned empty audio data")
                return None
                
        except Exception as e:
            log.error(f"TTS synthesis failed: {e}")
            return None
    
    async def synthesize_streaming(self, text: str):
        """ストリーミング音声合成（将来用）"""
        # 現在のOpenAI APIはストリーミングをサポートしていないため
        # 通常の合成を使用
        return await self.synthesize(text)
