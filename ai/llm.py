import openai
from config import Config
from utils.logger import setup_logger

logger = setup_logger()

class LLMService:
    def __init__(self):
        self.client = openai.OpenAI(api_key=Config.OPENAI_API_KEY)
        self.model = Config.OPENAI_LLM_MODEL
        logger.info(f"LLMService initialized with model: {self.model}")

    async def chat_completion(self, messages: list, stream: bool = False) -> str:
        try:
            # Add system prompt if not already present
            if not messages or messages[0]["role"] != "system":
                # ネコ太キャラクター設定を使用
                system_prompt = Config.CHARACTER_PROMPT + """

ユーザーが「覚えて」「覚えておいて」と言った時は、その情報を記憶してください。
ユーザーが過去の話題について質問したら、記憶している情報を活用して答えてください。"""
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
