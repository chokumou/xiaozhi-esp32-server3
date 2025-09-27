#!/usr/bin/env python3
"""
認証リゾルバのテストスクリプト
"""
import asyncio
import sys
import os

# プロジェクトルートをパスに追加
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from ai.auth_resolver import AuthResolver

async def test_auth_resolver():
    """認証リゾルバのテスト"""
    print("🔑 [AUTH_RESOLVER_TEST] Starting test...")
    
    resolver = AuthResolver()
    
    # テストケース
    test_cases = [
        "327546",  # 端末番号
        "467731",  # 端末番号
        "405fc146-3a70-4c35-9ed4-a245dd5a9ee0",  # UUID
        "92b63e50-4f65-49dc-a259-35fe14bea832",  # UUID
        "ESP32_9:58",  # レガシー形式
        "unknown",  # レガシー形式
    ]
    
    for identifier in test_cases:
        print(f"\n🔍 Testing identifier: {identifier}")
        try:
            jwt_token, user_id, device_number = await resolver.resolve_auth(identifier)
            
            if jwt_token and user_id:
                print(f"✅ Success: device={device_number}, user_id={user_id}, token={jwt_token[:20]}...")
            else:
                print(f"❌ Failed: No auth data returned")
                
        except Exception as e:
            print(f"❌ Error: {e}")
    
    # キャッシュ統計を表示
    print(f"\n📊 Cache stats:")
    stats = await resolver.get_cache_stats()
    print(f"  UUID to device cache: {stats['uuid_to_device_cache_size']} entries")
    print(f"  Device to UUID cache: {stats['device_to_uuid_cache_size']} entries")
    
    await resolver.close()
    print("\n🔑 [AUTH_RESOLVER_TEST] Test completed!")

if __name__ == "__main__":
    asyncio.run(test_auth_resolver())
