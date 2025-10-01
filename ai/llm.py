import openai
from config import Config
from utils.logger import setup_logger
from utils.short_memory_processor import ShortMemoryProcessor

logger = setup_logger()

class LLMService:
    def __init__(self):
        self.client = openai.OpenAI(api_key=Config.OPENAI_API_KEY)
        self.model = Config.OPENAI_LLM_MODEL
        self.short_memory_processor = None  # ユーザーIDが設定されたら初期化
        logger.info(f"LLMService initialized with model: {self.model}")

    def set_user_id(self, user_id: str):
        """ユーザーIDを設定して短期記憶プロセッサーを初期化"""
        if not self.short_memory_processor:
            self.short_memory_processor = ShortMemoryProcessor(user_id)
            logger.info(f"Short memory processor initialized for user: {user_id}")
        else:
            logger.info(f"Short memory processor already exists for user: {user_id}")

    async def chat_completion(self, messages: list, stream: bool = False, user_id: str = None) -> str:
        try:
            # Add system prompt if not already present
            if not messages or messages[0]["role"] != "system":
                # ユーザーIDが提供された場合は短期記憶プロセッサーを初期化
                if user_id and not self.short_memory_processor:
                    self.set_user_id(user_id)
                
                # 基本システムプロンプト
                system_prompt = Config.CHARACTER_PROMPT + """

ユーザーが「覚えて」「覚えておいて」と言った時は、その情報を記憶してください。
ユーザーが過去の話題について質問したら、記憶している情報を活用して答えてください。"""
                
                # プロンプトの内容をログ出力（デバッグ用）
                logger.info(f"📝 [PROMPT_DEBUG] Character prompt length: {len(Config.CHARACTER_PROMPT)} chars")
                logger.info(f"📝 [PROMPT_DEBUG] Character prompt preview: {Config.CHARACTER_PROMPT[:200]}...")
                
                # 短期記憶と辞書のコンテキストを追加
                if self.short_memory_processor:
                    try:
                        logger.info(f"🧠 [PROMPT_INTEGRATION] Adding short memory and glossary context")
                        
                        # 短期記憶コンテキストを取得
                        memory_context = self.short_memory_processor.get_context_for_prompt()
                        if memory_context:
                            system_prompt += f"\n\n[短期記憶]\n{memory_context}"
                            logger.info(f"🧠 [PROMPT_INTEGRATION] Added memory context: {memory_context[:100]}...")
                        else:
                            logger.info(f"🧠 [PROMPT_INTEGRATION] No memory context available")
                        
                        # 辞書コンテキストを取得（最近の用語から）
                        recent_terms = []
                        if len(messages) > 0:
                            # 最新のユーザーメッセージから用語を抽出
                            latest_message = messages[-1].get("content", "")
                            recent_terms = self.short_memory_processor.extract_candidate_terms(latest_message)
                            logger.info(f"🧠 [PROMPT_INTEGRATION] Extracted recent terms: {recent_terms}")
                        
                        glossary_context = self.short_memory_processor.get_glossary_for_prompt(recent_terms)
                        if glossary_context:
                            system_prompt += f"\n\n[辞書]\n{glossary_context}"
                            logger.info(f"🧠 [PROMPT_INTEGRATION] Added glossary context: {glossary_context[:100]}...")
                        else:
                            logger.info(f"🧠 [PROMPT_INTEGRATION] No glossary context available")
                    except Exception as e:
                        logger.error(f"🧠 [PROMPT_INTEGRATION] Error adding context: {e}")
                        # エラーが発生してもプロンプト生成は継続
                else:
                    logger.info(f"🧠 [PROMPT_INTEGRATION] No short memory processor available")
                
                # 最終的なシステムプロンプトをログ出力
                logger.info(f"📝 [PROMPT_DEBUG] Final system prompt length: {len(system_prompt)} chars")
                logger.info(f"📝 [PROMPT_DEBUG] Final system prompt:\n{system_prompt}")
                
                messages.insert(0, {"role": "system", "content": system_prompt})
            
            # Use synchronous API call in async context
            import asyncio
            response = await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: self.client.chat.completions.create(
                    model=self.model,
                    messages=messages,
                    max_tokens=500,
                    temperature=0.7,
                    stream=False  # Keep it simple for now
                )
            )
            
            result = response.choices[0].message.content if response.choices else ""
            logger.debug(f"LLM response: {result[:100]}...")
            return result
            
        except Exception as e:
            logger.error(f"LLM chat completion failed: {e}")
            return ""
