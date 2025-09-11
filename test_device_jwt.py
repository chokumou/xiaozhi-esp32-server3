#!/usr/bin/env python3
"""
デバイス登録からJWT取得、メモリー保存までの完全フローテスト
"""

import asyncio
import httpx
import json

async def test_complete_flow():
    """完全なデバイス→JWT→メモリー保存フロー"""
    
    device_id = "327546"  # 登録済みデバイス番号
    api_url = "https://nekota-server-production.up.railway.app"
    
    print(f"🚀 完全フローテスト開始")
    print(f"📱 Device ID: {device_id}")
    print(f"🌐 API URL: {api_url}")
    
    async with httpx.AsyncClient(timeout=30.0) as client:
        
        # 1. デバイス存在確認とJWT取得
        print(f"\n1️⃣ デバイス存在確認とJWT取得")
        try:
            # device_numberが必要（MACアドレスをデバイス番号として使用）
            device_response = await client.post(
                f"{api_url}/api/device/exists",
                json={"device_number": device_id}
            )
            
            print(f"Response Status: {device_response.status_code}")
            print(f"Response Body: {device_response.text}")
            
            if device_response.status_code == 200:
                device_data = device_response.json()
                token = device_data.get("token")
                if token:
                    print(f"✅ JWT取得成功: {token[:50]}...")
                else:
                    print(f"❌ レスポンスにtokenがありません: {device_data}")
                    return
            else:
                print(f"❌ デバイス存在確認失敗: {device_response.status_code}")
                return
                
        except Exception as e:
            print(f"❌ デバイス存在確認エラー: {e}")
            return
        
        # 2. 取得したJWTでメモリー保存テスト
        print(f"\n2️⃣ 取得したJWTでメモリー保存テスト")
        try:
            headers = {"Authorization": f"Bearer {token}"}
            memory_data = {
                "text": "好きな色は青です（完全フローテスト）",
                "user_id": device_id
            }
            
            print(f"📦 Payload: {memory_data}")
            print(f"🔑 Headers: Authorization: Bearer {token[:30]}...")
            
            memory_response = await client.post(
                f"{api_url}/api/memory/",
                json=memory_data,
                headers=headers
            )
            
            print(f"Response Status: {memory_response.status_code}")
            print(f"Response Body: {memory_response.text}")
            
            if memory_response.status_code == 201:
                result = memory_response.json()
                print(f"✅ メモリー保存成功: {result}")
            else:
                print(f"❌ メモリー保存失敗: {memory_response.status_code}")
                
        except Exception as e:
            print(f"❌ メモリー保存エラー: {e}")
        
        # 3. JWT詳細デコード
        print(f"\n3️⃣ JWT詳細デコード")
        try:
            import jwt as jwt_lib
            decoded = jwt_lib.decode(token, options={"verify_signature": False})
            print(f"JWT Payload: {json.dumps(decoded, indent=2)}")
        except Exception as e:
            print(f"JWT デコードエラー: {e}")

if __name__ == "__main__":
    asyncio.run(test_complete_flow())
