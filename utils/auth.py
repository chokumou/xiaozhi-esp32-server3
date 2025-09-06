from typing import Optional, Dict, Any
from jose import JWTError, jwt
from config import config
from utils.logger import log

class AuthManager:
    """JWT認証管理"""
    
    @staticmethod
    def verify_token(token: str) -> Optional[Dict[str, Any]]:
        """JWTトークンを検証"""
        try:
            # Bearer プレフィックスを削除
            if token.startswith("Bearer "):
                token = token[7:]
            
            # トークンをデコード
            payload = jwt.decode(
                token,
                config.JWT_SECRET_KEY,
                algorithms=[config.JWT_ALGORITHM]
            )
            
            # デバイスIDを取得
            device_id = payload.get("device_id")
            if not device_id:
                log.warning("Token missing device_id")
                return None
            
            log.info(f"Authentication successful for device: {device_id}")
            return payload
            
        except JWTError as e:
            log.warning(f"JWT verification failed: {e}")
            return None
        except Exception as e:
            log.error(f"Authentication error: {e}")
            return None
    
    @staticmethod
    def extract_device_id(headers: Dict[str, str]) -> Optional[str]:
        """ヘッダーからデバイスIDを抽出"""
        # Authorization ヘッダーから抽出
        auth_header = headers.get("authorization") or headers.get("Authorization")
        if auth_header:
            payload = AuthManager.verify_token(auth_header)
            if payload:
                return payload.get("device_id")
        
        # Device-ID ヘッダーから抽出
        device_id = headers.get("device-id") or headers.get("Device-ID")
        if device_id:
            return device_id
        
        return None
