import os
import time
import requests
import difflib
import mutagen
from mutagen.id3 import ID3, USLT, ID3NoHeaderError
from mutagen.flac import FLAC
from qobuz_dl.color import OFF, GREEN, RED, YELLOW, CYAN
from qobuz_dl.download_queue import in_background

try:
    import lyricsgenius
except ImportError:
    lyricsgenius = None

class LyricsEngine:
    """
    Roon-Ready Synchronized Lyrics Engine with Fuzzy Matching Security.
    Supports dynamic routing based on the user's preferred lyrics_sources.
    """

    def __init__(self, genius_token=None):
        self.genius_token = genius_token
        self.genius = None
        if self.genius_token and lyricsgenius:
            self.genius = lyricsgenius.Genius(self.genius_token, remove_section_headers=True)
            self.genius.verbose = False

    def _verify_match(self, req_artist, req_track, res_artist, res_track, is_parallel=False, print_func=print):
        """
        Fuzzy Matching Algorithm to prevent injecting wrong lyrics.
        Context-Aware: auto-skips borderline matches during parallel execution.
        """
        if not res_artist or not res_track:
            return False

        req_a_clean, req_t_clean = req_artist.lower().strip(), req_track.lower().strip()
        res_a_clean, res_t_clean = res_artist.lower().strip(), res_track.lower().strip()

        artist_ratio = difflib.SequenceMatcher(None, req_a_clean, res_a_clean).ratio()
        track_ratio = difflib.SequenceMatcher(None, req_t_clean, res_t_clean).ratio()
        
        match_percentage = ((artist_ratio * 0.4) + (track_ratio * 0.6)) * 100

        if match_percentage >= 75.0:
            return True
        elif 60.0 <= match_percentage < 75.0:
            if is_parallel or in_background():
                mode = "parallel mode" if is_parallel else "the background"
                print_func(f"    ⚠️ [LYRICS RADAR] Borderline match ({match_percentage:.1f}%). Auto-skipping in {mode}.")
                return False
            else:
                print_func(f"\n{YELLOW}    ⚠️ [LYRICS RADAR] Borderline match detected ({match_percentage:.1f}%){OFF}")
                print_func(f"       Requested : {req_artist} - {req_track}")
                print_func(f"       Found     : {res_artist} - {res_track}")
                
                while True:
                    try:
                        choice = input(f"       {CYAN}Accept these lyrics? [y/N]: {OFF}").lower().strip()
                    except EOFError:  # nothing to read an answer from: the default, no
                        return False
                    if choice in ['y', 'yes']:
                        return True
                    elif choice in ['n', 'no', '']:
                        return False
        else:
            print_func(f"    ❌ Lyrics skipped: Too different ({res_artist} - {res_track} | {match_percentage:.1f}%)")
            return False

    def fetch_and_inject(self, file_path, artist, track, album, save_lrc=True, embed_lyrics=True, is_parallel=False, track_id=None, qobuz_client=None, print_func=print, synced_only=False, lyrics_sources=None):
        """
        Orchestrates the retrieval and injection of synchronized lyrics.
        Iterates over the defined lyrics_sources array and stops at the first valid match.
        """
        if not save_lrc and not embed_lyrics:
            return
            
        if lyrics_sources is None:
            lyrics_sources = ['qobuz', 'lrclib', 'genius']
            
        try:
            print_func(f"    🔍 Searching lyrics for: {track}...")
            
            # Extract track duration dynamically for accurate LRCLIB matching
            duration = None
            if file_path and os.path.exists(file_path):
                try:
                    audio_file = mutagen.File(file_path)
                    if audio_file and audio_file.info:
                        duration = int(audio_file.info.length)
                except Exception:
                    pass

            def _request_lrclib(url, params):
                """Helper to manage HTTP 503 intermittent errors with automatic retry."""
                headers = {"User-Agent": "qobuz-dl-ultimate/2.6.5 (https://github.com/Sei969/qobuz-dl)"}
                for attempt in range(2):
                    try:
                        resp = requests.get(url, params=params, headers=headers, timeout=12)
                        if resp.status_code == 503:
                            time.sleep(2)
                            continue
                        return resp
                    except requests.exceptions.RequestException:
                        time.sleep(2)
                return None

            for index, source in enumerate(lyrics_sources):
                
                # --- QOBUZ NATIVE PROVIDER ---
                if source == 'qobuz' and qobuz_client and track_id:
                    qobuz_lyrics, is_synced = self._fetch_qobuz_native(qobuz_client, track_id)
                    if qobuz_lyrics:
                        if synced_only and not is_synced:
                            print_func(f"    ⏩ Qobuz native lyrics are unsynced. Skipping to next provider...")
                        else:
                            if embed_lyrics: self._inject_metadata(file_path, qobuz_lyrics)
                            if save_lrc: self._save_lrc_file(file_path, qobuz_lyrics)
                            
                            if is_synced:
                                print_func(f"    ✅ Official Qobuz lyrics injected (Native 1:1 sync)!")
                            else:
                                print_func(f"    ✅ Official Qobuz lyrics injected (Standard text)!")
                            return
                    
                    if index < len(lyrics_sources) - 1:
                        print_func(f"    ⏩ No valid results from QOBUZ. Falling back to {lyrics_sources[index + 1].upper()}...")

                # --- LRCLIB PROVIDER ---
                elif source == 'lrclib':
                    lrclib_get_url = "https://lrclib.net/api/get"
                    lrclib_search_url = "https://lrclib.net/api/search"
                    
                    params = {"artist_name": artist, "track_name": track, "album_name": album}
                    if duration: params["duration"] = duration

                    response = _request_lrclib(lrclib_get_url, params)
                    
                    if not response or response.status_code != 200:
                        fallback_params = {"artist_name": artist, "track_name": track}
                        if duration: fallback_params["duration"] = duration
                        response = _request_lrclib(lrclib_get_url, fallback_params)

                    if response and response.status_code == 200:
                        data = response.json()
                        api_artist = data.get("artistName", "")
                        api_track = data.get("trackName", "")
                        
                        if self._verify_match(artist, track, api_artist, api_track, is_parallel, print_func):
                            synced_lyrics = data.get("syncedLyrics")
                            plain_lyrics = data.get("plainLyrics")
                            
                            if synced_lyrics:
                                if embed_lyrics: self._inject_metadata(file_path, synced_lyrics)
                                if save_lrc: self._save_lrc_file(file_path, synced_lyrics)
                                print_func(f"    ✅ Synchronized lyrics injected (LRCLIB)!")
                                return
                            elif plain_lyrics and not synced_only:
                                if embed_lyrics: self._inject_metadata(file_path, plain_lyrics)
                                if save_lrc: self._save_lrc_file(file_path, plain_lyrics)
                                print_func(f"    ✅ Standard lyrics injected (LRCLIB)!")
                                return
                            elif plain_lyrics and synced_only:
                                print_func(f"    ⏩ Skipped plain lyrics (LRCLIB /api/get). Initiating deep search...")
                    
                    if synced_only:
                        # Use explicit fields instead of general 'q' parameter for better accuracy
                        search_params = {"artist_name": artist, "track_name": track}
                        search_resp = _request_lrclib(lrclib_search_url, search_params)
                        
                        if search_resp and search_resp.status_code == 200:
                            results = search_resp.json()
                            if isinstance(results, list):
                                valid_candidates = []
                                
                                for res in results:
                                    api_artist = res.get("artistName", "")
                                    api_track = res.get("trackName", "")
                                    
                                    # Use lambda to suppress output for multiple candidate evaluations
                                    if self._verify_match(artist, track, api_artist, api_track, is_parallel=True, print_func=lambda *a, **k: None):
                                        synced_lyrics = res.get("syncedLyrics")
                                        if synced_lyrics:
                                            res_duration = res.get("duration")
                                            
                                            # Tighter duration tolerance (3 seconds instead of 6)
                                            if duration and res_duration:
                                                diff = abs(duration - res_duration)
                                                if diff <= 3:
                                                    valid_candidates.append((diff, synced_lyrics))
                                            elif not duration:
                                                # If we have no local duration, accept the first valid fuzzy match
                                                valid_candidates.append((0, synced_lyrics))
                                
                                if valid_candidates:
                                    # Pick the closest candidate based on duration difference
                                    valid_candidates.sort(key=lambda x: x[0])
                                    best_lyrics = valid_candidates[0][1]
                                    
                                    if embed_lyrics: self._inject_metadata(file_path, best_lyrics)
                                    if save_lrc: self._save_lrc_file(file_path, best_lyrics)
                                    print_func(f"    ✅ Synchronized lyrics injected (LRCLIB Deep Search)!")
                                    return

                    if index < len(lyrics_sources) - 1:
                        print_func(f"    ⏩ No valid results from LRCLIB. Falling back to {lyrics_sources[index + 1].upper()}...")

                # --- GENIUS PROVIDER ---
                elif source == 'genius':
                    if self.genius and not synced_only:
                        song = self.genius.search_song(track, artist)
                        if song and song.lyrics:
                            if self._verify_match(artist, track, song.artist, song.title, is_parallel, print_func):
                                if embed_lyrics: self._inject_metadata(file_path, song.lyrics)
                                if save_lrc: self._save_lrc_file(file_path, song.lyrics)
                                print_func(f"    ✅ Standard lyrics injected via Genius and saved!")
                                return
                                
                    if index < len(lyrics_sources) - 1:
                        print_func(f"    ⏩ No valid results from GENIUS. Falling back to {lyrics_sources[index + 1].upper()}...")

            print_func(f"    ❌ No valid lyrics found for this track.")

        except Exception as e:
            print_func(f"    ⚠️ Error during lyrics search: {e}")

    def _save_lrc_file(self, audio_file_path, lyrics):
        base_name = os.path.splitext(audio_file_path)[0]
        lrc_path = f"{base_name}.lrc"
        with open(lrc_path, 'w', encoding='utf-8') as f:
            f.write(lyrics)

    def _inject_metadata(self, file_path, lyrics):
        if not lyrics: return
        ext = os.path.splitext(file_path)[1].lower()
        try:
            if ext == '.flac':
                audio = FLAC(file_path)
                audio['LYRICS'] = lyrics
                audio.save()
            elif ext == '.mp3':
                try:
                    audio = ID3(file_path)
                except ID3NoHeaderError:
                    audio = ID3()
                audio.add(USLT(encoding=3, lang='eng', desc='', text=lyrics))
                audio.save(file_path)
        except Exception:
            pass

    def _fetch_qobuz_native(self, qobuz_client, track_id):
        try:
            resp = qobuz_client.api_call("track/lyricsUrl", track_id=track_id)
            if not resp or "lyrics_url" not in resp:
                return None, False
                
            lyrics_url = resp.get("lyrics_url")
            if not lyrics_url:
                return None, False
                
            cdn_resp = requests.get(lyrics_url, timeout=10)
            if cdn_resp.status_code != 200:
                return None, False
                
            doc = cdn_resp.json()
            return self._parse_qobuz_to_lrc(doc)
        except Exception:
            return None, False

    def _parse_qobuz_to_lrc(self, doc):
        original = doc.get("original", {})
        lines = original.get("lines", [])
        
        if not lines:
            return None, False
            
        lrc_lines = []
        is_synced = False
        
        for entry in lines:
            text = entry.get("line", "").strip()
            start_ms = entry.get("start")
            
            if start_ms is not None:
                is_synced = True
                total_seconds = start_ms / 1000.0
                minutes = int(total_seconds // 60)
                seconds = total_seconds % 60
                timestamp = f"[{minutes:02d}:{seconds:05.2f}]"
                lrc_lines.append(f"{timestamp} {text}")
            else:
                lrc_lines.append(text)
                
        return "\n".join(lrc_lines).strip(), is_synced