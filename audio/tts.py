import openai
from config import Config
from utils.logger import setup_logger

logger = setup_logger()

class TTSService:
    def __init__(self):
        self.client = openai.OpenAI(api_key=Config.OPENAI_API_KEY)
        self.voice = Config.OPENAI_TTS_VOICE
        logger.info(f"TTSService initialized with voice: {self.voice}")

    async def generate_speech(self, text: str) -> bytes:
        try:
            # Use synchronous API call in async context
            import asyncio
            response = await asyncio.get_event_loop().run_in_executor(
                None, 
                lambda: self.client.audio.speech.create(
                    model="tts-1",
                    voice=self.voice,
                    input=text,
                    response_format="opus"  # Opus format for ESP32 compatibility
                )
            )
            
            # Get audio content
            audio_data = response.content
            logger.debug(f"TTS generated audio for text: {text[:50]}... ({len(audio_data)} bytes)")
            return audio_data
            
        except Exception as e:
            logger.error(f"TTS generation failed: {e}")
            return b""
