import edge_tts
import opuslib_next
from pydub import AudioSegment
from io import BytesIO
from config import Config
from utils.logger import setup_logger

logger = setup_logger()

class EdgeTTSService:
    def __init__(self):
        self.voice = Config.EDGE_TTS_VOICE
        logger.info(f"EdgeTTSService initialized with voice: {self.voice}")

    async def generate_speech(self, text: str) -> bytes:
        try:
            logger.info(f"🔄 [EDGE_TTS] Starting TTS generation for: '{text}'")
            
            # EdgeTTSで音声生成（Server2互換）
            communicate = edge_tts.Communicate(text, self.voice)
            audio_bytes = b""
            
            # 音声データを取得
            async for chunk in communicate.stream():
                if chunk["type"] == "audio":
                    audio_bytes += chunk["data"]
            
            logger.info(f"🔄 [EDGE_TTS] Generated {len(audio_bytes)} bytes audio for text: '{text[:50]}...'")
            
            # Server2準拠: MP3 → PCM → Opus フレーム分割処理
            opus_frames = await self._convert_to_opus_frames(audio_bytes, "mp3")
            
            return opus_frames
            
        except Exception as e:
            logger.error(f"❌ [EDGE_TTS] Generation failed: {e}")
            import traceback
            logger.error(f"❌ [EDGE_TTS] Stack trace: {traceback.format_exc()}")
            return b""
    
    async def _convert_to_opus_frames(self, audio_bytes: bytes, file_type: str) -> bytes:
        """Server2準拠: 音声データをOpusフレームに変換"""
        try:
            logger.debug(f"Converting {file_type} audio to Opus frames ({len(audio_bytes)} bytes)")
            
            # AudioSegment で PCM に変換 (Server2準拠: 16kHz)
            audio = AudioSegment.from_file(BytesIO(audio_bytes), format=file_type)
            audio = audio.set_channels(1).set_frame_rate(16000).set_sample_width(2)  # Server2準拠: 16kHz
            raw_data = audio.raw_data
            
            logger.debug(f"PCM conversion: {len(raw_data)} bytes")
            
            # Server2準拠: PCM を60msフレームでOpusエンコード（個別フレームリスト）
            opus_frames_list = await self._pcm_to_opus_frames(raw_data)
            
            # Server2準拠: 個別フレームのリストを返す
            logger.debug(f"Individual Opus frames generated: {len(opus_frames_list)} frames")
            logger.info(f"🔬 [EDGE_TTS] Returning individual Opus frames list")
            return opus_frames_list
            
        except Exception as e:
            logger.error(f"Audio conversion failed: {e}")
            return b""
    
    async def _pcm_to_opus_frames(self, raw_data: bytes) -> list:
        """Server2準拠: PCMデータを60msフレームでOpusエンコード（個別フレームリスト）"""
        try:
            import numpy as np
            
            # Opus エンコーダー初期化 (Server2準拠: 16kHz)
            encoder = opuslib_next.Encoder(16000, 1, opuslib_next.APPLICATION_AUDIO)
            
            # Server2準拠: opus_encoder_utils.py の設定を適用
            encoder.bitrate = 24000        # 24kbps bitrate
            encoder.complexity = 10        # 最高品質
            encoder.signal = opuslib_next.SIGNAL_VOICE  # 音声信号最適化
            
            # 60ms フレーム設定 (Server2準拠: 16kHz)
            frame_duration = 60  # 60ms per frame
            frame_size = int(16000 * frame_duration / 1000)  # 960 samples/frame (16kHz)
            
            opus_frames_list = []  # 個別フレームのリスト
            frame_count = 0
            
            # PCMデータを60msフレームごとにエンコード (Server2準拠)
            for i in range(0, len(raw_data), frame_size * 2):  # 16bit=2bytes/sample
                chunk = raw_data[i:i + frame_size * 2]
                
                # 最後のフレームが短い場合はパディング
                if len(chunk) < frame_size * 2:
                    chunk += b'\x00' * (frame_size * 2 - len(chunk))
                
                # Server2準拠: numpy配列経由でエンコード
                np_frame = np.frombuffer(chunk, dtype=np.int16)
                opus_frame = encoder.encode(np_frame.tobytes(), frame_size)
                
                # フレーム長をチェック (ESP32互換性)
                if len(opus_frame) > 0:
                    opus_frames_list.append(opus_frame)  # 個別フレームとして保存
                    frame_count += 1
                    
                    # 最初のフレーム詳細ログ
                    if frame_count == 1:
                        logger.info(f"🔬 [EDGE_OPUS] First frame: size={len(opus_frame)}bytes, pcm_samples={len(np_frame)}, hex={opus_frame[:8].hex()}")
                    
                    logger.debug(f"Encoded Opus frame {frame_count}: {len(opus_frame)} bytes")
                else:
                    logger.warning(f"Empty Opus frame generated for frame {frame_count}")
            
            logger.info(f"🎵 [EDGE_TTS] Generated {frame_count} Opus frames (16kHz, 60ms) from {len(raw_data)} bytes PCM")
            return opus_frames_list
            
        except Exception as e:
            logger.error(f"❌ [EDGE_TTS] Opus encoding failed: {e}")
            import traceback
            logger.error(f"❌ [EDGE_TTS] Opus encoding traceback: {traceback.format_exc()}")
            return []

