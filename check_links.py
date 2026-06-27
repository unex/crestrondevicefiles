import concurrent.futures
import datetime
from pathlib import Path

import requests
from fake_useragent import UserAgent
from jinja2 import Environment, FileSystemLoader

LINKS_FILE = Path("links.txt")
SITE_DIR = Path("site")
TEMPLATES_DIR = Path("templates")
BASE_URL = "https://crestrondevicefiles.blob.core.windows.net/"
MAX_WORKERS = 20
TIMEOUT = 20


def build_headers() -> dict:
    return {
        "User-Agent": UserAgent().chrome,
        "Accept": "*/*",
    }


def check_link(relative_url: str, headers: dict) -> dict:
    url = BASE_URL + relative_url
    status = None
    error = None

    try:
        resp = requests.head(url, headers=headers, allow_redirects=True, timeout=TIMEOUT)

        # Some endpoints don't support HEAD, fall back to a streamed GET.
        if resp.status_code in (403, 405):
            resp = requests.get(url, headers=headers, allow_redirects=True, timeout=TIMEOUT, stream=True)
            resp.close()

        status = resp.status_code

    except requests.exceptions.RequestException as e:
        error = str(e)

    return {
        "url": relative_url,
        "full_url": url,
        "status": status,
        "ok": status == 200,
        "error": error,
    }


def check_all_links(links: list[str]) -> list[dict]:
    headers = build_headers()
    results = []

    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = [executor.submit(check_link, link, headers) for link in links]
        for future in concurrent.futures.as_completed(futures):
            results.append(future.result())

    results.sort(key=lambda r: r["url"])
    return results


def build_tree(results: list[dict]) -> dict:
    root = {"type": "dir", "name": "", "path": "", "children": {}, "total_count": 0, "broken_count": 0}

    for r in results:
        parts = r["url"].split("/")
        node = root
        path = ""
        for part in parts[:-1]:
            path = part if not path else f"{path}/{part}"
            node = node["children"].setdefault(
                part, {"type": "dir", "name": part, "path": path, "children": {}, "total_count": 0, "broken_count": 0}
            )
        node["children"][parts[-1]] = {"type": "file", "name": parts[-1], "path": r["url"], "result": r}

    def finalize(node: dict) -> tuple[int, int]:
        if node["type"] == "file":
            return 1, 0 if node["result"]["ok"] else 1

        total = broken = 0
        for child in node["children"].values():
            t, b = finalize(child)
            total += t
            broken += b

        node["total_count"] = total
        node["broken_count"] = broken
        node["children"] = sorted(
            node["children"].values(), key=lambda n: (n["type"] != "dir", n["name"].lower())
        )
        return total, broken

    finalize(root)
    return root


def generate_html(results: list[dict], generated_at: str) -> str:
    tree = build_tree(results)

    env = Environment(loader=FileSystemLoader(TEMPLATES_DIR), autoescape=True)
    template = env.get_template("index.html.j2")
    return template.render(
        generated_at=generated_at,
        root=tree,
    )


def main():
    links = sorted(set(LINKS_FILE.read_text().split()))
    print(f"Checking {len(links)} links...")

    results = check_all_links(links)
    broken = [r for r in results if not r["ok"]]
    print(f"Done: {len(results) - len(broken)} OK, {len(broken)} broken")

    generated_at = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    SITE_DIR.mkdir(exist_ok=True)
    (SITE_DIR / "index.html").write_text(generate_html(results, generated_at))


if __name__ == "__main__":
    main()
