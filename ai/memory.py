import httpx
import jwt
import time
from typing import Optional, Dict
from config import Config
from utils.logger import setup_logger

logger = setup_logger()

class MemoryService:
    """nekota-server連携メモリー管理"""
    
    def __init__(self):
        self.api_url = Config.MANAGER_API_URL
        self.api_secret = Config.MANAGER_API_SECRET
        self.jwt_secret = Config.JWT_SECRET_KEY
        self.client = httpx.AsyncClient(
            base_url=self.api_url,
            headers={
                "User-Agent": "XiaozhiESP32Server3/1.0",
                "Accept": "application/json",
            },
            timeout=30
        )
        logger.info(f"MemoryService initialized with nekota-server URL: {self.api_url}")
    
    async def _get_valid_jwt_and_user(self, device_number: str) -> tuple:
        """nekota-serverから正規JWTとユーザー情報を取得"""
        try:
            response = await self.client.post("/api/device/exists",
                                            json={"device_number": device_number})
            if response.status_code == 200:
                data = response.json()
                jwt_token = data.get("token")
                user_data = data.get("user")
                user_id = user_data.get("id") if user_data else None
                logger.info(f"🔑 正規JWT取得成功: user_id={user_id}")
                return jwt_token, user_id
        except Exception as e:
            logger.error(f"❌ 正規JWT取得失敗: {e}")
        return None, None
    
    async def save_memory(self, device_id: str, text: str) -> bool:
        try:
            # MACアドレスからデバイス番号に変換（一時的なハードコード）
            # TODO: 動的にデバイス番号を取得する仕組みを実装
            device_number = "327546"  # 登録済みデバイス番号
            
            # 正規JWTとユーザーIDを取得
            jwt_token, user_id = await self._get_valid_jwt_and_user(device_number)
            
            if not jwt_token or not user_id:
                logger.error(f"❌ 正規JWT取得失敗: device_number={device_number}")
                return False
            
            # デバッグ用の詳細ログ
            logger.info(f"🔑 Using valid JWT for user_id: {user_id}")
            logger.info(f"📡 Sending to: {self.api_url}/api/memory/")
            logger.info(f"📦 Payload: {{'text': '{text[:30]}...', 'user_id': '{user_id}'}}")
            
            # Authorizationヘッダーを設定
            headers = {"Authorization": f"Bearer {jwt_token}"}
            
            response = await self.client.post(
                "/api/memory/",
                json={"text": text, "user_id": user_id},  # 正しいuser_idを使用
                headers=headers
            )
            response.raise_for_status()
            logger.info(f"✅ Memory saved for user {user_id}: {text[:50]}...")
            return True
        except httpx.HTTPStatusError as e:
            logger.error(f"❌ HTTP error saving memory: {e.response.status_code} - {e.response.text}")
            return False
        except Exception as e:
            logger.error(f"❌ Unexpected error saving memory: {e}")
            return False
    
    async def query_memory(self, device_id: str, keyword: str) -> Optional[str]:
        """
        nekota-serverからユーザーのメモリーを取得
        """
        try:
            # MACアドレスからデバイス番号に変換（一時的なハードコード）
            device_number = "327546"  # 登録済みデバイス番号
            
            # 正規JWTとユーザーIDを取得
            jwt_token, user_id = await self._get_valid_jwt_and_user(device_number)
            
            if not jwt_token or not user_id:
                logger.error(f"❌ 正規JWT取得失敗: device_number={device_number}")
                return None
            
            logger.info(f"🔍 [MEMORY_QUERY] Searching memories for user {user_id}, keyword '{keyword}'")
            
            # nekota-serverのメモリー検索APIを呼び出す
            headers = {"Authorization": f"Bearer {jwt_token}"}
            
            # まずは全メモリーを取得してみる
            response = await self.client.get(f"/api/memory/?user_id={user_id}", headers=headers)
            response.raise_for_status()
            
            memories_data = response.json()
            logger.info(f"🧠 [MEMORY_QUERY] Retrieved {len(memories_data)} memories")
            
            if not memories_data:
                logger.info(f"🧠 [MEMORY_QUERY] No memories found for user {user_id}")
                return None
            
            # メモリーを結合して返す（簡易実装）
            memory_texts = [memory.get("text", "") for memory in memories_data if memory.get("text")]
            if memory_texts:
                combined_memory = "君について覚えていることはこれだよ: " + "、".join(memory_texts)
                logger.info(f"🧠 [MEMORY_QUERY] Found memories: {combined_memory[:100]}...")
                return combined_memory
            else:
                logger.info(f"🧠 [MEMORY_QUERY] No memory text found")
                return None
                
        except httpx.HTTPStatusError as e:
            logger.error(f"❌ HTTP error querying memory: {e.response.status_code} - {e.response.text}")
            return None
        except Exception as e:
            logger.error(f"❌ Unexpected error querying memory: {e}")
            return None
