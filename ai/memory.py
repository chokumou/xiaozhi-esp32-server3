import httpx
from typing import Optional, List, Dict
from config import config
from utils.logger import log

class MemoryManager:
    """nekota-server連携メモリー管理"""
    
    def __init__(self):
        self.base_url = config.NEKOTA_SERVER_URL
        self.secret = config.NEKOTA_SERVER_SECRET
        self.headers = {
            "Authorization": f"Bearer {self.secret}",
            "Content-Type": "application/json",
            "User-Agent": "xiaozhi-server3/1.0"
        }
    
    async def save_memory(self, device_id: str, text: str) -> bool:
        """記憶をnekota-serverに保存"""
        try:
            log.debug(f"Saving memory for device {device_id}: '{text[:50]}...'")
            
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    f"{self.base_url}/api/memory/",
                    headers=self.headers,
                    json={
                        "user_id": device_id,
                        "text": text
                    },
                    timeout=10.0
                )
                
                if response.status_code == 200:
                    log.info(f"Memory saved successfully for device {device_id}")
                    return True
                else:
                    log.error(f"Memory save failed: {response.status_code} - {response.text}")
                    return False
                    
        except Exception as e:
            log.error(f"Memory save error: {e}")
            return False
    
    async def search_memory(self, device_id: str, keyword: str) -> Optional[str]:
        """記憶をnekota-serverから検索"""
        try:
            log.debug(f"Searching memory for device {device_id}, keyword: '{keyword}'")
            
            async with httpx.AsyncClient() as client:
                response = await client.get(
                    f"{self.base_url}/api/memory/search",
                    headers=self.headers,
                    params={
                        "device_id": device_id,
                        "keyword": keyword
                    },
                    timeout=10.0
                )
                
                if response.status_code == 200:
                    data = response.json()
                    if data and isinstance(data, list) and len(data) > 0:
                        # 検索結果を文字列にまとめる
                        memories = []
                        for memory in data:
                            if isinstance(memory, dict) and "text" in memory:
                                memories.append(memory["text"])
                            elif isinstance(memory, str):
                                memories.append(memory)
                        
                        if memories:
                            result = "\n".join(memories)
                            log.info(f"Memory found for device {device_id}: {len(memories)} entries")
                            return result
                    
                    log.info(f"No memory found for device {device_id}")
                    return None
                else:
                    log.error(f"Memory search failed: {response.status_code} - {response.text}")
                    return None
                    
        except Exception as e:
            log.error(f"Memory search error: {e}")
            return None
    
    async def get_recent_memories(self, device_id: str, limit: int = 5) -> Optional[List[str]]:
        """最近の記憶を取得"""
        try:
            log.debug(f"Getting recent memories for device {device_id}")
            
            async with httpx.AsyncClient() as client:
                response = await client.get(
                    f"{self.base_url}/api/memory/recent",
                    headers=self.headers,
                    params={
                        "device_id": device_id,
                        "limit": limit
                    },
                    timeout=10.0
                )
                
                if response.status_code == 200:
                    data = response.json()
                    if data and isinstance(data, list):
                        memories = []
                        for memory in data:
                            if isinstance(memory, dict) and "text" in memory:
                                memories.append(memory["text"])
                            elif isinstance(memory, str):
                                memories.append(memory)
                        
                        log.info(f"Retrieved {len(memories)} recent memories for device {device_id}")
                        return memories
                    
                    return []
                else:
                    log.error(f"Recent memories fetch failed: {response.status_code} - {response.text}")
                    return None
                    
        except Exception as e:
            log.error(f"Recent memories fetch error: {e}")
            return None
