import openai
from config import Config
from utils.logger import setup_logger

logger = setup_logger()

class ASRService:
    def __init__(self):
        self.client = openai.OpenAI(api_key=Config.OPENAI_API_KEY)
        self.model = Config.OPENAI_ASR_MODEL
        logger.info(f"ASRService initialized with model: {self.model}")

    async def transcribe(self, audio_input) -> str:
        try:
            # Handle both bytes and file-like objects
            if isinstance(audio_input, bytes):
                import io
                # Assume bytes are already converted WAV data
                audio_file = io.BytesIO(audio_input)
                audio_file.name = "audio.wav"
                logger.info(f"📁 [ASR] Processing {len(audio_input)} bytes as WAV file")
            else:
                audio_file = audio_input
                logger.info(f"📁 [ASR] Processing file-like object: {getattr(audio_file, 'name', 'unknown')}")
                
            # Skip very small audio data (likely silence or noise)
            if hasattr(audio_file, 'getvalue'):
                data_size = len(audio_file.getvalue())
            else:
                audio_file.seek(0, 2)  # Seek to end
                data_size = audio_file.tell()
                audio_file.seek(0)  # Seek back to beginning
                
            if data_size < 1000:  # Less than 1KB, likely too short
                logger.debug(f"Skipping small audio data: {data_size} bytes")
                return ""

            response = self.client.audio.transcriptions.create(
                model=self.model,
                file=audio_file,
                response_format="text",
                language="ja"  # Japanese
            )
            
            result = response.strip() if response else ""
            if result:
                logger.info(f"ASR success: '{result}'")
            else:
                logger.debug("ASR returned empty text")
            return result
            
        except Exception as e:
            logger.error(f"ASR transcription failed: {e}")
            return ""
ｓ