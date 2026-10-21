"""매일 저널 RSS + Crossref를 훑어 (반응 × 재료) 키워드에 맞는 신규 논문을 data/에 저장.
v2: 게재일 필터(최근 3일), DOI·초록 보강(Crossref/OpenAlex), DOI 기준 중복 제거."""
import json, re, time, html, datetime as dt, pathlib, calendar
import feedparser, requests

ROOT = pathlib.Path(__file__).parent
DATA = ROOT / "data"; DATA.mkdir(exist_ok=True)
FEEDS = json.loads((ROOT / "feeds.json").read_text())
KW = json.loads((ROOT / "keywords.json").read_text())
SEEN_F = DATA / "seen.json"
seen = set(json.loads(SEEN_F.read_text())) if SEEN_F.exists() else set()
MAIL = "amy19980724@gmail.com"
UA = {"User-Agent": f"paper-radar/2.0 (mailto:{MAIL})"}
KST = dt.timezone(dt.timedelta(hours=9))
today = dt.datetime.now(KST).date()
since = today - dt.timedelta(days=3)   # 색인 지연 대비 3일 겹쳐 조회, seen으로 중복 제거

def comp(d): return {k: re.compile("|".join(v), re.I) for k, v in d.items()}
RX, MX, STAR = comp(KW["reactions"]), comp(KW["materials"]), re.compile("|".join(KW["starred"]), re.I)
clean = lambda s: re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html.unescape(s or ""))).strip()
DOI_RX = re.compile(r"10\.\d{4,9}/[^\s?#\"<>]+")

def classify(text):
    return [k for k, x in RX.items() if x.search(text)], [k for k, x in MX.items() if x.search(text)]

def get(url, **kw):
    for i in range(2):
        try:
            r = requests.get(url, headers=UA, timeout=40, **kw)
            if r.status_code == 429: time.sleep(30); continue
            return r
        except Exception: time.sleep(5)
    return None

cands, status = {}, []
def add(journal, title, link, doi, date, abstract, src):
    doi = (doi or "").lower().strip().rstrip(".")
    key = doi or link or title.lower()
    if key in seen or key in cands: return
    r, m = classify(f"{title} {abstract}")
    if not (r and m): return
    cands[key] = dict(journal=journal, title=title, link=link, doi=doi, date=date or "",
                      abstract=abstract, source=src)

def entry_date(e):
    t = e.get("published_parsed") or e.get("updated_parsed")
    return dt.datetime.fromtimestamp(calendar.timegm(t), dt.timezone.utc).isoformat() if t else ""

# 1) RSS
for f in FEEDS:
    r = get(f["url"])
    if r is None: status.append(f"{f['journal']}: RSS 실패"); continue
    feed = feedparser.parse(r.content)
    status.append(f"{f['journal']}: RSS {r.status_code}, {len(feed.entries)}건")
    for e in feed.entries:
        doi = (e.get("prism_doi") or e.get("dc_identifier") or "").replace("doi:", "")
        if not DOI_RX.match(doi):
            mm = DOI_RX.search(e.get("link", "") + " " + e.get("id", ""))
            doi = mm.group(0) if mm else ""
        add(f["journal"], clean(e.get("title")), e.get("link", ""), doi,
            entry_date(e), clean(e.get("summary", "")), "rss")
    time.sleep(1)

# 2) Crossref (RSS가 막힌 저널 보완, 저널별 순차)
for f in FEEDS:
    r = get(f"https://api.crossref.org/journals/{f['issn']}/works?filter=from-created-date:{since},"
            f"type:journal-article&rows=300&select=DOI,title,abstract,created&mailto={MAIL}")
    items = r.json()["message"]["items"] if (r is not None and r.ok) else []
    status.append(f"{f['journal']}: Crossref {r.status_code if r is not None else '실패'}, {len(items)}건")
    for it in items:
        doi = it.get("DOI", "")
        add(f["journal"], clean((it.get("title") or [""])[0]), f"https://doi.org/{doi}", doi,
            it.get("created", {}).get("date-time", ""), clean(it.get("abstract", "")), "crossref")
    time.sleep(3)

# 3) 보강: DOI 없음(Elsevier pii) → Crossref, 초록/날짜 → Crossref·OpenAlex
def oa_abstract(inv):
    if not inv: return ""
    pos = sorted((p, w) for w, ps in inv.items() for p in ps)
    return " ".join(w for _, w in pos)

papers = {}
for c in cands.values():
    if not c["doi"]:
        pii = re.search(r"/pii/(S?[0-9X]+)", c["link"])
        if pii:
            r = get(f"https://api.crossref.org/works?filter=alternative-id:{pii.group(1)}&rows=1"
                    f"&select=DOI,created,abstract&mailto={MAIL}")
            if r is not None and r.ok and r.json()["message"]["items"]:
                it = r.json()["message"]["items"][0]
                c["doi"] = it["DOI"].lower()
                c["date"] = c["date"] or it.get("created", {}).get("date-time", "")
                c["abstract"] = c["abstract"] or clean(it.get("abstract", ""))
            time.sleep(1)
    if c["doi"] and (len(c["abstract"]) < 200 or not c["date"]):
        r = get(f"https://api.openalex.org/works/doi:{c['doi']}?mailto={MAIL}")
        if r is not None and r.ok:
            w = r.json()
            ab = oa_abstract(w.get("abstract_inverted_index"))
            if len(ab) > len(c["abstract"]): c["abstract"] = ab
            c["date"] = c["date"] or w.get("publication_date", "")
        time.sleep(1)
    if c["doi"]: c["link"] = f"https://doi.org/{c['doi']}"
    key = c["doi"] or c["link"]
    seen.add(key)
    if key in papers: continue
    # 게재일 필터: 날짜를 알면 최근 3일 이내만
    if c["date"] and c["date"][:10] < str(since):
        continue
    r_, m_ = classify(f"{c['title']} {c['abstract']}")
    c.update(reactions=r_, materials=m_, starred=bool(STAR.search(f"{c['title']} {c['abstract']}")),
             abstract=c["abstract"][:2500], date_unknown=not c["date"])
    papers[key] = c

hits = sorted(papers.values(), key=lambda h: (not h["starred"], h["journal"]))
out = {"run_date_kst": str(today), "since": str(since), "count": len(hits), "papers": hits, "feed_status": status}
(DATA / "latest.json").write_text(json.dumps(out, ensure_ascii=False, indent=1))
(DATA / f"{today}.json").write_text(json.dumps(out, ensure_ascii=False, indent=1))
SEEN_F.write_text(json.dumps(sorted(seen)[-30000:]))

md = [f"# {today} 신규 논문 ({len(hits)}편, {since} 이후)\n"]
for h in hits:
    md.append(f"- {'★ ' if h['starred'] else ''}**{h['title']}** — *{h['journal']}* ({h['date'][:10] or '날짜 미확인'})  \n"
              f"  {'/'.join(h['reactions'])} × {'/'.join(h['materials'])} · {h['link']} · 초록 {'있음' if len(h['abstract'])>200 else '없음'}")
md.append("\n<details><summary>수집 상태</summary>\n\n" + "\n".join(f"- {s}" for s in status) + "\n</details>")
(DATA / "latest.md").write_text("\n".join(md))
print("\n".join(status)); print(f"HITS: {len(hits)}")
