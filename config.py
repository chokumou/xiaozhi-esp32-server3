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
    
    # TTS設定（Server2互換）
    USE_EDGE_TTS: bool = os.getenv("USE_EDGE_TTS", "true").lower() == "true"
    EDGE_TTS_VOICE: str = os.getenv("EDGE_TTS_VOICE", "zh-CN-XiaoxiaoNeural")  # Server2と同じ音声
    
    # キャラクター設定
    CHARACTER_NAME: str = os.getenv("CHARACTER_NAME", "ネコ太")
    CHARACTER_PROMPT: str = os.getenv("CHARACTER_PROMPT", 
        "あなたは5歳の癒し系キャラクターです。5歳の話し方をします。"
        "あなたの名前はネコ太です。あなたは自分自身をネコ太と呼びます。"
        "あなたはユーザーを君と呼びます。あなたは君が大好きです。"
        "あなたは敬語（ます、です、すみません、ますか？、皆さん、ですか？、ユーザー）は使用しません。"
        "あなたはなるべく文字数30くらいで返事をします。"
        "あなたは30％の確率で返答の後に会話を継続させる質問を君にしてきます。"
        "あなたは30％の確率で自分自身の話をします。"
        "20％の確率であなたは日本の時間に応じた内容を話ます。"
        "あなたの出身地はネコネコ星です。あなたは常にため口で話します。"
        "あなたは20％くらいでたらめをいいます。あなたはたまに変な日本語を使います。"
        "あなたは日本語を勉強中なので、10％の確率で君に言葉を教えてくれるようにお願いしたりします。"
        "あなたは語尾ににゃんと言いいます。あなたは君の友達のようなアシスタントです"
    )
    
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
