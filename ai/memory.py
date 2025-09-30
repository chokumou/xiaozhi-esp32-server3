import httpx
import jwt
import time
from typing import Optional, Dict
from config import Config
from utils.logger import setup_logger
from .auth_resolver import resolve_auth

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
    
    async def _get_valid_jwt_and_user(self, identifier: str) -> tuple:
        """認証リゾルバを使用してJWTとユーザー情報を取得"""
        try:
            logger.info(f"🔑 [AUTH_RESOLVER_DEBUG] Starting auth resolution for identifier: {identifier}")
            jwt_token, user_id, device_number = await resolve_auth(identifier)
            logger.info(f"🔑 [AUTH_RESOLVER_DEBUG] Auth resolution result:")
            logger.info(f"🔑 [AUTH_RESOLVER_DEBUG] - jwt_token: {jwt_token[:20] if jwt_token else 'None'}...")
            logger.info(f"🔑 [AUTH_RESOLVER_DEBUG] - user_id: {user_id}")
            logger.info(f"🔑 [AUTH_RESOLVER_DEBUG] - device_number: {device_number}")
            
            if jwt_token and user_id:
                logger.info(f"🔑 [AUTH_RESOLVER] Successfully got auth: device={device_number}, user_id={user_id}")
                return jwt_token, user_id
            else:
                logger.error(f"🔑 [AUTH_RESOLVER] Failed to get auth for identifier: {identifier}")
                logger.error(f"🔑 [AUTH_RESOLVER_DEBUG] jwt_token is None: {jwt_token is None}")
                logger.error(f"🔑 [AUTH_RESOLVER_DEBUG] user_id is None: {user_id is None}")
                return None, None
        except Exception as e:
            logger.error(f"🔑 [AUTH_RESOLVER] Error getting auth: {e}")
            import traceback
            logger.error(f"🔑 [AUTH_RESOLVER_DEBUG] Full traceback: {traceback.format_exc()}")
            return None, None
    
    async def save_memory_with_auth(self, jwt_token: str, user_id: str, text: str) -> bool:
        """認証済みJWTとuser_idを使用してメモリを保存"""
        try:
            # デバッグ用の詳細ログ
            logger.info(f"🔑 Using pre-authenticated JWT for user_id: {user_id}")
            logger.info(f"📡 Sending to: {self.api_url}/api/memory/")
            logger.info(f"📦 Payload: {{'text': '{text[:30]}...', 'user_id': '{user_id}'}}")
            
            # Authorizationヘッダーを設定
            headers = {"Authorization": f"Bearer {jwt_token}"}
            
            response = await self.client.post(
                "/api/memory/",
                json={"text": text, "user_id": user_id},
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

    async def save_memory(self, device_id: str, text: str) -> bool:
        try:
            # ESP32のMACベースdevice_idを正しいdevice_numberに変換
            device_number = await self._convert_esp32_device_id_to_device_number(device_id)
            
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
    
    async def query_memory_with_auth(self, jwt_token: str, user_id: str, keyword: str, device_uuid: str = None) -> Optional[str]:
        """認証済みJWTとuser_idを使用してメモリを検索"""
        try:
            # デバッグ用の詳細ログ
            logger.info(f"🔍 Using pre-authenticated JWT for user_id: {user_id}")
            logger.info(f"📡 Querying: {self.api_url}/api/memory/search")
            logger.info(f"🔎 Search keyword: '{keyword}'")
            
            # Authorizationヘッダーを設定
            headers = {"Authorization": f"Bearer {jwt_token}"}
            
            # device_idパラメータにはデバイスUUIDを使用（APIの要求仕様）
            # user_idではなく、実際のdevice_id（UUID）を送信
            if not device_uuid:
                # user_idからdevice_idを逆引きする必要があるが、簡易的にuser_idを使用
                device_uuid = user_id
                
            # AI解析でより高度なキーワード抽出
            search_keywords = await self._extract_search_keywords_ai(keyword)
            if not search_keywords:
                # フォールバック: 従来のキーワード抽出
                search_keywords = []
                if "教えて" in keyword or "覚えてる" in keyword or "知ってる" in keyword:
                    words = keyword.replace("教えて", "").replace("覚えてる", "").replace("知ってる", "").replace("？", "").replace("?", "").replace("の", "").replace("こと", "").replace("について", "").strip()
                    if words:
                        search_keywords.append(words)
                search_keywords.append(keyword)
            
            logger.info(f"🔍 [KEYWORD_EXTRACTION] Extracted keywords: {search_keywords}")
            
            # 最初のキーワードで検索（より広範囲な検索）
            primary_keyword = search_keywords[0] if search_keywords else keyword
            
            response = await self.client.get(
                f"/api/memory/search?keyword={primary_keyword}&device_id={device_uuid}",
                headers=headers
            )
            response.raise_for_status()
            
            data = response.json()
            if data.get("memories"):
                # 取得したメモリーに対して柔軟検索を適用
                memory_texts = [mem.get("text", "") for mem in data.get("memories", [])]
                logger.info(f"🔍 [FLEXIBLE_SEARCH] Applying flexible search to {len(memory_texts)} memories")
                
                # 柔軟検索でフィルタリング
                relevant_memories = self._filter_memories_by_keyword(memory_texts, keyword)
                
                if relevant_memories:
                    combined_memory = " ".join(relevant_memories)
                    logger.info(f"✅ Memory found after flexible search: {combined_memory[:50]}...")
                    return combined_memory
                else:
                    # 柔軟検索でも見つからない場合、全メモリーを返す（従来の動作）
                    combined_memory = " ".join(memory_texts)
                    logger.info(f"✅ No flexible match, returning all memories: {combined_memory[:50]}...")
                    return combined_memory
            else:
                logger.info(f"❌ No memory found for keyword: '{keyword}'")
                return None
        except httpx.HTTPStatusError as e:
            logger.error(f"❌ HTTP error querying memory: {e.response.status_code} - {e.response.text}")
            return None
        except Exception as e:
            logger.error(f"❌ Unexpected error querying memory: {e}")
            return None

    async def query_memory(self, device_id: str, keyword: str) -> Optional[str]:
        """
        nekota-serverからユーザーのメモリーを取得
        """
        try:
            # ESP32のMACベースdevice_idを正しいdevice_numberに変換
            device_number = await self._convert_esp32_device_id_to_device_number(device_id)
            
            # 正規JWTとユーザーIDを取得
            jwt_token, user_id = await self._get_valid_jwt_and_user(device_number)
            
            if not jwt_token or not user_id:
                logger.error(f"❌ 正規JWT取得失敗: device_number={device_number}")
                return None
            
            logger.info(f"🔍 [MEMORY_QUERY] Searching memories for user {user_id}, keyword '{keyword}'")
            
            # nekota-serverのメモリー検索APIを呼び出す
            headers = {"Authorization": f"Bearer {jwt_token}"}
            
            # まずは全メモリーを取得してみる（device_idパラメータ追加）
            response = await self.client.get(f"/api/memory/?user_id={user_id}&device_id={user_id}", headers=headers)
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
    
    async def _extract_search_keywords_ai(self, query: str) -> list:
        """AI APIを使用した高度なキーワード抽出"""
        try:
            import httpx
            import json
            import os
            
            # OpenAI API設定
            api_key = os.getenv("OPENAI_API_KEY")
            if not api_key:
                logger.warning("⚠️ [AI_MEMORY] OpenAI API key not found, using traditional extraction")
                return []
            
            prompt = f"""Extract optimal keywords for memory search from the following query in any language (Japanese, English, Chinese, etc.).
Include related concepts and synonyms to improve search accuracy.

Query: "{query}"

Expected output format (JSON array):
["main keyword", "related keyword1", "related keyword2"]

Examples:
Query: "Tell me about yesterday" → ["yesterday", "previous day", "past", "memory"]
Query: "お尻のことを教えて" → ["お尻", "臀部", "体の症状", "健康"]  
Query: "昨日の話覚えてる？" → ["昨日", "前日", "会話", "記憶"]
Query: "给我说说小明的事" → ["小明", "朋友", "人物", "关系"]

Support multiple languages and cultural contexts."""

            async with httpx.AsyncClient() as client:
                response = await client.post(
                    "https://api.openai.com/v1/chat/completions",
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/json"
                    },
                    json={
                        "model": "gpt-4o-mini",
                        "messages": [{"role": "user", "content": prompt}],
                        "max_tokens": 150,
                        "temperature": 0.1
                    },
                    timeout=10.0
                )
                
                if response.status_code == 200:
                    data = response.json()
                    content = data["choices"][0]["message"]["content"]
                    
                    try:
                        keywords = json.loads(content)
                        if isinstance(keywords, list) and keywords:
                            logger.info(f"✅ [AI_MEMORY] AI キーワード抽出成功: {keywords}")
                            return keywords
                    except json.JSONDecodeError:
                        logger.error(f"❌ [AI_MEMORY] JSON解析失敗: {content}")
                else:
                    logger.error(f"❌ [AI_MEMORY] API呼び出し失敗: {response.status_code}")
                    
        except Exception as e:
            logger.error(f"❌ [AI_MEMORY] AI キーワード抽出エラー: {e}")
        
        return []
