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
    
    def _generate_device_jwt(self, device_id: str) -> str:
        """デバイス用のJWTトークンを生成（nekota-server互換）"""
        payload = {
            "sub": device_id,  # nekota-serverはsubフィールドを期待
            "user_id": device_id,  # 互換性のため両方設定
            "device_id": device_id,
            "iat": int(time.time()),
            "exp": int(time.time()) + 3600,  # 1時間有効
        }
        return jwt.encode(payload, self.jwt_secret, algorithm="HS256")
    
    async def save_memory(self, device_id: str, text: str) -> bool:
        try:
            # デバイス用のJWTトークンを生成
            jwt_token = self._generate_device_jwt(device_id)
            
            # デバッグ用の詳細ログ
            logger.info(f"🔑 Generated JWT token for device {device_id}: {jwt_token[:50]}...")
            logger.info(f"📡 Sending to: {self.api_url}/api/memory/")
            logger.info(f"📦 Payload: {{'text': '{text[:30]}...', 'user_id': '{device_id}'}}")
            
            # Authorizationヘッダーを設定
            headers = {"Authorization": f"Bearer {jwt_token}"}
            
            response = await self.client.post(
                "/api/memory/",
                json={"text": text, "user_id": device_id},
                headers=headers
            )
            response.raise_for_status()
            logger.info(f"✅ Memory saved for device {device_id}: {text[:50]}...")
            return True
        except httpx.HTTPStatusError as e:
            logger.error(f"❌ HTTP error saving memory: {e.response.status_code} - {e.response.text}")
            logger.error(f"🔍 Request URL: {self.api_url}/api/memory/")
            logger.error(f"🔍 Request headers: Authorization: Bearer {jwt_token[:30]}...")
            logger.error(f"🔍 Request body: {{'text': '{text[:30]}...', 'user_id': '{device_id}'}}")
            return False
        except Exception as e:
            logger.error(f"❌ Unexpected error saving memory: {e}")
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
