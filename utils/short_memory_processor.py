# utils/short_memory_processor.py

import re
import json
import logging
from typing import List, Dict, Tuple, Optional
from datetime import datetime
try:
    import requests
except ImportError:
    # requestsが利用できない場合はhttpxを使用
    import httpx as requests

logger = logging.getLogger(__name__)

class ShortMemoryProcessor:
    """短期記憶処理クラス"""
    
    def __init__(self, user_id: str):
        self.user_id = user_id
        self.glossary_cache = {}  # セッション中の辞書キャッシュ
        self.stm_chunk = []  # 同一トピック束の一時蓄積
        self.stm_last_topic_repr = ""  # 直近代表文
        self.load_glossary_cache()
    
    def load_glossary_cache(self):
        """セッション開始時に辞書をキャッシュにロード"""
        try:
            # nekota-serverのAPIを呼び出し
            api_url = "https://nekota-server-production.up.railway.app/api/memory"
            headers = {"Authorization": f"Bearer {self.get_jwt_token()}"}
            
            response = requests.get(api_url, headers=headers, timeout=10)
            if response.status_code == 200:
                data = response.json()
                if data and data.get("glossary"):
                    self.glossary_cache = data["glossary"]
            else:
                self.glossary_cache = {}
            logger.info(f"Loaded glossary cache for user {self.user_id}: {len(self.glossary_cache)} terms")
        except Exception as e:
            logger.error(f"Error loading glossary cache: {e}")
            self.glossary_cache = {}
    
    def get_jwt_token(self):
        """JWTトークンを取得（簡略化）"""
        # 実際の実装では認証システムから取得
        return "dummy_token"
    
    def extract_candidate_terms(self, text: str) -> List[str]:
        """候補語を抽出（固有名詞・カタカナ語・記号混じりの珍語・ユーザー口癖）"""
        # 正規表現で候補語を抽出
        pattern = r'([ぁ-んァ-ン一-龥A-Za-z0-9]{2,})'
        matches = re.findall(pattern, text)
        
        # 正規化キーを作成（ひらがな化/小文字化/空白除去）
        normalized_terms = []
        for match in matches:
            normalized = self.normalize_term(match)
            if normalized and len(normalized) >= 2:
                normalized_terms.append(normalized)
        
        return list(set(normalized_terms))  # 重複除去
    
    def normalize_term(self, term: str) -> str:
        """語句を正規化"""
        # ひらがな化、小文字化、空白除去
        term = term.lower().strip()
        # 簡単な正規化（実際の実装ではより詳細な処理が必要）
        return term
    
    def check_glossary_reference(self, terms: List[str]) -> Dict[str, str]:
        """辞書参照（メモリ参照のみ、DB I/Oなし）"""
        found_meanings = {}
        for term in terms:
            if term in self.glossary_cache:
                found_meanings[term] = self.glossary_cache[term]
        return found_meanings
    
    def detect_topic_boundary(self, current_text: str) -> bool:
        """話題境界判定（AI不使用）"""
        # 初回発話の場合は必ず境界とする
        if not self.stm_last_topic_repr:
            logger.info(f"Topic boundary detected: first utterance")
            return True
        
        # ノイズ合流チェック（短文は境界にしない）
        if current_text.strip() in ["はい", "了解", "うん", "そう", "なるほど"]:
            logger.info(f"Topic boundary not detected: noise response")
            return False
        
        # 類似度閾値チェック
        similarity = self.calculate_jaccard_similarity(
            self.stm_last_topic_repr, current_text
        )
        if similarity < 0.5:  # 閾値を下げて境界を検出しやすくする
            logger.info(f"Topic boundary detected by similarity: {similarity}")
            return True
        
        # 新規固有語の出現チェック
        current_terms = self.extract_candidate_terms(current_text)
        last_terms = self.extract_candidate_terms(self.stm_last_topic_repr)
        new_terms = set(current_terms) - set(last_terms)
        if len(new_terms) >= 1:  # 閾値を下げる
            logger.info(f"Topic boundary detected by new terms: {new_terms}")
            return True
        
        # 会話の長さチェック（stm_chunkが一定数に達したら境界）
        if len(self.stm_chunk) >= 3:
            logger.info(f"Topic boundary detected: chunk length reached {len(self.stm_chunk)}")
            return True
        
        logger.info(f"No topic boundary detected: similarity={similarity}, new_terms={len(new_terms)}, chunk_length={len(self.stm_chunk)}")
        return False
    
    def calculate_jaccard_similarity(self, text1: str, text2: str) -> float:
        """Jaccard類似度計算（正規化キーワード集合）"""
        terms1 = set(self.extract_candidate_terms(text1))
        terms2 = set(self.extract_candidate_terms(text2))
        
        if not terms1 and not terms2:
            return 1.0
        if not terms1 or not terms2:
            return 0.0
        
        intersection = len(terms1 & terms2)
        union = len(terms1 | terms2)
        
        return intersection / union if union > 0 else 0.0
    
    def process_conversation_turn(self, text: str) -> Dict[str, any]:
        """
        会話ターン処理（ASR→テキスト確定時点でフック）
        戻り値: {
            "is_boundary": bool,
            "glossary_updates": dict,
            "new_entry": str or None
        }
        """
        logger.info(f"🧠 [SHORT_MEMORY] Processing conversation turn: '{text}'")
        
        # 候補語抽出
        candidate_terms = self.extract_candidate_terms(text)
        logger.info(f"🧠 [SHORT_MEMORY] Extracted candidate terms: {candidate_terms}")
        
        # 辞書参照
        found_meanings = self.check_glossary_reference(candidate_terms)
        if found_meanings:
            logger.info(f"🧠 [SHORT_MEMORY] Found glossary meanings: {found_meanings}")
        
        # 辞書登録ヒューリスティック
        glossary_updates = self.apply_registration_heuristics(text, candidate_terms)
        
        # 話題境界判定
        is_boundary = self.detect_topic_boundary(text)
        logger.info(f"🧠 [SHORT_MEMORY] Topic boundary result: {is_boundary}")
        
        # 境界でない場合：stm_chunkに追加
        if not is_boundary:
            self.stm_chunk.append(text)
            logger.info(f"🧠 [SHORT_MEMORY] Added to chunk. Current chunk length: {len(self.stm_chunk)}")
            return {
                "is_boundary": False,
                "glossary_updates": glossary_updates,
                "new_entry": None
            }
        
        # 境界の場合：要約して記録
        if self.stm_chunk:
            self.stm_chunk.append(text)
            summary = self.generate_one_sentence_diary(self.stm_chunk)
            logger.info(f"🧠 [SHORT_MEMORY] Generated summary: '{summary}'")
            
            # データベースに保存
            self.save_memory_entry(summary)
            
            # 状態をリセット
            self.stm_chunk = []
            self.stm_last_topic_repr = text
            
            return {
                "is_boundary": True,
                "glossary_updates": glossary_updates,
                "new_entry": summary
            }
        
        logger.info(f"🧠 [SHORT_MEMORY] No chunk to process")
        return {
            "is_boundary": False,
            "glossary_updates": glossary_updates,
            "new_entry": None
        }
    
    def apply_registration_heuristics(self, text: str, terms: List[str]) -> Dict[str, str]:
        """辞書登録ヒューリスティック"""
        updates = {}
        
        # 定義文パターン
        definition_patterns = [
            r'(.+?)とは(.+?)のことです',
            r'(.+?)は(.+?)のことです',
            r'(.+?)って(.+?)のことです',
            r'(.+?)とは(.+?)です'
        ]
        
        for pattern in definition_patterns:
            match = re.search(pattern, text)
            if match:
                term = match.group(1).strip()
                meaning = match.group(2).strip()
                if len(term) <= 10 and len(meaning) <= 50:
                    updates[term] = meaning
                    logger.info(f"🧠 [SHORT_MEMORY] Detected definition: {term} = {meaning}")
        
        return updates
    
    def generate_one_sentence_diary(self, chunk: List[str]) -> str:
        """1文日記の生成（境界時だけLLM呼び出し）"""
        # 簡略化された実装
        # 実際にはOpenAI APIを呼び出して要約を生成
        
        # 簡易実装：会話束を結合して120字以内に要約
        combined_text = " ".join(chunk)
        summary = combined_text[:120] + ("..." if len(combined_text) > 120 else "")
        
        # サニタイズ：改行/多絵文字除去、全角記号統一、末尾「。」付与
        summary = re.sub(r'\n+', '', summary)
        summary = re.sub(r'[。！？]+', '。', summary)
        if not summary.endswith('。'):
            summary += '。'
        
        return summary
    
    def save_memory_entry(self, sentence: str):
        """記憶エントリをデータベースに保存"""
        try:
            # nekota-serverのAPIを呼び出し
            api_url = "https://nekota-server-production.up.railway.app/api/memory/append"
            headers = {
                "Authorization": f"Bearer {self.get_jwt_token()}",
                "Content-Type": "application/json"
            }
            data = {"sentence": sentence}
            
            response = requests.post(api_url, json=data, headers=headers, timeout=10)
            
            if response.status_code == 200:
                logger.info(f"Saved memory entry for user {self.user_id}: {sentence}")
            else:
                logger.error(f"Failed to save memory entry: {response.status_code}")
            
        except Exception as e:
            logger.error(f"Error saving memory entry: {e}")
    
    def get_context_for_prompt(self) -> str:
        """プロンプト用のコンテキストを取得"""
        try:
            # nekota-serverのAPIを呼び出し
            api_url = "https://nekota-server-production.up.railway.app/api/memory"
            headers = {"Authorization": f"Bearer {self.get_jwt_token()}"}
            
            response = requests.get(api_url, headers=headers, timeout=10)
            
            if response.status_code == 200:
                data = response.json()
                if data and data.get("memory_text"):
                    memory_text = data["memory_text"]
                    # 最後の300字を返す
                    return memory_text[-300:] if len(memory_text) > 300 else memory_text
            
            return ""
            
        except Exception as e:
            logger.error(f"Error getting context for prompt: {e}")
            return ""
    
    def get_glossary_for_prompt(self, recent_terms: List[str]) -> str:
        """プロンプト用の辞書情報を取得（必要語のみ、最大200字）"""
        glossary_lines = []
        total_length = 0
        
        for term in recent_terms:
            if term in self.glossary_cache:
                meaning = self.glossary_cache[term]
                line = f"{term}: {meaning}"
                
                if total_length + len(line) > 200:
                    break
                
                glossary_lines.append(line)
                total_length += len(line) + 1  # +1 for newline
        
        return "\n".join(glossary_lines)
