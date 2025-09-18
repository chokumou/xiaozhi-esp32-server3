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
            # TODO: 動的にデバイス番号を取得する仕組みが必要
            # 暫定的に固定値を使用（後で修正が必要）
            device_number = "327546"
            
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
            # TODO: 動的にデバイス番号を取得する仕組みが必要
            # 暫定的に固定値を使用（後で修正が必要）
            device_number = "327546"
            
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
            
            # レスポンス形式をデバッグ
            logger.info(f"🔍 [MEMORY_DEBUG] Response type: {type(memories_data)}")
            logger.info(f"🔍 [MEMORY_DEBUG] Response content: {memories_data}")
            
            # メモリーを結合して返す（レスポンス形式に応じた処理）
            memory_texts = []
            
            if isinstance(memories_data, dict) and 'memories' in memories_data:
                # nekota-server形式: {'memories': [...], 'total': 4, 'page': 1, 'limit': 10}
                memories_list = memories_data['memories']
                for memory in memories_list:
                    if isinstance(memory, dict):
                        text = memory.get("text", "")
                        if text:
                            memory_texts.append(text)
            elif isinstance(memories_data, list):
                # リスト形式の場合
                for memory in memories_data:
                    if isinstance(memory, dict):
                        text = memory.get("text", "")
                        if text:
                            memory_texts.append(text)
                    elif isinstance(memory, str):
                        memory_texts.append(memory)
            elif isinstance(memories_data, str):
                # 文字列形式の場合
                memory_texts = [memories_data]
            
            if memory_texts:
                # キーワード検索で関連するメモリをフィルタリング
                relevant_memories = self._filter_memories_by_keyword(memory_texts, keyword)
                
                if relevant_memories:
                    combined_memory = "君について覚えていることはこれだよ: " + "、".join(relevant_memories)
                    logger.info(f"🧠 [MEMORY_QUERY] Found relevant memories: {combined_memory[:100]}...")
                    return combined_memory
                else:
                    # 関連するメモリがない場合は全メモリを返す
                    combined_memory = "君について覚えていることはこれだよ: " + "、".join(memory_texts)
                    logger.info(f"🧠 [MEMORY_QUERY] No specific match, returning all memories: {combined_memory[:100]}...")
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

    def _normalize_japanese_text(self, text: str) -> list:
        """日本語テキストを正規化（ひらがな・カタカナ・漢字変換）"""
        import unicodedata
        
        normalized_variants = [text.lower()]
        
        # ひらがな→カタカナ変換
        hiragana_to_katakana = ""
        for char in text:
            if 'ひ' <= char <= 'ゖ':  # ひらがな範囲
                hiragana_to_katakana += chr(ord(char) + 0x60)
            else:
                hiragana_to_katakana += char
        if hiragana_to_katakana != text:
            normalized_variants.append(hiragana_to_katakana.lower())
        
        # カタカナ→ひらがな変換
        katakana_to_hiragana = ""
        for char in text:
            if 'ア' <= char <= 'ヶ':  # カタカナ範囲
                katakana_to_hiragana += chr(ord(char) - 0x60)
            else:
                katakana_to_hiragana += char
        if katakana_to_hiragana != text:
            normalized_variants.append(katakana_to_hiragana.lower())
        
        # 全角→半角変換
        half_width = unicodedata.normalize('NFKC', text).lower()
        if half_width != text.lower():
            normalized_variants.append(half_width)
        
        return list(set(normalized_variants))  # 重複除去

    def _calculate_similarity(self, str1: str, str2: str) -> float:
        """文字列の類似度を計算（日本語対応改良版）"""
        if not str1 or not str2:
            return 0.0
        
        # 正規化バリアントを生成
        str1_variants = self._normalize_japanese_text(str1)
        str2_variants = self._normalize_japanese_text(str2)
        
        max_similarity = 0.0
        
        # 全組み合わせで最高類似度を計算
        for v1 in str1_variants:
            for v2 in str2_variants:
                # 完全一致
                if v1 == v2:
                    return 1.0
                
                # 部分一致（含まれる関係）
                if v1 in v2 or v2 in v1:
                    max_similarity = max(max_similarity, 0.8)
                    continue
                
                # 共通文字数を計算
                len1, len2 = len(v1), len(v2)
                common = 0
                v2_chars = list(v2)
                
                for char in v1:
                    if char in v2_chars:
                        v2_chars.remove(char)  # 重複カウントを防ぐ
                        common += 1
                
                # ジャッカード係数的な計算
                union_size = len1 + len2 - common
                if union_size > 0:
                    similarity = common / union_size
                    max_similarity = max(max_similarity, similarity)
        
        return max_similarity

    def _filter_memories_by_keyword(self, memory_texts: list, keyword: str) -> list:
        """キーワードに関連するメモリをフィルタリング"""
        if not keyword or not memory_texts:
            return memory_texts
        
        logger.info(f"🔍 [MEMORY_FILTER] Filtering {len(memory_texts)} memories with keyword: '{keyword}'")
        
        relevant_memories = []
        
        for memory in memory_texts:
            # 直接的な含有チェック
            if keyword.lower() in memory.lower():
                relevant_memories.append(memory)
                logger.info(f"🎯 [MEMORY_MATCH] Direct match: '{memory[:50]}...'")
                continue
            
            # 日本語正規化による類似度チェック
            similarity = self._calculate_similarity(keyword, memory)
            logger.info(f"🔍 [MEMORY_SIMILARITY] '{keyword}' vs '{memory[:30]}...': {similarity}")
            
            if similarity > 0.3:  # 類似度閾値
                relevant_memories.append(memory)
                logger.info(f"🎯 [MEMORY_MATCH] Similarity match: '{memory[:50]}...'")
        
        logger.info(f"🔍 [MEMORY_FILTER] Found {len(relevant_memories)} relevant memories")
        return relevant_memories
