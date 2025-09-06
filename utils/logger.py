import sys
from loguru import logger
from config import Config

def setup_logger():
    """ログ設定をセットアップ"""
    # デフォルトハンドラーを削除
    logger.remove()
    
    # コンソール出力設定
    logger.add(
        sys.stdout,
        level=Config.LOG_LEVEL,
        format="<green>{time:YYMMDD HH:mm:ss}</green> | <level>{level:8}</level> | <cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - <level>{message}</level>",
        colorize=True
    )
    
    # ファイル出力設定
    logger.add(
        "logs/xiaozhi-server3.log",
        level="DEBUG",
        format="{time:YYYY-MM-DD HH:mm:ss} | {level:8} | {name}:{function}:{line} - {message}",
        rotation="1 day",
        retention="7 days",
        encoding="utf-8"
    )
    
    return logger

# ログインスタンス
log = setup_logger()
