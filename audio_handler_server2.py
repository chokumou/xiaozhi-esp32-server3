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
import uuid
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
        self.silence_threshold_ms = 2000  # 2秒無音で処理開始 (長い発話対応)
        self.rms_threshold = 250  # RMS閾値を上げて過敏反応を抑制
        self.voice_frame_count = 0  # 連続音声フレーム数
        self.silence_frame_count = 0  # 連続無音フレーム数
        self.is_processing = False  # 重複処理防止フラグ
        self.tts_in_progress = False  # TTS中は音声検知一時停止
        self.client_is_speaking = False  # AI発話中フラグ（server2準拠エコー防止）
        
        # RID追跡システム（検索しやすいログ用）
        self.current_request_id = None
        self.active_tts_rid = None  # 現在再生中のTTS RID
        
        # Server2準拠: wake_guard機能（連続発話対応）
        self.wake_until = 0  # この時間まで強制的に発話継続と判定
        self.wake_guard_ms = 500  # 有音後500ms間は発話継続（server2の300msより長め）
        
        # TTS終了後クールダウン（音響回り込み防止）
        self.tts_cooldown_until = 0  # この時間まで音声処理をスキップ
        self.tts_cooldown_ms = 800  # TTS終了後800msクールダウン（エコー完全安定化）
        
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
            current_time = time.time() * 1000
            
            # TTS終了後クールダウン期間のチェック（最優先）
            if current_time < self.tts_cooldown_until:
                logger.debug(f"[TTS_COOLDOWN] 音声処理スキップ: 残り{self.tts_cooldown_until - current_time:.0f}ms")
                return
            
            # 注意: DTXフィルタはConnection Handlerで既に処理済み
            # ここでは音声処理のみ実行
                
            # 1バイトDTXは追加で500ms制限（二重防御）
            if len(audio_data) == 1:
                if not hasattr(self, 'last_dtx_time'):
                    self.last_dtx_time = 0
                if current_time - self.last_dtx_time < 500:
                    return
                self.last_dtx_time = current_time
                logger.debug(f"[DTX_KEEPALIVE] 1バイトDTX keepalive許可")
                
            # 重複削除: 上記で既にDTXフィルタ済み

            # RMSベース音声検知 (server2準拠)
            is_voice = await self._detect_voice_with_rms(audio_data)
            
            # Server2準拠: wake_guard機能（有音検知時の処理）
            if is_voice:
                self.last_voice_activity_time = current_time
                # wake_guard設定: 有音後一定時間は強制的に発話継続と判定
                self.wake_until = current_time + self.wake_guard_ms
                logger.info(f"🔥 [WAKE_GUARD] 有音検知: current={current_time}, wake_until={self.wake_until}, guard_ms={self.wake_guard_ms}")

            # Server2準拠: TTS中のマイク制御（完全エコー防止）
            if self.client_is_speaking:
                # AI発話中は全音声を完全無視（マイクオフ状態）
                logger.info(f"🔇 [MIC_OFF_AUDIO] AI発話中マイクオフ: {len(audio_data)}B - 全音声破棄（エコー完全防止）")
                return  # 全音声完全破棄
            
            # デバッグ: RMS VAD動作確認
            # logger.info(f"🔍 [VAD_DEBUG] RMS検知結果: voice={is_voice}, audio_size={len(audio_data)}B")  # レート制限対策で削除
            
            # Store audio frame regardless (server2 style)
            self.asr_audio.append(audio_data)
            
            # 詳細フレーム蓄積ログ + DTX統計
            if len(self.asr_audio) % 30 == 0:  # 30フレームごとにログ
                dtx_drop = getattr(self, 'dtx_drop_count', 0)
                cooldown_active = current_time < self.tts_cooldown_until
                logger.info(f"📦 [FRAME_ACCUMULATION] 蓄積フレーム数: {len(self.asr_audio)}, 最新フレーム: {len(audio_data)}B, 音声検知: {is_voice}, DTXドロップ: {dtx_drop}, クールダウン: {cooldown_active}")
            self.asr_audio = self.asr_audio[-100:]  # Keep more frames
            
            # logger.info(f"[AUDIO_TRACE] Frame: {len(audio_data)}B, RMS_voice={is_voice}, frames={len(self.asr_audio)}")  # レート制限対策で削除
            
            if is_voice:
                # 音声検出時にTTS停止フラグもリセット（次のサイクル開始）
                if self.tts_in_progress:
                    logger.info("【TTS後音声検知】次のサイクル開始 - 音声検知再開")
                    self.tts_in_progress = False
                
                # 音声検出
                if not self.client_have_voice:
                    logger.info("【音声開始検出】音声蓄積開始 - 無音検知タイマー開始")
                self.client_have_voice = True
                self.last_voice_activity_time = current_time
                self.voice_frame_count += 1
                self.silence_frame_count = 0
            else:
                # 無音検出
                self.silence_frame_count += 1
                
                # server2準拠: 音声開始後のみ無音検知実行
                if self.client_have_voice:
                    silence_duration = current_time - self.last_voice_activity_time
                    # logger.info(f"【無音継続】{silence_duration:.0f}ms / {self.silence_threshold_ms}ms (有音後)")  # ログ削減
                    
                    # Server2準拠: wake_guard期間中は無音検知をスキップ
                    if current_time < self.wake_until:
                        logger.info(f"🛡️ [WAKE_GUARD] 無音検知スキップ: 残り{self.wake_until - current_time:.0f}ms")
                        return
                    
                    if silence_duration >= self.silence_threshold_ms and len(self.asr_audio) > 5 and not self.is_processing:
                        logger.info(f"🟠XIAOZHI_SILENCE_DETECT🟠 ※ここを送ってver2_SILENCE_DETECT※ 【無音検知完了】{silence_duration:.0f}ms無音 - 音声処理開始 (有音→無音)")
                        logger.info(f"🔍 [SILENCE_DEBUG] threshold={self.silence_threshold_ms}ms, audio_frames={len(self.asr_audio)}, is_processing={self.is_processing}")
                        logger.info(f"🔍 [SILENCE_DEBUG] last_voice_time={self.last_voice_activity_time}, current_time={current_time}")
                        await self._process_voice_stop()
                    elif silence_duration >= self.silence_threshold_ms:
                        # 閾値は超えているが他の条件で処理されない場合
                        logger.warning(f"🟡 [SILENCE_DEBUG] Threshold exceeded but not processing: duration={silence_duration:.0f}ms, audio_frames={len(self.asr_audio)}, is_processing={self.is_processing}")
                    elif silence_duration >= (self.silence_threshold_ms * 0.8):
                        # 閾値80%以上で警告
                        logger.debug(f"⚠️ [SILENCE_DEBUG] Approaching threshold: {silence_duration:.0f}ms / {self.silence_threshold_ms}ms")
                else:
                    # 有音検知前の無音は無視
                    logger.debug(f"[SILENCE_IGNORE] 有音検知前の無音フレーム: {self.silence_frame_count}")

        except Exception as e:
            logger.error(f"Error handling audio frame: {e}")

    async def _process_voice_stop(self):
        """Process accumulated audio when voice stops (server2 style)"""
        try:
            # 新しいリクエストID生成
            rid = str(uuid.uuid4())[:8]
            self.current_request_id = rid
            
            # 🎯 検索可能ログ: handle_voice_stop
            logger.info(f"🔥 RID[{rid}] HANDLE_VOICE_STOP_START: frames={len(self.asr_audio)}, is_processing={self.is_processing}, tts_active={getattr(self.handler, 'tts_active', False)}")
            
            # TTS中は音声処理を完全に無視
            if self.tts_in_progress:
                logger.warning(f"🔥 RID[{rid}] HANDLE_VOICE_STOP_BLOCKED: TTS中のためスキップ")
                return
            
            # Set processing flag at the start
            if self.is_processing:
                logger.warning(f"🔥 RID[{rid}] HANDLE_VOICE_STOP_DUPLICATE: 既に処理中のためスキップ")
                return
                
            self.is_processing = True
            logger.info(f"🔄 [PROCESSING] Starting voice processing")
                
            # Check minimum requirement (調整: 長い発話を確実に処理)
            estimated_pcm_bytes = len(self.asr_audio) * 1920  # Each Opus frame ~1920 PCM bytes
            min_pcm_bytes = 15000  # 調整: 24000から15000に下げて長い発話も処理
            
            logger.info(f"[AUDIO_TRACE] Voice stop: {len(self.asr_audio)} frames, ~{estimated_pcm_bytes} PCM bytes")
            
            if estimated_pcm_bytes < min_pcm_bytes:
                logger.info(f"🔥 RID[{rid}] HANDLE_VOICE_STOP_TOO_SMALL: {estimated_pcm_bytes} < {min_pcm_bytes}, discarding")
                self._reset_audio_state()
                return

            # Process accumulated frames
            audio_frames = self.asr_audio.copy()
            self._reset_audio_state()
            
            logger.info(f"🔥 RID[{rid}] HANDLE_VOICE_STOP_PROCESSING: Converting {len(audio_frames)} frames to WAV")
            
            # フレーム詳細分析
            total_bytes = sum(len(frame) for frame in audio_frames)
            frame_sizes = [len(frame) for frame in audio_frames[:10]]  # 最初の10フレーム
            logger.info(f"🔥 RID[{rid}] FRAME_ANALYSIS: total_bytes={total_bytes}, frame_sizes={frame_sizes}...")
            
            # Convert to WAV using server2 method
            wav_data = await self._opus_frames_to_wav(audio_frames)
            if wav_data:
                # Send to ASR
                logger.info(f"🔥 RID[{rid}] HANDLE_VOICE_STOP_ASR_START: wav_size={len(wav_data)}")
                await self._process_with_asr(wav_data, rid)
            else:
                logger.warning(f"🔥 RID[{rid}] HANDLE_VOICE_STOP_FAILED: WAV変換失敗")

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

    async def _process_with_asr(self, wav_data: bytes, rid: str = None):
        """Process WAV data with ASR"""
        try:
            if not rid:
                rid = str(uuid.uuid4())[:8]
            
            # 🎯 検索可能ログ: ASR処理開始
            logger.info(f"🔥 RID[{rid}] ASR_START: wav_size={len(wav_data)}")
            
            # ASR重複処理防止
            if hasattr(self, '_asr_processing') and self._asr_processing:
                logger.warning(f"🚨 [ASR_DUPLICATE_PREVENT] ASR already in progress, skipping")
                return
                
            self._asr_processing = True
            
            # Create file-like object for ASR
            wav_file = io.BytesIO(wav_data)
            wav_file.name = "audio.wav"
            
            # Call ASR service
            logger.info(f"🔥 RID[{rid}] ASR_WHISPER_START: Calling OpenAI Whisper API")
            transcribed_text = await self.handler.asr_service.transcribe(wav_file)
            logger.info(f"🔥 RID[{rid}] ASR_WHISPER_RESULT: '{transcribed_text}' (len: {len(transcribed_text) if transcribed_text else 0})")
            
            if transcribed_text and transcribed_text.strip():
                logger.info(f"🔥 RID[{rid}] START_TO_CHAT_TRIGGER: Sending '{transcribed_text}' to LLM")
                await self.handler.process_text(transcribed_text, rid)
            else:
                logger.warning(f"🔥 RID[{rid}] ASR_EMPTY: No valid transcription")

        except Exception as e:
            logger.error(f"🔥 RID[{rid}] ASR_ERROR: {e}")
        finally:
            self._asr_processing = False
            
            # TTS中は is_processing を維持（TTS中断防止）
            # WebSocketハンドラのtts_activeとtts_in_progressの両方をチェック
            tts_active = getattr(self.handler, 'tts_active', False) if hasattr(self, 'handler') else False
            
            if not self.tts_in_progress and not tts_active:
                self.is_processing = False
                logger.info(f"🔥 RID[{rid}] ASR_END: Processing complete, is_processing=False")
            else:
                logger.warning(f"🔥 RID[{rid}] ASR_END_TTS_PROTECTION: TTS中のためis_processing維持 (tts_in_progress={self.tts_in_progress}, tts_active={tts_active})")

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
        
        # TTS中は is_processing をリセットしない（TTS中断防止）
        if not self.tts_in_progress:
            self.is_processing = False
            logger.info("[AUDIO_TRACE] Audio state reset (is_processingもリセット)")
        else:
            logger.warning(f"🚨 [IS_PROCESSING_PROTECTION] TTS中のためis_processing維持 (tts_in_progress={self.tts_in_progress})")
            
        # is_processing変更を詳細ログ
        logger.info(f"🔍 [IS_PROCESSING_STATE] After _reset_audio_state: is_processing={self.is_processing}, tts_in_progress={self.tts_in_progress}")
