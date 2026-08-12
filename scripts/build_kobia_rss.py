import os
import re
import time
import requests
import urllib3
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse, parse_qs
from feedgen.feed import FeedGenerator
from datetime import datetime, timezone
from requests.exceptions import SSLError, RequestException

# KOBIA 서버 인증서 체인 검증 실패(CERTIFICATE_VERIFY_FAILED) 대비:
# verify=False 재시도 시 뜨는 경고 메시지를 숨긴다.
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

MAX_ITEMS_PER_BOARD = 30

# 공통 세션(헤더 재사용)
session = requests.Session()
session.headers.update(HEADERS)


def ensure_docs_dir():
    os.makedirs("docs", exist_ok=True)


def get_html(url, retries=3, delay=2):
    """
    정상 SSL 검증으로 먼저 접속을 시도하고,
    KOBIA처럼 인증서 검증이 실패하는 경우에만 verify=False로 재시도한다.
    네트워크가 일시적으로 흔들릴 때를 대비해 재시도 로직도 포함한다.
    """
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


def extract_post_links(list_url, board_id):
    html = get_html(list_url)
    soup = BeautifulSoup(html, "lxml")

    links = []
    seen = set()

    for a in soup.find_all("a", href=True):
        href = a["href"]
        full_url = urljoin(list_url, href)

        if "mode=view" not in full_url and "idx=" not in full_url:
            continue
        if f"boardid={board_id}" not in full_url:
            continue

        if full_url not in seen:
            seen.add(full_url)
            links.append(full_url)

    return links[:MAX_ITEMS_PER_BOARD]


def extract_text(element):
    if not element:
        return ""
    return " ".join(element.get_text(separator=" ", strip=True).split())


def guess_date(text):
    """
    페이지 텍스트에서 날짜를 추출해 timezone-aware datetime(UTC)으로 반환한다.
    feedgen의 pubDate/published는 tzinfo가 없으면 ValueError를 발생시키므로
    반드시 UTC 타임존을 붙여서 반환한다.
    """
    if not text:
        return None

    patterns = [
        r"(\d{4}[.\-/]\d{1,2}[.\-/]\d{1,2}\s+\d{1,2}:\d{2})",
        r"(\d{4}[.\-/]\d{1,2}[.\-/]\d{1,2})",
    ]

    for pattern in patterns:
        m = re.search(pattern, text)
        if m:
            raw = m.group(1).replace(".", "-").replace("/", "-")
            for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d"):
                try:
                    naive_dt = datetime.strptime(raw, fmt)
                    return naive_dt.replace(tzinfo=timezone.utc)
                except ValueError:
                    pass
    return None


def parse_post_meta_from_url(url):
    parsed = urlparse(url)
    qs = parse_qs(parsed.query)
    boardid = qs.get("boardid", [""])[0]
    idx = qs.get("idx", [""])[0]
    return boardid, idx


def extract_attachments(soup, page_url):
    attachments = []

    for a in soup.find_all("a", href=True):
        href = a["href"]
        text = a.get_text(" ", strip=True)
        full_url = urljoin(page_url, href)

        # 첨부파일로 보이는 확장자 후보
        if any(ext in full_url.lower() for ext in [
            ".pdf", ".xlsx", ".xls", ".doc", ".docx",
            ".hwp", ".zip", ".csv", ".ppt", ".pptx"
        ]):
            attachments.append({
                "name": text or os.path.basename(full_url),
                "url": full_url
            })
            continue

        # 다운로드 링크 형태 후보
        if any(keyword in full_url.lower() for keyword in ["download", "down", "file"]):
            attachments.append({
                "name": text or "첨부파일",
                "url": full_url
            })

    # 중복 제거
    unique = []
    seen = set()
    for att in attachments:
        key = (att["name"], att["url"])
        if key not in seen:
            seen.add(key)
            unique.append(att)

    return unique


def extract_post(post_url, board_name):
    html = get_html(post_url)
    soup = BeautifulSoup(html, "lxml")

    title = ""
    if soup.title:
        title = soup.title.get_text(strip=True)

    title_candidates = [
        soup.find(["h1", "h2", "h3"]),
        soup.find(class_=re.compile("title|subject", re.I)),
        soup.find("th"),
    ]
    for c in title_candidates:
        text = extract_text(c)
        if text and len(text) > 3:
            title = text
            break

    page_text = extract_text(soup)
    published = guess_date(page_text)
    attachments = extract_attachments(soup, post_url)

    body_candidates = [
        soup.find(class_=re.compile("content|view|body", re.I)),
        soup.find("td"),
        soup.find("article"),
        soup.find("div"),
    ]

    content_text = ""
    for c in body_candidates:
        text = extract_text(c)
        if text and len(text) > 50:
            content_text = text[:3000]
            break

    boardid, idx = parse_post_meta_from_url(post_url)
    guid = f"{boardid}-{idx}" if boardid and idx else post_url

    return {
        "board_name": board_name,
        "title": title or f"{board_name} 게시물",
        "link": post_url,
        "guid": guid,
        "published": published,
        "content_text": content_text,
        "attachments": attachments,
    }


def build_description(item):
    html = []
    html.append(f"<p><strong>게시판:</strong> {item['board_name']}</p>")

    if item["content_text"]:
        html.append(f"<p><strong>본문:</strong> {item['content_text']}</p>")

    if item["attachments"]:
        html.append("<p><strong>첨부파일:</strong></p><ul>")
        for att in item["attachments"]:
            html.append(f'<li><a href="{att["url"]}">{att["name"]}</a></li>')
        html.append("</ul>")

    html.append(f'<p><a href="{item["link"]}">게시글 바로가기</a></p>')
    return "".join(html)


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

        if item["published"]:
            fe.pubDate(item["published"])

        description = build_description(item)
        fe.description(description)
        fe.content(description, type="CDATA")

        if item["attachments"]:
            first = item["attachments"][0]
            fe.enclosure(first["url"], 0, "application/octet-stream")

    fg.rss_file(output_path, pretty=True)


def write_index():
    html = """<!DOCTYPE html>
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
        f.write(html)


def main():
    ensure_docs_dir()

    all_items = []

    for board in BOARDS:
        try:
            links = extract_post_links(board["list_url"], board["board_id"])
        except Exception as e:
            print(f"목록 페이지 처리 실패, 건너뜀: {board['list_url']} / {e}")
            links = []

        items = []
        for link in links:
            try:
                item = extract_post(link, board["name"])
                items.append(item)
                all_items.append(item)
            except Exception as e:
                print(f"게시글 처리 중 제외: {link} / {e}")

        write_feed(board["name"], items, board["output"], os.path.basename(board["output"]))

    all_items_sorted = sorted(
        all_items,
        key=lambda x: x["published"] or datetime(2000, 1, 1, tzinfo=timezone.utc),
        reverse=True
    )
    write_feed("통합", all_items_sorted, "docs/all.xml", "all.xml")
    write_index()


if __name__ == "__main__":
    main()
