import configparser
import hashlib
import json
import logging
import glob
import os
import sys
from getpass import getpass

from qobuz_dl.bundle import Bundle
from qobuz_dl.color import GREEN, RED, YELLOW
from qobuz_dl.commands import qobuz_dl_args
from qobuz_dl.core import QobuzDL
from qobuz_dl.downloader import DEFAULT_FOLDER, DEFAULT_TRACK

logging.basicConfig(
    level=logging.INFO,
    format="%(message)s",
)

if os.name == "nt":
    OS_CONFIG = os.environ.get("APPDATA")
else:
    OS_CONFIG = os.path.join(os.environ["HOME"], ".config")

CONFIG_PATH = os.path.join(OS_CONFIG, "qobuz-dl")
CONFIG_FILE = os.path.join(CONFIG_PATH, "config.ini")
QOBUZ_DB = os.path.join(CONFIG_PATH, "qobuz_dl.db")

TOKEN_HELP = (
    "Qobuz no longer accepts email/password logins from third-party apps,\n"
    "so qobuz-dl signs in with the auth token from your browser:\n"
    "  1. Log in at https://play.qobuz.com\n"
    "  2. Press F12 and open Application (Chrome/Edge) or Storage (Firefox)\n"
    "  3. Under Local Storage > https://play.qobuz.com, select 'localuser'\n"
    "  4. Copy the value of 'token' (pasting the whole 'localuser' value "
    "also works)"
)


def _read_token(raw):
    """Accept the bare token, or the whole 'localuser' JSON copied from the
    browser's local storage."""
    raw = raw.strip().strip("\"'")
    if not raw.startswith("{"):
        return raw
    try:
        data = json.loads(raw)
    except ValueError:
        return raw
    token = data.get("token") or data.get("user_auth_token")
    if not token:
        sys.exit(
            f"{RED}Couldn't find a token in what you pasted. Copy just the "
            "'token' value and run 'qobuz-dl -r' again."
        )
    return str(token)


def _get_app_tokens(previous):
    """Read the app ID and secrets from the web player. If that fails (Qobuz
    changes the bundle now and then), keep the ones from the old config."""
    try:
        bundle = Bundle()
        return str(bundle.get_app_id()), ",".join(bundle.get_secrets().values())
    except Exception as error:
        app_id = previous["DEFAULT"].get("app_id", "")
        secrets = previous["DEFAULT"].get("secrets", "")
        if not (app_id and secrets):
            sys.exit(
                f"{RED}Couldn't read the app ID and secrets from the Qobuz web "
                f"player: {error}"
            )
        logging.warning(
            f"{YELLOW}Couldn't read fresh app tokens from the Qobuz web player "
            f"({error}).\nKeeping the app ID and secrets from your existing "
            "config."
        )
        return app_id, secrets


def _reset_config(config_file):
    logging.info(f"{YELLOW}Creating config file: {config_file}")
    previous = configparser.ConfigParser()
    try:
        previous.read(config_file)
    except (configparser.Error, UnicodeDecodeError):
        pass  # a corrupted config just means there's nothing to fall back on
    config = configparser.ConfigParser()
    logging.info(f"{YELLOW}{TOKEN_HELP}")
    token = _read_token(
        getpass(
            "Paste your auth token (input is hidden; leave empty to use "
            "email/password instead)\n- "
        )
    )
    config["DEFAULT"]["user_auth_token"] = token
    if token:
        logging.info(f"{GREEN}Token received ({len(token)} characters).")
        config["DEFAULT"]["email"] = ""
        config["DEFAULT"]["password"] = ""
    else:
        config["DEFAULT"]["email"] = input("Enter your email:\n- ")
        password = getpass("Enter your password (input is hidden)\n- ")
        config["DEFAULT"]["password"] = hashlib.md5(
            password.encode("utf-8")
        ).hexdigest()
    config["DEFAULT"]["default_folder"] = (
        input("Folder for downloads (leave empty for default 'Qobuz Downloads')\n- ")
        or "Qobuz Downloads"
    )
    config["DEFAULT"]["default_quality"] = (
        input(
            "Download quality (5, 6, 7, 27) "
            "[320, LOSSLESS, 24B <96KHZ, 24B >96KHZ]"
            "\n(leave empty for default '6')\n- "
        )
        or "6"
    )
    config["DEFAULT"]["default_limit"] = "20"
    config["DEFAULT"]["no_m3u"] = "false"
    config["DEFAULT"]["albums_only"] = "false"
    config["DEFAULT"]["no_fallback"] = "false"
    config["DEFAULT"]["og_cover"] = "false"
    config["DEFAULT"]["embed_art"] = "false"
    config["DEFAULT"]["no_cover"] = "false"
    config["DEFAULT"]["no_database"] = "false"
    logging.info(f"{YELLOW}Getting tokens. Please wait...")
    app_id, secrets = _get_app_tokens(previous)
    config["DEFAULT"]["app_id"] = app_id
    config["DEFAULT"]["secrets"] = secrets
    config["DEFAULT"]["folder_format"] = DEFAULT_FOLDER
    config["DEFAULT"]["track_format"] = DEFAULT_TRACK
    config["DEFAULT"]["smart_discography"] = "false"
    with open(config_file, "w") as configfile:
        config.write(configfile)
    logging.info(
        f"{GREEN}Config file updated. Edit more options in {config_file}"
        "\nso you don't have to call custom flags every time you run "
        "a qobuz-dl command."
    )


def _remove_leftovers(directory):
    directory = os.path.join(directory, "**", ".*.tmp")
    for i in glob.glob(directory, recursive=True):
        try:
            os.remove(i)
        except:  # noqa
            pass


def _handle_commands(qobuz, arguments):
    try:
        if arguments.command == "dl":
            qobuz.download_list_of_urls(arguments.SOURCE)
        elif arguments.command == "lucky":
            query = " ".join(arguments.QUERY)
            qobuz.lucky_type = arguments.type
            qobuz.lucky_limit = arguments.number
            qobuz.lucky_mode(query)
        else:
            qobuz.interactive_limit = arguments.limit
            qobuz.interactive()

    except KeyboardInterrupt:
        logging.info(
            f"{RED}Interrupted by user\n{YELLOW}Already downloaded items will "
            "be skipped if you try to download the same releases again."
        )

    finally:
        _remove_leftovers(qobuz.directory)


def _initial_checks():
    if not os.path.isdir(CONFIG_PATH) or not os.path.isfile(CONFIG_FILE):
        os.makedirs(CONFIG_PATH, exist_ok=True)
        _reset_config(CONFIG_FILE)

    if len(sys.argv) < 2:
        sys.exit(qobuz_dl_args().print_help())


def main():
    _initial_checks()

    config = configparser.ConfigParser()
    config.read(CONFIG_FILE)

    try:
        email = config["DEFAULT"]["email"]
        password = config["DEFAULT"]["password"]
        # Configs written before token login don't have this key.
        user_auth_token = config["DEFAULT"].get("user_auth_token", "")
        default_folder = config["DEFAULT"]["default_folder"]
        default_limit = config["DEFAULT"]["default_limit"]
        default_quality = config["DEFAULT"]["default_quality"]
        no_m3u = config.getboolean("DEFAULT", "no_m3u")
        albums_only = config.getboolean("DEFAULT", "albums_only")
        no_fallback = config.getboolean("DEFAULT", "no_fallback")
        og_cover = config.getboolean("DEFAULT", "og_cover")
        embed_art = config.getboolean("DEFAULT", "embed_art")
        no_cover = config.getboolean("DEFAULT", "no_cover")
        no_database = config.getboolean("DEFAULT", "no_database")
        app_id = config["DEFAULT"]["app_id"]
        smart_discography = config.getboolean("DEFAULT", "smart_discography")
        folder_format = config["DEFAULT"]["folder_format"]
        track_format = config["DEFAULT"]["track_format"]

        secrets = [
            secret for secret in config["DEFAULT"]["secrets"].split(",") if secret
        ]
        arguments = qobuz_dl_args(
            default_quality, default_limit, default_folder
        ).parse_args()
    except (KeyError, UnicodeDecodeError, configparser.Error) as error:
        arguments = qobuz_dl_args().parse_args()
        if not arguments.reset:
            sys.exit(
                f"{RED}Your config file is corrupted: {error}! "
                "Run 'qobuz-dl -r' to fix this."
            )

    if arguments.reset:
        sys.exit(_reset_config(CONFIG_FILE))

    if arguments.show_config:
        print(f"Configuation: {CONFIG_FILE}\nDatabase: {QOBUZ_DB}\n---")
        with open(CONFIG_FILE, "r") as f:
            for line in f:
                # Don't print login secrets: this output gets pasted around.
                key, sep, value = line.partition("=")
                if sep and value.strip() and key.strip() in (
                    "user_auth_token",
                    "password",
                ):
                    line = f"{key}= <hidden>\n"
                print(line, end="")
        sys.exit()

    if arguments.purge:
        try:
            os.remove(QOBUZ_DB)
        except FileNotFoundError:
            pass
        sys.exit(f"{GREEN}The database was deleted.")

    qobuz = QobuzDL(
        arguments.directory,
        arguments.quality,
        arguments.embed_art or embed_art,
        ignore_singles_eps=arguments.albums_only or albums_only,
        no_m3u_for_playlists=arguments.no_m3u or no_m3u,
        quality_fallback=not arguments.no_fallback or not no_fallback,
        cover_og_quality=arguments.og_cover or og_cover,
        no_cover=arguments.no_cover or no_cover,
        downloads_db=None if no_database or arguments.no_db else QOBUZ_DB,
        folder_format=arguments.folder_format or folder_format,
        track_format=arguments.track_format or track_format,
        smart_discography=arguments.smart_discography or smart_discography,
    )
    qobuz.initialize_client(email, password, app_id, secrets, user_auth_token)

    _handle_commands(qobuz, arguments)


if __name__ == "__main__":
    sys.exit(main())
