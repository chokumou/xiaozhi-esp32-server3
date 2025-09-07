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
                from pydub import AudioSegment
                
                # Try to convert Opus to WAV
                try:
                    logger.info(f"🔄 [OPUS] Attempting to convert {len(audio_input)} bytes of Opus data")
                    # Load Opus data
                    audio_segment = AudioSegment.from_file(io.BytesIO(audio_input), format="opus")
                    logger.info(f"✅ [OPUS] Loaded audio segment: {len(audio_segment)}ms, {audio_segment.frame_rate}Hz")
                    
                    # Convert to WAV
                    wav_buffer = io.BytesIO()
                    audio_segment.export(wav_buffer, format="wav")
                    wav_buffer.seek(0)
                    
                    audio_file = wav_buffer
                    audio_file.name = "audio.wav"
                    logger.info(f"🎉 [OPUS] Successfully converted Opus to WAV: {len(audio_input)} -> {len(wav_buffer.getvalue())} bytes")
                    
                except Exception as convert_error:
                    logger.error(f"❌ [OPUS] Conversion failed: {convert_error}")
                    logger.error(f"❌ [OPUS] Error type: {type(convert_error).__name__}")
                    # Fallback: try raw data as WAV
                    logger.info(f"⚠️ [OPUS] Fallback: using raw data as WAV")
                    audio_file = io.BytesIO(audio_input)
                    audio_file.name = "audio.wav"
            else:
                audio_file = audio_input
                
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
