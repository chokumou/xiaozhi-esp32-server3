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
        
        # Initialize Opus decoder
        try:
            import opuslib_next
            self.opus_decoder = opuslib_next.Decoder(16000, 1)
            logger.info("Opus decoder initialized successfully")
        except Exception as e:
            logger.error(f"Failed to initialize Opus decoder: {e}")
            self.opus_decoder = None

    async def handle_audio_frame(self, audio_data: bytes):
        """Handle single audio frame (server2 style)"""
        try:
            # Drop tiny DTX packets (server2 style)
            dtx_threshold = 3
            if len(audio_data) <= dtx_threshold:
                logger.info(f"[AUDIO_TRACE] DROP_DTX pkt={len(audio_data)}")
                return

            # Simple VAD based on packet size (server2 approximation)
            have_voice = len(audio_data) > 50  # Larger packets indicate voice
            
            # Update activity time
            self.last_activity_time = time.time() * 1000
            
            # Store audio frame (server2 style)
            self.asr_audio.append(audio_data)
            self.asr_audio = self.asr_audio[-50:]  # Keep more frames for better quality
            
            logger.info(f"[AUDIO_TRACE] Frame: {len(audio_data)}B, voice={have_voice}, total_frames={len(self.asr_audio)}")
            
            # Voice state management (server2 style)
            if have_voice:
                if not self.client_have_voice:
                    logger.info("[AUDIO_TRACE] Voice detected - starting accumulation")
                self.client_have_voice = True
            else:
                if self.client_have_voice:
                    logger.info("[AUDIO_TRACE] Voice ended - trigger processing")
                    self.client_voice_stop = True

            # Process accumulated audio when voice stops (server2 style)
            if self.client_voice_stop and len(self.asr_audio) > 0:
                await self._process_voice_stop()

        except Exception as e:
            logger.error(f"Error handling audio frame: {e}")

    async def _process_voice_stop(self):
        """Process accumulated audio when voice stops (server2 style)"""
        try:
            # Check minimum requirement (server2 style)
            estimated_pcm_bytes = len(self.asr_audio) * 1920  # Each Opus frame ~1920 PCM bytes
            min_pcm_bytes = 12000  # server2 default
            
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

    def _reset_audio_state(self):
        """Reset audio state (server2 style)"""
        self.asr_audio.clear()
        self.client_have_voice = False
        self.client_voice_stop = False
        logger.info("[AUDIO_TRACE] Audio state reset")
