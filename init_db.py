# -*- coding: utf-8 -*-
"""初始化空的索引库 pan_index.db（可重复运行）。

用法:
    python init_db.py [--db PATH]

仓库里不附带任何索引数据，第一次运行后是一个空库，
需要靠 crawl_channels.py / crawl_full.py 或实时搜索慢慢积累。
"""
import argparse
import os
import sqlite3

BASE = os.path.dirname(os.path.abspath(__file__))

SCHEMA = '''
CREATE TABLE IF NOT EXISTS items(
  url TEXT PRIMARY KEY,
  title TEXT,
  disk TEXT,
  code TEXT DEFAULT '',
  size TEXT DEFAULT '',
  kw TEXT DEFAULT '',
  source TEXT DEFAULT '',
  first_seen TEXT,
  valid INTEGER DEFAULT NULL,
  needs_code TEXT DEFAULT '',
  checked_at TEXT,
  probe TEXT,
  probe_at TEXT,
  size_mb INTEGER,
  share_time TEXT,
  title_key TEXT,
  dup_of TEXT,
  access TEXT DEFAULT 'unknown',
  probe_tree TEXT,
  tree_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_items_disk   ON items(disk);
CREATE INDEX IF NOT EXISTS idx_items_valid  ON items(valid);
CREATE INDEX IF NOT EXISTS idx_items_access ON items(access);
CREATE INDEX IF NOT EXISTS idx_items_dup    ON items(dup_of);

CREATE TABLE IF NOT EXISTS saved(
  url TEXT PRIMARY KEY,
  title TEXT,
  disk TEXT,
  code TEXT DEFAULT '',
  size_mb INTEGER,
  share_time TEXT,
  source TEXT,
  note TEXT DEFAULT '',
  tags TEXT DEFAULT '',
  saved_at TEXT,
  probe TEXT DEFAULT '',
  access TEXT DEFAULT 'unknown'
);

CREATE INDEX IF NOT EXISTS idx_saved_tags  ON saved(tags);
CREATE INDEX IF NOT EXISTS idx_saved_disk  ON saved(disk);
CREATE INDEX IF NOT EXISTS idx_saved_at    ON saved(saved_at);

CREATE TABLE IF NOT EXISTS channel_cursor(
  ch TEXT PRIMARY KEY,
  last_id TEXT,
  updated TEXT
);

CREATE TABLE IF NOT EXISTS tag_defs(
  name TEXT PRIMARY KEY,
  created_at TEXT
);
'''


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--db', default=os.path.join(BASE, 'pan_index.db'))
    args = ap.parse_args()
    con = sqlite3.connect(args.db, timeout=15)
    try:
        con.executescript(SCHEMA)
        con.commit()
    finally:
        con.close()
    print('initialized:', args.db)


if __name__ == '__main__':
    main()
