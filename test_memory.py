#!/usr/bin/env python3
"""
メモリー機能のテストスクリプト
JWT生成、nekota-server連携、エンドポイント確認
"""

import asyncio
import sys
import os

# テスト用環境変数を設定
os.environ["NEKOTA_API_URL"] = "https://nekota-server-production.up.railway.app"
os.environ["NEKOTA_API_SECRET"] = "n7SQcEpEzkgJxRbuSnWYkNAl6CS8rgm1ihmLVzjiS9A"
os.environ["JWT_SECRET_KEY"] = "LIzfZ0vv4SnyaxF5YO99C7hlUUKKGhliyGaOvhUCP6NSDIU4bl7P5zfHJNcJludaZSZflTA0e+5BEYhlshjCFg=="

# プロジェクトのルートディレクトリをPythonパスに追加
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from ai.memory import MemoryService
from config import Config
import httpx
import jwt

async def test_memory_service():
    """メモリーサービスの動作テストを実行"""
    
    print("🧪 === メモリーサービステスト開始 ===")
    
    # 1. 設定確認
    print(f"📋 設定確認:")
    print(f"  - MANAGER_API_URL: {Config.MANAGER_API_URL}")
    print(f"  - MANAGER_API_SECRET: {'***' if Config.MANAGER_API_SECRET else 'None'}")
    print(f"  - JWT_SECRET_KEY: {'***' if Config.JWT_SECRET_KEY else 'None'}")
    
    if not Config.MANAGER_API_URL:
        print("❌ MANAGER_API_URLが設定されていません")
        return False
    
    if not Config.JWT_SECRET_KEY:
        print("❌ JWT_SECRET_KEYが設定されていません")
        return False
    
    # 2. JWT生成テスト
    print(f"\n🔑 JWT生成テスト:")
    test_device_id = "98:3d:ae:61:69:58"
    
    try:
        memory_service = MemoryService()
        jwt_token = memory_service._generate_device_jwt(test_device_id)
        print(f"  ✅ JWT生成成功: {jwt_token[:50]}...")
        
        # JWT デコードテスト
        decoded = jwt.decode(jwt_token, Config.JWT_SECRET_KEY, algorithms=["HS256"])
        print(f"  ✅ JWT検証成功: user_id={decoded.get('user_id')}")
        
    except Exception as e:
        print(f"  ❌ JWT生成失敗: {e}")
        return False
    
    # 3. nekota-server接続テスト
    print(f"\n🌐 nekota-server接続テスト:")
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(f"{Config.MANAGER_API_URL}/")
            print(f"  ✅ 接続成功: {response.status_code}")
    except Exception as e:
        print(f"  ❌ 接続失敗: {e}")
        return False
    
    # 4. メモリー保存テスト
    print(f"\n💾 メモリー保存テスト:")
    test_text = "好きな色は青です（テスト）"
    
    try:
        success = await memory_service.save_memory(test_device_id, test_text)
        if success:
            print(f"  ✅ メモリー保存成功")
        else:
            print(f"  ❌ メモリー保存失敗")
            return False
    except Exception as e:
        print(f"  ❌ メモリー保存エラー: {e}")
        return False
    
    # 5. 手動API確認
    print(f"\n🔍 手動API確認:")
    print(f"  URL: {Config.MANAGER_API_URL}/api/memory/")
    print(f"  Method: POST")
    print(f"  Headers: Authorization: Bearer {jwt_token[:30]}...")
    print(f"  Body: {{'text': '{test_text}', 'user_id': '{test_device_id}'}}")
    
    print(f"\n🎉 === テスト完了：全て成功しました！ ===")
    return True

async def test_simple_jwt():
    """簡単なJWT生成テスト"""
    print("\n🔧 === 簡単なJWT生成テスト ===")
    
    try:
        import time
        payload = {
            "user_id": "test_device",
            "device_id": "test_device", 
            "iat": int(time.time()),
            "exp": int(time.time()) + 3600
        }
        token = jwt.encode(payload, Config.JWT_SECRET_KEY, algorithm="HS256")
        print(f"✅ JWT生成成功: {token}")
        
        decoded = jwt.decode(token, Config.JWT_SECRET_KEY, algorithms=["HS256"])
        print(f"✅ JWT検証成功: {decoded}")
        
        return True
    except Exception as e:
        print(f"❌ JWT生成失敗: {e}")
        return False

async def main():
    """メインテスト実行"""
    print("🚀 メモリー機能テスト開始")
    
    # 基本的なJWTテスト
    jwt_ok = await test_simple_jwt()
    
    if jwt_ok:
        # 完全なメモリーサービステスト
        memory_ok = await test_memory_service()
        
        if memory_ok:
            print("\n✅ 全てのテストが成功しました！")
            print("🎯 実機テストを実行してください")
        else:
            print("\n❌ メモリーサービステストで問題が発生しました")
    else:
        print("\n❌ JWTテストで問題が発生しました")

if __name__ == "__main__":
    asyncio.run(main())
