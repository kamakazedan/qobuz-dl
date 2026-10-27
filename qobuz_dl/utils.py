import re
import string
import locale
import os
import logging
import subprocess
import time
from qobuz_dl.color import GREEN, RED, YELLOW, CYAN, OFF
import unicodedata

from mutagen.mp3 import EasyMP3
from mutagen.flac import FLAC

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

EXTENSIONS = (".mp3", ".flac")


class PartialFormatter(string.Formatter):
    """
    A custom string formatter that safely handles missing variables during string evaluation.
    Prevents KeyErrors when a requested metadata tag is missing from the API response.
    """
    def __init__(self, missing="n/a", bad_fmt="n/a"):
        self.missing, self.bad_fmt = missing, bad_fmt

    def get_field(self, field_name, args, kwargs):
        try:
            val = super(PartialFormatter, self).get_field(field_name, args, kwargs)
        except (KeyError, AttributeError):
            val = None, field_name
        return val

    def format_field(self, value, spec):
        if not value:
            return self.missing
        try:
            return super(PartialFormatter, self).format_field(value, spec)
        except ValueError:
            if self.bad_fmt:
                return self.bad_fmt
            raise

def read_config_file(config, path):
    """
    Reads config.ini into a ConfigParser as UTF-8, like every other text file
    written by qobuz-dl.

    A UTF-8 BOM (added by some editors, e.g. older Windows Notepad) is skipped.
    Files saved by older versions in the system encoding (e.g. cp1252 on Windows)
    are still read by falling back to that encoding.

    Args:
        config (configparser.ConfigParser): The parser to fill.
        path (str): Path to config.ini.

    Returns:
        list: The files that were successfully read (as ConfigParser.read).
    """
    try:
        return config.read(path, encoding="utf-8-sig")
    except UnicodeDecodeError:
        for section in config.sections():
            config.remove_section(section)
        config[config.default_section].clear()
        return config.read(path, encoding=locale.getpreferredencoding(False))


def make_m3u(pl_directory, remote_items=None):
    """
    Generates a UTF-8 encoded .m3u8 playlist file.
    
    If remote_items (Qobuz API playlist order) is provided, it utilizes a robust O(1) 
    4-pass matching algorithm (ID -> ISRC -> Title -> Filename) to preserve the exact 
    online track order, completely ignoring physical local filenames.

    Args:
        pl_directory (str): The local directory containing the downloaded audio files.
        remote_items (list, optional): The list of track dictionaries from the Qobuz API. Defaults to None.
    """
    import os
    import re
    import logging
    from mutagen.id3 import ID3
    from mutagen.flac import FLAC
    from mutagen import File
    
    logger = logging.getLogger(__name__)
    EXTENSIONS = (".mp3", ".flac")

    track_list = ["#EXTM3U"]
    rel_folder = os.path.basename(os.path.normpath(pl_directory))
    pl_name = rel_folder + ".m3u8"
    pl_full_path = os.path.join(pl_directory, pl_name)

    # 1. Scan the local folder and extract deep tags
    local_files_info = []
    for local, dirs, files in os.walk(pl_directory):
        dirs.sort()
        for f in files:
            if os.path.splitext(f)[-1].lower() in EXTENSIONS:
                audio_full_path = os.path.abspath(os.path.join(local, f))
                info = {
                    'path': audio_full_path, 
                    'title': '', 
                    'artist': '', 
                    'isrc': '', 
                    'qobuz_id': '',
                    'duration': 0
                }
                try:
                    # Generic length via mutagen.File
                    audio_gen = File(audio_full_path)
                    if audio_gen and audio_gen.info:
                        info['duration'] = int(audio_gen.info.length)

                    # Deep Tag Parsing
                    if audio_full_path.lower().endswith('.flac'):
                        audio = FLAC(audio_full_path)
                        info['qobuz_id'] = audio.get("QOBUZTRACKID", [None])[0]
                        info['isrc'] = audio.get("ISRC", [None])[0]
                        info['title'] = audio.get("TITLE", [""])[0]
                        info['artist'] = audio.get("ARTIST", [""])[0]
                    else:
                        audio = ID3(audio_full_path)
                        # Correct way to find custom TXXX frames in ID3
                        for frame in audio.getall("TXXX"):
                            if frame.desc.upper() == "QOBUZTRACKID":
                                info['qobuz_id'] = frame.text[0]
                                break
                        isrc_frame = audio.get("TSRC")
                        info['isrc'] = isrc_frame.text[0] if isrc_frame else None
                        tit2 = audio.get("TIT2")
                        info['title'] = tit2.text[0] if tit2 else ""
                        tpe1 = audio.get("TPE1")
                        info['artist'] = tpe1.text[0] if tpe1 else ""
                except Exception as e:
                    logger.debug(f"Error reading tags for {f}: {e}")
                    info['title'] = os.path.splitext(f)[0] # Fallback title
                
                local_files_info.append(info)

    ordered_files = []

    # 2. Match with Qobuz API order (4-Pass Algorithm)
    if remote_items:
        # Pre-index the local files into dictionaries for O(1) single lookups
        by_tid = {str(f['qobuz_id']): f for f in local_files_info if f.get('qobuz_id')}
        by_isrc = {str(f['isrc']): f for f in local_files_info if f.get('isrc')}
        by_title = {str(f['title']).strip().lower(): f for f in local_files_info if f.get('title')}
        
        missing_count = 0
        table_header = (
            f"\n{RED}{'━'*80}\n"
            f"{YELLOW}{'MISSING LOCAL TRACKS':^80}\n"
            f"{RED}{'━'*80}{OFF}\n"
            f"{CYAN}{'TITLE':<35} │ {'ARTIST':<25} │ {'ID':<12}{OFF}\n"
            f"{'─'*80}"
        )
        
        for item in remote_items:
            tid = str(item.get("id", ""))
            isrc = str(item.get("isrc", ""))
            track_title = item.get("title", "Unknown Title")
            album_artist = item.get("album", {}).get("artist", {}).get("name")
            performer_name = item.get("performer", {}).get("name", "Unknown Artist")
            final_artist = performer_name if album_artist in [None, "Various Artists"] else album_artist
            
            # Pass 1-3: Fast dictionary lookups
            best_match = by_tid.get(tid) or by_isrc.get(isrc) or by_title.get(track_title.strip().lower())
            
            # Pass 4: Fallback to filename substring match
            if not best_match and track_title != "Unknown Title":
                for f_info in local_files_info:
                    if track_title.lower() in os.path.basename(f_info['path']).lower():
                        best_match = f_info
                        break
            
            if best_match:
                ordered_files.append(best_match)
                # La riga available_files.remove(best_match) è stata rimossa
                # per permettere tracce duplicate all'interno della stessa playlist.
            else:
                if missing_count == 0:
                    logger.warning(table_header)
                row = f"{track_title[:35]:<35} │ {final_artist[:25]:<25} │ {tid:<12}"
                logger.warning(f"{YELLOW}{row}{OFF}")
                missing_count += 1
                
        if missing_count > 0:
            logger.warning(f"{RED}{'━'*80}{OFF}\n")

    # 3. Fallback (Albums or failed matching): Natural sort
    if not remote_items or len(ordered_files) == 0:
        def natural_sort_key(s):
            return [int(text) if text.isdigit() else text.lower() for text in re.split(r'(\d+)', s)]
        
        ordered_files = sorted(local_files_info, key=lambda x: natural_sort_key(os.path.basename(x['path'])))

    # 4. Generate M3U
    for f_info in ordered_files:
        audio_rel_path = os.path.relpath(f_info['path'], pl_directory)
        
        disp_title = f_info['title'] or "Unknown Title"
        disp_artist = f_info['artist'] or "Unknown Artist"
        length = f_info['duration']

        index = f"#EXTINF:{length}, {disp_artist} - {disp_title}\n{audio_rel_path}"
        track_list.append(index)

    if len(track_list) > 1:
        with open(pl_full_path, "w", encoding="utf-8") as pl:
            pl.write("\n".join(track_list))


def label_matches(label, filter_label):
    """
    Checks whether a Qobuz label matches the --filter-label value.

    A numeric filter must equal the label ID; any filter also matches when it is
    contained in the label name (case-insensitive). A missing label never matches.

    Args:
        label (dict | None): The "label" object from the Qobuz API.
        filter_label (str): The label name or numeric label ID given by the user.

    Returns:
        bool: True if the label matches the filter.
    """
    label = label or {}
    name = str(label.get("name") or "").strip()
    label_id = str(label.get("id") or "").strip()
    if filter_label.isdigit() and filter_label == label_id:
        return True
    return bool(name) and filter_label.lower() in name.lower()


def is_main_artist(release, artist_id, artist_name):
    """
    Checks whether a release from an artist's page is credited to that artist,
    rather than being someone else's release they appear on.

    Compares Qobuz artist IDs, falling back to the name (case-insensitive) when
    the release carries no artist ID.

    Args:
        release (dict): An album item from the artist's discography listing.
        artist_id (str | int): The requested artist's Qobuz ID.
        artist_name (str): The requested artist's name.

    Returns:
        bool: True if the release's main artist is the requested artist.
    """
    artist = release.get("artist") or {}
    if artist.get("id") is not None and artist_id:
        return str(artist["id"]) == str(artist_id)
    return str(artist.get("name") or "").casefold() == str(artist_name).casefold()


# --- Smart discography --------------------------------------------------------
# Titles are matched on whole words, so "Demon Days" isn't taken for a demo and
# "Alive" isn't taken for a live recording.
_DEMO_RE = re.compile(r"\bdemos?\b", re.I)
_LIVE_RE = re.compile(r"\blive\b", re.I)
# Brackets around notes in titles: mostly "(Live)" and "[Remastered]", but also
# "< LIVE >", "{Live}", "【LIVE】" and full-width ones such as "（Live）"
_OPENING, _CLOSING = "([{<（［｛＜【〈《〔", ")]}>）］｝＞】〉》〕"
_OPEN, _CLOSE = f"[{re.escape(_OPENING)}]", f"[{re.escape(_CLOSING)}]"
_BRACKETED = f"{_OPEN}[^{re.escape(_CLOSING)}]*{_CLOSE}"
# "Live" only marks a live recording in the version field, in a bracketed or
# dashed suffix ("(Live at Wembley)", "Timebomb < LIVE >", "Song - Live"), as
# "Unplugged" or "In Concert", or at the start of an album-length title ("Live
# at Leeds"). Titles such as "Live Through This", "Live Forever" and "Long Live"
# are left alone, and so is "I <3 Live Music": brackets other than ( and [ only
# count once they're closed.
_LIVE_SUFFIX_RE = re.compile(
    r"[(\[][^)\]]*\blive\b"
    rf"|{_OPEN}[^{re.escape(_CLOSING)}]*\blive\b[^{re.escape(_CLOSING)}]*{_CLOSE}"
    r"|\s[-–]\s.*\blive\b",
    re.I,
)
_CONCERT_RE = re.compile(r"\b(unplugged|in concert)\b", re.I)
_LIVE_TITLE_RE = re.compile(r"^\s*live\s+(at|in|from)\b", re.I)
_REMASTER_RE = re.compile(r"\bremaster(ed|s)?\b", re.I)
_YEAR_RE = re.compile(r"\b(?:19[5-9]\d|20\d\d)\b")
# Notes after the title: bracketed ("Album (2011 Remaster)") or after a spaced
# dash ("Album - 2011 Remaster"). _DASH_NOTE_RE takes only the last dashed part.
_NOTE_RE = re.compile(rf"{_BRACKETED}|\s[-–—]\s.*$")
_DASH_NOTE_RE = re.compile(r"\s[-–—]\s(?:(?!\s[-–—]\s).)*$")
# Notes that only describe the edition; versions that differ only by these are
# grouped together ("Album (Deluxe Edition)", "Album - Remastered" and "Album").
_EDITION_RE = re.compile(
    r"\b(deluxe|edition|remaster(ed|s)?|expanded|anniversary|explicit|clean"
    r"|bonus|special|super|collector'?s|reissue|mono|stereo|complete|extended"
    r"|version)\b|^\W*\d{4}\W*$",
    re.I,
)
# Words that make a release a different recording rather than another edition,
# so "Song (Acoustic)" isn't grouped with "Song".
_VARIANT_RE = re.compile(
    r"\b(remix(es|ed)?|acoustic|instrumentals?|karaoke|a ?cappella|orchestral"
    r"|reimagined)\b",
    re.I,
)
# Unfinished or archival recordings, as bonus tracks are often labelled:
# "(Roger's Demo)", "(Alternate Version)", "(Take 1)", "(Rough Mix)", ...
_OUTTAKE_RE = re.compile(
    r"\b(demos?|outtakes?|alternate (take|version|mix)|alt\.? (take|version)"
    r"|take \d+|rough mix|early (take|version|mix)|rehearsal|work in progress)\b",
    re.I,
)
# "Wish You Were Here 50th Anniversary Edition", without brackets or a dash
_ANNIVERSARY_PHRASE_RE = re.compile(r"\s+\d{1,3}(st|nd|rd|th)\s+anniversary\b.*$", re.I)
# "Wish You Were Here 50": a round number of years after the title
_ANNIVERSARY_NUMBER_RE = re.compile(r"^(?P<base>.*\S)\s+(?P<years>\d{2,3})$")
# A deluxe edition loses to the standard one when more than this share of its
# extra tracks are live recordings, demos or other outtakes.
DELUXE_MAX_UNWANTED_SHARE = 0.5
# A disc counts as live (or demos) when at least this share of its tracks are
# live recordings or demos. Smart discography doesn't download such discs, and
# skips an album outright when every disc is like that.
LIVE_DISC_SHARE = 0.8
# The best (bit depth, kHz) each quality setting downloads.
_QUALITY_CAPS = {5: (0, 0), 6: (16, 44.1), 7: (24, 96), 27: (24, 192)}


def _is_live(item: dict, album_length: bool = False) -> bool:
    """Whether a release (or a track) is a live recording."""
    title, version = item.get("title") or "", item.get("version") or ""
    return bool(
        str(item.get("release_type") or "").lower() == "live"
        or _LIVE_RE.search(version)
        or _LIVE_SUFFIX_RE.search(title)
        or _CONCERT_RE.search(f"{title} {version}")
        or (album_length and _LIVE_TITLE_RE.search(title))
    )


def _is_demo(item: dict) -> bool:
    text = f"{item.get('title') or ''} {item.get('version') or ''}"
    return bool(_DEMO_RE.search(text))


def _is_unwanted_bonus(track: dict) -> bool:
    """A live recording, demo or other outtake: the bonus material that pads
    out many deluxe and anniversary editions."""
    text = f"{track.get('title') or ''} {track.get('version') or ''}"
    return _is_live(track) or bool(_OUTTAKE_RE.search(text))


def _live_discs(tracks: list) -> set:
    """The discs (media numbers) that are mostly live recordings or demos."""
    discs = {}
    for track in tracks:
        discs.setdefault(track.get("media_number") or 1, []).append(track)
    return {
        disc
        for disc, items in discs.items()
        if sum(1 for t in items if _is_live(t) or _is_demo(t)) / len(items)
        >= LIVE_DISC_SHARE
    }


def _release_kind(album: dict) -> str:
    """'single' for singles and EPs, otherwise 'album'. Qobuz's own type is
    unreliable, so 1-3 track releases count as singles whatever they're tagged."""
    rtype = str(album.get("release_type") or album.get("product_type") or "").lower()
    tracks = album.get("tracks_count") or 0
    if rtype in ("single", "ep", "epmini") or 0 < tracks <= 3:
        return "single"
    return "album"


def _variant_word(word: str) -> str:
    """'Remixes'/'Remixed' -> 'remix', 'A Cappella' -> 'acappella'."""
    return re.sub(r"(es|ed|s)$", "", word.lower().replace(" ", ""))


def _note_variants(note: str):
    """For a note after the title: None if it's part of the title ("Part 2",
    "Volume 1"), otherwise the variant words it carries (often none)."""
    found = [_variant_word(m.group(0)) for m in _VARIANT_RE.finditer(note)]
    if found or _EDITION_RE.search(note.strip(" -–—" + _OPENING + _CLOSING)):
        return found
    return None


def _group_key(album: dict) -> tuple:
    """Versions of the same release share a key: the title without bracketed
    or dashed notes about the edition, plus any words that make it a different
    recording ("Acoustic", "Remixes"), whether Qobuz put them in the title or
    the version. Other notes stay part of the title ("Part 2", "- Volume 1")."""
    variants = set()

    def strip_note(match):
        found = _note_variants(match.group(0))
        if found is None:
            return match.group(0)
        variants.update(found)
        return ""

    title = (album.get("title") or "").replace("’", "'")
    base = re.sub(rf"\s*{_BRACKETED}", strip_note, title)
    # Dashed notes, from the end: "Album - Remastered - Deluxe Edition" -> "Album"
    while True:
        match = _DASH_NOTE_RE.search(base)
        found = _note_variants(match.group(0)) if match else None
        if found is None:
            break
        variants.update(found)
        base = base[: match.start()]
    base = _ANNIVERSARY_PHRASE_RE.sub("", base)
    version = album.get("version") or ""
    variants.update(_variant_word(m.group(0)) for m in _VARIANT_RE.finditer(version))
    base = re.sub(r"\s+", " ", base).strip().lower()
    return (_release_kind(album), base, tuple(sorted(variants)))


def _merge_anniversary_numbers(groups: dict):
    """Fold "Wish You Were Here 50" into "Wish You Were Here": a title that's
    another release's title plus a round number of years (10, 15, 20, ...) is
    an anniversary edition of it. Sequels like "Tubular Bells 2" don't qualify,
    and nothing is folded unless the plain title is in the discography too."""
    for key in list(groups):
        kind, base, variants = key
        match = _ANNIVERSARY_NUMBER_RE.match(base)
        if not match:
            continue
        years = int(match.group("years"))
        target = (kind, match.group("base"), variants)
        if years >= 10 and years % 5 == 0 and target in groups:
            groups[target].extend(groups.pop(key))


def _quality_cap(quality) -> tuple:
    try:
        return _QUALITY_CAPS[int(quality)]
    except (KeyError, TypeError, ValueError):
        return (float("inf"), float("inf"))


def _track_count(album: dict) -> int:
    return album.get("tracks_count") or 0


def _remaster_rank(album: dict) -> int:
    """0 for originals; remasters rank by year, newest highest. The year comes
    from the remaster note ("2011 Remaster", version "Remastered 2021"), not the
    album's own title ("1999"), and falls back to the release date."""
    title, version = album.get("title") or "", album.get("version") or ""
    if not _REMASTER_RE.search(f"{title} {version}"):
        return 0
    notes = [n for n in _NOTE_RE.findall(title) + [version] if _REMASTER_RE.search(n)]
    years = [int(year) for note in notes for year in _YEAR_RE.findall(note)]
    if not years:
        released = str(
            album.get("release_date_original") or album.get("release_date") or ""
        )[:4]
        years = [int(released)] if released.isdigit() else []
    return max(years, default=1)


def _describe(album: dict) -> str:
    version = f" ({album['version']})" if album.get("version") else ""
    explicit = " [E]" if album.get("parental_warning") else ""
    return (
        f"{album.get('title')}{version}{explicit} "
        f"{album.get('maximum_bit_depth')}/{album.get('maximum_sampling_rate')}"
    )


def _edition_score(edition: dict, standard: dict, kept) -> int:
    """2 for an edition with worthwhile extra tracks, 1 for the standard
    edition, 0 for an edition whose extra tracks are mostly live recordings,
    demos or outtakes. Only the tracks that would be downloaded count, so a
    live disc that's left out doesn't count against an edition."""
    if _track_count(edition) <= _track_count(standard):
        return 1
    tracks, standard_tracks = kept(edition), kept(standard)
    if tracks is None or standard_tracks is None:
        return 2  # can't check; give the bigger edition the benefit of the doubt
    extra = len(tracks) - len(standard_tracks)
    if extra <= 0:
        return 1
    unwanted = sum(map(_is_unwanted_bonus, tracks)) - sum(
        map(_is_unwanted_bonus, standard_tracks)
    )
    return 0 if unwanted / extra > DELUXE_MAX_UNWANTED_SHARE else 2


def _pick_version(versions: list, cap: tuple, details, kept) -> dict:
    """Choose one of several versions of the same release."""

    def explicit(album):
        if "parental_warning" not in album:  # not in the listing: look it up
            album = details(album) or album
        return bool(album.get("parental_warning"))

    def quality(album):  # what you'd actually get at the chosen quality
        return (
            min(album.get("maximum_bit_depth") or 0, cap[0]),
            min(album.get("maximum_sampling_rate") or 0, cap[1]),
        )

    # 1. explicit over clean, but only if the album itself is explicit, i.e. its
    # standard edition comes in an explicit version. An edition can also be
    # flagged explicit just for a bonus track (a live recording with a swear in
    # it), and that shouldn't beat a better-sounding original.
    fewest = min(_track_count(a) for a in versions)
    explicit_album = any(explicit(a) for a in versions if _track_count(a) == fewest)
    # 2. highest quality
    ranks = [(explicit(a) if explicit_album else False, quality(a)) for a in versions]
    top = [a for a, rank in zip(versions, ranks) if rank == max(ranks)]

    # 3. deluxe over standard unless its extras are mostly live/demos/outtakes,
    # then explicit over clean if that wasn't decided above,
    # 4. newest remaster over older ones and the original, then more tracks
    standard = min(top, key=_track_count)
    scores = {id(a): _edition_score(a, standard, kept) for a in top}
    best = max(
        top,
        key=lambda a: (scores[id(a)], explicit(a), _remaster_rank(a), _track_count(a)),
    )

    note = ""
    if any(score == 0 for score in scores.values()):
        note = "; skipped an edition padded with live tracks, demos or outtakes"
    logger.info(
        f"{OFF}Smart discography: {_describe(best)} "
        f"(best of {len(versions)} versions{note})"
    )
    return best


def smart_discography_filter(contents: list, quality=None, get_album=None) -> list:
    """
    Picks one version of each release in an artist's discography.

    Drops releases by other artists, live albums and demo collections (judged
    by their titles, and for albums also by their track lists), then groups the
    versions of each album or single (deluxe, anniversary, explicit, remastered,
    different qualities) and keeps the best one. Discs that are mostly live
    recordings or demos are left out of the download: the kept items carry
    them as "_skip_discs".

    The best version is chosen by:

        1. explicit over clean, when the album itself is explicit
        2. the highest quality you'll actually get at the chosen quality setting
        3. a deluxe/anniversary edition over the standard one, unless more than
           half of its extra tracks are live recordings, demos or outtakes, in
           which case the standard one wins
        4. the newest remaster over older ones and the original, then the
           version with more tracks

    Args:
        contents (list): Discography pages returned by the Qobuz API.
        quality (int, optional): The download quality setting (5, 6, 7 or 27).
        get_album (callable, optional): Returns an album's full metadata by ID;
            used to compare the track lists of different editions.

    Returns:
        list: The filtered items list.
    """
    cache = {}

    def details(album):
        """Full album metadata, fetched at most once per album."""
        if get_album is None:
            return None
        if album["id"] not in cache:
            try:
                cache[album["id"]] = get_album(album["id"])
            except Exception as e:
                logger.debug(f"Couldn't fetch album {album['id']}: {e}")
                cache[album["id"]] = None
        return cache[album["id"]]

    def disc_plan(album):
        """(the tracks that would be downloaded, the discs left out as live or
        demos), or (None, set()) when the track list isn't available."""
        tracks = ((details(album) or {}).get("tracks") or {}).get("items")
        if not tracks:
            return None, set()
        skip = _live_discs(tracks)
        return [t for t in tracks if (t.get("media_number") or 1) not in skip], skip

    def kept(album):
        return disc_plan(album)[0]

    def live_album(album):
        """Live or demos throughout, even if the title doesn't say so
        ("Pink Floyd at Pompeii - MCMLXXII"): every disc would be left out."""
        full = details(album)
        if full and str(full.get("release_type") or "").lower() == "live":
            return True
        return kept(album) == []

    artist_id, artist_name = contents[0].get("id"), contents[0]["name"]
    groups = {}
    # The discography comes in pages of 50 releases; use all of them, not just the first
    for album in (a for page in contents for a in page["albums"]["items"]):
        if not is_main_artist(album, artist_id, artist_name):
            continue
        is_album = _release_kind(album) == "album"
        if (
            _is_demo(album)
            or _is_live(album, is_album)
            or (is_album and live_album(album))
        ):
            logger.info(
                f"{OFF}Smart discography: skipping {_describe(album)} (live/demo)"
            )
            continue
        groups.setdefault(_group_key(album), []).append(album)
    _merge_anniversary_numbers(groups)

    cap = _quality_cap(quality)
    picked = []
    for versions in groups.values():
        best = versions[0]
        if len(versions) > 1:
            best = _pick_version(versions, cap, details, kept)
        skip = disc_plan(best)[1] if _release_kind(best) == "album" else set()
        if skip:
            best = dict(best, _skip_discs=sorted(skip))
            logger.info(
                f"{OFF}Smart discography: {_describe(best)}: leaving out disc(s) "
                f"{', '.join(map(str, sorted(skip)))} (live/demo)"
            )
        picked.append(best)
    return picked


def format_duration(duration):
    """
    Formats a duration given in seconds into a HH:MM:SS string.
    
    Args:
        duration (int): The duration in seconds.
        
    Returns:
        str: The formatted time string.
    """
    return time.strftime("%H:%M:%S", time.gmtime(duration))


def create_and_return_dir(directory):
    """
    Safely creates a directory path and returns its absolute path.
    
    Args:
        directory (str): The desired directory path.
        
    Returns:
        str: The absolute path of the created directory.
    """
    fix = os.path.abspath(os.path.expanduser(directory))
    os.makedirs(fix, exist_ok=True)
    return fix


def get_url_info(url):
    """
    Parses a Qobuz URL to extract the media type and ID using regular expressions.

    Compatible with urls of the form:
        https://www.qobuz.com/us-en/{type}/{name}/{id}
        https://open.qobuz.com/{type}/{id}
        https://play.qobuz.com/{type}/{id}
        /us-en/{type}/-/{id}
        
    Args:
        url (str): The input URL string.
        
    Returns:
        tuple: A tuple containing the extracted type (e.g., 'album', 'track') and the ID.
    """
    r = re.search(
        r"(?:https:\/\/(?:w{3}|open|play)\.qobuz\.com)?(?:\/[a-z]{2}-[a-z]{2})"
        r"?\/(album|artist|track|playlist|label)(?:\/[-\w\d]+)?\/([\w\d]+)",
        url,
    )
    return r.groups()


def get_album_artist(qobuz_album: dict) -> list:
    """
    Extracts the album's main artists from the Qobuz API response.
    Returns a list of strings to ensure true Native Multi-Artist Tagging 
    (discrete Vorbis Comments for FLAC files).
    
    Args:
        qobuz_album (dict): Qobuz API response dictionary.
        
    Returns:
        list: A list of the album's main artists.
    """
    try:
        # Se la chiave 'artists' non esiste, ritorna il singolo artista in una lista
        if not qobuz_album.get("artists"):
            single_artist = qobuz_album.get("artist", {}).get("name", "")
            return [single_artist] if single_artist else []

        # Filtra l'array isolando solo chi ha il ruolo 'main-artist'
        main_artists = list(filter(lambda a: "main-artist" in a.get("roles", []),
                                   qobuz_album.get("artists", [])))
        
        # Estrae i nomi puri e li restituisce come lista separata
        if main_artists:
            return [a["name"] for a in main_artists]
        else:
            single_artist = qobuz_album.get("artist", {}).get("name", "")
            return [single_artist] if single_artist else []
            
    except Exception as e:
        logger.error(f"Error getting album artist: {str(e)}")
        single_artist = qobuz_album.get("artist", {}).get("name", "")
        return [single_artist] if single_artist else []


def apply_legacy_charmap(filename: str) -> str:
    """
    Applies legacy character replacement rules for Windows path compatibility.
    Specifically requested for users who prefer standard ASCII over Unicode fullwidth characters.
    
    Args:
        filename (str): The raw string.
        
    Returns:
        str: The sanitized string utilizing basic ASCII replacements.
    """
    # Specific rules requested by the community (JosiahDanger)
    filename = filename.replace(':', '-')
    filename = filename.replace('?', '')
    
    # Standard legacy replacements for other invalid Windows characters
    filename = filename.replace('/', '-')
    filename = filename.replace('\\', '-')
    filename = filename.replace('*', '-')
    filename = filename.replace('"', "'")
    filename = filename.replace('<', '[')
    filename = filename.replace('>', ']')
    filename = filename.replace('|', '-')
    
    # Clean up potential double dashes created by multiple replacements (e.g., "A / B" -> "A - B")
    filename = re.sub(r'\s*-\s*-+', ' -', filename)
    
    return filename


def clean_filename(filename: str, legacy_charmap: bool = False) -> str:
    """
    Cleans up redundant special characters, spaces, and separators in filenames.
    Normalizes Unicode characters to NFC form to ensure cross-platform safety.
    
    Args:
        filename (str): The raw string to clean.
        legacy_charmap (bool, optional): If True, uses basic ASCII replacements instead of Unicode full-width characters. Defaults to False.
        
    Returns:
        str: The fully sanitized filename string.
    """
    # First normalize the Unicode string to NFC form
    filename = unicodedata.normalize('NFC', filename)
    
    # Clean up redundant spaces, separators, and brackets

    # Merge multiple separators (supports spaces, commas, periods, Chinese commas, colons, semicolons, vertical bars, slashes, backslashes, underscores. Does not support the - symbol) into one
    filename = re.sub(r'(?:\s*([,\.\:\;\|/\\_])\s*){2,}', r'\1 ', filename)

    # Define all paired bracket patterns
    patterns = [
        # Handle paired brackets containing only special characters
        (r'\(\s*\W*\s*\)', ''),  # (...)
        (r'\[\s*\W*\s*\]', ''),  # [...]
        (r'\{\s*\W*\s*\}', ''),  # {...}
        (r'<\s*\W*\s*>', ''),  # <...>
        (r'《\s*\W*\s*》', ''),  # 《...》
        (r'〈\s*\W*\s*〉', ''),  # 〈...〉
        (r'「\s*\W*\s*」', ''),  # 「...」
        (r'『\s*\W*\s*』', ''),  # 『...』
        (r'（\s*\W*\s*）', ''),  # （...）
        (r'［\s*\W*\s*］', ''),  # ［...］
        (r'【\s*\W*\s*】', ''),  # 【...】

        # Handle edge cases - remove all special characters and spaces at boundaries
        # If a left bracket is followed by a separator, or a separator is followed by a right bracket, remove them
        (r'(?<=[\(\[\{<《〈「『（［【])(\s*[,\.\:\;\|/\\_]\s*)\b', ''),
        (r'\b(\s*[,\.\:\;\|/\\_]\s*)(?=[】］）』」〉》>\}\]\)])', ''),
    ]

    # Apply each pattern sequentially
    for pattern, replacement in patterns:
        filename = re.sub(pattern, replacement, filename)

    # Merge multiple spaces
    filename = re.sub(r'\s+', ' ', filename)
    
    # Strip trailing dots and spaces
    filename = filename.strip().strip(".").strip()
    
    # --- NEW LOGIC FOR LEGACY CHARMAP ---
    if legacy_charmap:
        return apply_legacy_charmap(filename)
    else:
        return invalid_chars_to_fullwidth(filename)


def invalid_chars_to_fullwidth(filename):
    """
    Converts illegal Windows filename characters to visually similar full-width Unicode characters.
    
    Args:
        filename (str): The raw string.
        
    Returns:
        str: The safely converted string.
    """
    # Illegal characters to full-width characters
    invalid_to_fullwidth = {
        '/': '／',
        '\\': '＼',
        ':': '：',
        '*': '＊',
        '?': '？',
        '"': '＂',
        '<': '＜',
        '>': '＞',
        '|': '｜',
    }

    for invalid_char, fullwidth_char in invalid_to_fullwidth.items():
        filename = filename.replace(invalid_char, fullwidth_char)
    return filename