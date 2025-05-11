

import re
import zipfile
import argparse
import traceback
import subprocess

from pathlib import Path
from urllib.parse import urlparse


import backoff
import requests
from tqdm import tqdm
from fake_useragent import UserAgent


TEMP_DIR = Path("temp")
ROOT_DIR = Path("root")

RE_LINKS = re.compile(r'https?://(?:crestrondevicefiles\.blob\.core\.windows\.net|devicefiles\.crestron\.io)[^\s"<>]+')

ARCHIVE_EXTENSIONS = {'.zip', '.puf', '.apk'}
TEXT_EXTENSIONS    = {'.json', '.txt', '.hash'}
BINARY_EXTENSIONS  = {'.bin'}


class Manager:
    links: set[str]
    new_links: set[str]
    progress_bar = True
    search_links = True

    def __init__(self, base_folder='root', links_file='links.txt'):
        self.ua = UserAgent()

        TEMP_DIR.mkdir(exist_ok=True)
        ROOT_DIR.mkdir(exist_ok=True)


    def get_relative_link(self, url: str):
        parsed = urlparse(url)
        domain = parsed.netloc
        return url.split(domain + '/', 1)[1]


    def get_file_path(self, relative_url: str) -> Path:
        return ROOT_DIR.joinpath(relative_url)


    def extract_links(self, text: str) -> set:
        links = set()
        matches = RE_LINKS.findall(text)
        for match in matches:
            links.add(self.get_relative_link(match))
        return links


    def remove_directory(self, directory: Path):
        for root, dirs, files in directory.walk():
            for file in files:
                file_path = root.joinpath(file)
                file_path.unlink()
            for dir in dirs:
                dir_path = root.joinpath(dir)
                self.remove_directory(dir_path)

        directory.rmdir()


    def handle_archive(self, archive_path: Path, target_dir: Path):
        try:
            if target_dir.is_dir() and target_dir.exists() and target_dir.glob("*"):
                print(f"{archive_path} already extracted to {target_dir}, skipping extraction.")

            else:
                target_dir.mkdir(exist_ok=True)

                try:
                    with zipfile.ZipFile(archive_path, 'r') as zip_ref:
                        zip_ref.extractall(target_dir)
                    print(f"Extracted {archive_path} to {target_dir}")

                except zipfile.BadZipFile:
                    print("Error extracting")

            self.process_nested_archives(target_dir)

        finally:
            self.remove_directory(target_dir)


    def process_nested_archives(self, directory: Path) -> None:
        for root, _, files in directory.walk():
            for file_name in files:
                nested_archive = root.joinpath(file_name)
                if(nested_archive.suffix in ARCHIVE_EXTENSIONS):
                    archive_name = nested_archive.stem
                    target_dir = directory.joinpath(f"{archive_name}_extracted")
                    self.handle_archive(nested_archive, target_dir)
                    nested_archive.unlink()

                    self.process_nested_archives(target_dir)


    def process_new_archive(self, archive_path: Path) -> set[str]:
        archive_name = archive_path.stem
        target_dir = TEMP_DIR.joinpath(f"{archive_name}_extracted")
        self.handle_archive(archive_path, target_dir)
        return self.strings_search(target_dir)


    def download_links(self, links: str) -> None:
        for relative_url in links:
            file_path = self.get_file_path(relative_url)

            # Skip download if file exists and is not a text file.
            if file_path.exists() and file_path.suffix not in TEXT_EXTENSIONS:
                print(f"{relative_url} exists, skipping...")
                continue

            # In actions we only want to download the other files if they are new new
            if self.gh_actions and file_path.suffix not in TEXT_EXTENSIONS and relative_url in self.links:
                continue

            self.do_download(relative_url)


    @backoff.on_exception(backoff.expo, requests.exceptions.RequestException, max_tries=10)
    def do_download(self, relative_url):
        headers = {
            "User-Agent": self.ua.chrome,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,image/apng,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "gzip, deflate, br",
            "Connection": "keep-alive",
            "Upgrade-Insecure-Requests": "1",
        }

        response = requests.get(
            f"https://crestrondevicefiles.blob.core.windows.net/{relative_url}",
            headers=headers,
            stream=True,
            timeout=None
        )

        if response.status_code == 200:
            total_size = int(response.headers.get('content-length', 0))

            file_path = self.get_file_path(relative_url)
            file_path.parent.mkdir(exist_ok=True, parents=True)
            part_file_path = file_path.parent.joinpath(f"{file_path.name}.part")

            if part_file_path.exists():
                part_file_path.unlink()

            if not self.progress_bar:
                print(f"Downloading {relative_url}")

            with open(part_file_path, 'wb') as file, tqdm(
                total=total_size,
                unit='B',
                unit_scale=True,
                desc=f"Downloading {relative_url}",
                position=0,
                leave=True,
                disable=not self.progress_bar,
            ) as progress_bar:
                for chunk in response.iter_content(chunk_size=1024):
                    if chunk:
                        file.write(chunk)
                        progress_bar.update(len(chunk))

            part_file_path.rename(file_path)

            if not self.search_links:
                return

            if file_path.suffix.lower() in ARCHIVE_EXTENSIONS:
                links = self.process_new_archive(file_path)
                new_links = links - self.links
                print(f"Found {len(new_links)} new links ({len(links)} total) in {relative_url}")
                self.new_links.update(new_links)

            elif file_path.suffix.lower() in BINARY_EXTENSIONS:
                links = self.strings_search(file_path.parent)
                new_links = links - self.links
                print(f"Found {len(new_links)} new links ({len(links)} total) in {relative_url}")
                self.new_links.update(new_links)

        else:
            print(f"Download failed for {relative_url} with status code {response.status_code}")


    def search_links_in_files(self, directory: Path):
        print("Searching for plaintext links...")
        for root, _, files in directory.walk():
            for file_name in files:
                file_path = root.joinpath(file_name)
                if file_path.suffix.lower() in ARCHIVE_EXTENSIONS:
                    continue
                try:
                    with open(file_path, 'r', errors='ignore') as f:
                        content = f.read()
                    links = self.extract_links(content)
                    new_links = links - self.links
                    self.new_links.update(new_links)
                except Exception:
                    continue


    def strings_search(self, directory: Path) -> set[str]:
        print(f"strings_search {directory}")
        found_links = set()
        for root, _, files in directory.walk():
            for file_name in files:
                file_path = str(root.joinpath(file_name))
                try:
                    result = subprocess.run(['strings', file_path], capture_output=True, text=True)
                    output = result.stdout
                except Exception:
                    continue
                matches = RE_LINKS.findall(output)
                for url in matches:
                    found_links.add(self.get_relative_link(url))
        return found_links


    def force_process_archives(self):
        print("Forcing processing of archives...")
        for root, _, files in ROOT_DIR.walk():
            for file_name in files:
                file_path = root.joinpath(file_name)
                if file_path.suffix.lower() in ARCHIVE_EXTENSIONS:
                    links = self.process_new_archive(file_path)
                    new_links = links - self.links
                    print(f"Found {len(new_links)} new links ({len(links)} total) in {file_name}")
                    if new_links:
                        self.new_links.update(new_links)


    def run(self, args):
        self.gh_actions = args.gh_actions

        if self.gh_actions:
            self.progress_bar = False
            print("Running in gh-actions")

        with open('links.txt', 'r') as file:
            content = file.read()
            self.links = set(content.split())

        try:
            if args.force_archives:
                self.links.update(self.new_links)
                self.force_process_archives()
                return

            if args.download:
                self.progress_bar = False
                self.search_links = False
                self.download_links(self.links)
                return

            self.new_links = set(self.links) # copy

            while self.new_links:
                new_links = set(self.new_links) # copy
                self.new_links.clear()

                print(f"Found {len(new_links)} new links...")

                self.download_links(new_links)
                self.links.update(new_links)

                self.search_links_in_files(ROOT_DIR)

            print("Completed links search")

        except Exception:
            traceback.print_exc()

        finally:
            if self.links:
                with open('links.txt', 'w') as f:
                    f.write("\n".join(sorted(self.links)) + "\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--gh-actions", action="store_true", help="For gh-actions")
    parser.add_argument("--download", action="store_true", help="Download all links")
    parser.add_argument("--force-archives", action="store_true", help="Force processing of archives")
    args = parser.parse_args()

    manager = Manager()
    manager.run(args)
