"""
Server2 style audio processing for xiaozhi-esp32-server3
Complete port of server2's audio handling logic
"""
import asyncio
import json
import struct
import time
import io
import wave
from typing import List, Optional
from utils.logger import setup_logger

logger = setup_logger()

class AudioHandlerServer2:
    def __init__(self, websocket_handler):
        self.handler = websocket_handler
        self.asr_audio = []  # List of Opus frames
        self.client_have_voice = False
        self.client_voice_stop = False
        self.last_activity_time = time.time() * 1000
        
        # RMSベース音声検知システム (server2準拠)
        self.client_have_voice = False
        self.last_voice_activity_time = time.time() * 1000  # milliseconds
        self.silence_threshold_ms = 1000  # 1秒無音で処理開始 (server2準拠)
        self.rms_threshold = 200  # RMS閾値 (server2準拠)
        self.voice_frame_count = 0  # 連続音声フレーム数
        self.silence_frame_count = 0  # 連続無音フレーム数
        self.is_processing = False  # 重複処理防止フラグ
        
        # Initialize Opus decoder
        try:
            import opuslib_next
            self.opus_decoder = opuslib_next.Decoder(16000, 1)
            logger.info("Opus decoder initialized successfully")
        except Exception as e:
            logger.error(f"Failed to initialize Opus decoder: {e}")
            self.opus_decoder = None

    async def handle_audio_frame(self, audio_data: bytes):
        """Handle single audio frame with RMS-based silence detection (server2準拠)"""
        try:
            # Drop tiny DTX packets (server2 style)
            dtx_threshold = 3
            if len(audio_data) <= dtx_threshold:
                logger.info(f"[AUDIO_TRACE] DROP_DTX pkt={len(audio_data)}")
                return

            # RMSベース音声検知 (server2準拠)
            is_voice = await self._detect_voice_with_rms(audio_data)
            current_time = time.time() * 1000
            
            # デバッグ: RMS VAD動作確認
            logger.info(f"🔍 [VAD_DEBUG] RMS検知結果: voice={is_voice}, audio_size={len(audio_data)}B")
            
            # Store audio frame regardless (server2 style)
            self.asr_audio.append(audio_data)
            self.asr_audio = self.asr_audio[-100:]  # Keep more frames
            
            logger.info(f"[AUDIO_TRACE] Frame: {len(audio_data)}B, RMS_voice={is_voice}, frames={len(self.asr_audio)}")
            
            if is_voice:
                # 音声検出
                if not self.client_have_voice:
                    logger.info("【音声開始検出】音声蓄積開始")
                self.client_have_voice = True
                self.last_voice_activity_time = current_time
                self.voice_frame_count += 1
                self.silence_frame_count = 0
            else:
                # 無音検出
                self.silence_frame_count += 1
                
                # server2準拠: 音声あり → 無音 + 1秒経過で処理開始
                if self.client_have_voice:
                    silence_duration = current_time - self.last_voice_activity_time
                    logger.info(f"【無音継続】{silence_duration:.0f}ms / {self.silence_threshold_ms}ms")
                    
                    if silence_duration >= self.silence_threshold_ms and len(self.asr_audio) > 5 and not self.is_processing:
                        logger.info(f"【無音検知完了】{silence_duration:.0f}ms無音 - 音声処理開始")
                        self.is_processing = True  # 重複処理防止
                        await self._process_voice_stop()

        except Exception as e:
            logger.error(f"Error handling audio frame: {e}")

    async def _process_voice_stop(self):
        """Process accumulated audio when voice stops (server2 style)"""
        try:
            # 重複処理防止の追加チェック
            if self.is_processing:
                logger.warning(f"⚠️ [PROCESSING] Already processing audio, skipping duplicate request")
                return
                
            # Check minimum requirement (調整: 長い発話を確実に処理)
            estimated_pcm_bytes = len(self.asr_audio) * 1920  # Each Opus frame ~1920 PCM bytes
            min_pcm_bytes = 15000  # 調整: 24000から15000に下げて長い発話も処理
            
            logger.info(f"[AUDIO_TRACE] Voice stop: {len(self.asr_audio)} frames, ~{estimated_pcm_bytes} PCM bytes")
            
            if estimated_pcm_bytes < min_pcm_bytes:
                logger.info(f"[AUDIO_TRACE] Buffer too small ({estimated_pcm_bytes} < {min_pcm_bytes}), discarding")
                self._reset_audio_state()
                return

            # Process accumulated frames
            audio_frames = self.asr_audio.copy()
            self._reset_audio_state()
            
            # Convert to WAV using server2 method
            wav_data = await self._opus_frames_to_wav(audio_frames)
            if wav_data:
                # Send to ASR
                await self._process_with_asr(wav_data)
            else:
                logger.warning("Failed to convert Opus frames to WAV")

        except Exception as e:
            logger.error(f"Error processing voice stop: {e}")

    async def _opus_frames_to_wav(self, opus_frames: List[bytes]) -> Optional[bytes]:
        """Convert Opus frames to WAV (server2 style)"""
        try:
            if not self.opus_decoder:
                logger.error("Opus decoder not available")
                return None

            # Decode Opus frames to PCM (server2 style)
            pcm_data = []
            buffer_size = 960  # 60ms at 16kHz
            
            for i, opus_packet in enumerate(opus_frames):
                try:
                    if not opus_packet or len(opus_packet) == 0:
                        continue
                    
                    pcm_frame = self.opus_decoder.decode(opus_packet, buffer_size)
                    if pcm_frame and len(pcm_frame) > 0:
                        pcm_data.append(pcm_frame)
                        
                except Exception as e:
                    logger.warning(f"Opus decode error, skip packet {i}: {e}")
                    continue

            if not pcm_data:
                logger.warning("No valid PCM data from Opus frames")
                return None

            # Create WAV file from PCM data (server2 style)
            wav_buffer = io.BytesIO()
            with wave.open(wav_buffer, 'wb') as wav_file:
                wav_file.setnchannels(1)      # mono
                wav_file.setsampwidth(2)      # 16-bit
                wav_file.setframerate(16000)  # 16kHz
                wav_file.writeframes(b''.join(pcm_data))
            
            wav_buffer.seek(0)
            wav_data = wav_buffer.read()
            
            total_pcm_bytes = sum(len(frame) for frame in pcm_data)
            logger.info(f"[AUDIO_TRACE] Opus->WAV: {len(opus_frames)} frames -> {total_pcm_bytes} PCM bytes -> {len(wav_data)} WAV bytes")
            
            return wav_data

        except Exception as e:
            logger.error(f"Error converting Opus to WAV: {e}")
            return None

    async def _process_with_asr(self, wav_data: bytes):
        """Process WAV data with ASR"""
        try:
            # Create file-like object for ASR
            wav_file = io.BytesIO(wav_data)
            wav_file.name = "audio.wav"
            
            # Call ASR service
            logger.info(f"🎤 [ASR_START] ===== Calling OpenAI Whisper API =====")
            transcribed_text = await self.handler.asr_service.transcribe(wav_file)
            logger.info(f"📝 [ASR_RESULT] ===== ASR Result: '{transcribed_text}' (length: {len(transcribed_text) if transcribed_text else 0}) =====")
            
            if transcribed_text and transcribed_text.strip():
                logger.info(f"✅ [ASR] Processing transcription: {transcribed_text}")
                await self.handler.process_text(transcribed_text)
            else:
                logger.warning(f"❌ [ASR] No valid result for {self.handler.device_id}")

        except Exception as e:
            logger.error(f"Error processing with ASR: {e}")

    async def _detect_voice_with_rms(self, audio_data: bytes) -> bool:
        """RMSベース音声検知 (server2 WebRTC VAD準拠)"""
        try:
            if not self.opus_decoder:
                # Fallback: サイズベース判定
                return len(audio_data) > 30
                
            # Opus → PCM変換
            pcm_data = self.opus_decoder.decode(audio_data, 960)  # 60ms frame
            if not pcm_data or len(pcm_data) < 4:
                return False
                
            # RMS計算 (server2準拠)
            import audioop
            try:
                rms_value = audioop.rms(pcm_data, 2)  # 16-bit samples
                is_voice = rms_value >= self.rms_threshold
                logger.info(f"[RMS_VAD] rms={rms_value} threshold={self.rms_threshold} voice={is_voice}")
                return is_voice
            except Exception as e:
                logger.debug(f"RMS calculation failed: {e}")
                # Fallback: numpy-based energy calculation
                import numpy as np
                pcm_int16 = np.frombuffer(pcm_data, dtype=np.int16)
                if pcm_int16.size > 0:
                    energy = np.mean(np.abs(pcm_int16))
                    is_voice = energy >= 100  # Conservative threshold
                    logger.info(f"[ENERGY_VAD] energy={energy:.1f} voice={is_voice}")
                    return is_voice
                return False
                
        except Exception as e:
            logger.debug(f"Voice detection error: {e}")
            return len(audio_data) > 20  # Safe fallback

    def _detect_voice_activity(self, audio_data: bytes) -> bool:
        """Detect voice activity using energy analysis (server2 style)"""
        try:
            # Size-based initial filter (server2 style)
            if len(audio_data) <= 10:  # Very small packets are likely DTX/silence
                return False
            
            # Decode to PCM for energy analysis (if possible)
            if self.opus_decoder and len(audio_data) > 10:
                try:
                    pcm_data = self.opus_decoder.decode(audio_data, 960)  # 60ms frame
                    if pcm_data and len(pcm_data) > 0:
                        # Calculate energy (RMS-like)
                        import numpy as np
                        pcm_int16 = np.frombuffer(pcm_data, dtype=np.int16)
                        if pcm_int16.size > 0:
                            energy = np.mean(np.abs(pcm_int16))
                            # Energy threshold (調整: より敏感に)
                            voice_detected = energy >= 80  # より敏感な閾値で小声も拾う
                            logger.info(f"[VAD_ENERGY] pkt={len(audio_data)}B, energy={energy:.1f}, voice={voice_detected}")
                            return voice_detected
                except Exception as e:
                    logger.debug(f"[VAD] Opus decode failed for energy analysis: {e}")
            
            # Fallback: size-based detection (server2 backup method)
            voice_detected = len(audio_data) > 30  # Reasonable threshold for voice packets
            logger.info(f"[VAD_SIZE] pkt={len(audio_data)}B, voice={voice_detected}")
            return voice_detected
            
        except Exception as e:
            logger.error(f"VAD detection error: {e}")
            return len(audio_data) > 20  # Safe fallback

    def _reset_audio_state(self):
        """Reset audio state (server2 style with RMS VAD reset)"""
        self.asr_audio.clear()
        self.client_have_voice = False
        self.client_voice_stop = False
        self.voice_frame_count = 0
        self.silence_frame_count = 0
        self.last_voice_activity_time = time.time() * 1000
        self.is_processing = False  # 重複処理防止フラグもリセット
        logger.info("[AUDIO_TRACE] Audio state reset (RMS VAD+処理フラグリセット)")
