"""매일 저널 RSS + Crossref를 훑어 (반응 × 재료) 키워드에 맞는 신규 논문을 data/에 저장."""
import json, re, time, html, datetime as dt, pathlib, urllib.parse
import feedparser, requests

ROOT = pathlib.Path(__file__).parent
DATA = ROOT / "data"; DATA.mkdir(exist_ok=True)
FEEDS = json.loads((ROOT / "feeds.json").read_text())
KW = json.loads((ROOT / "keywords.json").read_text())
SEEN_F = DATA / "seen.json"
seen = set(json.loads(SEEN_F.read_text())) if SEEN_F.exists() else set()
UA = {"User-Agent": "paper-radar/1.0 (mailto:amy19980724@gmail.com)"}
KST = dt.timezone(dt.timedelta(hours=9))
today = dt.datetime.now(KST).date()
since = today - dt.timedelta(days=3)   # 색인 지연 대비 3일 겹쳐서 조회, seen으로 중복 제거

def comp(d): return {k: re.compile("|".join(v), re.I) for k, v in d.items()}
RX, MX, STAR = comp(KW["reactions"]), comp(KW["materials"]), re.compile("|".join(KW["starred"]), re.I)
clean = lambda s: re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html.unescape(s or ""))).strip()

def classify(text):
    r = [k for k, x in RX.items() if x.search(text)]
    m = [k for k, x in MX.items() if x.search(text)]
    return r, m

hits, status = [], []
def add(journal, title, link, doi, date, abstract, src):
    key = (doi or link or title).lower()
    if key in seen: return
    text = f"{title} {abstract}"
    r, m = classify(text)
    if not (r and m): return
    seen.add(key)
    hits.append(dict(journal=journal, title=title, link=link, doi=doi, date=date,
                     reactions=r, materials=m, starred=bool(STAR.search(text)),
                     abstract=abstract[:1500], source=src))

# 1) RSS
for f in FEEDS:
    try:
        resp = requests.get(f["url"], headers=UA, timeout=30)
        feed = feedparser.parse(resp.content)
        n = len(feed.entries)
        status.append(f"{f['journal']}: RSS {resp.status_code}, {n}건")
        for e in feed.entries:
            doi = (e.get("prism_doi") or e.get("dc_identifier") or "").replace("doi:", "")
            if not doi:
                mm = re.search(r"10\.\d{4,9}/[^\s?#]+", e.get("link", "") + " " + e.get("id", ""))
                doi = mm.group(0) if mm else ""
            add(f["journal"], clean(e.get("title")), e.get("link", ""), doi,
                e.get("published", e.get("updated", "")), clean(e.get("summary", "")), "rss")
    except Exception as ex:
        status.append(f"{f['journal']}: RSS 실패 ({type(ex).__name__})")
    time.sleep(1)

# 2) Crossref 보완 (RSS가 막힌 저널도 잡도록, 저널별 순차 조회)
for f in FEEDS:
    url = (f"https://api.crossref.org/journals/{f['issn']}/works?"
           f"filter=from-created-date:{since},type:journal-article&rows=200"
           f"&select=DOI,title,abstract,created&mailto=amy19980724@gmail.com")
    try:
        r = requests.get(url, headers=UA, timeout=60)
        if r.status_code == 429:
            time.sleep(30); r = requests.get(url, headers=UA, timeout=60)
        items = r.json()["message"]["items"] if r.ok else []
        status.append(f"{f['journal']}: Crossref {r.status_code}, {len(items)}건")
        for it in items:
            t = clean((it.get("title") or [""])[0]); doi = it.get("DOI", "")
            add(f["journal"], t, f"https://doi.org/{doi}", doi,
                it.get("created", {}).get("date-time", ""), clean(it.get("abstract", "")), "crossref")
    except Exception as ex:
        status.append(f"{f['journal']}: Crossref 실패 ({type(ex).__name__})")
    time.sleep(3)

hits.sort(key=lambda h: (not h["starred"], h["journal"]))
out = {"run_date_kst": str(today), "count": len(hits), "papers": hits, "feed_status": status}
(DATA / "latest.json").write_text(json.dumps(out, ensure_ascii=False, indent=1))
(DATA / f"{today}.json").write_text(json.dumps(out, ensure_ascii=False, indent=1))
SEEN_F.write_text(json.dumps(sorted(seen)[-20000:]))

md = [f"# {today} 신규 논문 ({len(hits)}편)\n"]
for h in hits:
    md.append(f"- {'★ ' if h['starred'] else ''}**{h['title']}** — *{h['journal']}* ({h['date'][:10]})  \n"
              f"  {'/'.join(h['reactions'])} × {'/'.join(h['materials'])} · {h['link']}")
md.append("\n<details><summary>수집 상태</summary>\n\n" + "\n".join(f"- {s}" for s in status) + "\n</details>")
(DATA / "latest.md").write_text("\n".join(md))
print("\n".join(status)); print(f"HITS: {len(hits)}")
