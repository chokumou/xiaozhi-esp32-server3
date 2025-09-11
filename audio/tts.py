import openai
import opuslib_next
from pydub import AudioSegment
from io import BytesIO
from config import Config
from utils.logger import setup_logger
from .edge_tts import EdgeTTSService
from .voicevox_tts import VoicevoxTTSService

logger = setup_logger()

class TTSService:
    def __init__(self):
        # 3段階フォールバック準備
        if Config.USE_VOICEVOX:
            self.voicevox = VoicevoxTTSService()
            logger.info(f"TTSService initialized with VOICEVOX speaker_id: {Config.VOICEVOX_SPEAKER_ID}")
        
        if Config.USE_EDGE_TTS:
            self.edge_tts = EdgeTTSService()
            logger.info(f"TTSService EdgeTTS backup prepared: {Config.EDGE_TTS_VOICE}")
        
        # OpenAI TTS（最終フォールバック）
        self.client = openai.OpenAI(api_key=Config.OPENAI_API_KEY)
        self.openai_voice = Config.OPENAI_TTS_VOICE
        logger.info(f"TTSService OpenAI TTS fallback prepared: {self.openai_voice}")

    async def generate_speech(self, text: str) -> bytes:
        """3段階フォールバック: VOICEVOX → EdgeTTS → OpenAI TTS"""
        try:
            # 1st Try: VOICEVOX（可愛い日本語音声）
            if Config.USE_VOICEVOX:
                try:
                    logger.info(f"🎵 [VOICEVOX] Using VOICEVOX for text: {text[:50]}...")
                    return await self.voicevox.generate_speech(text)
                except Exception as voicevox_error:
                    logger.error(f"⚠️ [VOICEVOX_FAILED] VOICEVOX failed: {voicevox_error}")
            
            # 2nd Try: EdgeTTS（日本語音声）
            if Config.USE_EDGE_TTS:
                try:
                    logger.info(f"🔄 [FALLBACK_EDGE] Switching to EdgeTTS...")
                    return await self.edge_tts.generate_speech(text)
                except Exception as edge_error:
                    logger.error(f"⚠️ [EDGE_TTS_FAILED] EdgeTTS fallback failed: {edge_error}")
            
            # 3rd Try: OpenAI TTS（最終フォールバック）
            logger.info(f"🔄 [FALLBACK_OPENAI] Switching to OpenAI TTS as final fallback...")
            return await self._generate_openai_speech(text)
            
        except Exception as e:
            logger.error(f"TTS generation completely failed: {e}")
            return b""
    
    async def _generate_openai_speech(self, text: str) -> bytes:
        """OpenAI TTS音声生成（フォールバック用）"""
        try:
            if not hasattr(self, 'client'):
                self.client = openai.OpenAI(api_key=Config.OPENAI_API_KEY)
            
            import asyncio
            response = await asyncio.get_event_loop().run_in_executor(
                None, 
                lambda: self.client.audio.speech.create(
                    model="tts-1",
                    voice=Config.OPENAI_TTS_VOICE,  # alloy
                    input=text,
                    response_format="mp3"  # MP3で取得してPCM変換後Opusエンコード
                )
            )
            
            # Get audio content and convert to Server2-style format
            audio_data = response.content
            logger.info(f"✅ [OPENAI_FALLBACK] Generated MP3 audio: {len(audio_data)} bytes")
            
            # Server2準拠: MP3 → PCM → Opus フレーム分割処理
            opus_frames = await self._convert_to_opus_frames(audio_data, "mp3")
            
            return opus_frames
            
        except Exception as e:
            logger.error(f"OpenAI TTS fallback failed: {e}")
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
            logger.info(f"🔬 [SERVER2_STYLE] Returning individual Opus frames list")
            return opus_frames_list
            
            # # ESP32プロトコル対応: BinaryProtocol3ヘッダーを追加
            # protocol_data = self._add_binary_protocol3_header(opus_data)
            # 
            # logger.debug(f"Protocol3 data generated: {len(protocol_data)} bytes (Opus: {len(opus_data)} bytes)")
            # return protocol_data
            
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
                        logger.info(f"🔬 [OPUS_ENCODE] First frame: size={len(opus_frame)}bytes, pcm_samples={len(np_frame)}, hex={opus_frame[:8].hex()}")
                    
                    logger.debug(f"Encoded Opus frame {frame_count}: {len(opus_frame)} bytes")
                else:
                    logger.warning(f"Empty Opus frame generated for frame {frame_count}")
            
            logger.info(f"🎵 [SERVER2_EXACT] Generated {frame_count} Opus frames (16kHz, 60ms) for batch send from {len(raw_data)} bytes PCM")
            return opus_frames_list
            
        except Exception as e:
            logger.error(f"Opus encoding failed: {e}")
            import traceback
            logger.error(f"Opus encoding traceback: {traceback.format_exc()}")
            return []
    
    def _add_binary_protocol3_header(self, opus_data: bytes) -> bytes:
        """ESP32 BinaryProtocol3ヘッダーを追加"""
        try:
            import struct
            
            # BinaryProtocol3構造:
            # uint8_t type;           // 0 = OPUS audio data
            # uint8_t reserved;       // 予約領域 (0)
            # uint16_t payload_size;  // ペイロードサイズ (ネットワークバイトオーダー)
            # uint8_t payload[];      // Opusデータ
            
            type_field = 0  # OPUS audio type
            reserved_field = 0  # 予約領域
            payload_size = len(opus_data)
            
            # ネットワークバイトオーダー (big-endian) でパック
            header = struct.pack('>BBH', type_field, reserved_field, payload_size)
            
            # ヘッダー + Opusデータ
            protocol_data = header + opus_data
            
            logger.debug(f"BinaryProtocol3 header: type={type_field}, reserved={reserved_field}, payload_size={payload_size}")
            return protocol_data
            
        except Exception as e:
            logger.error(f"BinaryProtocol3 header creation failed: {e}")
            return opus_data  # ヘッダー追加に失敗した場合は生データを返す
