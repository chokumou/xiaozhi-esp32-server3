import asyncio
from typing import List, Dict, Optional
from openai import AsyncOpenAI
from config import config
from utils.logger import log

class LLMProvider:
    """OpenAI GPT LLMプロバイダー"""
    
    def __init__(self):
        self.client = AsyncOpenAI(api_key=config.OPENAI_API_KEY)
        self.system_prompt = """あなたは小智（しゃおじー）という名前のAIアシスタントです。

特徴:
- 親しみやすく、フレンドリーな話し方
- 日本語で自然に会話
- ユーザーの記憶を大切にする
- 簡潔で分かりやすい回答

ユーザーが「覚えて」「覚えておいて」と言った時は、その情報を記憶してください。
ユーザーが過去の話題について質問したら、記憶している情報を活用して答えてください。"""
    
    async def chat(self, messages: List[Dict[str, str]], memory_context: str = "") -> Optional[str]:
        """チャット応答を生成"""
        try:
            log.debug(f"LLM chat start with {len(messages)} messages")
            
            # システムプロンプトを準備
            system_message = self.system_prompt
            if memory_context:
                system_message += f"\n\n記憶している情報:\n{memory_context}"
            
            # メッセージリストを構築
            api_messages = [{"role": "system", "content": system_message}]
            api_messages.extend(messages)
            
            # OpenAI GPT APIで応答生成
            response = await self.client.chat.completions.create(
                model=config.OPENAI_LLM_MODEL,
                messages=api_messages,
                max_tokens=500,
                temperature=0.7
            )
            
            content = response.choices[0].message.content
            if content:
                log.info(f"LLM response: '{content[:100]}...'")
                return content.strip()
            else:
                log.warning("LLM returned empty response")
                return None
                
        except Exception as e:
            log.error(f"LLM chat failed: {e}")
            return None
    
    async def summarize_memory(self, conversation: str) -> Optional[str]:
        """会話から記憶すべき情報を抽出"""
        try:
            prompt = f"""以下の会話から、ユーザーについて記憶すべき重要な情報を抽出してください。
個人情報、好み、重要な事実などを簡潔にまとめてください。

会話:
{conversation}

記憶すべき情報（簡潔に）:"""
            
            response = await self.client.chat.completions.create(
                model=config.OPENAI_LLM_MODEL,
                messages=[{"role": "user", "content": prompt}],
                max_tokens=200,
                temperature=0.3
            )
            
            summary = response.choices[0].message.content
            if summary:
                log.info(f"Memory summary created: '{summary[:100]}...'")
                return summary.strip()
            else:
                return None
                
        except Exception as e:
            log.error(f"Memory summarization failed: {e}")
            return None
