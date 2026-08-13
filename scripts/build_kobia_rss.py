import os
import re
import time
import requests
import urllib3
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse, parse_qs
from feedgen.feed import FeedGenerator
from datetime import datetime, timezone, date
from requests.exceptions import SSLError, RequestException

# KOBIA 서버 인증서 체인 검증 실패(CERTIFICATE_VERIFY_FAILED) 대비:
# verify=False 재시도 시 뜨는 경고 메시지를 숨긴다.
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

BASE_URL = "https://www.kobia.kr"

# ─────────────────────────────────────────────────────────────
# 모니터링(수집) 범위 : 이 기간에 등록된 게시물만 RSS에 담는다.
# ─────────────────────────────────────────────────────────────
MONITOR_START = date(2026, 8, 1)
MONITOR_END = date(2026, 8, 31)

# ─────────────────────────────────────────────────────────────
# 첨부파일 다운로드 URL 템플릿
# KOBIA 상세 페이지의 첨부파일 링크는 아래처럼 자바스크립트 함수로 되어 있다.
#   javascript:download('trend_4','48','14924','image/jpeg')
#   인자 순서 = (게시판ID, 게시글idx, 파일idx, MIME타입)
# 이 인자들을 실제 다운로드 주소로 조합한다.
#
# ※ 아래 템플릿 경로(/lib/download.php)는 일반적인 형태로 추정한 값이다.
#   실제 주소가 다르면(예: /memberlounge/download.php 등),
#   게시글 상세 페이지의 "소스 보기"에서 function download 정의를 확인해
#   이 템플릿 한 줄만 실제 경로/파라미터명에 맞게 바꾸면 된다.
# ─────────────────────────────────────────────────────────────
DOWNLOAD_URL_TEMPLATE = (
    "https://www.kobia.kr/lib/download.php"
    "?boardid={boardid}&idx={idx}&fileidx={fileidx}"
)

# javascript:download('trend_4','48','14924','image/jpeg') 형태에서 인자 추출
JS_DOWNLOAD_RE = re.compile(
    r"download\(\s*['\"]([^'\"]*)['\"]\s*,\s*['\"]([^'\"]*)['\"]\s*,\s*['\"]([^'\"]*)['\"]",
    re.I,
)

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

# 본문에서 걷어낼 메타/네비게이션 잔여 키워드
NAV_KEYWORDS = [
    "이전글", "다음글", "목록", "글쓰기", "답변", "수정", "삭제",
    "이름", "비밀번호", "인쇄", "메일",
]

# 상세 페이지 메타 헤더의 라벨(제목 다음에 나오는 항목들)
META_LABELS = "(?:구분|등록일|작성일|작성자|조회수|첨부파일)"

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


def extract_title_from_text(page_text):
    """
    상세 페이지 텍스트의 '제목 [실제 제목] 구분/등록일/작성자...' 패턴에서
    진짜 게시글 제목만 뽑아낸다.
    """
    if not page_text:
        return None
    m = re.search(rf"제목\s*(.+?)\s*{META_LABELS}", page_text)
    if m:
        title = m.group(1).strip()
        if title:
            return title
    return None


def extract_published(page_text):
    """
    '등록일 2025.09.19' 형태에서 날짜를 우선 추출하고,
    없으면 페이지 안의 첫 날짜 패턴을 사용한다.
    항상 timezone-aware datetime(UTC)으로 반환한다.
    """
    if not page_text:
        return None

    # 1) '등록일/작성일' 라벨 뒤 날짜 우선
    m = re.search(r"(?:등록일|작성일)\s*(\d{4})[.\-/]\s*(\d{1,2})[.\-/]\s*(\d{1,2})", page_text)
    if m:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        try:
            return datetime(y, mo, d, tzinfo=timezone.utc)
        except ValueError:
            pass

    # 2) fallback: 페이지 내 첫 날짜 패턴
    m2 = re.search(r"(\d{4})[.\-/](\d{1,2})[.\-/](\d{1,2})", page_text)
    if m2:
        y, mo, d = int(m2.group(1)), int(m2.group(2)), int(m2.group(3))
        try:
            return datetime(y, mo, d, tzinfo=timezone.utc)
        except ValueError:
            pass

    return None


def parse_post_meta_from_url(url):
    parsed = urlparse(url)
    qs = parse_qs(parsed.query)
    boardid = qs.get("boardid", [""])[0]
    idx = qs.get("idx", [""])[0]
    return boardid, idx


def resolve_download_url(href):
    """
    javascript:download('trend_4','48','14924','image/jpeg') 형태의 링크를
    실제 다운로드 URL로 변환한다. 파싱에 실패하면 None을 반환한다.
    """
    if not href:
        return None
    m = JS_DOWNLOAD_RE.search(href)
    if not m:
        return None
    boardid, idx, fileidx = m.group(1), m.group(2), m.group(3)
    return DOWNLOAD_URL_TEMPLATE.format(boardid=boardid, idx=idx, fileidx=fileidx)


ATTACH_EXTS = [
    ".pdf", ".xlsx", ".xls", ".doc", ".docx", ".hwp", ".hwpx",
    ".zip", ".csv", ".ppt", ".pptx", ".jpg", ".jpeg", ".png", ".gif",
]


def extract_attachments(soup, page_url):
    attachments = []

    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        text = a.get_text(" ", strip=True)

        real_url = None

        # 1) 자바스크립트 download() 함수 형태 → 실제 URL로 변환
        if "download(" in href.lower():
            real_url = resolve_download_url(href)

        # 2) 일반 링크: 첨부 확장자 또는 다운로드성 경로
        if not real_url:
            full_url = urljoin(page_url, href)
            lower = full_url.lower()
            if any(ext in lower for ext in ATTACH_EXTS):
                real_url = full_url
            elif any(k in lower for k in ["download", "/down", "fileidx", "file="]):
                real_url = full_url

        if not real_url:
            continue

        name = text or os.path.basename(urlparse(real_url).path) or "첨부파일"
        attachments.append({"name": name.strip(), "url": real_url})

    # 중복 제거(이름+URL 기준)
    unique = []
    seen = set()
    for att in attachments:
        key = (att["name"], att["url"])
        if key not in seen:
            seen.add(key)
            unique.append(att)

    return unique


def build_clean_summary(content_text, attachments):
    """
    페이지에서 긁어온 텍스트에서 메타정보(제목/구분/등록일/작성자/조회수),
    첨부파일명, 네비게이션 문구를 걷어내고 실제 본문만 남긴다.
    """
    text = content_text or ""

    # '조회수 000' 이후를 본문으로 추정 (메타 헤더가 대개 조회수로 끝남)
    m = re.search(r"조회수\s*[\d,]+\s*(.*)", text)
    if m:
        text = m.group(1)

    # 메타/첨부 표기 제거
    text = re.sub(r"첨부파일", " ", text)
    text = re.sub(r"조회수\s*[\d,]+", " ", text)
    text = re.sub(r"(?:등록일|작성일)\s*\d{4}[.\-/]\d{1,2}[.\-/]\d{1,2}(\s+\d{1,2}:\d{2})?", " ", text)
    text = re.sub(r"작성자\s*\S+", " ", text)
    text = re.sub(r"구분\s*\S+", " ", text)
    text = re.sub(r"^\s*제목\s*", " ", text)

    # 첨부파일명 자체를 본문에서 제거
    for att in attachments:
        name = att.get("name")
        if name:
            text = text.replace(name, " ")

    # 네비게이션 잔여 제거
    for kw in NAV_KEYWORDS:
        text = text.replace(kw, " ")

    text = " ".join(text.split())

    if not text:
        text = "세부 내용은 게시글 또는 첨부파일을 참고하세요."

    return text[:600]


def extract_post(post_url, board_name):
    html = get_html(post_url)
    soup = BeautifulSoup(html, "lxml")

    page_text = extract_text(soup)

    # 1) 제목: '제목 ... 구분/등록일...' 패턴 우선
    title = extract_title_from_text(page_text)

    # 2) fallback: 헤딩/타이틀 후보
    if not title:
        title_candidates = [
            soup.find(class_=re.compile("subject|title", re.I)),
            soup.find(["h1", "h2", "h3"]),
        ]
        for c in title_candidates:
            t = extract_text(c)
            if t and len(t) > 3:
                title = t
                break

    published = extract_published(page_text)
    attachments = extract_attachments(soup, post_url)

    # 본문 후보
    body_candidates = [
        soup.find(class_=re.compile("content|view|body", re.I)),
        soup.find("td"),
        soup.find("article"),
        soup.find("div"),
    ]

    content_text = ""
    for c in body_candidates:
        t = extract_text(c)
        if t and len(t) > 50:
            content_text = t[:3000]
            break

    clean_summary = build_clean_summary(content_text, attachments)

    boardid, idx = parse_post_meta_from_url(post_url)
    guid = f"{boardid}-{idx}" if boardid and idx else post_url

    return {
        "board_name": board_name,
        "title": title or f"{board_name} 게시물",
        "link": post_url,
        "guid": guid,
        "published": published,
        "summary": clean_summary,
        "attachments": attachments,
    }


def in_monitor_range(item):
    """등록일이 모니터링 범위(MONITOR_START ~ MONITOR_END) 안인지 확인."""
    if not item["published"]:
        return False
    d = item["published"].date()
    return MONITOR_START <= d <= MONITOR_END


def published_str(item):
    """게시일을 YYYY-MM-DD 문자열로 반환(없으면 빈 문자열)."""
    if item["published"]:
        return item["published"].strftime("%Y-%m-%d")
    return ""


def build_description(item):
    """
    RSS description을 순수 텍스트로 구성한다.
    각 항목을 라벨 마커로 구분해 Power Automate에서 나눠 쓰기 쉽게 한다.
    """
    attachments = item["attachments"]
    names = " | ".join(a["name"] for a in attachments) if attachments else ""
    urls = " | ".join(a["url"] for a in attachments) if attachments else ""

    lines = []
    lines.append(f"[게시판] {item['board_name']}")
    lines.append(f"[제목] {item['title']}")
    lines.append(f"[게시일] {published_str(item)}")
    lines.append(f"[본문요약] {item['summary']}")
    lines.append(f"[첨부파일명] {names}")
    lines.append(f"[첨부파일URL] {urls}")
    lines.append(f"[게시글링크] {item['link']}")

    return "\n".join(lines)


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

        # 게시판명을 category로 넣어 Power Automate에서 게시판 컬럼에 매핑 가능
        fe.category(term=item["board_name"])

        if item["published"]:
            fe.pubDate(item["published"])

        description = build_description(item)
        fe.description(description)

        # 대표 첨부 1개는 enclosure로도 제공(RSS 리더 호환용)
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

    print(f"모니터링 범위: {MONITOR_START} ~ {MONITOR_END}")

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
            except Exception as e:
                print(f"게시글 처리 중 제외: {link} / {e}")
                continue

            # 모니터링 범위 필터
            if not in_monitor_range(item):
                continue

            items.append(item)
            all_items.append(item)

        print(f"[{board['name']}] 수집 {len(items)}건")
        write_feed(board["name"], items, board["output"], os.path.basename(board["output"]))

    all_items_sorted = sorted(
        all_items,
        key=lambda x: x["published"] or datetime(2000, 1, 1, tzinfo=timezone.utc),
        reverse=True
    )
    write_feed("통합", all_items_sorted, "docs/all.xml", "all.xml")
    write_index()

    print(f"전체 수집 {len(all_items)}건 완료")


if __name__ == "__main__":
    main()
