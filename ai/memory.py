import httpx
from typing import Optional, Dict
from config import Config
from utils.logger import setup_logger

logger = setup_logger()

class MemoryService:
    """manager-api連携メモリー管理"""
    
    def __init__(self):
        self.api_url = Config.MANAGER_API_URL
        self.api_secret = Config.MANAGER_API_SECRET
        self.client = httpx.AsyncClient(
            base_url=self.api_url,
            headers={
                "User-Agent": "XiaozhiESP32Server3/1.0",
                "Accept": "application/json",
                "X-API-Secret": self.api_secret,  # manager-api uses X-API-Secret header
            },
            timeout=30
        )
        logger.info(f"MemoryService initialized with manager-api URL: {self.api_url}")
    
    async def save_memory(self, device_id: str, text: str) -> bool:
        try:
            response = await self.client.put(
                f"/agent/saveMemory/{device_id}",
                json={"summaryMemory": text}
            )
            response.raise_for_status()
            logger.info(f"Memory saved for device {device_id}: {text[:50]}...")
            return True
        except httpx.HTTPStatusError as e:
            logger.error(f"HTTP error saving memory: {e.response.status_code} - {e.response.text}")
            return False
        except Exception as e:
            logger.error(f"Error saving memory: {e}")
            return False
    
    async def query_memory(self, device_id: str, keyword: str) -> Optional[str]:
        """
        Server2方式: manager-apiから該当デバイスのsummaryMemoryを取得
        今のところ検索機能はなく、summaryMemory全体を返すのみ
        """
        try:
            # NOTE: manager-apiには検索エンドポイントがないため、
            # ここではエージェント情報取得を実装する必要がある
            # 一旦、簡易実装として空文字列を返す
            logger.info(f"Memory query requested for device {device_id}, keyword '{keyword}' - not implemented yet")
            return None
        except Exception as e:
            logger.error(f"Error querying memory: {e}")
            return None
