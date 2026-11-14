from datetime import date
from html.parser import HTMLParser
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Lock
from time import monotonic
from urllib.error import URLError
from urllib.request import Request, urlopen
import json
import os
import re
from urllib.parse import parse_qs, urlsplit


APP_DIR = Path(__file__).resolve().parent
CPC_URL = "https://www.cpc.com.tw/"
CACHE_SECONDS = 3600
PORT = int(os.environ.get("PORT", "4173"))
PRICE_CACHE = {"fetched_at": 0, "data": None}
CACHE_LOCK = Lock()


class CPCArticleParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.articles = []
        self.current = None
        self.depth = 0
        self.in_date = False

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag == "div" and "essay" in attributes.get("class", "").split():
            self.current = {"text": [], "date": []}
            self.depth = 1
        elif self.current:
            if tag == "div":
                self.depth += 1
            if tag == "i" and "mark" in attributes.get("class", "").split():
                self.in_date = True

    def handle_endtag(self, tag):
        if not self.current:
            return
        if tag == "i":
            self.in_date = False
        if tag == "div":
            self.depth -= 1
            if self.depth == 0:
                self.articles.append(self.current)
                self.current = None
                self.in_date = False

    def handle_data(self, data):
        if self.current:
            self.current["text"].append(data)
            if self.in_date:
                self.current["date"].append(data)


def parse_cpc_prices(html):
    parser = CPCArticleParser()
    parser.feed(html)
    for article in parser.articles:
        text = re.sub(r"\s+", " ", " ".join(article["text"]))
        prices = {}
        for grade in ("92", "95", "98"):
            match = re.search(
                rf"{grade}\s*無鉛汽油每公升\s*([0-9]+(?:\.[0-9]+)?)\s*元",
                text,
            )
            if match:
                prices[grade] = float(match.group(1))
        if len(prices) != 3:
            continue

        published = " ".join(article["date"]).strip()
        date_match = re.search(r"(\d{2,3})-(\d{2})-(\d{2})", published)
        if not date_match:
            continue
        year, month, day = map(int, date_match.groups())
        try:
            published_at = date(year + 1911, month, day).isoformat()
        except ValueError:
            continue
        return {"prices": prices, "publishedAt": published_at, "source": CPC_URL}
    raise ValueError("中油官網目前沒有可辨識的最新汽油價格公告")


def get_cpc_prices(force_refresh=False):
    with CACHE_LOCK:
        now = monotonic()
        if not force_refresh and PRICE_CACHE["data"] and now - PRICE_CACHE["fetched_at"] < CACHE_SECONDS:
            return PRICE_CACHE["data"]

        request = Request(CPC_URL, headers={"User-Agent": "ScooterHelper/1.0"})
        try:
            with urlopen(request, timeout=12) as response:
                html = response.read().decode("utf-8", "replace")
        except (URLError, TimeoutError) as error:
            raise RuntimeError("無法連線中油官網，請稍後再試") from error

        data = parse_cpc_prices(html)
        PRICE_CACHE["data"] = data
        PRICE_CACHE["fetched_at"] = now
        return data


class ScooterHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(APP_DIR), **kwargs)

    def do_GET(self):
        if urlsplit(self.path).path != "/api/fuel-prices":
            super().do_GET()
            return

        try:
            force_refresh = parse_qs(urlsplit(self.path).query).get("refresh") == ["1"]
            body = json.dumps(get_cpc_prices(force_refresh), ensure_ascii=False).encode("utf-8")
            status = 200
        except (RuntimeError, ValueError) as error:
            body = json.dumps({"error": str(error)}, ensure_ascii=False).encode("utf-8")
            status = 502

        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)


if __name__ == "__main__":
    server = ThreadingHTTPServer(("", PORT), ScooterHandler)
    print(f"小白機車小幫手：http://localhost:{PORT}")
    print("按 Ctrl+C 停止伺服器")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()