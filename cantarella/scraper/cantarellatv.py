from cantarella.core.proxy import get_random_proxy, get_proxy_dict
from pathlib import Path
from queue import Queue
import math
from curl_cffi import requests
import re
import json
import time
import subprocess
import shutil
import os as _os
from threading import Thread
from cantarella.scraper.megacloud import Megacloud


class cantarellatvDownloader:
    def __init__(self, download_path="anime_downloads", progress_queue=None):
        self.download_path = Path(download_path)
        self.download_path.mkdir(exist_ok=True)
        self.binary_path = self._get_binary_path()
        self.progress_queue = progress_queue or Queue()
        self.base_url = "https://aniwatchtv.to"
        self.headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        }
        self.proxy = get_random_proxy()
        self.session = requests.Session()
        proxy_dict = get_proxy_dict(self.proxy)
        if proxy_dict:
            self.session.proxies.update(proxy_dict)

    def _get_binary_path(self):
        candidates = [
            Path("binary") / "N_m3u8DL-RE",           # Linux (local binary folder)
            Path("binary") / "N_m3u8DL-RE.exe",       # Windows local
            Path("/usr/local/bin/N_m3u8DL-RE"),        # Docker / Heroku container
        ]
        for p in candidates:
            if p.exists():
                print(f"Found N_m3u8DL-RE binary at: {p}")
                return p
        # Last resort: check PATH via shutil.which
        which_path = shutil.which("N_m3u8DL-RE")
        if which_path:
            print(f"Found N_m3u8DL-RE in PATH: {which_path}")
            return Path(which_path)
        raise FileNotFoundError(f"N_m3u8DL-RE binary not found. Checked: {candidates} and PATH")

    def _format_bytes(self, bytes_num):
        if bytes_num == 0:
            return '0 B'
        size_name = ["B", "KB", "MB", "GB", "TB"]
        i = int(math.floor(math.log(bytes_num, 1024)))
        p = math.pow(1024, i)
        s = round(bytes_num / p, 2)
        return f"{s} {size_name[i]}"

    def get_episode_id(self, url):
        if "aniwatchtv.to" in url:
             match = re.search(r'ep=(\d+)', url)
             if match: return match.group(1)

        match = re.search(r'/([^/]+)-episode-(\d+)', url)
        if not match:
             match = re.search(r'watch/([^/]+)-(\d+)', url)

        if match:
            anime_name = match.group(1).replace('-', ' ')
            ep_num = match.group(2)
            return self.search_cantarella(anime_name, ep_num)

        match = re.search(r'-(\d+)$', url)
        if match: return match.group(1)
        return None

    def search_cantarella(self, anime_name, ep_num):
        search_url = f"{self.base_url}/search?keyword={anime_name.replace(' ', '+')}"
        try:
            resp = self.session.get(search_url, headers=self.headers, impersonate="chrome")
            match = re.search(rf'href="/([^"]+)"[^>]*title="{re.escape(anime_name)}"', resp.text, re.I)
            if not match:
                match = re.search(r'href="/([^"/]+)-(\d+)"', resp.text)

            if match:
                anime_id = match.group(2) if len(match.groups()) > 1 else match.group(1).split('-')[-1].split('?')[0]
                ep_list_url = f"{self.base_url}/ajax/v2/episode/list/{anime_id}"
                resp_eps = self.session.get(ep_list_url, headers={"X-Requested-With": "XMLHttpRequest", **self.headers}, impersonate="chrome")
                if resp_eps.status_code == 200:
                    html = resp_eps.json().get('html', '')
                    ep_match = re.search(rf'data-number="{ep_num}"[^>]+data-id="(\d+)"', html)
                    if ep_match:
                        return ep_match.group(1)
        except:
            pass
        return None

    def get_episode_data(self, ep_id):
        server_url = f"{self.base_url}/ajax/v2/episode/servers?episodeId={ep_id}"
        result = {'sub': None, 'dub': None}
        try:
            resp_servers = self.session.get(server_url, headers={"X-Requested-With": "XMLHttpRequest", **self.headers}, impersonate="chrome")
            if resp_servers.status_code != 200: return None

            html = resp_servers.json().get('html', '')

            def find_sources_with_priority(data_type):
                # Priority: MegaCloud (1), then VidSrc (4)
                # Selector matches: data-type="sub" data-id="123" data-server-id="1"
                # Regex will find all matches for the given type
                pattern = rf'data-type="{data_type}" data-id="(\d+)"[^>]+data-server-id="(\d+)"'
                matches = re.findall(pattern, html)

                # Sort matches by priority list [1, 4]
                priority = {"1": 1, "4": 2}
                sorted_matches = sorted(matches, key=lambda x: priority.get(x[1], 99))

                for data_id, server_id in sorted_matches:
                    print(f"Trying server {server_id} for {data_type}...")
                    sources = self._get_sources(data_id)
                    if sources and sources.get('sources'):
                        print(f"Success! Found sources on server {server_id}")
                        return sources
                return None

            result['sub'] = find_sources_with_priority('sub')
            result['dub'] = find_sources_with_priority('dub')

            return result if result['sub'] or result['dub'] else None
        except Exception as e:
            print(f"Error in get_episode_data: {e}")
            pass
        return None

    def _get_sources(self, server_data_id):
        try:
            sources_url = f"{self.base_url}/ajax/v2/episode/sources?id={server_data_id}"
            resp_sources = self.session.get(sources_url, headers={"X-Requested-With": "XMLHttpRequest", **self.headers}, impersonate="chrome")
            if resp_sources.status_code != 200:
                 return None

            sources_data = resp_sources.json()
            embed_url = sources_data.get('link')
            if embed_url:
                # If it's a MegaCloud link, we use our specialized extractor
                if "megacloud" in embed_url.lower() or "rapid-cloud" in embed_url.lower() or "cloud-stream" in embed_url.lower():
                    scraper = Megacloud(embed_url)
                    data = scraper.extract()
                    if isinstance(data.get('sources'), list) and data['sources']:
                         return data
                else:
                    # For other servers like VidSrc, we return the raw data with the link
                    # The downloader logic might need to be adjusted if it expects direct m3u8
                    # rapid-cloud also often works similarly to MegaCloud.
                    return sources_data
        except Exception as e:
            print(f"Error in _get_sources: {e}")
            pass
        return None


    def get_episode_info(self, url):
        # First try to find anime_id and ep_id from url
        anime_id = None
        ep_id = None

        # Format: watch/hells-paradise-season-2-20405?ep=162597
        match = re.search(r'watch/([^?]+)\?ep=(\d+)', url)
        if match:
             anime_id = match.group(1)
             ep_id = match.group(2)

        if not anime_id:
             match = re.search(r'watch/([^/-]+)-(\d+)', url)
             if match: anime_id = f"{match.group(1)}-{match.group(2)}"

        if not anime_id: return "Anime", "0", "Unknown", "1"

        numeric_id = anime_id.split('-')[-1]

        # ── Try to get the REAL anime title from the page HTML ──
        anime_name = None
        try:
            page_url = f"{self.base_url}/watch/{anime_id}" if "?" not in anime_id else f"{self.base_url}/watch/{anime_id.split('?')[0]}"
            resp_page = self.session.get(page_url, headers=self.headers, impersonate="chrome")
            if resp_page.status_code == 200:
                from bs4 import BeautifulSoup
                soup = BeautifulSoup(resp_page.text, 'html.parser')
                # Primary: the main heading or title on the anime page
                title_elem = soup.select_one('h2.film-name, h2.dynamic-name, .anis-watch-detail .film-name')
                if title_elem:
                    anime_name = title_elem.get_text(strip=True)
                else:
                    # Fallback: og:title meta tag
                    og_title = soup.select_one('meta[property="og:title"]')
                    if og_title and og_title.get('content'):
                        anime_name = og_title['content'].split(' - ')[0].strip()
        except Exception as e:
            print(f"Could not fetch anime title from page: {e}")

        ep_list_url = f"{self.base_url}/ajax/v2/episode/list/{numeric_id}"

        try:
            resp_eps = self.session.get(ep_list_url, headers={"X-Requested-With": "XMLHttpRequest", **self.headers}, impersonate="chrome")
            if resp_eps.status_code == 200:
                html = resp_eps.json().get('html', '')

                # Find the specific episode in the list
                anchor = None
                if ep_id:
                    ep_match = re.search(rf'<a[^>]+data-id="{ep_id}"[^>]+>', html)
                    if ep_match: anchor = ep_match.group(0)
                else:
                    num_match = re.search(r'episode-(\d+)', url)
                    if num_match:
                         ep_num = num_match.group(1)
                         ep_match = re.search(rf'<a[^>]+data-number="{ep_num}"[^>]+>', html)
                         if ep_match: anchor = ep_match.group(0)

                if anchor:
                    num_match = re.search(r'data-number="(\d+)"', anchor)
                    title_match = re.search(r'title="([^"]+)"', anchor)

                    ep_num = num_match.group(1) if num_match else "0"
                    ep_title = title_match.group(1).replace('&#39;', "'") if title_match else "Unknown"

                    # Use the page-fetched title if available, otherwise fall back to URL slug
                    if not anime_name:
                        anime_full_name = anime_id.replace('-', ' ').title()
                        # Remove numeric ID from end
                        anime_full_name = re.sub(r' \d+$', '', anime_full_name)
                        anime_name = anime_full_name

                    season_match = re.search(r'Season (\d+)', anime_name, re.I)
                    if season_match:
                         season = season_match.group(1)
                         clean_name = anime_name.replace(season_match.group(0), '').strip()
                         anime_name = re.sub(r'\s+', ' ', clean_name)
                    else:
                         season = "1"

                    return anime_name, ep_num, ep_title, season
        except: pass

        if anime_name:
            return anime_name, "0", "Unknown", "1"
        return anime_id.replace('-', ' ').title(), "0", "Unknown", "1"

    def download_episode(self, url, quality="auto", name_override=None, season_override=None, ep_num_override=None):
        if quality == "all":
            # If all qualities are requested, download each one sequentially
            success = True
            for q in ["360", "720", "1080"]:
                if not self._download_single_episode(url, quality=q, name_override=name_override, season_override=season_override, ep_num_override=ep_num_override):
                    success = False
            return success
        else:
            return self._download_single_episode(url, quality=quality, name_override=name_override, season_override=season_override, ep_num_override=ep_num_override)

    def _download_single_episode(self, url, quality="auto", name_override=None, season_override=None, ep_num_override=None):
        ep_id = self.get_episode_id(url)
        if not ep_id:
            self.progress_queue.put({'error': 'Could not find episode ID.'})
            return False

        all_data = self.get_episode_data(ep_id)
        if not all_data or (not all_data.get('sub') and not all_data.get('dub')):
            self.progress_queue.put({'error': 'Could not find video source.'})
            return False

        anime_name, ep_num, ep_title, season = self.get_episode_info(url)

        # Apply overrides
        final_name = name_override if name_override else anime_name
        final_season = season_override if season_override else season
        final_ep_num = ep_num_override if ep_num_override else ep_num

        # Audio status
        audio = "JP"
        if all_data.get('sub') and all_data.get('dub'):
             audio = "Dual Audio"
        elif all_data.get('dub'):
             audio = "EN"

        qual_str = quality if quality in ["360", "720", "1080"] else "auto"

        # Sanitize filename to prevent WinError 123
        def sanitize(name):
            return re.sub(r'[\\/*?:"<>|]', "", name)

        # Fetch the FORMAT from config and inject parameters
        try:
            from config import FORMAT
        except ImportError:
            FORMAT = "[S{season}-E{episode}] {title} [{quality}] [{audio}]"

        base_filename_str = FORMAT.format(
            season=final_season,
            episode=final_ep_num,
            title=final_name,
            quality=f"{qual_str}p",
            audio=audio
        )

        # Sanitize filename to prevent WinError 123
        base_filename = sanitize(base_filename_str)

        # Isolation: Use a task-specific subdirectory to prevent file collisions
        task_dir = self.download_path / f"{ep_id}_{qual_str}"
        task_dir.mkdir(exist_ok=True)

        # Paths
        video_temp = task_dir / f"{base_filename}_sub.mkv"
        audio_temp = task_dir / f"{base_filename}_dub.mkv"
        final_file = self.download_path / f"{base_filename}.mkv"

        # Download SUB (Japanese audio with subs)
        data = all_data.get('sub') or all_data.get('dub')
        m3u8_url = data['sources'][0]['file']

        self.progress_queue.put({'status': f"📥 **Downloading: {final_name} [{qual_str}p]**\nPlease wait..."})

        def run_n_m3u8dl(url, save_name, dl_type='sub', quality="auto"):
            cmd = [
                str(self.binary_path),
                url,
                "--save-dir", str(task_dir),
                "--save-name", save_name,
                "-H", "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "-H", "Referer: https://megacloud.tv/",
                "--check-segments-count", "False",
                "-mt",
                "--thread-count", "50",
                "--download-retry-count", "5"
            ]

            if self.proxy:
                cmd.extend(["--custom-proxy", self.proxy])

            # Select specific video quality, fallback to auto-select if not found
            if quality == "1080":
                cmd.extend(["-sv", "res='1080':for=best"])
            elif quality == "720":
                cmd.extend(["-sv", "res='720':for=best"])
            elif quality == "360":
                cmd.extend(["-sv", "res='360':for=best"])
            else:
                cmd.extend(["--auto-select"])

            try:
                print(f"[{dl_type.upper()}] Running: {' '.join(cmd[:3])}... (binary: {cmd[0]})", flush=True)

                # Verify binary exists and is executable
                if not _os.path.isfile(cmd[0]):
                    print(f"Binary not found at: {cmd[0]}", flush=True)
                    return False

                # Use binary mode and bufsize=0 to get real-time unbuffered output
                process = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    bufsize=0
                )

                # Collect last few lines for error reporting
                last_lines = []

                # Read output char by char to handle carriage returns (\r)
                buffer = b""
                while True:
                    char = process.stdout.read(1)
                    if not char:
                        break
                    if char in (b'\r', b'\n'):
                        try:
                            line = buffer.decode('utf-8', errors='replace').strip()
                        except:
                            line = ""

                        if line:
                            # Strip ANSI escape codes
                            line = re.sub(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])', '', line)

                            # Keep last 5 lines for error diagnosis
                            last_lines.append(line)
                            if len(last_lines) > 5:
                                last_lines.pop(0)

                            # Print to console logs for debugging
                            if "%" in line:
                                print(f"[{dl_type.upper()}] {line}", flush=True)
                            else:
                                # Print non-progress lines too (errors, info)
                                print(f"[{dl_type.upper()}] {line}", flush=True)

                            if "%" in line:
                                percent_match = re.search(r"(\d+(\.\d+)?)%", line)

                                # Search for speed specifically after the percentage indicator,
                                # as the actual download speed usually appears after progress
                                parts = re.split(r"\d+(\.\d+)?%", line)
                                speed_match = None
                                if len(parts) > 1:
                                    after_percent = parts[-1]
                                    speed_match = re.search(r"(\d+(\.\d+)?\s*[MKG]?i?(B/s|bps|b/s|bit/s))", after_percent, re.I)
                                    if not speed_match:
                                        speed_match = re.search(r"(\d+(\.\d+)?\s*\S+/(s|sec))", after_percent, re.I)

                                # Fallback to general search if the specific one fails
                                if not speed_match:
                                    speed_match = re.search(r"(\d+(\.\d+)?\s*[MKG]?i?(B/s|bps|b/s|bit/s))", line, re.I)
                                    if not speed_match:
                                        speed_match = re.search(r"(\d+(\.\d+)?\s*\S+/(s|sec))", line, re.I)

                                size_match = re.search(r"(\d+(\.\d+)?\s*\S+)\s*/\s*(\d+(\.\d+)?\s*\S+)", line, re.I)

                                if percent_match:
                                    # Extract values with fallbacks
                                    pct_val = percent_match.group(1)
                                    speed_val = speed_match.group(1) if speed_match else "0 MB/s"

                                    progress_data = {
                                        'percent': f"{pct_val}%",
                                        'speed': speed_val,
                                        'downloaded': size_match.group(1) if size_match else "0 MB",
                                        'total': size_match.group(3) if size_match else "0 MB",
                                        'type': dl_type,
                                        'title': ep_title
                                    }
                                    self.progress_queue.put(progress_data)
                        buffer = b""
                    else:
                        buffer += char

                process.wait()
                exit_code = process.returncode
                if exit_code != 0:
                    error_detail = '\n'.join(last_lines) if last_lines else 'No output captured'
                    print(f"[{dl_type.upper()}] N_m3u8DL-RE exited with code {exit_code}:\n{error_detail}", flush=True)
                return exit_code == 0
            except FileNotFoundError:
                print(f"N_m3u8DL-RE binary not found or not executable: {cmd[0]}", flush=True)
                return False
            except PermissionError:
                print(f"N_m3u8DL-RE binary not executable (permission denied): {cmd[0]}", flush=True)
                return False
            except Exception as e:
                print(f"Error running N_m3u8DL-RE: {e}", flush=True)
                return False

        # Dub settings
        dub_downloaded = [False]
        dub_thread = None
        if all_data.get('sub') and all_data.get('dub'):
             dub_url = all_data['dub']['sources'][0]['file']

             def download_dub():
                 save_name = f"{base_filename}_dub"
                 if run_n_m3u8dl(dub_url, save_name, dl_type='dub', quality=quality):
                     # Usually saves as .mp4 or .m4a or .mkv, let's find it and rename it to audio_temp (.mkv)
                     # Actually N_m3u8DL-RE outputs to {save_name}.mp4 by default if it's video+audio, or .m4a if audio only.
                     for ext in ['.mp4', '.m4a', '.mkv', '.ts']:
                         p = task_dir / f"{save_name}{ext}"
                         if p.exists():
                             p.rename(audio_temp)
                             dub_downloaded[0] = True
                             break
                 else:
                     self.progress_queue.put({'status': f"⚠️ **Dub download failed**\nProceeding with Japanese only."})

             dub_thread = Thread(target=download_dub)
             dub_thread.start()

        # Start Sub download in main thread
        save_name_sub = f"{base_filename}_sub"
        if run_n_m3u8dl(m3u8_url, save_name_sub, dl_type='sub', quality=quality):
            for ext in ['.mp4', '.mkv', '.ts']:
                p = task_dir / f"{save_name_sub}{ext}"
                if p.exists():
                    p.rename(video_temp)
                    break
        else:
            self.progress_queue.put({'error': "Video download failed"})
            if dub_thread: dub_thread.join()
            return False

        if dub_thread:
            dub_thread.join()

        dub_downloaded = dub_downloaded[0]

        # 2. Download subtitles manually
        sub_files = []
        if data.get('tracks'):
            subs = [t for t in data['tracks'] if t.get('kind') == 'captions']
            for i, s in enumerate(subs):
                lang = s.get('label', f'sub_{i}').lower().replace(' ', '_')
                sub_path = task_dir / f"{base_filename}_{lang}.vtt"
                try:
                    r = requests.get(s['file'], timeout=10)
                    if r.status_code == 200:
                        with open(sub_path, 'wb') as f:
                            f.write(r.content)
                        sub_files.append((sub_path, lang))
                except: pass

        # 3. Merge with ffmpeg
        ffmpeg_exe = 'ffmpeg'

        # Ensure video_temp exists, if not, find the file with any extension
        if not video_temp.exists():
             for f in task_dir.iterdir():
                  if f.name.startswith(f"{base_filename}_sub."):
                       f.replace(video_temp)
                       break

        # Check if we have anything to merge
        if not shutil.which(ffmpeg_exe) or (not sub_files and not dub_downloaded):
            if video_temp.exists():
                 video_temp.replace(final_file)

            # Cleanup task dir
            try: shutil.rmtree(task_dir)
            except: pass

            self.progress_queue.put({'finished': True, 'filename': str(final_file), 'title': base_filename})
            return True

        self.progress_queue.put({'status': f"🎬 **Merging Tracks for: {ep_title}**\nPlease wait..."})

        cmd = [ffmpeg_exe, '-y']

        # Add inputs only if they exist
        if video_temp.exists():
             cmd.extend(['-i', str(video_temp)])
        else:
             # If no video exists, we cannot merge anything
             self.progress_queue.put({'error': 'Video file disappeared before merge.'})
             return False

        if dub_downloaded and audio_temp.exists():
             cmd.extend(['-i', str(audio_temp)])
        else:
             dub_downloaded = False

        valid_subs = []
        for sub_path, lang in sub_files:
            if sub_path.exists():
                 cmd.extend(['-i', str(sub_path)])
                 valid_subs.append((sub_path, lang))

        sub_files = valid_subs

        # Mapping tracks
        cmd.extend(['-map', '0:v']) # First input video
        cmd.extend(['-map', '0:a']) # First input audio (Japanese)
        if dub_downloaded:
             cmd.extend(['-map', '1:a:0']) # Second input's first audio track (English)

        sub_offset = 2 if dub_downloaded else 1
        for i in range(len(sub_files)):
            cmd.extend(['-map', f'{i + sub_offset}:s'])

        # Metadata & Codecs
        cmd.extend(['-c', 'copy', '-c:s', 'srt'])
        cmd.extend(['-metadata:s:a:0', 'language=jpn', '-metadata:s:a:0', 'title=Japanese'])
        if dub_downloaded:
             cmd.extend(['-metadata:s:a:1', 'language=eng', '-metadata:s:a:1', 'title=English'])

        # Set the first subtitle track (usually English) as default to auto-select
        if sub_files:
             cmd.extend(['-disposition:s:0', 'default'])

        cmd.append(str(final_file))

        try:
            subprocess.run(cmd, check=True, capture_output=True)
            # Cleanup task dir
            try: shutil.rmtree(task_dir)
            except: pass

            self.progress_queue.put({'finished': True, 'filename': str(final_file), 'title': base_filename})
            return True
        except Exception as e:
            # Fallback to original video if merge fails
            if video_temp.exists():
                 video_temp.replace(final_file)
            elif not final_file.exists():
                 # Look for any sub file that might have been renamed or left over in task_dir
                 for f in task_dir.iterdir():
                      if f.name.startswith(f"{base_filename}_sub."):
                           f.replace(final_file)
                           break

            # Cleanup task dir
            try: shutil.rmtree(task_dir)
            except: pass

            self.progress_queue.put({'finished': True, 'filename': str(final_file), 'title': base_filename})
            return True

    def list_episodes(self, anime_url):
        anime_slug = None
        if "aniwatchtv.to" in anime_url:
             anime_slug = anime_url.split('/')[-1].split('?')[0]
             anime_id = anime_slug.split('-')[-1]
        else:
             anime_name, ep_num = self.get_episode_info(anime_url)
             search_url = f"{self.base_url}/search?keyword={anime_name.replace(' ', '+')}"
             try:
                 resp = self.session.get(search_url, headers=self.headers, impersonate="chrome")
                 m = re.search(rf'href="/([^"]+)"[^>]*title="{re.escape(anime_name)}"', resp.text, re.I)
                 if not m: m = re.search(r'href="/([^"/]+)-(\d+)"', resp.text)
                 if m:
                     anime_slug = f"{m.group(1)}-{m.group(2)}" if len(m.groups()) > 1 else m.group(1)
                     anime_id = m.group(2) if len(m.groups()) > 1 else m.group(1).split('-')[-1].split('?')[0]
                 else: return []
             except: return []

        ep_list_url = f"{self.base_url}/ajax/v2/episode/list/{anime_id}"
        try:
            resp_eps = self.session.get(ep_list_url, headers={"X-Requested-With": "XMLHttpRequest", **self.headers}, impersonate="chrome")
            if resp_eps.status_code == 200:
                html = resp_eps.json().get('html', '')
                # Using a more flexible regex to find title, data-number, and data-id regardless of order
                results = []
                for match in re.finditer(r'<a\s+[^>]*class="[^"]*ep-item[^"]*"[^>]*>', html):
                    tag = match.group(0)
                    title_m = re.search(r'title="([^"]+)"', tag)
                    num_m = re.search(r'data-number="(\d+)"', tag)
                    id_m = re.search(r'data-id="(\d+)"', tag)

                    if title_m and num_m and id_m:
                        results.append({
                            'title': title_m.group(1),
                            'url': f"{self.base_url}/watch/{anime_slug}?ep={id_m.group(1)}",
                            'ep_number': num_m.group(1),
                            'ep_id': id_m.group(1)
                        })
                return results
        except Exception as e:
            print(f"Error fetching episodes: {e}")
        return []

    def download_all_episodes(self, anime_url, quality="auto"):
        eps = self.list_episodes(anime_url)
        for ep in eps:
            self.download_episode(ep['url'], quality=quality)
        return True

    def download_range(self, anime_url, start, end, quality="auto"):
        eps = self.list_episodes(anime_url)
        for ep in eps:
            try:
                num = int(re.search(r'Episode (\d+)', ep['title']).group(1))
                if start <= num <= end:
                    self.download_episode(ep['url'], quality=quality)
            except: pass
        return True
