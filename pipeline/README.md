# やまびこ — セットアップ

ポッドキャストの最新回を自動で文字起こし＋日本語対訳し、`docs/episodes/*.json` として出力します。
アプリ (`yamabiko.html`) を同じ `docs/` に置くと、その最新回を読み込みます。

## リポジトリ構成
```
your-repo/
├─ .github/workflows/build.yml      ← pipeline/build.yml をここへ
├─ pipeline/
│   ├─ transcribe.py
│   ├─ feeds.json
│   └─ requirements.txt
└─ docs/                            ← GitHub Pages の公開元にする
    ├─ index.html                   ← yamabiko.html をこの名前で置く
    └─ episodes/                    ← パイプラインが自動生成 (最初は空でOK)
```

## 手順
1. **リポジトリは Private 推奨**。文字起こし全文の公開再配布を避けるため。
   Private でも GitHub Actions は無料枠 2,000分/月あり、番組1〜数本/日なら十分収まります。
2. GitHub Secrets に `GEMINI_API_KEY` を登録（Settings → Secrets and variables → Actions）。
   キーは https://aistudio.google.com/apikey から無料発行。
3. `feeds.json` の使いたい番組を `"enabled": true` にし、`rss` にRSSを設定。
   RSSは castfeedvalidator.com や podnews.net で確認できます（All Ears English は設定済み）。
4. Settings → Pages で公開元を **`/docs`** に設定。
5. Actions タブから **Run workflow** で初回実行（以後は毎日06:00 JSTに自動）。
   完了すると `docs/episodes/` にJSONが増え、アプリで選べるようになります。

## 調整できる環境変数（build.yml 内）
- `WHISPER_MODEL` … `tiny`/`base`/`small`/`medium`。精度↑=遅い。既定 `small`
- `LATEST_PER_FEED` … 各番組から取り込む最新件数。既定 `1`
- `KEEP_TOTAL` … manifest に残す総件数。既定 `60`

## 出力JSONの形（アプリが読む契約）
```json
{
  "id": "aee-xxxx-abc123",
  "show": "All Ears English",
  "title": "…",
  "date": "2026-07-10",
  "audioUrl": "https://…/episode.mp3",
  "sentences": [
    { "en": "So, did you catch what he said?", "ja": "彼の言ったこと聞いた？",
      "start": 12.34, "end": 14.90, "idioms": ["did you catch"] }
  ]
}
```
`start`/`end` があるので、アプリの「実音源モード」では区間リピートになります。

## ローカルで試す
```bash
pip install -r pipeline/requirements.txt
GEMINI_API_KEY=xxx WHISPER_MODEL=base python pipeline/transcribe.py
```
