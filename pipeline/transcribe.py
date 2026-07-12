#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
やまびこ パイプライン
RSS -> 最新エピソード検出 -> faster-whisper で文字起こし(単語タイムスタンプ)
-> 文単位に整形 -> Gemini で日本語対訳 + イディオム抽出 -> docs/episodes/*.json 出力
GitHub Actions から定期実行する想定。ローカル実行も可。

環境変数:
  GEMINI_API_KEY   … Google AI Studio の無料APIキー(必須)
  WHISPER_MODEL    … tiny/base/small/medium (既定 small)
  LATEST_PER_FEED  … 各番組から取り込む最新件数 (既定 1)
  KEEP_TOTAL       … manifest に残す総件数 (既定 60)
  GEMINI_MODEL     … 既定 gemini-2.5-flash
"""
import os, re, json, sys, time, tempfile, pathlib, hashlib
import requests
import feedparser
from faster_whisper import WhisperModel

ROOT = pathlib.Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "docs" / "episodes"
FEEDS_FILE = pathlib.Path(__file__).resolve().parent / "feeds.json"

GEMINI_KEY   = os.environ.get("GEMINI_API_KEY", "").strip()
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
WHISPER_MODEL= os.environ.get("WHISPER_MODEL", "small")
LATEST_PER_FEED = int(os.environ.get("LATEST_PER_FEED", "1"))
KEEP_TOTAL   = int(os.environ.get("KEEP_TOTAL", "60"))

SENT_MAX_SEC = 16.0   # 1文が長すぎる場合の強制分割しきい値
GEMINI_BATCH = 40     # 対訳を一度に投げる文数


def slug(s: str, n: int = 40) -> str:
    s = re.sub(r"[^a-zA-Z0-9]+", "-", s).strip("-").lower()
    return (s[:n] or "ep").strip("-")


def load_manifest():
    f = OUT_DIR / "index.json"
    if f.exists():
        try:
            return json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"episodes": []}


def save_manifest(man):
    man["episodes"].sort(key=lambda e: e.get("date", ""), reverse=True)
    man["episodes"] = man["episodes"][:KEEP_TOTAL]
    (OUT_DIR / "index.json").write_text(
        json.dumps(man, ensure_ascii=False, indent=2), encoding="utf-8")


def enclosure_url(entry):
    for l in entry.get("links", []):
        if l.get("rel") == "enclosure" and l.get("href"):
            return l["href"]
    if entry.get("enclosures"):
        return entry["enclosures"][0].get("href")
    return None


def download_audio(url, dest):
    with requests.get(url, stream=True, timeout=120,
                      headers={"User-Agent": "yamabiko/0.1"}) as r:
        r.raise_for_status()
        with open(dest, "wb") as f:
            for chunk in r.iter_content(chunk_size=1 << 16):
                f.write(chunk)


def transcribe(model, path):
    """faster-whisper -> 文リスト [{en,start,end}]"""
    segments, info = model.transcribe(
        str(path), language="en", word_timestamps=True, vad_filter=True,
        beam_size=5,
    )
    words = []
    for seg in segments:
        if seg.words:
            words.extend(seg.words)
        else:  # 単語なしのセグメントは丸ごと1語扱い
            words.append(type("W", (), {"word": seg.text, "start": seg.start, "end": seg.end}))

    sentences, cur = [], []
    for w in words:
        cur.append(w)
        text = (w.word or "").strip()
        dur = cur[-1].end - cur[0].start
        ends = text[-1:] in (".", "?", "!") if text else False
        if (ends and len(cur) >= 2) or dur >= SENT_MAX_SEC:
            en = "".join((x.word or "") for x in cur).strip()
            if en:
                sentences.append({"en": en, "start": round(cur[0].start, 2),
                                  "end": round(cur[-1].end, 2)})
            cur = []
    if cur:
        en = "".join((x.word or "") for x in cur).strip()
        if en:
            sentences.append({"en": en, "start": round(cur[0].start, 2),
                              "end": round(cur[-1].end, 2)})
    return sentences


def gemini_json(prompt):
    url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
           f"{GEMINI_MODEL}:generateContent?key={GEMINI_KEY}")
    body = {"contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"responseMimeType": "application/json",
                                 "temperature": 0.3}}
    for attempt in range(4):
        r = requests.post(url, json=body, timeout=180)
        if r.status_code == 429:      # レート超過は待って再試行
            time.sleep(20 * (attempt + 1)); continue
        r.raise_for_status()
        parts = r.json()["candidates"][0]["content"]["parts"]
        txt = "".join(p.get("text", "") for p in parts)
        return json.loads(re.sub(r"```json|```", "", txt).strip())
    raise RuntimeError("Gemini rate limited")


def translate(sentences):
    """en の各文に ja と idioms を付与"""
    for i in range(0, len(sentences), GEMINI_BATCH):
        batch = sentences[i:i + GEMINI_BATCH]
        payload = [{"i": j, "en": s["en"]} for j, s in enumerate(batch)]
        prompt = (
            "英語学習教材の字幕対訳を作ります。次の各英文について、自然な日本語訳と、"
            "その文に含まれる注目イディオム・句動詞・連結フレーズ(2〜5個, 無ければ空配列)を返してください。\n"
            "必ず次のJSON配列のみ返す(前置き禁止, 入力と同じ長さ, iで対応):\n"
            '[{"i":0,"ja":"日本語訳","idioms":["..."]}]\n'
            f"入力: {json.dumps(payload, ensure_ascii=False)}"
        )
        try:
            arr = gemini_json(prompt)
        except Exception as e:
            print(f"  translate batch {i} failed: {e}", file=sys.stderr)
            arr = []
        by_i = {o.get("i"): o for o in arr if isinstance(o, dict)}
        for j, s in enumerate(batch):
            o = by_i.get(j, {})
            s["ja"] = o.get("ja", "")
            s["idioms"] = o.get("idioms", []) or []
    return sentences


def process_feed(model, feed, man, seen_ids):
    print(f"[{feed['id']}] {feed['name']} …")
    d = feedparser.parse(feed["rss"])
    if not d.entries:
        print("  no entries"); return 0
    added = 0
    for entry in d.entries[:LATEST_PER_FEED]:
        title = entry.get("title", "untitled")
        guid = entry.get("id") or entry.get("link") or title
        ep_id = f"{feed['id']}-{slug(title)}-{hashlib.md5(guid.encode()).hexdigest()[:6]}"
        if ep_id in seen_ids:
            print(f"  skip (done): {title}"); continue
        audio = enclosure_url(entry)
        if not audio:
            print(f"  skip (no audio): {title}"); continue
        print(f"  transcribing: {title}")
        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=True) as tmp:
            try:
                download_audio(audio, tmp.name)
                sents = transcribe(model, tmp.name)
            except Exception as e:
                print(f"  ERROR: {e}", file=sys.stderr); continue
        if not sents:
            print("  no speech detected"); continue
        print(f"  {len(sents)} sentences -> translating")
        sents = translate(sents)

        date = ""
        if entry.get("published_parsed"):
            date = time.strftime("%Y-%m-%d", entry["published_parsed"])
        ep = {"id": ep_id, "show": feed["name"], "title": title,
              "date": date, "audioUrl": audio, "sentences": sents}
        (OUT_DIR / f"{ep_id}.json").write_text(
            json.dumps(ep, ensure_ascii=False, indent=2), encoding="utf-8")
        man["episodes"].append({"id": ep_id, "show": feed["name"],
                                "title": title, "date": date,
                                "file": f"episodes/{ep_id}.json"})
        seen_ids.add(ep_id); added += 1
        print(f"  saved {ep_id}.json")
    return added


def main():
    if not GEMINI_KEY:
        print("GEMINI_API_KEY is not set", file=sys.stderr); sys.exit(1)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    feeds = json.loads(FEEDS_FILE.read_text(encoding="utf-8"))["feeds"]
    man = load_manifest()
    seen = {e["id"] for e in man["episodes"]}

    print(f"loading whisper: {WHISPER_MODEL} (cpu/int8)")
    model = WhisperModel(WHISPER_MODEL, device="cpu", compute_type="int8")

    total = 0
    for feed in feeds:
        if feed.get("enabled") and feed.get("rss"):
            total += process_feed(model, feed, man, seen)
    save_manifest(man)
    print(f"done. added {total} episode(s).")


if __name__ == "__main__":
    main()
