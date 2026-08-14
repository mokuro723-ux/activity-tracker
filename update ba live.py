#!/usr/bin/env python3
"""
ブルアカらいぶ／メンテナンス 自動データ更新スクリプト
====================================================
神ゲー攻略・game8の複数ページを巡回し、index.html内の
BA_LIVE_HISTORY / BA_REGULAR_HISTORY / BA_REGULAR_MAINTE_EXTRA を更新する。

設計方針（2026年7月のファクトチェックで判明した知見を反映）:
- 通常らいぶ→メンテは「+2日後・水曜11:00〜17:00」が基本パターン
  （利用者証言「基本は2週間に1度、水曜日の11時〜17時」で裏付け済み）
- 周年・ハーフ周年らいぶは複数日開催のことがあり、後夜祭は「最終日の翌日」
- 周年系は「1回目メンテ（周年開始）」の約14日後に「2回目メンテ（大型・エリア追加等）」が来る
- メンテ日はページ内の断片的な記述から抽出するため、複数パターンで拾い、
  取れなければ確定済みパターンから推定する
- 週1実行を前提に、直近30件程度のページを見れば取りこぼしはほぼ無い想定
"""

import re
import sys
import time
import datetime
import urllib.request
import urllib.error

# ────────────────────────────────────────────────
# 設定
# ────────────────────────────────────────────────
INDEX_HTML = "index.html"
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

# 巡回先（複数ソースで裏取りする）
# 巡回先（複数ソースで裏取りする。個別記事はページ移動・統合されやすいため、
# らいぶ情報も複数URLを試して合算する設計にしている）
LIVE_LIST_URLS = [
    "https://game8.jp/blue-archive/637876",          # ブルアカらいぶ最新情報まとめ
    "https://game8.jp/blue-archive/640082",          # 最新情報まとめ（放送告知も載ることがある）
]
MAINTE_INFO_URLS = [
    "https://game8.jp/blue-archive/650323",          # メンテ・アップデート最新情報
    "https://kamigame.jp/bluearchive/page/143252335034956337.html",  # メンテ最新情報
    "https://game8.jp/blue-archive/640082",          # 最新情報まとめ（メンテ後実装の記述が多い）
]

# 周年・ハーフ周年月（1月=周年, 7月=ハーフ周年）
ANNIV_MONTHS = {1, 7}

DOW_JP = ["日", "月", "火", "水", "木", "金", "土"]

# ────────────────────────────────────────────────
# 日付ユーティリティ
# ────────────────────────────────────────────────
def fmt(d):
    return d.strftime("%Y-%m-%d")

def weekday_to_dow(d):
    """datetime.date -> 0=日,1=月...6=土"""
    return (d.weekday() + 1) % 7

def dow_jp(d):
    return DOW_JP[weekday_to_dow(d)]

# ────────────────────────────────────────────────
# HTTP fetch（リトライ付き）
# ────────────────────────────────────────────────
def fetch(url, retries=3):
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
    }
    req = urllib.request.Request(url, headers=headers)
    for i in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                return r.read().decode("utf-8", errors="replace")
        except Exception as e:
            print(f"  fetch error ({i+1}/{retries}) {url}: {e}", file=sys.stderr)
            time.sleep(3)
    return ""

def _guess_year(this_year, month, day):
    """年表記のないM/D文字列に対し、今日から半年以上離れないよう年を推定する"""
    live_date = datetime.date(this_year, month, day)
    today = datetime.date.today()
    if (live_date - today).days > 200:
        live_date = datetime.date(this_year - 1, month, day)
    elif (today - live_date).days > 200:
        live_date = datetime.date(this_year + 1, month, day)
    return live_date

# ────────────────────────────────────────────────
# 1) 通常らいぶ・周年らいぶの新規放送を検出
# ────────────────────────────────────────────────
def extract_live_broadcasts(html):
    """
    ページ内から「ブルアカらいぶ」関連の"放送告知"の日付を検出する。
    時刻が本文になくても日付だけで拾えるようにし（見つからなければ19:00をデフォルト値とする）、
    その代わり「配信」「放送」「開催」等の告知語が近くにあるものだけを候補として残す
    （そうしないと「メンテ後の実装内容紹介」のような無関係な日付まで拾ってしまうため）。

    対応パターン例：
      「ブルアカらいぶ！ざ☆すたーとおぶさまー！SP」が2026/6/20(土)19:00より配信されています。
      「夏のブルアカらいぶ！さんしゃいんさまーぱーてぃー！SP」を2025年7月19日（土）18:30から配信
      ブルアカらいぶが2026年7月18日(土)19時より放送決定！
      生放送「ブルアカらいぶ」は7/18(土)19:00から配信予定です。
      ブルアカらいぶ開催決定！2026年8月2日(日)19:00〜（時刻表記が本文と離れているケースにも対応）
    """
    results = []

    # 「ブルアカらいぶ」というキーワードの前後200文字を「放送関連の文脈」として切り出す
    context_windows = []
    for m in re.finditer(r'ブルアカ\s*らいぶ|ブルーアーカイブ\s*らいぶ', html):
        start = max(0, m.start() - 100)
        end = min(len(html), m.end() + 200)
        context_windows.append(html[start:end])

    # 告知として扱ってよい文脈かどうかの判定語（この語が窓内になければ日付だけあっても除外）
    ANNOUNCE_KEYWORDS = ("配信", "放送", "生放送")
    # 「メンテ後の実装内容紹介」など、らいぶ告知ではない文脈を除外するための語
    EXCLUDE_KEYWORDS = ("メンテ後", "メンテナンス後", "実装されます", "追加されます")

    # 日付＋時刻の抽出パターン（時刻は任意）
    date_time_patterns = [
        # YYYY/M/D(曜)HH:MM または YYYY年M月D日（曜）HH時MM分
        r'(\d{4})[年/](\d{1,2})[月/](\d{1,2})日?[（(][^)）]{1,3}[）)]\s*(\d{1,2})[:時](\d{2})',
        # YYYY年M月D日（曜）HH時（分表記なし・「19時より」等）
        r'(\d{4})[年/](\d{1,2})[月/](\d{1,2})日?[（(][^)）]{1,3}[）)]\s*(\d{1,2})時(?!\d)',
        # 年なし: M/D(曜)HH:MM
        r'(?<!\d)(\d{1,2})/(\d{1,2})[（(][^)）]{1,3}[）)]\s*(\d{1,2}):(\d{2})',
        # 年なし: M/D(曜)HH時（分表記なし）
        r'(?<!\d)(\d{1,2})/(\d{1,2})[（(][^)）]{1,3}[）)]\s*(\d{1,2})時(?!\d)',
        # 日付のみ、時刻表記なし（YYYY/M/D(曜) or YYYY年M月D日（曜）単体）
        r'(\d{4})[年/](\d{1,2})[月/](\d{1,2})日?[（(][^)）]{1,3}[）)](?!\s*\d{1,2}[:時])',
        # 日付のみ、年なし（M/D(曜) 単体）
        r'(?<!\d)(\d{1,2})/(\d{1,2})[（(][^)）]{1,3}[）)](?!\s*\d{1,2}[:時])',
    ]

    this_year = datetime.date.today().year

    for ctx in context_windows:
        # このウィンドウが「告知」らしい文脈かどうか（告知語が1つも無ければスキップ）
        if not any(kw in ctx for kw in ANNOUNCE_KEYWORDS):
            continue
        # 「メンテ後の実装紹介」等の無関係な文脈は除外
        if any(kw in ctx for kw in EXCLUDE_KEYWORDS):
            continue

        # タイトルは「ブルアカらいぶ」キーワードに最も近い鍵括弧を優先して探す
        kw_pos = ctx.find('らいぶ')
        title_candidates = list(re.finditer(r'[「『]([^」』]{3,40})[」』]', ctx))
        if title_candidates:
            title_m = min(title_candidates, key=lambda m: abs(m.start() - kw_pos)) if kw_pos >= 0 else title_candidates[0]
            default_title = title_m.group(1).strip()
        else:
            default_title = None

        for pat in date_time_patterns:
            for m in re.finditer(pat, ctx):
                groups = m.groups()
                hh, mm = "19", "00"  # 時刻不明時のデフォルト（通常らいぶの最多開始時刻）
                try:
                    if len(groups) == 5:
                        y, mo, d, hh, mm = groups
                        live_date = datetime.date(int(y), int(mo), int(d))
                    elif len(groups) == 4 and len(groups[0]) == 4:
                        # YYYY年M月D日HH時（分なし）
                        y, mo, d, hh = groups
                        mm = "00"
                        live_date = datetime.date(int(y), int(mo), int(d))
                    elif len(groups) == 4:
                        # M/D(曜)HH:MM（年なし）
                        mo, d, hh, mm = groups
                        live_date = _guess_year(this_year, int(mo), int(d))
                    elif len(groups) == 3:
                        # YYYY年M月D日（曜）単体（時刻なし）
                        y, mo, d = groups
                        live_date = datetime.date(int(y), int(mo), int(d))
                    else:
                        # M/D(曜) 単体（年なし・時刻なし）
                        mo, d = groups
                        live_date = _guess_year(this_year, int(mo), int(d))
                except ValueError:
                    continue

                title = default_title or f"ブルアカらいぶ {fmt(live_date)}"

                results.append({
                    "title": title,
                    "date": fmt(live_date),
                    "time": f"{int(hh):02d}:{mm}",
                    "is_anniv_month": live_date.month in ANNIV_MONTHS,
                })

    # 重複排除（同じ日付は1件に）
    seen = set()
    dedup = []
    for r in results:
        if r["date"] in seen:
            continue
        seen.add(r["date"])
        dedup.append(r)
    return dedup

# ────────────────────────────────────────────────
# 2) メンテ日の抽出（複数ソース共通）
# ────────────────────────────────────────────────
def extract_mainte_dates(html, after_date=None, within_days=20):
    """
    ページ内から「M/D(曜)メンテ」「YYYY/M/D(曜)メンテ」等の記述を全部拾う。
    after_date が指定されていれば、その日から within_days 日以内のものだけ返す。
    ※ within_days=20 は公式サイトの「定期メンテナンスはおよそ14日〜20日のペースで
      行われます」という明記に基づくデフォルト値。

    「メンテ」という語が日付の前後どちらに来ても拾えるよう、
    まず「メンテ」キーワード周辺のテキストを切り出してから日付を探す方式に変更。
    """
    found = []
    this_year = datetime.date.today().year

    # 「メンテ」関連キーワードの前後150文字を候補テキストとして切り出す
    mainte_windows = []
    for m in re.finditer(r'メンテナンス|メンテ(?!ナンス中)', html):
        start = max(0, m.start() - 100)
        end = min(len(html), m.end() + 100)
        mainte_windows.append(html[start:end])

    date_patterns = [
        # YYYY/M/D(曜) or YYYY年M月D日（曜）
        r'(\d{4})[年/](\d{1,2})[月/](\d{1,2})日?(?:[（(][^)）]{1,3}[）)])?',
        # M/D(曜) ※年なし
        r'(?<!\d)(\d{1,2})/(\d{1,2})[（(][^)）]{1,3}[）)]',
        # M月D日（曜）※年なし
        r'(?<!\d)(\d{1,2})月(\d{1,2})日[（(][^)）]{1,3}[）)]',
    ]

    for window in mainte_windows:
        for pat in date_patterns:
            for m in re.finditer(pat, window):
                groups = m.groups()
                try:
                    if len(groups) == 3:
                        d = datetime.date(int(groups[0]), int(groups[1]), int(groups[2]))
                    else:
                        # 年不明。after_dateがあればその年、なければ今年で仮置きし、
                        # 半年以上のズレがあれば前後の年に補正
                        base_year = after_date.year if after_date else this_year
                        d = datetime.date(base_year, int(groups[0]), int(groups[1]))
                        ref = after_date or datetime.date.today()
                        if (d - ref).days > 200:
                            d = datetime.date(base_year - 1, int(groups[0]), int(groups[1]))
                        elif (ref - d).days > 200:
                            d = datetime.date(base_year + 1, int(groups[0]), int(groups[1]))
                except ValueError:
                    continue
                if after_date and not (after_date <= d <= after_date + datetime.timedelta(days=within_days)):
                    continue
                found.append(d)

    return sorted(set(found))

def extract_gacha_switch_dates(html, after_date=None, within_days=20):
    """
    周年の「2回目メンテ（大型・エリア追加やガチャ切替）」はメンテ関連語を伴わない
    記述になることがある（例：「記念限定募集は8月5日(水)11:00から」）ため、
    ガチャ・排出率関連のキーワード周辺から日付を拾う専用関数。
    extract_mainte_dates で見つからなかった場合のフォールバックとして使う想定。
    """
    found = []
    this_year = datetime.date.today().year

    gacha_windows = []
    for m in re.finditer(r'限定募集|フェスガチャ|排出率|排出確率', html):
        start = max(0, m.start() - 100)
        end = min(len(html), m.end() + 100)
        gacha_windows.append(html[start:end])

    date_patterns = [
        r'(\d{4})[年/](\d{1,2})[月/](\d{1,2})日?(?:[（(][^)）]{1,3}[）)])?',
        r'(?<!\d)(\d{1,2})/(\d{1,2})[（(][^)）]{1,3}[）)]',
        r'(?<!\d)(\d{1,2})月(\d{1,2})日[（(][^)）]{1,3}[）)]',
    ]

    for window in gacha_windows:
        for pat in date_patterns:
            for m in re.finditer(pat, window):
                groups = m.groups()
                try:
                    if len(groups) == 3:
                        d = datetime.date(int(groups[0]), int(groups[1]), int(groups[2]))
                    else:
                        base_year = after_date.year if after_date else this_year
                        d = datetime.date(base_year, int(groups[0]), int(groups[1]))
                        ref = after_date or datetime.date.today()
                        if (d - ref).days > 200:
                            d = datetime.date(base_year - 1, int(groups[0]), int(groups[1]))
                        elif (ref - d).days > 200:
                            d = datetime.date(base_year + 1, int(groups[0]), int(groups[1]))
                except ValueError:
                    continue
                if after_date and not (after_date <= d <= after_date + datetime.timedelta(days=within_days)):
                    continue
                found.append(d)

    return sorted(set(found))

def estimate_mainte_date(live_date, is_anniv):
    """
    実測できなかった場合の推定。
    通常らいぶ: +2日、水曜優先（確定パターン）
    周年らいぶ: +1〜3日、月・火曜優先
    """
    if is_anniv:
        base = live_date + datetime.timedelta(days=2)
        preferred = [1, 2]  # 月・火
    else:
        base = live_date + datetime.timedelta(days=2)
        preferred = [3, 2]  # 水・火

    for offset in [0, 1, -1, 2, -2]:
        d = base + datetime.timedelta(days=offset)
        if weekday_to_dow(d) in preferred:
            return d
    return base

# ────────────────────────────────────────────────
# 3) index.html の既存データ読み取り
# ────────────────────────────────────────────────
def extract_existing_dates(html, var_name):
    block_m = re.search(rf'const {var_name}\s*=\s*\[(.*?)\];', html, re.S)
    if not block_m:
        return set()
    return set(re.findall(r'"(\d{4}-\d{2}-\d{2})"', block_m.group(1)))

def extract_existing_live_titles(html):
    """BA_LIVE_HISTORY / BA_REGULAR_HISTORY 双方のタイトル文字列を集める（重複判定の補助）"""
    titles = set()
    for var in ("BA_LIVE_HISTORY", "BA_REGULAR_HISTORY"):
        block_m = re.search(rf'const {var}\s*=\s*\[(.*?)\];', html, re.S)
        if block_m:
            titles |= set(re.findall(r'label:"([^"]+)"', block_m.group(1)))
    return titles

# ────────────────────────────────────────────────
# 4) index.html への追記
# ────────────────────────────────────────────────
def insert_anniv_entry(html, live_date, live_time, title, mainte_date, mainte2_date=None):
    """周年系エントリ（live/kouyasai/mainte/mainte2）をBA_LIVE_HISTORYに追記"""
    offset = (mainte_date - live_date).days
    kouyasai = live_date + datetime.timedelta(days=1)
    lines = [
        f'  {{ label:"{title}", date:"{fmt(live_date)}", time:"{live_time}", note:"自動取得", type:"live" }},',
        f'  {{ label:"{title}後夜祭", date:"{fmt(kouyasai)}", time:null, note:"らいぶ翌日", type:"kouyasai" }},',
        f'  {{ label:"{title}メンテ", date:"{fmt(mainte_date)}", time:"11:00", note:"らいぶから{offset}日後（{dow_jp(mainte_date)}）自動取得", type:"mainte" }},',
    ]
    if mainte2_date:
        offset2 = (mainte2_date - mainte_date).days
        lines.append(
            f'  {{ label:"{title}大型メンテ", date:"{fmt(mainte2_date)}", time:"11:00", note:"メンテから{offset2}日後（{dow_jp(mainte2_date)}）自動取得・推定含む", type:"mainte2" }},'
        )
    new_block = "\n".join(lines) + "\n"
    def _replace(m):
        existing = m.group(2)
        # 既存内容の末尾に改行がなければ補う（連結時に1行化するのを防ぐ）
        if existing and not existing.endswith("\n"):
            existing += "\n"
        return m.group(1) + existing + new_block + m.group(3).lstrip("\n")
    return re.sub(
        r'(const BA_LIVE_HISTORY\s*=\s*\[)(.*?)(\n\];)',
        _replace,
        html, flags=re.S, count=1
    )

def insert_regular_entry(html, live_date, live_time, title, mainte_date):
    """通常らいぶエントリをBA_REGULAR_HISTORYに追記"""
    offset = (mainte_date - live_date).days
    entry = (
        f'  {{ label:"{live_date.year}/{live_date.month:02d}/{live_date.day:02d} {title[:16]}", '
        f'liveDate:"{fmt(live_date)}", mainteDate:"{fmt(mainte_date)}", '
        f'offset:{offset}, liveTime:"{live_time}" }},\n'
    )
    def _replace(m):
        existing = m.group(2)
        if existing and not existing.endswith("\n"):
            existing += "\n"
        return m.group(1) + existing + entry + m.group(3).lstrip("\n")
    return re.sub(
        r'(const BA_REGULAR_HISTORY\s*=\s*\[)(.*?)(\n\];)',
        _replace,
        html, flags=re.S, count=1
    )

def insert_regular_mainte_extra(html, dates):
    """BA_REGULAR_HISTORYに紐付かない単発メンテ日をBA_REGULAR_MAINTE_EXTRAに追記"""
    block_m = re.search(r'const BA_REGULAR_MAINTE_EXTRA\s*=\s*\[(.*?)\];', html, re.S)
    if not block_m:
        return html
    existing = set(re.findall(r'"(\d{4}-\d{2}-\d{2})"', block_m.group(1)))
    to_add = sorted(d for d in dates if d not in existing)
    if not to_add:
        return html
    new_lines = "".join(f'  "{d}",\n' for d in to_add)
    def _replace(m):
        existing = m.group(2)
        if existing and not existing.endswith("\n"):
            existing += "\n"
        return m.group(1) + existing + new_lines + m.group(3).lstrip("\n")
    return re.sub(
        r'(const BA_REGULAR_MAINTE_EXTRA\s*=\s*\[)(.*?)(\n\];)',
        _replace,
        html, flags=re.S, count=1
    )

# ────────────────────────────────────────────────
# メイン処理
# ────────────────────────────────────────────────
def main():
    print("=== ブルアカらいぶ／メンテ 自動更新開始 ===")
    print(f"実行日時: {datetime.datetime.now().isoformat()}")

    try:
        with open(INDEX_HTML, encoding="utf-8") as f:
            html = f.read()
    except FileNotFoundError:
        print(f"ERROR: {INDEX_HTML} が見つかりません", file=sys.stderr)
        sys.exit(1)

    existing_live_dates = extract_existing_dates(html, "BA_LIVE_HISTORY")
    existing_regular_dates = set()
    block_m = re.search(r'const BA_REGULAR_HISTORY\s*=\s*\[(.*?)\];', html, re.S)
    if block_m:
        existing_regular_dates = set(re.findall(r'liveDate:"(\d{4}-\d{2}-\d{2})"', block_m.group(1)))
    existing_titles = extract_existing_live_titles(html)

    print(f"既存: 周年系ライブ日 {len(existing_live_dates)}件 / 通常ライブ日 {len(existing_regular_dates)}件")

    added = 0

    # ── STEP 1: 新規らいぶ放送の検出 ──
    print(f"\n[1/2] らいぶ情報取得（{len(LIVE_LIST_URLS)}ソースを巡回）")
    live_html_parts = []
    for url in LIVE_LIST_URLS:
        print(f"  取得: {url}")
        h = fetch(url)
        if h:
            live_html_parts.append(h)
        else:
            print(f"    WARNING: 取得失敗", file=sys.stderr)
        time.sleep(1)
    live_html = "\n".join(live_html_parts)
    new_mainte_singles = []  # BA_REGULAR_HISTORYに紐付かないメンテ日の候補

    if live_html:
        try:
            broadcasts = extract_live_broadcasts(live_html)
            print(f"  検出した放送: {len(broadcasts)}件")

            for b in broadcasts:
                already_known = (
                    b["date"] in existing_live_dates
                    or b["date"] in existing_regular_dates
                    or b["title"] in existing_titles
                )
                if already_known:
                    continue

                live_date = datetime.date.fromisoformat(b["date"])
                print(f"  新規放送候補: {b['date']} {b['title'][:30]} ({'周年月' if b['is_anniv_month'] else '通常'})")

                # メンテ日を実データから探す（放送ページ本文＋メンテ専用ページの両方をあたる）
                mainte_candidates = extract_mainte_dates(live_html, after_date=live_date, within_days=10)
                if not mainte_candidates:
                    for murl in MAINTE_INFO_URLS:
                        mainte_html = fetch(murl)
                        time.sleep(1)
                        if mainte_html:
                            mainte_candidates = extract_mainte_dates(mainte_html, after_date=live_date, within_days=10)
                            if mainte_candidates:
                                break

                if mainte_candidates:
                    mainte_date = mainte_candidates[0]
                    print(f"    → メンテ日実測: {fmt(mainte_date)} ({dow_jp(mainte_date)})")
                else:
                    mainte_date = estimate_mainte_date(live_date, b["is_anniv_month"])
                    print(f"    → メンテ日推定: {fmt(mainte_date)} ({dow_jp(mainte_date)})")

                if b["is_anniv_month"]:
                    # 周年系：2回目メンテ（大型）も推定 or 実測を試みる。
                    # 過去実績は+7日（5.5周年）〜+15日（4.5周年）と幅があるため、
                    # 探索範囲は1回目メンテの翌日から広めに取る。
                    mainte2_candidates = extract_mainte_dates(
                        live_html, after_date=mainte_date + datetime.timedelta(days=1), within_days=20
                    )
                    if not mainte2_candidates:
                        # メンテ関連語を伴わない「限定募集開始」等の記述もフォールバックで拾う
                        mainte2_candidates = extract_gacha_switch_dates(
                            live_html, after_date=mainte_date + datetime.timedelta(days=1), within_days=20
                        )
                    mainte2_date = mainte2_candidates[0] if mainte2_candidates else (mainte_date + datetime.timedelta(days=11))
                    html = insert_anniv_entry(html, live_date, b["time"], b["title"], mainte_date, mainte2_date)
                    existing_live_dates.add(b["date"])
                else:
                    html = insert_regular_entry(html, live_date, b["time"], b["title"], mainte_date)
                    existing_regular_dates.add(b["date"])

                existing_titles.add(b["title"])
                added += 1
        except Exception as e:
            print(f"  WARNING: STEP1でエラー発生（処理は継続します）: {e}", file=sys.stderr)
    else:
        print("  WARNING: らいぶ情報ページの取得に失敗しました（ネットワークまたはサイト側の問題）", file=sys.stderr)

    # ── STEP 2: 単発の定期メンテ実績（らいぶに紐付かないもの）の収集 ──
    print(f"\n[2/2] 単発メンテ情報取得")
    try:
        for url in MAINTE_INFO_URLS:
            print(f"  取得: {url}")
            mhtml = fetch(url)
            if not mhtml:
                print(f"    WARNING: 取得失敗、スキップ", file=sys.stderr)
                continue
            # 直近1年以内のメンテ日を広く拾う
            one_year_ago = datetime.date.today() - datetime.timedelta(days=365)
            dates = extract_mainte_dates(mhtml)
            recent = [d for d in dates if d >= one_year_ago]
            new_mainte_singles.extend(fmt(d) for d in recent)
            time.sleep(1)

        if new_mainte_singles:
            before = len(extract_existing_dates(html, "BA_REGULAR_MAINTE_EXTRA"))
            html = insert_regular_mainte_extra(html, set(new_mainte_singles))
            after = len(extract_existing_dates(html, "BA_REGULAR_MAINTE_EXTRA"))
            if after > before:
                print(f"  → 単発メンテ日を{after - before}件追加")
                added += 1
    except Exception as e:
        print(f"  WARNING: STEP2でエラー発生（処理は継続します）: {e}", file=sys.stderr)

    # ── 保存 ──
    if added > 0:
        # データを実際に変更した場合のみ、画面表示用のビルド日付も今日の日付に更新する
        # （index.html側でこの値を画面下部に小さく表示し、実機がキャッシュで
        #   古い内容を表示していないかを目視確認できるようにしている）
        today_str = fmt(datetime.date.today())
        html = re.sub(
            r'const BUILD_DATE = "[\d-]+";',
            f'const BUILD_DATE = "{today_str}";',
            html
        )
        with open(INDEX_HTML, "w", encoding="utf-8") as f:
            f.write(html)
        print(f"\n=== 更新完了：{added}件の変更を{INDEX_HTML}に反映（BUILD_DATE: {today_str}） ===")
    else:
        print("\n=== 新規データなし。更新はスキップされました ===")

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        # 想定外のエラーでも異常終了コードは返さない
        # （GitHub Actions側でジョブ全体を失敗扱いにしないため。
        #   ログはstep summaryに出るので後から気づける）
        print(f"FATAL: 予期しないエラーが発生しました: {e}", file=sys.stderr)
        import traceback
        traceback.print_exc()
