import base64
import logging
import re
from collections import OrderedDict
from urllib.parse import urljoin

from requests import Session

# Modified code based on DashLt's spoofbuz

logger = logging.getLogger(__name__)

_SEED_TIMEZONE_REGEX = re.compile(
    r'[a-z]\.initialSeed\("(?P<seed>[\w=]+)",window\.utimezone\.(?P<timezone>[a-z]+)\)'
)
_INFO_EXTRAS_REGEX = r'name:"\w+/(?P<timezone>{timezones})",info:"(?P<info>[\w=]+)",extras:"(?P<extras>[\w=]+)"'
# Only the production appId is needed; tolerate whitespace and whatever
# follows it, since the minified config around it has changed before.
_APP_ID_REGEX = re.compile(
    r'production:\s*\{\s*api:\s*\{\s*appId:\s*"(?P<app_id>\d+)"'
)
# Any script whose src mentions "bundle" and ends in .js: don't pin the
# version folder or the tag's other attributes, both have changed before.
_BUNDLE_URL_REGEX = re.compile(
    r"""<script[^>]+src=["']([^"']*bundle[^"']*\.js)["']"""
)

_BASE_URL = "https://play.qobuz.com"
# The default python-requests User-Agent can be served a different page.
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
}


class Bundle:
    def __init__(self):
        self._session = Session()
        self._session.headers.update(_HEADERS)

        logger.debug("Getting login page")
        response = self._session.get(f"{_BASE_URL}/login")
        response.raise_for_status()

        srcs = _BUNDLE_URL_REGEX.findall(response.text)
        if not srcs:
            raise NotImplementedError(
                f"Couldn't find bundle.js on {_BASE_URL}/login "
                f"(HTTP {response.status_code})"
            )
        self.bundle_urls = [
            urljoin(_BASE_URL + "/", src) for src in dict.fromkeys(srcs)
        ]

        # Use the first bundle that carries the app config.
        for url in self.bundle_urls:
            logger.debug(f"Getting bundle {url}")
            response = self._session.get(url)
            response.raise_for_status()
            self._bundle = response.text
            if _APP_ID_REGEX.search(self._bundle):
                break

    def get_app_id(self):
        match = _APP_ID_REGEX.search(self._bundle)
        if not match:
            raise NotImplementedError(
                "Failed to match APP ID in " + ", ".join(self.bundle_urls)
            )

        return match.group("app_id")

    def get_secrets(self):
        logger.debug("Getting secrets")
        seed_matches = _SEED_TIMEZONE_REGEX.finditer(self._bundle)
        secrets = OrderedDict()

        for match in seed_matches:
            seed, timezone = match.group("seed", "timezone")
            secrets[timezone] = [seed]

        keypairs = list(secrets.items())
        if len(keypairs) < 2:
            raise NotImplementedError(
                "Failed to match app secrets in " + ", ".join(self.bundle_urls)
            )
        secrets.move_to_end(keypairs[1][0], last=False)
        info_extras_regex = _INFO_EXTRAS_REGEX.format(
            timezones="|".join([timezone.capitalize() for timezone in secrets])
        )
        info_extras_matches = re.finditer(info_extras_regex, self._bundle)
        for match in info_extras_matches:
            timezone, info, extras = match.group("timezone", "info", "extras")
            secrets[timezone.lower()] += [info, extras]
        for secret_pair in secrets:
            secrets[secret_pair] = base64.standard_b64decode(
                "".join(secrets[secret_pair])[:-44]
            ).decode("utf-8")
        return secrets
