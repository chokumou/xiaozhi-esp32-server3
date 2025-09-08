import openai
import opuslib_next
from pydub import AudioSegment
from io import BytesIO
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
                    response_format="mp3"  # MP3で取得してPCM変換後Opusエンコード
                )
            )
            
            # Get audio content and convert to Server2-style format
            audio_data = response.content
            logger.debug(f"TTS generated MP3 audio for text: {text[:50]}... ({len(audio_data)} bytes)")
            
            # Server2準拠: MP3 → PCM → Opus フレーム分割処理
            opus_frames = await self._convert_to_opus_frames(audio_data, "mp3")
            
            return opus_frames
            
        except Exception as e:
            logger.error(f"TTS generation failed: {e}")
            return b""
    
    async def _convert_to_opus_frames(self, audio_bytes: bytes, file_type: str) -> bytes:
        """Server2準拠: 音声データをOpusフレームに変換"""
        try:
            logger.debug(f"Converting {file_type} audio to Opus frames ({len(audio_bytes)} bytes)")
            
            # AudioSegment で PCM に変換
            audio = AudioSegment.from_file(BytesIO(audio_bytes), format=file_type)
            audio = audio.set_channels(1).set_frame_rate(16000).set_sample_width(2)
            raw_data = audio.raw_data
            
            logger.debug(f"PCM conversion: {len(raw_data)} bytes")
            
            # Server2準拠: PCM を60msフレームでOpusエンコード
            opus_data = await self._pcm_to_opus_frames(raw_data)
            
            logger.debug(f"Opus frames generated: {len(opus_data)} bytes")
            return opus_data
            
        except Exception as e:
            logger.error(f"Audio conversion failed: {e}")
            return b""
    
    async def _pcm_to_opus_frames(self, raw_data: bytes) -> bytes:
        """Server2準拠: PCMデータを60msフレームでOpusエンコード"""
        try:
            # Opus エンコーダー初期化
            encoder = opuslib_next.Encoder(16000, 1, opuslib_next.APPLICATION_AUDIO)
            
            # 60ms フレーム設定 (Server2と同じ)
            frame_duration = 60  # 60ms per frame
            frame_size = int(16000 * frame_duration / 1000)  # 960 samples/frame
            
            opus_frames = bytearray()
            
            # PCMデータを60msフレームごとにエンコード
            for i in range(0, len(raw_data), frame_size * 2):  # 16bit=2bytes/sample
                chunk = raw_data[i:i + frame_size * 2]
                
                # 最後のフレームが短い場合はパディング
                if len(chunk) < frame_size * 2:
                    chunk += b'\x00' * (frame_size * 2 - len(chunk))
                
                # Opus エンコード
                opus_frame = encoder.encode(chunk, frame_size)
                opus_frames.extend(opus_frame)
            
            logger.debug(f"Encoded {len(opus_frames)} bytes of Opus frames from {len(raw_data)} bytes PCM")
            return bytes(opus_frames)
            
        except Exception as e:
            logger.error(f"Opus encoding failed: {e}")
            return b""
