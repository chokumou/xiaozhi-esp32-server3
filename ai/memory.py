import httpx
from typing import Optional, Dict
from config import Config
from utils.logger import setup_logger

logger = setup_logger()

class MemoryService:
    """nekota-server連携メモリー管理"""
    
    def __init__(self):
        self.api_url = Config.NEKOTA_API_URL
        self.api_secret = Config.NEKOTA_API_SECRET
        self.client = httpx.AsyncClient(
            base_url=self.api_url,
            headers={
                "User-Agent": "XiaozhiESP32Server3/1.0",
                "Accept": "application/json",
                "Authorization": f"Bearer {self.api_secret}",
            },
            timeout=30
        )
        logger.info(f"MemoryService initialized with nekota-server URL: {self.api_url}")
    
    async def save_memory(self, device_id: str, text: str) -> bool:
        try:
            response = await self.client.post(
                "/api/memory/",
                json={"user_id": device_id, "text": text}
            )
            response.raise_for_status()
            result = response.json()
            if result.get("status") == "ok":
                logger.info(f"Memory saved for device {device_id}: {text[:50]}...")
                return True
            else:
                logger.error(f"Failed to save memory: {result.get('msg', 'Unknown error')}")
                return False
        except httpx.HTTPStatusError as e:
            logger.error(f"HTTP error saving memory: {e.response.status_code} - {e.response.text}")
            return False
        except Exception as e:
            logger.error(f"Error saving memory: {e}")
            return False
    
    async def query_memory(self, device_id: str, keyword: str) -> Optional[str]:
        try:
            response = await self.client.get(
                "/api/memory/search",
                params={"device_id": device_id, "keyword": keyword}
            )
            response.raise_for_status()
            result = response.json()
            if result and isinstance(result, list) and len(result) > 0:
                # Assuming the API returns a list of memory objects, we'll just return the first one's text
                memory_text = result[0].get("text", "")
                logger.info(f"Memory queried for device {device_id}, keyword '{keyword}': {memory_text[:50]}...")
                return memory_text
            else:
                logger.info(f"No memory found for device {device_id}, keyword '{keyword}'")
                return None
        except httpx.HTTPStatusError as e:
            logger.error(f"HTTP error querying memory: {e.response.status_code} - {e.response.text}")
            return None
        except Exception as e:
            logger.error(f"Error querying memory: {e}")
            return None
