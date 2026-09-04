"""
CSO 데일리 브리핑 자동 생성기
- 한국 제약 전문 매체 RSS 피드에서 실제 기사를 가져옴
- Claude API로 요약·분류
- index.html 생성 → GitHub Pages로 자동 배포
"""

import os
import re
import json
import html
import datetime
import urllib.request
import urllib.error
import xml.etree.ElementTree as ET
import anthropic

# ── 설정 ────────────────────────────────────────────────────────
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
HOURS_BACK = 72          # 최근 N시간 이내 기사만 사용 (주말 대비 72시간)
MAX_ARTICLES = 20        # Claude에 넘길 최대 기사 수
MAX_NEWS_CARDS = 9       # HTML에 표시할 최대 카드 수
KST_OFFSET = 9           # UTC+9

RSS_FEEDS = [
    {
        "name": "팜뉴스",
        "url": "https://www.pharmnews.com/rss/allArticle.xml",
        "fallback": None,
    },
    {
        "name": "히트뉴스",
        "url": "https://www.hitnews.co.kr/rss/allArticle.xml",
        "fallback": None,
    },
    {
        "name": "메디코파마",
        "url": "https://www.medicopharma.co.kr/rss/allArticle.xml",
        "fallback": None,
    },
    {
        "name": "메디칼업저버",
        "url": "https://www.monews.co.kr/rss/allArticle.xml",
        "fallback": None,
    },
    {
        "name": "청년의사",
        "url": "https://www.docdocdoc.co.kr/rss/allArticle.xml",
        "fallback": None,
    },
]

# CSO를 활용하는(엑셀 수수료 보유) 주요 제약사
CSO_PHARMA_COMPANIES = [
    "메디카코리아", "대화제약", "한국파마", "셀트리온제약", "유유제약",
    "대웅바이오", "대웅제약", "JW중외제약", "신풍제약", "경동제약",
    "명인제약", "하나제약", "동화약품", "영진약품", "삼일제약",
    "일동제약", "안국약품", "삼진제약", "환인제약", "휴온스",
    "휴텍스", "한국휴텍스제약", "제일약품", "동구바이오제약", "팜젠사이언스",
    "알리코제약", "마더스제약", "테라젠이텍스", "이연제약", "진양제약",
    "국제약품", "신일제약", "삼천당제약", "한올바이오파마", "부광약품",
    "동국제약", "경보제약", "구주제약", "대원제약", "위더스제약",
    "케이엠에스제약", "라이트팜텍", "씨엠지제약", "CMG제약", "동광제약",
    "유니메드제약", "한국유니온제약", "정우신약", "바이넥스", "서울제약",
    "메딕스제약", "휴비스트제약", "코스맥스파마", "새한제약", "펜믹스",
    "티디에스팜", "성원애드콕제약", "메디포럼제약", "제뉴파마", "제뉴원사이언스",
    "다산제약", "넥스팜코리아", "한국프라임제약", "이든파마", "비보존제약",
    "더유제약", "오스틴제약", "메타파마", "휴온스메디텍", "글로벌제약",
    "삼익제약", "한국비엔씨", "파마킹", "에이프로젠제약", "일화",
    "우리들제약", "조아제약", "고려제약", "태극제약", "신신제약",
    "경남제약", "삼성제약", "한국글로벌제약", "화이트생명과학", "킴스제약",
    "대한뉴팜", "대우제약", "동방에프티엘", "에스케이케미칼", "한독",
    "보령", "광동제약", "유한양행", "종근당", "한미약품",
    "녹십자", "동아에스티", "일양약품", "현대약품", "삼아제약",
]

CSO_KEYWORDS = [
    "CSO", "CMR", "판매대행", "영업대행", "판촉영업자",
    "약가", "리베이트", "신고제", "수수료", "판촉", "제약 영업",
    "의약품 시장", "복제약", "제네릭", "공정경쟁규약", "보험약가",
    "도매", "처방약", "전문의약품", "지출보고서", "경제적 이익",
    "약사법", "혁신형 제약", "재위탁", "위탁판매", "CP ", "공정거래",
] + CSO_PHARMA_COMPANIES

# "CSO"가 C레벨 임원(Chief Scientific/Strategy Officer) 의미로 쓰인 기사 제외
CSO_FALSE_POSITIVE = ["선임", "영입", "승진", "신임 CSO", "CSO 임명", "최고전략책임자", "최고과학책임자"]


# ── RSS 파싱 ─────────────────────────────────────────────────────
def fetch_rss(url: str) -> bytes | None:
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "Mozilla/5.0 CSO-Daily-Bot/1.0"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.read()
    except Exception as e:
        print(f"  RSS 오류 {url}: {e}")
        return None


def parse_date(date_str: str) -> datetime.datetime | None:
    """RFC 2822 및 일반 날짜 형식 파싱"""
    if not date_str:
        return None
    date_str = date_str.strip()
    formats = [
        "%a, %d %b %Y %H:%M:%S %z",
        "%a, %d %b %Y %H:%M:%S GMT",
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%d %H:%M:%S",
    ]
    for fmt in formats:
        try:
            return datetime.datetime.strptime(date_str, fmt)
        except ValueError:
            continue
    return None


def get_articles() -> list[dict]:
    now_utc = datetime.datetime.now(datetime.timezone.utc)
    cutoff = now_utc - datetime.timedelta(hours=HOURS_BACK)
    articles = []

    for feed in RSS_FEEDS:
        print(f"  📡 {feed['name']} 피드 수집 중...")
        raw = fetch_rss(feed["url"])
        if raw is None and feed.get("fallback"):
            raw = fetch_rss(feed["fallback"])
        if raw is None:
            continue

        try:
            root = ET.fromstring(raw)
        except ET.ParseError as e:
            print(f"  XML 파싱 오류 {feed['name']}: {e}")
            continue

        ns = {"atom": "http://www.w3.org/2005/Atom"}
        items = root.findall(".//item") or root.findall(".//atom:entry", ns)

        for item in items:
            def txt(tag):
                el = item.find(tag)
                return (el.text or "").strip() if el is not None else ""

            title = html.unescape(txt("title"))
            link = txt("link") or txt("guid")
            pub = txt("pubDate") or txt("published") or txt("updated")
            desc = html.unescape(re.sub(r"<[^>]+>", "", txt("description") or txt("summary") or ""))

            # 날짜 필터
            pub_dt = parse_date(pub)
            if pub_dt:
                if pub_dt.tzinfo is None:
                    pub_dt = pub_dt.replace(tzinfo=datetime.timezone.utc)
                if pub_dt < cutoff:
                    continue
                age_hours = int((now_utc - pub_dt).total_seconds() / 3600)
            else:
                age_hours = 0  # 날짜 불명 기사는 포함

            # CSO 관련 키워드 필터
            combined = (title + " " + desc).lower()
            is_relevant = any(kw.lower() in combined for kw in CSO_KEYWORDS)
            if not is_relevant:
                continue

            # C레벨 임원 CSO(동음이의) 기사 제외: "CSO"만으로 매칭됐고 인사 관련 표현이 있으면 스킵
            if "cso" in combined and any(fp in title for fp in CSO_FALSE_POSITIVE):
                has_real_signal = any(s in combined for s in ["판촉영업자", "영업대행", "판매대행", "수수료", "위탁", "신고제"])
                if not has_real_signal:
                    continue

            articles.append({
                "source": feed["name"],
                "title": title,
                "url": link,
                "desc": desc[:300] if desc else "",
                "pub": pub_dt.strftime("%Y-%m-%d %H:%M") if pub_dt else "날짜미상",
                "age_hours": age_hours,
            })

    # 최신순 정렬, 중복 제거
    seen = set()
    unique = []
    for a in sorted(articles, key=lambda x: x["age_hours"]):
        key = a["title"][:40]
        if key not in seen:
            seen.add(key)
            unique.append(a)

    print(f"  ✅ 관련 기사 {len(unique)}건 수집 완료")
    return unique[:MAX_ARTICLES]


# ── Claude 요약·분류 ──────────────────────────────────────────────
def summarize_with_claude(articles: list[dict]) -> list[dict]:
    if not articles:
        return []

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    articles_text = "\n\n".join(
        f"[{i+1}] 출처: {a['source']} | 날짜: {a['pub']}\n"
        f"제목: {a['title']}\n"
        f"내용: {a['desc']}\n"
        f"URL: {a['url']}"
        for i, a in enumerate(articles)
    )

    prompt = f"""당신은 한국 CSO(의약품 판촉영업자·판매대행) 산업 전문 에디터입니다.
'CSO 위클리 인사이트' 뉴스레터의 편집 기준을 따라, 아래 실제 뉴스 기사들을 CSO·CMR 종사자 관점에서 분석해주세요.

[편집 우선순위 — impact 판정 기준]
1. 제약사의 CSO 전환·철수·조직 개편 사례 (예: JW중외제약 CSO 철수, 신풍제약 의원급 CSO 전환) → high
2. 지출보고서 공개, 판촉영업자 신고제, 약사법·규제 변화 → high
3. 수수료 구조·재위탁·CP(공정거래 자율준수) 관련 → high
4. CSO를 활용하는 제약사(엑셀 수수료 보유 제약사)의 실적·품목·영업 소식 → medium~high
5. 약가·제네릭·시장 동향 일반 → medium
6. 그 외 참고성 기사 → low

[규제 인과관계에 주목]
리베이트 적발 → 혁신형 인증 취소 → 약가 우대 박탈로 이어지는 구조처럼,
단순 사실 나열이 아니라 CSO·제약사에 미치는 실질적 손익 인과를 짚어 요약할 것.

[주의]
- "CSO"가 Chief Scientific/Strategy Officer(임원 인사) 의미인 기사는 제외.
- 요약·제목에서 "리베이트"라는 단어 대신 "판매대행수수료" 등 중립적 표현을 사용 (기사 인용 시 불가피한 경우 제외).

각 기사를 JSON 배열로 반환하세요. 형식:
[
  {{
    "index": 기사번호(1부터),
    "category": "policy|market|company|cso" 중 하나,
    "tag": "규제·정책|시장동향|제약사소식|CSO산업" 중 하나,
    "headline": "CSO 종사자 관점에서 핵심을 담은 한국어 제목 (40자 이내)",
    "summary": "CSO 업무에 미치는 영향 중심으로 2~3문장 요약",
    "impact": "high|medium|low",
    "url": "원본 URL 그대로"
  }}
]

기사가 CSO 업무와 무관하면 해당 항목 생략.
반드시 실제 기사 내용만 사용, 추가 사실 삽입 금지.

--- 기사 목록 ---
{articles_text}
"""

    print("  🤖 Claude 요약 중...")
    message = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=4096,
        messages=[{"role": "user", "content": prompt}],
    )

    raw = message.content[0].text.strip()
    # JSON 블록 추출
    match = re.search(r"\[[\s\S]*\]", raw)
    if not match:
        print("  ⚠️ Claude 응답에서 JSON 추출 실패")
        return []

    try:
        summarized = json.loads(match.group())
    except json.JSONDecodeError as e:
        print(f"  ⚠️ JSON 파싱 오류: {e}")
        return []

    # 원본 기사 정보 병합
    for item in summarized:
        idx = item.get("index", 1) - 1
        if 0 <= idx < len(articles):
            item["source"] = articles[idx]["source"]
            item["pub"] = articles[idx]["pub"]
            if not item.get("url"):
                item["url"] = articles[idx]["url"]

    # impact 높은 것 우선
    order = {"high": 0, "medium": 1, "low": 2}
    summarized.sort(key=lambda x: order.get(x.get("impact", "low"), 2))

    print(f"  ✅ {len(summarized)}건 요약 완료")
    return summarized[:MAX_NEWS_CARDS]


# ── HTML 생성 ────────────────────────────────────────────────────
TAG_STYLES = {
    "policy": ("tag-policy", "규제·정책"),
    "market": ("tag-market", "시장동향"),
    "company": ("tag-company", "제약사소식"),
    "cso": ("tag-cso", "CSO산업"),
}

IMPACT_LABELS = {
    "high": ("tag-urgent", "주목"),
    "medium": ("tag-market", "관심"),
    "low": ("tag-company", "참고"),
}


def make_tag(cat: str) -> str:
    cls, label = TAG_STYLES.get(cat, ("tag-company", cat))
    return f'<span class="tag {cls}">{label}</span>'


def make_impact_tag(impact: str) -> str:
    cls, label = IMPACT_LABELS.get(impact, ("tag-company", "참고"))
    return f'<span class="tag {cls}" style="font-size:10px">{label}</span>'


def build_html(articles: list[dict]) -> str:
    now_kst = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=KST_OFFSET)))
    date_str = now_kst.strftime("%Y년 %m월 %d일")
    weekday = ["월", "화", "수", "목", "금", "토", "일"][now_kst.weekday()]
    updated_at = now_kst.strftime("%H:%M")

    lead = articles[0] if articles else None
    side_cards = articles[1:4] if len(articles) > 1 else []
    grid_cards = articles[4:] if len(articles) > 4 else []

    def card_html(a: dict, style: str = "news") -> str:
        cat = a.get("category", "cso")
        tag_html = make_tag(cat)
        imp_tag = make_impact_tag(a.get("impact", "medium"))
        headline = html.escape(a.get("headline", ""))
        summary = html.escape(a.get("summary", ""))
        source = html.escape(a.get("source", ""))
        pub = html.escape(a.get("pub", "")[:10])
        url = a.get("url", "#")

        if style == "lead":
            return f"""
      <a href="{url}" target="_blank" rel="noopener" class="card lead-card" data-category="{cat}">
        <div class="lead-card-header">
          <div class="lead-card-eyebrow">{imp_tag} {tag_html}</div>
          <h2 class="lead-headline">{headline}</h2>
          <p class="lead-summary">{summary}</p>
        </div>
        <div class="lead-footer">
          <span class="source-chip">{source} <span class="sep">·</span> {pub}</span>
          <span class="read-more-link">전체 기사 →</span>
        </div>
      </a>"""

        if style == "side":
            return f"""
      <a href="{url}" target="_blank" rel="noopener" class="card side-card" data-category="{cat}">
        <div class="side-card-inner">
          <div>{tag_html}</div>
          <h3 class="side-headline">{headline}</h3>
          <p class="side-summary">{summary}</p>
        </div>
        <div class="side-footer">
          <span>{source}</span>
          {imp_tag}
        </div>
      </a>"""

        # news grid card
        return f"""
    <a href="{url}" target="_blank" rel="noopener" class="card news-card" data-category="{cat}">
      <div class="news-card-inner">
        {tag_html}
        <h3 class="news-headline">{headline}</h3>
        <p class="news-body">{summary}</p>
      </div>
      <div class="news-footer">
        <span>{source}</span>
        <span>{pub}</span>
      </div>
    </a>"""

    lead_html = card_html(lead, "lead") if lead else "<p style='padding:24px;color:var(--text-muted)'>오늘은 관련 뉴스가 없습니다.</p>"
    side_html = "\n".join(card_html(a, "side") for a in side_cards)
    grid_html = "\n".join(card_html(a, "news") for a in grid_cards)

    no_articles_msg = "" if articles else "<p style='text-align:center;padding:40px;color:var(--text-muted)'>오늘은 새로운 CSO 관련 뉴스가 없습니다.</p>"

    return f"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>CSO 데일리 브리핑 — {date_str}</title>
<meta name="description" content="CSO·CMR 전문가를 위한 제약 산업 아침 뉴스 브리핑. 약가, 규제, 제약사 소식을 매일 자동 업데이트.">
<meta property="og:title" content="CSO 데일리 브리핑 {date_str}">
<meta property="og:description" content="CSO·CMR 종사자를 위한 제약 산업 핵심 뉴스 | Powered by 프로엠알">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Noto+Serif+KR:wght@400;600;700&family=Noto+Sans+KR:wght@300;400;500;600&display=swap">
<style>
:root {{
  --bg:#EEF3F8;--surface:#FFFFFF;--surface2:#F5F9FC;--border:#D4E0EB;
  --text:#0D1B2E;--text-muted:#4F6B82;--text-faint:#7A9BB5;
  --accent:#0ABFBC;--accent-dark:#088F8D;--amber:#E8941A;--amber-bg:#FFF7E8;
  --navy:#0D1B2E;--navy-mid:#1A3354;--red:#D94040;--red-bg:#FFF0F0;
  --green:#1DAA6B;--shadow:0 1px 4px rgba(13,27,46,.08),0 4px 16px rgba(13,27,46,.06);
  --radius:10px;
}}
@media(prefers-color-scheme:dark){{
  :root:not([data-theme="light"]){{
    --bg:#080F1A;--surface:#0D1B2E;--surface2:#121F30;--border:#1E3354;
    --text:#E8F1F8;--text-muted:#7A9BB5;--text-faint:#3D5A72;
    --accent:#0ADBD8;--accent-dark:#0ABFBC;--amber:#F5A623;--amber-bg:#1A1200;
    --navy:#E8F1F8;--navy-mid:#B0C8DC;--red:#FF6B6B;--red-bg:#1A0A0A;
    --green:#2DCF82;--shadow:0 1px 4px rgba(0,0,0,.3),0 4px 16px rgba(0,0,0,.25);
  }}
}}
:root[data-theme="dark"]{{
  --bg:#080F1A;--surface:#0D1B2E;--surface2:#121F30;--border:#1E3354;
  --text:#E8F1F8;--text-muted:#7A9BB5;--text-faint:#3D5A72;
  --accent:#0ADBD8;--accent-dark:#0ABFBC;--amber:#F5A623;--amber-bg:#1A1200;
  --navy:#E8F1F8;--navy-mid:#B0C8DC;--red:#FF6B6B;--red-bg:#1A0A0A;
  --green:#2DCF82;--shadow:0 1px 4px rgba(0,0,0,.3),0 4px 16px rgba(0,0,0,.25);
}}
*,*::before,*::after{{box-sizing:border-box;margin:0;padding:0}}
html{{font-size:16px;-webkit-text-size-adjust:100%}}
body{{font-family:'Noto Sans KR','Apple SD Gothic Neo',sans-serif;background:var(--bg);color:var(--text);line-height:1.7;min-height:100vh}}
a{{color:inherit;text-decoration:none}}
.masthead{{background:var(--navy-mid);color:#fff;position:relative;overflow:hidden}}
.masthead::before{{content:'';position:absolute;inset:0;background:linear-gradient(135deg,#0D1B2E 0%,#1A3354 60%,#0ABFBC22 100%);pointer-events:none}}
.masthead-inner{{position:relative;max-width:1200px;margin:0 auto;padding:28px 32px 24px;display:flex;flex-direction:column;gap:12px}}
.masthead-top{{display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:12px}}
.brand-eyebrow{{font-size:10px;font-weight:500;letter-spacing:.12em;text-transform:uppercase;color:var(--accent)}}
.brand-title{{font-family:'Noto Serif KR',Georgia,serif;font-size:clamp(24px,4vw,36px);font-weight:700;color:#fff;line-height:1.15;text-wrap:balance}}
.brand-sub{{font-size:13px;color:rgba(255,255,255,.55);margin-top:2px;font-weight:300}}
.masthead-meta{{display:flex;flex-direction:column;align-items:flex-end;gap:6px}}
.date-badge{{font-size:12px;color:rgba(255,255,255,.7);letter-spacing:.04em}}
.updated-badge{{font-size:11px;color:rgba(255,255,255,.45)}}
.promr-badge{{display:flex;align-items:center;gap:8px;background:rgba(10,191,188,.15);border:1px solid rgba(10,191,188,.35);border-radius:6px;padding:6px 14px;font-size:12px;font-weight:500;color:var(--accent);cursor:pointer;transition:background .2s}}
.promr-badge:hover{{background:rgba(10,191,188,.25)}}
.promr-badge .dot{{width:7px;height:7px;border-radius:50%;background:var(--accent);animation:pulse 2s infinite}}
@keyframes pulse{{0%,100%{{opacity:1}}50%{{opacity:.4}}}}
.masthead-divider{{height:1px;background:rgba(255,255,255,.12);margin:4px 0}}
.ticker-row{{display:flex;align-items:center;gap:16px;overflow:hidden;font-size:12px;color:rgba(255,255,255,.65)}}
.ticker-label{{font-size:10px;letter-spacing:.1em;text-transform:uppercase;color:var(--accent);font-weight:600;white-space:nowrap;flex-shrink:0}}
.ticker-items{{display:flex;gap:28px;white-space:nowrap;animation:scroll-ticker 32s linear infinite}}
@keyframes scroll-ticker{{0%{{transform:translateX(0)}}100%{{transform:translateX(-50%)}}}}
.ticker-item{{display:flex;align-items:center;gap:6px;flex-shrink:0}}
.cat-nav{{background:var(--surface);border-bottom:1px solid var(--border);position:sticky;top:0;z-index:100}}
.cat-nav-inner{{max-width:1200px;margin:0 auto;padding:0 32px;display:flex;align-items:center;gap:4px;overflow-x:auto;scrollbar-width:none}}
.cat-nav-inner::-webkit-scrollbar{{display:none}}
.cat-btn{{font-family:'Noto Sans KR',sans-serif;font-size:13px;font-weight:500;padding:14px 16px;color:var(--text-muted);border:none;background:transparent;cursor:pointer;white-space:nowrap;border-bottom:2px solid transparent;transition:color .2s,border-color .2s}}
.cat-btn:hover{{color:var(--text)}}
.cat-btn.active{{color:var(--accent);border-bottom-color:var(--accent)}}
.main{{max-width:1200px;margin:0 auto;padding:32px 32px 120px}}
.alert-banner{{background:var(--amber-bg);border:1px solid rgba(232,148,26,.3);border-left:4px solid var(--amber);border-radius:var(--radius);padding:14px 18px;display:flex;align-items:flex-start;gap:12px;margin-bottom:28px}}
.alert-icon{{font-size:18px;flex-shrink:0;margin-top:1px}}
.alert-text{{font-size:13.5px;color:var(--text);line-height:1.6}}
.alert-text strong{{color:var(--amber)}}
.section-header{{display:flex;align-items:baseline;gap:12px;margin-bottom:20px}}
.section-label{{font-size:10px;font-weight:600;letter-spacing:.12em;text-transform:uppercase;color:var(--accent)}}
.section-rule{{flex:1;height:1px;background:var(--border)}}
.lead-grid{{display:grid;grid-template-columns:1fr 340px;gap:24px;margin-bottom:40px}}
@media(max-width:900px){{.lead-grid{{grid-template-columns:1fr}}}}
.card{{background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);box-shadow:var(--shadow);overflow:hidden;transition:box-shadow .2s,transform .15s;display:block}}
.card:hover{{box-shadow:0 4px 20px rgba(13,27,46,.12),0 8px 32px rgba(13,27,46,.08);transform:translateY(-2px)}}
.lead-card{{display:flex;flex-direction:column}}
.lead-card-header{{padding:28px 28px 20px;border-bottom:1px solid var(--border)}}
.lead-card-eyebrow{{display:flex;align-items:center;gap:8px;margin-bottom:14px}}
.tag{{display:inline-block;font-size:10px;font-weight:600;letter-spacing:.1em;text-transform:uppercase;padding:3px 9px;border-radius:4px}}
.tag-market{{background:rgba(10,191,188,.12);color:var(--accent-dark)}}
.tag-policy{{background:rgba(232,148,26,.12);color:var(--amber)}}
.tag-company{{background:rgba(13,27,46,.06);color:var(--text-muted)}}
.tag-urgent{{background:rgba(217,64,64,.12);color:var(--red)}}
.tag-cso{{background:rgba(29,170,107,.1);color:var(--green)}}
.lead-headline{{font-family:'Noto Serif KR',serif;font-size:clamp(20px,2.5vw,26px);font-weight:700;line-height:1.35;color:var(--text);text-wrap:balance;margin-bottom:14px}}
.lead-summary{{font-size:14.5px;color:var(--text-muted);line-height:1.75}}
.lead-footer{{display:flex;align-items:center;gap:12px;padding:16px 28px;border-top:1px solid var(--border);flex-wrap:wrap}}
.source-chip{{font-size:11px;color:var(--text-faint);display:flex;align-items:center;gap:4px}}
.source-chip .sep{{color:var(--border)}}
.read-more-link{{font-size:12px;font-weight:600;color:var(--accent);letter-spacing:.04em;margin-left:auto}}
.side-stack{{display:flex;flex-direction:column;gap:16px}}
.side-card{{display:flex;flex-direction:column}}
.side-card-inner{{padding:18px 20px;display:flex;flex-direction:column;gap:10px;flex:1}}
.side-headline{{font-family:'Noto Serif KR',serif;font-size:15px;font-weight:700;color:var(--text);line-height:1.4;text-wrap:balance}}
.side-summary{{font-size:13px;color:var(--text-muted);line-height:1.65}}
.side-footer{{display:flex;align-items:center;justify-content:space-between;padding:10px 20px;border-top:1px solid var(--border);font-size:11px;color:var(--text-faint)}}
.news-grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:20px;margin-bottom:40px}}
.news-card{{display:flex;flex-direction:column}}
.news-card-inner{{padding:20px;display:flex;flex-direction:column;gap:10px;flex:1}}
.news-headline{{font-family:'Noto Serif KR',serif;font-size:15.5px;font-weight:700;color:var(--text);line-height:1.45;text-wrap:balance}}
.news-body{{font-size:13px;color:var(--text-muted);line-height:1.7;flex:1}}
.news-footer{{display:flex;align-items:center;justify-content:space-between;padding:12px 20px;border-top:1px solid var(--border);font-size:11px;color:var(--text-faint)}}
.promr-section{{background:linear-gradient(135deg,#0D1B2E 0%,#1A3354 100%);border:1px solid rgba(10,191,188,.2);border-radius:14px;padding:36px 40px;margin-bottom:40px;display:grid;grid-template-columns:1fr auto;gap:32px;align-items:center;position:relative;overflow:hidden}}
.promr-section::after{{content:'';position:absolute;right:-40px;top:-40px;width:200px;height:200px;border-radius:50%;background:radial-gradient(circle,rgba(10,191,188,.12) 0%,transparent 70%);pointer-events:none}}
@media(max-width:640px){{.promr-section{{grid-template-columns:1fr;padding:24px}}}}
.promr-content{{display:flex;flex-direction:column;gap:10px}}
.promr-eyebrow{{font-size:10px;font-weight:600;letter-spacing:.14em;text-transform:uppercase;color:var(--accent)}}
.promr-headline{{font-family:'Noto Serif KR',serif;font-size:clamp(18px,2.5vw,22px);font-weight:700;color:#fff;line-height:1.4;text-wrap:balance}}
.promr-desc{{font-size:13.5px;color:rgba(255,255,255,.65);line-height:1.7}}
.promr-features{{display:flex;flex-wrap:wrap;gap:8px;margin-top:4px}}
.feature-chip{{display:flex;align-items:center;gap:6px;font-size:12px;color:rgba(255,255,255,.8);background:rgba(255,255,255,.07);border:1px solid rgba(255,255,255,.12);border-radius:20px;padding:4px 12px}}
.feature-chip .fc-dot{{width:5px;height:5px;border-radius:50%;background:var(--accent);flex-shrink:0}}
.promr-cta-group{{display:flex;flex-direction:column;gap:12px;align-items:center;flex-shrink:0}}
.btn-primary{{display:inline-flex;align-items:center;gap:8px;background:var(--accent);color:#fff;font-size:14px;font-weight:600;padding:13px 28px;border-radius:8px;border:none;cursor:pointer;white-space:nowrap;transition:background .2s,transform .15s}}
.btn-primary:hover{{background:#08A8A5;transform:translateY(-1px)}}
.sticky-strip{{position:fixed;bottom:0;left:0;right:0;z-index:200;background:#1A3354;border-top:1px solid rgba(10,191,188,.3);padding:12px 32px;display:flex;align-items:center;justify-content:space-between;gap:16px;box-shadow:0 -4px 24px rgba(0,0,0,.25)}}
.strip-text{{font-size:13px;color:rgba(255,255,255,.8);display:flex;align-items:center;gap:8px}}
.strip-text strong{{color:var(--accent)}}
.strip-cta{{display:inline-flex;align-items:center;gap:6px;background:var(--accent);color:#fff;font-size:13px;font-weight:600;padding:9px 20px;border-radius:6px;white-space:nowrap;cursor:pointer;border:none;transition:background .2s;text-decoration:none}}
.strip-cta:hover{{background:#08A8A5}}
.strip-dismiss{{background:transparent;border:none;color:rgba(255,255,255,.4);font-size:18px;cursor:pointer;line-height:1;padding:0 4px}}
.site-footer{{border-top:1px solid var(--border);padding:28px 32px;max-width:1200px;margin:0 auto;display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:12px;font-size:12px;color:var(--text-faint)}}
.footer-brand{{font-weight:600;color:var(--accent)}}
.footer-links{{display:flex;gap:20px}}
.footer-links a{{color:var(--text-faint);transition:color .2s}}
.footer-links a:hover{{color:var(--accent)}}
@media(max-width:640px){{.main{{padding:20px 16px 120px}}.masthead-inner{{padding:20px 16px 16px}}.cat-nav-inner{{padding:0 16px}}.promr-section{{padding:24px 20px}}.sticky-strip{{padding:10px 16px}}}}
</style>
</head>
<body>

<header class="masthead">
  <div class="masthead-inner">
    <div class="masthead-top">
      <div>
        <div class="brand-eyebrow">의약품 판매대행 · 아침 브리핑</div>
        <h1 class="brand-title">CSO 데일리 브리핑</h1>
        <p class="brand-sub">CSO·CMR 전문가를 위한 제약 산업 인텔리전스</p>
      </div>
      <div class="masthead-meta">
        <span class="date-badge">{date_str} {weekday}요일</span>
        <span class="updated-badge">업데이트 {updated_at} KST</span>
        <a href="https://promr.co.kr" target="_blank" rel="noopener" class="promr-badge">
          <span class="dot"></span>
          ProMR — CSO 영업관리 플랫폼
        </a>
      </div>
    </div>
    <div class="masthead-divider"></div>
    <div class="ticker-row">
      <span class="ticker-label">속보</span>
      <div class="ticker-items" id="ticker-content">
        {''.join(f'<span class="ticker-item">📌 {html.escape(a.get("headline",""))}</span>' for a in articles[:6])}
        {''.join(f'<span class="ticker-item">📌 {html.escape(a.get("headline",""))}</span>' for a in articles[:6])}
      </div>
    </div>
  </div>
</header>

<nav class="cat-nav">
  <div class="cat-nav-inner">
    <button class="cat-btn active" onclick="filterCards('all',this)">전체</button>
    <button class="cat-btn" onclick="filterCards('policy',this)">규제·정책</button>
    <button class="cat-btn" onclick="filterCards('market',this)">시장동향</button>
    <button class="cat-btn" onclick="filterCards('company',this)">제약사소식</button>
    <button class="cat-btn" onclick="filterCards('cso',this)">CSO산업</button>
  </div>
</nav>

<main class="main">

  <div class="section-header">
    <span class="section-label">오늘의 헤드라인</span>
    <div class="section-rule"></div>
  </div>

  <div class="lead-grid">
    {lead_html}
    <div class="side-stack">
      {side_html}
    </div>
  </div>

  {"" if not grid_cards else '<div class="section-header"><span class="section-label">오늘의 뉴스</span><div class="section-rule"></div></div>'}
  <div class="news-grid" id="news-grid">
    {grid_html}
    {no_articles_msg}
  </div>

  <div class="promr-section">
    <div class="promr-content">
      <span class="promr-eyebrow">Powered by ProMR · 제약 CSO 전용 SaaS 플랫폼</span>
      <h2 class="promr-headline">CSO 영업관리·정산 자동화,<br>프로엠알 하나로 해결하세요</h2>
      <p class="promr-desc">국내 최초 CSO·CMR 전용 영업 플랫폼. AI 기반 정산 자동화, CSO 신고제 대응, 계약·실적 관리까지. 대화제약 등 주요 제약사·CSO 업체가 이미 도입했습니다.</p>
      <div class="promr-features">
        <span class="feature-chip"><span class="fc-dot"></span>정산 자동화</span>
        <span class="feature-chip"><span class="fc-dot"></span>계약·도네이션 관리</span>
        <span class="feature-chip"><span class="fc-dot"></span>CSO 신고제 대응</span>
        <span class="feature-chip"><span class="fc-dot"></span>실적 분석 리포트</span>
        <span class="feature-chip"><span class="fc-dot"></span>모바일 앱 지원</span>
      </div>
    </div>
    <div class="promr-cta-group">
      <a href="https://promr.co.kr" target="_blank" rel="noopener" class="btn-primary">무료로 시작하기 →</a>
    </div>
  </div>

</main>

<footer class="site-footer">
  <div>
    <span class="footer-brand">CSO 데일리 브리핑</span>
    &ensp;·&ensp; Powered by <a href="https://promr.co.kr" target="_blank" rel="noopener" style="color:var(--accent)">프로엠알(ProMR)</a>
    &ensp;·&ensp; {date_str} {updated_at} KST 기준
  </div>
  <nav class="footer-links">
    <a href="https://promr.co.kr" target="_blank" rel="noopener">프로엠알</a>
    <a href="https://www.pharmcso.co.kr" target="_blank" rel="noopener">제약CSO신문</a>
    <a href="http://kcsoa.co.kr" target="_blank" rel="noopener">한국CSO협회</a>
  </nav>
</footer>

<div class="sticky-strip" id="strip">
  <span class="strip-text">💊 <strong>프로엠알(ProMR)</strong> — CSO 영업관리·정산 자동화. 지금 무료로 시작하세요.</span>
  <a href="https://promr.co.kr" target="_blank" rel="noopener" class="strip-cta">무료 체험 →</a>
  <button class="strip-dismiss" onclick="document.getElementById('strip').style.display='none'">×</button>
</div>

<script>
function filterCards(cat, btn) {{
  document.querySelectorAll('.cat-btn').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
  document.querySelectorAll('#news-grid .card, .lead-card, .side-card').forEach(card => {{
    card.style.display = (cat === 'all' || card.dataset.category === cat) ? '' : 'none';
  }});
}}
if (window.matchMedia('(prefers-reduced-motion: reduce)').matches) {{
  document.getElementById('ticker-content').style.animation = 'none';
}}
</script>
</body>
</html>"""


# ── 진입점 ───────────────────────────────────────────────────────
def main():
    if not ANTHROPIC_API_KEY:
        raise RuntimeError("ANTHROPIC_API_KEY 환경변수가 설정되지 않았습니다.")

    print("🔍 RSS 피드 수집 중...")
    raw_articles = get_articles()

    if not raw_articles:
        print("⚠️ 관련 기사 없음 — 빈 페이지 생성")
        articles = []
    else:
        print("📝 Claude 요약 및 분류 중...")
        articles = summarize_with_claude(raw_articles)

    print("🏗️  HTML 생성 중...")
    html_content = build_html(articles)

    out_path = os.path.join(os.path.dirname(__file__), "index.html")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html_content)

    print(f"✅ 완료: {out_path} ({len(articles)}건 기사)")
    return out_path


if __name__ == "__main__":
    main()
