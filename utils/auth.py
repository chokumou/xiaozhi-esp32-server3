import jwt
from datetime import datetime, timedelta
from config import Config
from utils.logger import setup_logger

logger = setup_logger()

class AuthError(Exception):
    pass

class AuthManager:
    def __init__(self):
        self.secret = Config.JWT_SECRET_KEY
        self.algorithm = Config.JWT_ALGORITHM
        if not self.secret:
            logger.error("JWT_SECRET_KEY is not set in config.")
            raise AuthError("JWT_SECRET_KEY is not configured.")

    def create_token(self, device_id: str, expires_delta: timedelta = None) -> str:
        if expires_delta:
            expire = datetime.utcnow() + expires_delta
        else:
            expire = datetime.utcnow() + timedelta(minutes=30) # Default 30 minutes

        to_encode = {"exp": expire, "sub": device_id}
        encoded_jwt = jwt.encode(to_encode, self.secret, algorithm=self.algorithm)
        return encoded_jwt

    def decode_token(self, token: str) -> str:
        try:
            payload = jwt.decode(token, self.secret, algorithms=[self.algorithm])
            device_id = payload.get("sub")
            if not device_id:
                raise AuthError("Invalid token payload: 'sub' not found.")
            return device_id
        except jwt.ExpiredSignatureError:
            raise AuthError("Token has expired.")
        except jwt.InvalidTokenError:
            raise AuthError("Invalid token.")