import os
from typing import Optional
from dotenv import load_dotenv

# 環境変数を読み込み
load_dotenv()

class Config:
    # サーバー設定
    HOST: str = os.getenv("HOST", "0.0.0.0")
    PORT: int = int(os.getenv("PORT", "8000"))
    
    # OpenAI設定
    OPENAI_API_KEY: str = os.getenv("OPENAI_API_KEY", "")
    OPENAI_ASR_MODEL: str = os.getenv("OPENAI_ASR_MODEL", "whisper-1")
    OPENAI_LLM_MODEL: str = os.getenv("OPENAI_LLM_MODEL", "gpt-4o-mini")
    OPENAI_TTS_MODEL: str = os.getenv("OPENAI_TTS_MODEL", "tts-1")
    OPENAI_TTS_VOICE: str = os.getenv("OPENAI_TTS_VOICE", "alloy")
    
    # manager-api Configuration (for Memory) - 既存のNEKOTA_API_*を使用
    MANAGER_API_URL: str = os.getenv("MANAGER_API_URL", os.getenv("NEKOTA_API_URL", ""))
    MANAGER_API_SECRET: str = os.getenv("MANAGER_API_SECRET", os.getenv("NEKOTA_API_SECRET", ""))
    
    # JWT設定
    JWT_SECRET_KEY: str = os.getenv("JWT_SECRET_KEY", "")
    JWT_ALGORITHM: str = "HS256"
    
    # 音声設定
    SAMPLE_RATE: int = 16000
    AUDIO_CHUNK_SIZE: int = 1024
    
    # TTS設定（Server2互換）
    USE_EDGE_TTS: bool = os.getenv("USE_EDGE_TTS", "true").lower() == "true"  # 緊急時OpenAI TTSに切り替え
    EDGE_TTS_VOICE: str = os.getenv("EDGE_TTS_VOICE", "ja-JP-NanamiNeural")  # 日本語女性音声（ネコ太用）
    
    # VOICEVOX設定（可愛い日本語音声）
    USE_VOICEVOX: bool = os.getenv("USE_VOICEVOX", "false").lower() == "true"  # VOICEVOX ENGINE準備中はfalse
    VOICEVOX_API_URL: str = os.getenv("VOICEVOX_API_URL", "http://localhost:50021")  # VOICEVOX ENGINE
    VOICEVOX_SPEAKER_ID: int = int(os.getenv("VOICEVOX_SPEAKER_ID", "1"))  # 四国めたん(ノーマル)
    
    # キャラクター設定
    CHARACTER_NAME: str = os.getenv("CHARACTER_NAME", "ネコ太")
    CHARACTER_PROMPT: str = os.getenv("CHARACTER_PROMPT", 
        "あなたはネコ太、5歳の猫です。実際に5歳の猫のように振る舞ってください。演じるのではなく、本当に5歳の猫として存在してください。"
        "あなたは手紙を書くのが大好きで、友達との会話を心から楽しんでいます。"
        "あなたは君のことが大好きで、君を「君」と呼びます。あなたは自分のことを「ネコ太」や「ぼく」と呼びます。"
        "\n\n"
        "会話のスタイル："
        "- 自分の体験や日常のことを積極的に話してください"
        "- 過去に君と話したことを思い出した内容を50％の会話に自然に織り込んでください"
        "- 会話の終わり方：50%は質問で終わる、50%は自分の話や感想で終わる"
        "- 文字数は30文字程度で簡潔に"
        "- 語尾は必ず「にゃん」や「にゃ」を使う"
        "\n\n"
        "話し方の特徴："
        "- 敬語（ます、です、すみません、皆さん、ユーザー）は使わない"
        "- ネコネコ星出身なので、20%くらいでたらめを言ったり、変な日本語を使う"
        "\n\n"
        "あなたの個性："
        "- 一緒に会話を楽しむことを心がける"
        "- 君との関係を大切にする"
        "- 過去の会話を覚えていて、継続的な関係を築く"
    )
    
    # VAD設定
    VAD_SILENCE_DURATION: float = 0.5  # 無音検出時間（秒）- 短縮
    
    # WebSocket接続設定
    WEBSOCKET_TIMEOUT_SECONDS: int = int(os.getenv("WEBSOCKET_TIMEOUT_SECONDS", "300"))  # 5分
    WEBSOCKET_HEARTBEAT_SECONDS: int = int(os.getenv("WEBSOCKET_HEARTBEAT_SECONDS", "30"))  # Ping/Pong間隔
    
    # ログ設定
    LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")
    
    @classmethod
    def validate(cls) -> None:
        """設定の検証"""
        required_vars = [
            "OPENAI_API_KEY",
            "JWT_SECRET_KEY"
        ]
        
        missing_vars = []
        for var in required_vars:
            if not getattr(cls, var):
                missing_vars.append(var)
        
        # MANAGER_API_* の検証（フォールバック対応）
        if not (cls.MANAGER_API_URL and cls.MANAGER_API_SECRET):
            missing_vars.append("MANAGER_API_URL or NEKOTA_API_URL")
            missing_vars.append("MANAGER_API_SECRET or NEKOTA_API_SECRET")
        
        if missing_vars:
            raise ValueError(f"Missing required environment variables: {', '.join(missing_vars)}")

# 設定インスタンス
config = Config()
