import os
from typing import Optional
from dotenv import load_dotenv

# 環境変数を読み込み
load_dotenv()

class Config:
    # サーバー設定
    HOST: str = os.getenv("HOST", "0.0.0.0")
    PORT: int = int(os.getenv("PORT", "8080"))
    
    # OpenAI設定
    OPENAI_API_KEY: str = os.getenv("OPENAI_API_KEY", "")
    OPENAI_ASR_MODEL: str = os.getenv("OPENAI_ASR_MODEL", "whisper-1")
    OPENAI_LLM_MODEL: str = os.getenv("OPENAI_LLM_MODEL", "gpt-4o-mini")
    OPENAI_TTS_MODEL: str = os.getenv("OPENAI_TTS_MODEL", "tts-1")
    OPENAI_TTS_VOICE: str = os.getenv("OPENAI_TTS_VOICE", "alloy")
    
    # nekota-server API Configuration (for Memory)
    NEKOTA_API_URL: str = os.getenv("NEKOTA_API_URL", "https://nekota-server-production.up.railway.app")
    NEKOTA_API_SECRET: str = os.getenv("NEKOTA_API_SECRET", "")
    
    # JWT設定
    JWT_SECRET_KEY: str = os.getenv("JWT_SECRET_KEY", "")
    JWT_ALGORITHM: str = "HS256"
    
    # 音声設定
    SAMPLE_RATE: int = 16000
    AUDIO_CHUNK_SIZE: int = 1024
    
    # VAD設定
    VAD_SILENCE_DURATION: float = 1.0  # 無音検出時間（秒）
    
    # ログ設定
    LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")
    
    @classmethod
    def validate(cls) -> None:
        """設定の検証"""
        required_vars = [
            "OPENAI_API_KEY",
            "NEKOTA_API_URL", 
            "NEKOTA_API_SECRET",
            "JWT_SECRET_KEY"
        ]
        
        missing_vars = []
        for var in required_vars:
            if not getattr(cls, var):
                missing_vars.append(var)
        
        if missing_vars:
            raise ValueError(f"Missing required environment variables: {', '.join(missing_vars)}")

# 設定インスタンス
config = Config()
