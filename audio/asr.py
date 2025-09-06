import io
import asyncio
from typing import Optional
from openai import AsyncOpenAI
from config import config
from utils.logger import log

class ASRProvider:
    """OpenAI Whisper音声認識プロバイダー"""
    
    def __init__(self):
        self.client = AsyncOpenAI(api_key=config.OPENAI_API_KEY)
    
    async def transcribe(self, audio_data: bytes, format: str = "wav") -> Optional[str]:
        """音声データを文字に変換"""
        try:
            log.debug(f"ASR transcription start, audio size: {len(audio_data)} bytes")
            
            # 音声データをファイルオブジェクトに変換
            audio_file = io.BytesIO(audio_data)
            audio_file.name = f"audio.{format}"
            
            # OpenAI Whisper APIで音声認識
            response = await self.client.audio.transcriptions.create(
                model=config.OPENAI_ASR_MODEL,
                file=audio_file,
                language="ja"  # 日本語指定
            )
            
            text = response.text.strip()
            if text:
                log.info(f"ASR success: '{text}'")
                return text
            else:
                log.warning("ASR returned empty text")
                return None
                
        except Exception as e:
            log.error(f"ASR transcription failed: {e}")
            return None
