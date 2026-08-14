import os
import re
import time
import html
import requests
import urllib3
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse, parse_qs
from feedgen.feed import FeedGenerator
from datetime import datetime, timezone, date
from requests.exceptions import SSLError, RequestException

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

BASE_URL = "https://www.kobia.kr"

BOARDS = [
    {
        "name": "업계동향",
        "list_url": "https://www.kobia.kr/memberlounge/trend.php",
        "board_id": "trend",
        "output": "docs/trend.xml",
    },
    {
        "name": "임상·허가",
        "list_url": "https://www.kobia.kr/memberlounge/trend_3.php",
        "board_id": "trend_3",
        "output": "docs/trend_3.xml",
    },
    {
        "name": "글로벌 동향",
        "list_url": "https://www.kobia.kr/memberlounge/trend_4.php",
        "board_id": "trend_4",
        "output": "docs/trend_4.xml",
    },
]

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; KOBIA-RSS-Bot/1.0)"
}

MAX_ITEMS_PER_BOARD = 50
MONITOR_START = date(2026, 8, 1)
MONITOR_END = date(2026, 8, 31)

session = requests.Session()
session.headers.update(HEADERS)


def ensure_docs_dir():
    os.makedirs("docs", exist_ok=True)


def get_html(url, retries=3, delay=2):
    last_error = None

    for attempt in range(1, retries + 1):
        try:
            response = session.get(url, timeout=30)
            response.raise_for_status()
            response.encoding = response.apparent_encoding
            return response.text
        except SSLError:
            try:
                print(f"SSL 검증 실패로 verify=False 재시도: {url}")
                response = session.get(url, timeout=30, verify=False)
                response.raise_for_status()
                response.encoding = response.apparent_encoding
                return response.text
            except RequestException as e:
                last_error = e
        except RequestException as e:
            last_error = e

        if attempt < retries:
            print(f"재시도 {attempt}/{retries}: {url}")
            time.sleep(delay)

    raise last_error


def clean_text(text):
    if not text:
        return ""
    text = html.unescape(text)
    text = text.replace("\xa0", " ")
    text = re.sub(r"\s+", " ", text).strip()
    return text


def extract_text(element):
    if not element:
        return ""
    return clean_text(element.get_text(separator=" ", strip=True))


def parse_date_string(raw):
    if not raw:
        return None
    raw = raw.strip().replace(".", "-").replace("/", "-")
    for fmt in ("%Y-%m-%d", "%Y-%m-%d %H:%M"):
        try:
            dt = datetime.strptime(raw, fmt)
            return dt.replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    return None


def guess_date(text):
    if not text:
        return None

    patterns = [
        r"등록일\s*(\d{4}[.\-/]\d{1,2}[.\-/]\d{1,2})",
        r"(\d{4}[.\-/]\d{1,2}[.\-/]\d{1,2}\s+\d{1,2}:\d{2})",
        r"(\d{4}[.\-/]\d{1,2}[.\-/]\d{1,2})",
    ]

    for pattern in patterns:
        m = re.search(pattern, text)
        if m:
            dt = parse_date_string(m.group(1))
            if dt:
                return dt
    return None


def extract_post_links(list_url, board_id):
    html_text = get_html(list_url)
    soup = BeautifulSoup(html_text, "lxml")

    links = []
    seen = set()

    for a in soup.find_all("a", href=True):
        href = a["href"]
        full_url = urljoin(list_url, href)

        if "mode=view" not in full_url or "idx=" not in full_url:
            continue
        if f"boardid={board_id}" not in full_url:
            continue

        if full_url not in seen:
            seen.add(full_url)
            links.append(full_url)

    return links[:MAX_ITEMS_PER_BOARD]


def parse_post_meta_from_url(url):
    parsed = urlparse(url)
    qs = parse_qs(parsed.query)
    boardid = qs.get("boardid", [""])[0]
    idx = qs.get("idx", [""])[0]
    return boardid, idx


def within_monitor_range(dt):
    if not dt:
        return False
    d = dt.date()
    return MONITOR_START <= d <= MONITOR_END


def find_title(page_text, soup, board_name):
    m = re.search(r"제목\s*(.+?)\s*구분\s*", page_text)
    if m:
        title = clean_text(m.group(1))
        if title and title != board_name:
            return title

    for tag in soup.find_all(["h1", "h2", "h3", "th", "strong"]):
        txt = extract_text(tag)
        if not txt:
            continue
        if txt in {board_name, "업계동향", "임상·허가", "글로벌 동향"}:
            continue
        if len(txt) >= 5 and not re.search(r"등록일|작성자|조회수|첨부파일|구분", txt):
            return txt

    if soup.title:
        txt = clean_text(soup.title.get_text(strip=True))
        txt = re.sub(r"\s*\|.*$", "", txt).strip()
        if txt and txt != board_name:
            return txt

    return f"{board_name} 게시물"


def extract_registered_date(page_text):
    m = re.search(r"등록일\s*(\d{4}[.\-/]\d{1,2}[.\-/]\d{1,2})", page_text)
    if m:
        return parse_date_string(m.group(1))
    return guess_date(page_text)


def build_attachment_url_from_js(href):
    m = re.search(
        r"javascript:download\('\s*([^']+)\s*'\s*,\s*'\s*([^']+)\s*'\s*,\s*'\s*([^']+)\s*'\s*,\s*'\s*([^']*)\s*'\)",
        href,
        re.I,
    )
    if not m:
        return None

    boardid, idx, fileidx, mime = m.groups()

    candidates = [
        f"{BASE_URL}/common/download.php?boardid={boardid}&idx={idx}&fileidx={fileidx}",
        f"{BASE_URL}/bbs/download.php?boardid={boardid}&idx={idx}&fileidx={fileidx}",
        f"{BASE_URL}/download.php?boardid={boardid}&idx={idx}&fileidx={fileidx}",
        f"{BASE_URL}/lib/download.php?boardid={boardid}&idx={idx}&fileidx={fileidx}",
    ]

    for url in candidates:
        try:
            r = session.head(url, allow_redirects=True, timeout=15, verify=False)
            ctype = (r.headers.get("Content-Type") or "").lower()
            if r.status_code < 400 and "text/html" not in ctype:
                return url
        except RequestException:
            pass

    for url in candidates:
        try:
            r = session.get(url, stream=True, allow_redirects=True, timeout=15, verify=False)
            ctype = (r.headers.get("Content-Type") or "").lower()
            r.close()
            if r.status_code < 400 and "text/html" not in ctype:
                return url
        except RequestException:
            pass

    return None


def extract_attachments(soup, page_url):
    attachments = []
    seen = set()

    for a in soup.find_all("a", href=True):
        href = (a.get("href") or "").strip()
        text = clean_text(a.get_text(" ", strip=True))
        full_url = None

        if href.lower().startswith("javascript:download("):
            full_url = build_attachment_url_from_js(href)
        elif any(ext in href.lower() for ext in [
            ".pdf", ".xlsx", ".xls", ".doc", ".docx",
            ".hwp", ".zip", ".csv", ".ppt", ".pptx", ".jpg", ".jpeg", ".png"
        ]):
            full_url = urljoin(page_url, href)
        elif any(keyword in href.lower() for keyword in ["download", "down", "file"]):
            full_url = urljoin(page_url, href)

        if not full_url:
            continue

        name = text or os.path.basename(urlparse(full_url).path) or "첨부파일"
        key = (name, full_url)
        if key in seen:
            continue
        seen.add(key)
        attachments.append({"name": name, "url": full_url})

    return attachments


def looks_like_noise(text, title, attachment_names):
    if not text:
        return True
    if len(text) < 20:
        return True

    patterns = [
        r"^제목\s+",
        r"등록일",
        r"작성자",
        r"조회수",
        r"첨부파일",
        r"목록",
        r"이전글",
        r"다음글",
    ]
    if any(re.search(p, text) for p in patterns) and len(text) < 120:
        return True

    reduced = text
    if title:
        reduced = reduced.replace(title, " ")
    for name in attachment_names:
        reduced = reduced.replace(name, " ")
    reduced = re.sub(r"등록일\s*\d{4}[.\-/]\d{1,2}[.\-/]\d{1,2}", " ", reduced)
    reduced = re.sub(r"작성자\s*\S+", " ", reduced)
    reduced = re.sub(r"조회수\s*\d+", " ", reduced)
    reduced = re.sub(r"첨부파일", " ", reduced)
    reduced = clean_text(reduced)

    return len(reduced) < 20


def extract_body_text(soup, title, attachments):
    attachment_names = [a["name"] for a in attachments]

    selectors = [
        "div.board_view_con",
        "div.view_cont",
        "div.view_content",
        "div.content",
        "div.cont",
        "td.view_content",
        "td.content",
        "article",
    ]

    candidates = []
    for selector in selectors:
        for node in soup.select(selector):
            txt = extract_text(node)
            if txt:
                candidates.append(txt)

    for tag in soup.find_all(["td", "div"]):
        txt = extract_text(tag)
        if txt and len(txt) >= 40:
            candidates.append(txt)

    best = ""
    best_score = -1

    for txt in candidates:
        work = txt
        work = work.replace(title, " ") if title else work
        for name in attachment_names:
            work = work.replace(name, " ")
        work = re.sub(r"제목\s*", " ", work)
        work = re.sub(r"구분\s*\S+", " ", work)
        work = re.sub(r"등록일\s*\d{4}[.\-/]\d{1,2}[.\-/]\d{1,2}", " ", work)
        work = re.sub(r"작성자\s*\S+", " ", work)
        work = re.sub(r"조회수\s*\d+", " ", work)
        work = re.sub(r"첨부파일", " ", work)
        work = re.sub(r"목록|이전글|다음글", " ", work)
        work = clean_text(work)

        if looks_like_noise(work, title, attachment_names):
            continue

        score = len(work)
        if score > best_score:
            best_score = score
            best = work

    if not best:
        page_text = extract_text(soup)
        if title:
            page_text = page_text.replace(title, " ")
        for name in attachment_names:
            page_text = page_text.replace(name, " ")
        page_text = re.sub(r"제목\s*", " ", page_text)
        page_text = re.sub(r"구분\s*\S+", " ", page_text)
        page_text = re.sub(r"등록일\s*\d{4}[.\-/]\d{1,2}[.\-/]\d{1,2}", " ", page_text)
        page_text = re.sub(r"작성자\s*\S+", " ", page_text)
        page_text = re.sub(r"조회수\s*\d+", " ", page_text)
        page_text = re.sub(r"첨부파일", " ", page_text)
        page_text = re.sub(r"목록|이전글|다음글", " ", page_text)
        page_text = clean_text(page_text)
        if not looks_like_noise(page_text, title, attachment_names):
            best = page_text

    return best[:3000] if best else ""


def extract_post(post_url, board_name):
    html_text = get_html(post_url)
    soup = BeautifulSoup(html_text, "lxml")

    page_text = extract_text(soup)
    title = find_title(page_text, soup, board_name)
    published = extract_registered_date(page_text)
    attachments = extract_attachments(soup, post_url)
    content_text = extract_body_text(soup, title, attachments)

    boardid, idx = parse_post_meta_from_url(post_url)
    guid = f"{boardid}-{idx}" if boardid and idx else post_url

    return {
        "board_name": board_name,
        "title": title,
        "link": post_url,
        "guid": guid,
        "published": published,
        "content_text": content_text,
        "attachments": attachments,
    }


def format_post_date(dt):
    if not dt:
        return ""
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d")


def build_description(item):
    attachment_names = " | ".join(att["name"] for att in item["attachments"])
    attachment_urls = " | ".join(att["url"] for att in item["attachments"])
    body = item["content_text"] or ""

    parts = [
        f"[게시판] {item['board_name']}",
        f"[제목] {item['title']}",
        f"[게시일] {format_post_date(item['published'])}",
        f"[본문요약] {body}",
        f"[첨부파일명] {attachment_names}",
        f"[첨부파일URL] {attachment_urls}",
        f"[게시글링크] {item['link']}",
    ]
    return " ".join(parts).strip()


def write_feed(board_name, items, output_path, feed_path_name):
    fg = FeedGenerator()
    fg.title(f"KOBIA {board_name} RSS")
    fg.link(href=f"https://example.github.io/{feed_path_name}", rel="self")
    fg.description(f"KOBIA {board_name} 게시판 RSS")
    fg.language("ko")

    for item in items:
        fe = fg.add_entry()
        fe.id(item["guid"])
        fe.title(item["title"])
        fe.link(href=item["link"])
        fe.category(term=item["board_name"])

        if item["published"]:
            fe.pubDate(item["published"])

        description = build_description(item)
        fe.description(description)
        fe.content(description, type="CDATA")

        if item["attachments"] and item["attachments"][0]["url"]:
            first = item["attachments"][0]
            fe.enclosure(first["url"], 0, "application/octet-stream")

    fg.rss_file(output_path, pretty=True)


def write_index():
    html_text = """<!DOCTYPE html>
<html lang="ko">
<head>
  <meta charset="UTF-8">
  <title>KOBIA RSS 목록</title>
</head>
<body>
  <h1>KOBIA RSS 목록</h1>
  <ul>
    <li><a href="./trend.xml">업계동향 RSS</a></li>
    <li><a href="./trend_3.xml">임상·허가 RSS</a></li>
    <li><a href="./trend_4.xml">글로벌 동향 RSS</a></li>
    <li><a href="./all.xml">통합 RSS</a></li>
  </ul>
</body>
</html>
"""
    with open("docs/index.html", "w", encoding="utf-8") as f:
        f.write(html_text)


def main():
    ensure_docs_dir()
    print(f"모니터링 범위: {MONITOR_START} ~ {MONITOR_END}")

    all_items = []

    for board in BOARDS:
        try:
            links = extract_post_links(board["list_url"], board["board_id"])
        except Exception as e:
            print(f"목록 페이지 처리 제외: {board['list_url']} / {e}")
            links = []

        items = []
        for link in links:
            try:
                item = extract_post(link, board["name"])
                if not within_monitor_range(item["published"]):
                    continue
                items.append(item)
                all_items.append(item)
            except Exception as e:
                print(f"게시글 처리 제외: {link} / {e}")

        items.sort(key=lambda x: x["published"] or datetime(2000, 1, 1, tzinfo=timezone.utc), reverse=True)
        write_feed(board["name"], items, board["output"], os.path.basename(board["output"]))
        print(f"[{board['name']}] 수집 {len(items)}건")

    all_items_sorted = sorted(
        all_items,
        key=lambda x: x["published"] or datetime(2000, 1, 1, tzinfo=timezone.utc),
        reverse=True,
    )
    write_feed("통합", all_items_sorted, "docs/all.xml", "all.xml")
    write_index()
    print(f"전체 수집 {len(all_items_sorted)}건 완료")


if __name__ == "__main__":
    main()
