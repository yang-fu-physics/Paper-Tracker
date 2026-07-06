"""
从 arxiv_push 的 pushed_papers.db 导入数据到 ./data/papers.db
用法: python import.py /path/to/pushed_papers.db
"""
import sqlite3
import sys
from pathlib import Path

src = Path(sys.argv[1]) if len(sys.argv) > 1 else None
if not src or not src.exists():
    print("用法: python import.py /path/to/pushed_papers.db")
    sys.exit(1)

dst = Path(__file__).parent / "data" / "papers.db"
dst.parent.mkdir(parents=True, exist_ok=True)

with sqlite3.connect(src) as s, sqlite3.connect(dst) as d:
    d.executescript("""
        CREATE TABLE IF NOT EXISTS pushed_papers (
            url TEXT PRIMARY KEY, journal TEXT, title TEXT, pushed_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS paper_evaluations (
            url TEXT PRIMARY KEY, is_transport INTEGER NOT NULL,
            title_zh TEXT, abstract_zh TEXT, evaluated_at TEXT NOT NULL,
            summary_zh TEXT, abstract_en TEXT
        );
    """)
    try:
        d.execute("ALTER TABLE paper_evaluations ADD COLUMN summary_zh TEXT")
    except sqlite3.OperationalError: pass
    try:
        d.execute("ALTER TABLE paper_evaluations ADD COLUMN abstract_en TEXT")
    except sqlite3.OperationalError: pass

    rows = s.execute("SELECT url, journal, title, pushed_at FROM pushed_papers").fetchall()
    d.executemany("INSERT OR IGNORE INTO pushed_papers VALUES (?,?,?,?)", rows)
    
    cur = s.cursor()
    cur.execute("PRAGMA table_info(paper_evaluations)")
    cols = [col[1] for col in cur.fetchall()]
    has_new = 'summary_zh' in cols and 'abstract_en' in cols
    
    if has_new:
        rows = s.execute("SELECT url, is_transport, title_zh, abstract_zh, evaluated_at, summary_zh, abstract_en FROM paper_evaluations").fetchall()
    else:
        rows = [(*r, "", "") for r in s.execute("SELECT url, is_transport, title_zh, abstract_zh, evaluated_at FROM paper_evaluations").fetchall()]
        
    d.executemany("INSERT OR REPLACE INTO paper_evaluations VALUES (?,?,?,?,?,?,?)", rows)
    print(f"导入完成 → {dst}")
