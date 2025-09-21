"""
自然言語処理ユーティリティ
メモリー検索の柔軟性機能をアラーム設定・メッセージ送信に適用
"""
import re
import datetime
import pytz
from typing import Optional, Dict, List, Tuple
import logging

logger = logging.getLogger(__name__)

class FlexibleTextParser:
    """柔軟なテキスト解析クラス"""
    
    def __init__(self):
        self.logger = logging.getLogger(__name__)
    
    def extract_keywords(self, text: str) -> List[str]:
        """
        メモリー検索で実装された柔軟なキーワード抽出機能
        「お尻のことを教えて」→「お尻」を抽出
        """
        search_keywords = []
        
        # 質問形式の場合、名詞を抽出
        if any(word in text for word in ["教えて", "覚えてる", "知ってる", "について", "のこと"]):
            words = text
            for remove_word in ["教えて", "覚えてる", "知ってる", "？", "?", "の", "こと", "について"]:
                words = words.replace(remove_word, "")
            words = words.strip()
            if words:
                search_keywords.append(words)
        
        # 元のテキストも追加
        search_keywords.append(text)
        
        self.logger.info(f"🔍 [KEYWORD_EXTRACTION] Extracted keywords from '{text}': {search_keywords}")
        return search_keywords
    
    def _normalize_japanese_text(self, text: str) -> List[str]:
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
        
        # AI解析を使用するため、基本的な正規化のみ実行
        # 詳細な読み方パターンはAIに任せる
        
        return list(set(normalized_variants))  # 重複除去
    
    def calculate_similarity(self, str1: str, str2: str) -> float:
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

class AlarmTextParser(FlexibleTextParser):
    """アラーム設定用の自然言語処理クラス"""
    
    def parse_alarm_text(self, text: str, timezone: str = "Asia/Tokyo") -> Optional[Dict]:
        """
        自然言語からアラーム設定を解析
        「明日の朝7時にお薬を飲むことを思い出させて」→ {date: "2025-09-21", time: "07:00", message: "お薬を飲む"}
        """
        try:
            self.logger.info(f"🔍 [ALARM_PARSE] Parsing alarm text: '{text}'")
            
            # 日付・時刻のパターンマッチング
            result = {}
            
            # 時刻パターンの抽出
            time_patterns = [
                (r'(\d{1,2})時(\d{1,2})分', lambda m: f"{int(m.group(1)):02d}:{int(m.group(2)):02d}"),
                (r'(\d{1,2})時半', lambda m: f"{int(m.group(1)):02d}:30"),
                (r'(\d{1,2})時', lambda m: f"{int(m.group(1)):02d}:00"),
                (r'午前(\d{1,2})時', lambda m: f"{int(m.group(1)):02d}:00"),
                (r'午後(\d{1,2})時', lambda m: f"{int(m.group(1)) + 12 if int(m.group(1)) < 12 else int(m.group(1)):02d}:00"),
                (r'朝(\d{1,2})時', lambda m: f"{int(m.group(1)):02d}:00"),
                (r'夜(\d{1,2})時', lambda m: f"{int(m.group(1)) + 12 if int(m.group(1)) < 12 else int(m.group(1)):02d}:00"),
            ]
            
            # 日付パターンの抽出
            date_patterns = [
                (r'今日', lambda: datetime.datetime.now(pytz.timezone(timezone)).date()),
                (r'明日', lambda: (datetime.datetime.now(pytz.timezone(timezone)) + datetime.timedelta(days=1)).date()),
                (r'明後日', lambda: (datetime.datetime.now(pytz.timezone(timezone)) + datetime.timedelta(days=2)).date()),
                (r'(\d{1,2})日', lambda m: self._get_next_date_with_day(int(m.group(1)), timezone)),
            ]
            
            # 時刻を抽出
            time_found = None
            for pattern, converter in time_patterns:
                match = re.search(pattern, text)
                if match:
                    time_found = converter(match)
                    break
            
            # 日付を抽出
            date_found = None
            for pattern, converter in date_patterns:
                if re.search(pattern, text):
                    date_found = converter()
                    break
            
            # デフォルト値の設定
            if not date_found:
                date_found = datetime.datetime.now(pytz.timezone(timezone)).date()
            
            if not time_found:
                # 時刻が指定されていない場合は現在時刻から1時間後
                now = datetime.datetime.now(pytz.timezone(timezone))
                default_time = now + datetime.timedelta(hours=1)
                time_found = f"{default_time.hour:02d}:{default_time.minute:02d}"
            
            # メッセージを抽出（時刻・日付以外の部分）
            message = text
            for pattern, _ in time_patterns + [(p, None) for p, _ in date_patterns]:
                message = re.sub(pattern, '', message)
            
            # 不要な単語を除去
            remove_words = ["に", "を", "思い出させて", "教えて", "お知らせ", "アラーム", "設定", "して"]
            for word in remove_words:
                message = message.replace(word, "")
            message = message.strip()
            
            if not message:
                message = "アラーム"
            
            result = {
                "date": date_found.strftime("%Y-%m-%d"),
                "time": time_found,
                "timezone": timezone,
                "message": message
            }
            
            self.logger.info(f"✅ [ALARM_PARSE] Parsed result: {result}")
            return result
            
        except Exception as e:
            self.logger.error(f"❌ [ALARM_PARSE] Error parsing alarm text: {e}")
            return None
    
    def _get_next_date_with_day(self, day: int, timezone: str) -> datetime.date:
        """指定された日付の次の該当日を取得"""
        try:
            now = datetime.datetime.now(pytz.timezone(timezone))
            current_day = now.day
            
            if day > current_day:
                # 今月の指定日
                return now.replace(day=day).date()
            else:
                # 来月の指定日
                if now.month == 12:
                    next_month = now.replace(year=now.year + 1, month=1, day=day)
                else:
                    next_month = now.replace(month=now.month + 1, day=day)
                return next_month.date()
        except:
            # エラーの場合は明日を返す
            return (datetime.datetime.now(pytz.timezone(timezone)) + datetime.timedelta(days=1)).date()

class MessageTextParser(FlexibleTextParser):
    """メッセージ送信用の自然言語処理クラス"""
    
    async def parse_message_command_ai(self, text: str) -> Optional[Dict]:
        """AI APIを使用した高精度解析"""
        try:
            import httpx
            import json
            import os
            
            # OpenAI API設定
            api_key = os.getenv("OPENAI_API_KEY")
            if not api_key:
                self.logger.warning("⚠️ [AI_PARSE] OpenAI API key not found, falling back to regex")
                return self.parse_message_command_legacy(text)
            
            prompt = f"""以下のテキストからメッセージ送信の意図を解析してください。
送信相手の名前とメッセージ内容をJSONで返してください。

テキスト: "{text}"

期待される出力形式:
{{"recipient": "相手の名前", "message": "メッセージ内容"}}

重要：名前の読み方は柔軟に認識してください。
- 漢字、ひらがな、カタカナの違いは無視
- 「君」「くん」「きみ」など読み方の違いは無視
- 例えば「うんちくん」「うんち君」「うんちきみ」は全て同じ名前として認識
- 敬称（さん、君、ちゃん、くん）は除去してください。

送信の意図がない場合はnullを返してください。"""

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
                        "max_tokens": 100,
                        "temperature": 0
                    },
                    timeout=10.0
                )
                
                if response.status_code == 200:
                    data = response.json()
                    content = data["choices"][0]["message"]["content"]
                    
                    try:
                        result = json.loads(content)
                        if result and result.get("recipient") and result.get("message"):
                            result["action"] = "send_message"
                            self.logger.info(f"✅ [AI_PARSE] AI解析成功: {result}")
                            return result
                    except json.JSONDecodeError:
                        self.logger.error(f"❌ [AI_PARSE] JSON解析失敗: {content}")
                else:
                    self.logger.error(f"❌ [AI_PARSE] API呼び出し失敗: {response.status_code}")
                    
        except Exception as e:
            self.logger.error(f"❌ [AI_PARSE] AI解析エラー: {e}")
        
        # フォールバック: 従来の正規表現方式
        self.logger.info("🔄 [AI_PARSE] Falling back to regex parsing")
        return self.parse_message_command_legacy(text)

    async def parse_message_command(self, text: str) -> Optional[Dict]:
        """
        メインの解析メソッド（AI優先、フォールバック付き）
        """
        # まずAI解析を試行
        result = await self.parse_message_command_ai(text)
        if result:
            return result
        
        # AI解析が失敗した場合は正規表現方式
        return self.parse_message_command_legacy(text)
    
    def parse_message_command_regex(self, text: str) -> Optional[Dict]:
        """
        正規表現ベースの解析（柔軟版）
        """
        try:
            self.logger.info(f"🔍 [MESSAGE_PARSE] Parsing message text: '{text}'")
            
            # 送信を示すキーワード
            send_keywords = ["伝えて", "送って", "送信して", "送る", "送信", "メッセージ", "手紙書いて", "レター"]
            
            # 送信キーワードが含まれているかチェック
            has_send_keyword = any(keyword in text for keyword in send_keywords)
            if not has_send_keyword:
                return None
            
            # 柔軟な解析：名前候補を抽出
            potential_names = self._extract_potential_names(text)
            
            # メッセージ部分を抽出
            message_part = self._extract_message_part(text, potential_names)
            
            if potential_names and message_part:
                # 最も可能性の高い名前を選択
                best_name = potential_names[0]
                
                parsed_result = {
                    "recipient": best_name,
                    "message": message_part,
                    "action": "send_message"
                }
                
                self.logger.info(f"✅ [MESSAGE_PARSE] Flexible parsed result: {parsed_result}")
                return parsed_result
            
            return None
            
        except Exception as e:
            self.logger.error(f"❌ [MESSAGE_PARSE] Error parsing message text: {e}")
            return None
    
    def _extract_potential_names(self, text: str) -> List[str]:
        """テキストから名前候補を抽出"""
        names = []
        
        # パターン1: 「〜さん」「〜君」「〜ちゃん」
        name_patterns = [
            r'(\w+)さん',
            r'(\w+)君',
            r'(\w+)ちゃん',
        ]
        
        for pattern in name_patterns:
            matches = re.findall(pattern, text)
            names.extend(matches)
        
        # パターン2: 「〜に」「〜へ」の前の単語
        target_patterns = [
            r'(\w+)に(?=.*(?:伝えて|送って|送信))',
            r'(\w+)へ(?=.*(?:伝えて|送って|送信))',
        ]
        
        for pattern in target_patterns:
            matches = re.findall(pattern, text)
            names.extend(matches)
        
        # 重複除去・正規化
        unique_names = []
        for name in names:
            clean_name = name.replace("さん", "").replace("君", "").replace("ちゃん", "").strip()
            if clean_name and clean_name not in unique_names:
                unique_names.append(clean_name)
        
        return unique_names
    
    def _extract_message_part(self, text: str, names: List[str]) -> str:
        """名前と送信ワードを除いてメッセージ部分を抽出"""
        message = text
        
        # 送信ワードを除去
        send_words = ["伝えて", "送って", "送信して", "送る", "送信", "メッセージ", "手紙書いて", "レター"]
        for word in send_words:
            message = message.replace(word, "")
        
        # 名前関連を除去
        for name in names:
            variations = [f"{name}さん", f"{name}君", f"{name}ちゃん", name]
            for variation in variations:
                message = message.replace(variation, "")
        
        # 助詞・接続詞を除去
        particles = ["に", "へ", "と", "って", "を"]
        for particle in particles:
            message = message.replace(particle, "")
        
        return message.strip()
    
    def parse_message_command_legacy(self, text: str) -> Optional[Dict]:
        """
        従来の正規表現パターン（バックアップ用）
        """
        try:
            # メッセージ送信のパターンマッチング
            message_patterns = [
                # 通常順序: 「田中さんにお疲れ様と送って」
                (r'(.+?)に(.+?)と?(伝えて|送って|送信して|送る|送信|メッセージ)', lambda m: {"recipient": m.group(1).strip(), "message": m.group(2).strip()}),
                (r'(.+?)へ(.+?)と?(伝えて|送って|送信して|送る|送信|メッセージ)', lambda m: {"recipient": m.group(1).strip(), "message": m.group(2).strip()}),
                (r'(.+?)さんに(.+?)と?(伝えて|送って|送信して|送る|送信)', lambda m: {"recipient": m.group(1).strip(), "message": m.group(2).strip()}),
                # 逆順序: 「こんにちはって田中さんに送って」
                (r'(.+?)って(.+?)に(伝えて|送って|送信して|送る|送信)', lambda m: {"recipient": m.group(2).strip(), "message": m.group(1).strip()}),
                (r'(.+?)って(.+?)へ(伝えて|送って|送信して|送る|送信)', lambda m: {"recipient": m.group(2).strip(), "message": m.group(1).strip()}),
                (r'(.+?)って(.+?)さんに(伝えて|送って|送信して|送る|送信)', lambda m: {"recipient": m.group(2).strip(), "message": m.group(1).strip()}),
                # 手紙パターン
                (r'(.+?)に(.+?)って?(手紙書いて|レター)', lambda m: {"recipient": m.group(1).strip(), "message": m.group(2).strip()}),
                (r'(.+?)へ(.+?)って?(手紙書いて|レター)', lambda m: {"recipient": m.group(1).strip(), "message": m.group(2).strip()}),
            ]
            
            for pattern, extractor in message_patterns:
                match = re.search(pattern, text)
                if match:
                    result = extractor(match)
                    
                    # 受信者名の正規化
                    recipient = result["recipient"]
                    recipient = recipient.replace("さん", "").replace("君", "").replace("ちゃん", "").strip()
                    
                    # メッセージの正規化
                    message = result["message"]
                    message = message.replace("と", "").strip()
                    
                    if recipient and message:
                        parsed_result = {
                            "recipient": recipient,
                            "message": message,
                            "action": "send_message"
                        }
                        
                        return parsed_result
            
            return None
            
        except Exception as e:
            self.logger.error(f"❌ [MESSAGE_PARSE] Error parsing message text: {e}")
            return None
    
    def find_similar_friend_name(self, input_name: str, friend_list: List[str]) -> Optional[str]:
        """
        柔軟な友達名検索（メモリー検索の類似度計算を活用）
        「田中」→「田中太郎」のような部分マッチングを実現
        """
        try:
            self.logger.info(f"🔍 [FRIEND_SEARCH] Searching for '{input_name}' in friend list: {friend_list}")
            
            best_match = None
            best_similarity = 0.0
            similarity_threshold = 0.3  # メモリー検索と同じ閾値
            
            for friend_name in friend_list:
                similarity = self.calculate_similarity(input_name, friend_name)
                self.logger.info(f"🔍 [FRIEND_SIMILARITY] '{input_name}' vs '{friend_name}': {similarity}")
                
                if similarity > best_similarity and similarity > similarity_threshold:
                    best_similarity = similarity
                    best_match = friend_name
            
            if best_match:
                self.logger.info(f"✅ [FRIEND_MATCH] Best match for '{input_name}': '{best_match}' (similarity: {best_similarity})")
                return best_match
            else:
                self.logger.info(f"❌ [FRIEND_MATCH] No suitable match found for '{input_name}'")
                return None
                
        except Exception as e:
            self.logger.error(f"❌ [FRIEND_SEARCH] Error in friend name search: {e}")
            return None

# インスタンスを作成
alarm_parser = AlarmTextParser()
message_parser = MessageTextParser()
