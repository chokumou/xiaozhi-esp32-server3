#!/usr/bin/env python3
"""
JWT詳細デバッグスクリプト
"""

import os
import jwt
import time
import json

# テスト用環境変数を設定
os.environ["JWT_SECRET_KEY"] = "LIzfZ0vv4SnyaxF5YO99C7hlUUKKGhliyGaOvhUCP6NSDIU4bl7P5zfHJNcJludaZSZflTA0e+5BEYhlshjCFg=="

def debug_jwt():
    """JWT詳細デバッグ"""
    
    jwt_secret = os.environ["JWT_SECRET_KEY"]
    device_id = "98:3d:ae:61:69:58"
    
    print("🔍 JWT詳細デバッグ")
    print(f"JWT_SECRET_KEY: {jwt_secret[:20]}...")
    print(f"Device ID: {device_id}")
    
    # JWT生成
    payload = {
        "user_id": device_id,
        "device_id": device_id,
        "iat": int(time.time()),
        "exp": int(time.time()) + 3600,
    }
    
    print(f"\nペイロード:")
    print(json.dumps(payload, indent=2))
    
    token = jwt.encode(payload, jwt_secret, algorithm="HS256")
    print(f"\n生成されたJWT:")
    print(token)
    
    # JWT検証
    try:
        decoded = jwt.decode(token, jwt_secret, algorithms=["HS256"])
        print(f"\n検証成功:")
        print(json.dumps(decoded, indent=2))
    except Exception as e:
        print(f"\n検証失敗: {e}")
    
    # nekota-serverで期待される形式を確認
    print(f"\n🎯 nekota-serverで期待される形式:")
    print(f"Authorization: Bearer {token}")
    print(f"Body: {{'text': 'テストメッセージ', 'user_id': '{device_id}'}}")
    
    return token

def test_different_payloads():
    """異なるペイロード形式をテスト"""
    
    jwt_secret = os.environ["JWT_SECRET_KEY"]
    device_id = "98:3d:ae:61:69:58"
    
    print(f"\n🧪 異なるペイロード形式のテスト")
    
    # パターン1: シンプル
    payload1 = {
        "user_id": device_id,
        "exp": int(time.time()) + 3600,
    }
    token1 = jwt.encode(payload1, jwt_secret, algorithm="HS256")
    print(f"パターン1 (シンプル): {token1}")
    
    # パターン2: sub フィールド付き
    payload2 = {
        "sub": device_id,
        "user_id": device_id,
        "exp": int(time.time()) + 3600,
    }
    token2 = jwt.encode(payload2, jwt_secret, algorithm="HS256")
    print(f"パターン2 (sub付き): {token2}")
    
    # パターン3: 標準的
    payload3 = {
        "user_id": device_id,
        "iat": int(time.time()),
        "exp": int(time.time()) + 3600,
        "iss": "xiaozhi-esp32-server3"
    }
    token3 = jwt.encode(payload3, jwt_secret, algorithm="HS256")
    print(f"パターン3 (標準): {token3}")

if __name__ == "__main__":
    token = debug_jwt()
    test_different_payloads()

