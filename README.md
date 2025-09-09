# xiaozhi-esp32-server3

シンプルで安定したAI音声対話サーバー

## 特徴

- 🎙️ **音声認識**: OpenAI Whisper
- 🤖 **AI対話**: OpenAI GPT-4o-mini  
- 🔊 **音声合成**: OpenAI TTS
- 🧠 **記憶機能**: nekota-server連携
- 🔒 **認証**: JWT認証
- 📱 **ESP32対応**: WebSocket通信

## 環境設定

1. `.env` ファイルを作成（`env.example` を参考に）
2. 必要な環境変数を設定：
   - `OPENAI_API_KEY`: OpenAI APIキー
   - `NEKOTA_SERVER_URL`: nekota-serverのURL
   - `NEKOTA_SERVER_SECRET`: nekota-serverの認証シークレット
   - `JWT_SECRET_KEY`: JWT署名用シークレット

## インストール・実行

```bash
# 依存関係インストール
pip install -r requirements.txt

# サーバー起動
python main.py
```

## API仕様

### WebSocket接続
- エンドポイント: `ws://localhost:8080/`
- プロトコル: WebSocket

### メッセージ形式

#### Hello (接続時)
```json
{
  "type": "hello",
  "headers": {
    "authorization": "Bearer YOUR_JWT_TOKEN"
  }
}
```

#### テキスト入力
```json
{
  "type": "text",
  "text": "こんにちは",
  "device_id": "your_device_id"
}
```

#### 音声データ
- バイナリデータとして送信
- 形式: WAV, 16kHz, モノラル

### レスポンス

#### Welcome
```json
{
  "type": "welcome",
  "message": "xiaozhi-server3 connected",
  "device_id": "your_device_id"
}
```

#### テキスト応答
```json
{
  "type": "text", 
  "text": "こんにちは！何かお手伝いできることはありますか？",
  "device_id": "your_device_id"
}
```

#### 音声応答
```json
{
  "type": "audio",
  "format": "opus",
  "device_id": "your_device_id"
}
```
（続いてOPUS音声データがバイナリで送信）

## 記憶機能

- 「覚えて」「覚えておいて」で情報を記憶
- 「覚えてる？」「何だっけ？」で記憶を呼び出し
- nekota-serverデータベースに永続保存

## ログ

- コンソール出力: 設定可能レベル
- ファイル出力: `logs/xiaozhi-server3.log`
- ローテーション: 1日毎、7日間保持

