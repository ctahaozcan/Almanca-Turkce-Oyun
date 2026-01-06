#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import sqlite3
import datetime as dt
import random
import re
from difflib import SequenceMatcher
from typing import List, Optional, Tuple, Dict
from flask import Flask, request, redirect, url_for, render_template_string, session, flash

DB_PATH = "kelimeler.db"

LEITNER_INTERVALS_DAYS = {1: 0, 2: 1, 3: 3, 4: 7, 5: 14}

FUZZY_THRESHOLD = 0.88

app = Flask(__name__)

def now_utc_iso() -> str:
    return dt.datetime.utcnow().replace(microsecond=0).isoformat()

def add_days_iso(days: int) -> str:
    t = dt.datetime.utcnow() + dt.timedelta(days=days)
    return t.replace(microsecond=0).isoformat()

def schedule_next(box: int) -> str:
    days = LEITNER_INTERVALS_DAYS.get(box, 0)
    return add_days_iso(days)

def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn
def migrate_if_needed() -> None:
    conn = connect()
    cur = conn.cursor()

    cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='words'")
    if not cur.fetchone():
        conn.close()
        return

    cur.execute("PRAGMA table_info(words)")
    cols = [r["name"] for r in cur.fetchall()]
    if "turkish" not in cols:
        conn.close()
        return  # zaten yeni şema

    cur.execute("""
    CREATE TABLE IF NOT EXISTS translations (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        word_id INTEGER NOT NULL,
        turkish TEXT NOT NULL,
        created_at TEXT NOT NULL,
        FOREIGN KEY(word_id) REFERENCES words(id) ON DELETE CASCADE
    );
    """)
    cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS uniq_word_tr ON translations(word_id, turkish);")

    cur.execute("""
    CREATE TABLE IF NOT EXISTS words_new (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        german TEXT NOT NULL,
        example TEXT,
        notes TEXT,
        box INTEGER NOT NULL DEFAULT 1,
        correct_count INTEGER NOT NULL DEFAULT 0,
        wrong_count INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        last_seen_at TEXT,
        next_due_at TEXT NOT NULL
    );
    """)

    cur.execute("""
        INSERT INTO words_new (id, german, example, notes, box, correct_count, wrong_count, created_at, updated_at, last_seen_at, next_due_at)
        SELECT id, german, example, notes, box, correct_count, wrong_count, created_at, updated_at, last_seen_at, next_due_at
        FROM words
    """)

    cur.execute("SELECT id, turkish, created_at FROM words")
    rows = cur.fetchall()
    for r in rows:
        t = (r["turkish"] or "").strip()
        if t:
            try:
                cur.execute(
                    "INSERT INTO translations (word_id, turkish, created_at) VALUES (?, ?, ?)",
                    (int(r["id"]), t, r["created_at"] or now_utc_iso())
                )
            except sqlite3.IntegrityError:
                pass

    cur.execute("DROP TABLE words")
    cur.execute("ALTER TABLE words_new RENAME TO words")

    cur.execute("CREATE INDEX IF NOT EXISTS idx_words_due ON words(next_due_at);")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_trans_word ON translations(word_id);")

    conn.commit()
    conn.close()

def init_db() -> None:
    conn = connect()
    cur = conn.cursor()

    cur.execute("""
    CREATE TABLE IF NOT EXISTS words (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        german TEXT NOT NULL,
        example TEXT,
        notes TEXT,
        box INTEGER NOT NULL DEFAULT 1,
        correct_count INTEGER NOT NULL DEFAULT 0,
        wrong_count INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        last_seen_at TEXT,
        next_due_at TEXT NOT NULL
    );
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS translations (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        word_id INTEGER NOT NULL,
        turkish TEXT NOT NULL,
        created_at TEXT NOT NULL,
        FOREIGN KEY(word_id) REFERENCES words(id) ON DELETE CASCADE
    );
    """)

    cur.execute("CREATE INDEX IF NOT EXISTS idx_words_due ON words(next_due_at);")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_trans_word ON translations(word_id);")
    cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS uniq_word_tr ON translations(word_id, turkish);")

    conn.commit()
    conn.close()

def de_normalize(s: str) -> str:
    s = s.strip().lower()
    s = re.sub(r"\s+", " ", s)
    s = (s.replace("ß", "ss")
           .replace("ä", "ae")
           .replace("ö", "oe")
           .replace("ü", "ue"))
    return s

def tr_normalize(s: str) -> str:
    s = s.strip().lower()
    s = re.sub(r"\s+", " ", s)
    return s

def similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, a, b).ratio()

def is_close_enough(user: str, correct: str, lang: str) -> Tuple[bool, float]:
    if lang == "de":
        u, c = de_normalize(user), de_normalize(correct)
    else:
        u, c = tr_normalize(user), tr_normalize(correct)
    if not u or not c:
        return False, 0.0
    if u == c:
        return True, 1.0
    if len(u) <= 3 or len(c) <= 3:
        return False, similarity(u, c)
    sim = similarity(u, c)
    return (sim >= FUZZY_THRESHOLD), sim

def parse_translations(raw: str) -> List[str]:
    parts = re.split(r"[;,/]+", raw or "")
    out = []
    for p in parts:
        p = p.strip()
        if p:
            out.append(p)
    seen = set()
    uniq = []
    for t in out:
        key = tr_normalize(t)
        if key not in seen:
            uniq.append(t)
            seen.add(key)
    return uniq


# -------------------------
# DB işlemleri
# -------------------------
def create_word(german: str, translations: List[str], example: Optional[str], notes: Optional[str]) -> int:
    german = (german or "").strip()
    if not german:
        raise ValueError("Almanca boş olamaz.")
    if not translations:
        raise ValueError("En az bir Türkçe karşılık gir.")

    conn = connect()
    cur = conn.cursor()
    ts = now_utc_iso()

    cur.execute("""
        INSERT INTO words (german, example, notes, box, created_at, updated_at, next_due_at)
        VALUES (?, ?, ?, 1, ?, ?, ?)
    """, (german, example, notes, ts, ts, schedule_next(1)))
    word_id = cur.lastrowid

    for tr in translations:
        try:
            cur.execute("""
                INSERT INTO translations (word_id, turkish, created_at)
                VALUES (?, ?, ?)
            """, (word_id, tr.strip(), ts))
        except sqlite3.IntegrityError:
            pass

    conn.commit()
    conn.close()
    return int(word_id)

def add_translation(word_id: int, turkish: str) -> None:
    trs = parse_translations(turkish)
    if not trs:
        return
    conn = connect()
    cur = conn.cursor()
    ts = now_utc_iso()
    for t in trs:
        try:
            cur.execute(
                "INSERT INTO translations (word_id, turkish, created_at) VALUES (?, ?, ?)",
                (word_id, t, ts)
            )
        except sqlite3.IntegrityError:
            pass
    conn.commit()
    conn.close()

def update_word(word_id: int, german: str, translations_raw: str,
                example: Optional[str], notes: Optional[str]) -> None:
    german = (german or "").strip()
    if not german:
        raise ValueError("Almanca boş olamaz.")

    translations = parse_translations(translations_raw)
    if not translations:
        raise ValueError("En az bir Türkçe karşılık gerekli.")

    conn = connect()
    cur = conn.cursor()
    ts = now_utc_iso()

    # Ana kelimeyi güncelle
    cur.execute("""
        UPDATE words
        SET german = ?, example = ?, notes = ?, updated_at = ?
        WHERE id = ?
    """, (german, example, notes, ts, word_id))

    if cur.rowcount == 0:
        conn.close()
        raise ValueError("Kelime bulunamadı.")

    # Çevirileri komple yenile (basit ve temiz)
    cur.execute("DELETE FROM translations WHERE word_id = ?", (word_id,))
    for tr in translations:
        cur.execute("""
            INSERT INTO translations (word_id, turkish, created_at)
            VALUES (?, ?, ?)
        """, (word_id, tr, ts))

    conn.commit()
    conn.close()

def delete_word(word_id: int) -> None:
    conn = connect()
    cur = conn.cursor()
    cur.execute("DELETE FROM words WHERE id = ?", (word_id,))
    conn.commit()
    conn.close()

def list_words(limit: int = 100, sort: str = "updated", direction: str = "desc") -> List[dict]:
    # Güvenli alanlar (SQLi yememek için whitelist)
    sort_map = {
        "updated": "updated_at",
        "id": "id",
        "german": "german",
        "box": "box",
        "due": "next_due_at",
    }
    col = sort_map.get(sort, "updated_at")

    direction = (direction or "desc").lower()
    direction = "asc" if direction == "asc" else "desc"

    conn = connect()
    cur = conn.cursor()

    cur.execute(f"""
        SELECT id, german, box, correct_count, wrong_count, next_due_at, example, notes
        FROM words
        ORDER BY {col} COLLATE NOCASE {direction}
        LIMIT ?
    """, (limit,))
    rows = cur.fetchall()

    ids = [r["id"] for r in rows]
    trans_map: Dict[int, List[str]] = {i: [] for i in ids}
    if ids:
        q = f"SELECT word_id, turkish FROM translations WHERE word_id IN ({','.join(['?']*len(ids))}) ORDER BY id ASC"
        cur.execute(q, ids)
        for tr in cur.fetchall():
            trans_map[int(tr["word_id"])].append(tr["turkish"])

    conn.close()

    enriched = []
    for r in rows:
        d = dict(r)
        d["translations"] = trans_map.get(int(r["id"]), [])
        enriched.append(d)
    return enriched

def get_due_words(limit: int = 20) -> List[sqlite3.Row]:
    conn = connect()
    cur = conn.cursor()
    cur.execute("""
        SELECT * FROM words
        WHERE next_due_at <= ?
        ORDER BY next_due_at ASC
        LIMIT ?
    """, (now_utc_iso(), limit))
    rows = cur.fetchall()
    conn.close()
    return rows

def get_random_words(limit: int = 20) -> List[sqlite3.Row]:
    conn = connect()
    cur = conn.cursor()
    cur.execute("""
        SELECT * FROM words
        ORDER BY RANDOM()
        LIMIT ?
    """, (limit,))
    rows = cur.fetchall()
    conn.close()
    return rows

def get_translations(word_id: int) -> List[str]:
    conn = connect()
    cur = conn.cursor()
    cur.execute("SELECT turkish FROM translations WHERE word_id = ? ORDER BY id ASC", (word_id,))
    trs = [r["turkish"] for r in cur.fetchall()]
    conn.close()
    return trs

def update_after_answer(word_id: int, was_correct: bool, current_box: int) -> Tuple[int, str]:
    new_box = min(5, current_box + 1) if was_correct else 1
    ts = now_utc_iso()
    due = schedule_next(new_box)

    conn = connect()
    cur = conn.cursor()
    if was_correct:
        cur.execute("""
            UPDATE words
            SET box = ?, correct_count = correct_count + 1,
                updated_at = ?, last_seen_at = ?, next_due_at = ?
            WHERE id = ?
        """, (new_box, ts, ts, due, word_id))
    else:
        cur.execute("""
            UPDATE words
            SET box = ?, wrong_count = wrong_count + 1,
                updated_at = ?, last_seen_at = ?, next_due_at = ?
            WHERE id = ?
        """, (new_box, ts, ts, due, word_id))

    conn.commit()
    conn.close()
    return new_box, due

def stats() -> dict:
    conn = connect()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) AS c FROM words")
    total = cur.fetchone()["c"]
    cur.execute("SELECT COUNT(*) AS c FROM words WHERE next_due_at <= ?", (now_utc_iso(),))
    due = cur.fetchone()["c"]
    cur.execute("SELECT box, COUNT(*) AS c FROM words GROUP BY box ORDER BY box")
    boxes = {int(r["box"]): int(r["c"]) for r in cur.fetchall()}

    conn.close()
    return {"total": int(total), "due": int(due), "boxes": boxes}


# -------------------------
# Oyun akışı (session)
# -------------------------
def start_game(mode: str, pool: str, n: int) -> None:
    words = get_due_words(n) if pool == "due" else get_random_words(n)
    ids = [int(w["id"]) for w in words]
    session["game"] = {
        "mode": mode,        # "de->tr" | "tr->de" | "mixed"
        "pool": pool,
        "queue": ids,
        "idx": 0,
        "score": 0,
        "total": len(ids),
        "last_feedback": None,
    }

def get_current_word() -> Optional[sqlite3.Row]:
    g = session.get("game")
    if not g:
        return None
    queue = g.get("queue", [])
    idx = g.get("idx", 0)
    if idx >= len(queue):
        return None
    word_id = queue[idx]
    conn = connect()
    cur = conn.cursor()
    cur.execute("SELECT * FROM words WHERE id = ?", (word_id,))
    w = cur.fetchone()
    conn.close()
    return w

def advance_game(was_correct: bool) -> None:
    g = session.get("game")
    if not g:
        return
    if was_correct:
        g["score"] = int(g.get("score", 0)) + 1
    g["idx"] = int(g.get("idx", 0)) + 1
    session["game"] = g



BASE = """
<!doctype html>
<html lang="tr">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Almanca Kelime Oyunu</title>
  <style>
    body { font-family: system-ui, -apple-system, Segoe UI, Roboto, Arial; margin: 24px; max-width: 960px; }
    a { color: #0b57d0; text-decoration: none; }
    a:hover { text-decoration: underline; }
    .nav { display:flex; gap:12px; flex-wrap:wrap; margin-bottom:16px; }
    .card { border:1px solid #ddd; border-radius:12px; padding:16px; margin: 12px 0; }
    .muted { color:#666; font-size: 0.95rem; }
    .row { display:flex; gap:12px; flex-wrap:wrap; }
    input, textarea, select { width:100%; padding:10px; border:1px solid #ccc; border-radius:10px; }
    button { padding:10px 14px; border:0; border-radius:10px; background:#111; color:#fff; cursor:pointer; }
    button.secondary { background:#444; }
    table { width: 100%; border-collapse: collapse; }
    th, td { text-align:left; padding:10px; border-bottom:1px solid #eee; vertical-align: top; }
    .flash { padding:10px 12px; border-radius:10px; margin: 10px 0; }
    .ok { background:#e9f7ee; border:1px solid #bfe6cb; }
    .warn { background:#fff3cd; border:1px solid #ffe49a; }
    .bad { background:#fdecea; border:1px solid #f5b8b3; }
    .mono { font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono"; }
    .pill { display:inline-block; padding:3px 10px; border-radius:999px; border:1px solid #ddd; font-size:0.9rem; margin-right:6px;}
    .btnrow { display:flex; gap:8px; flex-wrap:wrap; }
  </style>
</head>
<body>
  <div class="nav">
    <a href="{{ url_for('home') }}">🏠 Ana sayfa</a>
    <a href="{{ url_for('add') }}">➕ Kelime ekle</a>
    <a href="{{ url_for('words') }}">📚 Kelimeler</a>
    <a href="{{ url_for('play_setup') }}">🎮 Oyna</a>
    <a href="{{ url_for('stats_page') }}">📊 İstatistik</a>
  </div>

  {% with messages = get_flashed_messages(with_categories=true) %}
    {% if messages %}
      {% for cat, msg in messages %}
        <div class="flash {{cat}}">{{msg|safe}}</div>
      {% endfor %}
    {% endif %}
  {% endwith %}

  {{ body|safe }}
</body>
</html>
"""

def page(body: str, **ctx):
    return render_template_string(BASE, body=render_template_string(body, **ctx))


# -------------------------
# Routes
# -------------------------
@app.route("/")
def home():
    s = stats()
    body = """
    <div class="card">
      <h2>Almanca Kelime Oyunu</h2>
      <p class="muted">
        SQLite + Leitner kutuları + yazım toleransı + çoklu anlamlar.
      </p>
      <div class="row">
        <div class="card" style="flex:1;">
          <div class="muted">Toplam kelime</div>
          <div style="font-size:2rem; font-weight:700;">{{s.total}}</div>
        </div>
        <div class="card" style="flex:1;">
          <div class="muted">Vadesi gelen</div>
          <div style="font-size:2rem; font-weight:700;">{{s.due}}</div>
        </div>
      </div>
      <p class="muted">Kutular:
        {% for k in [1,2,3,4,5] %}
          <span class="pill">K{{k}}: {{ s.boxes.get(k,0) }}</span>
        {% endfor %}
      </p>
      <p><a href="{{ url_for('play_setup') }}"><button>🎮 Hemen Oyna</button></a></p>
    </div>
    """
    return page(body, s=s)

@app.route("/add", methods=["GET", "POST"])
def add():
    if request.method == "POST":
        german = request.form.get("german", "")
        turkish_raw = request.form.get("turkish", "")
        example = request.form.get("example", "") or None
        notes = request.form.get("notes", "") or None

        try:
            trs = parse_translations(turkish_raw)
            word_id = create_word(german, trs, example, notes)
            flash(f"✅ Eklendi. ID: <span class='mono'>{word_id}</span>", "ok")
            return redirect(url_for("add"))
        except Exception as e:
            flash(f"❌ {e}", "bad")

    body = """
    <div class="card">
      <h2>Kelime Ekle</h2>
      <form method="post">
        <label>Almanca</label>
        <input name="german" placeholder="der Apfel / gehen / schön" required>

        <label style="margin-top:10px;">Türkçe karşılık(lar)</label>
        <input name="turkish" placeholder="elma, elma ağacı; (çoklu anlam için virgül/; kullan)" required>
        <div class="muted" style="margin-top:6px;">Örn: <span class="mono">gitmek, yürümek; ilerlemek</span></div>

        <label style="margin-top:10px;">Örnek cümle (opsiyonel)</label>
        <input name="example" placeholder="Ich esse einen Apfel.">

        <label style="margin-top:10px;">Not (opsiyonel)</label>
        <input name="notes" placeholder="isim / fiil / çekim / ipucu...">

        <div style="margin-top:14px;">
          <button type="submit">Kaydet</button>
        </div>
      </form>
    </div>
    """
    return page(body)

@app.route("/edit/<int:word_id>", methods=["GET", "POST"])
def edit(word_id: int):
    conn = connect()
    cur = conn.cursor()
    cur.execute("SELECT * FROM words WHERE id = ?", (word_id,))
    w = cur.fetchone()
    if not w:
        conn.close()
        flash("❌ Kelime bulunamadı.", "bad")
        return redirect(url_for("words"))

    cur.execute("SELECT turkish FROM translations WHERE word_id = ? ORDER BY id ASC", (word_id,))
    trs = [r["turkish"] for r in cur.fetchall()]
    conn.close()

    if request.method == "POST":
        try:
            update_word(
                word_id=word_id,
                german=request.form.get("german", ""),
                translations_raw=request.form.get("turkish", ""),
                example=request.form.get("example", "") or None,
                notes=request.form.get("notes", "") or None,
            )
            flash("✅ Kelime güncellendi.", "ok")
            return redirect(url_for("edit", word_id=word_id))

        except Exception as e:
            flash(f"❌ {e}", "bad")

    body = """
    <div class="card">
      <h2>✏️ Kelime Düzenle (ID {{w.id}})</h2>
      <form method="post">
        <label>Almanca</label>
        <input name="german" value="{{w.german}}" required>

        <label style="margin-top:10px;">Türkçe karşılıklar</label>
        <input name="turkish" value="{{ trs | join(', ') }}" required>
        <div class="muted" style="margin-top:6px;">Virgül / ; / / ile ayırabilirsin</div>

        <label style="margin-top:10px;">Örnek cümle</label>
        <input name="example" value="{{w.example or ''}}">

        <label style="margin-top:10px;">Not</label>
        <input name="notes" value="{{w.notes or ''}}">

        <div class="btnrow" style="margin-top:14px;">
          <button type="submit">Kaydet</button>
          <a href="{{ url_for('words') }}"><button type="button" class="secondary">İptal</button></a>
        </div>
      </form>
    </div>
    """
    return page(body, w=w, trs=trs)

@app.route("/words", methods=["GET", "POST"])
def words():
    if request.method == "POST":
        action = request.form.get("action")
        wid = int(request.form.get("word_id", "0") or 0)

        if action == "delete":
            delete_word(wid)
            flash("🗑️ Silindi.", "ok")
            return redirect(url_for("words"))

        if action == "add_tr":
            t = request.form.get("new_tr", "")
            add_translation(wid, t)
            flash("✅ Türkçe karşılık eklendi (varsa tekrarlar atlandı).", "ok")
            return redirect(url_for("words"))

    sort = request.args.get("sort", "updated")
    direction = request.args.get("dir", "desc")
    rows = list_words(200, sort=sort, direction=direction)

    body = """
    <div class="card">
      <h2>Kelimeler</h2>
      <p class="muted">Son 200 kayıt.</p>

      <form method="get" style="margin: 10px 0;">
        <div class="row">
          <div style="flex:1; min-width: 220px;">
            <label>Sırala</label>
            <select name="sort">
              <option value="updated" {% if sort=='updated' %}selected{% endif %}>Son güncellenen</option>
              <option value="id" {% if sort=='id' %}selected{% endif %}>ID</option>
              <option value="german" {% if sort=='german' %}selected{% endif %}>Almanca</option>
              <option value="box" {% if sort=='box' %}selected{% endif %}>Kutu</option>
              <option value="due" {% if sort=='due' %}selected{% endif %}>Vade (due)</option>
            </select>
          </div>
          <div style="flex:1; min-width: 220px;">
            <label>Yön</label>
            <select name="dir">
              <option value="asc" {% if dir=='asc' %}selected{% endif %}>Artan</option>
              <option value="desc" {% if dir=='desc' %}selected{% endif %}>Azalan</option>
            </select>
          </div>
          <div style="align-self:flex-end;">
            <button type="submit">Uygula</button>
          </div>
        </div>
      </form>

      {% if not rows %}
        <p>📭 Henüz kelime yok.</p>
      {% else %}
        <table>
          <thead>
            <tr>
              <th>ID</th>
              <th>Almanca</th>
              <th>Türkçe karşılıklar</th>
              <th>Kutu</th>
              <th>✅/❌</th>
              <th>Due</th>
              <th>İşlem</th>
            </tr>
          </thead>
          <tbody>
          {% for r in rows %}
            <tr>
              <td class="mono">{{r.id}}</td>
              <td>
                <div><b>{{r.german}}</b></div>
                {% if r.example %}<div class="muted">Örnek: {{r.example}}</div>{% endif %}
                {% if r.notes %}<div class="muted">Not: {{r.notes}}</div>{% endif %}
              </td>
              <td>
                {% if r.translations %}
                  <div>{{ r.translations | join(", ") }}</div>
                {% else %}
                  <div class="muted">(yok)</div>
                {% endif %}
                <form method="post" style="margin-top:8px;">
                  <input type="hidden" name="action" value="add_tr">
                  <input type="hidden" name="word_id" value="{{r.id}}">
                  <input name="new_tr" placeholder="Yeni Türkçe karşılık ekle (virgülle çoklu)" />
                  <div style="margin-top:6px;"><button class="secondary" type="submit">Ekle</button></div>
                </form>
              </td>
              <td>K{{r.box}}</td>
              <td>{{r.correct_count}} / {{r.wrong_count}}</td>
              <td class="mono">{{r.next_due_at}}</td>
              <td>
                <div class="btnrow">
                  <a href="{{ url_for('edit', word_id=r.id) }}">
                    <button type="button" class="secondary">Düzenle</button>
                  </a>

                  <form method="post" onsubmit="return confirm('Silinsin mi?');">
                    <input type="hidden" name="action" value="delete">
                    <input type="hidden" name="word_id" value="{{r.id}}">
                    <button type="submit" class="secondary">Sil</button>
                  </form>
                </div>
              </td>
            </tr>
          {% endfor %}
          </tbody>
        </table>
      {% endif %}
    </div>
    """
    return page(body, rows=rows, sort=sort, dir=direction)

@app.route("/play", methods=["GET", "POST"])
def play_setup():
    if request.method == "POST":
        pool = request.form.get("pool", "due")
        mode = request.form.get("mode", "de->tr")
        n = request.form.get("n", "15")
        try:
            n_int = max(1, min(100, int(n)))
        except:
            n_int = 15
        start_game(mode=mode, pool=pool, n=n_int)
        return redirect(url_for("play_question"))

    s = stats()
    body = """
    <div class="card">
      <h2>Oyun Kur</h2>
      <p class="muted">Vadesi gelen: <b>{{s.due}}</b> / Toplam: <b>{{s.total}}</b></p>
      <form method="post">
        <label>Havuz</label>
        <select name="pool">
          <option value="due">Vadesi gelenler</option>
          <option value="random">Rastgele</option>
        </select>

        <label style="margin-top:10px;">Mod</label>
        <select name="mode">
          <option value="de->tr">🇩🇪 → 🇹🇷 (Almanca sor)</option>
          <option value="tr->de">🇹🇷 → 🇩🇪 (Türkçe sor)</option>
          <option value="mixed">Karışık</option>
        </select>

        <label style="margin-top:10px;">Soru sayısı (1-100)</label>
        <input name="n" value="15">

        <div style="margin-top:14px;">
          <button type="submit">Başlat</button>
        </div>
      </form>
    </div>
    """
    return page(body, s=s)

@app.route("/play/q", methods=["GET", "POST"])
def play_question():
    g = session.get("game")
    if not g:
        flash("Önce oyunu başlat.", "warn")
        return redirect(url_for("play_setup"))

    w = get_current_word()
    if not w:
        return redirect(url_for("play_done"))

    mode = g.get("mode", "de->tr")
    if mode == "mixed":
        mode = random.choice(["de->tr", "tr->de"])

    trs = get_translations(int(w["id"]))
    prompt = ""
    correct_list = []
    answer_lang = ""

    if mode == "de->tr":
        prompt = f"🇩🇪 <b>{w['german']}</b> → 🇹🇷 ?"
        correct_list = trs[:]
        answer_lang = "tr"
    else:
        tr_pick = random.choice(trs) if trs else "(Türkçe karşılık yok)"
        session["game_tr_pick"] = tr_pick
        prompt = f"🇹🇷 <b>{tr_pick}</b> → 🇩🇪 ?"
        correct_list = [w["german"]]
        answer_lang = "de"

    feedback = g.get("last_feedback")
    example = w["example"]
    notes = w["notes"]

    if request.method == "POST":
        user_answer = (request.form.get("answer", "") or "").strip()

        if not user_answer:
            new_box, due = update_after_answer(int(w["id"]), was_correct=False, current_box=int(w["box"]))
            g["last_feedback"] = {
                "kind": "warn",
                "msg": f"⏭️ Pas. Doğru(lar): <b>{', '.join(correct_list) if correct_list else '—'}</b> | Kutu → <b>K{new_box}</b>"
            }
            session["game"] = g
            advance_game(False)
            return redirect(url_for("play_question"))

        best_sim = 0.0
        matched = False
        best_target = None

        for target in correct_list:
            ok, sim = is_close_enough(user_answer, target, lang=answer_lang)
            if sim > best_sim:
                best_sim = sim
                best_target = target
            if ok:
                matched = True
                break

        if matched:
            new_box, due = update_after_answer(int(w["id"]), was_correct=True, current_box=int(w["box"]))
            exact = (answer_lang == "de" and de_normalize(user_answer) == de_normalize(best_target or "")) or \
                    (answer_lang == "tr" and tr_normalize(user_answer) == tr_normalize(best_target or ""))

            extra = "" if exact else f" (yakın doğru, benzerlik: {best_sim:.2f})"
            g["last_feedback"] = {
                "kind": "ok",
                "msg": f"✅ Doğru{extra}! Doğru(lar): <b>{', '.join(correct_list) if correct_list else '—'}</b> | Kutu → <b>K{new_box}</b>"
            }
            session["game"] = g
            advance_game(True)
        else:
            new_box, due = update_after_answer(int(w["id"]), was_correct=False, current_box=int(w["box"]))
            g["last_feedback"] = {
                "kind": "bad",
                "msg": f"❌ Yanlış. Senin: <b>{user_answer}</b> | Doğru(lar): <b>{', '.join(correct_list) if correct_list else '—'}</b> | Kutu → <b>K{new_box}</b> (benzerlik: {best_sim:.2f})"
            }
            session["game"] = g
            advance_game(False)

        return redirect(url_for("play_question"))

    body = """
    <div class="card">
      <h2>🎮 Oyun</h2>
      <div class="muted">Soru {{g.idx + 1}} / {{g.total}} | Skor: <b>{{g.score}}</b></div>

      {% if feedback %}
        <div class="flash {{feedback.kind}}">{{feedback.msg|safe}}</div>
      {% endif %}

      <div class="card">
        <div style="font-size:1.3rem;">{{prompt|safe}}</div>
        {% if example %}<div class="muted" style="margin-top:6px;">Örnek: {{example}}</div>{% endif %}
        {% if notes %}<div class="muted">Not: {{notes}}</div>{% endif %}

        <form method="post" style="margin-top:12px;">
          <input name="answer" autofocus placeholder="Cevabın (boş=pas)">
          <div style="margin-top:10px;">
            <button type="submit">Gönder</button>
            <a href="{{ url_for('play_done') }}"><button type="button" class="secondary">Bitir</button></a>
          </div>
        </form>
      </div>
    </div>
    """
    return page(body, g=g, prompt=prompt, feedback=feedback, example=example, notes=notes)

@app.route("/play/done")
def play_done():
    g = session.get("game")
    if not g:
        flash("Aktif oyun yok.", "warn")
        return redirect(url_for("play_setup"))

    score = int(g.get("score", 0))
    total = int(g.get("total", 0))
    pool = g.get("pool", "due")
    mode = g.get("mode", "de->tr")

    session.pop("game", None)
    session.pop("game_tr_pick", None)

    body = """
    <div class="card">
      <h2>Oyun Bitti</h2>
      <p style="font-size:1.4rem;"><b>Skor: {{score}} / {{total}}</b></p>
      <p class="muted">Havuz: {{pool}} | Mod: {{mode}}</p>
      <p>
        <a href="{{ url_for('play_setup') }}"><button>Tekrar Oyna</button></a>
        <a href="{{ url_for('words') }}"><button class="secondary">Kelimelere Git</button></a>
      </p>
    </div>
    """
    return page(body, score=score, total=total, pool=pool, mode=mode)

@app.route("/stats")
def stats_page():
    s = stats()
    body = """
    <div class="card">
      <h2>📊 İstatistik</h2>
      <p>Toplam: <b>{{s.total}}</b> | Vadesi gelen: <b>{{s.due}}</b></p>
      <p>Kutular:</p>
      <ul>
        <li>Kutu 1: {{ s.boxes.get(1,0) }}</li>
        <li>Kutu 2: {{ s.boxes.get(2,0) }}</li>
        <li>Kutu 3: {{ s.boxes.get(3,0) }}</li>
        <li>Kutu 4: {{ s.boxes.get(4,0) }}</li>
        <li>Kutu 5: {{ s.boxes.get(5,0) }}</li>
      </ul>
      <div class="muted">Not: “vade” UTC saatine göre tutuluyor (kolaylık olsun diye).</div>
    </div>
    """
    return page(body, s=s)

@app.route("/seed")
def seed():
    demo = [
        ("der Apfel", ["elma"], "Ich esse einen Apfel.", "isim"),
        ("gehen", ["gitmek", "yürümek"], "Wir gehen nach Hause.", "fiil"),
        ("schön", ["güzel", "hoş"], "Das ist ein schönes Bild.", "sıfat"),
        ("bekommen", ["almak", "elde etmek"], "Ich bekomme ein Geschenk.", "fiil"),
    ]
    for g, trs, ex, note in demo:
        try:
            create_word(g, trs, ex, note)
        except:
            pass
    flash("✅ Demo kelimeler eklendi.", "ok")
    return redirect(url_for("words"))


if __name__ == "__main__":
    init_db()
    migrate_if_needed()
    app.run(debug=True)

